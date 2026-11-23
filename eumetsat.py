# eumetsat.py — acceso al Data Store de EUMETSAT con la API v2 (OAuth2) para bajar UN trozo del FCI L1c.
# -----------------------------------------------------------------------------
# Token: la API v2 se autentica con el usuario del portal (flujo con navegador). Aquí solo se guarda el
# REFRESH TOKEN: cada renovación devuelve uno nuevo e invalida el anterior (comprobado el 23-nov-2026).
#   - El refresh vigente vive en memoria (self._rt) y en disco (DATA_DIR/tokens.json, escrito con fsync).
#     Si el disco falla, se sigue con el de memoria y se reintenta guardarlo en cada uso (error visible).
#   - Un candidato nuevo (POST /admin/token) se PRUEBA antes de sustituir al bueno.
#   - Cerrojo entre procesos (flock) durante la renovación: dos procesos no deben rotar el mismo token.
#   - Como mucho una renovación forzada (por un 401) cada 15 min.
# Nunca se escriben los tokens en el log ni en las respuestas.
# -----------------------------------------------------------------------------
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

try:
    import fcntl                     # Linux (Render); en Windows (pruebas locales) no hay cerrojo entre procesos
except ImportError:                  # pragma: no cover
    fcntl = None

API = "https://api.eumetsat.int"
CAS_TOKEN = "https://user.eumetsat.int/cas/oauth2.0/accessToken"
CLIENT_ID = "api-public-auth-code"
UTC = dt.timezone.utc
PLAZO_DESCARGA_S = 150               # tiempo TOTAL máximo de una descarga de ~19 MB
MIN_ENTRE_FORZADAS_S = 900


class TokenError(Exception):
    """Fallo del token. permanente=True: hay que volver a entrar en EUMETSAT; False: fallo pasajero."""

    def __init__(self, msg: str, permanente: bool = True):
        super().__init__(msg)
        self.permanente = permanente


