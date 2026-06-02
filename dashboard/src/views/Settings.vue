<script setup lang="ts">
import { ref, onMounted, computed } from "vue";
import {
  fetchAllSettings,
  patchGeneral,
  patchPricing,
  patchCustomBuy,
  patchSecurity,
  patchBackup,
  runBackupNow,
  patchPremiumEmoji,
  patchButtonStyle,
  type AllSettings,
  type ButtonStyle,
} from "@/api/settings";
import { ApiError } from "@/api/client";
import { fmtRelativeTime } from "@/utils/format";

type Tab = "general" | "pricing" | "custom_buy" | "security" | "backup" | "premium_emoji" | "button_style";

const tab = ref<Tab>("general");
const data = ref<AllSettings | null>(null);
const loading = ref(true);
const errorMsg = ref<string | null>(null);
const banner = ref<string | null>(null);
const bannerTone = ref<"ok" | "warn">("ok");
const busy = ref(false);

// Local editable copies (tab-local so each panel has its own Save button
// and unsaved edits in one tab don't leak into others).
const general = ref<AllSettings["general"]>({} as any);
const pricing = ref<AllSettings["pricing"]>({} as any);
const customBuy = ref<AllSettings["custom_buy"]>({} as any);
const security = ref<AllSettings["security"]>({} as any);
const backup = ref<AllSettings["backup"]>({} as any);
const premiumEmoji = ref<AllSettings["premium_emoji"]>({} as any);
const buttonStyle = ref<AllSettings["button_style"]>({} as any);

// Premium emoji map editor — array of rows for v-model ergonomics
const emojiRows = ref<Array<{ key: string; value: string }>>([]);

// The four style values the dashboard exposes. Empty = no styling (Telegram's default look).
const BUTTON_STYLE_OPTIONS: Array<{ value: ButtonStyle; label: string }> = [
  { value: "primary", label: "🔵 آبی" },
  { value: "success", label: "🟢 سبز" },
  { value: "danger",  label: "🔴 قرمز" },
  { value: "violet",  label: "🟣 بنفش" },
  { value: "amber",   label: "🟡 زرد" },
  { value: "orange",  label: "🟠 نارنجی" },
  { value: "",        label: "⚪️ بدون رنگ" },
];

async function refresh() {
  loading.value = true;
  errorMsg.value = null;
  try {
    const r = await fetchAllSettings();
    data.value = r;
    general.value = { ...r.general };
    pricing.value = { ...r.pricing };
    customBuy.value = { ...r.custom_buy };
    security.value = { ...r.security };
    backup.value = { ...r.backup };
    premiumEmoji.value = { ...r.premium_emoji };
    emojiRows.value = Object.entries(r.premium_emoji.emoji_map || {}).map(
      ([key, value]) => ({ key, value }),
    );
    buttonStyle.value = { ...r.button_style };
  } catch (exc) {
    errorMsg.value = exc instanceof ApiError ? exc.detail : "خطای شبکه";
  } finally {
    loading.value = false;
  }
}
onMounted(refresh);

function flash(msg: string, tone: "ok" | "warn" = "ok") {
  banner.value = msg;
  bannerTone.value = tone;
  setTimeout(() => (banner.value = null), 4000);
}

