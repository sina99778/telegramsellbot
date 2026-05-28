<script setup lang="ts">
import { ref, computed, onMounted, watch } from "vue";
import { useRouter } from "vue-router";
import { listUsers, type UserListItem } from "@/api/users";
import { ApiError } from "@/api/client";
import { fmtMoney, fmtNumber, fmtRelativeTime } from "@/utils/format";

const router = useRouter();

const items = ref<UserListItem[]>([]);
const total = ref(0);
const totalPages = ref(1);

const q = ref("");
const status = ref<"" | "active" | "banned">("");
const page = ref(1);
const pageSize = ref(25);
const sort = ref<"created_at" | "last_seen_at" | "telegram_id">("created_at");
const order = ref<"asc" | "desc">("desc");

const loading = ref(false);
const errorMsg = ref<string | null>(null);

async function refresh() {
  loading.value = true;
  errorMsg.value = null;
  try {
    const r = await listUsers({
      q: q.value.trim() || undefined,
      status: status.value || undefined,
      page: page.value,
      page_size: pageSize.value,
      sort: sort.value,
      order: order.value,
    });
    items.value = r.items;
    total.value = r.total;
    totalPages.value = r.total_pages;
  } catch (exc) {
    errorMsg.value = exc instanceof ApiError ? exc.detail : "خطای شبکه";
  } finally {
    loading.value = false;
  }
}

let searchTimer: ReturnType<typeof setTimeout> | null = null;
watch(q, () => {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    page.value = 1;
    refresh();
  }, 300);
});
watch([status, sort, order], () => {
  page.value = 1;
  refresh();
});
watch(page, refresh);

onMounted(refresh);

function openUser(u: UserListItem) {
  router.push({ name: "user-detail", params: { id: u.id } });
}

const statusBadgeClass = computed(() => (s: string) =>
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
          <span v-if="total > 0">مجموع: <b>{{ fmtNumber(total) }}</b> کاربر</span>
        </p>
      </div>
    </header>

    <!-- Filters -->
    <div class="card mb-4 flex flex-wrap gap-3 items-end">
      <div class="flex-1 min-w-[180px]">
        <label class="label">جستجو</label>
        <input
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
    </div>

    <!-- Error -->
    <div
      v-if="errorMsg"
      class="card border-rose-500/40 bg-rose-500/10 text-rose-300 mb-4"
    >
      {{ errorMsg }}
    </div>

    <!-- Table -->
    <div class="card overflow-hidden p-0">
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
              <div class="font-medium text-white">{{ u.first_name || "—" }}</div>
              <div class="text-[11px] text-slate-500">
                {{ u.username ? "@" + u.username : "(بدون یوزرنیم)" }}
              </div>
            </td>
            <td><code class="text-slate-300">{{ u.telegram_id }}</code></td>
            <td class="font-mono">{{ fmtMoney(u.balance_usd) }}</td>
            <td class="font-mono text-slate-400">{{ fmtMoney(u.credit_limit_usd) }}</td>
            <td>
              <span :class="statusBadgeClass(u.status)">
                {{ u.status === "banned" ? "مسدود" : "فعال" }}
              </span>
              <span v-if="u.role !== 'user'" class="badge badge-warn ms-1">
                {{ u.role }}
              </span>
            </td>
            <td class="text-slate-500 text-xs">{{ fmtRelativeTime(u.last_seen_at) }}</td>
          </tr>
          <tr v-if="!items.length && !loading">
            <td colspan="6" class="text-center text-slate-500 py-8">
              کاربری پیدا نشد.
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Pagination -->
    <div class="flex items-center justify-between mt-4">
      <div class="text-xs text-slate-500">
        صفحه {{ page }} از {{ totalPages }}
      </div>
      <div class="flex gap-2">
        <button
          class="btn btn-secondary"
          :disabled="page <= 1 || loading"
          @click="page = Math.max(1, page - 1)"
        >قبلی</button>
        <button
          class="btn btn-secondary"
          :disabled="page >= totalPages || loading"
          @click="page = Math.min(totalPages, page + 1)"
        >بعدی</button>
      </div>
    </div>
  </div>
</template>