def escribir_atomico(ruta: Path, texto: str) -> None:
    """Escribe con fichero temporal + fsync + os.replace (+ fsync de la carpeta). No deja temporales."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(ruta.parent), prefix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(texto)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ruta)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError, AttributeError):
        dfd = os.open(str(ruta.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


class Token:
    """Access token de la API v2 renovado con el refresh token (memoria + disco)."""

    def __init__(self, ruta: Path):
        self.ruta = ruta
        self._lock = threading.Lock()
        self._rt: Optional[str] = None
        self._sin_guardar = False
        self._ultima_forzada = 0.0
        self.access: Optional[str] = None
        self.caduca: Optional[float] = None
        self.margen = 1800.0
        self.ultimo_refresco: Optional[str] = None
        self.error: Optional[str] = None
        self.error_permanente = False
        self.cerrando = False                 # al apagar: no empezar renovaciones nuevas
        self._ultimo_intento_muerto = 0.0
        with contextlib.suppress(OSError, ValueError, KeyError, TypeError, AttributeError):
            self._rt = json.loads(self.ruta.read_text(encoding="utf-8"))["refresh_token"] or None

    # ---------- disco
    def _guardar(self, rt: str) -> None:
        try:
            escribir_atomico(self.ruta, json.dumps({"refresh_token": rt, "guardado": dt.datetime.now(UTC).isoformat()}))
            self._sin_guardar = False
        except OSError:
            self._sin_guardar = True          # se sigue con el de memoria y se reintenta en el próximo uso

    @contextlib.contextmanager
    def _cerrojo(self):
        if fcntl is None:
            yield
            return
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ruta.with_suffix(".lock"), "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def hay_refresh(self) -> bool:
        return bool(self._rt)

    # ---------- renovación
    def _pedir(self, rt: str) -> dict:
        try:
            r = requests.post(CAS_TOKEN, data={"grant_type": "refresh_token", "refresh_token": rt,
                                               "client_id": CLIENT_ID}, timeout=(5, 15))
        except requests.RequestException as e:
            raise TokenError(f"renovación sin respuesta ({type(e).__name__})", permanente=False)
        if r.status_code in (400, 401, 403):
            raise TokenError(f"renovación rechazada (HTTP {r.status_code}): hay que volver a entrar en EUMETSAT")
        if r.status_code != 200:
            raise TokenError(f"renovación fallida (HTTP {r.status_code}), se reintentará", permanente=False)
        try:
            d = r.json()
        except ValueError:
            raise TokenError("renovación: respuesta que no es JSON, se reintentará", permanente=False)
        if not d.get("access_token") or not d.get("refresh_token"):
            raise TokenError("renovación sin tokens en la respuesta", permanente=False)
        return d

    def _renovar(self, candidato: Optional[str] = None) -> None:
        if self.cerrando:
            raise TokenError("apagando: no se renueva", permanente=False)
        if candidato is None and self.error_permanente and time.time() - self._ultimo_intento_muerto < 1800:
            raise TokenError(self.error or "token rechazado", permanente=True)   # no insistir: esperar a /admin/token
        with self._cerrojo():
            if candidato is None:
                # otro proceso pudo rotarlo: el del disco manda si es distinto
                with contextlib.suppress(OSError, ValueError, KeyError, TypeError, AttributeError):
                    disco = json.loads(self.ruta.read_text(encoding="utf-8"))["refresh_token"]
                    if disco and disco != self._rt and not self._sin_guardar:
                        self._rt = disco
            rt = candidato or self._rt
            if not rt:
                self.error, self.error_permanente = "no hay refresh token: cargar uno con POST /admin/token", True
                raise TokenError(self.error)
            try:
                d = self._pedir(rt)
            except TokenError as e:
                if candidato is None:          # un candidato malo no estropea el estado del bueno
                    self.error, self.error_permanente = str(e), e.permanente
                    if e.permanente:
                        self._ultimo_intento_muerto = time.time()
                raise
            # PRIMERO dejar a salvo el refresh nuevo (el viejo ya no vale)
            self._rt = d["refresh_token"]
            self._guardar(self._rt)
        try:
            exp = min(86400, max(300, int(d.get("expires_in") or 28800)))
        except (TypeError, ValueError):
            exp = 28800
        self.access = d["access_token"]
        self.caduca = time.time() + exp
        self.margen = min(1800.0, exp / 2)
        self.ultimo_refresco = dt.datetime.now(UTC).isoformat(timespec="seconds")
        self.error, self.error_permanente = None, False

    def cargar_refresh(self, refresh_token: str) -> None:
        """Carga un refresh token nuevo (POST /admin/token): solo sustituye al vigente si funciona."""
        rt = (refresh_token or "").strip()
        if not rt:
            raise TokenError("refresh token vacío")
        with self._lock:
            self._renovar(candidato=rt)

    def get(self, forzar: bool = False) -> str:
        with self._lock:
            if self._sin_guardar and self._rt:
                self._guardar(self._rt)
            if forzar and time.time() - self._ultima_forzada < MIN_ENTRE_FORZADAS_S and self.access:
                forzar = False                 # un 401 permanente no debe rotar el token cada minuto
            if forzar:
                self._ultima_forzada = time.time()
            if forzar or not self.access or not self.caduca or time.time() > self.caduca - self.margen:
                try:
                    self._renovar()
                except TokenError:
                    # con un access aún válido se sigue con él (si el refresh está muerto, /health ya dice
                    # «sin_token» y da tiempo a volver a entrar); se reintenta en el próximo uso
                    if not self.access or not self.caduca or time.time() > self.caduca - 60:
                        raise
            return self.access

    def esperar_libre(self, segundos: float) -> None:
        """Al apagar: impide renovaciones nuevas y espera a que termine la que esté en curso."""
        self.cerrando = True
        if self._lock.acquire(timeout=segundos):
            self._lock.release()

    def estado(self) -> dict:
        ok = self.error is None and self.hay_refresh() and not self._sin_guardar
        return {"ok": ok, "sin_guardar": self._sin_guardar,
                "pasajero": bool(self.error) and not self.error_permanente,
                "caduca": dt.datetime.fromtimestamp(self.caduca, UTC).isoformat(timespec="seconds") if self.caduca else None,
                "ultimo_refresco": self.ultimo_refresco,
                "error": self.error or ("el token renovado no se ha podido guardar en disco" if self._sin_guardar else None),
                "volver_a_entrar": bool(self.error and self.error_permanente) or not self.hay_refresh()}


def _slot_de(fecha_ini: dt.datetime) -> dt.datetime:
    t = fecha_ini + dt.timedelta(minutes=2)          # inicios a xx:x0:07; tolera pequeñas variaciones
    return t.replace(minute=t.minute // 10 * 10, second=0, microsecond=0)


def buscar(coleccion: str, desde: dt.datetime, hasta: dt.datetime) -> list[dict]:
    """Discos de la colección con inicio entre desde y hasta (búsqueda pública), del más antiguo al más nuevo."""
    r = requests.get(f"{API}/data/search-products/2.0.0/os", params={
        "pi": coleccion, "format": "json", "c": 100, "sort": "start,time,1",
        "dtstart": desde.strftime("%Y-%m-%dT%H:%M:%SZ"), "dtend": hasta.strftime("%Y-%m-%dT%H:%M:%SZ")}, timeout=60)
    r.raise_for_status()
    out = {}
    for f in r.json().get("features", []):
        ini = dt.datetime.fromisoformat(f["properties"]["date"].split("/")[0].replace("Z", "+00:00"))
        entradas = [e["title"] for e in f["properties"].get("links", {}).get("sip-entries", [])]
        if not any("CHK-BODY" in e for e in entradas):
            continue                    # productos solo con «quicklook» (aviso de la FAQ del Data Store)
        out.setdefault(_slot_de(ini), {"id": f["id"], "slot": _slot_de(ini), "entradas": entradas})
    return [out[k] for k in sorted(out)]


def tiempos_trozo(nombre: str) -> dict:
    """Del nombre del trozo: creación del fichero y barrido (inicio-fin) de ESE trozo."""
    m = re.search(r"_C_EUMT_(\d{14})_IDPFI_OPE_(\d{14})_(\d{14})", nombre)
    if not m:
        return {}
    f = lambda s: dt.datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    return {"creado": f(m[1]), "barrido_ini": f(m[2]), "barrido_fin": f(m[3])}


class TrozoNoDisponible(Exception):
    """Fallo permanente de ese barrido (no tiene el trozo): no tiene sentido reintentar."""


def bajar_trozo(coleccion: str, producto: dict, trozo: int, token: Token, destino: Path) -> tuple[Path, str]:
    nombre = next((e for e in producto["entradas"] if e.endswith(f"_{trozo:04d}.nc") and "CHK-BODY" in e), None)
    if not nombre:
        raise TrozoNoDisponible(f"el producto no tiene el trozo {trozo}")
    url = (f"{API}/data/download/2.0.0/collections/{quote(coleccion, safe='')}/products/"
           f"{quote(producto['id'], safe='')}/entry?name={quote(nombre, safe='')}")
    for intento in range(2):
        with requests.get(url, headers={"Authorization": "Bearer " + token.get(forzar=intento == 1)},
                          timeout=(10, 60), stream=True) as r:
            if r.status_code == 401:
                if intento == 0:
                    continue            # token caducado antes de tiempo: renovar (con límite) y repetir una vez
                raise TokenError("401 también con el token renovado", permanente=False)
            r.raise_for_status()
            destino.parent.mkdir(parents=True, exist_ok=True)
            # plazo TOTAL: un temporizador cierra la conexión aunque el servidor vaya «a goteo»
            vencido = threading.Event()
            reloj = threading.Timer(PLAZO_DESCARGA_S, lambda: (vencido.set(), r.close()))
            reloj.daemon = True
            reloj.start()
            try:
                with open(destino, "wb") as f:
                    for bloque in r.iter_content(64 << 10):
                        f.write(bloque)
            except Exception:
                if vencido.is_set():
                    raise TimeoutError(f"descarga de más de {PLAZO_DESCARGA_S} s")
                raise
            finally:
                reloj.cancel()
            if vencido.is_set():
                raise TimeoutError(f"descarga de más de {PLAZO_DESCARGA_S} s")
        with open(destino, "rb") as f:
            if f.read(4) != b"\x89HDF":
                raise ValueError("lo descargado no es un netCDF4/HDF5")
        return destino, nombre
    raise TokenError("sin respuesta válida tras renovar el token", permanente=False)
