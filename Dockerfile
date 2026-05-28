# ─── Stage 1: Python dependencies ───────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VENV_PATH=/opt/venv

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && python -m venv "${VENV_PATH}" \
    && "${VENV_PATH}/bin/pip" install --upgrade pip setuptools wheel \
    && rm -rf /var/lib/apt/lists/*

# Install ONLY dependencies (not the project itself) — avoids slow wheel build.
# Note: the version pins MUST stay in sync with pyproject.toml. The Dockerfile
# does NOT read pyproject (faster build), so adding a dep there is not enough —
# you must also list it here.
COPY pyproject.toml ./
RUN "${VENV_PATH}/bin/pip" install \
    "aiogram>=3.27.0,<4.0.0" \
    fastapi \
    "uvicorn[standard]" \
    "python-multipart>=0.0.9" \
    sqlalchemy \
    asyncpg \
    alembic \
    "redis[hiredis]" \
    pydantic \
    pydantic-settings \
    httpx \
    orjson \
    python-dotenv \
    structlog \
    apscheduler \
    tenacity \
    cryptography \
    segno \
    pillow


# ─── Stage 2: Vue 3 dashboard build ─────────────────────────────────────
# Built separately so a Python-only change doesn't trigger an npm install.
FROM node:20-alpine AS dashboard-builder

WORKDIR /dashboard

# Copy manifests first for cache-friendly layer.
COPY dashboard/package.json dashboard/package-lock.json* ./
RUN npm install --no-audit --no-fund

# Now copy the rest of the source and build.
COPY dashboard/ ./
RUN npm run build


# ─── Stage 3: Runtime image ─────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VENV_PATH=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH="/app"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl postgresql-client \
    && groupadd --system appgroup \
    && useradd --system --gid appgroup --create-home --home-dir /home/appuser appuser \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY --chown=appuser:appgroup pyproject.toml ./
COPY --chown=appuser:appgroup apps ./apps
COPY --chown=appuser:appgroup core ./core
COPY --chown=appuser:appgroup models ./models
COPY --chown=appuser:appgroup repositories ./repositories
COPY --chown=appuser:appgroup schemas ./schemas
COPY --chown=appuser:appgroup services ./services
COPY --chown=appuser:appgroup miniapp ./miniapp
COPY --chown=appuser:appgroup scripts ./scripts
# Vue 3 dashboard — built in stage 2 above. We copy only `dist/` so the
# runtime image stays small (no node_modules baggage). FastAPI serves
# this directory at /dashboard/ — see apps/api/main.py.
COPY --from=dashboard-builder --chown=appuser:appgroup /dashboard/dist /app/dashboard/dist

USER appuser

CMD ["python", "-m", "apps.bot.main"]
