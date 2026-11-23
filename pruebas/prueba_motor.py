# prueba_motor.py — el motor del servicio debe dar las MISMAS alarmas que el análisis validado
# (scripts/investigacion-calor/fci_alarma.py, umbral 4,5) en los 10 días ya extraídos (datos/fci/*.npz),
# barrido a barrido y desde el primero de cada día (los dos usan ya la misma regla de arranque).
#
#   python pruebas/prueba_motor.py C:/Claude/scripts/investigacion-calor [--margenes 5,1] [--sin-g4g5]
#
# Por defecto: con G4 y G5 (lo que corre en Render), zona del término municipal + 5 km y + 1 km.
# --sin-g4g5 compara además con los dos apagados (en el motor: calor_propio = ∞ y congelar_max_min = 0).
# También: «vis» (nubes a ≤ 2 km de los sitios con calor) frente a la máscara final del análisis, y que un reinicio
# con el fondo guardado (estado_fondo → fondo.npz → cargar_fondo) sigue EXACTAMENTE igual que sin reiniciar.
# Qué NO prueba: la lectura del fichero (eso es prueba_lectura.py) ni el bucle del servicio (prueba_servicio.py).
from __future__ import annotations

import datetime as dt
import io
import math
import sys
import warnings
from pathlib import Path

for _f in (sys.stdout, sys.stderr):                      # consola de Windows (cp1252): que «≥», «…» no revienten
    if hasattr(_f, "reconfigure"):
        _f.reconfigure(encoding="utf-8", errors="replace")

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motor import A_UTM, Motor, Parametros, Recorte, Zona   # noqa: E402
from shapely import contains_xy, distance, points           # noqa: E402

warnings.simplefilter("ignore", RuntimeWarning)
args = sys.argv[1:]
base = Path(args[0])
margenes = [float(x) for x in args[args.index("--margenes") + 1].split(",")] if "--margenes" in args else [5.0, 1.0]
configs = [(True, True)] + ([(False, False)] if "--sin-g4g5" in args else [])
sys.path.insert(0, str(base))
src = (base / "fci_alarma.py").read_text(encoding="utf-8").split("if __name__ == '__main__':")[0]
ns = {"__file__": str(base / "fci_alarma.py")}
exec(compile(src, "fci_alarma", "exec"), ns)          # procesa() del análisis validado, sin ejecutar el balance

