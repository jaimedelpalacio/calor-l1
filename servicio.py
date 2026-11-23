# servicio.py — bucle de vigilancia: busca cada barrido nuevo de MTG FCI L1c, baja el trozo de la zona,
# lo analiza (motor.py) y guarda el resultado.
# -----------------------------------------------------------------------------
# Estado en DATA_DIR (disco persistente en Render):
#   tokens.json      refresh token de EUMETSAT (ver eumetsat.py)
#   estado.json      últimos barridos (24 h, ordenados por slot) y el último slot cerrado en orden
#   fondo.npz        fondo de la última hora y rachas de calor del motor: al reiniciar se sigue EXACTAMENTE igual
#   tiempos.csv      por barrido: paso del satélite, fichero creado, visto en el catálogo, analizado
# Al arrancar: si fondo.npz cuadra con estado.json, se carga; si no, se rehace la última hora EN SILENCIO
# (sin añadir resultados) bajando sus barridos. Si el último slot guardado tiene más de 3 h, se empieza de nuevo
# por la última hora (lo de en medio queda como «hueco»).
#
# Barridos publicados tarde (detrás de uno más nuevo ya cerrado): si llegan < 30 min después de su fin, se analizan
# y se guardan sin mover el último slot (sirven para el fondo y el registro, no para avisar: el consumidor ya pasó de
# ese slot); si no, quedan apuntados con error. Los slots DETRÁS del último cerrado que no aparecen en el catálogo en
# ese plazo se apuntan como «no analizado (hueco)»: en /estado no falta ningún slot de las últimas 24 h. Los de
# DELANTE que aún no han salido están pendientes, no son hueco (si el catálogo va con retraso, se analizan al salir).
#
# /health dice «ok» SOLO si de verdad está vigilando: token bien, disco persistente, bucle vivo,
# barrido analizado hace < 30 min, sin fallos seguidos, con fondo suficiente y con ≥ 50 % de la zona vigilada.
# -----------------------------------------------------------------------------
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import logging
import os
import tempfile
import time
import traceback
from pathlib import Path
from typing import Optional

import numpy as np

import eumetsat as E
from motor import MARGEN_PX, MAX_PIX, MIN_VALIDOS, VENTANA_MIN, ErrorPermanente, Motor, Parametros, Zona, leer_trozo

UTC = dt.timezone.utc
log = logging.getLogger("calor-l1")
MAX_BARRIDOS = 144                       # 24 h de resultados en estado.json
REINTENTO_MAX = dt.timedelta(minutes=30)  # se reintenta un barrido fallido hasta 30 min desde que se vio
TARDE_MAX = dt.timedelta(minutes=30)     # plazo (desde el fin del slot) para analizar uno publicado tarde o darlo por hueco
ATRASO_MIN = 30
PARADO_MIN = 8                           # un barrido lento (401 + renovación + descarga) puede tardar ~5 min
COBERTURA_MIN = 50.0                     # % de la zona vigilada por debajo del cual el estado es «parcial»
DIEZ = dt.timedelta(minutes=10)


def _ahora() -> dt.datetime:
    return dt.datetime.now(UTC)


def _iso(t: Optional[dt.datetime]) -> Optional[str]:
    return t.isoformat(timespec="seconds").replace("+00:00", "Z") if t else None


