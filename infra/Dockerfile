FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CALIPER_BACKEND=vertex \
    CALIPER_LEDGER=/data/ledger.db

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir . && mkdir -p /data

# Cloud Run supplies PORT; default matches the local `caliper serve` port.
ENV PORT=8080
EXPOSE 8080

# No credentials are baked in. On Cloud Run the runtime service account
# provides ADC for Vertex; locally, mount ~/.config/gcloud.
CMD exec uvicorn caliper.api:api --host 0.0.0.0 --port ${PORT}
