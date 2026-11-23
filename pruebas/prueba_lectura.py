# prueba_lectura.py — la LECTURA del servicio (motor.leer_trozo) frente a la del análisis validado
# (scripts/investigacion-calor/fci_l1_alcala.py) con un trozo 34 real, y las comprobaciones del fichero.
#
#   python pruebas/prueba_lectura.py C:/Claude/scripts/investigacion-calor
#
# Usa descargas/prueba/mtg_l1/hr_trozo_34.nc (no llama a EUMETSAT). Comprueba:
#   1. temperatura de brillo a 3,8 y 10,5 µm idéntica en todos los píxeles comunes, y la zona validada dentro del recorte;
#   2. el recorte deja ≥ MARGEN_PX (15) píxeles alrededor de la zona (con 5 y con 1 km);
#   3. nubes y anomalías S38/SD de la zona iguales con el recorte del servicio y con el del análisis (el borde no influye);
#   4. proyección distinta o eje x sin espejo → error permanente y claro;
#   5. recuentos fuera de valid_range, _FillValue y avisos de calidad que falsean → NaN; saturación y rango extendido, no.
from __future__ import annotations

import sys
import tempfile
import warnings
from pathlib import Path

for _f in (sys.stdout, sys.stderr):                      # consola de Windows (cp1252): que «≥», «…» no revienten
    if hasattr(_f, "reconfigure"):
        _f.reconfigure(encoding="utf-8", errors="replace")

import h5py
import numpy as np

raiz = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(raiz))
import motor as M   # noqa: E402

warnings.simplefilter("ignore", RuntimeWarning)
base = Path(sys.argv[1])
sys.path.insert(1, str(base))
import fci_l1_alcala as F   # noqa: E402

TROZO = base / "descargas" / "prueba" / "mtg_l1" / "hr_trozo_34.nc"
fallos = []


def comprobar(cond, texto):
    print(("  OK   " if cond else "  FALLA ") + texto)
    if not cond:
        fallos.append(texto)


GEOJSON = str(raiz / "zonas" / "alcala.geojson")
a38, a105, q, la, lo, zona_val = F.leer(TROZO)

# 1. lectura idéntica
z5 = M.Zona(GEOJSON, 5.0)
b38, b105, rec = M.leer_trozo(str(TROZO), z5)
d = {(round(float(x), 6), round(float(y), 6)): (u, v) for x, y, u, v in zip(rec.lat.ravel(), rec.lon.ravel(), b38.ravel(), b105.ravel())}
comunes = distintos = fuera = 0
for x, y, u, v, zz in zip(la.ravel(), lo.ravel(), a38.ravel(), a105.ravel(), zona_val.ravel()):
    k = (round(float(x), 6), round(float(y), 6))
    if k in d:
        comunes += 1
        distintos += not (np.isclose(d[k][0], u, equal_nan=True) and np.isclose(d[k][1], v, equal_nan=True))
    elif zz:
        fuera += 1
comprobar(comunes > 1000 and distintos == 0, f"BT38/BT105 idénticas a las del análisis en los {comunes} píxeles comunes ({distintos} distintos)")
comprobar(fuera == 0 and int(rec.en_zona.sum()) == int(zona_val.sum()),
          f"la zona validada ({int(zona_val.sum())} píx.) cae entera en el recorte del servicio ({int(rec.en_zona.sum())} píx.)")

# 2. margen en píxeles
for km in (5.0, 1.0):
    M._RECORTES.clear()
    _, _, r = M.leer_trozo(str(TROZO), M.Zona(GEOJSON, km))
    f, c = np.where(r.en_zona)
    libres = min(f.min(), r.en_zona.shape[0] - 1 - f.max(), c.min(), r.en_zona.shape[1] - 1 - c.max())
    comprobar(libres >= M.MARGEN_PX, f"{km:g} km: {libres} píxeles libres alrededor de la zona (≥ {M.MARGEN_PX})")

# 3. el tamaño del recorte no cambia nubes ni anomalías dentro de la zona (un barrido, sin fondo)
mot = M.Motor(z5)
n1, s1, d1, _, _ = mot.mascara(b38, b105, [])
n2, s2, d2, _, _ = mot.mascara(a38.astype(float), a105.astype(float), [])
pos1 = {(round(float(x), 6), round(float(y), 6)): (n, s, e) for x, y, n, s, e in
        zip(rec.lat[rec.en_zona], rec.lon[rec.en_zona], n1[rec.en_zona], s1[rec.en_zona], d1[rec.en_zona])}
