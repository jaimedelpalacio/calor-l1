# servicio.py — bucle de vigilancia: busca cada barrido nuevo de MTG FCI L1c, baja el trozo de la zona,
# lo analiza (motor.py) y guarda el resultado.
# -----------------------------------------------------------------------------
# Estado en DATA_DIR (disco persistente en Render):
#   tokens.json      refresh token de EUMETSAT (ver eumetsat.py)
#   estado.json      últimos barridos analizados (24 h) y el último slot
#   tiempos.csv      por barrido: paso del satélite, fichero creado, visto en el catálogo, analizado
# Al arrancar rehace la última hora EN SILENCIO (sin añadir resultados) para tener fondo y no quedar
# ciego; si el último slot guardado tiene más de 3 h, empieza de nuevo por la última hora.
#
# /health dice «ok» SOLO si de verdad está vigilando: token bien, disco persistente, bucle vivo,
# barrido analizado hace < 30 min, sin fallos seguidos y con fondo suficiente (no en arranque).
# -----------------------------------------------------------------------------
from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

import eumetsat as E
from motor import MIN_VALIDOS, Motor, Parametros, Zona, leer_trozo

UTC = dt.timezone.utc
log = logging.getLogger("calor-l1")
MAX_BARRIDOS = 144                       # 24 h de resultados en estado.json
REINTENTO_MAX = dt.timedelta(minutes=30)  # se reintenta un barrido fallido hasta 30 min desde que se vio
MAX_PIX = 50                             # píxeles con calor que se guardan por barrido (se cuenta el total)
ATRASO_MIN = 30
PARADO_MIN = 8                           # un barrido lento (401 + renovación + descarga) puede tardar ~5 min


def _ahora() -> dt.datetime:
    return dt.datetime.now(UTC)


def _iso(t: Optional[dt.datetime]) -> Optional[str]:
    return t.isoformat(timespec="seconds").replace("+00:00", "Z") if t else None


def _fecha(s) -> Optional[dt.datetime]:
    if not s:
        return None
    t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


