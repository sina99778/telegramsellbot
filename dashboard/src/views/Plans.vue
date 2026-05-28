<script setup lang="ts">
import { ref, onMounted, computed } from "vue";
import {
  listPlans,
  listInboundOptions,
  createPlan,
  updatePlan,
  deletePlan,
  type PlanItem,
  type InboundOption,
} from "@/api/plans";
import { ApiError } from "@/api/client";
import { fmtNumber } from "@/utils/format";

const items = ref<PlanItem[]>([]);
const inbounds = ref<InboundOption[]>([]);
const loading = ref(true);
const errorMsg = ref<string | null>(null);
const banner = ref<string | null>(null);
const bannerTone = ref<"ok" | "warn">("ok");
const busyId = ref<string>("");

const showCreate = ref(false);
const createBusy = ref(false);
const createForm = ref({
  name: "",
  protocol: "vless",
  inbound_id: "" as string,
  duration_days: 30,
  volume_gb: 30,
  price: 5,
  renewal_price: 5,
  currency: "USD",
  // Per-plan overrides — empty string means "use global default".
  ip_limit: "" as string | number,
  renewal_price_per_gb: "" as string | number,
  renewal_price_per_day: "" as string | number,
});

const editing = ref<PlanItem | null>(null);
const editBusy = ref(false);
const editForm = ref({
  name: "",
  protocol: "vless",
  inbound_id: "" as string,
  duration_days: 30,
  volume_gb: 30,
  price: 5,
  renewal_price: 5,
  currency: "USD",
  is_active: true,
  ip_limit: "" as string | number,
  renewal_price_per_gb: "" as string | number,
  renewal_price_per_day: "" as string | number,
});

