<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useRouter } from "vue-router";
import {
  getUser,
  adjustBalance,
  setCreditLimit,
  setUserStatus,
  sendMessage,
  transferConfigs,
  type UserDetailResponse,
} from "@/api/users";
import { ApiError } from "@/api/client";
import { fmtBytes, fmtMoney, fmtRelativeTime } from "@/utils/format";

const props = defineProps<{ id: string }>();
const router = useRouter();

const data = ref<UserDetailResponse | null>(null);
const loading = ref(true);
const errorMsg = ref<string | null>(null);
const banner = ref<string | null>(null);
const bannerTone = ref<"ok" | "warn">("ok");

// action state
const adjustAmount = ref<string>("");
const adjustDesc = ref<string>("");
const creditLimit = ref<string>("");
const msgText = ref<string>("");
const transferTarget = ref<string>("");
const busy = ref<string>(""); // which action is in-flight

async function refresh() {
  loading.value = true;
  errorMsg.value = null;
  try {
    data.value = await getUser(props.id);
    creditLimit.value = String(data.value.user.credit_limit_usd);
  } catch (exc) {
    errorMsg.value = exc instanceof ApiError ? exc.detail : "خطای شبکه";
  } finally {
    loading.value = false;
  }
}
onMounted(refresh);

function flash(message: string, tone: "ok" | "warn" = "ok") {
  banner.value = message;
  bannerTone.value = tone;
  setTimeout(() => (banner.value = null), 4000);
}

async function doAdjust() {
  const amt = Number(adjustAmount.value);
  if (!amt || isNaN(amt)) {
    flash("مقدار نامعتبر است.", "warn");
    return;
  }
  busy.value = "adjust";
  try {
    const r = await adjustBalance(props.id, amt, adjustDesc.value || "Adjusted from dashboard");
    if (data.value) data.value.user.balance_usd = r.balance_usd;
    flash(`موجودی به‌روزرسانی شد. موجودی جدید: ${fmtMoney(r.balance_usd)}`);
    adjustAmount.value = "";
    adjustDesc.value = "";
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    busy.value = "";
  }
}

async function doCreditLimit() {
  const v = Number(creditLimit.value);
  if (isNaN(v) || v < 0) {
    flash("سقف اعتبار نامعتبر.", "warn");
    return;
  }
  busy.value = "credit";
  try {
    const r = await setCreditLimit(props.id, v);
    if (data.value) data.value.user.credit_limit_usd = r.credit_limit_usd;
    flash(`سقف اعتبار: ${fmtMoney(r.credit_limit_usd)}`);
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    busy.value = "";
  }
}

async function doToggleBan() {
  if (!data.value) return;
  const next = data.value.user.status === "banned" ? "active" : "banned";
  busy.value = "ban";
  try {
    const r = await setUserStatus(props.id, next);
    data.value.user.status = r.status;
    flash(next === "banned" ? "کاربر مسدود شد." : "کاربر فعال شد.");
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    busy.value = "";
  }
}

async function doSendMessage() {
  if (!msgText.value.trim()) {
    flash("متن پیام خالی است.", "warn");
    return;
  }
  busy.value = "msg";
  try {
    await sendMessage(props.id, msgText.value);
    msgText.value = "";
    flash("پیام ارسال شد.");
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    busy.value = "";
  }
}

async function doTransfer(subId: string | null, all: boolean) {
  const target = transferTarget.value.trim();
  if (!target) {
    flash("اکانت مقصد را وارد کنید (آی‌دی عددی یا یوزرنیم).", "warn");
    return;
  }
  const what = all ? "همه‌ی کانفیگ‌های این کاربر" : "این کانفیگ";
  if (!window.confirm(`${what} به «${target}» منتقل شود؟\nلینکِ کانفیگ تغییر نمی‌کند؛ فقط مالکیت منتقل می‌شود.`)) {
    return;
  }
  busy.value = all ? "transfer-all" : `transfer-${subId}`;
  try {
    const payload = all
      ? { target, all: true }
      : { target, subscription_id: subId as string };
    const r = await transferConfigs(props.id, payload);
    flash(r.message || "انتقال انجام شد.");
    transferTarget.value = "";
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    busy.value = "";
  }
}

function subStatusBadge(s: string): string {
  if (s === "active" || s === "pending_activation") return "badge badge-success";
  if (s === "expired") return "badge badge-warn";
  return "badge badge-muted";
}
</script>

