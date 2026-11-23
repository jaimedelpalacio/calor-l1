# repaso.py — pasa por el MISMO código del servicio (búsqueda, descarga, lectura y motor) un periodo pasado.
# Sirve para probar el servicio con episodios conocidos sin esperar a que pase algo.
#
#   ⚠️ tokens.json de un inicio de sesión PROPIO, nunca el mismo que usa Render (cada renovación invalida el anterior).
#   EXIGIR_DISCO=0 DATA_DIR=<carpeta con tokens.json> python repaso.py 2026-05-13T19:30 2026-05-13T21:10
#
# Imprime una línea por barrido: hora, nubes, señal máxima y píxeles con calor. No toca estado.json.
from __future__ import annotations

import datetime as dt
import os
import sys

os.environ.setdefault("BUCLE", "0")
os.environ.setdefault("EXIGIR_DISCO", "0")
import eumetsat as E                     # noqa: E402
from servicio import Servicio            # noqa: E402

UTC = dt.timezone.utc


def main(a: str, b: str) -> None:
    t0 = dt.datetime.fromisoformat(a).replace(tzinfo=UTC)
    t1 = dt.datetime.fromisoformat(b).replace(tzinfo=UTC)
    s = Servicio()
    prods = E.buscar(s.coleccion, t0 - dt.timedelta(minutes=61), t1)
    for p in prods:
        r = s.analizar(p, dt.datetime.now(UTC), guardar=False)
        if p["slot"] < t0:
            continue                      # la hora previa solo da fondo
        mx = r["maximo"] or {}
        calor = " | ".join(f"{c['t38']}/{c['td']} K en ({c['lat']}, {c['lon']}) {c['en']}" + (" INTENSO" if c["intenso"] else "")
                           for c in r["calor"][:4])
        paso = (r.get("barrido_fin") or "?" * 19)[11:19]
        nub = r.get("nube_pct")
        print(f"{p['slot']:%Y-%m-%d %H:%M}  paso {paso}  nubes {nub if nub is not None else float('nan'):5.1f} %  "
              f"máx {mx.get('t38', float('nan')):5.1f}  {'CALOR: ' + calor if calor else ''}{'  (arranque)' if r['arranque'] else ''}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
