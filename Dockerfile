# Slim image for the Intelligence API (Phase 1).
# The detection pipeline (OpenCV/YOLO) runs separately and is intentionally not
# baked into this image to keep `docker compose up` fast and small.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App + dashboard + pipeline code (dashboard/pipeline are light, no CV deps).
COPY app/ ./app/
COPY dashboard/ ./dashboard/
COPY pipeline/ ./pipeline/
COPY data/ ./data/

# Persist the SQLite file outside the layer (mounted as a volume by compose).
ENV DATABASE_URL=sqlite:////data/store_intelligence.db

EXPOSE 8000

# Container-level health probe hits the API's own /health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
