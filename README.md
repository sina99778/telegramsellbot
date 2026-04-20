# TelegramSellBot

TelegramSellBot is a production-oriented Telegram sales bot for selling and managing VPN or proxy subscriptions. The project combines:

- `aiogram` for the Telegram bot
- `FastAPI` for webhooks and admin APIs
- PostgreSQL for persistent data
- Redis for queueing and background coordination
- X-UI integration for provisioning and lifecycle management
- NOWPayments and TetraPay payment integrations

## Current Release Status

This repository is now documented for public visibility, but it is still intended for operators who are comfortable deploying Python services with Docker.

Important operational notes:

- The application currently bootstraps database tables with SQLAlchemy `create_all`.
- The `migrations/` directory does not yet contain a complete Alembic history for fresh-to-latest upgrades.
- `NOWPAYMENTS_IPN_SECRET` should always be configured in production.

## Features

- Telegram bot flows for purchase, renewal, wallet top-up, support, and self-service actions
- Admin panels for plans, discounts, broadcasts, users, stats, support, subscriptions, and servers
- Payment webhook handling for NOWPayments and TetraPay
- Background workers for expiry notifications, broadcasts, reconciliation, backups, and health checks
- Dockerized deployment for API, bot, worker, PostgreSQL, and Redis
- Automated tests covering payments, discounts, and webhook validation paths

## Repository Layout

- `apps/api/` FastAPI application and HTTP routes
- `apps/bot/` Telegram bot handlers, keyboards, states, and middleware
- `apps/worker/` scheduled jobs and worker entrypoint
- `core/` settings, database bootstrap, security helpers, and shared utilities
- `models/` SQLAlchemy models
- `repositories/` data access layer
- `services/` external integrations and business services
- `tests/` regression tests

## Requirements

- Python `3.12+`
- Docker Engine with either `docker compose` plugin or `docker-compose`
- A PostgreSQL-compatible runtime through the bundled Docker Compose stack
- A Redis runtime through the bundled Docker Compose stack
- Telegram bot token
- X-UI panel credentials
- NOWPayments credentials
- Optional TetraPay credentials

## Quick Start

1. Copy `.env.example` to `.env`.
2. Fill in all required secrets and callback URLs.
3. Review `docker-compose.prod.yml`.
4. Run the installer or deploy scripts on a Linux host:

```bash
chmod +x install.sh setup.sh deploy.sh
./install.sh
```

For an existing prepared host you can also run:

```bash
./deploy.sh full
```

## Environment Variables

Use `.env.example` as the canonical reference. The most important values are:

- `BOT_TOKEN`
- `OWNER_TELEGRAM_ID`
- `APP_SECRET_KEY`
- `DATABASE_URL`
- `REDIS_URL`
- `POSTGRES_PASSWORD`
- `REDIS_PASSWORD`
- `XUI_BASE_URL`
- `XUI_USERNAME`
- `XUI_PASSWORD`
- `NOWPAYMENTS_API_KEY`
- `NOWPAYMENTS_IPN_SECRET`
- `NOWPAYMENTS_IPN_CALLBACK_URL`
- `ADMIN_API_KEY`

## Development

Install dependencies:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .[dev]
```

Run tests:

```bash
pytest -q
```

## CI

GitHub Actions runs the test suite on pushes and pull requests to `master` and `main`.

## Security

- Never commit a real `.env`.
- Rotate all provider secrets before using this project in production.
- Use strong, unique values for `APP_SECRET_KEY`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, and `ADMIN_API_KEY`.
- Do not expose internal admin routes without network controls.

See [SECURITY.md](SECURITY.md) for disclosure guidance and deployment cautions.

## Database Notes

See [docs/DATABASE.md](docs/DATABASE.md) for the current bootstrap and migration story.

## License

This repository is released publicly for visibility, but the code remains proprietary. See [LICENSE](LICENSE).
