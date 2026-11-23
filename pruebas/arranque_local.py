# arranque_local.py — levanta el servicio en local para probarlo (no se usa en Render).
#   DATA_DIR = carpeta con tokens.json (por defecto ./datos-local), API_KEY = «prueba-local», puerto 8091.
import os
import sys
from pathlib import Path

raiz = Path(__file__).resolve().parents[1]
os.chdir(raiz)
sys.path.insert(0, str(raiz))
os.environ.setdefault("DATA_DIR", str(raiz / "datos-local"))
os.environ.setdefault("API_KEY", "prueba-local")
os.environ.setdefault("EXIGIR_DISCO", "0")          # en local no hay disco montado
import uvicorn  # noqa: E402

uvicorn.run("app:app", host="127.0.0.1", port=int(os.getenv("PORT", "8091")))
