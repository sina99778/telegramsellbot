from __future__ import annotations


class Buttons:
    BUY_CONFIG = "🛍 خرید کانفیگ"
    PROFILE_WALLET = "👤 پروفایل و کیف پول"
    SUPPORT = "🛠 پشتیبانی (تیکت)"
    FREE_TRIAL = "🎁 اکانت تست رایگان"
    TOPUP_CRYPTO = "💳 شارژ کیف پول (کریپتو)"
    CUSTOM_AMOUNT = "مبلغ دلخواه"
    OPEN_PAYMENT = "🔗 باز کردن صفحه پرداخت"
    PREV = "⬅️ قبلی"
    NEXT = "بعدی ➡️"


class Messages:
    MENU_PLACEHOLDER = "یکی از گزینه‌ها را انتخاب کنید"
    WELCOME_NEW = (
        "سلام {name}.\n\n"
        "حساب کاربری و کیف پول شما آماده است. از منوی پایین می‌توانید کانفیگ بخرید، "
        "موجودی خود را مدیریت کنید و با پشتیبانی در ارتباط باشید."
    )
    WELCOME_BACK = (
        "خوش برگشتی {name}.\n\n"
        "داشبورد شما آماده است. از منوی پایین ادامه بده."
    )
    WALLET_NOT_FOUND = "کیف پول شما بارگذاری نشد. لطفاً دوباره /start را بزنید."
    PROFILE_OVERVIEW = (
        "پروفایل: {name}\n"
        "موجودی: {balance} دلار\n"
        "سقف اعتبار: {credit_limit} دلار"
    )
    TOPUP_CHOOSE_AMOUNT = "مبلغ شارژ را انتخاب کنید یا یک مبلغ دلخواه وارد کنید."
    TOPUP_ENTER_CUSTOM = "مبلغ دلخواه شارژ را به دلار وارد کنید. مثال: `12.50`"
    TOPUP_INVALID_AMOUNT = "مبلغ واردشده معتبر نیست. لطفاً عددی مثل `10` یا `12.50` وارد کنید."
    TOPUP_AMOUNT_GT_ZERO = "مبلغ باید بیشتر از صفر باشد."
    ACCOUNT_NOT_FOUND = "حساب شما پیدا نشد. لطفاً دوباره /start را بزنید."
    ACCESS_DENIED = "دسترسی شما به ربات محدود شده است. برای پیگیری با پشتیبانی تماس بگیرید."
    PAYMENT_GATEWAY_UNAVAILABLE = "درگاه پرداخت موقتاً در دسترس نیست. کمی بعد دوباره تلاش کنید."
    TOPUP_INVOICE_CREATED = (
        "فاکتور شارژ کیف پول به مبلغ {amount} دلار ساخته شد.\n\n"
        "از دکمه زیر وارد صفحه پرداخت شوید. بعد از تایید NOWPayments، "
        "کیف پول شما به صورت خودکار شارژ می‌شود."
    )
    NO_PLANS_AVAILABLE = "در حال حاضر هیچ پلنی برای خرید موجود نیست. لطفاً بعداً دوباره تلاش کنید."
    CHOOSE_PLAN = "پلن مورد نظر خود را برای خرید انتخاب کنید:"
    PLAN_NOT_AVAILABLE = "این پلن در حال حاضر در دسترس نیست."
    INSUFFICIENT_BALANCE = (
        "موجودی کیف پول شما {balance} دلار است، اما قیمت این پلن {price} {currency} است.\n"
        "لطفاً ابتدا کیف پول خود را شارژ کنید."
    )
    BALANCE_NOT_SUFFICIENT_ANYMORE = "موجودی شما دیگر کافی نیست. لطفاً کیف پول را شارژ کرده و دوباره تلاش کنید."
    PROVISIONING_FAILED_REFUNDED = (
        "در حال حاضر پنل نتوانست کانفیگ شما را ایجاد کند. "
        "مبلغ به صورت خودکار به کیف پول شما برگشت داده شد."
    )
    TRIAL_ALREADY_RECEIVED = "شما قبلاً اکانت تست خود را دریافت کرده‌اید."
    TRIAL_PLAN_NOT_FOUND = "در حال حاضر اکانت تست موجود نیست. لطفاً به پشتیبانی پیام دهید."
    CONFIG_CREATED = (
        "کانفیگ شما با موفقیت ساخته شد.\n\n"
        "پلن: {plan_name}\n"
        "حجم: {volume_label}\n"
        "کاربر: {client_email}\n"
        "ساب لینک: {sub_link}\n\n"
        "زمان اکانت از اولین استفاده شروع می‌شود."
    )
    CANCELLED = "عملیات لغو شد."


