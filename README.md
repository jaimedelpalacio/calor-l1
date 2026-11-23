# calor-l1

Vigilancia de **calor** en una zona (hoy: Alcalá de Guadaíra + 1 km) con el **nivel 1 de MTG FCI**
(alta resolución, canales de 3,8 y 10,5 µm a 1 km, un barrido cada 10 min).

> **Piloto de investigación.** Usa los datos de EUMETSAT de menos de 1 h con la licencia de **investigación**
> («< 1 hr», Personal/Educational/Research): uso **no operativo ni comercial**. Para probarlo, lo consume un
> centro de control de SecurvigIA. **Antes de usarlo de forma operativa con un cliente hace falta la licencia
> operativa de EUMETSAT.**
> Hablamos siempre de **calor**, nunca de «incendio»: el satélite ve calor; puede ser un incendio, pero no lo afirma.

## Qué hace

Cada minuto mira en el Data Store de EUMETSAT (API v2) si hay un barrido nuevo de la colección
`EO:EUM:DAT:0665`. Si lo hay, baja **solo el trozo 34** (filas de 36,4 a 40,0° N, ~19 MB), lee el recorte
de la zona (la zona + 15 píxeles alrededor) y aplica el mismo algoritmo que `scripts/investigacion-calor/fci_alarma.py`
(ver `motor.py`):

- **lectura**: recuentos fuera de `valid_range` o de relleno y píxeles con avisos de calidad que falsean la radiancia
  o la posición (missing, radiometric, noise, geolocation, straylight) → sin dato. La saturación y el rango extendido
  **no** se descartan: los da un fuego de verdad. Se comprueba en cada fichero la proyección (MTG en 0°) y el eje x en
  espejo (la coordenada real es −x·H); si cambian, error permanente y claro.
- **nubes** (dilatadas 2 píxeles): BT 10,5 µm < 265 K, o 3 K más fría que sus vecinos, o 3 K más fría que su fondo
  de la última hora. Un píxel **con calor propio** (sin nube propia y con 3,8 µm ≥ 8 K y 3,8−10,5 µm ≥ 7,5 K por
  encima de sus vecinos sin nube) nunca es nube: así el humo del incendio, que enfría el 10,5 µm alrededor, no tapa el fuego.
- **calor** en un píxel despejado: su anomalía de 3,8 µm respecto a los vecinos ha subido más de **4,5 K** respecto a
  su fondo de la última hora, la de 3,8−10,5 µm más de 4 K, y la segunda no supera a la primera en más de 0,7 K
  (así se descarta el sol reflejado en los bordes de las nubes); **intenso** a partir de 8 K. Hacen falta ≥ 3 barridos
  con fondo válido en la última hora.
- **fondo congelado**: un píxel con calor no mete su valor en el fondo (guarda el que tenía), como mucho 6 h seguidas.
  Sin esto, el incendio se «comía» su propio fondo y dejaba de verse a los 3-4 barridos.
- **vecinos suficientes**: un píxel solo se juzga si al menos el 65 % de su ventana 11×11 está despejada (Hally y otros,
  2018: el fondo contextual se degrada con pocos vecinos válidos). En los 10 días de ajuste quita la única falsa alarma
  clara (un borde de nube) sin perder ni acortar ningún incendio. Los píxeles que no se juzgan no cuentan como vigilados.
  Un píxel con calor propio se juzga aunque le falten vecinos (G6 no anula a G4), y uno con racha de calor abierta que
  hoy no se juzga por falta de vecinos sigue con el fondo congelado (si no, el incendio se callaría al volver a juzgarse).

**Barridos que llegan mal**:
- publicados tarde (detrás de uno más nuevo): si llegan < 30 min después de su fin, se analizan y se guardan en su
  sitio (`"tarde": true`); si no, se apuntan con error «publicado tarde (más de 30 min): no analizado». **Los tardíos
  solo sirven para el fondo y el registro, no para avisar**: quien lee `/estado` ya ha pasado de ese slot. Un tardío
  tampoco toca las rachas de calor ni el fondo congelado de los barridos posteriores;
- los que quedan **detrás** del último slot cerrado y no salen en el catálogo pasado ese plazo:
  `"error": "no analizado (hueco)"`, `"hueco": true`. En `/estado` no falta ningún slot de las últimas 24 h. Los de
  **delante** que aún no han salido no son hueco sino pendientes (el consumidor avanza con cada slot apuntado): si el
  catálogo va con retraso, se analizan en orden cuando salen;
- que fallan al bajarlos o leerlos: se reintentan (3 veces si hay otros más nuevos esperando; si es el último, hasta
  30 min) y luego quedan con su error.

