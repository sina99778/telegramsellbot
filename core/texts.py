from __future__ import annotations


class Buttons:
    BUY_CONFIG = "🛒 | خرید سرویس جدید"
    PROFILE_WALLET = "👤 | حساب کاربری و شارژ"
    SUPPORT = "💬 | پشتیبانی آنلاین"
    MY_CONFIGS = "📋 | سرویس‌های من"
    TOPUP_CRYPTO = "💳 | شارژ با کریپتو"
    CUSTOM_AMOUNT = "🔢 | مبلغ دلخواه"
    OPEN_PAYMENT = "🔗 | ورود به درگاه پرداخت"
    RENEW_SERVICE = "🔄 | تمدید سرویس"
    RENEW_TIME = "⏳ | تمدید زمان"
    RENEW_VOLUME = "💾 | تمدید حجم"
    PREV = "◀️ | قبلی"
    NEXT = "بعدی | ▶️"
    BACK = "🔙 | بازگشت"


class Messages:
    MENU_PLACEHOLDER = "🔹 راهنمای سریع:"
    WELCOME_NEW = (
        "👋 سلام {name} عزیز، به ربات ما خوش آمدید!\n\n"
        "🚀 این ربات سریع‌ترین و پایدارترین سرویس‌های V2Ray را به شما ارائه می‌دهد.\n\n"
        "✨ امکانات ربات:\n"
        "🔸 خرید آنلاین و تحویل آنی\n"
        "🔸 مدیریت کامل سرویس‌ها و تمدید\n"
        "🔸 شارژ حساب کاربری با کریپتو\n"
        "🔸 پشتیبانی ۲۴ ساعته\n\n"
        "👇 برای شروع از منوی زیر استفاده کنید:"
    )
    WELCOME_BACK = (
        "👋 خیلی خوش برگشتی {name} عزیز!\n\n"
        "⚡ همه‌چیز آماده‌ست. از منوی زیر گزینه‌ی مورد نظرت رو انتخاب کن 👇"
    )
    WALLET_NOT_FOUND = "❌ کیف پول شما بارگذاری نشد. لطفاً دوباره /start را بزنید."
    PROFILE_OVERVIEW = (
        "👤 **حساب کاربری شما**\n"
        "━━━━━━━━━━━━━━\n"
        "نام: `{name}`\n"
        "💰 موجودی: `{balance}` دلار\n"
        "💳 سقف اعتبار: `{credit_limit}` دلار\n"
        "━━━━━━━━━━━━━━\n"
        "👈 برای شارژ حساب از دکمه‌های زیر استفاده کنید:"
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
    PLAN_CONFIG_UNAVAILABLE = "زیرساخت این پلن در حال حاضر در دسترس نیست. لطفاً پلن دیگری انتخاب کنید یا با پشتیبانی تماس بگیرید."
    INSUFFICIENT_BALANCE = (
        "موجودی کیف پول شما {balance} دلار است، اما قیمت این پلن {price} {currency} است.\n"
        "لطفاً ابتدا کیف پول خود را شارژ کنید."
    )
    BALANCE_NOT_SUFFICIENT_ANYMORE = "موجودی شما دیگر کافی نیست. لطفاً کیف پول را شارژ کرده و دوباره تلاش کنید."
    PROVISIONING_FAILED_REFUNDED = (
        "در حال حاضر پنل نتوانست کانفیگ شما را ایجاد کند. "
        "مبلغ به صورت خودکار به کیف پول شما برگشت داده شد."
    )
    PROVISIONING_FAILED_MANUAL_REFUND = (
        "در حال حاضر ساخت کانفیگ ناموفق بود و بازگشت خودکار مبلغ هم کامل نشد. "
        "موضوع برای پیگیری دستی ثبت شده است؛ لطفاً با پشتیبانی تماس بگیرید."
    )
    RENEWAL_OPTIONS = "نحوه تمدید سرویس را انتخاب کنید:"
    RENEWAL_ENTER_VOLUME = "تعداد گیگابایت حجم برای اضافه شدن را وارد کنید (مثلا 10):"
    RENEWAL_ENTER_TIME = "تعداد روزهایی که می‌خواهید تمدید کنید را وارد کنید (مثلا 30):"
    RENEWAL_INVALID_VALUE = "مقدار وارد شده معتبر نیست. لطفا یک عدد بزرگتر از صفر وارد کنید."
    RENEWAL_INVOICE = (
        "🔖 فاکتور تمدید:\n\n"
        "حجم اضافه: {volume} گیگابایت\n"
        "زمان اضافه: {time} روز\n"
        "مبلغ قابل پرداخت: {price} دلار\n\n"
        "آیا تایید می‌کنید؟"
    )
    RENEWAL_SUCCESS = "سرویس شما با موفقیت تمدید شد. زمان و حجم اکانت شما در سیستم ثبت گردید."

    CONFIG_CREATED = (
        "✅ کانفیگ شما با موفقیت و آنی ساخته شد!\n\n"
        "━━━━━━━━━━━━━━\n"
        "🚀 پلن: `{plan_name}`\n"
        "💾 حجم: `{volume_label}`\n"
        "👤 کاربر: `{client_email}`\n"
        "━━━━━━━━━━━━━━\n"
        "🔗 **لینک اشتراک (ساب‌لینک):**\n"
        "`{sub_link}`\n\n"
        "⏱ *زمان اکانت شما از اولین اتصال محاسبه خواهد شد.*\n"
        "❤️ از خرید شما متشکریم!"
    )
    CANCELLED = "🚫 عملیات با موفقیت لغو شد."

class SupportTexts:
    START = "💬 لطفاً سوال یا مشکل خود را به صورت کامل بنویسید.\nتیم پشتیبانی ما در اسرع وقت، همینجا پاسخگوی شما خواهد بود.\n\n🔙 برای بازگشت /cancel را ارسال کنید."
    ACCOUNT_NOT_FOUND = "❌ حساب شما پیدا نشد. لطفاً دوباره /start را بزنید."
    TICKET_CREATED = "✅ پیام شما با موفقیت ثبت و برای تیم پشتیبانی ارسال شد.\n\nکد پیگیری: #{ticket_id}"
    ADMIN_ALERT = (
        "🚨 تیکت جدید پشتیبانی\n"
        "━━━━━━━━━━━━━━\n"
        "تیکت: #{ticket_id}\n"
        "کاربر: {name}\n"
        "آیدی تلگرام: `{telegram_id}`\n\n"
        "📝 پیام:\n"
        "{message}"
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
    EXIT_SUPPORT = "🔙 خروج از پشتیبانی"
    HISTORY_TITLE = "📜 سوابق تیکت شما #{ticket_id}:\n\n"
    PHOTO_MARKER = "[📷 تصویر]"



class MarketingTexts:
    RETARGETING_REMINDER = (
        "دلتنگت شدیم.\n\n"
        "هر زمان بخواهی برگردی، پنل تو آماده است. "
        "اگر کانفیگ جدید می‌خواهی یا برای انتخاب پلن نیاز به کمک داری، فقط ربات را باز کن."
    )


class AdminButtons:
    MANAGE_SERVERS = "🖥 مدیریت سرورها"
    MANAGE_PLANS = "📦 مدیریت پلن‌ها"
    STATISTICS = "📊 آمار و گزارش‌ها"
    MANAGE_USERS = "👥 مدیریت کاربران"
    BROADCAST = "📢 پیام همگانی"
    MANAGE_RETARGETING = "🎯 مدیریت ریتارگتینگ"
    MANAGE_TICKETS = "🛠 بررسی تیکت‌ها"
    BACKUP = "🗄 دریافت بکاپ"
    BOT_SETTINGS = "⚙️ تنظیمات ربات"
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
    EDIT_RETARGETING_TEXT = "✏️ ویرایش متن"
    EDIT_RETARGETING_DAYS = "⏳ تغییر بازه روز"
    TOGGLE_RETARGETING = "🔁 فعال/غیرفعال"
    BACK = "🔙 بازگشت"
    TEST_RETARGETING = "🧪 ارسال تست"
    RESET_REVENUE = "🗑 صفر کردن درآمد"
    MANAGE_DISCOUNTS = "🏷 مدیریت تخفیف‌ها"


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
    PLAN_CODE_EXISTS = "پلنی با همین مشخصات قبلاً برای این اینباند ثبت شده است. نام یا اینباند دیگری انتخاب کنید."
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
    TICKETS_OVERVIEW = "تیکت‌های باز و پاسخ‌داده‌شده:"
    NO_OPEN_TICKETS = "فعلاً تیکت بازی برای بررسی وجود ندارد."
    TICKET_DETAILS = (
        "تیکت: {ticket_id}\n"
        "کاربر: {user_name}\n"
        "تلگرام آیدی: {telegram_id}\n"
        "وضعیت: {status}\n\n"
        "آخرین پیام‌ها:\n{messages}"
    )
    RETARGETING_MENU = (
        "مدیریت ریتارگتینگ\n\n"
        "وضعیت: {status}\n"
        "بازه عدم خرید: {days} روز\n\n"
        "متن فعلی:\n{message}"
    )
    RETARGETING_ENTER_MESSAGE = "متن جدید ریتارگتینگ را بفرستید. برای لغو /cancel را ارسال کنید."
    RETARGETING_ENTER_DAYS = "تعداد روزهای عدم خرید را وارد کنید. برای لغو /cancel را ارسال کنید."
    RETARGETING_UPDATED = "تنظیمات ریتارگتینگ به‌روزرسانی شد."
    RETARGETING_TEST_SENT = "نسخه تست پیام ریتارگتینگ برای شما ارسال شد."
    PLAN_CREATION_CANCELLED = "ساخت پلن لغو شد."
    PLAN_CREATION_INTERRUPTED = "ساخت پلن لغو شد. حالا می‌توانید گزینه موردنظر خود را دوباره انتخاب کنید."
    SETTINGS_MENU = (
        "⚙️ تنظیمات عمومی ربات\n\n"
        "قیمت تمدید هر ۱ گیگابایت: {price_per_gb} دلار\n"
        "قیمت تمدید هر ۱۰ روز: {price_per_10_days} دلار\n"
        "💱 نرخ دلار به تومان: {toman_rate} تومان\n"
    )
    ENTER_PRICE_PER_GB = "قیمت تمدید برای ۱ گیگابایت (به دلار) را وارد کنید:"
    ENTER_PRICE_PER_10_DAYS = "قیمت تمدید برای ۱۰ روز (به دلار) را وارد کنید:"
    SETTINGS_UPDATED = "تنظیمات با موفقیت بروزرسانی شد."
    CONFIRM_RESET_REVENUE = "آیا از صفر کردن آمار درآمد اطمینان دارید؟ این عمل قابل بازگشت نیست."
    REVENUE_RESET_SUCCESS = "آمار درآمد با موفقیت صفر شد."


class Common:
    ACTIVE = "فعال"
    INACTIVE = "غیرفعال"
