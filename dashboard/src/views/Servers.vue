<script setup lang="ts">
import { ref, onMounted, computed } from "vue";
import {
  listServers,
  createServer,
  updateServer,
  deleteServer,
  testServer,
  getServer,
  type ServerListItem,
  type ServerDetail,
} from "@/api/servers";
import { ApiError } from "@/api/client";
import { fmtNumber } from "@/utils/format";

const items = ref<ServerListItem[]>([]);
const loading = ref(true);
const errorMsg = ref<string | null>(null);
const banner = ref<string | null>(null);
const bannerTone = ref<"ok" | "warn">("ok");
const busyId = ref<string>(""); // server id of an in-flight action

// ── Create-server dialog state ──────────────────────────────────────
const showCreate = ref(false);
const createForm = ref({
  name: "",
  base_url: "",
  panel_username: "",
  panel_password: "",
  config_domain: "",
  sub_domain: "",
  subscription_port: 2096,
  priority: 100,
});
const createBusy = ref(false);

// ── Detail drawer state ────────────────────────────────────────────
const drawer = ref<ServerDetail | null>(null);
const drawerLoading = ref(false);

// ── Edit dialog state ──────────────────────────────────────────────
const editing = ref<ServerListItem | null>(null);
const editBusy = ref(false);
const editForm = ref({
  name: "",
  base_url: "",
  panel_username: "",
  panel_password: "",
  config_domain: "",
  sub_domain: "",
  subscription_port: 2096,
  max_clients: 0,
  priority: 100,
  is_active: true,
});

