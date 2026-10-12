<script setup lang="ts">
import { ref, onMounted, computed, watch, onBeforeUnmount } from "vue";
import {
  listReceipts,
  getReceipt,
  approveReceipt,
  rejectReceipt,
  receiptPhotoUrl,
  type ReceiptListItem,
  type ReceiptDetail,
  type ReceiptFilter,
} from "@/api/receipts";
import { useOperations } from "@/stores/operations";
import { ApiError } from "@/api/client";
import { fmtNumber } from "@/utils/format";
import { latestOnly } from "@/utils/latest";

const operations = useOperations();
const search = ref("");
const items = ref<ReceiptListItem[]>([]);
const page = ref(1);
const pageSize = ref(50);
const total = ref(0);
const totalPages = ref(1);
const loading = ref(true);
const errorMsg = ref<string | null>(null);
const banner = ref<string | null>(null);
const bannerTone = ref<"ok" | "warn">("ok");
const busyId = ref<string>("");

const filter = ref<ReceiptFilter>("pending");
const filters: { key: ReceiptFilter; label: string }[] = [
  { key: "pending", label: "در انتظار" },
  { key: "approved", label: "تأییدشده" },
  { key: "rejected", label: "ردشده" },
  { key: "all", label: "همه" },
];

const drawer = ref<ReceiptDetail | null>(null);
const drawerLoading = ref(false);

// The list and the drawer get separate runners: opening a receipt must not
// cancel the list refresh behind it, and closing the drawer must not cancel the
// list either.
const runList = latestOnly();
const runDrawer = latestOnly();

async function refresh() {
  loading.value = true;
  errorMsg.value = null;
  await runList(
    (signal) =>
      listReceipts(filter.value, signal, {
        page: page.value,
        page_size: pageSize.value,
        q: search.value.trim() || undefined,
      }),
    {
      commit: (r) => {
        items.value = r.items;
        total.value = r.total;
        totalPages.value = r.total_pages;
        if (page.value > r.total_pages) page.value = r.total_pages;
      },
      fail: (exc) => {
        errorMsg.value = exc instanceof ApiError ? exc.detail : "خطا";
      },
      settle: () => {
        loading.value = false;
      },
    },
  );
}
onMounted(refresh);
let searchTimer: ReturnType<typeof setTimeout> | undefined;
watch(search, () => {
  clearTimeout(searchTimer);
  runList.cancel();
  loading.value = true;
  searchTimer = setTimeout(() => {
    if (page.value !== 1) page.value = 1;
    else refresh();
  }, 300);
});
watch(page, refresh);
watch(pageSize, () => {
  if (page.value !== 1) page.value = 1;
  else refresh();
});
onBeforeUnmount(() => {
  clearTimeout(searchTimer);
  runList.cancel();
  runDrawer.cancel();
});

function setFilter(f: ReceiptFilter) {
  if (filter.value === f) return;
  filter.value = f;
  if (page.value !== 1) page.value = 1;
  else refresh();
}

function statusLabel(s: string): string {
  if (s === "pending_approval") return "در انتظار";
  if (s === "finished") return "تأییدشده";
  if (s === "rejected") return "ردشده";
  return s;
}
function statusBadge(s: string): string {
  if (s === "finished") return "badge badge-success";
  if (s === "rejected") return "badge badge-danger";
  if (s === "pending_approval") return "badge badge-warn";
  return "badge badge-muted";
}

function flash(msg: string, tone: "ok" | "warn" = "ok") {
  banner.value = msg;
  bannerTone.value = tone;
  setTimeout(() => (banner.value = null), 4000);
}

const totalPendingAmount = computed(() =>
  items.value.reduce((s, r) => s + r.price_amount_usd, 0),
);

function fmtDate(s: string | null): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleString("fa-IR", {
      dateStyle: "short",
      timeStyle: "short",
    });
  } catch {
    return s;
  }
}

