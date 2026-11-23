# motor.py — análisis del nivel 1 de MTG FCI (alta resolución) para vigilar calor en una zona.
# -----------------------------------------------------------------------------
# El MISMO algoritmo que scripts/investigacion-calor/fci_alarma.py (validado con 6 episodios de Alcalá de
# Guadaíra y 10 días enteros; pruebas/prueba_motor.py comprueba que dan las mismas alarmas barrido a barrido):
#   NUBE PROPIA si  BT105 < 265 K
#               o   BT105 − mediana 11×11 < −3 K
#               o   BT105 − mediana del fondo de BT105 en la última hora < −3 K
#   NUBE = nube propia dilatada 2 píxeles, SALVO en los píxeles con CALOR PROPIO (G4): sin nube propia y con
#     BT38 ≥ 8 K y (BT38−BT105) ≥ 7,5 K por encima de la mediana 11×11 de sus vecinos sin nube propia.
#     (El humo del incendio enfría el 10,5 µm de los vecinos y la dilatación tapaba el propio fuego.)
#   En píxeles despejados:
#     S38 = BT38 − mediana 11×11 (solo despejados)     T38 = S38 − mediana del fondo de S38 en la última hora
#     SD  = (BT38−BT105) − mediana 11×11               TD  = SD  − mediana del fondo de SD  en la última hora
#   CALOR si T38 > UMBRAL, TD > UMBRAL − 0,5 y TD − T38 ≤ 0,7 K (el 10,5 µm no se enfría: así se
#   descarta el sol reflejado en bordes de nube), en la zona vigilada y con ≥ 3 barridos con fondo válido
#   en la última hora para ese píxel.
#   FONDO (G5): un píxel con CALOR no mete en el fondo su valor sino el fondo que tenía (S38, SD y BT105
#   congelados), como mucho 6 h seguidas; así el incendio no se «come» su fondo a los 3-4 barridos.
# Lectura: recuentos fuera de valid_range, _FillValue y avisos de calidad que falsean radiancia o posición → NaN.
# ⚠️ El eje x del FCI L1c va en espejo: la coordenada real de la proyección es −x·H (se comprueba en cada
#    fichero: escala de x negativa y la proyección esperada; si cambia, error permanente y claro).
# -----------------------------------------------------------------------------
from __future__ import annotations

import datetime as dt
import json
import math
import warnings
from dataclasses import dataclass, field
from typing import Optional

import h5py
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from pyproj import Proj, Transformer
from shapely import contains_xy, distance, points, prepare
from shapely.geometry import shape
from shapely.ops import transform as stransform

H = 35786400.0
A_EJE, B_EJE = 6378137.0, 6356752.31424518
GEOS = Proj(proj="geos", h=H, lon_0=0, sweep="y", a=A_EJE, b=B_EJE)
A_UTM = Transformer.from_crs(4326, 25830, always_xy=True)
VENTANA_MIN = 60          # «la última hora»: barridos anteriores que sirven de fondo
MIN_VALIDOS = 3           # barridos con fondo válido que hacen falta en esa hora para juzgar un píxel
VIS_RADIO_KM = 2.0        # «vis»: nubes a ≤ 2 km de cada sitio con calor reciente
VIS_HORAS = 24            #        sitios de los barridos de las últimas 24 h (y el actual)
VIS_TOPE = 60             #        como mucho 60 sitios (los más recientes)
MAX_PIX = 50              # píxeles con calor que se apuntan por barrido (calor[]); «vis» solo usa esos
MARGEN_PX = 15            # filas/columnas alrededor de la zona: el resultado de un píxel depende de hasta 15 píxeles:
                          # su ventana 11×11 (5) → nube de un vecino, que con G4 depende del calor propio de ese vecino
                          # (mediana 5 de sus vecinos sin nube propia) → nube propia de estos (mediana de BT105, 5).
                          # Sin G4 bastaban 12 (5 + dilatación 2 + 5); G6 (vecinos despejados, 5) no pasa de 15.