class SupportTexts:
    START = "مشکل خود را بنویسید. پشتیبانی در همین‌جا به شما پاسخ می‌دهد. برای لغو /cancel را بفرستید."
    ACCOUNT_NOT_FOUND = "حساب شما پیدا نشد. لطفاً اول /start را بزنید."
    TICKET_CREATED = "پیام شما برای پشتیبانی ارسال شد. شناسه تیکت: {ticket_id}"
    ADMIN_ALERT = (
        "تیکت جدید پشتیبانی\n\n"
        "تیکت: {ticket_id}\n"
        "کاربر: {name}\n"
        "تلگرام آیدی: {telegram_id}\n"
        "پیام: {message}"
    )
    ADMIN_REPLY_BUTTON = "💬 پاسخ (تیکت #{ticket_short})"
    ADMIN_REPLY_PROMPT = "پاسخ این تیکت را بنویسید. برای لغو /cancel را بفرستید."
    ADMIN_NO_TICKET = "هیچ تیکتی انتخاب نشده است."
    ADMIN_TICKET_NOT_FOUND = "تیکت پیدا نشد."
    ADMIN_REPLY_SENT = "پاسخ با موفقیت ارسال شد."
    ADMIN_USER_BLOCKED = "کاربر ربات را بلاک کرده است. تیکت به صورت خودکار بسته شد."
    USER_REPLY = "پاسخ پشتیبانی برای تیکت {ticket_id}\n\n{message}"
    CLOSE_TICKET = "🔒 بستن تیکت"
    TICKET_CLOSED = "تیکت `{ticket_id}` بسته شد."


class AdminButtons:
    MANAGE_SERVERS = "🖥 مدیریت سرورها"
    MANAGE_PLANS = "📦 مدیریت پلن‌ها"
    STATISTICS = "📊 آمار و گزارش‌ها"
    MANAGE_USERS = "👥 مدیریت کاربران"
    BROADCAST = "📢 پیام همگانی"
    ADD_SERVER = "افزودن سرور"
    LIST_SERVERS = "لیست سرورها"
    TOGGLE_SERVER = "تغییر وضعیت فعال/غیرفعال"
    DELETE = "حذف"
    CREATE_PLAN = "ایجاد پلن"
    LIST_PLANS = "لیست پلن‌ها"
    TOGGLE_PLAN = "تغییر وضعیت فعال/غیرفعال"
    EDIT_BALANCE = "💰 تغییر موجودی"
    BAN_USER = "🚫 مسدود کردن کاربر"
    UNBAN_USER = "✅ رفع مسدودی کاربر"
    VIEW_CONFIGS = "📋 مشاهده کانفیگ‌های کاربر"
    REVOKE_CONFIG = "🗑 لغو کانفیگ"


