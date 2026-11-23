# motor.py — análisis del nivel 1 de MTG FCI (alta resolución) para vigilar calor en una zona.
# -----------------------------------------------------------------------------
# Lo mismo que se validó el 23-nov-2026 con 6 episodios de Alcalá de Guadaíra y 10 días enteros
# (scripts/investigacion-calor/fci_alarma.py):
#   NUBE (dilatada 2 píxeles) si  BT105 < 265 K
#                             o   BT105 − mediana 11×11 < −3 K
#                             o   BT105 − mediana de los barridos de la última hora < −3 K
#   En píxeles despejados:
#     S38 = BT38 − mediana 11×11 (solo despejados)     T38 = S38 − mediana de S38 en la última hora
#     SD  = (BT38−BT105) − mediana 11×11               TD  = SD  − mediana de SD  en la última hora
#   CALOR si T38 > UMBRAL, TD > UMBRAL − 0,5 y TD − T38 ≤ 0,7 K (el 10,5 µm no se enfría: así se
#   descarta el sol reflejado en bordes de nube), en la zona vigilada y con ≥ 3 barridos válidos
#   en la última hora para ese píxel.
# ⚠️ El eje x del FCI L1c va en espejo: la coordenada real de la proyección es −x·H.
# -----------------------------------------------------------------------------
from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass, field
from typing import Optional

import h5py
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from pyproj import Proj, Transformer
from shapely import contains_xy, prepare
from shapely.geometry import shape
from shapely.ops import transform as stransform

H = 35786400.0
GEOS = Proj(proj="geos", h=H, lon_0=0, sweep="y", a=6378137, b=6356752.31424518)
A_UTM = Transformer.from_crs(4326, 25830, always_xy=True)
VENTANA_MIN = 60          # «la última hora»: barridos anteriores que sirven de fondo
MIN_VALIDOS = 3           # barridos despejados que hacen falta en esa hora para juzgar un píxel


@dataclass
class Parametros:
    umbral: float = 4.5          # K, T38
    margen_td: float = 0.5       # TD > umbral − margen_td
    sol: float = 0.7             # TD − T38 ≤ sol
    intenso: float = 8.0         # K, T38 para marcar «intenso»
    nube_fria: float = 265.0     # K, BT105
    nube_salto: float = 3.0      # K
    dilatacion: int = 2          # píxeles


class Zona:
    """Término municipal (GeoJSON) + margen en km. Calcula la caja de recorte en lat/lon."""

    def __init__(self, ruta_geojson: str, margen_km: float, nombre: Optional[str] = None):
        fc = json.load(open(ruta_geojson, encoding="utf-8"))
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
        # recorte = caja de la zona + 0,15° en latitud y 0,2° en longitud (para la mediana 11×11 en los bordes)
        self.caja = (s - 0.15, n + 0.15, w - 0.2, e + 0.2)
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


_RECORTES: dict = {}


def recorte(m, zona: Zona) -> Recorte:
    """Filas/columnas del trozo que caen en la caja de la zona, con lat/lon de cada píxel (se calcula una vez)."""
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
        raise ValueError("la zona no cae en este trozo")
    XX, YY = np.meshgrid(-xa[cols.min():cols.max() + 1] * H, ya[filas.min():filas.max() + 1] * H)
    lon, lat = GEOS(XX, YY, inverse=True)
    lon = np.where(np.abs(lon) > 360, np.nan, lon)
    lat = np.where(np.abs(lat) > 90, np.nan, lat)
    # misma definición que el análisis validado: filas/columnas cuyos píxeles caen dentro de la caja
    dentro = (lat > la0) & (lat < la1) & (lon > lo0) & (lon < lo1)
    f, c = np.where(dentro)
    if len(f) == 0:
        raise ValueError("la zona no cae en este trozo")
    f0, f1, c0, c1 = f.min(), f.max() + 1, c.min(), c.max() + 1
    lat, lon = lat[f0:f1, c0:c1], lon[f0:f1, c0:c1]
    ex, ny = A_UTM.transform(lon, lat)
    en_zona = contains_xy(zona.zona, ex, ny)
    en_muni = contains_xy(zona.muni, ex, ny)
    from shapely import distance, points
    dist = np.where(en_muni, 0.0, distance(zona.muni, points(ex, ny)) / 1000.0)
    r = Recorte(slice(filas.min() + f0, filas.min() + f1), slice(cols.min() + c0, cols.min() + c1),
                lat, lon, en_zona, en_muni, dist)
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
    rad = np.where((v > 4095) & (ws != 0), v * ws + wo, frio)   # 3,8 µm: rango extendido para lo caliente
    rad[v == 65535] = np.nan
    rad[rad <= 0] = np.nan
    k = {n: float(m[f"radiance_to_bt_conversion_{n}"][()]) for n in
         ("coefficient_a", "coefficient_b", "coefficient_wavenumber", "constant_c1", "constant_c2")}
    wn = k["coefficient_wavenumber"]
    with np.errstate(invalid="ignore", divide="ignore"):
        return (k["constant_c2"] * wn / np.log(1 + k["constant_c1"] * wn ** 3 / rad) - k["coefficient_b"]) / k["coefficient_a"]