A = 4.5
total_fallos = 0
for margen in margenes:
    zona = Zona(str(Path(__file__).resolve().parents[1] / "zonas" / "alcala.geojson"), margen)
    for g4, g5 in configs:
        p = Parametros(umbral=A, calor_propio=8.0 if g4 else math.inf, congelar_max_min=360 if g5 else 0)
        fallos, total_ref, total_srv, iguales, congelados, vis_n, vis_mal = 0, 0, 0, 0, 0, 0, 0
        for f in sorted((base / "datos" / "fci").glob("*.npz")):
            dia = f.stem
            z = np.load(f)
            la, lo = z["lat"], z["lon"]
            ex, ny = A_UTM.transform(lo, la)
            en_muni = contains_xy(zona.muni, ex, ny)
            en_zona = contains_xy(zona.zona, ex, ny)
            R = ns["procesa"](dia, A, g4, g5, margen)
            assert (en_zona == R["zona"]).all(), f"{dia}: la zona del servicio no coincide con la del análisis ({margen} km)"
            rec = Recorte(slice(0, la.shape[0]), slice(0, la.shape[1]), la, lo, en_zona, en_muni,
                          np.where(en_muni, 0.0, distance(zona.muni, points(ex, ny)) / 1000))
            ref = R["alarma"]
            m = Motor(zona, p)
            t0 = dt.datetime.strptime(dia, "%Y%m%d").replace(tzinfo=dt.timezone.utc)
            previos, sitios_ref = [], []
            for k in range(144):
                if np.all(np.isnan(z["bt38"][k])):
                    continue
                slot = t0 + dt.timedelta(minutes=10 * k)
                r = m.procesa(slot, z["bt38"][k].astype(float), z["bt105"][k].astype(float), rec, previos=previos)
                previos.append({"slot": slot.strftime("%Y-%m-%dT%H:%M:%SZ"), "calor": r["calor"][:50]})
                congelados += r["pix_fondo_congelado"]
                # «vis» calculada aparte con la máscara FINAL del análisis validado y sus alarmas
                for i, j in zip(*np.where(ref[k])):
                    s_ = (round(float(la[i, j]), 4), round(float(lo[i, j]), 4))
                    if s_ not in sitios_ref:
                        sitios_ref.append(s_)
                esperado = {}
                for a_, b_ in sitios_ref:
                    cerca = np.hypot((la - a_) * 111.2, (lo - b_) * 111.2 * math.cos(math.radians(a_))) <= 2.0
                    esperado[(a_, b_)] = round(100.0 * float(R["mascara"][k][cerca].sum()) / int(cerca.sum()), 1)
                obtenido = {(v[0], v[1]): v[2] for v in r["vis"]}
                if len(sitios_ref) < 60:
                    vis_n += len(obtenido)
                    if obtenido != esperado:
                        vis_mal += 1
                        print(f"  VIS DISTINTA {dia} {k // 6:02d}:{k % 6 * 10:02d}: {esperado} / {obtenido}")
                srv = {(c["lat"], c["lon"]) for c in r["calor"]}
                rr = {(round(float(la[i, j]), 4), round(float(lo[i, j]), 4)) for i, j in zip(*np.where(ref[k]))}
                total_ref += len(rr)
                total_srv += len(srv)
                if srv == rr:
                    iguales += 1
                else:
                    fallos += 1
                    print(f"  DIFERENCIA {dia} {k // 6:02d}:{k % 6 * 10:02d}: análisis {sorted(rr)} / servicio {sorted(srv)}")
        print(f"{margen:g} km, G4 {'sí' if g4 else 'no'}, G5 {'sí' if g5 else 'no'}: píxeles con calor: análisis {total_ref}, "
              f"servicio {total_srv}; barridos iguales {iguales}; diferencias {fallos}; píxel·barridos con fondo congelado {congelados}; "
              f"«vis»: {vis_n} sitio·barridos, {vis_mal} barridos distintos")
        total_fallos += fallos + vis_mal

# reinicio a mitad de un incendio (E1, 7-sep-2026, y E6, 30-jun-2025): guardar el fondo y seguir con un motor nuevo
zona = Zona(str(Path(__file__).resolve().parents[1] / "zonas" / "alcala.geojson"), 1.0)
for dia, k_corte in (("20260907", 26), ("20250630", 94)):
    z = np.load(base / "datos" / "fci" / f"{dia}.npz")
    la, lo = z["lat"], z["lon"]
    ex, ny = A_UTM.transform(lo, la)
    en_muni = contains_xy(zona.muni, ex, ny)
    rec = Recorte(slice(0, la.shape[0]), slice(0, la.shape[1]), la, lo, contains_xy(zona.zona, ex, ny), en_muni,
                  np.where(en_muni, 0.0, distance(zona.muni, points(ex, ny)) / 1000))
    t0 = dt.datetime.strptime(dia, "%Y%m%d").replace(tzinfo=dt.timezone.utc)
    seguido, reiniciado = Motor(zona, Parametros()), None
    iguales_r = distintos_r = 0
    for k in range(144):
        slot = t0 + dt.timedelta(minutes=10 * k)
        a = seguido.procesa(slot, z["bt38"][k].astype(float), z["bt105"][k].astype(float), rec)
        if k == k_corte:                                  # «reinicio»: lo que se guarda en fondo.npz y se vuelve a leer
            buf = io.BytesIO()
            np.savez(buf, **seguido.estado_fondo())
            buf.seek(0)
            reiniciado = Motor(zona, Parametros())
            with np.load(buf, allow_pickle=False) as f:
                reiniciado.cargar_fondo({x: f[x] for x in f.files})
        elif reiniciado is not None:
            b = reiniciado.procesa(slot, z["bt38"][k].astype(float), z["bt105"][k].astype(float), rec)
            if a == b:
                iguales_r += 1
            else:
                distintos_r += 1
    print(f"reinicio {dia} tras las {k_corte // 6:02d}:{k_corte % 6 * 10:02d}: barridos iguales {iguales_r}, distintos {distintos_r}")
    total_fallos += distintos_r
sys.exit(1 if total_fallos else 0)