**Reinicios**: el fondo de la última hora y las rachas de calor se guardan en `DATA_DIR/fondo.npz` después de cada
barrido (antes que `estado.json`); al arrancar se cargan y se sigue **exactamente** igual. `fondo.npz` lleva una huella
(colección, trozo, zona, `MARGEN_KM`, margen en píxeles) y las filas/columnas del recorte: si no coinciden (otra zona u
otro margen, o el primer barrido trae otro recorte), o no cuadra con `estado.json`, o no está, se rehace la última hora
bajándola (sin añadir resultados). Si el servicio ha estado parado más de 3 h, empieza de nuevo por la última hora
(lo de en medio queda como hueco).

## Puntos de acceso

| Ruta | Qué devuelve |
|---|---|
| `GET /ping` | vida (comprobación de salud de Render); 503 si el bucle lleva > 15 min colgado, para que Render lo reinicie |
| `GET /health` | `estado` (ver abajo) y los datos del último barrido analizado (sin datos del token salvo ok/no ok) |
| `GET /estado?n=36` | últimos *n* barridos (máx. 144 = 24 h), ordenados por slot, con los **píxeles con calor**. Cabecera `X-API-Key` con `API_KEY` |
| `GET /zona` | contorno de la zona vigilada (GeoJSON, línea), centro y parámetros |
| `POST /admin/token` | `{"refresh_token": "…"}` prueba y carga el refresh token de EUMETSAT. Cabecera `X-API-Key` con **`ADMIN_KEY`** (no vale `API_KEY`); sin `ADMIN_KEY` (o igual que `API_KEY`): 503 «desactivado» |

No hay `/docs`, `/redoc` ni `/openapi.json`.

### Cada barrido en `/estado`

`slot`, `barrido_ini`, `barrido_fin` (paso del trozo 34), `creado` (fichero), `visto` (en el catálogo), `analizado`,
`retraso_min`, `arranque` (sin fondo aún), `vigilado`, `tarde`, `nube_pct` (% de la zona con nube), `pix_zona`,
`pix_con_fondo` (despejados y con fondo, antes de exigir vecinos), `pix_vigilados` (los que se juzgan: con fondo y con
el 65 % de vecinos despejados o con calor propio), `cobertura_pct` (= `pix_vigilados` / `pix_zona`, %), `fondo_barridos`,
`pix_fondo_congelado`, `sol_elev`, `maximo` (`t38`, `lat`, `lon` de la señal más alta aunque no llegue al umbral),
`n_calor` y `calor` (hasta 50 píxeles: `lat`, `lon`, `t38`, `td`, `bt38`, `bt105`, `en` municipio/margen, `dist_km`,
`intenso`, `racha_min` = minutos seguidos con calor), y **`vis`**. Los no analizados llevan `error` (y `hueco` o `tarde`).

**`vis`**: `[[lat, lon, pct], …]`, casi siempre vacía. Un elemento por cada **sitio** con calor en este barrido o en los de
las 24 h anteriores (sin repetir, como mucho 60, primero los del barrido —solo los 50 que salen en su `calor[]`— y
luego del más reciente al más antiguo).
`lat` y `lon` son **exactamente** los de `calor[]` (4 decimales); `pct` (1 decimal) es el % de los píxeles del
recorte a ≤ 2 km del sitio que son nube en la máscara final del barrido (la dilatada; los píxeles con calor propio no
cuentan como nube; los que no tienen dato, sí) **o no se pueden juzgar por no tener el 65 % de vecinos despejados**
(G6: lo que no se juzga tampoco «se ve»). Sin píxeles a ≤ 2 km: 100. Sirve para saber si un sitio se ve: una
nube encima del fuego con la zona despejada no es «el calor ha terminado». La respuesta de `/estado` lo recuerda
en `vis_formato`.

### Estados de `/health`

| estado | significado |
|---|---|
| `ok` | vigilando: último barrido analizado hace poco, con fondo, con suelo visible y ≥ 50 % de la zona vigilada |
| `parcial` | vigilando, pero menos del 50 % de la zona (`cobertura_pct` < 50: nubes o falta de fondo). No es un fallo |
| `nublado` | nada vigilado por las nubes: zona cubierta (≥ 95 %), o los claros que tienen fondo no tienen el 65 % de vecinos despejados (`pix_con_fondo` > 0). No es un fallo, pero no se vigila |
| `arrancando` | recién arrancado: aún no ha analizado un barrido en este proceso o le falta la hora de fondo |
| `sin_fondo` | > 45 min en marcha, nada vigilado y ningún píxel despejado con fondo (`pix_con_fondo` = 0) sin estar cubierto: falta de verdad la hora de fondo |
| `atrasado` | el último barrido analizado terminó (slot + 10 min) hace más de 30 min: unos 40 min desde su inicio |
| `error` | en este proceso: barridos que fallan (≥ 3 intentos), el último cerrado en orden con error, o 3 vueltas seguidas fallidas |
| `parado` | el bucle lleva > 8 min sin latido |
| `sin_disco` | no hay disco persistente en `DATA_DIR`, o el token renovado no se ha podido guardar |
| `sin_token` | hay que volver a entrar en EUMETSAT y cargar un refresh token (`POST /admin/token`) |

