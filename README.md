# TelegramSellBot

Persian README: [README-FA.md](README-FA.md)

TelegramSellBot is a self-hosted Telegram sales bot for operators who sell and manage VPN or proxy subscriptions through an X-UI panel. It bundles a Telegram bot, HTTP API, payment webhooks, background jobs, and deployment scripts into a single repository.

## Latest Additions

- Ready-config sales mode for operators who cannot connect an X-UI panel: admins can create ready-config plans, upload a text list of configs, and the bot automatically delivers the next available config after payment.
- Telegram Mini App user panel for store, wallet top-ups, renewals, active services, support tickets, referrals, and payment refresh without sending the user back to chat flows.
- Telegram Mini App admin panel for stats, finance, users, customers, services, plans, ready-config inventory, servers, tickets, discounts, settings, audit logs, and operational actions.
- Mini App ticketing for users and admins, including threaded views, replies, and close actions inside the web app.
- Gateway payment review and refresh actions for payments that need automatic re-checking after callback delays.
- Cleaner Mini App interface using inline SVG icons instead of emoji-based navigation and cards.

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
- sells preloaded ready configs from uploaded text inventory when X-UI is not connected
- supports renewals, top-ups, discounts, support tickets, and admin workflows
- provides a Telegram Mini App for user and admin workflows
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
- `models/` and `repositories/` persistence layer, including ready-config inventory
- `core/` settings, database bootstrap, security, and shared utilities
- `tests/` regression coverage for critical flows

## Feature Overview

## Ready-Config Sales Guide

Use ready-config sales when you cannot connect an X-UI panel, or when you want to sell a fixed inventory of already-created configs.

1. Open the admin panel in the bot or Mini App.
2. Go to `Ready Config Sales`.
3. Create a ready-config plan with name, duration, volume, and price.
4. Upload a `.txt` file or paste text where each non-empty line is one full config link.
5. Keep the ready-config pool active.
6. Users buy the plan like any normal plan.
7. After wallet or gateway payment is confirmed, the bot takes the oldest available line, marks it sold, creates the subscription, and sends that config to the user.

Operational notes:

- One line is delivered to one customer only.
- Empty lines and duplicate uploaded configs are ignored.
- If the ready-config inventory is empty, the plan is hidden from user purchase lists.
- Plan stock can also be limited from plan management. A stock limit of `0` means unlimited plan sales. For ready-config plans, the effective visible stock is still capped by the number of available uploaded configs.
- Plan duration is applied from delivery time. Updating a plan duration affects future purchases, not already-delivered subscriptions.
- Admins can resend delivered configs from recovery/support tools if a user misses the message.

### User Flows

- start and onboarding
- purchase and automatic provisioning
- ready-config purchase and automatic delivery from uploaded inventory
- renewal and top-up flows
- wallet charging and spending
- support tickets
- config delivery and resend flows
- Mini App flows for store, wallet, services, tickets, and referral

### Admin Flows

- plan management
- ready-config plan and inventory management
- discount code management
- user lookup and search
- subscription management
- support and recovery actions
- broadcast and retargeting tools
- server and X-UI credential management
- stats and financial overview
- Mini App admin panel for common management actions

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
- optional ready-config text inventory when X-UI is unavailable
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
