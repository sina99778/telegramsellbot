<script setup lang="ts">
import { ref, onMounted, computed } from "vue";
import {
  listDiscounts,
  createDiscount,
  updateDiscount,
  deleteDiscount,
  type DiscountItem,
} from "@/api/discounts";
import { ApiError } from "@/api/client";
import { fmtNumber } from "@/utils/format";

const items = ref<DiscountItem[]>([]);
const loading = ref(true);
const errorMsg = ref<string | null>(null);
const banner = ref<string | null>(null);
const bannerTone = ref<"ok" | "warn">("ok");
const busyId = ref<string>("");

const showCreate = ref(false);
const createBusy = ref(false);
const createForm = ref({
  code: "",
  discount_percent: 10,
  max_uses: 1,
  expires_at: "",
});

const editing = ref<DiscountItem | null>(null);
const editBusy = ref(false);
const editForm = ref({
  discount_percent: 10,
  max_uses: 1,
  is_active: true,
  expires_at: "",
});

async function refresh() {
  loading.value = true;
  errorMsg.value = null;
  try {
    const r = await listDiscounts();
    items.value = r.items;
  } catch (exc) {
    errorMsg.value = exc instanceof ApiError ? exc.detail : "خطا";
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

const activeCount = computed(() => items.value.filter((d) => d.is_active).length);
const totalRedemptions = computed(() => items.value.reduce((s, d) => s + d.used_count, 0));

function fmtDate(s: string | null): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleString("fa-IR", { dateStyle: "short", timeStyle: "short" });
  } catch {
    return s;
  }
}

function isExpired(d: DiscountItem): boolean {
  if (!d.expires_at) return false;
  try {
    return new Date(d.expires_at).getTime() < Date.now();
  } catch {
    return false;
  }
}

async function doCreate() {
  if (!createForm.value.code.trim()) {
    flash("کد تخفیف خالی است.", "warn");
    return;
  }
  createBusy.value = true;
  try {
    await createDiscount({
      code: createForm.value.code.trim(),
      discount_percent: createForm.value.discount_percent,
      max_uses: createForm.value.max_uses,
      expires_at: createForm.value.expires_at || null,
    });
    flash("کد تخفیف اضافه شد.");
    showCreate.value = false;
    createForm.value = { code: "", discount_percent: 10, max_uses: 1, expires_at: "" };
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    createBusy.value = false;
  }
}

function openEdit(d: DiscountItem) {
  editing.value = d;
  editForm.value = {
    discount_percent: d.discount_percent,
    max_uses: d.max_uses,
    is_active: d.is_active,
    expires_at: d.expires_at ? d.expires_at.slice(0, 16) : "",
  };
}

async function doEdit() {
  if (!editing.value) return;
  editBusy.value = true;
  try {
    await updateDiscount(editing.value.id, {
      discount_percent: editForm.value.discount_percent,
      max_uses: editForm.value.max_uses,
      is_active: editForm.value.is_active,
      expires_at: editForm.value.expires_at || "",
    });
    flash("کد تخفیف به‌روزرسانی شد.");
    editing.value = null;
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    editBusy.value = false;
  }
}

async function doToggle(d: DiscountItem) {
  busyId.value = d.id;
  try {
    await updateDiscount(d.id, { is_active: !d.is_active });
    flash(d.is_active ? "کد غیرفعال شد." : "کد فعال شد.");
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    busyId.value = "";
  }
}

