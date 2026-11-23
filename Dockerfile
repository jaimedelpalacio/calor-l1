# Dockerfile — calor-l1 (servicio web en Render)
FROM python:3.11-slim

WORKDIR /app

# Todas las dependencias tienen ruedas binarias para Linux (h5py, numpy, pyproj, shapely): no hace falta compilar
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py eumetsat.py motor.py servicio.py repaso.py ./
COPY zonas ./zonas

ENV HOST=0.0.0.0 \
    PORT=8080 \
    DATA_DIR=/data

EXPOSE 8080

# UN SOLO proceso: nunca --workers (el bucle y el refresh token de EUMETSAT no deben duplicarse)
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