def leer_trozo(ruta: str, zona: Zona) -> tuple[np.ndarray, np.ndarray, Recorte]:
    with h5py.File(ruta, "r") as h:
        m38, m105 = h["data/ir_38_hr/measured"], h["data/ir_105_hr/measured"]
        rec = recorte(m38, zona)
        return _bt(m38, rec), _bt(m105, rec), rec


def nanmed_espacial(a: np.ndarray, k: int = 11) -> np.ndarray:
    p = k // 2
    pad = np.pad(a, p, mode="edge")
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
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
    import warnings
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


@dataclass
class _Historico:
    slot: dt.datetime
    S38: np.ndarray
    SD: np.ndarray
    b105: np.ndarray


@dataclass
class Motor:
    zona: Zona
    p: Parametros = field(default_factory=Parametros)
    hist: list = field(default_factory=list)

    def _previos(self, slot: dt.datetime) -> list:
        return [h for h in self.hist if slot - dt.timedelta(minutes=VENTANA_MIN) <= h.slot < slot]

    def procesa(self, slot: dt.datetime, b38: np.ndarray, b105: np.ndarray, rec: Recorte) -> dict:
        """Analiza un barrido. Devuelve el resultado (y guarda lo necesario para los siguientes)."""
        p = self.p
        prev = [h for h in self._previos(slot) if h.S38.shape == b38.shape]   # si cambia la rejilla, fondo nuevo
        D = b38 - b105
        if np.all(np.isnan(b105)):
            nube = np.ones(b38.shape, bool)
        else:
            nube = (b105 < p.nube_fria) | (b105 - nanmed_espacial(b105) < -p.nube_salto)
            if prev:
                ref, _ = _nanmed_t([h.b105 for h in prev])
                with np.errstate(invalid="ignore"):
                    nube |= (b105 - ref) < -p.nube_salto
            nube |= np.isnan(b38) | np.isnan(b105)
            nube = dilatar(nube, p.dilatacion)
        b = np.where(nube, np.nan, b38)
        d = np.where(nube, np.nan, D)
        S38 = b - nanmed_espacial(b)
        SD = d - nanmed_espacial(d)
        if prev:
            m38, val = _nanmed_t([h.S38 for h in prev])
            mD, _ = _nanmed_t([h.SD for h in prev])
            T38, TD = S38 - m38, SD - mD
        else:
            val = np.zeros(b38.shape, int)
            T38 = TD = np.full(b38.shape, np.nan)
        ok = (val >= MIN_VALIDOS) & ~nube & rec.en_zona
        with np.errstate(invalid="ignore"):
            calor = ok & (T38 > p.umbral) & (TD > p.umbral - p.margen_td) & ((TD - T38) <= p.sol)
        # guardar para los barridos siguientes (solo la última hora)
        self.hist = [h for h in self.hist if h.slot >= slot - dt.timedelta(minutes=VENTANA_MIN) and h.slot != slot] + \
                    [_Historico(slot, S38.astype(np.float32), SD.astype(np.float32), b105.astype(np.float32))]
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
            "pix_vigilados": int(ok.sum()),          # despejados y con fondo suficiente
            "fondo_barridos": len(prev),
            "sol_elev": round(elevacion_solar(slot + dt.timedelta(minutes=8), lat_c, lon_c), 1),
            "calor": pix,
            "maximo": maximo,
        }