Campos: `ok` (solo con `ok`), `estado`, `vigilando` (con `ok` y `parcial`), `cobertura_pct` y `nube_pct` (del último
barrido analizado), `aviso_token` (fallo pasajero al renovar, p. ej. CAS caído con el access aún válido: no cambia el
estado), `token` (`ok`, `volver_a_entrar`), `ultimo_barrido_ok`, `edad_min`, `ultimo_slot`, `min_sin_latido`,
`ultima_vuelta`, `error_vuelta`, `vueltas_fallidas`, `fondo_al_arrancar`, `barridos_reintentando`,
`barridos_saltados_tarde` (últimas 3 h), `huecos_24h`, `disco_persistente`, `zona`.

## Token de EUMETSAT

La API v2 se autentica con el usuario del portal en el navegador (OAuth2 con PKCE). El servicio guarda
**solo el refresh token**, en `DATA_DIR/tokens.json` (disco persistente): cada renovación da uno nuevo e
invalida el anterior, así que no puede ir en una variable de entorno. Cada rotación es un riesgo (si se pierde la
respuesta de CAS, el viejo ya no vale), así que un 401 de descarga solo fuerza la renovación con el access cerca de
caducar o, si no, como mucho 2 veces al día. **Apagado**: en un redespliegue Render manda SIGTERM y mata el proceso a
los ~30 s; el servicio no empieza renovaciones nuevas y espera hasta 27 s a que acabe la que esté en curso, y la
espera a CAS es como mucho 5 + 20 s: así una renovación lenta termina y guarda el refresh nuevo antes de morir (si
tardase más, el viejo ya no valdría y habría que volver a entrar). No sabemos cuánto dura un refresh token sin volver a
entrar. Si `/health` dice `sin_token`, hay que volver a entrar en EUMETSAT y cargar uno nuevo con `POST /admin/token`
(con `ADMIN_KEY`). Cada entorno (Render, pruebas locales, `repaso.py`) necesita **su propio** inicio de sesión: si dos
comparten un refresh token, el primero que renueve deja sin acceso al otro. UN SOLO proceso por servicio (sin `--workers`).

## Variables de entorno

| Variable | Por defecto | |
|---|---|---|
| `API_KEY` | — | obligatoria para `/estado` (sin ella, 503). Es la que usan los programas que leen `/estado` |
| `ADMIN_KEY` | — | para `/admin/token`, distinta de `API_KEY`; sin ella, `/admin/token` desactivado (503). Solo en Render y en credenciales.env |
| `DATA_DIR` | `/data` | debe ser un disco persistente |
| `EXIGIR_DISCO` | `1` | `0` solo en pruebas locales |
| `ZONA_GEOJSON` | `zonas/alcala.geojson` | término municipal |
| `ZONA_NOMBRE` | el `nombre` del GeoJSON | |
| `MARGEN_KM` | `5` | en Render, `1` desde el 23-nov-2026 |
| `UMBRAL` | `4.5` | K, T38 |
| `INTENSO` | `8` | K, T38 para «intenso» |
| `COLECCION` | `EO:EUM:DAT:0665` | |
| `TROZO` | `34` | |
| `INTERVALO_S` | `60` | segundos entre vueltas |
| `BUCLE` | `1` | `0`: no arranca el bucle (pruebas, `repaso.py`) |

## Pruebas (no llaman a EUMETSAT)

- `python pruebas/prueba_motor.py <carpeta investigacion-calor> [--margenes 5,1] [--sin-g4g5]`: el motor da
  **las mismas alarmas** que `fci_alarma.py` barrido a barrido en los 10 días extraídos, desde el primer barrido de
  cada día, con 5 y con 1 km; `vis` igual a la calculada con la máscara del análisis; y un reinicio a mitad de un
  incendio (fondo guardado y cargado) sigue exactamente igual. **No** prueba la lectura del fichero ni el bucle.
- `python pruebas/prueba_lectura.py <carpeta investigacion-calor>`: con un trozo 34 real, la temperatura de brillo es
  idéntica a la del análisis validado, el recorte deja 15 píxeles, el tamaño del recorte no cambia la zona, y las
  comprobaciones de proyección, `valid_range` y calidad.
- `python pruebas/prueba_servicio.py`: el bucle y el token con EUMETSAT simulado (arranque, reinicio con `fondo.npz`,
  barridos tardíos, huecos, fallos, estados de `/health`, `vis`, claves). Necesita las dependencias de `requirements.txt`.
- `DATA_DIR=<carpeta con tokens.json propio> MARGEN_KM=1 python repaso.py 2026-05-13T20:00 2026-05-13T21:00`: pasa un
  periodo pasado por el mismo código del servicio (búsqueda, descarga, lectura y motor). Sí llama a EUMETSAT.
- `python pruebas/arranque_local.py`: el servicio en local (puerto 8091, `API_KEY=prueba-local`).