# pixel_quality (bits): 1 missing, 2 radiometric, 4 noise, 8 geolocation, 16 saturation, 32 straylight,
# 64 extended_dynamic_range, 128 encoding_saturation. Se descartan los que falsean la radiancia o la posición;
# la saturación y el rango extendido NO (los da un fuego de verdad: descartarlos taparía los más fuertes).
CALIDAD_MALA = 1 | 2 | 4 | 8 | 32


class ErrorPermanente(ValueError):
    """El fichero o la configuración no valen para este análisis: no tiene sentido reintentar el barrido."""


@dataclass
class Parametros:
    umbral: float = 4.5          # K, T38
    margen_td: float = 0.5       # TD > umbral − margen_td
    sol: float = 0.7             # TD − T38 ≤ sol
    intenso: float = 8.0         # K, T38 para marcar «intenso»
    nube_fria: float = 265.0     # K, BT105
    nube_salto: float = 3.0      # K
    dilatacion: int = 2          # píxeles
    calor_propio: float = 8.0    # K (G4): S38 y SD+margen_td de un píxel sin nube propia que nunca es nube
    congelar_max_min: int = 360  # min (G5): como mucho 6 h seguidas con el fondo congelado
    vecinos_min: float = 0.65    # (G6) fracción mínima de vecinos despejados en la ventana 11×11 para juzgar un píxel
                                 # (Hally y otros 2018: el fondo contextual se degrada con pocos vecinos válidos)


class Zona:
    """Término municipal (GeoJSON) + margen en km. Calcula la caja de la zona en lat/lon."""

    def __init__(self, ruta_geojson: str, margen_km: float, nombre: Optional[str] = None):
        with open(ruta_geojson, encoding="utf-8") as f:
            fc = json.load(f)
        feat = fc["features"][0] if fc.get("type") == "FeatureCollection" else fc
        geom = shape(feat["geometry"])
        self.nombre = nombre or feat.get("properties", {}).get("nombre", "zona")
        self.margen_km = margen_km
        self.muni = stransform(A_UTM.transform, geom)
        self.zona = self.muni.buffer(margen_km * 1000)
        prepare(self.muni)
        prepare(self.zona)
        a_geo = Transformer.from_crs(25830, 4326, always_xy=True).transform
        self.zona_geo = stransform(a_geo, self.zona)
        w, s, e, n = self.zona_geo.bounds
        self.caja = (s, n, w, e)          # el margen para las medianas se añade en píxeles (MARGEN_PX)
        c = self.muni.centroid
        self.centro = Transformer.from_crs(25830, 4326, always_xy=True).transform(c.x, c.y)  # (lon, lat)

    def contorno_geojson(self) -> dict:
        """Contorno de la zona vigilada como LÍNEA (en el mapa las zonas van como líneas)."""
        ext = self.zona_geo.exterior if hasattr(self.zona_geo, "exterior") else self.zona_geo.geoms[0].exterior
        return {"type": "LineString", "coordinates": [[round(x, 5), round(y, 5)] for x, y in ext.coords]}


@dataclass
class Recorte:
    filas: slice
    cols: slice
    lat: np.ndarray
    lon: np.ndarray
    en_zona: np.ndarray
    en_muni: np.ndarray
    dist_muni_km: np.ndarray

    @property
    def clave(self) -> tuple:
        """Filas y columnas del trozo: si cambian, el fondo anterior no vale (otros píxeles)."""
        return (int(self.filas.start), int(self.filas.stop), int(self.cols.start), int(self.cols.stop))


_RECORTES: dict = {}


def _attr(a, k):
    v = a[k]
    if isinstance(v, np.ndarray):
        v = v.ravel()[0] if v.size else None
    if isinstance(v, bytes):
        v = v.decode()
    return v


