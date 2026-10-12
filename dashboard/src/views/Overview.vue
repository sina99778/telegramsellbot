<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { RouterLink } from "vue-router";
import { fetchOverview, type OverviewPayload } from "@/api/overview";
import { ApiError } from "@/api/client";
import { useOperations, serverHealth } from "@/stores/operations";
import { useAuthStore } from "@/stores/auth";
import { fmtBytes, fmtMoney, fmtNumber, fmtRelativeTime } from "@/utils/format";
import { latestOnly } from "@/utils/latest";
import { downloadCsv } from "@/utils/csv";
import Icon from "@/components/Icon.vue";
import LineChart from "@/components/LineChart.vue";
const ops = useOperations();
const auth = useAuthStore();
const data = ref<OverviewPayload | null>(null);
const loading = ref(false);
const error = ref("");
const clock = ref(Date.now());
const range = ref<7 | 30>(30);
const autoRefresh = ref(false);
const activityFilter = ref("all");
const run = latestOnly();
let interval: ReturnType<typeof setInterval>;
async function refresh() {
  loading.value = true;
  error.value = "";
  await Promise.all([
    ops.refresh(),
    run((signal) => fetchOverview(signal), {
      commit: (r) => {
        data.value = r;
      },
      fail: (e) => {
        error.value =
          e instanceof ApiError
            ? e.detail
            : "دریافت اطلاعات انجام نشد؛ دوباره تلاش کنید.";
      },
      settle: () => {
        loading.value = false;
      },
    }),
  ]);
}
onMounted(() => {
  refresh();
  interval = setInterval(() => {
    clock.value = Date.now();
    if (
      autoRefresh.value &&
      !loading.value &&
      document.visibilityState === "visible"
    )
      refresh();
  }, 60000);
});
onBeforeUnmount(() => {
  clearInterval(interval);
  run.cancel();
});
const kpis = computed(() => data.value?.kpis);
const metrics = computed(() =>
  kpis.value
    ? [
        {
          label: "درآمد ماه جاری",
          value: fmtMoney(kpis.value.revenue_mtd_usd),
          hint: "درآمد ثبت‌شده به دلار",
          icon: "money",
          color: "green",
        },
        {
          label: "درآمد ۲۴ ساعت",
          value: fmtMoney(kpis.value.last_24h.revenue_usd),
          hint: fmtNumber(kpis.value.last_24h.purchases) + " سفارش در ۲۴ ساعت",
          icon: "chart",
          color: "blue",
        },
        {
          label: "سرویس‌های فعال",
          value: fmtNumber(kpis.value.active_subs),
          hint: "شامل انتظار اولین استفاده",
          icon: "server",
          color: "green",
        },
        {
          label: "کل کاربران",
          value: fmtNumber(kpis.value.total_users),
          hint: fmtNumber(kpis.value.last_24h.signups) + " ثبت‌نام در ۲۴ ساعت",
          icon: "users",
          color: "purple",
        },
        {
          label: "سرورهای فعال",
          value: fmtNumber(kpis.value.active_servers),
          hint: "فعال در تنظیمات سامانه",
          icon: "server",
          color: "blue",
        },
        {
          label: "ترافیک تحویل‌شده",
          value: fmtBytes(kpis.value.traffic_delivered_bytes),
          hint: "مجموع مصرف ثبت‌شده",
          icon: "traffic",
          color: "amber",
        },
      ]
    : [],
);
const revenue = computed(
  () => data.value?.charts.revenue_30d.slice(-range.value) || [],
);
const signups = computed(
  () => data.value?.charts.signups_30d.slice(-range.value) || [],
);
const activity = computed(() =>
  (data.value?.recent_activity || []).filter(
    (e) => activityFilter.value === "all" || e.kind === activityFilter.value,
  ),
);
const monitoredServers = computed(() =>
  [...(ops.servers || [])]
    .sort((a, b) => {
      const priority = { unhealthy: 0, unknown: 1, healthy: 2, disabled: 3 };
      return priority[serverHealth(a)] - priority[serverHealth(b)];
    })
    .slice(0, 7),
);
const healthLabels = {
  healthy: "سالم",
  unhealthy: "اختلال",
  unknown: "بررسی‌نشده",
  disabled: "غیرفعال",
};
const dateLabel = computed(() =>
  new Intl.DateTimeFormat("fa-IR-u-nu-latn", { dateStyle: "long" }).format(
    new Date(),
  ),
);
const generatedRel = computed(() =>
  fmtRelativeTime(data.value?.generated_at, clock.value),
);
const currentPeriod = computed(() =>
  range.value === 7 ? "۷ روز اخیر" : "۳۰ روز اخیر",
);
function exportSummary() {
  if (!kpis.value) return;
  const k = kpis.value;
  downloadCsv(
    "dashboard-summary-" + new Date().toISOString().slice(0, 10) + ".csv",
    [
      ["بخش", "شاخص یا تاریخ", "مقدار"],
      ["آمار", "درآمد ماه جاری USD", k.revenue_mtd_usd],
      ["آمار", "کاربران", k.total_users],
      ["آمار", "سرویس فعال", k.active_subs],
      ["آمار", "ترافیک بایت", k.traffic_delivered_bytes],
      ...revenue.value.map((p) => ["درآمد روزانه USD", p.date, p.value]),
      ...signups.value.map((p) => ["ثبت‌نام روزانه", p.date, p.value]),
    ],
  );
}
</script>
<template>
  <div class="p-6 lg:p-8 max-w-7xl mx-auto operations-view">
    <header class="operations-header">
      <div>
        <div class="eyebrow mb-1">
          {{ dateLabel }} <span class="mx-2">/</span> نمای مدیریتی
        </div>
        <h1 class="text-2xl font-bold text-white">مرکز عملیات</h1>
        <p class="text-xs text-slate-400 mt-1">
          {{ auth.profile?.display_name || auth.profile?.username }}، وضعیت
          کسب‌وکار و کارهای نیازمند رسیدگی را اینجا ببینید.
        </p>
      </div>
      <div class="operations-header-actions">
        <label class="auto-refresh"
          ><input type="checkbox" v-model="autoRefresh" /> خودکار · ۶۰
          ثانیه</label
        ><button
          class="btn btn-secondary"
          :disabled="!data"
          @click="exportSummary"
        >
          <Icon name="download" :size="15" /> خروجی آمار</button
        ><button class="btn btn-secondary" :disabled="loading" @click="refresh">
          <Icon
            name="refresh"
            :size="15"
            :class="{ 'animate-spin': loading }"
          />
          به‌روزرسانی
        </button>
      </div>
    </header>
    <div v-if="error" class="console-alert danger" role="alert">
      <Icon name="alert" :size="16" /><span>{{ error }}</span>
    </div>
    <div v-if="ops.errors.length" class="console-alert warning" role="status">
      <Icon name="alert" :size="16" /><span
        >اطلاعات {{ ops.errors.join("، ") }} به‌روز نشد؛ مقادیر قبلی ممکن است
        نمایش داده شوند.</span
      ><button class="ms-auto underline" @click="ops.refresh()">
        تلاش دوباره
      </button>
    </div>
    <div
      v-if="loading && !data"
      class="metric-grid"
      aria-label="در حال دریافت آمار"
    >
      <div v-for="i in 6" :key="i" class="metric-card animate-pulse h-28"></div>
    </div>
    <div v-if="kpis" class="metric-grid">
      <article
        v-for="metric in metrics"
        :key="metric.label"
        class="metric-card"
      >
        <div class="metric-label">
          <span>{{ metric.label }}</span
          ><span class="metric-icon" :class="metric.color"
            ><Icon :name="metric.icon" :size="15"
          /></span>
        </div>
        <strong
          class="metric-number"
          :class="{ 'is-long': metric.value.length > 12 }"
          :title="metric.value"
          dir="ltr"
          >{{ metric.value }}</strong
        ><small>{{ metric.hint }}</small>
      </article>
    </div>
    <div class="attention-strip">
      <RouterLink to="/receipts" class="attention-item"
        ><span class="attention-icon amber"><Icon name="receipt" /></span
        ><span
          ><strong>رسید بانکی</strong><small>در انتظار بررسی دستی</small></span
        ><b>{{
          ops.receiptCount === null ? "—" : fmtNumber(ops.receiptCount)
        }}</b
        ><Icon name="chevron" :size="14" /></RouterLink
      ><RouterLink to="/transactions?tab=pending" class="attention-item"
        ><span class="attention-icon blue"><Icon name="clock" /></span
        ><span
          ><strong>پرداخت‌های باز</strong
          ><small>نیازمند بررسی وضعیت</small></span
        ><b>{{
          ops.pendingCount === null ? "—" : fmtNumber(ops.pendingCount)
        }}</b
        ><Icon name="chevron" :size="14" /></RouterLink
      ><RouterLink to="/servers?health=unhealthy" class="attention-item"
        ><span
          class="attention-icon"
          :class="ops.unhealthy.length ? 'red' : ops.unknown.length ? 'amber' : ops.servers ? 'green' : 'blue'"
          ><Icon name="server" /></span
        ><span
          ><strong>اختلال سرور</strong
          ><small>{{
            ops.unknown.length
              ? ops.unknown.length + " سرور بررسی‌نشده"
              : "آخرین وضعیت ثبت‌شده"
          }}</small></span
        ><b>{{
          ops.servers === null ? "—" : fmtNumber(ops.unhealthy.length)
        }}</b
        ><Icon name="chevron" :size="14"
      /></RouterLink>
    </div>
    <section v-if="data" class="overview-chart-section">
      <div class="section-heading">
        <div>
          <h2>روند کسب‌وکار</h2>
          <span>{{
            data.generated_at ? "آخرین دریافت آمار: " + generatedRel : ""
          }}</span>
        </div>
        <div class="segmented" role="group" aria-label="بازهٔ نمودار">
          <button
            :class="{ 'is-active': range === 7 }"
            :aria-pressed="range === 7"
            @click="range = 7"
          >
            ۷ روز</button
          ><button
            :class="{ 'is-active': range === 30 }"
            :aria-pressed="range === 30"
            @click="range = 30"
          >
            ۳۰ روز
          </button>
        </div>
      </div>
      <div class="overview-charts">
        <LineChart
          title="درآمد روزانه"
          :points="revenue"
          color="#5dd6a0"
          unit="$"
          :period-label="currentPeriod"
        /><LineChart
          title="ثبت‌نام کاربران"
          :points="signups"
          color="#769de8"
          :period-label="currentPeriod"
        />
      </div>
    </section>
    <div class="operations-bottom-grid">
      <section class="console-panel">
        <header class="panel-heading">
          <div>
            <h2><Icon name="server" :size="16" /> وضعیت سرورها</h2>
            <p>اختلال‌ها در ابتدای فهرست · سلامت ثبت‌شده در سامانه</p>
          </div>
          <RouterLink to="/servers" class="panel-link"
            >همهٔ سرورها <Icon name="chevron" :size="12"
          /></RouterLink>
        </header>
        <div class="table-scroll" tabindex="0" aria-label="وضعیت سرورها">
          <table class="data-table server-monitor">
            <thead>
              <tr>
                <th>سرور</th>
                <th>سلامت</th>
                <th>کانفیگ فعال / سقف</th>
                <th>اینباند فعال</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="s in monitoredServers" :key="s.id">
                <td>
                  <strong>{{ s.name }}</strong
                  ><small dir="ltr">{{
                    s.panel_type === "sanaei_xui"
                      ? "3x-ui / Sanaei"
                      : s.panel_type
                  }}</small>
                </td>
                <td>
                  <span class="health-indicator" :class="serverHealth(s)"
                    ><i></i>{{ healthLabels[serverHealth(s)] }}</span
                  >
                </td>
                <td>
                  <div class="font-mono text-xs numeric" dir="ltr">
                    {{ fmtNumber(s.active_client_count) }}
                    <span class="text-slate-500"
                      >/
                      {{
                        s.max_clients === null
                          ? "نامحدود"
                          : fmtNumber(s.max_clients)
                      }}</span
                    >
                  </div>
                  <div
                    v-if="s.max_clients !== null && s.max_clients > 0"
                    class="capacity-track"
                    :class="{
                      'near-full': s.active_client_count / s.max_clients >= 0.9,
                    }"
                  >
                    <i
                      :style="{
                        width:
                          Math.min(
                            100,
                            Math.max(
                              0,
                              (s.active_client_count / s.max_clients) * 100,
                            ),
                          ) + '%',
                      }"
                    ></i>
                  </div>
                </td>
                <td class="font-mono numeric" dir="ltr">
                  {{ s.active_inbound_count }}
                  <span class="text-slate-500">/ {{ s.inbound_count }}</span>
                </td>
                <td>
                  <RouterLink
                    :to="'/servers?q=' + encodeURIComponent(s.name)"
                    class="icon-button"
                    :aria-label="'مدیریت سرور ' + s.name"
                    ><Icon name="chevron" :size="14"
                  /></RouterLink>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="!ops.servers" class="empty-state">
          {{
            ops.errors.length
              ? "اطلاعات سرورها در دسترس نیست."
              : "در حال دریافت وضعیت سرورها…"
          }}
        </p>
        <p v-else-if="!ops.servers.length" class="empty-state">
          هنوز سروری ثبت نشده است.
          <RouterLink to="/servers" class="text-accent">افزودن سرور</RouterLink>
        </p>
        <footer class="panel-footnote">
          <Icon name="clock" :size="12" />{{
            ops.updatedAt
              ? "آخرین دریافت: " +
                ops.updatedAt.toLocaleTimeString("fa-IR-u-nu-latn", {
                  hour: "2-digit",
                  minute: "2-digit",
                })
              : "وضعیت از پنل‌های ثبت‌شده دریافت می‌شود"
          }}
        </footer>
      </section>
      <section class="console-panel">
        <header class="panel-heading">
          <div>
            <h2><Icon name="clock" :size="16" /> فعالیت‌های اخیر</h2>
            <p>سفارش‌ها و ثبت‌نام‌های اخیر</p>
          </div>
        </header>
        <div class="activity-filters" role="group" aria-label="فیلتر فعالیت">
          <button
            v-for="f in [
              { key: 'all', label: 'همه' },
              { key: 'order', label: 'سفارش' },
              { key: 'signup', label: 'ثبت‌نام' },
            ]"
            :key="f.key"
            :class="{ 'is-active': activityFilter === f.key }"
            :aria-pressed="activityFilter === f.key"
            @click="activityFilter = f.key"
          >
            {{ f.label }}
          </button>
        </div>
        <ul v-if="activity.length" class="activity-list">
          <li
            v-for="(ev, index) in activity.slice(0, 6)"
            :key="ev.at + '-' + index"
          >
            <span
              class="activity-dot"
              :class="ev.kind === 'order' ? 'green' : 'blue'"
              ><Icon :name="ev.kind === 'order' ? 'money' : 'users'" :size="14"
            /></span>
            <div>
              <strong
                >{{ ev.kind === "order" ? "سفارش ثبت‌شده" : "ثبت‌نام جدید" }}
                <span v-if="ev.kind === 'order'" dir="ltr">{{
                  fmtMoney(ev.amount_usd || 0)
                }}</span></strong
              ><RouterLink
                v-if="ev.user_telegram_id"
                :to="'/users?q=' + ev.user_telegram_id"
                >{{ ev.user_first_name || ev.user_telegram_id }}</RouterLink
              ><span v-else>{{ ev.user_first_name || "بدون نام" }}</span>
            </div>
            <small>{{ fmtRelativeTime(ev.at) }}</small>
          </li>
        </ul>
        <p v-else class="empty-state">
          {{
            data
              ? "فعالیتی با این فیلتر ثبت نشده است."
              : "در حال دریافت فعالیت‌ها…"
          }}
        </p>
        <RouterLink to="/transactions" class="activity-footer-link"
          >مشاهدهٔ تراکنش‌ها <Icon name="chevron" :size="13"
        /></RouterLink>
      </section>
    </div>
  </div>
</template>