async function saveGeneral() {
  busy.value = true;
  try {
    await patchGeneral(general.value);
    flash("ذخیره شد.");
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally { busy.value = false; }
}

async function savePricing() {
  busy.value = true;
  try {
    await patchPricing(pricing.value);
    flash("ذخیره شد.");
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally { busy.value = false; }
}

async function saveCustomBuy() {
  busy.value = true;
  try {
    await patchCustomBuy(customBuy.value);
    flash("ذخیره شد.");
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally { busy.value = false; }
}

async function saveSecurity() {
  busy.value = true;
  try {
    await patchSecurity(security.value);
    flash("ذخیره شد.");
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally { busy.value = false; }
}

async function saveBackup() {
  busy.value = true;
  try {
    await patchBackup({
      interval_hours: backup.value.interval_hours,
      channel_chat_id: backup.value.channel_chat_id ?? undefined,
    });
    flash("ذخیره شد.");
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally { busy.value = false; }
}

async function clearBackupChannel() {
  busy.value = true;
  try {
    await patchBackup({ clear_channel: true });
    backup.value.channel_chat_id = null;
    flash("کانال بکاپ پاک شد.");
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally { busy.value = false; }
}

async function fireBackupNow() {
  if (!confirm("بکاپ همین الان ساخته و ارسال شود؟")) return;
  busy.value = true;
  try {
    await runBackupNow();
    flash("بکاپ ساخته شد و در حال ارسال است (لاگ‌های worker را چک کنید).");
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally { busy.value = false; }
}

function addEmojiRow() {
  emojiRows.value.push({ key: "", value: "" });
}
function removeEmojiRow(idx: number) {
  emojiRows.value.splice(idx, 1);
}
async function savePremiumEmoji() {
  busy.value = true;
  const map: Record<string, string> = {};
  for (const row of emojiRows.value) {
    const k = row.key.trim();
    const v = row.value.trim();
    if (k && v) map[k] = v;
  }
  try {
    await patchPremiumEmoji({
      enabled: premiumEmoji.value.enabled,
      emoji_map: map,
    });
    flash("ذخیره شد. کش بات هم refresh شد.");
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally { busy.value = false; }
}

async function saveButtonStyle() {
  busy.value = true;
  try {
    await patchButtonStyle({
      enabled: buttonStyle.value.enabled,
      confirm: buttonStyle.value.confirm,
      destructive: buttonStyle.value.destructive,
      navigation: buttonStyle.value.navigation,
      info: buttonStyle.value.info,
    });
    // The bot reads from a 30s in-process cache; we can't clear it
    // from here (different process), but updates land within ~30s.
    flash("ذخیره شد. حداکثر ۳۰ ثانیه طول می‌کشد تا روی دکمه‌های ربات اعمال شود.");
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally { busy.value = false; }
}

const TABS: Array<{ id: Tab; label: string }> = [
  { id: "general",        label: "عمومی" },
  { id: "pricing",        label: "قیمت‌گذاری" },
  { id: "custom_buy",     label: "خرید دلخواه" },
  { id: "security",       label: "امنیت" },
  { id: "backup",         label: "بکاپ" },
  { id: "premium_emoji",  label: "اموجی پریمیم" },
  { id: "button_style",   label: "رنگ دکمه‌ها" },
];
</script>

<template>
  <div class="p-6 lg:p-8 max-w-5xl mx-auto">
    <header class="mb-6">
      <h1 class="text-2xl font-bold text-white">تنظیمات</h1>
      <p class="text-sm text-slate-400 mt-1">
        همه‌ی toggleهای مدیر بات + بکاپ + اموجی پریمیم.
      </p>
    </header>

    <div
      v-if="banner"
      :class="['mb-4 px-4 py-2 rounded-lg text-sm',
               bannerTone === 'ok'
                  ? 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30'
                  : 'bg-amber-500/15 text-amber-300 border border-amber-500/30']"
    >{{ banner }}</div>

    <div v-if="errorMsg" class="card border-rose-500/40 bg-rose-500/10 text-rose-300 mb-4">
      {{ errorMsg }}
    </div>

    <!-- Tabs -->
    <div class="flex border-b border-bg-border mb-4 overflow-x-auto">
      <button
        v-for="t in TABS"
        :key="t.id"
        :class="['px-4 py-2.5 text-sm font-medium whitespace-nowrap transition-colors',
                 tab === t.id
                   ? 'text-accent border-b-2 border-accent'
                   : 'text-slate-400 hover:text-slate-200']"
        @click="tab = t.id"
      >
        {{ t.label }}
      </button>
    </div>

    <div v-if="loading" class="card animate-pulse h-40" />

    <!-- ── General ─────────────────────────────────────────────── -->
    <section v-else-if="tab === 'general'" class="card space-y-4">
      <h3 class="font-bold text-white">وضعیت کلی</h3>
      <label class="flex items-center justify-between gap-3 py-2 border-b border-bg-border/60">
        <div>
          <div class="text-sm text-white">فروش سرویس</div>
          <div class="text-[11px] text-slate-500">اگر خاموش، کاربران نمی‌توانند سرویس جدید بخرند.</div>
        </div>
        <input type="checkbox" v-model="general.sales_enabled" class="w-5 h-5 accent-accent" />
      </label>
      <label class="flex items-center justify-between gap-3 py-2 border-b border-bg-border/60">
        <div>
          <div class="text-sm text-white">تمدید سرویس</div>
          <div class="text-[11px] text-slate-500">امکان تمدید زمان/حجم برای کاربر.</div>
        </div>
        <input type="checkbox" v-model="general.renewals_enabled" class="w-5 h-5 accent-accent" />
      </label>
      <label class="flex items-center justify-between gap-3 py-2 border-b border-bg-border/60">
        <div>
          <div class="text-sm text-white">حذف کانفیگ توسط کاربر</div>
          <div class="text-[11px] text-slate-500">دکمه‌ی «حذف کانفیگ» در پنل کاربر.</div>
        </div>
        <input type="checkbox" v-model="general.delete_enabled" class="w-5 h-5 accent-accent" />
      </label>
      <label class="flex items-center justify-between gap-3 py-2">
        <div>
          <div class="text-sm text-white">بازپرداخت کانفیگ استفاده‌نشده</div>
          <div class="text-[11px] text-slate-500">برای کانفیگ‌های pending_activation که هنوز فعال نشدن.</div>
        </div>
        <input type="checkbox" v-model="general.refund_enabled" class="w-5 h-5 accent-accent" />
      </label>
      <div class="flex justify-end pt-2">
        <button class="btn btn-primary" :disabled="busy" @click="saveGeneral">ذخیره</button>
      </div>
    </section>

    <!-- ── Pricing ─────────────────────────────────────────────── -->
    <section v-else-if="tab === 'pricing'" class="card space-y-4">
      <h3 class="font-bold text-white">قیمت‌ها</h3>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <label class="label">قیمت هر ۱ گیگ تمدید (USD)</label>
          <input v-model.number="pricing.price_per_gb" class="input" type="number" step="0.01" min="0" />
        </div>
        <div>
          <label class="label">قیمت هر ۱۰ روز تمدید (USD)</label>
          <input v-model.number="pricing.price_per_10_days" class="input" type="number" step="0.01" min="0" />
        </div>
        <div>
          <label class="label">نرخ دلار به تومان</label>
          <input v-model.number="pricing.toman_rate" class="input" type="number" step="1" min="1" />
          <div class="text-[11px] text-slate-500 mt-1">USD × این عدد = تومان معادل</div>
        </div>
        <div>
          <label class="label">ارز نمایش به کاربر</label>
          <select v-model="pricing.display_currency" class="input">
            <option value="USD">دلار $</option>
            <option value="IRT">تومان 💵</option>
          </select>
        </div>
      </div>
      <div class="flex justify-end pt-2">
        <button class="btn btn-primary" :disabled="busy" @click="savePricing">ذخیره</button>
      </div>
    </section>

    <!-- ── Custom Buy ───────────────────────────────────────────── -->
    <section v-else-if="tab === 'custom_buy'" class="card space-y-4">
      <h3 class="font-bold text-white">خرید دلخواه (custom volume/duration)</h3>
      <label class="flex items-center justify-between gap-3 py-2 border-b border-bg-border/60">
        <div>
          <div class="text-sm text-white">فعال</div>
          <div class="text-[11px] text-slate-500">کاربر می‌تونه حجم و مدت دلخواه انتخاب کنه.</div>
        </div>
        <input type="checkbox" v-model="customBuy.enabled" class="w-5 h-5 accent-accent" />
      </label>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <label class="label">قیمت هر ۱ گیگ دلخواه (USD)</label>
          <input v-model.number="customBuy.price_per_gb" class="input" type="number" step="0.01" min="0" />
        </div>
        <div>
          <label class="label">قیمت هر ۱ روز دلخواه (USD)</label>
          <input v-model.number="customBuy.price_per_day" class="input" type="number" step="0.01" min="0" />
        </div>
      </div>
      <div class="flex justify-end pt-2">
        <button class="btn btn-primary" :disabled="busy" @click="saveCustomBuy">ذخیره</button>
      </div>
    </section>

    <!-- ── Security ─────────────────────────────────────────────── -->
    <section v-else-if="tab === 'security'" class="card space-y-4">
      <h3 class="font-bold text-white">امنیت سرویس</h3>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <label class="label">limitIp پنل X-UI</label>
          <input v-model.number="security.xui_limit_ip" class="input" type="number" min="0" />
          <div class="text-[11px] text-slate-500 mt-1">حداکثر IP همزمان مجاز در پنل (0 = نامحدود)</div>
        </div>
        <div>
          <label class="label">سقف IP متمایز</label>
          <input v-model.number="security.max_distinct_ips" class="input" type="number" min="0" />
        </div>
      </div>
      <label class="flex items-center justify-between gap-3 py-2">
        <div>
          <div class="text-sm text-white">ضد اشتراک‌گذاری خودکار</div>
          <div class="text-[11px] text-slate-500">اگر کاربر بیش از سقف IP داشت، خودکار غیرفعالش کنه.</div>
        </div>
        <input type="checkbox" v-model="security.auto_disable_ip_abuse" class="w-5 h-5 accent-accent" />
      </label>
      <div class="flex justify-end pt-2">
        <button class="btn btn-primary" :disabled="busy" @click="saveSecurity">ذخیره</button>
      </div>
    </section>

    <!-- ── Backup ───────────────────────────────────────────────── -->
    <section v-else-if="tab === 'backup'" class="card space-y-4">
      <h3 class="font-bold text-white">بکاپ خودکار</h3>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <label class="label">فاصله‌ی زمانی (ساعت)</label>
          <input v-model.number="backup.interval_hours" class="input" type="number" min="1" max="168" />
          <div class="text-[11px] text-slate-500 mt-1">
            هر چند ساعت یک‌بار بکاپ جدید ساخته بشه. (مثلاً 6 = هر ۶ ساعت)
          </div>
        </div>
        <div>
          <label class="label">کانال اختصاصی بکاپ (chat_id)</label>
          <input v-model.number="backup.channel_chat_id" class="input" type="number"
                 placeholder="مثلاً -1002131367720" />
          <div class="text-[11px] text-slate-500 mt-1">
            خالی = به کانال گزارش فروش (اگر تنظیم شده) یا DM ادمین می‌ره.
          </div>
        </div>
      </div>

      <div class="text-xs text-slate-500 grid grid-cols-1 md:grid-cols-2 gap-2 pt-2 border-t border-bg-border">
        <div>
          آخرین اجرا:
          <span class="text-slate-300">
            {{ backup.last_run_at ? fmtRelativeTime(backup.last_run_at) : "هرگز" }}
          </span>
        </div>
        <div v-if="backup.sales_channel_chat_id">
          کانال گزارش فروش (fallback):
          <code class="text-slate-300">{{ backup.sales_channel_chat_id }}</code>
        </div>
      </div>

      <div class="flex flex-wrap gap-2 justify-end pt-2">
        <button class="btn btn-ghost text-amber-300" :disabled="busy || !backup.channel_chat_id" @click="clearBackupChannel">
          پاک کردن کانال
        </button>
        <button class="btn btn-secondary" :disabled="busy" @click="fireBackupNow">
          🗄 ساخت بکاپ همین الان
        </button>
        <button class="btn btn-primary" :disabled="busy" @click="saveBackup">ذخیره</button>
      </div>
    </section>

    <!-- ── Premium emoji ────────────────────────────────────────── -->
    <section v-else-if="tab === 'premium_emoji'" class="card space-y-4">
      <h3 class="font-bold text-white">اموجی پریمیم</h3>

      <label class="flex items-center justify-between gap-3 py-2 border-b border-bg-border/60">
        <div>
          <div class="text-sm text-white">فعال</div>
          <div class="text-[11px] text-slate-500">اموجی‌های پایه به اموجی پریمیم (Telegram Stars) جایگزین می‌شن.</div>
        </div>
        <input type="checkbox" v-model="premiumEmoji.enabled" class="w-5 h-5 accent-accent" />
      </label>

      <div>
        <div class="flex items-center justify-between mb-2">
          <h4 class="text-sm font-bold text-white">نگاشت اموجی</h4>
          <button class="btn btn-ghost btn-sm" @click="addEmojiRow">+ افزودن ردیف</button>
        </div>
        <div class="text-[11px] text-slate-500 mb-2">
          ستون چپ = اموجی پایه که در متن می‌فرستی (مثلاً 🛒).<br />
          ستون راست = شناسه‌ی custom_emoji پریمیم (عدد ۱۹ رقمی).
        </div>

        <div class="space-y-2">
          <div v-for="(row, idx) in emojiRows" :key="idx" class="flex gap-2 items-center">
            <input v-model="row.key" class="input flex-shrink-0 w-24 text-center" placeholder="🛒" />
            <input v-model="row.value" class="input flex-1 font-mono text-xs" placeholder="5379748062124056162" />
            <button class="btn btn-ghost text-rose-300 btn-sm" @click="removeEmojiRow(idx)">×</button>
          </div>
          <div v-if="!emojiRows.length" class="text-xs text-slate-500 text-center py-4">
            هنوز هیچ نگاشتی ثبت نشده. روی «افزودن ردیف» بزن.
          </div>
        </div>
      </div>

      <div class="flex justify-end pt-2">
        <button class="btn btn-primary" :disabled="busy" @click="savePremiumEmoji">ذخیره</button>
      </div>
    </section>

    <!-- ── Button style (Bot API 9.4 colored inline buttons) ─────── -->
    <section v-else-if="tab === 'button_style'" class="card space-y-4">
      <h3 class="font-bold text-white">🎨 رنگ دکمه‌های ربات</h3>
      <div class="text-[11px] text-slate-400 leading-5">
        هر نقش یک رنگ می‌گیرد و ربات یک دایره‌ی رنگی (🟢🔵🔴🟣🟡🟠) جلوی دکمه‌ها می‌گذارد —
        این روی <b>همه‌ی</b> نسخه‌های تلگرام دیده می‌شود. سه رنگِ آبی/سبز/قرمز علاوه بر این،
        رنگِ نیتیوِ تلگرام (Bot API 9.4) را هم روی نسخه‌های جدید اعمال می‌کنند.
        پس از ذخیره، تا ۳۰ ثانیه طول می‌کشد ربات تغییرات را اعمال کند.
      </div>

      <label class="flex items-center justify-between gap-3 py-2 border-b border-bg-border/60">
        <div>
          <div class="text-sm text-white">رنگی‌سازی فعال</div>
          <div class="text-[11px] text-slate-500">با خاموش کردن، تمام دکمه‌ها به ظاهر پیش‌فرض تلگرام برمی‌گردند.</div>
        </div>
        <input type="checkbox" v-model="buttonStyle.enabled" class="w-5 h-5 accent-accent" />
      </label>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-4 pt-2">
        <div>
          <label class="label">✅ تأیید / مالی</label>
          <select v-model="buttonStyle.confirm" class="input">
            <option v-for="opt in BUTTON_STYLE_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
          </select>
          <div class="text-[10px] text-slate-500 mt-1">دکمه‌های خرید، آمار، گزارش، تأیید پرداخت</div>
        </div>
        <div>
          <label class="label">🗑 خطرناک / تنظیمات</label>
          <select v-model="buttonStyle.destructive" class="input">
            <option v-for="opt in BUTTON_STYLE_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
          </select>
          <div class="text-[10px] text-slate-500 mt-1">دکمه‌های حذف، بَن، تنظیمات حساس</div>
        </div>
        <div>
          <label class="label">🔙 بازگشت / پیمایش</label>
          <select v-model="buttonStyle.navigation" class="input">
            <option v-for="opt in BUTTON_STYLE_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
          </select>
          <div class="text-[10px] text-slate-500 mt-1">دکمه‌های Back و منوی اصلی</div>
        </div>
        <div>
          <label class="label">ℹ️ نمایش / مدیریت</label>
          <select v-model="buttonStyle.info" class="input">
            <option v-for="opt in BUTTON_STYLE_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
          </select>
          <div class="text-[10px] text-slate-500 mt-1">دکمه‌های لیست‌ها و فهرست‌های اطلاعاتی</div>
        </div>
      </div>

      <div class="flex justify-end pt-2">
        <button class="btn btn-primary" :disabled="busy" @click="saveButtonStyle">ذخیره</button>
      </div>
    </section>
  </div>
</template>
