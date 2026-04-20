# TelegramSellBot

نسخه فارسی README: [README.md](README.md)

TelegramSellBot یک ربات فروش تلگرام برای فروش و مدیریت اشتراک‌های VPN یا Proxy است که با نگاه عملیاتی و مناسب استقرار ساخته شده. این پروژه این بخش‌ها را کنار هم قرار می‌دهد:

- `aiogram` برای ربات تلگرام
- `FastAPI` برای وبهوک‌ها و API ادمین
- PostgreSQL برای داده‌های پایدار
- Redis برای صف و هماهنگی پردازش‌های پس‌زمینه
- اتصال به X-UI برای ساخت و مدیریت سرویس
- اتصال به NOWPayments و TetraPay برای پرداخت

## وضعیت فعلی انتشار

این مخزن حالا برای دیده‌شدن عمومی مستندسازی شده، اما همچنان بیشتر مناسب اپراتورهایی است که با استقرار سرویس‌های Python و Docker راحت هستند.

نکات مهم عملیاتی:

- ساختار دیتابیس فعلاً با `SQLAlchemy create_all` هم bootstrap می‌شود.
- پوشه `migrations/` هنوز history کامل Alembic برای ارتقا از اولین نسخه تا آخرین نسخه را ندارد.
- در محیط production باید حتماً `NOWPAYMENTS_IPN_SECRET` تنظیم شود.

## قابلیت‌ها

- فرایندهای خرید، تمدید، شارژ کیف پول، پشتیبانی و self-service در ربات
- پنل‌های ادمین برای پلن‌ها، کد تخفیف، برودکست، کاربران، آمار، تیکت‌ها، اشتراک‌ها و سرورها
- پردازش وبهوک‌های پرداخت NOWPayments و TetraPay
- workerهای پس‌زمینه برای اعلان انقضا، برودکست، reconciliation، بکاپ و بررسی سلامت سرورها
- استقرار Dockerized برای API، bot، worker، PostgreSQL و Redis
- تست‌های خودکار برای مسیرهای مهم پرداخت، تخفیف و اعتبارسنجی وبهوک

## ساختار مخزن

- `apps/api/` اپ FastAPI و routeهای HTTP
- `apps/bot/` handlerها، keyboardها، stateها و middlewareهای ربات
- `apps/worker/` jobهای زمان‌بندی‌شده و entrypoint ورکر
- `core/` تنظیمات، bootstrap دیتابیس، امنیت و ابزارهای مشترک
- `models/` مدل‌های SQLAlchemy
- `repositories/` لایه دسترسی به داده
- `services/` سرویس‌های کسب‌وکاری و اتصال به سرویس‌های خارجی
- `tests/` تست‌های رگرشن

## پیش‌نیازها

- Python `3.12+`
- Docker Engine به همراه `docker compose` یا `docker-compose`
- توکن ربات تلگرام
- اطلاعات پنل X-UI
- اطلاعات NOWPayments
- اطلاعات TetraPay در صورت نیاز

## شروع سریع

1. فایل `.env.example` را به `.env` کپی کنید.
2. همه secretها و callback URLهای لازم را پر کنید.
3. فایل `docker-compose.prod.yml` را مرور کنید.
4. روی یک هاست لینوکسی اسکریپت نصب یا استقرار را اجرا کنید:

```bash
chmod +x install.sh setup.sh deploy.sh
./install.sh
```

اگر هاست از قبل آماده است، می‌توانید این را هم اجرا کنید:

```bash
./deploy.sh full
```

## متغیرهای محیطی مهم

فایل `.env.example` مرجع اصلی تنظیمات است. مهم‌ترین متغیرها:

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

## توسعه

نصب وابستگی‌ها:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .[dev]
```

اجرای تست‌ها:

```bash
pytest -q
```

## CI

در GitHub Actions روی push و pull requestهای `master` و `main` تست‌ها اجرا می‌شوند.

## امنیت

- هیچ‌وقت فایل واقعی `.env` را commit نکنید.
- قبل از استفاده production همه secretها را rotate کنید.
- برای `APP_SECRET_KEY`، `POSTGRES_PASSWORD`، `REDIS_PASSWORD` و `ADMIN_API_KEY` از مقادیر قوی و یکتا استفاده کنید.
- routeهای ادمین را بدون کنترل دسترسی شبکه در معرض اینترنت باز نگذارید.

برای جزئیات بیشتر فایل [SECURITY.md](SECURITY.md) را ببینید.

## وضعیت دیتابیس

شرح فعلی bootstrap و migration در [docs/DATABASE.md](docs/DATABASE.md) آمده است.

## لایسنس

این مخزن به‌صورت عمومی قابل مشاهده است، اما کد همچنان proprietary باقی می‌ماند. فایل [LICENSE](LICENSE) را ببینید.