async function doDelete(d: DiscountItem) {
  if (!confirm(`«${d.code}» حذف شود؟`)) return;
  busyId.value = d.id;
  try {
    await deleteDiscount(d.id);
    flash("کد تخفیف حذف شد.");
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
        <h1 class="text-2xl font-bold text-white">کدهای تخفیف</h1>
        <p class="text-sm text-slate-400 mt-1">
          {{ activeCount }} کد فعال — {{ fmtNumber(totalRedemptions) }} بار مصرف‌شده
        </p>
      </div>
      <div class="flex gap-2">
        <button class="btn btn-secondary" @click="refresh" :disabled="loading">به‌روزرسانی</button>
        <button class="btn btn-primary" @click="showCreate = true">+ کد جدید</button>
      </div>
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

    <div v-if="loading" class="card animate-pulse h-32" />

    <div v-else class="card overflow-hidden p-0">
      <table class="data-table">
        <thead>
          <tr>
            <th>کد</th>
            <th>درصد</th>
            <th>مصرف</th>
            <th>انقضا</th>
            <th>وضعیت</th>
            <th class="text-end">عملیات</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="d in items" :key="d.id">
            <td>
              <code class="text-white font-mono">{{ d.code }}</code>
            </td>
            <td class="font-mono">{{ d.discount_percent }}%</td>
            <td class="font-mono">{{ fmtNumber(d.used_count) }} / {{ fmtNumber(d.max_uses) }}</td>
            <td class="text-xs">
              <span :class="isExpired(d) ? 'text-rose-400' : 'text-slate-300'">
                {{ fmtDate(d.expires_at) }}
              </span>
            </td>
            <td>
              <span v-if="isExpired(d)" class="badge badge-danger">منقضی</span>
              <span v-else-if="d.is_active" class="badge badge-success">فعال</span>
              <span v-else class="badge badge-muted">غیرفعال</span>
            </td>
            <td class="text-end whitespace-nowrap">
              <button class="btn btn-ghost btn-sm" :disabled="busyId === d.id" @click="openEdit(d)">ویرایش</button>
              <button class="btn btn-ghost btn-sm" :disabled="busyId === d.id" @click="doToggle(d)">
                {{ d.is_active ? "غیرفعال" : "فعال" }}
              </button>
              <button class="btn btn-ghost btn-sm text-rose-300" :disabled="busyId === d.id" @click="doDelete(d)">حذف</button>
            </td>
          </tr>
          <tr v-if="!items.length">
            <td colspan="6" class="text-center text-slate-500 py-8">کد تخفیفی ثبت نشده.</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ── Create dialog ─────────────────────────────────────────── -->
    <div
      v-if="showCreate"
      class="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4"
      @click.self="showCreate = false"
    >
      <div class="card w-full max-w-md space-y-3">
        <h3 class="text-lg font-bold text-white">کد تخفیف جدید</h3>
        <div>
          <label class="label">کد</label>
          <input v-model="createForm.code" class="input font-mono uppercase" placeholder="SUMMER2026" />
        </div>
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="label">درصد تخفیف</label>
            <input v-model.number="createForm.discount_percent" class="input" type="number" min="1" max="100" />
          </div>
          <div>
            <label class="label">حداکثر تعداد مصرف</label>
            <input v-model.number="createForm.max_uses" class="input" type="number" min="1" />
          </div>
        </div>
        <div>
          <label class="label">تاریخ انقضا (اختیاری)</label>
          <input v-model="createForm.expires_at" class="input" type="datetime-local" />
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button class="btn btn-secondary" :disabled="createBusy" @click="showCreate = false">انصراف</button>
          <button class="btn btn-primary" :disabled="createBusy" @click="doCreate">
            {{ createBusy ? "..." : "افزودن" }}
          </button>
        </div>
      </div>
    </div>

    <!-- ── Edit dialog ───────────────────────────────────────────── -->
    <div
      v-if="editing"
      class="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4"
      @click.self="editing = null"
    >
      <div class="card w-full max-w-md space-y-3">
        <h3 class="text-lg font-bold text-white">
          ویرایش — <code class="font-mono">{{ editing.code }}</code>
        </h3>
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="label">درصد تخفیف</label>
            <input v-model.number="editForm.discount_percent" class="input" type="number" min="1" max="100" />
          </div>
          <div>
            <label class="label">حداکثر مصرف</label>
            <input v-model.number="editForm.max_uses" class="input" type="number" min="1" />
          </div>
        </div>
        <div>
          <label class="label">تاریخ انقضا</label>
          <input v-model="editForm.expires_at" class="input" type="datetime-local" />
        </div>
        <div class="flex items-center gap-2">
          <input id="dc_active" v-model="editForm.is_active" type="checkbox" class="w-4 h-4" />
          <label for="dc_active" class="text-sm text-slate-200">فعال</label>
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button class="btn btn-secondary" :disabled="editBusy" @click="editing = null">انصراف</button>
          <button class="btn btn-primary" :disabled="editBusy" @click="doEdit">
            {{ editBusy ? "..." : "ذخیره" }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
