#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${PROJECT_DIR}"

if command -v git >/dev/null 2>&1 && [ -d .git ]; then
  git pull --ff-only
fi

docker compose -f docker-compose.prod.yml up -d postgres redis --build
docker compose -f docker-compose.prod.yml run --rm api python -m alembic upgrade head
docker compose -f docker-compose.prod.yml up -d --build api bot worker
