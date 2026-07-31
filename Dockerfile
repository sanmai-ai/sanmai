# SanMai AI core — Cloud Run image.
#
# The app package is `be` (imports are absolute: `be.app.main`, `be.config`),
# so `pip install -e .` puts `be` + `cli` on the path and the entrypoint is
# `be.app.main:app`. Runtime talks to Postgres over asyncpg via the Cloud SQL
# unix socket (SANMAI_DATABASE_URL uses `?host=/cloudsql/<conn>`); Cloud Run
# mounts that socket when the service is deployed with --add-cloudsql-instances.
FROM python:3.11-slim

# Faster, quieter, unbuffered logs for Cloud Run.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install deps first (better layer caching), then the package itself.
COPY pyproject.toml README.md ./
COPY be/ ./be/
COPY cli/ ./cli/
RUN pip install -e .

# Cloud Run provides $PORT (default 8080). Bind the app factory instance.
ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "uvicorn be.app.main:app --host 0.0.0.0 --port ${PORT}"]
