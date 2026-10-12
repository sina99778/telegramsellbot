export const navigation = [
  {
    group: "پایش و گزارش",
    items: [
      {
        name: "overview",
        label: "مرکز عملیات",
        to: "/",
        icon: "home",
        hint: "خلاصه، سلامت سرورها و کارهای در انتظار",
      },
      {
        name: "analytics",
        label: "تحلیل درآمد",
        to: "/analytics",
        icon: "chart",
        hint: "گزارش‌های مالی و روند فروش",
      },
    ],
  },
  {
    group: "مدیریت سرویس",
    items: [
      {
        name: "users",
        label: "کاربران",
        to: "/users",
        icon: "users",
        hint: "جست‌وجو، کیف پول و اشتراک‌ها",
      },
      {
        name: "servers",
        label: "سرورها",
        to: "/servers",
        icon: "server",
        hint: "اتصال، اینباند و تنظیمات پنل",
      },
      {
        name: "plans",
        label: "پلن‌های فروش",
        to: "/plans",
        icon: "plans",
        hint: "تعرفه‌ها و موجودی پلن",
      },
      {
        name: "discounts",
        label: "تخفیف‌ها",
        to: "/discounts",
        icon: "tag",
        hint: "کدهای تخفیف و اعتبار آن‌ها",
      },
    ],
  },
  {
    group: "امور مالی",
    items: [
      {
        name: "transactions",
        label: "تراکنش‌ها",
        to: "/transactions",
        icon: "money",
        hint: "سفارش‌ها، پرداخت‌ها و کیف پول",
      },
      {
        name: "receipts",
        label: "بررسی رسیدها",
        to: "/receipts",
        icon: "receipt",
        hint: "تأیید دستی واریزهای بانکی",
      },
    ],
  },
  {
    group: "ابزارها",
    items: [
      {
        name: "broadcast",
        label: "پیام همگانی",
        to: "/broadcast",
        icon: "megaphone",
        hint: "ارسال پیام به کاربران",
      },
      {
        name: "content",
        label: "محتوا و برند",
        to: "/content",
        icon: "pencil",
        hint: "متن‌ها و هویت ربات",
      },
      {
        name: "settings",
        label: "تنظیمات",
        to: "/settings",
        icon: "settings",
        hint: "درگاه‌ها و تنظیمات کسب‌وکار",
      },
    ],
  },
];
export const navigationItems = navigation.flatMap((section) => section.items);
export function normalizeSearch(value: string) {
  return value
    .normalize("NFKC")
    .replace(/ي/g, "ی")
    .replace(/ك/g, "ک")
    .toLocaleLowerCase()
    .trim();
}
