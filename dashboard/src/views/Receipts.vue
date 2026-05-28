<script setup lang="ts">
import { ref, onMounted, computed } from "vue";
import {
  listReceipts,
  getReceipt,
  approveReceipt,
  rejectReceipt,
  receiptPhotoUrl,
  type ReceiptListItem,
  type ReceiptDetail,
} from "@/api/receipts";
import { ApiError } from "@/api/client";
import { fmtNumber } from "@/utils/format";

const items = ref<ReceiptListItem[]>([]);
const loading = ref(true);
const errorMsg = ref<string | null>(null);
const banner = ref<string | null>(null);
const bannerTone = ref<"ok" | "warn">("ok");
const busyId = ref<string>("");

const drawer = ref<ReceiptDetail | null>(null);
const drawerLoading = ref(false);

async function refresh() {
  loading.value = true;
  errorMsg.value = null;
  try {
    const r = await listReceipts();
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

const totalPendingAmount = computed(() => items.value.reduce((s, r) => s + r.price_amount_usd, 0));

function fmtDate(s: string | null): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleString("fa-IR", { dateStyle: "short", timeStyle: "short" });
  } catch {
    return s;
  }
}

async function openDrawer(r: ReceiptListItem) {
  drawerLoading.value = true;
  drawer.value = null;
  try {
    drawer.value = await getReceipt(r.id);
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    drawerLoading.value = false;
  }
}