iguales = all(np.isclose(pos1[(round(float(x), 6), round(float(y), 6))][1], s, equal_nan=True)
              and pos1[(round(float(x), 6), round(float(y), 6))][0] == n
              and np.isclose(pos1[(round(float(x), 6), round(float(y), 6))][2], e, equal_nan=True)
              for x, y, n, s, e in zip(la[zona_val], lo[zona_val], n2[zona_val], s2[zona_val], d2[zona_val]))
comprobar(iguales, "nubes, S38 y SD de la zona iguales con el recorte del servicio y con el del análisis")


# 4 y 5. ficheros sintéticos
def fichero(ruta, h_sat=M.H, sx=-2.79435763e-05, cuentas=None, calidad=None):
    with h5py.File(ruta, "w") as h:
        pr = h.create_dataset("data/mtg_geos_projection", data=0)
        for k, v in {"perspective_point_height": h_sat, "longitude_of_projection_origin": 0.0,
                     "semi_major_axis": M.A_EJE, "semi_minor_axis": M.B_EJE}.items():
            pr.attrs[k] = np.array([v])
        pr.attrs["sweep_angle_axis"] = np.array(["y"], dtype=h5py.string_dtype())
        m = h.create_group("data/ir_38_hr/measured")
        x = m.create_dataset("x", data=np.arange(4, dtype=np.int16))
        x.attrs["scale_factor"], x.attrs["add_offset"] = np.array([sx]), np.array([0.0])
        y = m.create_dataset("y", data=np.arange(2, dtype=np.int16))
        y.attrs["scale_factor"], y.attrs["add_offset"] = np.array([2.79435763e-05]), np.array([0.0])
        if cuentas is not None:
            er = m.create_dataset("effective_radiance", data=np.array(cuentas, np.uint16))
            for k, v in {"scale_factor": 0.00122469, "add_offset": -0.24983633, "warm_scale_factor": 0.02422214,
                         "warm_add_offset": -94.42409}.items():
                er.attrs[k] = np.array([v], np.float32)
            er.attrs["_FillValue"] = np.array([65535], np.uint16)
            er.attrs["valid_range"] = np.array([0, 8191], np.uint16)
            er.attrs["valid_cold_range"] = np.array([0, 4095], np.uint16)
            m.create_dataset("pixel_quality", data=np.array(calidad, np.uint8))
            for k, v in {"coefficient_a": 0.99667156, "coefficient_b": 2.258397, "coefficient_wavenumber": 2644.329,
                         "constant_c1": 1.191043e-05, "constant_c2": 1.438775}.items():
                m.create_dataset(f"radiance_to_bt_conversion_{k}", data=np.float32(v))


tmp = Path(tempfile.mkdtemp())
with h5py.File(TROZO, "r") as h:
    try:
        M.comprobar_proyeccion(h, h["data/ir_38_hr/measured"])
        comprobar(True, "la proyección del trozo real es la esperada")
    except M.ErrorPermanente as ex:
        comprobar(False, f"la proyección del trozo real es la esperada ({ex})")
for nombre, kw in (("otra altura del satélite", {"h_sat": 35786000.0}), ("eje x sin espejo", {"sx": 2.79435763e-05})):
    fichero(tmp / "p.nc", **kw)
    with h5py.File(tmp / "p.nc", "r") as h:
        try:
            M.comprobar_proyeccion(h, h["data/ir_38_hr/measured"])
            comprobar(False, f"{nombre}: error permanente")
        except M.ErrorPermanente as ex:
            comprobar("proyección" in str(ex), f"{nombre}: error permanente y claro")

#            válido  fuera de rango  relleno  cálido   calidad 2   saturado 16   rango ext. 64   geoloc. 8
cuentas = [[1500, 8192, 65535, 5000], [1500, 1500, 1500, 1500]]
calidad = [[0, 0, 0, 0], [2, 16, 64, 8]]
fichero(tmp / "c.nc", cuentas=cuentas, calidad=calidad)
with h5py.File(tmp / "c.nc", "r") as h:
    r = M.Recorte(slice(0, 2), slice(0, 4), None, None, None, None, None)
    bt = M._bt(h["data/ir_38_hr/measured"], r)
esperado_nan = np.array([[False, True, True, False], [True, False, False, True]])
comprobar((np.isnan(bt) == esperado_nan).all(), f"NaN en fuera de rango, relleno y calidad 1/2/4/8/32; no en saturado ni rango extendido ({np.round(bt, 1).tolist()})")
comprobar(bt[0, 3] > bt[0, 0] + 30, f"el rango cálido del 3,8 µm da más temperatura ({bt[0, 3]:.0f} K frente a {bt[0, 0]:.0f} K)")

print(f"\n{len(fallos)} fallos")
sys.exit(1 if fallos else 0)
