# TelegramSellBot

Persian README: [README-FA.md](README-FA.md)

TelegramSellBot is a self-hosted Telegram sales system for VPN/proxy operators. It includes a Telegram bot, FastAPI backend, Telegram Mini App, payment webhooks, background workers, Sanaei X-UI integration, ready-config inventory, wallet accounting, ticketing, admin tools, reports, and deployment scripts.

## Main Features

- Sell plans and automatically provision configs through Sanaei X-UI after successful payment.
- Sell preloaded ready configs from uploaded `.txt` inventory without connecting X-UI.
- Let users buy custom volume and custom duration using admin-defined pricing.
- Renew services, buy extra volume, and buy extra time.
- Internal wallet with top-up and wallet checkout.
- NOWPayments, TetraPay, manual crypto payment, and card-to-card payment.
- Manual admin approval for crypto hashes and card-transfer receipt photos.
- Gateway payment refresh/review when callbacks arrive late.
- Optional mobile number verification before purchase, disabled by default.
- Iranian-only or any-phone-number verification modes.
- Discount codes, plan stock limits, plan inventory display, and unlimited-stock mode.
- Bulk volume/time gifts for active configs, all configs, all servers, or one server.
- Trial config option with admin toggle and per-user reset.
- Support tickets in both the bot and the Mini App.
- User Mini App and admin Mini App panels.
- Weekly purchase Excel report including purchased config name.
- Expiry and low-volume alerts with renewal actions.
- Backup, broadcast, retargeting, recovery, and payment reconciliation tools.

## Quick Server Install

Run this on a fresh Ubuntu server as `root`:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/sina99778/telegramsellbot/master/setup.sh)
```

The installer:

- installs required base packages
- places the project in `/opt/telegramsellbot`
- creates `.env` through an interactive flow
- installs Docker, Nginx, and Certbot
- starts `api`, `bot`, `worker`, `postgres`, and `redis`

## Manual Install

```bash
sudo -i
apt-get update
apt-get install -y git curl rsync
git clone https://github.com/sina99778/telegramsellbot.git /opt/telegramsellbot
cd /opt/telegramsellbot
chmod +x setup.sh install.sh deploy.sh
bash install.sh
```

Full deploy:

```bash
cd /opt/telegramsellbot
./deploy.sh full
```

Quick service reload:

```bash
cd /opt/telegramsellbot
./deploy.sh reload
```

Update to the latest version:

```bash
cd /opt/telegramsellbot
git pull
./deploy.sh full
```

## `.env` Configuration

Important environment variables:

- `BOT_TOKEN`: Telegram bot token from BotFather
- `BOT_USERNAME`: bot username without `@`
- `OWNER_TELEGRAM_ID`: numeric Telegram ID of the owner
- `ADMIN_API_KEY`: internal admin API key
- `DATABASE_URL`: PostgreSQL connection URL inside Docker
- `REDIS_URL`: Redis connection URL inside Docker
- `XUI_BASE_URL`, `XUI_USERNAME`, `XUI_PASSWORD`: X-UI panel connection
- `NOWPAYMENTS_API_KEY`, `NOWPAYMENTS_IPN_SECRET`, `NOWPAYMENTS_IPN_CALLBACK_URL`: NOWPayments settings
- `TETRAPAY_API_KEY`, `TETRAPAY_CALLBACK_URL`: TetraPay settings
- `WEB_BASE_URL`: public domain for the backend and Mini App
- `SUPPORT_URL`: support or panel URL

Card-to-card settings, manual crypto wallets, phone verification, custom purchase pricing, trial config, referral settings, and Toman exchange rate are configured from the admin panel, not from `.env`.

## First Run

1. Create a bot in BotFather and place the token in `.env`.
2. Point your domain to the server and set `WEB_BASE_URL`.
3. If using gateways, set provider callbacks:
   `https://your-domain.com/api/webhooks/nowpayments`
   `https://your-domain.com/api/webhooks/tetrapay`