async function openDrawer(r: ReceiptListItem) {
  drawerLoading.value = true;
  drawer.value = null;
  await runDrawer((signal) => getReceipt(r.id, signal), {
    commit: (d) => {
      drawer.value = d;
    },
    fail: (exc) => {
      flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
    },
    settle: () => {
      drawerLoading.value = false;
    },
  });
}

// Closing the drawer must also drop the detail request still in flight,
// otherwise its response would reopen the panel the operator just dismissed —
// and the approve/reject buttons act on exactly that content.
function closeDrawer() {
  runDrawer.cancel();
  drawer.value = null;
  drawerLoading.value = false;
}

async function doApprove(r: ReceiptListItem) {
  if (
    !confirm(
      `تأیید رسید پرداخت ${r.price_amount_usd.toFixed(2)} $؟ کیف پول کاربر شارژ می‌شود.`,
    )
  )
    return;
  busyId.value = r.id;
  try {
    await approveReceipt(r.id);
    operations.refresh();
    flash("✅ رسید تأیید و کیف پول شارژ شد.");
    drawer.value = null;
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    busyId.value = "";
  }
}

async function doReject(r: ReceiptListItem) {
  if (!confirm("رد رسید پرداخت؟")) return;
  busyId.value = r.id;
  try {
    await rejectReceipt(r.id);
    operations.refresh();
    flash("رسید رد شد.");
    closeDrawer();
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    busyId.value = "";
  }
}
</script>