class AdminMessages:
    PANEL_TITLE = "پنل مدیریت:"
    SERVER_MANAGEMENT = "مدیریت سرورها:"
    PLAN_MANAGEMENT = "مدیریت پلن‌ها:"
    NO_SERVERS = "هنوز هیچ سروری ثبت نشده است."
    NO_PLANS = "هنوز هیچ پلنی ثبت نشده است."
    ENTER_SERVER_NAME = "نام سرور را وارد کنید."
    ENTER_SERVER_BASE_URL = "آدرس پایه X-UI را وارد کنید. مثال: `http://1.2.3.4:2053`"
    ENTER_SERVER_USERNAME = "نام کاربری پنل X-UI را وارد کنید."
    ENTER_SERVER_PASSWORD = "رمز عبور پنل X-UI را وارد کنید."
    SERVER_CONNECTION_FAILED = "تست اتصال ناموفق بود. لطفاً آدرس و اطلاعات ورود را بررسی کنید."
    SERVER_CREATED = "سرور `{name}` با موفقیت اضافه شد."
    SERVER_NOT_FOUND = "سرور پیدا نشد."
    SERVER_TOGGLED = "سرور `{name}` اکنون {status} است."
    SERVER_SOFT_DELETED = "سرور `{name}` به منابع متصل است؛ بنابراین غیرفعال شد و به صورت نرم حذف شد."
    SERVER_DELETED = "سرور `{name}` حذف شد."
    ENTER_PLAN_NAME = "نام پلن را وارد کنید."
    ENTER_PROTOCOL = "پروتکل را وارد کنید: `vless` یا `vmess`"
    INVALID_PROTOCOL = "پروتکل باید `vless` یا `vmess` باشد."
    ENTER_DURATION = "مدت پلن را بر حسب روز وارد کنید."
    INVALID_INTEGER = "لطفاً یک عدد صحیح معتبر وارد کنید."
    DURATION_GT_ZERO = "مدت باید بیشتر از صفر باشد."
    ENTER_VOLUME = "حجم پلن را بر حسب گیگابایت وارد کنید."
    VOLUME_GT_ZERO = "حجم باید بیشتر از صفر باشد."
    ENTER_PRICE = "قیمت را به دلار وارد کنید."
    INVALID_PRICE = "لطفاً یک قیمت اعشاری معتبر وارد کنید."
    PRICE_GT_ZERO = "قیمت باید بیشتر از صفر باشد."
    PLAN_CREATED = "پلن `{name}` با موفقیت ساخته شد و اکنون برای خرید در دسترس است."
    PLAN_NOT_FOUND = "پلن پیدا نشد."
    PLAN_TOGGLED = "پلن `{name}` اکنون {status} است."
    STATS_DASHBOARD = (
        "داشبورد آمار\n\n"
        "کل کاربران: {total_users}\n"
        "اشتراک‌های فعال: {total_active_subscriptions}\n"
        "کل درآمد: {total_revenue} دلار\n"
        "سرورهای فعال: {total_active_servers}"
    )
    PERMISSION_DENIED = "شما دسترسی لازم را ندارید."
    ASK_USER_TELEGRAM_ID = "شناسه تلگرام کاربر را ارسال کنید."
    USER_NOT_FOUND = "کاربر پیدا نشد."
    USER_PROFILE = (
        "کاربر: {name}\n"
        "تلگرام آیدی: {telegram_id}\n"
        "وضعیت: {status}\n"
        "موجودی کیف پول: {wallet_balance} دلار\n"
        "تعداد کل سفارش‌ها: {total_orders}"
    )
    ENTER_BALANCE_ADJUSTMENT = "مبلغ را وارد کنید. عدد مثبت برای افزایش و عدد منفی برای کسر."
    AMOUNT_NOT_ZERO = "مبلغ نمی‌تواند صفر باشد."
    NO_ACTIVE_CONFIGS = "این کاربر هیچ کانفیگ فعال یا در انتظار فعالی‌سازی ندارد."
    SUBSCRIPTION_NOT_FOUND = "اشتراک پیدا نشد."
    SUBSCRIPTION_REVOKED = "کانفیگ `{subscription_id}` لغو شد."
    BROADCAST_START = "پیام همگانی را ارسال کنید. فعلاً متن و عکس پشتیبانی می‌شود."
    BROADCAST_UNSUPPORTED = "فعلاً فقط پیام متنی یا عکس برای ارسال همگانی پشتیبانی می‌شود."
    BROADCAST_CONFIRM = "برای قرار گرفتن در صف ارسال، `confirm` را بفرستید و برای لغو، `cancel` را بفرستید."
    BROADCAST_CANCELLED = "ارسال همگانی لغو شد."
    BROADCAST_CONFIRM_HINT = "فقط `confirm` یا `cancel` را ارسال کنید."
    BROADCAST_QUEUED = "پیام همگانی `{job_id}` با موفقیت در صف قرار گرفت."


class Common:
    ACTIVE = "فعال"
    INACTIVE = "غیرفعال"
