# calor-l1

Vigilancia de **calor** en una zona (hoy: Alcalá de Guadaíra + 5 km) con el **nivel 1 de MTG FCI**
(alta resolución, canales de 3,8 y 10,5 µm a 1 km, un barrido cada 10 min).

> Investigación. Los datos de EUMETSAT de menos de 1 h se usan con la licencia de **investigación**
> («< 1 hr», Personal/Educational/Research): uso **no operativo ni comercial** y resultados en abierto.
> Hablamos siempre de **calor**, nunca de «incendio»: el satélite ve calor; puede ser un incendio, pero no lo afirma.

## Qué hace

Cada minuto mira en el Data Store de EUMETSAT (API v2) si hay un barrido nuevo de la colección
`EO:EUM:DAT:0665`. Si lo hay, baja **solo el trozo 34** (filas de 36,4 a 40,0° N, ~19 MB), lee el recorte
de la zona y aplica el análisis validado el 23-nov-2026 con 6 episodios reales y 10 días enteros
(ver `motor.py`):

- **nubes** (dilatadas 2 píxeles): BT 10,5 µm < 265 K, o 3 K más fría que sus vecinos, o 3 K más fría que en la última hora;
- **calor** en un píxel despejado: su anomalía de 3,8 µm respecto a los vecinos ha subido más de **4,5 K** respecto a la
  última hora, la de 3,8−10,5 µm más de 4 K, y la segunda no supera a la primera en más de 0,7 K
  (así se descarta el sol reflejado en los bordes de las nubes); **intenso** a partir de 8 K.

⚠️ El eje x del FCI L1c va en espejo: la coordenada real de la proyección es −x·H.

## Puntos de acceso

| Ruta | Qué devuelve |
|---|---|
| `GET /ping` | vida (comprobación de salud de Render) |
| `GET /health` | `estado` = ok / arrancando / atrasado (> 30 min sin barrido) / sin_token / error; último barrido, token, última vuelta |
| `GET /estado?n=36` | últimos *n* barridos (máx. 144 = 24 h): hora de paso del satélite, creación del fichero, cuándo lo vimos y lo analizamos, retraso, % de nubes, píxeles vigilados, elevación del sol, señal máxima y **píxeles con calor**. Cabecera `X-API-Key` |
| `GET /zona` | contorno de la zona vigilada (GeoJSON, línea), centro y parámetros |
| `POST /admin/token` | `{"refresh_token": "…"}` carga el refresh token de EUMETSAT y lo prueba. Cabecera `X-API-Key` |

## Token de EUMETSAT

La API v2 se autentica con el usuario del portal en el navegador (OAuth2 con PKCE). El servicio guarda
**solo el refresh token**, en `DATA_DIR/tokens.json` (disco persistente): cada renovación da uno nuevo e
invalida el anterior, así que no puede ir en una variable de entorno. Si `/health` dice `sin_token`,
hay que volver a entrar en EUMETSAT y cargar uno nuevo con `POST /admin/token`.

## Variables de entorno

`API_KEY` (obligatoria para `/estado` y `/admin/token`), `DATA_DIR` (`/data`), `ZONA_GEOJSON`
(`zonas/alcala.geojson`), `MARGEN_KM` (5), `UMBRAL` (4.5), `INTENSO` (8), `COLECCION` (`EO:EUM:DAT:0665`),
`TROZO` (34), `INTERVALO_S` (60).

## Pruebas

- `python pruebas/prueba_motor.py <carpeta investigacion-calor>`: el motor da exactamente las mismas alarmas
  que el análisis validado en los 10 días extraídos.
- `DATA_DIR=<carpeta con tokens.json> python repaso.py 2026-05-13T20:00 2026-05-13T21:00`: pasa un periodo
  pasado por el mismo código del servicio (búsqueda, descarga, lectura y motor).
- `python pruebas/arranque_local.py`: el servicio en local (puerto 8091, `API_KEY=prueba-local`).
