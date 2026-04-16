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

# Install ONLY dependencies (not the project itself) — avoids slow wheel build
COPY pyproject.toml ./
RUN "${VENV_PATH}/bin/pip" install \
    aiogram \
    fastapi \
    "uvicorn[standard]" \
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
    segno


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

USER appuser

CMD ["python", "-m", "apps.bot.main"]
