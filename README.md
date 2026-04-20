# TelegramSellBot

Persian README: [README-FA.md](README-FA.md)

TelegramSellBot is a self-hosted Telegram sales bot for operators who sell and manage VPN or proxy subscriptions through an X-UI panel. It bundles a Telegram bot, HTTP API, payment webhooks, background jobs, and deployment scripts into a single repository.

## Install On A Server

### One-Line Installer

Run this on a fresh Ubuntu server as `root`:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/sina99778/telegramsellbot/master/setup.sh)
```

What it does:

- installs required base packages
- downloads the project into `/opt/telegramsellbot`
- launches the interactive installer

### Manual Installation

If you prefer to install manually on the server:

```bash
sudo -i
apt-get update
apt-get install -y git curl rsync
git clone https://github.com/sina99778/telegramsellbot.git /opt/telegramsellbot
cd /opt/telegramsellbot
chmod +x setup.sh install.sh deploy.sh
bash install.sh
```

The interactive installer then helps you:

- build `.env`
- install Docker, Nginx, and Certbot
- deploy API, bot, worker, PostgreSQL, and Redis
- reload services later
- update project files without touching `.env`

## What It Does

- sells plans through Telegram
- provisions configs on X-UI after successful payment
- supports renewals, top-ups, discounts, support tickets, and admin workflows
- exposes webhook endpoints for payment providers
- runs scheduled jobs for expiry reminders, reconciliation, broadcasts, backups, and health checks

## Stack

- Bot: `aiogram`
- API: `FastAPI`
- Database: `PostgreSQL`
- Cache and coordination: `Redis`
- ORM: `SQLAlchemy`
- Scheduler: `APScheduler`
- Providers: `NOWPayments`, `TetraPay`, `Sanaei X-UI`

## Main Components

- `apps/bot/` Telegram bot handlers, keyboards, states, and middlewares
- `apps/api/` FastAPI app, admin endpoints, mini-app routes, and payment webhooks
- `apps/worker/` background jobs for operational automation
- `services/` payment, wallet, provisioning, notification, and X-UI integrations
- `models/` and `repositories/` persistence layer
- `core/` settings, database bootstrap, security, and shared utilities
- `tests/` regression coverage for critical flows

## Feature Overview

### User Flows

- start and onboarding
- purchase and automatic provisioning
- renewal and top-up flows
- wallet charging and spending
- support tickets
- config delivery and resend flows

### Admin Flows

- plan management
- discount code management
- user lookup and search
- subscription management
- support and recovery actions
- broadcast and retargeting tools
- server and X-UI credential management
- stats and financial overview

### Background Jobs

- payment reconciliation
- expiry notifications
- broadcast delivery
- retargeting
- backup jobs
- server health monitoring

## Requirements

- Ubuntu server with root access
- domain pointed to the server for webhook and SSL setup
- Telegram bot token
- X-UI panel URL and credentials
- NOWPayments credentials
- optional TetraPay credentials

## Local Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .[dev]
pytest -q
```

## Operational Notes

- The repository is public, but the software remains proprietary. See [LICENSE](LICENSE).
- Production deployments should always define `NOWPAYMENTS_IPN_SECRET`.
- Production deployments should always use strong secrets for app, database, Redis, and admin access.
- The current database bootstrap path still relies on SQLAlchemy metadata creation.
- The `migrations/` directory is not yet a full Alembic history for every historical upgrade path.
- the one-line installer uses `setup.sh`, not `install.sh`, because `setup.sh` is responsible for fetching the repository to the server first

More details:

- security guidance: [SECURITY.md](SECURITY.md)
- database notes: [docs/DATABASE.md](docs/DATABASE.md)

## CI

GitHub Actions runs the test suite on pushes and pull requests targeting `master` and `main`.