def comprobar_proyeccion(h, m) -> None:
    """La geolocalización supone la proyección de MTG en 0° y el eje x en espejo: si el fichero dice otra cosa, error."""
    try:
        pr = h["data/mtg_geos_projection"].attrs
        esperado = {"perspective_point_height": H, "longitude_of_projection_origin": 0.0,
                    "semi_major_axis": A_EJE, "semi_minor_axis": B_EJE}
        malos = [f"{k}={_attr(pr, k)}" for k, v in esperado.items() if not math.isclose(float(_attr(pr, k)), v, abs_tol=1e-3)]
        if str(_attr(pr, "sweep_angle_axis")).strip().lower() != "y":
            malos.append(f"sweep_angle_axis={_attr(pr, 'sweep_angle_axis')}")
    except KeyError as ex:
        raise ErrorPermanente(f"el fichero no trae la proyección (mtg_geos_projection): {ex}")
    if float(m["x"].attrs["scale_factor"][0]) >= 0 or float(m["y"].attrs["scale_factor"][0]) <= 0:
        malos.append("signo de las escalas de x/y (se espera x en espejo: escala de x negativa)")
    if malos:
        raise ErrorPermanente("la proyección del fichero no es la esperada (revisar la geolocalización): " + ", ".join(malos))


def recorte(m, zona: Zona) -> Recorte:
    """Filas/columnas del trozo con la zona + MARGEN_PX alrededor, con lat/lon de cada píxel (se calcula una vez)."""
    x, y = m["x"], m["y"]
    xa = x[()] * x.attrs["scale_factor"][0] + x.attrs["add_offset"][0]
    ya = y[()] * y.attrs["scale_factor"][0] + y.attrs["add_offset"][0]
    clave = (id(zona), round(float(ya[0]), 7), len(ya), round(float(xa[0]), 7), len(xa))
    if clave in _RECORTES:
        return _RECORTES[clave]
    la0, la1, lo0, lo1 = zona.caja
    # Columnas y filas candidatas: proyectar el borde de la caja (con el x en espejo)
    bl = np.concatenate([np.linspace(lo0, lo1, 30), np.full(30, lo1), np.linspace(lo1, lo0, 30), np.full(30, lo0)])
    bb = np.concatenate([np.full(30, la0), np.linspace(la0, la1, 30), np.full(30, la1), np.linspace(la1, la0, 30)])
    X, Y = GEOS(bl, bb)
    ang_x, ang_y = -np.asarray(X) / H, np.asarray(Y) / H
    cols = np.where((xa >= ang_x.min() - 1e-4) & (xa <= ang_x.max() + 1e-4))[0]
    filas = np.where((ya >= ang_y.min() - 1e-4) & (ya <= ang_y.max() + 1e-4))[0]
    if len(cols) == 0 or len(filas) == 0:
        raise ErrorPermanente("la zona no cae en este trozo")
    extra = MARGEN_PX + 2
    r0, r1 = max(0, filas.min() - extra), min(len(ya), filas.max() + extra + 1)
    c0, c1 = max(0, cols.min() - extra), min(len(xa), cols.max() + extra + 1)
    XX, YY = np.meshgrid(-xa[c0:c1] * H, ya[r0:r1] * H)
    lon, lat = GEOS(XX, YY, inverse=True)
    lon = np.where(np.abs(lon) > 360, np.nan, lon)
    lat = np.where(np.abs(lat) > 90, np.nan, lat)
    ex, ny = A_UTM.transform(lon, lat)
    zf, zc = np.where(contains_xy(zona.zona, ex, ny))
    if len(zf) == 0:
        raise ErrorPermanente("la zona no cae en este trozo")
    f0, f1, k0, k1 = zf.min() - MARGEN_PX, zf.max() + MARGEN_PX + 1, zc.min() - MARGEN_PX, zc.max() + MARGEN_PX + 1
    if f0 < 0 or k0 < 0 or f1 > lat.shape[0] or k1 > lat.shape[1]:
        raise ErrorPermanente(f"la zona está a menos de {MARGEN_PX} píxeles del borde del trozo")
    lat, lon, ex, ny = lat[f0:f1, k0:k1], lon[f0:f1, k0:k1], ex[f0:f1, k0:k1], ny[f0:f1, k0:k1]
    en_zona = contains_xy(zona.zona, ex, ny)
    en_muni = contains_xy(zona.muni, ex, ny)
    dist = np.where(en_muni, 0.0, distance(zona.muni, points(ex, ny)) / 1000.0)
    r = Recorte(slice(int(r0 + f0), int(r0 + f1)), slice(int(c0 + k0), int(c0 + k1)), lat, lon, en_zona, en_muni, dist)
    _RECORTES[clave] = r
    return r