class Servicio:
    def __init__(self):
        self.coleccion = os.getenv("COLECCION", "EO:EUM:DAT:0665")
        self.trozo = int(os.getenv("TROZO", "34"))
        self.intervalo = int(os.getenv("INTERVALO_S", "60"))
        self.exigir_disco = os.getenv("EXIGIR_DISCO", "1") == "1"
        base = Path(__file__).parent
        self.zona = Zona(os.getenv("ZONA_GEOJSON", str(base / "zonas" / "alcala.geojson")),
                         float(os.getenv("MARGEN_KM", "5")), os.getenv("ZONA_NOMBRE") or None)
        self.p = Parametros(umbral=float(os.getenv("UMBRAL", "4.5")), intenso=float(os.getenv("INTENSO", "8")))
        self.motor = Motor(self.zona, self.p)
        self.datos, self.disco_ok = self._carpeta()
        # descargas FUERA del disco persistente, en una carpeta propia de este proceso
        self.tmp = Path(tempfile.mkdtemp(prefix="calor-l1-"))
        self.token = E.Token(self.datos / "tokens.json")
        self.inicio = _ahora()
        self.barridos: list[dict] = []
        self.ultimo_slot: Optional[dt.datetime] = None        # último slot cerrado (analizado o abandonado)
        self.ultimo_ok: Optional[dict] = None                 # último barrido analizado de verdad EN ESTE PROCESO
        self.pendientes: dict = {}                            # slot -> {"visto": t, "fallos": n, "error": str}
        self.ultima_vuelta: Optional[dt.datetime] = None
        self.latido: dt.datetime = _ahora()                   # se renueva al empezar cada vuelta y cada barrido
        self.error_vuelta: Optional[str] = None
        self.vueltas_fallidas = 0
        self.reconstruido = False
        self._cargar()

    # ---------------------------------------------------------------- disco
    def _carpeta(self) -> tuple[Path, bool]:
        d = Path(os.getenv("DATA_DIR", "/data"))
        try:
            d.mkdir(parents=True, exist_ok=True)
            E.escribir_atomico(d / ".prueba", "ok")
            (d / ".prueba").unlink()
            # en Render, /data debe ser un disco montado; si no, todo se pierde en cada despliegue
            return d, (not self.exigir_disco) or os.stat(str(d)).st_dev != os.stat("/").st_dev
        except OSError:
            alt = Path(tempfile.gettempdir()) / "calor-l1"
            alt.mkdir(parents=True, exist_ok=True)
            log.warning("DATA_DIR %s no se puede escribir; uso %s (se pierde al reiniciar)", d, alt)
            return alt, False

    def _cargar(self) -> None:
        try:
            e = json.loads((self.datos / "estado.json").read_text(encoding="utf-8"))
            buenos = []
            for b in e.get("barridos", []):
                try:
                    if isinstance(b, dict) and _fecha(b.get("slot")):
                        buenos.append(b)
                except (TypeError, ValueError):
                    pass
            self.barridos = buenos[-MAX_BARRIDOS:]
            self.ultimo_slot = _fecha(e.get("ultimo_slot"))
            # ultimo_ok NO se toma del fichero: «ok» exige haber analizado algo en este proceso
        except Exception as ex:                       # estado ilegible: se empieza de cero
            if (self.datos / "estado.json").exists():
                log.warning("estado.json ilegible (%s): se empieza de cero", type(ex).__name__)
            self.barridos, self.ultimo_slot = [], None

    def _guardar(self) -> None:
        try:
            E.escribir_atomico(self.datos / "estado.json",
                               json.dumps({"ultimo_slot": _iso(self.ultimo_slot), "barridos": self.barridos}, ensure_ascii=False))
        except OSError as ex:
            log.error("no se pudo guardar estado.json: %s", ex)

    def _apuntar_tiempos(self, r: dict) -> None:
        f = self.datos / "tiempos.csv"
        with contextlib.suppress(OSError):
            nuevo = not f.exists()
            with open(f, "a", encoding="utf-8") as fh:
                if nuevo:
                    fh.write("slot,barrido_fin,creado,visto,analizado,retraso_min,n_calor,nube_pct\n")
                fh.write(f"{r['slot']},{r.get('barrido_fin')},{r.get('creado')},{r.get('visto')},{r.get('analizado')},"
                         f"{r.get('retraso_min')},{r.get('n_calor')},{r.get('nube_pct')}\n")

    def _cerrar(self, r: dict, slot: dt.datetime) -> None:
        self.barridos = (self.barridos + [r])[-MAX_BARRIDOS:]
        self.ultimo_slot = slot
        self.pendientes.pop(slot, None)
        self._guardar()

    # ---------------------------------------------------------------- análisis de un barrido
    def analizar(self, prod: dict, visto: dt.datetime, guardar: bool = True) -> dict:
        tmp = self.tmp / f"{prod['slot']:%Y%m%d%H%M}_{self.trozo}.nc"
        try:
            ruta, nombre = E.bajar_trozo(self.coleccion, prod, self.trozo, self.token, tmp)
            b38, b105, rec = leer_trozo(str(ruta), self.zona)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
        t = E.tiempos_trozo(nombre)
        res = self.motor.procesa(prod["slot"], b38, b105, rec)
        fin = t.get("barrido_fin")
        ahora = _ahora()
        calor = res.pop("calor")
        arranque = res["fondo_barridos"] < MIN_VALIDOS
        r = {"slot": _iso(prod["slot"]), "barrido_ini": _iso(t.get("barrido_ini")), "barrido_fin": _iso(fin),
             "creado": _iso(t.get("creado")), "visto": _iso(visto), "analizado": _iso(ahora),
             "retraso_min": round((ahora - fin).total_seconds() / 60, 1) if fin else None,
             "arranque": arranque, "vigilado": (not arranque) and res["pix_vigilados"] > 0,
             **res, "n_calor": len(calor), "calor": calor[:MAX_PIX]}
        if guardar:
            self._cerrar(r, prod["slot"])
            self.ultimo_ok = r
            self._apuntar_tiempos(r)
        return r

    def _reconstruir(self) -> None:
        """Rehace en silencio la hora anterior al último slot guardado (fondo para la anomalía temporal)."""
        if not self.ultimo_slot or _ahora() - self.ultimo_slot > dt.timedelta(hours=3):
            self.ultimo_slot = None
            self.reconstruido = True
            return
        prods = E.buscar(self.coleccion, self.ultimo_slot - dt.timedelta(minutes=61), self.ultimo_slot + dt.timedelta(minutes=1))
        for p in prods:
            self.latido = _ahora()
            try:
                self.analizar(p, _ahora(), guardar=False)
            except E.TokenError:
                raise
            except Exception as ex:                     # un hueco no impide seguir
                log.warning("reconstruir %s: %s", p["slot"], ex)
        self.reconstruido = True                         # solo si la búsqueda ha ido bien

    # ---------------------------------------------------------------- una vuelta del bucle
    def vuelta(self) -> None:
        if not self.token.hay_refresh():
            raise E.TokenError("no hay refresh token: cargar uno con POST /admin/token")
        if not self.reconstruido:
            self._reconstruir()
        ahora = _ahora()
        # también la hora anterior al último cerrado: así se ven los barridos publicados tarde (fuera de orden)
        desde = self.ultimo_slot - dt.timedelta(minutes=60) if self.ultimo_slot else ahora - dt.timedelta(minutes=80)
        desde = max(desde, ahora - dt.timedelta(hours=3))
        lista = E.buscar(self.coleccion, desde, ahora)
        en_lista = {p["slot"] for p in lista}
        cerrados = {b.get("slot") for b in self.barridos}
        # olvidar pendientes viejos y los que ya no salen en el catálogo y quedaron atrás
        self.pendientes = {k: v for k, v in self.pendientes.items() if ahora - k < dt.timedelta(hours=3)
                           and (v.get("tarde") or k in en_lista or not self.ultimo_slot or k > self.ultimo_slot)}
        for i, p in enumerate(lista):
            if self.ultimo_slot and p["slot"] <= self.ultimo_slot:
                if p["slot"] < self.ultimo_slot and _iso(p["slot"]) not in cerrados and p["slot"] not in self.pendientes:
                    log.warning("barrido %s publicado tarde, detrás de uno más nuevo: se salta", p["slot"])
                    self.pendientes[p["slot"]] = {"visto": _ahora(), "fallos": 0, "error": "publicado tarde", "tarde": True}
                continue
            self.latido = _ahora()
            hay_mas_nuevos = i < len(lista) - 1
            pend = self.pendientes.setdefault(p["slot"], {"visto": _ahora(), "fallos": 0, "error": None})
            try:
                r = self.analizar(p, pend["visto"])
                log.info("barrido %s: %d píxeles con calor, nubes %s %%, retraso %s min",
                         r["slot"], r["n_calor"], r["nube_pct"], r["retraso_min"])
            except E.TokenError:
                raise
            except Exception as ex:
                pend["fallos"] += 1
                pend["error"] = f"{type(ex).__name__}: {str(ex)[:200]}"
                permanente = isinstance(ex, E.TrozoNoDisponible) or "no cae en este trozo" in str(ex)
                log.warning("barrido %s: fallo %d (%s)", p["slot"], pend["fallos"], pend["error"])
                # con barridos más nuevos esperando: como mucho 3 intentos; si es el último: hasta 30 min
                if not permanente and (pend["fallos"] < 3 if hay_mas_nuevos else _ahora() - pend["visto"] < REINTENTO_MAX):
                    return                                # se reintenta en la próxima vuelta
                self._cerrar({"slot": _iso(p["slot"]), "error": pend["error"], "visto": _iso(pend["visto"]),
                              "analizado": _iso(_ahora()), "arranque": False, "vigilado": False,
                              "pix_vigilados": 0, "n_calor": 0, "calor": []}, p["slot"])

    def bucle(self) -> None:
        while True:
            t0 = time.monotonic()
            self.latido = _ahora()
            try:
                self.vuelta()
                self.error_vuelta = None
                self.vueltas_fallidas = 0
            except Exception as ex:
                self.error_vuelta = f"{type(ex).__name__}: {str(ex)[:300]}"
                self.vueltas_fallidas += 1
                log.error("vuelta: %s\n%s", self.error_vuelta, traceback.format_exc(limit=3))
            self.ultima_vuelta = _ahora()
            time.sleep(max(5.0, self.intervalo - (time.monotonic() - t0)))

    # ---------------------------------------------------------------- para la API
    def salud(self) -> dict:
        ahora = _ahora()
        tok = self.token.estado()
        uok = self.ultimo_ok
        ok_slot = _fecha(uok["slot"]) if uok else None
        edad = round((ahora - (ok_slot + dt.timedelta(minutes=10))).total_seconds() / 60, 1) if ok_slot else None
        en_marcha_min = (ahora - self.inicio).total_seconds() / 60
        pend = dict(self.pendientes)                     # copia: el bucle lo modifica a la vez
        pend_fallos = max([v["fallos"] for v in pend.values() if not v.get("tarde")] or [0])
        ultimo = self.barridos[-1] if self.barridos else None
        sin_latido = (ahora - self.latido).total_seconds() / 60
        if tok["volver_a_entrar"]:
            estado = "sin_token"
        elif not self.disco_ok or tok["sin_guardar"]:
            estado = "sin_disco"
        elif sin_latido > PARADO_MIN:
            estado = "parado"
        elif self.vueltas_fallidas >= 3 or pend_fallos >= 3 or (ultimo and ultimo.get("error")):
            estado = "error"
        elif ok_slot is None or not self.reconstruido:
            estado = "arrancando"
        elif edad is not None and edad > ATRASO_MIN:
            estado = "atrasado"
        elif uok.get("fondo_barridos", 0) >= MIN_VALIDOS and uok.get("pix_vigilados", 0) == 0 \
                and (uok.get("nube_pct") or 0) >= 95:
            estado = "nublado"                           # todo cubierto: no se ve el suelo
        elif not uok.get("vigilado"):
            estado = "arrancando" if en_marcha_min < 45 else "sin_fondo"
        else:
            estado = "ok"
        return {"ok": estado == "ok", "estado": estado, "vigilando": estado == "ok",
                "aviso_token": tok["error"] if tok.get("pasajero") else None,
                "min_sin_latido": round(sin_latido, 1),
                "ultimo_barrido_ok": _iso(ok_slot), "edad_min": edad, "ultimo_slot": _iso(self.ultimo_slot),
                "token": tok, "ultima_vuelta": _iso(self.ultima_vuelta), "error_vuelta": self.error_vuelta,
                "vueltas_fallidas": self.vueltas_fallidas,
                "barridos_reintentando": [{"slot": _iso(k), "fallos": v["fallos"], "error": v["error"]}
                                          for k, v in sorted(pend.items()) if v["fallos"]],
                "barridos_saltados_tarde": [_iso(k) for k, v in sorted(pend.items()) if v.get("tarde")],
                "disco_persistente": self.disco_ok, "zona": self.zona.nombre}