async function refresh() {
  loading.value = true;
  errorMsg.value = null;
  try {
    const [r, ib] = await Promise.all([listPlans(), listInboundOptions()]);
    items.value = r.items;
    inbounds.value = ib.items;
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

const activeCount = computed(() => items.value.filter((p) => p.is_active).length);
const totalSubs = computed(() => items.value.reduce((s, p) => s + p.subscription_count, 0));

// "" / null in the form means "leave the override unset (fall back to global)".
// Anything numeric — including 0 — is taken at face value.
function _numOrNull(v: string | number): number | null {
  if (v === "" || v === null || v === undefined) return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

async function doCreate() {
  if (!createForm.value.name.trim()) {
    flash("نام پلن خالی است.", "warn");
    return;
  }
  createBusy.value = true;
  try {
    await createPlan({
      name: createForm.value.name.trim(),
      protocol: createForm.value.protocol,
      inbound_id: createForm.value.inbound_id || null,
      duration_days: createForm.value.duration_days,
      volume_gb: createForm.value.volume_gb,
      price: createForm.value.price,
      renewal_price: createForm.value.renewal_price,
      currency: createForm.value.currency,
      ip_limit: _numOrNull(createForm.value.ip_limit),
      renewal_price_per_gb: _numOrNull(createForm.value.renewal_price_per_gb),
      renewal_price_per_day: _numOrNull(createForm.value.renewal_price_per_day),
    });
    flash("پلن جدید اضافه شد.");
    showCreate.value = false;
    createForm.value = {
      name: "", protocol: "vless", inbound_id: "",
      duration_days: 30, volume_gb: 30, price: 5, renewal_price: 5, currency: "USD",
      ip_limit: "", renewal_price_per_gb: "", renewal_price_per_day: "",
    };
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    createBusy.value = false;
  }
}

function openEdit(p: PlanItem) {
  editing.value = p;
  editForm.value = {
    name: p.name,
    protocol: p.protocol,
    inbound_id: p.inbound_id || "",
    duration_days: p.duration_days,
    volume_gb: p.volume_gb,
    price: p.price,
    renewal_price: p.renewal_price,
    currency: p.currency,
    is_active: p.is_active,
    ip_limit: p.ip_limit ?? "",
    renewal_price_per_gb: p.renewal_price_per_gb ?? "",
    renewal_price_per_day: p.renewal_price_per_day ?? "",
  };
}

// Map a form field back to the PATCH payload. Empty string in the form
// means "unset" -> send -1 (the backend's "clear" sentinel). A number
// is sent as-is. Same convention applies for ip_limit & both renewal
// pricing overrides.
function _formToPatchOverride(v: string | number): number {
  if (v === "" || v === null || v === undefined) return -1;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : -1;
}

async function doEdit() {
  if (!editing.value) return;
  editBusy.value = true;
  try {
    await updatePlan(editing.value.id, {
      name: editForm.value.name.trim(),
      protocol: editForm.value.protocol,
      inbound_id: editForm.value.inbound_id || null,
      duration_days: editForm.value.duration_days,
      volume_gb: editForm.value.volume_gb,
      price: editForm.value.price,
      renewal_price: editForm.value.renewal_price,
      currency: editForm.value.currency,
      is_active: editForm.value.is_active,
      ip_limit: _formToPatchOverride(editForm.value.ip_limit),
      renewal_price_per_gb: _formToPatchOverride(editForm.value.renewal_price_per_gb),
      renewal_price_per_day: _formToPatchOverride(editForm.value.renewal_price_per_day),
    });
    flash("پلن به‌روزرسانی شد.");
    editing.value = null;
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    editBusy.value = false;
  }
}

async function doToggle(p: PlanItem) {
  busyId.value = p.id;
  try {
    await updatePlan(p.id, { is_active: !p.is_active });
    flash(p.is_active ? "پلن غیرفعال شد." : "پلن فعال شد.");
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    busyId.value = "";
  }
}

async function doDelete(p: PlanItem) {
  if (p.subscription_count > 0) {
    flash(`این پلن ${p.subscription_count} سرویس دارد — به‌جای حذف، آن را غیرفعال کن.`, "warn");
    return;
  }
  if (!confirm(`«${p.name}» حذف شود؟`)) return;
  busyId.value = p.id;
  try {
    await deletePlan(p.id);
    flash("پلن حذف شد.");
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
        <h1 class="text-2xl font-bold text-white">مدیریت پلن‌ها</h1>
        <p class="text-sm text-slate-400 mt-1">
          {{ activeCount }} پلن فعال — مجموعاً {{ fmtNumber(totalSubs) }} سرویس فروخته‌شده
        </p>
      </div>
      <div class="flex gap-2">
        <button class="btn btn-secondary" @click="refresh" :disabled="loading">به‌روزرسانی</button>
        <button class="btn btn-primary" @click="showCreate = true">+ پلن جدید</button>
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
            <th>نام</th>
            <th>پروتکل</th>
            <th>مدت</th>
            <th>حجم</th>
            <th>قیمت / تمدید</th>
            <th>اینباند</th>
            <th>فروش</th>
            <th>وضعیت</th>
            <th class="text-end">عملیات</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in items" :key="p.id">
            <td>
              <div class="text-white font-medium">{{ p.name }}</div>
              <div class="text-[11px] text-slate-500 font-mono">{{ p.code }}</div>
              <div class="text-[11px] mt-1">
                <span v-if="p.ip_limit !== null" class="badge badge-info">
                  IP: {{ p.ip_limit === 0 ? "∞" : p.ip_limit }}
                </span>
                <span v-else class="text-slate-500">IP: عمومی</span>
              </div>
            </td>
            <td class="font-mono text-xs uppercase">{{ p.protocol }}</td>
            <td>{{ p.duration_days }} روز</td>
            <td class="font-mono">{{ p.volume_gb.toFixed(0) }} GB</td>
            <td>
              <div class="font-mono">{{ p.price.toFixed(2) }} {{ p.currency }}</div>
              <div class="text-[11px] text-slate-500 font-mono space-y-0.5">
                <div>تمدید کامل: {{ p.renewal_price.toFixed(2) }}</div>
                <div v-if="p.renewal_price_per_gb !== null">
                  <span class="text-sky-300">گیگ: {{ p.renewal_price_per_gb.toFixed(2) }}</span>
                </div>
                <div v-if="p.renewal_price_per_day !== null">
                  <span class="text-sky-300">روز: {{ p.renewal_price_per_day.toFixed(2) }}</span>
                </div>
              </div>
            </td>
            <td class="text-xs">
              <div v-if="p.inbound_label" class="text-slate-300">{{ p.inbound_label }}</div>
              <div v-else class="text-slate-500">—</div>
              <div v-if="p.server_name" class="text-[11px] text-slate-500">{{ p.server_name }}</div>
            </td>
            <td class="font-mono">{{ fmtNumber(p.subscription_count) }}</td>
            <td>
              <span :class="p.is_active ? 'badge badge-success' : 'badge badge-muted'">
                {{ p.is_active ? "فعال" : "غیرفعال" }}
              </span>
            </td>
            <td class="text-end whitespace-nowrap">
              <button class="btn btn-ghost btn-sm" :disabled="busyId === p.id" @click="openEdit(p)">ویرایش</button>
              <button class="btn btn-ghost btn-sm" :disabled="busyId === p.id" @click="doToggle(p)">
                {{ p.is_active ? "غیرفعال" : "فعال" }}
              </button>
              <button class="btn btn-ghost btn-sm text-rose-300" :disabled="busyId === p.id" @click="doDelete(p)">حذف</button>
            </td>
          </tr>
          <tr v-if="!items.length">
            <td colspan="9" class="text-center text-slate-500 py-8">پلنی ثبت نشده.</td>
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
      <div class="card w-full max-w-2xl space-y-3">
        <h3 class="text-lg font-bold text-white">افزودن پلن جدید</h3>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div class="md:col-span-2">
            <label class="label">نام پلن</label>
            <input v-model="createForm.name" class="input" placeholder="مثلاً ۳۰ گیگ یک‌ماهه" />
          </div>
          <div>
            <label class="label">پروتکل</label>
            <select v-model="createForm.protocol" class="input">
              <option value="vless">vless</option>
              <option value="vmess">vmess</option>
              <option value="trojan">trojan</option>
              <option value="shadowsocks">shadowsocks</option>
            </select>
          </div>
          <div>
            <label class="label">اینباند پیش‌فرض (اختیاری)</label>
            <select v-model="createForm.inbound_id" class="input">
              <option value="">— هیچ‌کدام —</option>
              <option v-for="ib in inbounds" :key="ib.id" :value="ib.id">{{ ib.label }}</option>
            </select>
          </div>
          <div>
            <label class="label">مدت (روز)</label>
            <input v-model.number="createForm.duration_days" class="input" type="number" min="1" />
          </div>
          <div>
            <label class="label">حجم (GB)</label>
            <input v-model.number="createForm.volume_gb" class="input" type="number" min="0" step="1" />
          </div>
          <div>
            <label class="label">قیمت فروش</label>
            <input v-model.number="createForm.price" class="input" type="number" min="0" step="0.01" />
          </div>
          <div>
            <label class="label">قیمت تمدید</label>
            <input v-model.number="createForm.renewal_price" class="input" type="number" min="0" step="0.01" />
          </div>
          <div>
            <label class="label">واحد ارز</label>
            <select v-model="createForm.currency" class="input">
              <option value="USD">USD</option>
              <option value="IRR">IRR (تومان)</option>
            </select>
          </div>
        </div>

        <div class="border-t border-bg-border pt-3 mt-2">
          <div class="text-[12px] text-slate-400 mb-2">
            موارد زیر اختیاری‌اند — خالی بگذار تا از پیش‌فرض عمومی استفاده شود.
          </div>
          <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
            <div>
              <label class="label">محدودیت آی‌پی (هم‌زمان)</label>
              <input v-model="createForm.ip_limit" class="input" type="number" min="0" placeholder="پیش‌فرض" />
              <div class="text-[10px] text-slate-500 mt-1">۰ = نامحدود (طبق قرارداد X-UI)</div>
            </div>
            <div>
              <label class="label">قیمت تمدید هر گیگ</label>
              <input v-model="createForm.renewal_price_per_gb" class="input" type="number" min="0" step="0.01" placeholder="پیش‌فرض" />
            </div>
            <div>
              <label class="label">قیمت تمدید هر روز</label>
              <input v-model="createForm.renewal_price_per_day" class="input" type="number" min="0" step="0.01" placeholder="پیش‌فرض" />
            </div>
          </div>
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
      <div class="card w-full max-w-2xl space-y-3">
        <h3 class="text-lg font-bold text-white">ویرایش پلن — {{ editing.name }}</h3>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div class="md:col-span-2">
            <label class="label">نام پلن</label>
            <input v-model="editForm.name" class="input" />
          </div>
          <div>
            <label class="label">پروتکل</label>
            <select v-model="editForm.protocol" class="input">
              <option value="vless">vless</option>
              <option value="vmess">vmess</option>
              <option value="trojan">trojan</option>
              <option value="shadowsocks">shadowsocks</option>
            </select>
          </div>
          <div>
            <label class="label">
              اینباند
              <span v-if="editing.subscription_count > 0" class="text-[10px] text-amber-300">
                (به‌خاطر {{ editing.subscription_count }} سرویس فعال، تغییر غیرفعال است)
              </span>
            </label>
            <select v-model="editForm.inbound_id" class="input" :disabled="editing.subscription_count > 0">
              <option value="">— هیچ‌کدام —</option>
              <option v-for="ib in inbounds" :key="ib.id" :value="ib.id">{{ ib.label }}</option>
            </select>
          </div>
          <div>
            <label class="label">مدت (روز)</label>
            <input v-model.number="editForm.duration_days" class="input" type="number" min="1" />
          </div>
          <div>
            <label class="label">حجم (GB)</label>
            <input v-model.number="editForm.volume_gb" class="input" type="number" min="0" step="1" />
          </div>
          <div>
            <label class="label">قیمت فروش</label>
            <input v-model.number="editForm.price" class="input" type="number" min="0" step="0.01" />
          </div>
          <div>
            <label class="label">قیمت تمدید</label>
            <input v-model.number="editForm.renewal_price" class="input" type="number" min="0" step="0.01" />
          </div>
          <div>
            <label class="label">واحد ارز</label>
            <select v-model="editForm.currency" class="input">
              <option value="USD">USD</option>
              <option value="IRR">IRR (تومان)</option>
            </select>
          </div>
          <div class="flex items-center gap-2">
            <input id="ed_active" v-model="editForm.is_active" type="checkbox" class="w-4 h-4" />
            <label for="ed_active" class="text-sm text-slate-200">فعال</label>
          </div>
        </div>

        <div class="border-t border-bg-border pt-3 mt-2">
          <div class="text-[12px] text-slate-400 mb-2">
            خالی بگذار تا از پیش‌فرض عمومی استفاده شود (در «تنظیمات»).
          </div>
          <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
            <div>
              <label class="label">محدودیت آی‌پی</label>
              <input v-model="editForm.ip_limit" class="input" type="number" min="0" placeholder="پیش‌فرض" />
              <div class="text-[10px] text-slate-500 mt-1">۰ = نامحدود</div>
            </div>
            <div>
              <label class="label">قیمت تمدید هر گیگ</label>
              <input v-model="editForm.renewal_price_per_gb" class="input" type="number" min="0" step="0.01" placeholder="پیش‌فرض" />
            </div>
            <div>
              <label class="label">قیمت تمدید هر روز</label>
              <input v-model="editForm.renewal_price_per_day" class="input" type="number" min="0" step="0.01" placeholder="پیش‌فرض" />
            </div>
          </div>
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
