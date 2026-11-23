# app.py — calor-l1: vigilancia de calor con el nivel 1 de MTG FCI (1 km, cada 10 min) en una zona.
# -----------------------------------------------------------------------------
# PILOTO DE INVESTIGACIÓN con la licencia de investigación de EUMETSAT para datos de menos de 1 h («< 1 hr»): uso NO
# operativo ni comercial. Para probarlo lo consume un centro de control de SecurvigIA; antes de usarlo de forma
# operativa con un cliente hace falta la licencia operativa de EUMETSAT.
# Hablamos de CALOR, nunca de «incendio»: el satélite ve calor; puede ser un incendio, pero no lo afirma.
#
#   GET  /ping            vida (comprobación de salud de Render)
#   GET  /health          estado: último barrido, su antigüedad, token, última vuelta, errores
#   GET  /estado?n=36     últimos n barridos (ordenados por slot), con los píxeles con calor y «vis»  [cabecera X-API-Key]
#                         vis = [[lat, lon, pct], …]: por cada sitio con calor en las últimas 24 h (lat/lon tal cual en
#                         calor[], 4 decimales), % (1 decimal) de píxeles del recorte a ≤ 2 km que son nube en ese barrido
#   GET  /zona            contorno de la zona vigilada (GeoJSON, línea) y parámetros
#   POST /admin/token     {"refresh_token": "..."} prueba y carga el refresh token de EUMETSAT [cabecera X-API-Key
#                         con ADMIN_KEY, distinta de API_KEY; sin ADMIN_KEY: 503 «desactivado»]
#
# UN SOLO proceso (sin --workers): el bucle y el token no deben duplicarse.
# -----------------------------------------------------------------------------
from __future__ import annotations

import hmac
import logging
import os
import threading
import time
from dataclasses import asdict
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel

import eumetsat as E
from servicio import Servicio

VERSION = "1.1.0"
logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(name)s: %(message)s")
logging.Formatter.converter = time.gmtime          # registros en UTC, como los datos
API_KEY = os.getenv("API_KEY", "")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")            # solo para /admin/token: no debe salir de Render ni de credenciales.env

# sin /docs, /redoc ni /openapi.json: no hay por qué publicar la API
app = FastAPI(title="calor-l1", version=VERSION, default_response_class=ORJSONResponse,
              docs_url=None, redoc_url=None, openapi_url=None)
S = Servicio()
if ADMIN_KEY and ADMIN_KEY == API_KEY:
    logging.getLogger("calor-l1").error("ADMIN_KEY es igual que API_KEY: /admin/token queda desactivado")


def _comprobar(x_api_key: Optional[str], clave: str) -> None:
    if not hmac.compare_digest((x_api_key or "").encode(), clave.encode()):
        raise HTTPException(status_code=401, detail="clave incorrecta")


def _clave(x_api_key: Optional[str]) -> None:
    """Solo por cabecera X-API-Key (en la URL acabaría en los registros). Sin API_KEY configurada: cerrado."""
    if not API_KEY:
        raise HTTPException(status_code=503, detail="servicio sin API_KEY configurada")
    _comprobar(x_api_key, API_KEY)


def _clave_admin(x_api_key: Optional[str]) -> None:
    """/admin/token, con su propia clave: la de lectura (API_KEY) vive en los programas que leen /estado."""
    if not ADMIN_KEY or ADMIN_KEY == API_KEY:
        raise HTTPException(status_code=503, detail="desactivado: falta ADMIN_KEY (distinta de API_KEY)")
    _comprobar(x_api_key, ADMIN_KEY)


VIS_FORMATO = ("vis: [[lat, lon, pct], ...] por cada sitio con calor en el barrido o en las 24 h anteriores (máx. 60); "
               "lat/lon exactamente como en calor[] (4 decimales); pct = % (1 decimal) de píxeles del recorte a <= 2 km "
               "que son nube en la máscara final del barrido (sin dato = nube; sin píxeles cerca = 100)")


@app.on_event("startup")
def _arranque() -> None:
    if os.getenv("BUCLE", "1") == "1":
        threading.Thread(target=S.bucle, name="bucle", daemon=True).start()


@app.on_event("shutdown")
def _apagado() -> None:
    # si se está renovando el token, esperar a que termine: el refresh nuevo debe quedar guardado
    S.token.esperar_libre(E.ESPERA_APAGADO_S)        # 27 s: Render mata a los ~30 s; CAS tiene como mucho 25 s


@app.exception_handler(RequestValidationError)
def _peticion_no_valida(request: Request, exc: RequestValidationError):
    # sin devolver lo recibido (podría ser un token)
    return ORJSONResponse(status_code=422, content={"detail": "petición no válida"})


@app.get("/ping")
def ping():
    # Render reinicia el servicio si esto falla: solo cuando el bucle lleva > 15 min sin latido (colgado)
    sin_latido = round((time.time() - S.latido.timestamp()) / 60, 1)      # sin depender del resto de salud()
    if os.getenv("BUCLE", "1") == "1" and sin_latido > 15:
        return ORJSONResponse(status_code=503, content={"ok": False, "min_sin_latido": sin_latido})
    return {"ok": True, "version": VERSION}


@app.get("/health")
def health():
    return {"version": VERSION, **S.salud()}


@app.get("/estado")
def estado(n: int = Query(36, ge=1, le=144), x_api_key: Optional[str] = Header(None)):
    _clave(x_api_key)
    barridos = list(S.barridos[-n:])
    return {"version": VERSION, "zona": S.zona.nombre, "margen_km": S.zona.margen_km,
            "parametros": asdict(S.p), "salud": S.salud(), "vis_formato": VIS_FORMATO, "barridos": barridos}


@app.get("/zona")
def zona():
    return {"nombre": S.zona.nombre, "margen_km": S.zona.margen_km, "contorno": S.zona.contorno_geojson(),
            "centro": {"lon": round(S.zona.centro[0], 5), "lat": round(S.zona.centro[1], 5)},
            "parametros": asdict(S.p)}


class CargaToken(BaseModel):
    refresh_token: str


@app.post("/admin/token")
def admin_token(body: CargaToken, x_api_key: Optional[str] = Header(None)):
    _clave_admin(x_api_key)
    try:
        S.token.cargar_refresh(body.refresh_token)
    except E.TokenError as ex:
        raise HTTPException(status_code=400, detail=f"no se ha cargado (el anterior sigue): {ex}")
    return {"ok": True, "token": S.token.estado()}