def _fecha(s) -> Optional[dt.datetime]:
    if not s:
        return None
    t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _sin_analizar(slot: dt.datetime, error: str, visto: Optional[dt.datetime] = None, **extra) -> dict:
    return {"slot": _iso(slot), "error": error, "visto": _iso(visto), "analizado": _iso(_ahora()), "arranque": False,
            "vigilado": False, "pix_vigilados": 0, "cobertura_pct": 0.0, "n_calor": 0, "calor": [], **extra}


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
        self.ultimo_slot: Optional[dt.datetime] = None        # último slot cerrado EN ORDEN (analizado o abandonado)
        self.ultimo_ok: Optional[dict] = None                 # último barrido analizado de verdad EN ESTE PROCESO
        self.ultimo_cerrado: Optional[dict] = None            # último cerrado en orden EN ESTE PROCESO (para «error»)
        self.pendientes: dict = {}                            # slot -> {"visto": t, "fallos": n, "error": str, "tarde": b}
        self.ultima_vuelta: Optional[dt.datetime] = None
        self.latido: dt.datetime = _ahora()                   # se renueva al empezar cada vuelta y cada barrido
        self.error_vuelta: Optional[str] = None
        self.vueltas_fallidas = 0
        self.reconstruido = False
        self.fondo_de: Optional[str] = None                   # de dónde salió el fondo al arrancar (para /health)
        self._clave_fondo: Optional[tuple] = None             # recorte del fondo.npz cargado, por comprobar
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
                    t = _fecha(b.get("slot")) if isinstance(b, dict) else None
                    if t:
                        buenos.append(dict(b, slot=_iso(t)))
                except (TypeError, ValueError):
                    pass
            self.barridos = sorted(buenos, key=lambda b: b["slot"])[-MAX_BARRIDOS:]
            self.ultimo_slot = _fecha(e.get("ultimo_slot"))
            # ultimo_ok y ultimo_cerrado NO se toman del fichero: «ok» y «error» hablan de ESTE proceso
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

    def _huella(self) -> str:
        """Lo que hace que el fondo guardado valga para este servicio: zona, margen, trozo y recorte."""
        b = [round(v, 5) for v in self.zona.zona_geo.bounds]
        return (f"{self.coleccion}|{self.trozo}|{self.zona.nombre}|{self.zona.margen_km:g} km|"
                f"{round(self.zona.zona.area)} m2|{b}|{MARGEN_PX} px")

    def _guardar_fondo(self) -> None:
        d = self.motor.estado_fondo()
        if d is None:
            return
        d["huella"] = np.array(self._huella())
        try:
            buf = io.BytesIO()
            np.savez(buf, **d)
            E.escribir_atomico(self.datos / "fondo.npz", buf.getvalue())
        except OSError as ex:
            log.error("no se pudo guardar fondo.npz: %s", ex)

    def _cargar_fondo(self) -> bool:
        """Fondo del motor guardado en disco, si cuadra con el último slot de estado.json."""
        f = self.datos / "fondo.npz"
        if not f.exists() or not self.ultimo_slot:
            return False
        try:
            with np.load(f, allow_pickle=False) as z:
                d = {k: z[k] for k in z.files}
            if "huella" not in d or str(d["huella"]) != self._huella():
                log.warning("fondo.npz de otra zona, margen o trozo: se rehace la última hora")
                return False
            if not len(d["slots"]):
                return False
            ultimo = dt.datetime.fromtimestamp(int(d["slots"].max()), UTC)
            if ultimo < self.ultimo_slot - dt.timedelta(minutes=VENTANA_MIN) or ultimo > self.ultimo_slot + DIEZ:
                log.warning("fondo.npz no cuadra con estado.json (%s / %s): se rehace la última hora", ultimo, self.ultimo_slot)
                return False
            self.motor.cargar_fondo(d)
            self._clave_fondo = tuple(int(x) for x in d["clave"])   # el primer barrido comprueba que el recorte es el mismo
            return True
        except Exception as ex:                       # fondo ilegible: se rehace bajando la última hora
            log.warning("fondo.npz ilegible (%s): se rehace la última hora", type(ex).__name__)
            return False

    def _apuntar_tiempos(self, r: dict) -> None:
        f = self.datos / "tiempos.csv"
        with contextlib.suppress(OSError):
            nuevo = not f.exists()
            with open(f, "a", encoding="utf-8") as fh:
                if nuevo:
                    fh.write("slot,barrido_fin,creado,visto,analizado,retraso_min,n_calor,nube_pct\n")
                fh.write(f"{r['slot']},{r.get('barrido_fin')},{r.get('creado')},{r.get('visto')},{r.get('analizado')},"
                         f"{r.get('retraso_min')},{r.get('n_calor')},{r.get('nube_pct')}\n")

    def _cerrar(self, rs: list[dict], mover: bool) -> None:
        """Apunta barridos (ordenados por slot, sin repetir). mover=True: es el siguiente en orden (avanza ultimo_slot)."""
        nuevos = {r["slot"] for r in rs}
        self.barridos = sorted([b for b in self.barridos if b["slot"] not in nuevos] + rs,
                               key=lambda b: b["slot"])[-MAX_BARRIDOS:]
        for r in rs:
            self.pendientes.pop(_fecha(r["slot"]), None)
        if mover:
            self.ultimo_slot = _fecha(rs[-1]["slot"])
            self.ultimo_cerrado = rs[-1]
        self._guardar()

    # ---------------------------------------------------------------- análisis de un barrido
    def analizar(self, prod: dict, visto: dt.datetime, guardar: bool = True, mover: bool = True,
                 tarde: bool = False) -> dict:
        tmp = self.tmp / f"{prod['slot']:%Y%m%d%H%M}_{self.trozo}.nc"
        try:
            ruta, nombre = E.bajar_trozo(self.coleccion, prod, self.trozo, self.token, tmp)
            b38, b105, rec = leer_trozo(str(ruta), self.zona)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
        t = E.tiempos_trozo(nombre)
        if self._clave_fondo is not None:
            clave, self._clave_fondo = self._clave_fondo, None
            if rec.clave != clave:                       # fondo.npz con otras filas/columnas: no vale, se rehace bajándolo
                log.warning("fondo.npz con otro recorte (%s, ahora %s): se rehace la última hora", clave, rec.clave)
                self.motor.hist, self.motor.desde = [], None
                self._rehacer_hora(prod["slot"])
                self.fondo_de = "fondo.npz de otro recorte: rehecho bajando la última hora"
        res = self.motor.procesa(prod["slot"], b38, b105, rec, previos=self.barridos,   # «vis»: sitios de las últimas 24 h
                                 tardio=tarde)
        fin = t.get("barrido_fin")
        ahora = _ahora()
        calor = res.pop("calor")
        arranque = res["fondo_barridos"] < MIN_VALIDOS
        r = {"slot": _iso(prod["slot"]), "barrido_ini": _iso(t.get("barrido_ini")), "barrido_fin": _iso(fin),
             "creado": _iso(t.get("creado")), "visto": _iso(visto), "analizado": _iso(ahora),
             "retraso_min": round((ahora - fin).total_seconds() / 60, 1) if fin else None,
             "arranque": arranque, "vigilado": (not arranque) and res["pix_vigilados"] > 0, "tarde": tarde,
             **res, "n_calor": len(calor), "calor": calor[:MAX_PIX]}
        if guardar:
            self._guardar_fondo()                        # ANTES que estado.json: si se corta entre los dos, el barrido
            self._cerrar([r], mover)                     # se repite al arrancar (el fondo no lo duplica)
            if mover:
                self.ultimo_ok = r
            self._apuntar_tiempos(r)
        return r

    def _rehacer_hora(self, hasta: dt.datetime, incluido: bool = False) -> None:
        """Rehace en silencio (sin apuntar resultados) los barridos de la hora anterior a «hasta»."""
        prods = E.buscar(self.coleccion, hasta - dt.timedelta(minutes=61), hasta + dt.timedelta(minutes=1))
        for p in prods:
            if p["slot"] > hasta or (p["slot"] == hasta and not incluido):
                continue
            self.latido = _ahora()
            try:
                self.analizar(p, _ahora(), guardar=False)
            except E.TokenError:
                raise
            except Exception as ex:                     # un hueco no impide seguir
                log.warning("reconstruir %s: %s", p["slot"], ex)

    def _reconstruir(self) -> None:
        """Fondo para la anomalía temporal: fondo.npz si cuadra; si no, rehace en silencio la hora anterior al último slot."""
        if not self.ultimo_slot or _ahora() - self.ultimo_slot > dt.timedelta(hours=3):
            self.ultimo_slot = None
            self.reconstruido, self.fondo_de = True, "sin fondo (más de 3 h parado)"
            return
        if self._cargar_fondo():
            self.reconstruido, self.fondo_de = True, "fondo.npz"
            return
        self._rehacer_hora(self.ultimo_slot, incluido=True)
        self._guardar_fondo()
        self.reconstruido, self.fondo_de = True, "rehecho bajando la última hora"   # solo si la búsqueda ha ido bien

    def _cerrar_huecos(self, ahora: dt.datetime, en_lista: set) -> None:
        """Slots de las últimas 24 h DETRÁS del último cerrado que no salen en el catálogo pasado el plazo: «hueco».
        Los de delante que aún no han salido están pendientes: el consumidor avanza su cursor con cada slot apuntado."""
        if not self.barridos or not self.ultimo_slot:
            return                                   # recién instalado (o sin último slot): nada de lo anterior es nuestro
        cerrados = {b["slot"] for b in self.barridos}
        limite = ahora - DIEZ - TARDE_MAX
        s = max(_fecha(self.barridos[0]["slot"]), ahora - dt.timedelta(hours=24))
        s = s.replace(minute=s.minute // 10 * 10, second=0, microsecond=0)
        huecos = []
        while s <= limite and s < self.ultimo_slot:
            if _iso(s) not in cerrados and s not in en_lista and s not in self.pendientes:
                huecos.append(_sin_analizar(s, "no analizado (hueco)", hueco=True))
            s += DIEZ
        if huecos:
            log.warning("%d slots sin barrido en el catálogo: apuntados como hueco (%s … %s)",
                        len(huecos), huecos[0]["slot"], huecos[-1]["slot"])
            self._cerrar(huecos, mover=False)

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
        # olvidar pendientes viejos y los que ya no salen en el catálogo
        self.pendientes = {k: v for k, v in self.pendientes.items() if ahora - k < dt.timedelta(hours=3) and k in en_lista}
        cerrados = {b["slot"] for b in self.barridos}
        try:
            self._recorrer(lista, cerrados)
        finally:                                          # después: con el último slot ya movido en esta vuelta
            self._cerrar_huecos(ahora, en_lista)

    def _recorrer(self, lista: list, cerrados: set) -> None:
        for i, p in enumerate(lista):
            s = p["slot"]
            if _iso(s) in cerrados or (self.ultimo_slot and s == self.ultimo_slot):
                continue
            tarde = bool(self.ultimo_slot and s < self.ultimo_slot)
            self.latido = _ahora()
            pend = self.pendientes.setdefault(s, {"visto": _ahora(), "fallos": 0, "error": None, "tarde": tarde})
            if tarde and _ahora() - (s + DIEZ) > TARDE_MAX:
                log.warning("barrido %s publicado tarde (más de %d min): no se analiza", s, TARDE_MAX.seconds // 60)
                self._cerrar([_sin_analizar(s, f"publicado tarde (más de {TARDE_MAX.seconds // 60} min): no analizado",
                                            pend["visto"], tarde=True)], mover=False)
                continue
            hay_mas_nuevos = i < len(lista) - 1
            try:
                r = self.analizar(p, pend["visto"], mover=not tarde, tarde=tarde)
                log.info("barrido %s%s: %d píxeles con calor, nubes %s %%, retraso %s min", r["slot"],
                         " (publicado tarde)" if tarde else "", r["n_calor"], r["nube_pct"], r["retraso_min"])
            except E.TokenError:
                raise
            except Exception as ex:
                pend["fallos"] += 1
                pend["error"] = f"{type(ex).__name__}: {str(ex)[:200]}"
                permanente = isinstance(ex, (E.TrozoNoDisponible, ErrorPermanente))
                log.warning("barrido %s: fallo %d (%s)", s, pend["fallos"], pend["error"])
                if tarde:                                 # uno tardío nunca frena a los nuevos
                    if not permanente and pend["fallos"] < 3:
                        continue
                    self._cerrar([_sin_analizar(s, pend["error"], pend["visto"], tarde=True)], mover=False)
                    continue
                # con barridos más nuevos esperando: como mucho 3 intentos; si es el último: hasta 30 min
                if not permanente and (pend["fallos"] < 3 if hay_mas_nuevos else _ahora() - pend["visto"] < REINTENTO_MAX):
                    return                                # se reintenta en la próxima vuelta
                self._cerrar([_sin_analizar(s, pend["error"], pend["visto"])], mover=True)

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
        edad = round((ahora - (ok_slot + DIEZ)).total_seconds() / 60, 1) if ok_slot else None
        en_marcha_min = (ahora - self.inicio).total_seconds() / 60
        pend = dict(self.pendientes)                     # copia: el bucle lo modifica a la vez
        pend_fallos = max([v["fallos"] for v in pend.values() if not v.get("tarde")] or [0])
        ultimo = self.ultimo_cerrado                     # solo lo cerrado por ESTE proceso
        sin_latido = (ahora - self.latido).total_seconds() / 60
        cobertura = uok.get("cobertura_pct") if uok else None
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
        elif cobertura is not None and cobertura < COBERTURA_MIN:
            estado = "parcial"                           # se vigila, pero menos de la mitad de la zona
        else:
            estado = "ok"
        hace3h = _iso(ahora - dt.timedelta(hours=3))
        return {"ok": estado == "ok", "estado": estado, "vigilando": estado in ("ok", "parcial"),
                "cobertura_pct": cobertura, "nube_pct": uok.get("nube_pct") if uok else None,
                "aviso_token": tok["error"] if tok.get("pasajero") else None,
                "min_sin_latido": round(sin_latido, 1),
                "ultimo_barrido_ok": _iso(ok_slot), "edad_min": edad, "ultimo_slot": _iso(self.ultimo_slot),
                "token": {"ok": tok["ok"], "volver_a_entrar": tok["volver_a_entrar"]},
                "ultima_vuelta": _iso(self.ultima_vuelta), "error_vuelta": self.error_vuelta,
                "vueltas_fallidas": self.vueltas_fallidas, "fondo_al_arrancar": self.fondo_de,
                "barridos_reintentando": [{"slot": _iso(k), "fallos": v["fallos"], "error": v["error"]}
                                          for k, v in sorted(pend.items()) if v["fallos"]],
                "barridos_saltados_tarde": [b["slot"] for b in self.barridos
                                            if b.get("tarde") and b.get("error") and b["slot"] >= hace3h],
                "huecos_24h": sum(1 for b in self.barridos if b.get("hueco")),
                "disco_persistente": self.disco_ok, "zona": self.zona.nombre}
