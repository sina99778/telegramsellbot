<script setup lang="ts">
// Overview / "home" of the dashboard. Six KPI tiles + two charts +
// a recent-activity feed. Refreshes on mount and on demand.

import { ref, onMounted, computed } from "vue";
import { fetchOverview, type OverviewPayload } from "@/api/overview";
import { ApiError } from "@/api/client";
import { fmtBytes, fmtMoney, fmtNumber, fmtRelativeTime } from "@/utils/format";
import StatCard from "@/components/StatCard.vue";
import LineChart from "@/components/LineChart.vue";

const data = ref<OverviewPayload | null>(null);
const loading = ref(false);
const errorMsg = ref<string | null>(null);

async function refresh() {
  loading.value = true;
  errorMsg.value = null;
  try {
    data.value = await fetchOverview();
  } catch (exc) {
    if (exc instanceof ApiError) {
      errorMsg.value = exc.detail || `خطا (${exc.status})`;
    } else {
      errorMsg.value = "خطای شبکه — اتصال خود را بررسی کنید.";
    }
  } finally {
    loading.value = false;
  }
}

onMounted(refresh);

const kpis = computed(() => data.value?.kpis);
const charts = computed(() => data.value?.charts);
const activity = computed(() => data.value?.recent_activity || []);
const generatedRel = computed(() =>
  data.value ? fmtRelativeTime(data.value.generated_at) : "",
);
</script>

<template>
  <div class="p-6 lg:p-8 max-w-7xl mx-auto">
    <!-- ── Header ─────────────────────────────────────────────────── -->
    <header class="flex flex-wrap items-center justify-between gap-3 mb-6">
      <div>
        <h1 class="text-2xl font-bold text-white">نمای کلی</h1>
        <p class="text-sm text-slate-400 mt-1">
          خلاصه‌ی فعالیت ربات
          <span v-if="generatedRel" class="text-slate-500">— به‌روزرسانی: {{ generatedRel }}</span>
        </p>
      </div>
      <button class="btn btn-secondary" :disabled="loading" @click="refresh">
        <svg
          v-if="!loading"
          class="w-4 h-4"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
        >
          <path stroke-linecap="round" stroke-linejoin="round" d="M3 12a9 9 0 1 0 3-6.7M3 4v6h6" />
        </svg>
        <svg v-else class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
          <circle
            cx="12"
            cy="12"
            r="10"
            stroke="currentColor"
            stroke-width="3"
            stroke-dasharray="40 60"
            stroke-linecap="round"
          />
        </svg>
        به‌روزرسانی
      </button>
    </header>

    <!-- ── Error ─────────────────────────────────────────────────── -->
    <div
      v-if="errorMsg"
      class="card border-rose-500/40 bg-rose-500/10 text-rose-300 mb-6"
    >
      {{ errorMsg }}
    </div>

    <!-- ── Loading skeleton ──────────────────────────────────────── -->
    <div v-if="loading && !data" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      <div v-for="i in 6" :key="i" class="card animate-pulse h-28" />
    </div>

    <!-- ── KPI grid ──────────────────────────────────────────────── -->
    <div v-if="kpis" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 mb-6">
      <StatCard
        tone="accent"
        icon="users"
        label="مجموع کاربران"
        :value="fmtNumber(kpis.total_users)"
        :hint="`+${kpis.last_24h.signups} امروز`"
        :trendLabel="kpis.last_24h.signups > 0 ? `+${kpis.last_24h.signups}` : '0'"
        :trendTone="kpis.last_24h.signups > 0 ? 'ok' : 'warn'"
      />
      <StatCard
        tone="success"
        icon="service"
        label="سرویس‌های فعال"
        :value="fmtNumber(kpis.active_subs)"
        hint="active + pending_activation"
      />
      <StatCard
        tone="warn"
        icon="money"
        label="درآمد ماه جاری"
        :value="fmtMoney(kpis.revenue_mtd_usd)"
        :hint="`${fmtMoney(kpis.last_24h.revenue_usd)} در ۲۴ ساعت اخیر`"
      />
      <StatCard
        tone="accent"
        icon="traffic"
        label="حجم تحویل‌شده"
        :value="fmtBytes(kpis.traffic_delivered_bytes)"
        hint="lifetime_used + current cycle"
      />
      <StatCard
        tone="success"
        icon="server"
        label="سرورهای فعال"
        :value="fmtNumber(kpis.active_servers)"
        hint="X-UI panels online"
      />
      <StatCard
        tone="warn"
        icon="money"
        label="خریدهای ۲۴ ساعت اخیر"
        :value="fmtNumber(kpis.last_24h.purchases)"
        :hint="`مجموع: ${fmtMoney(kpis.last_24h.revenue_usd)}`"
      />
    </div>

    <!-- ── Charts ────────────────────────────────────────────────── -->
    <div v-if="charts" class="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6">
      <LineChart
        title="درآمد روزانه (USD)"
        :points="charts.revenue_30d"
        color="#36D1E0"
        unit="$"
      />
      <LineChart
        title="کاربران جدید روزانه"
        :points="charts.signups_30d"
        color="#10b981"
      />
    </div>

    <!-- ── Recent activity ───────────────────────────────────────── -->
    <div v-if="activity.length" class="card">
      <h3 class="text-sm font-bold text-white mb-3">فعالیت‌های اخیر</h3>
      <ul class="space-y-2">
        <li
          v-for="(ev, idx) in activity"
          :key="idx"
          class="flex items-center gap-3 py-2 border-b border-bg-border/60 last:border-b-0"
        >
          <span
            :class="['inline-flex w-8 h-8 items-center justify-center rounded-full',
                     ev.kind === 'order' ? 'bg-emerald-500/15 text-emerald-300' : 'bg-accent/15 text-accent']"
          >
            <svg
              v-if="ev.kind === 'order'"
              class="w-4 h-4"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <rect x="2" y="6" width="20" height="12" rx="2" />
              <circle cx="12" cy="12" r="3" />
            </svg>
            <svg
              v-else
              class="w-4 h-4"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <path
                stroke-linecap="round"
                stroke-linejoin="round"
                d="M16 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2M9 7a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z"
              />
            </svg>
          </span>
          <div class="flex-1 min-w-0">
            <div class="text-sm text-white">
              <template v-if="ev.kind === 'order'">
                خرید جدید: <b>{{ fmtMoney(ev.amount_usd || 0) }}</b>
                <span class="text-slate-400">
                  ({{ ev.user_first_name || ev.user_telegram_id }})
                </span>
              </template>
              <template v-else>
                کاربر جدید:
                <b>{{ ev.user_first_name || ev.user_telegram_id || "?" }}</b>
              </template>
            </div>
            <div class="text-[11px] text-slate-500">{{ fmtRelativeTime(ev.at) }}</div>
          </div>
        </li>
      </ul>
    </div>
  </div>
</template>
