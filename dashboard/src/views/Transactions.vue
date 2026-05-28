<script setup lang="ts">
import { ref, computed, watch, onMounted } from "vue";
import {
  listOrders,
  listPayments,
  listWalletTxns,
  listPending,
  ordersExportUrl,
  type OrderRow,
  type PaymentRow,
  type WalletTxnRow,
  type PendingRow,
} from "@/api/transactions";
import { ApiError } from "@/api/client";
import { fmtMoney, fmtNumber, fmtRelativeTime } from "@/utils/format";

type Tab = "orders" | "payments" | "wallet" | "pending";

const tab = ref<Tab>("orders");

// Shared filter state
const filterStatus = ref("");
const filterProvider = ref("");
const filterUserTgId = ref<string>("");
const filterDirection = ref<"" | "credit" | "debit">("");
const filterFromDate = ref("");
const filterToDate = ref("");
const page = ref(1);
const pageSize = ref(25);

// Tab data
const orders = ref<OrderRow[]>([]);
const payments = ref<PaymentRow[]>([]);
const wallet = ref<WalletTxnRow[]>([]);
const pending = ref<PendingRow[]>([]);
const total = ref(0);
const totalPages = ref(1);

const loading = ref(false);
const errorMsg = ref<string | null>(null);

const userTgIdNum = computed(() => {
  const n = parseInt(filterUserTgId.value.trim(), 10);
  return Number.isNaN(n) ? undefined : n;
});

async function refresh() {
  loading.value = true;
  errorMsg.value = null;
  try {
    if (tab.value === "orders") {
      const r = await listOrders({
        status: filterStatus.value || undefined,
        user_telegram_id: userTgIdNum.value,
        from_date: filterFromDate.value || undefined,
        to_date: filterToDate.value || undefined,
        page: page.value,
        page_size: pageSize.value,
      });
      orders.value = r.items;
      total.value = r.total;
      totalPages.value = r.total_pages;
    } else if (tab.value === "payments") {
      const r = await listPayments({
        status: filterStatus.value || undefined,
        provider: filterProvider.value || undefined,
        user_telegram_id: userTgIdNum.value,
        page: page.value,
        page_size: pageSize.value,
      });
      payments.value = r.items;
      total.value = r.total;
      totalPages.value = r.total_pages;
    } else if (tab.value === "wallet") {
      const r = await listWalletTxns({
        direction: filterDirection.value || undefined,
        user_telegram_id: userTgIdNum.value,
        page: page.value,
        page_size: pageSize.value,
      });
      wallet.value = r.items;
      total.value = r.total;
      totalPages.value = r.total_pages;
    } else {
      const r = await listPending();
      pending.value = r.items;
      total.value = r.total;
      totalPages.value = 1;
    }
  } catch (exc) {
    errorMsg.value = exc instanceof ApiError ? exc.detail : "خطا";
  } finally {
    loading.value = false;
  }
}

watch(tab, () => {
  page.value = 1;
  filterStatus.value = "";
  filterProvider.value = "";
  filterDirection.value = "";
  refresh();
});
watch([filterStatus, filterProvider, filterDirection, filterUserTgId, filterFromDate, filterToDate], () => {
  page.value = 1;
  refresh();
});
watch(page, refresh);
onMounted(refresh);

const exportHref = computed(() =>
  ordersExportUrl({
    status: filterStatus.value || undefined,
    from_date: filterFromDate.value || undefined,
    to_date: filterToDate.value || undefined,
  }),
);

function statusBadge(s: string): string {
  if (["paid", "provisioned", "finished"].includes(s)) return "badge badge-success";
  if (["pending", "pending_approval", "waiting_hash", "waiting_receipt", "processing"].includes(s))
    return "badge badge-warn";
  if (["refunded", "cancelled", "failed"].includes(s)) return "badge badge-danger";
  return "badge badge-muted";
}
</script>