def _bt(m, rec: Recorte) -> np.ndarray:
    """Temperatura de brillo (K) de un canal IR de FCI L1c, solo en el recorte."""
    er = m["effective_radiance"]
    v = er[rec.filas, rec.cols].astype(np.float64)
    a = er.attrs
    frio = v * a["scale_factor"][0] + a["add_offset"][0]
    ws = a["warm_scale_factor"][0] if "warm_scale_factor" in a else 0.0
    wo = a["warm_add_offset"][0] if "warm_add_offset" in a else 0.0
    frio_max = a["valid_cold_range"][1] if "valid_cold_range" in a else 4095
    rad = np.where((v > frio_max) & (ws != 0), v * ws + wo, frio)   # 3,8 µm: rango extendido para lo caliente
    malo = v == (a["_FillValue"][0] if "_FillValue" in a else 65535)
    malo |= v == 65535
    if "valid_range" in a:
        malo |= (v < a["valid_range"][0]) | (v > a["valid_range"][1])
    if "pixel_quality" in m:
        malo |= (m["pixel_quality"][rec.filas, rec.cols] & CALIDAD_MALA) != 0
    rad[malo] = np.nan
    rad[rad <= 0] = np.nan
    k = {n: float(np.asarray(m[f"radiance_to_bt_conversion_{n}"][()]).ravel()[0]) for n in
         ("coefficient_a", "coefficient_b", "coefficient_wavenumber", "constant_c1", "constant_c2")}
    wn = k["coefficient_wavenumber"]
    with np.errstate(invalid="ignore", divide="ignore"):
        return (k["constant_c2"] * wn / np.log(1 + k["constant_c1"] * wn ** 3 / rad) - k["coefficient_b"]) / k["coefficient_a"]


def leer_trozo(ruta: str, zona: Zona) -> tuple[np.ndarray, np.ndarray, Recorte]:
    with h5py.File(ruta, "r") as h:
        m38, m105 = h["data/ir_38_hr/measured"], h["data/ir_105_hr/measured"]
        comprobar_proyeccion(h, m38)
        rec = recorte(m38, zona)
        return _bt(m38, rec), _bt(m105, rec), rec


def frac_despejados(nube: np.ndarray, k: int = 11) -> np.ndarray:
    """Fracción de píxeles sin nube en la ventana k×k de cada píxel (borde replicado), contando cuántos hay (exacto)."""
    p = k // 2
    pad = np.pad(~nube, p, mode="edge")
    return sliding_window_view(pad, (k, k)).sum(axis=(-2, -1)) / (k * k)


def nanmed_espacial(a: np.ndarray, k: int = 11) -> np.ndarray:
    p = k // 2
    pad = np.pad(a, p, mode="edge")
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmedian(sliding_window_view(pad, (k, k)), axis=(-2, -1))


def dilatar(m: np.ndarray, it: int) -> np.ndarray:
    """Dilatación con cruz (vecinos arriba/abajo/izquierda/derecha), como scipy.ndimage.binary_dilation."""
    m = m.copy()
    for _ in range(it):
        n = m.copy()
        n[1:, :] |= m[:-1, :]
        n[:-1, :] |= m[1:, :]
        n[:, 1:] |= m[:, :-1]
        n[:, :-1] |= m[:, 1:]
        m = n
    return m


def _nanmed_t(pila: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    s = np.stack(pila)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmedian(s, axis=0), np.sum(~np.isnan(s), axis=0)


def elevacion_solar(t: dt.datetime, lat: float, lon: float) -> float:
    """Elevación del sol en grados (fórmula aproximada de la NOAA, error < 0,5°)."""
    n = t.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1 + (t.hour - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g)
                    - 0.040849 * math.sin(2 * g))
    tst = t.hour * 60 + t.minute + t.second / 60 + eqt + 4 * lon
    ha = math.radians(tst / 4 - 180)
    la = math.radians(lat)
    cz = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cz))))