<template>
  <div class="p-6 lg:p-8 max-w-7xl mx-auto">
    <header class="flex flex-wrap items-end justify-between gap-3 mb-6">
      <div>
        <h1 class="text-2xl font-bold text-white">رسیدهای کارت به کارت</h1>
        <p class="text-sm text-slate-400 mt-1">
          {{ fmtNumber(total) }} رسید با این فیلتر — جمع این صفحه:
          {{ totalPendingAmount.toFixed(2) }} $
        </p>
      </div>
      <div class="flex items-center gap-2">
        <div class="flex gap-1 bg-bg-elev/40 rounded-lg p-1">
          <button
            v-for="f in filters"
            :key="f.key"
            :class="[
              'btn btn-sm',
              filter === f.key ? 'btn-primary' : 'btn-ghost',
            ]"
            @click="setFilter(f.key)"
          >
            {{ f.label }}
          </button>
        </div>
        <button class="btn btn-secondary" @click="refresh" :disabled="loading">
          به‌روزرسانی
        </button>
      </div>
    </header>
    <div class="console-alert warning mb-4">
      <span
        >رسید تصویری، اثبات واریز نیست؛ پیش از تأیید، مبلغ و مقصد را با حساب
        بانکی بررسی کنید.</span
      >
    </div>
    <div class="card mb-4">
      <label class="label" for="receipt-search"
        >جست‌وجو در همهٔ رسیدهای این فیلتر</label
      ><input
        id="receipt-search"
        v-model="search"
        class="input"
        placeholder="نام کاربر، یوزرنیم، شناسهٔ تلگرام یا شناسهٔ رسید…"
      />
    </div>
    <div
      v-if="banner"
      :class="[
        'mb-4 px-4 py-2 rounded-lg text-sm',
        bannerTone === 'ok'
          ? 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30'
          : 'bg-amber-500/15 text-amber-300 border border-amber-500/30',
      ]"
    >
      {{ banner }}
    </div>

    <div
      v-if="errorMsg"
      class="card border-rose-500/40 bg-rose-500/10 text-rose-300 mb-4"
    >
      {{ errorMsg }}
    </div>

    <div v-if="loading" class="card animate-pulse h-32" />

    <div v-else class="card overflow-hidden p-0">
      <div class="table-scroll" tabindex="0" aria-label="جدول اطلاعات">
        <table class="data-table">
          <thead>
            <tr>
              <th>کاربر</th>
              <th>مبلغ</th>
              <th>کارت</th>
              <th>وضعیت</th>
              <th>تاریخ</th>
              <th class="text-end">عملیات</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in items" :key="r.id">
              <td>
                <button
                  class="text-white hover:text-accent text-sm"
                  @click="openDrawer(r)"
                >
                  {{ r.user?.first_name || r.user?.username || "بدون نام" }}
                </button>
                <div class="text-[11px] text-slate-500 font-mono">
                  {{ r.user?.telegram_id || "—" }}
                </div>
              </td>
              <td>
                <div class="font-mono text-white">
                  {{ r.price_amount_usd.toFixed(2) }} $
                </div>
                <div class="text-[11px] text-slate-500 font-mono">
                  {{ fmtNumber(r.pay_amount) }} {{ r.pay_currency }}
                </div>
              </td>
              <td class="text-xs">
                <div class="text-slate-300">{{ r.card_holder || "—" }}</div>
                <div class="font-mono text-slate-500">
                  {{ r.card_number || "" }}
                </div>
              </td>
              <td>
                <span :class="statusBadge(r.status)">{{
                  statusLabel(r.status)
                }}</span>
              </td>
              <td class="text-xs text-slate-300">
                {{ fmtDate(r.created_at) }}
              </td>
              <td class="text-end whitespace-nowrap">
                <button class="btn btn-ghost btn-sm" @click="openDrawer(r)">
                  مشاهده
                </button>
                <template v-if="r.status === 'pending_approval'">
                  <button
                    class="btn btn-ghost btn-sm text-emerald-300"
                    :disabled="busyId === r.id"
                    @click="doApprove(r)"
                  >
                    تأیید
                  </button>
                  <button
                    class="btn btn-ghost btn-sm text-rose-300"
                    :disabled="busyId === r.id"
                    @click="doReject(r)"
                  >
                    رد
                  </button>
                </template>
              </td>
            </tr>
            <tr v-if="!items.length">
              <td colspan="6" class="text-center text-slate-500 py-8">
                {{
                  errorMsg
                    ? "اطلاعات رسیدها دریافت نشد."
                    : search
                      ? "رسیدی مطابق این جست‌وجو پیدا نشد."
                      : filter === "pending"
                        ? "هیچ رسیدی در صف نیست."
                        : "رسیدی یافت نشد."
                }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="flex flex-wrap items-center justify-between gap-3 mt-4">
      <label class="text-xs text-slate-500 flex items-center gap-2"
        >ردیف در صفحه<select v-model.number="pageSize" class="input !w-auto">
          <option :value="25">25</option>
          <option :value="50">50</option>
          <option :value="100">100</option>
        </select></label
      ><span class="text-xs text-slate-500"
        >صفحه {{ page }} از {{ totalPages }}</span
      >
      <div class="flex gap-2">
        <button
          class="btn btn-secondary"
          :disabled="page <= 1 || loading"
          @click="page--"
        >
          قبلی</button
        ><button
          class="btn btn-secondary"
          :disabled="page >= totalPages || loading"
          @click="page++"
        >
          بعدی
        </button>
      </div>
    </div>

    <!-- ── Detail drawer ─────────────────────────────────────────── -->
    <div
      v-if="drawer || drawerLoading"
      class="fixed inset-0 bg-black/60 backdrop-blur-sm z-40 flex items-center justify-center p-4"
      @click.self="closeDrawer()"
    >
      <div class="card w-full max-w-2xl max-h-[90vh] overflow-y-auto">
        <div class="flex justify-between items-center mb-3">
          <h3 class="text-lg font-bold text-white">جزئیات رسید</h3>
          <button class="btn btn-ghost" @click="closeDrawer()">×</button>
        </div>
        <div v-if="drawerLoading" class="animate-pulse h-40" />
        <div v-else-if="drawer" class="space-y-4">
          <div class="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
            <div>
              <div class="text-[11px] text-slate-500">کاربر</div>
              <div class="text-slate-200">
                {{ drawer.user?.first_name || "—" }}
              </div>
              <div class="text-[11px] text-slate-500 font-mono">
                {{ drawer.user?.telegram_id }}
              </div>
            </div>
            <div>
              <div class="text-[11px] text-slate-500">مبلغ</div>
              <div class="text-white font-mono">
                {{ drawer.price_amount_usd.toFixed(2) }} $
              </div>
              <div class="text-[11px] text-slate-500 font-mono">
                {{ fmtNumber(drawer.pay_amount) }} {{ drawer.pay_currency }}
              </div>
            </div>
            <div>
              <div class="text-[11px] text-slate-500">کارت مقصد</div>
              <div class="text-slate-200">{{ drawer.card_holder || "—" }}</div>
              <div class="font-mono text-slate-300">
                {{ drawer.card_number || "" }}
              </div>
              <div class="text-[11px] text-slate-500">
                {{ drawer.card_bank || "" }}
              </div>
            </div>
            <div>
              <div class="text-[11px] text-slate-500">زمان ثبت</div>
              <div class="text-slate-300">{{ fmtDate(drawer.created_at) }}</div>
              <span
                :class="statusBadge(drawer.status)"
                class="mt-1 inline-block"
                >{{ statusLabel(drawer.status) }}</span
              >
            </div>
          </div>

          <div class="border-t border-bg-border pt-3">
            <h4 class="text-sm font-bold text-white mb-2">ارزیابی ریسک</h4>
            <div class="grid grid-cols-3 gap-3 text-xs">
              <div>
                <div class="text-[11px] text-slate-500">سن حساب</div>
                <div class="font-mono text-slate-200">
                  {{
                    drawer.context.account_age_days !== null
                      ? `${drawer.context.account_age_days} روز`
                      : "—"
                  }}
                </div>
              </div>
              <div>
                <div class="text-[11px] text-slate-500">رد ۳۰ روز اخیر</div>
                <div
                  :class="
                    drawer.context.rejected_recent > 1
                      ? 'text-rose-300 font-bold'
                      : 'text-slate-200'
                  "
                  class="font-mono"
                >
                  {{ drawer.context.rejected_recent }}
                </div>
              </div>
              <div>
                <div class="text-[11px] text-slate-500">پرداخت‌های موفق کل</div>
                <div class="font-mono text-slate-200">
                  {{ drawer.context.paid_lifetime }}
                </div>
              </div>
            </div>
          </div>

          <div v-if="drawer.ocr" class="border-t border-bg-border pt-3">
            <h4 class="text-sm font-bold text-white mb-2">
              🤖 بررسی خودکار رسید (OCR)
            </h4>
            <div
              class="text-sm rounded-lg px-3 py-2 border"
              :class="
                drawer.ocr.ok === true
                  ? 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30'
                  : drawer.ocr.ok === false
                    ? 'bg-rose-500/10 text-rose-300 border-rose-500/40'
                    : 'bg-slate-500/10 text-slate-300 border-bg-border'
              "
            >
              {{ drawer.ocr.summary }}
            </div>
          </div>

          <div class="border-t border-bg-border pt-3">
            <h4 class="text-sm font-bold text-white mb-2">عکس رسید</h4>
            <div
              v-if="drawer.receipt_file_id"
              class="rounded-lg overflow-hidden border border-bg-border bg-bg-elev/30"
            >
              <img
                :src="receiptPhotoUrl(drawer.id)"
                class="max-w-full max-h-[60vh] mx-auto block"
                alt="رسید پرداخت"
              />
            </div>
            <div v-else class="text-xs text-slate-500">عکسی ضمیمه نشده.</div>
          </div>

          <div
            v-if="drawer.status === 'pending_approval'"
            class="flex justify-end gap-2 pt-2"
          >
            <button
              class="btn btn-ghost text-rose-300"
              :disabled="busyId === drawer.id"
              @click="doReject(drawer)"
            >
              رد
            </button>
            <button
              class="btn btn-primary"
              :disabled="busyId === drawer.id"
              @click="doApprove(drawer)"
            >
              تأیید و شارژ کیف پول
            </button>
          </div>
          <div v-else class="text-xs text-slate-500 pt-2 text-end">
            این رسید قبلاً «{{ statusLabel(drawer.status) }}» شده — فقط برای
            مشاهده.
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