<template>
  <div class="p-6 lg:p-8 max-w-7xl mx-auto">
    <header class="flex flex-wrap items-end justify-between gap-3 mb-6">
      <div>
        <h1 class="text-2xl font-bold text-white">تراکنش‌ها و سفارش‌ها</h1>
        <p class="text-sm text-slate-400 mt-1">گزارش‌های مالی + صف تأیید دستی.</p>
      </div>
    </header>

    <!-- Tabs -->
    <div class="flex border-b border-bg-border mb-4 overflow-x-auto">
      <button
        v-for="t in ['orders', 'payments', 'wallet', 'pending'] as const"
        :key="t"
        :class="['px-4 py-2.5 text-sm font-medium whitespace-nowrap transition-colors',
                 tab === t
                   ? 'text-accent border-b-2 border-accent'
                   : 'text-slate-400 hover:text-slate-200']"
        @click="tab = t"
      >
        {{ t === "orders" ? "سفارش‌ها" : t === "payments" ? "پرداخت‌ها" : t === "wallet" ? "کیف پول" : "در انتظار تأیید" }}
      </button>
    </div>

    <!-- Filters (per-tab) -->
    <div class="card mb-4 flex flex-wrap gap-3 items-end" v-if="tab !== 'pending'">
      <div>
        <label class="label">Telegram ID کاربر</label>
        <input v-model="filterUserTgId" class="input" placeholder="مثلاً 5177632415" />
      </div>
      <div v-if="tab === 'orders'">
        <label class="label">وضعیت</label>
        <select v-model="filterStatus" class="input">
          <option value="">همه</option>
          <option v-for="s in ['pending','paid','processing','provisioned','refunded','cancelled']" :key="s" :value="s">{{ s }}</option>
        </select>
      </div>
      <div v-if="tab === 'payments'">
        <label class="label">وضعیت</label>
        <input v-model="filterStatus" class="input" placeholder="مثلاً waiting_hash" />
      </div>
      <div v-if="tab === 'payments'">
        <label class="label">درگاه</label>
        <select v-model="filterProvider" class="input">
          <option value="">همه</option>
          <option value="manual_crypto">manual_crypto</option>
          <option value="card_to_card">card_to_card</option>
          <option value="nowpayments">nowpayments</option>
          <option value="tetrapay">tetrapay</option>
          <option value="tronado">tronado</option>
        </select>
      </div>
      <div v-if="tab === 'wallet'">
        <label class="label">جهت</label>
        <select v-model="filterDirection" class="input">
          <option value="">همه</option>
          <option value="credit">واریز</option>
          <option value="debit">برداشت</option>
        </select>
      </div>
      <div v-if="tab === 'orders'">
        <label class="label">از تاریخ</label>
        <input v-model="filterFromDate" class="input" type="date" />
      </div>
      <div v-if="tab === 'orders'">
        <label class="label">تا تاریخ</label>
        <input v-model="filterToDate" class="input" type="date" />
      </div>
      <a v-if="tab === 'orders'" class="btn btn-secondary" :href="exportHref" download>
        📥 خروجی CSV
      </a>
    </div>

    <div v-if="errorMsg" class="card border-rose-500/40 bg-rose-500/10 text-rose-300 mb-4">
      {{ errorMsg }}
    </div>

    <!-- ── Orders table ─────────────────────────────────────────── -->
    <div v-if="tab === 'orders'" class="card p-0 overflow-hidden">
      <table class="data-table">
        <thead>
          <tr>
            <th>زمان</th>
            <th>کاربر</th>
            <th>پلن</th>
            <th>مبلغ</th>
            <th>وضعیت</th>
            <th>منبع</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="o in orders" :key="o.id">
            <td class="text-xs text-slate-500">{{ fmtRelativeTime(o.created_at) }}</td>
            <td>
              <div class="text-white">{{ o.user_first_name || "—" }}</div>
              <div class="text-[11px] text-slate-500">TG {{ o.user_telegram_id }}</div>
            </td>
            <td>{{ o.plan_name }}</td>
            <td class="font-mono">{{ fmtMoney(o.amount) }} {{ o.currency }}</td>
            <td><span :class="statusBadge(o.status)">{{ o.status }}</span></td>
            <td><span class="badge badge-muted">{{ o.source }}</span></td>
          </tr>
          <tr v-if="!orders.length && !loading">
            <td colspan="6" class="text-center text-slate-500 py-8">سفارشی پیدا نشد.</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ── Payments table ──────────────────────────────────────── -->
    <div v-if="tab === 'payments'" class="card p-0 overflow-hidden">
      <table class="data-table">
        <thead>
          <tr>
            <th>زمان</th>
            <th>کاربر</th>
            <th>درگاه</th>
            <th>مبلغ</th>
            <th>پرداختی</th>
            <th>وضعیت</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in payments" :key="p.id">
            <td class="text-xs text-slate-500">{{ fmtRelativeTime(p.created_at) }}</td>
            <td>
              <div class="text-white">{{ p.user_first_name || "—" }}</div>
              <div class="text-[11px] text-slate-500">TG {{ p.user_telegram_id }}</div>
            </td>
            <td>
              <span class="badge badge-muted">{{ p.provider }}</span>
              <span class="text-[11px] text-slate-500 ms-1">{{ p.kind }}</span>
            </td>
            <td class="font-mono">{{ fmtMoney(p.price_amount) }} {{ p.price_currency }}</td>
            <td class="font-mono text-slate-400">
              <template v-if="p.pay_amount !== null">{{ p.pay_amount }} {{ p.pay_currency }}</template>
              <template v-else>—</template>
            </td>
            <td><span :class="statusBadge(p.status)">{{ p.status }}</span></td>
          </tr>
          <tr v-if="!payments.length && !loading">
            <td colspan="6" class="text-center text-slate-500 py-8">پرداختی پیدا نشد.</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ── Wallet ─────────────────────────────────────────────── -->
    <div v-if="tab === 'wallet'" class="card p-0 overflow-hidden">
      <table class="data-table">
        <thead>
          <tr>
            <th>زمان</th>
            <th>کاربر</th>
            <th>نوع</th>
            <th>جهت</th>
            <th>مقدار</th>
            <th>موجودی پس از تراکنش</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in wallet" :key="t.id">
            <td class="text-xs text-slate-500">{{ fmtRelativeTime(t.created_at) }}</td>
            <td>
              <div class="text-white">{{ t.user_first_name || "—" }}</div>
              <div class="text-[11px] text-slate-500">TG {{ t.user_telegram_id }}</div>
            </td>
            <td>{{ t.type }}</td>
            <td>
              <span :class="t.direction === 'credit' ? 'badge badge-success' : 'badge badge-danger'">
                {{ t.direction === 'credit' ? '➕ واریز' : '➖ برداشت' }}
              </span>
            </td>
            <td class="font-mono">{{ fmtMoney(t.amount) }} {{ t.currency }}</td>
            <td class="font-mono text-slate-400">
              <template v-if="t.balance_after !== null">{{ fmtMoney(t.balance_after) }}</template>
              <template v-else>—</template>
            </td>
          </tr>
          <tr v-if="!wallet.length && !loading">
            <td colspan="6" class="text-center text-slate-500 py-8">تراکنشی پیدا نشد.</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ── Pending approvals ──────────────────────────────────── -->
    <div v-if="tab === 'pending'" class="card p-0 overflow-hidden">
      <div class="px-4 py-3 border-b border-bg-border bg-amber-500/5 text-xs text-amber-200">
        تأیید نهایی این پرداخت‌ها از طریق منوی «تأیید پرداخت‌های دستی» در ربات انجام می‌شود.
        این صفحه فقط برای مرور سریع صف است.
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>زمان</th>
            <th>کاربر</th>
            <th>درگاه</th>
            <th>مبلغ</th>
            <th>پرداختی</th>
            <th>وضعیت</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in pending" :key="p.id">
            <td class="text-xs text-slate-500">{{ fmtRelativeTime(p.created_at) }}</td>
            <td>
              <div class="text-white">{{ p.user_first_name || "—" }}</div>
              <div class="text-[11px] text-slate-500">TG {{ p.user_telegram_id }}</div>
            </td>
            <td><span class="badge badge-muted">{{ p.provider }}</span></td>
            <td class="font-mono">{{ fmtMoney(p.price_amount) }}</td>
            <td class="font-mono text-slate-400">
              <template v-if="p.pay_amount !== null">{{ p.pay_amount }} {{ p.pay_currency }}</template>
              <template v-else>—</template>
            </td>
            <td><span :class="statusBadge(p.status)">{{ p.status }}</span></td>
          </tr>
          <tr v-if="!pending.length && !loading">
            <td colspan="6" class="text-center text-slate-500 py-8">صف تأیید خالی است. ✨</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Pagination (skip for pending — single page) -->
    <div v-if="tab !== 'pending'" class="flex items-center justify-between mt-4">
      <div class="text-xs text-slate-500">
        صفحه {{ page }} از {{ totalPages }} ({{ fmtNumber(total) }} ردیف)
      </div>
      <div class="flex gap-2">
        <button class="btn btn-secondary" :disabled="page <= 1 || loading" @click="page = Math.max(1, page - 1)">قبلی</button>
        <button class="btn btn-secondary" :disabled="page >= totalPages || loading" @click="page = Math.min(totalPages, page + 1)">بعدی</button>
      </div>
    </div>
  </div>
</template>
