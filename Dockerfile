# --- frontend build ---------------------------------------------------------------
FROM node:22.23.4-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- runtime -------------------------------------------------------------------------
FROM python:3.13.18-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    ABTEILO_FRONTEND_DIST=/app/web
WORKDIR /app
COPY backend/pyproject.toml ./
COPY backend/abteilo ./abteilo
RUN pip install . && rm -rf build abteilo.egg-info && useradd --system --uid 10001 abteilo
COPY backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY --from=web /web/dist ./web
USER abteilo
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz').status != 200)"
CMD ["uvicorn", "abteilo.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
