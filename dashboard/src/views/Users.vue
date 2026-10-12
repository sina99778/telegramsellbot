<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from "vue";
import { useRouter, useRoute } from "vue-router";
import { listUsers, type UserListItem } from "@/api/users";
import { ApiError } from "@/api/client";
import { fmtMoney, fmtNumber, fmtRelativeTime } from "@/utils/format";
import { latestOnly } from "@/utils/latest";

import Icon from "@/components/Icon.vue";
import { downloadCsv } from "@/utils/csv";
const router = useRouter();
const route = useRoute();

const items = ref<UserListItem[]>([]);
const total = ref(0);
const totalPages = ref(1);

const q = ref(typeof route.query.q === "string" ? route.query.q : "");
const status = ref<"" | "active" | "banned">("");
const page = ref(1);
const pageSize = ref(25);
const sort = ref<"created_at" | "last_seen_at" | "telegram_id">("created_at");
const order = ref<"asc" | "desc">("desc");

const loading = ref(false);
const errorMsg = ref<string | null>(null);

// Typing in the search box or paging fast keeps several requests in flight;
// only the newest one is allowed to paint the table.
const runLatest = latestOnly();

async function refresh() {
  loading.value = true;
  errorMsg.value = null;
  await runLatest(
    (signal) =>
      listUsers(
        {
          q: q.value.trim() || undefined,
          status: status.value || undefined,
          page: page.value,
          page_size: pageSize.value,
          sort: sort.value,
          order: order.value,
        },
        signal,
      ),
    {
      commit: (r) => {
        items.value = r.items;
        total.value = r.total;
        totalPages.value = r.total_pages;
      },
      fail: (exc) => {
        errorMsg.value = exc instanceof ApiError ? exc.detail : "خطای شبکه";
      },
      settle: () => {
        loading.value = false;
      },
    },
  );
}

let searchTimer: ReturnType<typeof setTimeout> | null = null;
watch(q, () => {
  loading.value = true;
  runLatest.cancel();
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    if (page.value !== 1) page.value = 1;
    else refresh();
  }, 300);
});
watch([status, sort, order, pageSize], () => {
  if (page.value !== 1) page.value = 1;
  else refresh();
});
watch(page, refresh);

onMounted(refresh);
onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer);
  runLatest.cancel();
});
function exportPage() {
  downloadCsv("users-page-" + page.value + ".csv", [
    [
      "نام",
      "یوزرنیم",
      "شناسه تلگرام",
      "موجودی USD",
      "اعتبار USD",
      "وضعیت",
      "آخرین فعالیت",
    ],
    ...items.value.map((u) => [
      u.first_name,
      u.username,
      u.telegram_id,
      u.balance_usd,
      u.credit_limit_usd,
      u.status,
      u.last_seen_at,
    ]),
  ]);
}
function resetFilters() {
  q.value = "";
  status.value = "";
  sort.value = "created_at";
  order.value = "desc";
}
const pageBalance = computed(() =>
  items.value.reduce((sum, u) => sum + u.balance_usd, 0),
);

function openUser(u: UserListItem) {
  router.push({ name: "user-detail", params: { id: u.id } });
}

const statusBadgeClass = computed(
  () => (s: string) =>
    s === "banned" ? "badge badge-danger" : "badge badge-success",
);
</script>