def _slot_iso(s) -> Optional[dt.datetime]:
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def sitios_recientes(slot: dt.datetime, actuales: list, previos: Optional[list]) -> list:
    """Posiciones (lat, lon tal cual salen en calor[]) con calor en este barrido y en los de las VIS_HORAS anteriores,
    sin repetir y como mucho VIS_TOPE: primero las del barrido actual, luego de más reciente a más antiguo."""
    out, vistos = [], set()

    def poner(c):
        k = (c["lat"], c["lon"])
        if k not in vistos and len(out) < VIS_TOPE:
            vistos.add(k)
            out.append(k)

    for c in actuales[:MAX_PIX]:          # los mismos que salen en calor[]: el consumidor no conoce otros
        poner(c)
    desde = slot - dt.timedelta(hours=VIS_HORAS)
    for t, b in sorted(((_slot_iso(b.get("slot")), b) for b in (previos or []) if isinstance(b, dict)),
                       key=lambda x: x[0] or desde, reverse=True):
        if t is None or not (desde <= t < slot):
            continue
        for c in b.get("calor") or []:
            poner(c)
    return out


def visibilidad(rec: Recorte, nube: np.ndarray, sitios: list) -> list:
    """[[lat, lon, pct], …]: % de píxeles del RECORTE a ≤ VIS_RADIO_KM de cada sitio que no se ven según «nube»: el
    motor le pasa la máscara FINAL del barrido (dilatada; los píxeles con calor propio no son nube; sin datos = nube)
    más los que no se pueden juzgar por falta de vecinos despejados (G6). Sin píxeles cerca: 100."""
    out = []
    for la_s, lo_s in sitios:
        with np.errstate(invalid="ignore"):
            d = np.hypot((rec.lat - la_s) * 111.2, (rec.lon - lo_s) * 111.2 * math.cos(math.radians(la_s)))
            cerca = d <= VIS_RADIO_KM
        n = int(cerca.sum())
        out.append([la_s, lo_s, round(100.0 * float(nube[cerca].sum()) / n, 1) if n else 100.0])
    return out


@dataclass
class _Historico:
    slot: dt.datetime
    clave: tuple
    S38: np.ndarray        # lo que este barrido deja como fondo (congelado en los píxeles con calor: G5)
    SD: np.ndarray
    b105: np.ndarray


