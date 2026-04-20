# TelegramSellBot

English README: [README.md](README.md)

TelegramSellBot یک ربات سلف‌هاست برای فروش و مدیریت اشتراک VPN یا Proxy در تلگرام است. این پروژه فقط یک بات ساده نیست؛ API، وبهوک پرداخت، پردازش‌های پس‌زمینه، اتصال به X-UI و اسکریپت‌های استقرار را هم در یک مخزن کنار هم می‌آورد.

## نصب روی سرور

### نصب یک‌خطی

روی یک سرور Ubuntu و با کاربر `root` این دستور را اجرا کنید:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/sina99778/telegramsellbot/master/setup.sh)
```

این دستور:

- پیش‌نیازهای پایه را نصب می‌کند
- پروژه را داخل `/opt/telegramsellbot` می‌ریزد
- نصاب تعاملی را اجرا می‌کند

### نصب دستی روی سرور

اگر بخواهید همه‌چیز را دستی انجام بدهید:

```bash
sudo -i
apt-get update
apt-get install -y git curl rsync
git clone https://github.com/sina99778/telegramsellbot.git /opt/telegramsellbot
cd /opt/telegramsellbot
chmod +x setup.sh install.sh deploy.sh
bash install.sh
```

بعد از آن، نصاب تعاملی این کارها را برایت جلو می‌برد:

- ساخت `.env`
- نصب Docker، Nginx و Certbot
- استقرار API، bot، worker، PostgreSQL و Redis
- reload سرویس‌ها
- آپدیت فایل‌های پروژه بدون دست‌زدن به `.env`

## این پروژه چه کاری انجام می‌دهد

- فروش پلن از داخل تلگرام
- ساخت خودکار کانفیگ بعد از پرداخت موفق
- پشتیبانی از تمدید، شارژ کیف پول، کد تخفیف و تیکت
- دریافت وبهوک از درگاه‌های پرداخت
- اجرای jobهای زمان‌بندی‌شده برای اعلان انقضا، reconciliation، برودکست، بکاپ و مانیتورینگ

## تکنولوژی‌ها

- بات: `aiogram`
- API: `FastAPI`
- دیتابیس: `PostgreSQL`
- کش و هماهنگی: `Redis`
- ORM: `SQLAlchemy`
- زمان‌بندی: `APScheduler`
- سرویس‌های خارجی: `NOWPayments`، `TetraPay`، `Sanaei X-UI`

## بخش‌های اصلی پروژه

- `apps/bot/` هندلرها، کیبوردها، stateها و middlewareهای ربات
- `apps/api/` اپ FastAPI، مسیرهای ادمین، mini-app و وبهوک‌های پرداخت
- `apps/worker/` jobهای پس‌زمینه
- `services/` منطق پرداخت، کیف پول، provision، نوتیفیکیشن و X-UI
- `models/` و `repositories/` لایه داده
- `core/` تنظیمات، bootstrap دیتابیس، امنیت و ابزارهای مشترک
- `tests/` تست‌های رگرشن مسیرهای حساس

## قابلیت‌ها

### سمت کاربر

- شروع و onboarding
- خرید و دریافت خودکار کانفیگ
- تمدید سرویس
- شارژ و مصرف کیف پول
- ارسال تیکت پشتیبانی
- دریافت مجدد کانفیگ

### سمت ادمین

- مدیریت پلن‌ها
- مدیریت کدهای تخفیف
- جست‌وجوی کاربر و سفارش
- مدیریت اشتراک‌ها
- رسیدگی به تیکت و recovery
- برودکست و retargeting
- مدیریت سرورها و اطلاعات X-UI
- آمار و نمای مالی

### پردازش‌های پس‌زمینه

- reconciliation پرداخت
- اعلان انقضا
- ارسال برودکست
- retargeting
- بکاپ
- بررسی سلامت سرورها

## پیش‌نیازها

- سرور Ubuntu با دسترسی `root`
- دامنه‌ای که به سرور اشاره کند تا وبهوک و SSL درست بالا بیاید
- توکن ربات تلگرام
- آدرس و اطلاعات پنل X-UI
- اطلاعات NOWPayments
- اطلاعات TetraPay در صورت نیاز

## توسعه محلی

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .[dev]
pytest -q
```

## نکات عملیاتی مهم

- مخزن عمومی است، اما کد همچنان proprietary است. فایل [LICENSE](LICENSE) را ببینید.
- در محیط production باید `NOWPAYMENTS_IPN_SECRET` حتما تنظیم شود.
- برای app، دیتابیس، Redis و دسترسی ادمین از secretهای قوی و یکتا استفاده کنید.
- مسیر فعلی bootstrap دیتابیس هنوز به ساخت schema از روی metadata متکی است.
- پوشه `migrations/` هنوز history کامل Alembic برای همه سناریوهای ارتقا را پوشش نمی‌دهد.
- برای نصب یک‌خطی باید از `setup.sh` استفاده شود، نه `install.sh`، چون `setup.sh` اول مخزن را روی سرور دریافت می‌کند

جزئیات بیشتر:

- راهنمای امنیت: [SECURITY.md](SECURITY.md)
- وضعیت دیتابیس: [docs/DATABASE.md](docs/DATABASE.md)

## CI

در GitHub Actions روی push و pull requestهای `master` و `main` تست‌ها اجرا می‌شوند.