4. Run `./deploy.sh full`.
5. Open Telegram and send `/start` to the bot. If `OWNER_TELEGRAM_ID` is correct, the owner can access admin menus.
6. Configure servers, plans, gateways, and sales settings.

## Admin Menu

Important admin sections:

- Server management: X-UI credentials, config domain, subscription domain, and max clients
- Plan management: create plans and edit name, price, duration, volume, stock, and active status
- Ready-config sales: create ready-config plans and upload config inventory
- User management: search users, inspect status, adjust balance, message users, and manage access
- Customers and services: inspect purchases, active services, and config details
- Finance and payments: view payments, review gateways, approve/reject manual payments
- Bot settings: renewal pricing, Toman rate, custom purchase, gateways, phone verification, trial config, referral, and force-join
- Tickets: reply to tickets and close them
- Discounts: create and manage discount codes
- Volume/time gifts: apply gifts to active configs, all configs, all servers, or one server
- Recovery and reconciliation: recover deliveries and recheck payments
- Reports and backup: Excel reports, statistics, and manual backups

## Payment Gateway Setup

Open `Admin Panel -> Bot Settings -> Payment Gateways`. Each gateway has its own menu.

### NOWPayments

1. Open the NOWPayments menu.
2. Enable or disable the gateway.
3. Optionally set an API key and IPN secret in the admin panel. Empty values fall back to `.env`.
4. In NOWPayments, set IPN callback to `NOWPAYMENTS_IPN_CALLBACK_URL`.
5. After confirmed payment, the webhook automatically credits wallet or delivers the purchased config.

### TetraPay

1. Open the TetraPay menu.
2. Enable or disable the gateway.
3. Set the API key or use the `.env` value.
4. Set USD to Toman exchange rate in bot settings.
5. Users choose the Rial gateway and the bot processes delivery after verification.

### Manual Crypto Payment

1. Open the manual crypto menu.
2. Enable the gateway.
3. Select the currency, for example `USDT TRC20`.
4. Add one or more wallet addresses.
5. Users select the currency, see amount and address, and submit TX hash.
6. Admin approves or rejects the request.
7. If it is a wallet top-up, the wallet is credited. If it is a direct purchase, the config is delivered after approval.

### Card-To-Card Payment

1. Open `Payment Gateways -> Card To Card`.
2. Set card number, cardholder name, bank name, and optional payment note.
3. Enable card-to-card payment.
4. Users choose card-to-card during purchase or wallet top-up.
5. The bot shows Toman amount and card details.
6. User sends a receipt photo after payment.
7. Admins receive the receipt with approve/reject buttons.
8. Approval delivers the config for direct purchases or credits the wallet for top-ups.

## Phone Verification

Open `Admin Panel -> Bot Settings -> Phone Verification`.

- Disabled by default.
- Admin can enable or disable it.
- `Iran only` accepts Iranian mobile numbers.
- `Any number` accepts international or numeric phone numbers.

When enabled, users must send their own mobile number before seeing purchase plans. The verified number is stored in the user profile and is not requested again.

## Normal Plan Setup

1. Open plan management.
2. Create a new plan.
3. Select the target X-UI inbound.
4. Enter plan name, duration, volume, and price.
5. Optionally configure stock/sales limit.
6. Stock limit `0` means unlimited and is not shown to users.
7. Active plans appear in the bot store and Mini App store.

## Editing Plan Price, Duration, Volume, And Stock

From plan details, admins can change:

- plan duration
- plan volume
- purchase price
- stock limit
- active/inactive status

Changes affect future purchases only. Existing subscriptions are not modified.

## Ready-Config Sales

Use ready-config sales when you cannot connect X-UI or already have a fixed list of configs.

1. Open `Ready Config Sales`.
2. Create a ready-config plan with name, duration, volume, and price.
3. Upload a `.txt` file where each line is one full config.
4. Keep the pool active.
5. Users buy the ready plan like any normal plan.
6. After payment, the bot takes the oldest available config, marks it sold, creates the subscription record, and sends that config to the user.