@dataclass
class Motor:
    zona: Zona
    p: Parametros = field(default_factory=Parametros)
    hist: list = field(default_factory=list)
    desde: Optional[np.ndarray] = None       # G5: minuto (desde 1970) en que empezó la racha de calor de cada píxel
    clave_desde: Optional[tuple] = None

    def _previos(self, slot: dt.datetime, clave: tuple) -> list:
        return [h for h in self.hist
                if slot - dt.timedelta(minutes=VENTANA_MIN) <= h.slot < slot and h.clave == clave]

    def mascara(self, b38: np.ndarray, b105: np.ndarray, prev: list):
        """Nubes y anomalías espaciales de un barrido:
        (nube, S38, SD, referencia temporal de BT105 o None, calor_propio de G4)."""
        p = self.p
        D = b38 - b105
        ref = None
        calor_propio = np.zeros(b38.shape, bool)
        if np.all(np.isnan(b105)):
            nube = np.ones(b38.shape, bool)
        else:
            with np.errstate(invalid="ignore"):
                propia = (b105 < p.nube_fria) | (b105 - nanmed_espacial(b105) < -p.nube_salto)
                if prev:
                    ref, _ = _nanmed_t([h.b105 for h in prev])
                    propia |= (b105 - ref) < -p.nube_salto
                propia |= np.isnan(b38) | np.isnan(b105)
                nube = dilatar(propia, p.dilatacion)
                # G4: un píxel sin nube propia y con calor propio claro no es nube aunque lo rodeen (humo del fuego)
                bp, dp = np.where(propia, np.nan, b38), np.where(propia, np.nan, D)
                calor_propio = ~propia & (bp - nanmed_espacial(bp) >= p.calor_propio) \
                    & (dp - nanmed_espacial(dp) >= p.calor_propio - p.margen_td)
                nube &= ~calor_propio
        b = np.where(nube, np.nan, b38)
        d = np.where(nube, np.nan, D)
        return nube, b - nanmed_espacial(b), d - nanmed_espacial(d), ref, calor_propio

    def procesa(self, slot: dt.datetime, b38: np.ndarray, b105: np.ndarray, rec: Recorte,
                previos: Optional[list] = None, tardio: bool = False) -> dict:
        """Analiza un barrido. Devuelve el resultado (y guarda lo necesario para los siguientes).
        previos: barridos ya apuntados (como en /estado), para «vis» (nubes sobre los sitios con calor de las últimas 24 h).
        tardio: barrido publicado detrás de otros más nuevos ya analizados: no toca las rachas de calor (G5) y, en los
        píxeles con racha abierta, deja en el fondo el fondo que ya había (no altera el fondo congelado de los posteriores)."""
        p = self.p
        clave = rec.clave
        prev = self._previos(slot, clave)          # si cambian filas/columnas, fondo nuevo
        nube, S38, SD, ref, calor_propio = self.mascara(b38, b105, prev)
        if prev:
            m38, val = _nanmed_t([h.S38 for h in prev])
            mD, _ = _nanmed_t([h.SD for h in prev])
            T38, TD = S38 - m38, SD - mD
        else:
            m38 = mD = None
            val = np.zeros(b38.shape, int)
            T38 = TD = np.full(b38.shape, np.nan)
        con_fondo = (val >= MIN_VALIDOS) & ~nube & rec.en_zona     # despejados, en la zona y con fondo (antes de G6)
        # G6: sin un 65 % de vecinos despejados el fondo no es fiable (bordes de nube): el píxel no se juzga,
        # salvo que tenga calor propio (G4): G6 no anula a G4
        vecinos = frac_despejados(nube) >= p.vecinos_min
        ok = con_fondo & (vecinos | calor_propio)
        with np.errstate(invalid="ignore"):
            calor = ok & (T38 > p.umbral) & (TD > p.umbral - p.margen_td) & ((TD - T38) <= p.sol)
        # G5: racha de calor de cada píxel y fondo congelado mientras dure (como mucho congelar_max_min)
        if self.desde is None or self.clave_desde != clave or self.desde.shape != b38.shape:
            self.desde, self.clave_desde = np.full(b38.shape, np.nan), clave
        t = slot.timestamp() / 60
        if tardio:
            cong = (calor | ~np.isnan(self.desde)) if prev else np.zeros(b38.shape, bool)
        else:
            self.desde[calor & np.isnan(self.desde)] = t
            self.desde[ok & ~calor] = np.nan
            # también los de racha abierta que hoy no se juzgan (despejados pero sin vecinos, G6): su valor caliente
            # no debe entrar en el fondo, o al volver a juzgarse el incendio se callaría
            sin_juzgar = ~nube & rec.en_zona & ~ok & ~np.isnan(self.desde)
            with np.errstate(invalid="ignore"):
                cong = (calor | sin_juzgar) & (t - self.desde < p.congelar_max_min)
        S38f, SDf, b105f = S38, SD, b105
        if cong.any():                               # sin fondo en la última hora, lo congelado queda sin dato
            nan = np.full(b38.shape, np.nan)
            S38f = np.where(cong, m38 if m38 is not None else nan, S38)
            SDf = np.where(cong, mD if mD is not None else nan, SD)
            b105f = np.where(cong, ref if ref is not None else nan, b105)
        # guardar para los barridos siguientes (solo la última hora)
        self.hist = [h for h in self.hist if h.slot >= slot - dt.timedelta(minutes=VENTANA_MIN) and h.slot != slot] + \
                    [_Historico(slot, clave, S38f.astype(np.float32), SDf.astype(np.float32), b105f.astype(np.float32))]
        zona = rec.en_zona
        n_zona = int(zona.sum())
        pix = []
        for r, c in zip(*np.where(calor)):
            pix.append({
                "lat": round(float(rec.lat[r, c]), 4), "lon": round(float(rec.lon[r, c]), 4),
                "t38": round(float(T38[r, c]), 1), "td": round(float(TD[r, c]), 1),
                "bt38": round(float(b38[r, c]), 1), "bt105": round(float(b105[r, c]), 1),
                "en": "municipio" if rec.en_muni[r, c] else "margen",
                "dist_km": round(float(rec.dist_muni_km[r, c]), 2),
                "intenso": bool(T38[r, c] >= p.intenso),
                "racha_min": None if tardio or np.isnan(self.desde[r, c]) else int(round(t - self.desde[r, c])),
            })
        pix.sort(key=lambda x: -x["t38"])
        # la señal más alta de la zona aunque no llegue al umbral (para el widget)
        Tz = np.where(ok, T38, np.nan)
        maximo = None
        if np.any(~np.isnan(Tz)):
            r, c = np.unravel_index(np.nanargmax(Tz), Tz.shape)
            maximo = {"t38": round(float(Tz[r, c]), 1), "lat": round(float(rec.lat[r, c]), 4),
                      "lon": round(float(rec.lon[r, c]), 4)}
        lon_c, lat_c = self.zona.centro
        return {
            "nube_pct": round(100 * float(nube[zona].mean()), 1) if n_zona else None,
            "pix_zona": n_zona,
            "pix_con_fondo": int(con_fondo.sum()),   # despejados, en la zona y con fondo (antes de G6)
            "pix_vigilados": int(ok.sum()),          # juzgados: con fondo y con vecinos despejados (G6) o calor propio (G4)
            "cobertura_pct": round(100 * float(ok.sum()) / n_zona, 1) if n_zona else None,
            "fondo_barridos": len(prev),
            "pix_fondo_congelado": int(cong.sum()),
            "sol_elev": round(elevacion_solar(slot + dt.timedelta(minutes=8), lat_c, lon_c), 1),
            "calor": pix,
            "maximo": maximo,
            # «vis»: lo que no se puede juzgar por falta de vecinos despejados (G6) tampoco «se ve»
            "vis": visibilidad(rec, nube | (~vecinos & ~calor_propio), sitios_recientes(slot, pix, previos)),
        }

    # ---------------------------------------------------------------- fondo en disco (reinicios)
    def estado_fondo(self) -> Optional[dict]:
        """Arrays para np.savez: el fondo de la última hora y las rachas de calor (sin objetos: allow_pickle=False)."""
        if not self.hist:
            return None
        clave = self.hist[-1].clave
        hs = sorted((h for h in self.hist if h.clave == clave), key=lambda h: h.slot)
        d = {"slots": np.array([int(h.slot.timestamp()) for h in hs], np.int64), "clave": np.array(clave, np.int64),
             "S38": np.stack([h.S38 for h in hs]), "SD": np.stack([h.SD for h in hs]), "b105": np.stack([h.b105 for h in hs])}
        if self.desde is not None and self.clave_desde == clave:
            d["desde"] = self.desde
        return d

    def cargar_fondo(self, d) -> None:
        clave = tuple(int(x) for x in d["clave"])
        S38, SD, b105 = d["S38"], d["SD"], d["b105"]
        if not (len(d["slots"]) == len(S38) == len(SD) == len(b105)) or S38.shape != SD.shape or S38.shape != b105.shape:
            raise ValueError("fondo.npz incoherente")
        self.hist = [_Historico(dt.datetime.fromtimestamp(int(s), dt.timezone.utc), clave,
                                S38[i].astype(np.float32), SD[i].astype(np.float32), b105[i].astype(np.float32))
                     for i, s in enumerate(d["slots"])]
        if "desde" in d and d["desde"].shape == S38.shape[1:]:
            self.desde, self.clave_desde = np.array(d["desde"], np.float64), clave
        else:
            self.desde, self.clave_desde = None, None