async function refresh() {
  loading.value = true;
  errorMsg.value = null;
  try {
    const r = await listServers();
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

async function doTest(s: ServerListItem) {
  busyId.value = s.id;
  try {
    const r = await testServer(s.id);
    if (r.ok) {
      flash(`✅ اتصال موفق — ${r.inbound_count} اینباند`);
      refresh();
    } else {
      flash(`❌ ${r.error || "خطا"}`, "warn");
      refresh();
    }
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    busyId.value = "";
  }
}

async function doToggle(s: ServerListItem) {
  busyId.value = s.id;
  try {
    await updateServer(s.id, { is_active: !s.is_active });
    flash(s.is_active ? "سرور غیرفعال شد." : "سرور فعال شد.");
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    busyId.value = "";
  }
}

async function doDelete(s: ServerListItem) {
  if (!confirm(`«${s.name}» حذف شود؟ این عملیات قابل بازگشت نیست.`)) return;
  busyId.value = s.id;
  try {
    await deleteServer(s.id);
    flash("سرور حذف شد.");
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    busyId.value = "";
  }
}

async function doCreate() {
  if (!createForm.value.name || !createForm.value.base_url
      || !createForm.value.panel_username || !createForm.value.panel_password) {
    flash("نام، URL، یوزرنیم و رمز همگی الزامی‌اند.", "warn");
    return;
  }
  createBusy.value = true;
  try {
    await createServer({
      name: createForm.value.name.trim(),
      base_url: createForm.value.base_url.trim(),
      panel_username: createForm.value.panel_username,
      panel_password: createForm.value.panel_password,
      config_domain: createForm.value.config_domain || null,
      sub_domain: createForm.value.sub_domain || null,
      subscription_port: createForm.value.subscription_port,
      priority: createForm.value.priority,
    });
    flash("سرور اضافه شد.");
    showCreate.value = false;
    createForm.value = {
      name: "", base_url: "", panel_username: "", panel_password: "",
      config_domain: "", sub_domain: "", subscription_port: 2096, priority: 100,
    };
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    createBusy.value = false;
  }
}

async function openDrawer(s: ServerListItem) {
  drawerLoading.value = true;
  drawer.value = null;
  try {
    drawer.value = await getServer(s.id);
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    drawerLoading.value = false;
  }
}

async function openEdit(s: ServerListItem) {
  // Pull the full detail so credentials_username is available; the list
  // payload doesn't include the X-UI username.
  editBusy.value = true;
  try {
    const detail = await getServer(s.id);
    editing.value = s;
    editForm.value = {
      name: detail.server.name,
      base_url: detail.server.base_url,
      panel_username: detail.server.credentials_username || "",
      // Password is never returned from the API — operator types a new
      // one only if they want to rotate it.
      panel_password: "",
      config_domain: detail.server.config_domain || "",
      sub_domain: detail.server.sub_domain || "",
      subscription_port: detail.server.subscription_port || 2096,
      max_clients: detail.server.max_clients ?? 0,
      priority: detail.server.priority ?? 100,
      is_active: detail.server.is_active,
    };
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    editBusy.value = false;
  }
}

async function doEdit() {
  if (!editing.value) return;
  if (!editForm.value.name.trim() || !editForm.value.base_url.trim()) {
    flash("نام و URL سرور الزامی‌اند.", "warn");
    return;
  }
  editBusy.value = true;
  try {
    // PATCH only the fields that differ — but the API treats `null` as
    // "don't touch", so we send everything and let SQLAlchemy be the
    // tiebreaker. Password is sent only if the operator typed a new one.
    const body: any = {
      name: editForm.value.name.trim(),
      base_url: editForm.value.base_url.trim(),
      panel_username: editForm.value.panel_username.trim() || undefined,
      config_domain: editForm.value.config_domain.trim() || null,
      sub_domain: editForm.value.sub_domain.trim() || null,
      subscription_port: editForm.value.subscription_port,
      max_clients: editForm.value.max_clients > 0 ? editForm.value.max_clients : null,
      priority: editForm.value.priority,
      is_active: editForm.value.is_active,
    };
    if (editForm.value.panel_password.trim()) {
      body.panel_password = editForm.value.panel_password.trim();
    }
    await updateServer(editing.value.id, body);
    flash("سرور به‌روزرسانی شد.");
    editing.value = null;
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    editBusy.value = false;
  }
}

function healthBadgeClass(s: string): string {
  if (s === "ok") return "badge badge-success";
  if (s === "error") return "badge badge-danger";
  return "badge badge-muted";
}

function healthLabel(s: string): string {
  return s === "ok" ? "سالم" : s === "error" ? "خطا" : "نامشخص";
}
</script>

<template>
  <div class="p-6 lg:p-8 max-w-7xl mx-auto">
    <header class="flex flex-wrap items-end justify-between gap-3 mb-6">
      <div>
        <h1 class="text-2xl font-bold text-white">مدیریت سرورها</h1>
        <p class="text-sm text-slate-400 mt-1">پنل‌های X-UI، اینباندها، تست اتصال.</p>
      </div>
      <div class="flex gap-2">
        <button class="btn btn-secondary" @click="refresh" :disabled="loading">به‌روزرسانی</button>
        <button class="btn btn-primary" @click="showCreate = true">+ افزودن سرور</button>
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
            <th>سرور</th>
            <th>URL</th>
            <th>وضعیت</th>
            <th>سلامت</th>
            <th>اینباند</th>
            <th>کلاینت</th>
            <th class="text-end">عملیات</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in items" :key="s.id">
            <td>
              <button class="text-white hover:text-accent font-medium" @click="openDrawer(s)">
                {{ s.name }}
              </button>
              <div class="text-[11px] text-slate-500">priority: {{ s.priority }}</div>
            </td>
            <td>
              <code class="text-slate-300 break-all">{{ s.base_url }}</code>
            </td>
            <td>
              <span :class="s.is_active ? 'badge badge-success' : 'badge badge-muted'">
                {{ s.is_active ? "فعال" : "غیرفعال" }}
              </span>
            </td>
            <td>
              <span :class="healthBadgeClass(s.health_status)">
                {{ healthLabel(s.health_status) }}
              </span>
            </td>
            <td class="font-mono">{{ s.active_inbound_count }}/{{ s.inbound_count }}</td>
            <td class="font-mono">{{ fmtNumber(s.client_count) }}</td>
            <td class="text-end whitespace-nowrap">
              <button class="btn btn-ghost btn-sm" :disabled="busyId === s.id" @click="openEdit(s)">ویرایش</button>
              <button class="btn btn-ghost btn-sm" :disabled="busyId === s.id" @click="doTest(s)">تست</button>
              <button class="btn btn-ghost btn-sm" :disabled="busyId === s.id" @click="doToggle(s)">
                {{ s.is_active ? "غیرفعال" : "فعال" }}
              </button>
              <button class="btn btn-ghost btn-sm text-rose-300" :disabled="busyId === s.id" @click="doDelete(s)">حذف</button>
            </td>
          </tr>
          <tr v-if="!items.length">
            <td colspan="7" class="text-center text-slate-500 py-8">سروری ثبت نشده.</td>
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
      <div class="card w-full max-w-lg space-y-3">
        <h3 class="text-lg font-bold text-white">افزودن سرور X-UI</h3>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div class="md:col-span-2">
            <label class="label">نام دلخواه</label>
            <input v-model="createForm.name" class="input" placeholder="مثلاً Frankfurt-1" />
          </div>
          <div class="md:col-span-2">
            <label class="label">آدرس پنل (Base URL)</label>
            <input v-model="createForm.base_url" class="input" placeholder="https://panel.example.com:2053" />
          </div>
          <div>
            <label class="label">نام کاربری پنل</label>
            <input v-model="createForm.panel_username" class="input" />
          </div>
          <div>
            <label class="label">رمز پنل</label>
            <input v-model="createForm.panel_password" class="input" type="password" />
          </div>
          <div>
            <label class="label">دامنه‌ی کانفیگ (اختیاری)</label>
            <input v-model="createForm.config_domain" class="input" placeholder="proxy.example.com" />
          </div>
          <div>
            <label class="label">دامنه‌ی sub-link (اختیاری)</label>
            <input v-model="createForm.sub_domain" class="input" placeholder="sub.example.com" />
          </div>
          <div>
            <label class="label">پورت sub</label>
            <input v-model.number="createForm.subscription_port" class="input" type="number" min="1" max="65535" />
          </div>
          <div>
            <label class="label">اولویت</label>
            <input v-model.number="createForm.priority" class="input" type="number" min="0" />
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
      <div class="card w-full max-w-lg space-y-3 max-h-[90vh] overflow-y-auto">
        <h3 class="text-lg font-bold text-white">ویرایش سرور — {{ editing.name }}</h3>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div class="md:col-span-2">
            <label class="label">نام دلخواه</label>
            <input v-model="editForm.name" class="input" />
          </div>
          <div class="md:col-span-2">
            <label class="label">آدرس پنل (Base URL)</label>
            <input v-model="editForm.base_url" class="input ltr font-mono text-xs" />
          </div>
          <div>
            <label class="label">نام کاربری پنل</label>
            <input v-model="editForm.panel_username" class="input ltr" />
          </div>
          <div>
            <label class="label">
              رمز پنل
              <span class="text-[10px] text-slate-500">(فقط اگه می‌خوای عوض کنی)</span>
            </label>
            <input v-model="editForm.panel_password" class="input" type="password" placeholder="بدون تغییر" />
          </div>
          <div>
            <label class="label">دامنه‌ی کانفیگ</label>
            <input v-model="editForm.config_domain" class="input" placeholder="proxy.example.com" />
          </div>
          <div>
            <label class="label">دامنه‌ی sub-link</label>
            <input v-model="editForm.sub_domain" class="input" placeholder="sub.example.com" />
          </div>
          <div>
            <label class="label">پورت sub</label>
            <input v-model.number="editForm.subscription_port" class="input" type="number" min="1" max="65535" />
          </div>
          <div>
            <label class="label">حداکثر کلاینت</label>
            <input v-model.number="editForm.max_clients" class="input" type="number" min="0" placeholder="0 = نامحدود" />
          </div>
          <div>
            <label class="label">اولویت</label>
            <input v-model.number="editForm.priority" class="input" type="number" min="0" />
          </div>
          <div class="flex items-center gap-2">
            <input id="sv_active" v-model="editForm.is_active" type="checkbox" class="w-4 h-4" />
            <label for="sv_active" class="text-sm text-slate-200">فعال</label>
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

    <!-- ── Detail drawer ─────────────────────────────────────────── -->
    <div
      v-if="drawer || drawerLoading"
      class="fixed inset-0 bg-black/60 backdrop-blur-sm z-40 flex items-center justify-center p-4"
      @click.self="drawer = null"
    >
      <div class="card w-full max-w-2xl max-h-[80vh] overflow-y-auto">
        <div class="flex justify-between items-center mb-3">
          <h3 class="text-lg font-bold text-white">جزئیات سرور</h3>
          <button class="btn btn-ghost" @click="drawer = null">×</button>
        </div>
        <div v-if="drawerLoading" class="animate-pulse h-40" />
        <div v-else-if="drawer">
          <div class="text-xl font-bold text-white">{{ drawer.server.name }}</div>
          <code class="text-sm text-slate-300 break-all">{{ drawer.server.base_url }}</code>
          <div class="mt-4 grid grid-cols-2 gap-3 text-sm">
            <div>
              <div class="text-[11px] text-slate-500">سلامت</div>
              <div><span :class="healthBadgeClass(drawer.server.health_status)">{{ healthLabel(drawer.server.health_status) }}</span></div>
            </div>
            <div>
              <div class="text-[11px] text-slate-500">وضعیت</div>
              <div><span :class="drawer.server.is_active ? 'badge badge-success' : 'badge badge-muted'">{{ drawer.server.is_active ? 'فعال' : 'غیرفعال' }}</span></div>
            </div>
            <div>
              <div class="text-[11px] text-slate-500">یوزرنیم پنل</div>
              <div class="text-slate-300 font-mono">{{ drawer.server.credentials_username || "—" }}</div>
            </div>
            <div>
              <div class="text-[11px] text-slate-500">اولویت</div>
              <div class="text-slate-300">{{ drawer.server.priority }}</div>
            </div>
            <div>
              <div class="text-[11px] text-slate-500">دامنه‌ی کانفیگ</div>
              <div class="text-slate-300">{{ drawer.server.config_domain || "—" }}</div>
            </div>
            <div>
              <div class="text-[11px] text-slate-500">دامنه‌ی sub</div>
              <div class="text-slate-300">{{ drawer.server.sub_domain || "—" }}</div>
            </div>
          </div>

          <h4 class="text-sm font-bold text-white mt-5 mb-2">اینباندها ({{ drawer.inbounds.length }})</h4>
          <table class="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>نام</th>
                <th>پروتکل / پورت</th>
                <th>کلاینت</th>
                <th>وضعیت</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="ib in drawer.inbounds" :key="ib.id">
                <td class="font-mono">{{ ib.xui_inbound_remote_id }}</td>
                <td>{{ ib.remark || "—" }}</td>
                <td class="font-mono">{{ ib.protocol || "?" }}:{{ ib.port || "?" }}</td>
                <td>{{ ib.client_count }}</td>
                <td>
                  <span :class="ib.is_active ? 'badge badge-success' : 'badge badge-muted'">
                    {{ ib.is_active ? 'فعال' : 'غیرفعال' }}
                  </span>
                </td>
              </tr>
              <tr v-if="!drawer.inbounds.length">
                <td colspan="5" class="text-center text-slate-500 py-4">اینباندی ثبت نشده — تست سرور برای sync.</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>