<template>
  <div class="p-6 lg:p-8 max-w-7xl mx-auto">
    <!-- Header -->
    <header class="flex flex-wrap items-end justify-between gap-3 mb-6">
      <div>
        <h1 class="text-2xl font-bold text-white">مدیریت کاربران</h1>
        <p class="text-sm text-slate-400 mt-1">
          <span v-if="total > 0"
            >مجموع: <b>{{ fmtNumber(total) }}</b> کاربر</span
          >
        </p>
      </div>
      <div class="flex gap-2">
        <button
          class="btn btn-secondary"
          :disabled="loading || !items.length"
          @click="exportPage"
        >
          <Icon name="download" :size="15" />خروجی این صفحه</button
        ><button class="btn btn-secondary" :disabled="loading" @click="refresh">
          <Icon name="refresh" :size="15" />به‌روزرسانی
        </button>
      </div>
    </header>
    <div class="table-summary">
      <span
        ><b>{{ fmtNumber(total) }}</b> کاربر با این فیلتر</span
      ><span
        ><b>{{ items.length }}</b> ردیف در این صفحه</span
      ><span
        >جمع موجودی صفحه: <b dir="ltr">{{ fmtMoney(pageBalance) }}</b></span
      >
    </div>
    <!-- Filters -->
    <div class="card mb-4 flex flex-wrap gap-3 items-end">
      <div class="flex-1 min-w-[180px]">
        <label class="label" for="user-search">جستجو</label>
        <input
          id="user-search"
          v-model="q"
          class="input"
          placeholder="نام، یوزرنیم، یا Telegram ID …"
          type="text"
        />
      </div>
      <div>
        <label class="label">وضعیت</label>
        <select v-model="status" class="input">
          <option value="">همه</option>
          <option value="active">فعال</option>
          <option value="banned">مسدود</option>
        </select>
      </div>
      <div>
        <label class="label">مرتب‌سازی</label>
        <select v-model="sort" class="input">
          <option value="created_at">تاریخ ثبت‌نام</option>
          <option value="last_seen_at">آخرین فعالیت</option>
          <option value="telegram_id">Telegram ID</option>
        </select>
      </div>
      <div>
        <label class="label">جهت</label>
        <select v-model="order" class="input">
          <option value="desc">↓ نزولی</option>
          <option value="asc">↑ صعودی</option>
        </select>
      </div>
      <button
        v-if="q || status || sort !== 'created_at' || order !== 'desc'"
        class="btn btn-ghost"
        @click="resetFilters"
      >
        پاک‌کردن فیلترها
      </button>
    </div>

    <!-- Error -->
    <div
      v-if="errorMsg"
      class="card border-rose-500/40 bg-rose-500/10 text-rose-300 mb-4"
    >
      {{ errorMsg }}
    </div>

    <!-- Table -->
    <div
      class="card overflow-hidden p-0"
      :aria-busy="loading"
      :class="{ 'table-loading': loading }"
    >
      <div class="table-scroll" tabindex="0" aria-label="جدول اطلاعات">
        <table class="data-table">
          <thead>
            <tr>
              <th>کاربر</th>
              <th>Telegram ID</th>
              <th>موجودی</th>
              <th>اعتبار</th>
              <th>وضعیت</th>
              <th>آخرین فعالیت</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="u in items"
              :key="u.id"
              class="cursor-pointer"
              @click="openUser(u)"
            >
              <td>
                <button
                  class="font-medium text-white hover:text-accent"
                  @click.stop="openUser(u)"
                >
                  {{ u.first_name || "—" }}
                </button>
                <div class="text-[11px] text-slate-500">
                  {{ u.username ? "@" + u.username : "(بدون یوزرنیم)" }}
                </div>
              </td>
              <td>
                <code class="text-slate-300">{{ u.telegram_id }}</code>
              </td>
              <td class="font-mono">{{ fmtMoney(u.balance_usd) }}</td>
              <td class="font-mono text-slate-400">
                {{ fmtMoney(u.credit_limit_usd) }}
              </td>
              <td>
                <span :class="statusBadgeClass(u.status)">
                  {{ u.status === "banned" ? "مسدود" : "فعال" }}
                </span>
                <span v-if="u.role !== 'user'" class="badge badge-warn ms-1">
                  {{
                    u.role === "admin"
                      ? "مدیر"
                      : u.role === "owner"
                        ? "مالک"
                        : u.role === "reseller"
                          ? "فروشنده"
                          : u.role
                  }}
                </span>
              </td>
              <td class="text-slate-500 text-xs">
                {{ fmtRelativeTime(u.last_seen_at) }}
              </td>
            </tr>
            <tr v-if="!items.length && !loading">
              <td colspan="6" class="text-center text-slate-500 py-8">
                کاربری پیدا نشد.
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Pagination -->
    <div class="flex items-center justify-between mt-4">
      <div class="text-xs text-slate-500">
        <label class="inline-flex items-center gap-2"
          >ردیف در صفحه
          <select v-model.number="pageSize" class="input !w-auto">
            <option :value="25">25</option>
            <option :value="50">50</option>
            <option :value="100">100</option>
          </select></label
        ><span class="ms-3">صفحه {{ page }} از {{ totalPages }}</span>
      </div>
      <div class="flex gap-2">
        <button
          class="btn btn-secondary"
          :disabled="page <= 1 || loading"
          @click="page = Math.max(1, page - 1)"
        >
          قبلی
        </button>
        <button
          class="btn btn-secondary"
          :disabled="page >= totalPages || loading"
          @click="page = Math.min(totalPages, page + 1)"
        >
          بعدی
        </button>
      </div>
    </div>
  </div>
</template>