async function doApprove(r: ReceiptListItem) {
  if (!confirm(`تأیید رسید پرداخت ${r.price_amount_usd.toFixed(2)} $؟ کیف پول کاربر شارژ می‌شود.`)) return;
  busyId.value = r.id;
  try {
    await approveReceipt(r.id);
    flash("✅ رسید تأیید و کیف پول شارژ شد.");
    drawer.value = null;
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally {
    busyId.value = "";
  }
}

async function doReject(r: ReceiptListItem) {
  if (!confirm("رد رسید پرداخت؟")) return;
  busyId.value = r.id;
  try {
    await rejectReceipt(r.id);
    flash("رسید رد شد.");
    drawer.value = null;
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
        <h1 class="text-2xl font-bold text-white">رسیدهای در انتظار</h1>
        <p class="text-sm text-slate-400 mt-1">
          {{ fmtNumber(items.length) }} رسید — جمع: {{ totalPendingAmount.toFixed(2) }} $
        </p>
      </div>
      <button class="btn btn-secondary" @click="refresh" :disabled="loading">به‌روزرسانی</button>
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
            <th>کاربر</th>
            <th>مبلغ</th>
            <th>کارت</th>
            <th>تاریخ</th>
            <th class="text-end">عملیات</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in items" :key="r.id">
            <td>
              <button class="text-white hover:text-accent text-sm" @click="openDrawer(r)">
                {{ r.user?.first_name || r.user?.username || "بدون نام" }}
              </button>
              <div class="text-[11px] text-slate-500 font-mono">
                {{ r.user?.telegram_id || "—" }}
              </div>
            </td>
            <td>
              <div class="font-mono text-white">{{ r.price_amount_usd.toFixed(2) }} $</div>
              <div class="text-[11px] text-slate-500 font-mono">
                {{ fmtNumber(r.pay_amount) }} {{ r.pay_currency }}
              </div>
            </td>
            <td class="text-xs">
              <div class="text-slate-300">{{ r.card_holder || "—" }}</div>
              <div class="font-mono text-slate-500">{{ r.card_number || "" }}</div>
            </td>
            <td class="text-xs text-slate-300">{{ fmtDate(r.created_at) }}</td>
            <td class="text-end whitespace-nowrap">
              <button class="btn btn-ghost btn-sm" @click="openDrawer(r)">مشاهده</button>
              <button class="btn btn-ghost btn-sm text-emerald-300" :disabled="busyId === r.id" @click="doApprove(r)">تأیید</button>
              <button class="btn btn-ghost btn-sm text-rose-300" :disabled="busyId === r.id" @click="doReject(r)">رد</button>
            </td>
          </tr>
          <tr v-if="!items.length">
            <td colspan="5" class="text-center text-slate-500 py-8">
              ✅ هیچ رسیدی در صف نیست.
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ── Detail drawer ─────────────────────────────────────────── -->
    <div
      v-if="drawer || drawerLoading"
      class="fixed inset-0 bg-black/60 backdrop-blur-sm z-40 flex items-center justify-center p-4"
      @click.self="drawer = null"
    >
      <div class="card w-full max-w-2xl max-h-[90vh] overflow-y-auto">
        <div class="flex justify-between items-center mb-3">
          <h3 class="text-lg font-bold text-white">جزئیات رسید</h3>
          <button class="btn btn-ghost" @click="drawer = null">×</button>
        </div>
        <div v-if="drawerLoading" class="animate-pulse h-40" />
        <div v-else-if="drawer" class="space-y-4">
          <div class="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
            <div>
              <div class="text-[11px] text-slate-500">کاربر</div>
              <div class="text-slate-200">{{ drawer.user?.first_name || "—" }}</div>
              <div class="text-[11px] text-slate-500 font-mono">{{ drawer.user?.telegram_id }}</div>
            </div>
            <div>
              <div class="text-[11px] text-slate-500">مبلغ</div>
              <div class="text-white font-mono">{{ drawer.price_amount_usd.toFixed(2) }} $</div>
              <div class="text-[11px] text-slate-500 font-mono">
                {{ fmtNumber(drawer.pay_amount) }} {{ drawer.pay_currency }}
              </div>
            </div>
            <div>
              <div class="text-[11px] text-slate-500">کارت مقصد</div>
              <div class="text-slate-200">{{ drawer.card_holder || "—" }}</div>
              <div class="font-mono text-slate-300">{{ drawer.card_number || "" }}</div>
              <div class="text-[11px] text-slate-500">{{ drawer.card_bank || "" }}</div>
            </div>
            <div>
              <div class="text-[11px] text-slate-500">زمان ثبت</div>
              <div class="text-slate-300">{{ fmtDate(drawer.created_at) }}</div>
            </div>
          </div>

          <div class="border-t border-bg-border pt-3">
            <h4 class="text-sm font-bold text-white mb-2">ارزیابی ریسک</h4>
            <div class="grid grid-cols-3 gap-3 text-xs">
              <div>
                <div class="text-[11px] text-slate-500">سن حساب</div>
                <div class="font-mono text-slate-200">
                  {{ drawer.context.account_age_days !== null ? `${drawer.context.account_age_days} روز` : "—" }}
                </div>
              </div>
              <div>
                <div class="text-[11px] text-slate-500">رد ۳۰ روز اخیر</div>
                <div :class="drawer.context.rejected_recent > 1 ? 'text-rose-300 font-bold' : 'text-slate-200'" class="font-mono">
                  {{ drawer.context.rejected_recent }}
                </div>
              </div>
              <div>
                <div class="text-[11px] text-slate-500">پرداخت‌های موفق کل</div>
                <div class="font-mono text-slate-200">{{ drawer.context.paid_lifetime }}</div>
              </div>
            </div>
          </div>

          <div class="border-t border-bg-border pt-3">
            <h4 class="text-sm font-bold text-white mb-2">عکس رسید</h4>
            <div v-if="drawer.receipt_file_id" class="rounded-lg overflow-hidden border border-bg-border bg-bg-elev/30">
              <img
                :src="receiptPhotoUrl(drawer.id)"
                class="max-w-full max-h-[60vh] mx-auto block"
                alt="رسید پرداخت"
              />
            </div>
            <div v-else class="text-xs text-slate-500">عکسی ضمیمه نشده.</div>
          </div>

          <div class="flex justify-end gap-2 pt-2">
            <button
              class="btn btn-ghost text-rose-300"
              :disabled="busyId === drawer.id"
              @click="doReject(drawer)"
            >رد</button>
            <button
              class="btn btn-primary"
              :disabled="busyId === drawer.id"
              @click="doApprove(drawer)"
            >تأیید و شارژ کیف پول</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
