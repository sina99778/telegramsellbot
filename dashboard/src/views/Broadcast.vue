<script setup lang="ts">
import { ref, onMounted, computed } from "vue";
import {
  listBroadcasts,
  createBroadcast,
  type BroadcastJobItem,
} from "@/api/broadcast";
import { ApiError } from "@/api/client";
import { fmtNumber } from "@/utils/format";

const items = ref<BroadcastJobItem[]>([]);
const totalUsers = ref(0);
const loading = ref(true);
const errorMsg = ref<string | null>(null);
const banner = ref<string | null>(null);
const bannerTone = ref<"ok" | "warn">("ok");

const composing = ref(false);
const composeBusy = ref(false);
const composeForm = ref({
  text: "",
  audience: "all" as "all" | "active" | "inactive",
});

async function refresh() {
  loading.value = true;
  errorMsg.value = null;
  try {
    const r = await listBroadcasts();
    items.value = r.items;
    totalUsers.value = r.total_users;
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

function fmtDate(s: string | null): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleString("fa-IR", { dateStyle: "short", timeStyle: "short" });
  } catch {
    return s;
  }
}

function statusBadgeClass(s: string): string {
  if (s === "completed" || s === "finished") return "badge badge-success";
  if (s === "running" || s === "in_progress") return "badge badge-info";
  if (s === "failed") return "badge badge-danger";
  if (s === "queued" || s === "pending") return "badge badge-warn";
  return "badge badge-muted";
}

function statusLabel(s: string): string {
  const map: Record<string, string> = {
    queued: "در صف",
    pending: "در صف",
    running: "در حال ارسال",
    in_progress: "در حال ارسال",
    completed: "تکمیل‌شده",
    finished: "تکمیل‌شده",
    failed: "خطا",
  };
  return map[s] || s;
}

function progressPercent(j: BroadcastJobItem): number {
  if (!j.total) return 0;
  return Math.min(100, Math.round((j.processed / j.total) * 100));
}

const audienceLabel = computed(() => {
  if (composeForm.value.audience === "active") return "فقط کاربران دارای سرویس فعال";
  if (composeForm.value.audience === "inactive") return "فقط کاربرانِ بدون سرویس فعال";
  return `همه‌ی کاربران (${fmtNumber(totalUsers.value)} نفر)`;
});

async function doSend() {
  const txt = composeForm.value.text.trim();
  if (!txt) {
    flash("متن پیام خالی است.", "warn");
    return;
  }
  if (txt.length > 4000) {
    flash("متن طولانی‌تر از حد مجاز تلگرام است.", "warn");
    return;
  }
  if (!confirm(`پیام برای ${audienceLabel.value} ارسال شود؟`)) return;
  composeBusy.value = true;
  try {
    await createBroadcast({ text: txt, audience: composeForm.value.audience });
    flash("پیام در صف ارسال قرار گرفت — Worker طی ۲۰ ثانیه شروع می‌کند.");
    composing.value = false;
    composeForm.value = { text: "", audience: "all" };
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    composeBusy.value = false;
  }
}
</script>

<template>
  <div class="p-6 lg:p-8 max-w-7xl mx-auto">
    <header class="flex flex-wrap items-end justify-between gap-3 mb-6">
      <div>
        <h1 class="text-2xl font-bold text-white">پیام همگانی</h1>
        <p class="text-sm text-slate-400 mt-1">
          {{ fmtNumber(totalUsers) }} کاربر در دیتابیس — Worker هر ۲۰ ثانیه صف را پردازش می‌کند.
        </p>
      </div>
      <div class="flex gap-2">
        <button class="btn btn-secondary" @click="refresh" :disabled="loading">به‌روزرسانی</button>
        <button class="btn btn-primary" @click="composing = true">+ پیام جدید</button>
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
            <th>پیام</th>
            <th>منبع</th>
            <th>وضعیت</th>
            <th>پیشرفت</th>
            <th>تاریخ ایجاد</th>
            <th>پایان</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="j in items" :key="j.id">
            <td>
              <div class="text-slate-200 text-sm whitespace-pre-wrap max-w-md line-clamp-3">
                {{ j.text_preview || "—" }}
              </div>
              <div class="text-[11px] text-slate-500 mt-1">{{ j.message_type }}</div>
            </td>
            <td>
              <span :class="j.via === 'dashboard' ? 'badge badge-info' : 'badge badge-muted'">
                {{ j.via }}
              </span>
            </td>
            <td>
              <span :class="statusBadgeClass(j.status)">{{ statusLabel(j.status) }}</span>
              <div v-if="j.failed > 0" class="text-[11px] text-rose-400 mt-1">
                {{ fmtNumber(j.failed) }} ناموفق
              </div>
            </td>
            <td class="min-w-[160px]">
              <div class="text-xs font-mono text-slate-300 mb-1">
                {{ fmtNumber(j.processed) }} / {{ fmtNumber(j.total) }}
                ({{ progressPercent(j) }}%)
              </div>
              <div class="w-full h-1.5 bg-bg-elev rounded">
                <div
                  class="h-full bg-accent rounded transition-all"
                  :style="{ width: progressPercent(j) + '%' }"
                />
              </div>
            </td>
            <td class="text-xs text-slate-300">{{ fmtDate(j.created_at) }}</td>
            <td class="text-xs text-slate-300">{{ fmtDate(j.finished_at) }}</td>
          </tr>
          <tr v-if="!items.length">
            <td colspan="6" class="text-center text-slate-500 py-8">هنوز پیامی فرستاده نشده.</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ── Compose dialog ────────────────────────────────────────── -->
    <div
      v-if="composing"
      class="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4"
      @click.self="composing = false"
    >
      <div class="card w-full max-w-2xl space-y-3">
        <h3 class="text-lg font-bold text-white">ارسال پیام همگانی</h3>
        <div>
          <label class="label">مخاطب</label>
          <select v-model="composeForm.audience" class="input">
            <option value="all">همه‌ی کاربران</option>
            <option value="active">فقط کاربران دارای سرویس فعال</option>
            <option value="inactive">فقط کاربرانِ بدون سرویس فعال</option>
          </select>
          <div class="text-[11px] text-slate-400 mt-1">{{ audienceLabel }}</div>
        </div>
        <div>
          <label class="label">
            متن پیام
            <span class="text-[11px] text-slate-500 font-mono">
              ({{ composeForm.text.length }}/4000)
            </span>
          </label>
          <textarea
            v-model="composeForm.text"
            class="input min-h-[180px]"
            placeholder="متن پیام را اینجا بنویس..."
            maxlength="4000"
          />
          <div class="text-[11px] text-slate-500 mt-1">
            از HTML تلگرام پشتیبانی می‌شود: &lt;b&gt;، &lt;i&gt;، &lt;a href&gt;، &lt;code&gt;
          </div>
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button class="btn btn-secondary" :disabled="composeBusy" @click="composing = false">انصراف</button>
          <button class="btn btn-primary" :disabled="composeBusy" @click="doSend">
            {{ composeBusy ? "..." : "قرار دادن در صف ارسال" }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
