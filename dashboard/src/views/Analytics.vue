<script setup lang="ts">
import { ref, onMounted, computed } from "vue";
import { useRouter } from "vue-router";
import { fetchAnalytics, type AnalyticsPayload } from "@/api/analytics";
import { ApiError } from "@/api/client";
import { fmtMoney, fmtNumber } from "@/utils/format";
import StatCard from "@/components/StatCard.vue";

const router = useRouter();
const data = ref<AnalyticsPayload | null>(null);
const loading = ref(true);
const errorMsg = ref<string | null>(null);

async function refresh() {
  loading.value = true;
  errorMsg.value = null;
  try {
    data.value = await fetchAnalytics();
  } catch (e) {
    errorMsg.value = e instanceof ApiError ? e.detail : "خطا";
  } finally {
    loading.value = false;
  }
}
onMounted(refresh);

const maxPlanRevenue = computed(() =>
  Math.max(1, ...(data.value?.revenue_by_plan.map((p) => p.revenue) ?? [1])),
);
function barWidth(rev: number): string {
  return Math.round((rev / maxPlanRevenue.value) * 100) + "%";
}
</script>

<template>
  <div class="p-6 lg:p-8 max-w-7xl mx-auto">
    <header class="flex items-center justify-between mb-6">
      <h1 class="text-2xl font-bold text-white">هوش مالی</h1>
      <button class="btn btn-secondary" @click="refresh" :disabled="loading">به‌روزرسانی</button>
    </header>

    <div v-if="errorMsg" class="card border-rose-500/40 bg-rose-500/10 text-rose-300 mb-4">{{ errorMsg }}</div>
    <div v-if="loading" class="card animate-pulse h-40" />

    <div v-else-if="data">
      <!-- ── KPI tiles ──────────────────────────────────────────── -->
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <StatCard label="درآمد کل" :value="fmtMoney(data.kpis.total_revenue)" tone="success" icon="money" />
        <StatCard label="درآمد ۳۰ روز" :value="fmtMoney(data.kpis.revenue_30d)" tone="accent" icon="money" />
        <StatCard label="درآمد ۷ روز" :value="fmtMoney(data.kpis.revenue_7d)" tone="accent" icon="money" />
        <StatCard label="نرخ نگه‌داشت" :value="data.churn.retention_rate + '%'"
                  :tone="data.churn.retention_rate >= 50 ? 'success' : 'warn'" icon="users" />
        <StatCard label="ARPU (درآمد هر مشتری)" :value="fmtMoney(data.kpis.arpu)" tone="neutral" icon="users" />
        <StatCard label="میانگین ارزش سفارش" :value="fmtMoney(data.kpis.avg_order_value)" tone="neutral" icon="money" />
        <StatCard label="مشتری‌های پرداخت‌کننده" :value="fmtNumber(data.kpis.paying_users)" tone="neutral" icon="users" />
        <StatCard label="کاربرانِ جدید (۳۰ روز)" :value="fmtNumber(data.churn.new_users_30d)" tone="accent" icon="users" />
      </div>

      <!-- ── Churn / retention ──────────────────────────────────── -->
      <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
        <div class="card">
          <div class="text-xs text-slate-500">مشترکینِ فعال</div>
          <div class="text-2xl font-bold text-emerald-300">{{ fmtNumber(data.churn.active_subscribers) }}</div>
        </div>
        <div class="card">
          <div class="text-xs text-slate-500">از‌دست‌رفته (پرداخت کرده، سرویسِ فعال ندارد)</div>
          <div class="text-2xl font-bold text-rose-300">{{ fmtNumber(data.churn.churned_users) }}</div>
        </div>
        <div class="card">
          <div class="text-xs text-slate-500">کلِ پرداخت‌کننده‌ها</div>
          <div class="text-2xl font-bold text-white">{{ fmtNumber(data.churn.paying_users) }}</div>
        </div>
      </div>

      <!-- ── Revenue by plan ────────────────────────────────────── -->
      <section class="card mb-6">
        <h3 class="text-sm font-bold text-white mb-3">درآمد به تفکیک پلن</h3>
        <div v-if="!data.revenue_by_plan.length" class="text-xs text-slate-500 py-4 text-center">داده‌ای ثبت نشده.</div>
        <div v-else class="space-y-3">
          <div v-for="p in data.revenue_by_plan" :key="p.plan" class="text-sm">
            <div class="flex justify-between mb-1 gap-2">
              <span class="text-slate-200 truncate">{{ p.plan }}</span>
              <span class="font-mono text-slate-300 whitespace-nowrap">{{ fmtMoney(p.revenue) }} · {{ p.orders }} سفارش</span>
            </div>
            <div class="h-2 rounded bg-bg-border/40 overflow-hidden">
              <div class="h-full bg-accent rounded" :style="{ width: barWidth(p.revenue) }" />
            </div>
          </div>
        </div>
      </section>

      <!-- ── Top customers (LTV) ────────────────────────────────── -->
      <section class="card overflow-hidden p-0">
        <h3 class="text-sm font-bold text-white p-4 pb-2">مشتری‌های برتر (ارزشِ طول عمر)</h3>
        <table class="data-table">
          <thead>
            <tr><th>کاربر</th><th>مجموع خرید</th><th>سفارش‌ها</th></tr>
          </thead>
          <tbody>
            <tr v-for="c in data.top_customers" :key="c.user_id"
                class="cursor-pointer" @click="router.push(`/users/${c.user_id}`)">
              <td>
                <div class="text-white text-sm">{{ c.name }}</div>
                <div class="text-[11px] text-slate-500 font-mono">{{ c.telegram_id }}</div>
              </td>
              <td class="font-mono text-emerald-300">{{ fmtMoney(c.total_spent) }}</td>
              <td class="text-slate-300">{{ c.orders }}</td>
            </tr>
            <tr v-if="!data.top_customers.length">
              <td colspan="3" class="text-center text-slate-500 py-6">داده‌ای ثبت نشده.</td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>
  </div>
</template>
