# prueba_motor.py — el motor del servicio debe dar las MISMAS alarmas que el análisis validado
# (scripts/investigacion-calor/fci_alarma.py, umbral 4,5) en los 10 días ya extraídos (datos/fci/*.npz).
#
#   python pruebas/prueba_motor.py C:/Claude/scripts/investigacion-calor
#
# Compara barrido a barrido el conjunto de píxeles con calor. Diferencias admitidas: la primera hora de
# cada día (el análisis validado no juzga los 6 primeros barridos del fichero; el servicio sí tiene fondo).
from __future__ import annotations

import datetime as dt
import sys
import warnings
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motor import Motor, Parametros, Recorte, Zona   # noqa: E402

warnings.simplefilter("ignore", RuntimeWarning)
base = Path(sys.argv[1])
sys.path.insert(0, str(base))
src = (base / "fci_alarma.py").read_text(encoding="utf-8").split("UMBRALES =")[0]
ns = {"__file__": str(base / "fci_alarma.py")}
exec(compile(src, "fci_alarma", "exec"), ns)          # procesa() del análisis validado, sin ejecutar el balance

zona = Zona(str(Path(__file__).resolve().parents[1] / "zonas" / "alcala.geojson"), 5.0)
from motor import A_UTM                                 # noqa: E402
from shapely import contains_xy, distance, points        # noqa: E402

A = 4.5
fallos, total_ref, total_srv, iguales = 0, 0, 0, 0
for f in sorted((base / "datos" / "fci").glob("*.npz")):
    dia = f.stem
    z = np.load(f)
    la, lo = z["lat"], z["lon"]
    ex, ny = A_UTM.transform(lo, la)
    en_muni = contains_xy(zona.muni, ex, ny)
    en_zona = contains_xy(zona.zona, ex, ny)
    assert (en_zona == z["zona"]).all(), f"{dia}: la zona del servicio no coincide con la del análisis"
    rec = Recorte(slice(0, la.shape[0]), slice(0, la.shape[1]), la, lo, en_zona, en_muni,
                  np.where(en_muni, 0.0, distance(zona.muni, points(ex, ny)) / 1000))
    R = ns["procesa"](dia)
    ref = (R["T38"] > A) & (R["TD"] > A - 0.5) & (R["TD"] - R["T38"] <= 0.7)
    m = Motor(zona, Parametros(umbral=A))
    t0 = dt.datetime.strptime(dia, "%Y%m%d").replace(tzinfo=dt.timezone.utc)
    for k in range(144):
        if np.all(np.isnan(z["bt38"][k])):
            continue
        r = m.procesa(t0 + dt.timedelta(minutes=10 * k), z["bt38"][k].astype(float), z["bt105"][k].astype(float), rec)
        srv = {(c["lat"], c["lon"]) for c in r["calor"]}
        rr = {(round(float(la[i, j]), 4), round(float(lo[i, j]), 4)) for i, j in zip(*np.where(ref[k]))}
        total_ref += len(rr)
        total_srv += len(srv)
        if srv == rr:
            iguales += 1
        elif k >= 6:
            fallos += 1
            print(f"  DIFERENCIA {dia} {k // 6:02d}:{k % 6 * 10:02d}: análisis {sorted(rr)} / servicio {sorted(srv)}")
print(f"píxeles con calor: análisis {total_ref}, servicio {total_srv}; barridos iguales {iguales}; diferencias fuera de la 1.ª hora: {fallos}")
sys.exit(1 if fallos else 0)