<template>
  <div class="p-6 lg:p-8 max-w-7xl mx-auto">
    <!-- Top bar -->
    <header class="flex items-center gap-3 mb-6">
      <button class="btn btn-ghost" @click="router.back()">
        <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M19 12H5m7-7-7 7 7 7" />
        </svg>
        بازگشت
      </button>
      <h1 class="text-2xl font-bold text-white">پروفایل کاربر</h1>
    </header>

    <!-- Banner -->
    <div
      v-if="banner"
      :class="['mb-4 px-4 py-2 rounded-lg text-sm',
               bannerTone === 'ok'
                  ? 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30'
                  : 'bg-amber-500/15 text-amber-300 border border-amber-500/30']"
    >{{ banner }}</div>

    <!-- Error -->
    <div v-if="errorMsg" class="card border-rose-500/40 bg-rose-500/10 text-rose-300 mb-4">
      {{ errorMsg }}
    </div>

    <!-- Loading -->
    <div v-if="loading" class="card animate-pulse h-40" />

    <div v-else-if="data" class="grid grid-cols-1 lg:grid-cols-3 gap-4">
      <!-- ── Profile + actions (left 2 cols) ───────────────────────── -->
      <section class="card lg:col-span-2 space-y-4">
        <!-- Profile header -->
        <div class="flex items-center gap-4">
          <div class="w-14 h-14 rounded-full bg-accent text-bg-base font-bold flex items-center justify-center text-xl">
            {{ (data.user.first_name || "U")[0].toUpperCase() }}
          </div>
          <div class="flex-1 min-w-0">
            <div class="text-xl font-bold text-white">
              {{ data.user.first_name || "—" }}
              <span v-if="data.user.last_name" class="text-slate-400 ms-1">{{ data.user.last_name }}</span>
            </div>
            <div class="text-sm text-slate-400">
              {{ data.user.username ? "@" + data.user.username : "(بدون یوزرنیم)" }}
              <span class="ms-2">— TG <code>{{ data.user.telegram_id }}</code></span>
            </div>
          </div>
          <div class="flex flex-col items-end">
            <span :class="data.user.status === 'banned' ? 'badge badge-danger' : 'badge badge-success'">
              {{ data.user.status === 'banned' ? 'مسدود' : 'فعال' }}
            </span>
            <span v-if="data.user.role !== 'user'" class="badge badge-warn mt-1">
              {{ data.user.role }}
            </span>
          </div>
        </div>

        <!-- Quick facts -->
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <div>
            <div class="text-[11px] text-slate-500">موجودی</div>
            <div class="text-emerald-300 font-mono">{{ fmtMoney(data.user.balance_usd) }}</div>
          </div>
          <div>
            <div class="text-[11px] text-slate-500">سقف اعتبار</div>
            <div class="text-slate-300 font-mono">{{ fmtMoney(data.user.credit_limit_usd) }}</div>
          </div>
          <div>
            <div class="text-[11px] text-slate-500">عضویت</div>
            <div class="text-slate-300">{{ fmtRelativeTime(data.user.created_at) }}</div>
          </div>
          <div>
            <div class="text-[11px] text-slate-500">آخرین فعالیت</div>
            <div class="text-slate-300">{{ fmtRelativeTime(data.user.last_seen_at) }}</div>
          </div>
        </div>

        <!-- Actions -->
        <div class="border-t border-bg-border pt-4 space-y-4">
          <!-- Balance adjust -->
          <div class="grid grid-cols-1 md:grid-cols-3 gap-3 items-end">
            <div>
              <label class="label">تنظیم موجودی (USD)</label>
              <input v-model="adjustAmount" class="input" type="number" step="0.01" placeholder="+10.50 یا -5" />
            </div>
            <div class="md:col-span-1">
              <label class="label">توضیح</label>
              <input v-model="adjustDesc" class="input" type="text" placeholder="اختیاری" />
            </div>
            <button class="btn btn-primary" :disabled="busy === 'adjust'" @click="doAdjust">
              {{ busy === 'adjust' ? '...' : 'اعمال' }}
            </button>
          </div>

          <!-- Credit limit -->
          <div class="grid grid-cols-1 md:grid-cols-3 gap-3 items-end">
            <div>
              <label class="label">سقف اعتبار (USD)</label>
              <input v-model="creditLimit" class="input" type="number" step="0.01" min="0" />
            </div>
            <div class="md:col-span-1 text-xs text-slate-500 self-center">
              مقدار منفی برای صفر تنظیم می‌شود
            </div>
            <button class="btn btn-secondary" :disabled="busy === 'credit'" @click="doCreditLimit">
              {{ busy === 'credit' ? '...' : 'ذخیره' }}
            </button>
          </div>

          <!-- Ban / unban -->
          <div class="flex flex-wrap gap-2 items-center">
            <button
              :class="data.user.status === 'banned' ? 'btn btn-secondary' : 'btn btn-danger'"
              :disabled="busy === 'ban'"
              @click="doToggleBan"
            >
              {{ data.user.status === 'banned' ? 'رفع مسدودیت' : 'مسدود کردن' }}
            </button>
            <span v-if="data.user.is_bot_blocked" class="badge badge-warn">کاربر ربات را block کرده</span>
          </div>

          <!-- Send message -->
          <div>
            <label class="label">ارسال پیام به کاربر</label>
            <textarea v-model="msgText" class="input" rows="3" placeholder="متن پیام …"></textarea>
            <div class="flex justify-end mt-2">
              <button class="btn btn-primary" :disabled="busy === 'msg' || !msgText.trim()" @click="doSendMessage">
                {{ busy === 'msg' ? '...' : 'ارسال' }}
              </button>
            </div>
          </div>

          <!-- Transfer configs -->
          <div class="border-t border-bg-border pt-4">
            <label class="label">🔄 انتقال کانفیگ‌ها به اکانت دیگر</label>
            <div class="grid grid-cols-1 md:grid-cols-3 gap-3 items-end">
              <div class="md:col-span-2">
                <input v-model="transferTarget" class="input" type="text" dir="ltr"
                       placeholder="آی‌دی عددی یا یوزرنیم اکانت مقصد" />
              </div>
              <button class="btn btn-secondary" :disabled="busy === 'transfer-all'" @click="doTransfer(null, true)">
                {{ busy === 'transfer-all' ? '...' : 'انتقال همه (' + data.subscriptions.length + ')' }}
              </button>
            </div>
            <p class="text-[11px] text-slate-500 mt-1">
              لینکِ کانفیگ تغییر نمی‌کند؛ فقط مالکیت منتقل می‌شود. برای انتقال تکی، دکمه‌ی «انتقال» کنار هر سرویس را بزنید.
            </p>
          </div>
        </div>
      </section>

      <!-- ── Subscriptions panel (right col, top) ────────────────── -->
      <section class="card">
        <h3 class="text-sm font-bold text-white mb-3">سرویس‌ها ({{ data.subscriptions.length }})</h3>
        <div v-if="!data.subscriptions.length" class="text-xs text-slate-500 py-6 text-center">
          سرویسی ثبت نشده.
        </div>
        <ul v-else class="space-y-2">
          <li
            v-for="s in data.subscriptions"
            :key="s.id"
            class="border border-bg-border/60 rounded-md p-3 text-sm"
          >
            <div class="flex items-center justify-between">
              <div class="font-medium text-white truncate">
                <span v-if="s.source === 'imported_legacy'">🗂 </span>
                {{ s.name || "—" }}
              </div>
              <span :class="subStatusBadge(s.status)">{{ s.status }}</span>
            </div>
            <div class="text-[11px] text-slate-400 font-mono mt-1">
              {{ fmtBytes(s.used_bytes) }} / {{ fmtBytes(s.volume_bytes) }}
              <span v-if="s.ends_at" class="ms-2">• تا {{ s.ends_at.slice(0, 10) }}</span>
            </div>
            <div class="mt-2 flex justify-end">
              <button class="btn btn-ghost text-xs px-2 py-1"
                      :disabled="busy === 'transfer-' + s.id"
                      @click="doTransfer(s.id, false)">
                {{ busy === 'transfer-' + s.id ? '...' : 'انتقال' }}
              </button>
            </div>
          </li>
        </ul>
      </section>

      <!-- ── Recent transactions (bottom, full width) ────────────── -->
      <section class="card lg:col-span-3">
        <h3 class="text-sm font-bold text-white mb-3">آخرین تراکنش‌های کیف پول ({{ data.wallet_transactions.length }})</h3>
        <div v-if="!data.wallet_transactions.length" class="text-xs text-slate-500 py-6 text-center">
          تراکنشی ثبت نشده.
        </div>
        <table v-else class="data-table">
          <thead>
            <tr>
              <th>نوع</th>
              <th>جهت</th>
              <th>مقدار</th>
              <th>توضیح</th>
              <th>زمان</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="t in data.wallet_transactions" :key="t.id">
              <td>{{ t.type }}</td>
              <td>
                <span :class="t.direction === 'credit' ? 'text-emerald-300' : 'text-rose-300'">
                  {{ t.direction === 'credit' ? '➕' : '➖' }}
                </span>
              </td>
              <td class="font-mono">{{ fmtMoney(t.amount) }} {{ t.currency }}</td>
              <td class="text-slate-400">{{ t.description || "—" }}</td>
              <td class="text-slate-500 text-xs">{{ fmtRelativeTime(t.created_at) }}</td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>
  </div>
</template>