Operational notes:

- One line is delivered to one customer only.
- Empty lines and duplicate configs are ignored during upload.
- If inventory is empty, the plan is hidden from user purchase lists.
- If a plan stock limit is set, effective stock is capped by both plan stock and available ready configs.

## Custom Volume And Duration Purchases

Open `Bot Settings -> Custom Purchase`.

1. Enable custom purchase.
2. Set price per 1 GB.
3. Set price per 1 day.
4. Users see the custom volume/duration option in the store.
5. Users enter volume, duration, config name, and payment method.
6. After payment, the bot creates a config with the requested limits.

This feature needs at least one active plan connected to an inbound so the bot can use it as the template for provisioning.

## Trial Config

Open `Bot Settings -> Trial Config`.

- Admin can enable or disable trial configs.
- Each user can receive one trial by default.
- Admin can reset a user's trial limit.

## Renewal And Volume/Time Gifts

Users can renew from `My Services` or from low-volume/expiry alerts.

Admins can gift:

- volume
- time
- both volume and time
- to active configs only
- to all configs
- across all servers
- on one specific server

## Wallet

Users can top up wallets through gateways, manual crypto, or card-to-card. They can then buy plans using wallet balance.

Wallet is used for:

- faster purchases
- manual admin balance adjustments
- automatic refunds after provisioning failures
- referral rewards

## Discount Codes

Admins can create and manage discount codes with:

- discount percent
- max uses
- expiry
- active/inactive status

Users can enter a code during purchase or continue without a code.

## Support Tickets

Users can:

- open tickets
- send text or photos
- see admin replies in the same thread

Admins can:

- view tickets from bot or Mini App
- reply
- close tickets

Ticket messages are not deleted when admins reply; the conversation is updated.

## Telegram Mini App

### User Panel

- Home and account summary
- Store and plan purchase
- Custom volume/duration purchase
- Wallet top-up
- My services and renewal
- Support tickets
- Referral
- Payment refresh

### Admin Panel

- Stats and reports
- Finance and payments
- Users and search
- Customers
- Services
- Plans and inventory
- Ready-config sales
- Tickets
- Discounts
- Settings
- Admin actions

Only admins can see the Mini App admin-panel button.

## Reports

The weekly purchase Excel report includes purchase details, user, amount, payment method, and purchased config name.

## Operations

Useful server commands:

```bash
cd /opt/telegramsellbot
./deploy.sh full
./deploy.sh reload
docker compose -f docker-compose.prod.yml logs -f bot
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml logs -f worker
```

If a gateway payment is not delivered:

1. Open finance/payment management.
2. Find the payment.
3. Press review/refresh.
4. If the provider confirms payment, the bot automatically credits wallet or delivers the config.

## Local Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .[dev]
pytest -q
```

## Project Structure

- `apps/bot/`: Telegram bot handlers, keyboards, states, and middleware
- `apps/api/`: FastAPI, Mini App routes, and payment webhooks
- `apps/worker/`: background jobs
- `models/`: database models
- `repositories/`: data access and app settings
- `services/`: payment, provisioning, wallet, X-UI, notifications, and domain logic
- `miniapp/`: Mini App frontend
- `tests/`: regression tests

## Security And Operational Notes

- The repository is public, but the software remains proprietary. See [LICENSE](LICENSE).
- Use strong secrets for PostgreSQL, Redis, `APP_SECRET_KEY`, and `ADMIN_API_KEY`.
- Always set `NOWPAYMENTS_IPN_SECRET` in production.
- Never commit `.env`.
- Gateway callbacks require a valid public domain and SSL.
- The `migrations/` directory is not yet a full Alembic history for every historical upgrade path; current deployment still uses metadata-based bootstrap paths.

More details:

- Security guidance: [SECURITY.md](SECURITY.md)
- Database notes: [docs/DATABASE.md](docs/DATABASE.md)

## CI

GitHub Actions runs tests on pushes and pull requests targeting `master` and `main`.
