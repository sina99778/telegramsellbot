<script setup lang="ts">
import { ref, onMounted, computed } from "vue";
import {
  fetchBrand,
  patchBrand,
  fetchTextTemplates,
  patchTextTemplates,
  type BrandSettings,
  type TextTemplateRow,
} from "@/api/brand_text";
import { ApiError } from "@/api/client";

type Tab = "brand" | "texts";

const tab = ref<Tab>("brand");
const loading = ref(true);
const busy = ref(false);
const errorMsg = ref<string | null>(null);
const banner = ref<string | null>(null);
const bannerTone = ref<"ok" | "warn">("ok");

// Brand state
const brand = ref<BrandSettings>({
  name: "", logo_url: "", accent_color: "#3b82f6", support_handle: "",
});

// Text templates state
const catalogue = ref<TextTemplateRow[]>([]);
const overrides = ref<Record<string, string>>({});
const draft = ref<Record<string, string>>({});
const dirty = ref<Set<string>>(new Set());

const groups = computed(() => {
  const out: Record<string, TextTemplateRow[]> = {};
  for (const t of catalogue.value) {
    (out[t.group] ||= []).push(t);
  }
  return out;
});
const GROUP_LABEL: Record<string, string> = {
  welcome: "خوش‌آمدگویی",
  purchase: "خرید",
  renewal: "تمدید",
  wallet: "کیف پول",
  support: "پشتیبانی",
  errors: "خطاها",
};

async function refresh() {
  loading.value = true;
  errorMsg.value = null;
  try {
    const [b, t] = await Promise.all([fetchBrand(), fetchTextTemplates()]);
    brand.value = { ...b };
    catalogue.value = t.catalogue;
    overrides.value = { ...t.overrides };
    draft.value = {};
    dirty.value = new Set();
    for (const row of t.catalogue) {
      draft.value[row.key] = overrides.value[row.key] ?? "";
    }
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

function onTextEdit(key: string) {
  if (draft.value[key] !== (overrides.value[key] ?? "")) {
    dirty.value.add(key);
  } else {
    dirty.value.delete(key);
  }
}

function resetToDefault(row: TextTemplateRow) {
  draft.value[row.key] = "";
  dirty.value.add(row.key);
}

async function saveBrand() {
  busy.value = true;
  try {
    await patchBrand(brand.value);
    flash("ذخیره شد. مینی‌اپ تا چند ثانیه دیگه از این مقادیر استفاده می‌کنه.");
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally { busy.value = false; }
}

async function saveTexts() {
  if (!dirty.value.size) {
    flash("تغییری برای ذخیره وجود نداره.", "warn");
    return;
  }
  busy.value = true;
  try {
    // Empty string → null so the backend clears the override.
    const body: Record<string, string | null> = {};
    for (const key of dirty.value) {
      const v = draft.value[key]?.trim();
      body[key] = v ? v : null;
    }
    await patchTextTemplates(body);
    flash("ذخیره شد. ربات تا حداکثر ۳۰ ثانیه دیگه از این متن‌ها استفاده می‌کنه.");
    refresh();
  } catch (exc) {
    flash(exc instanceof ApiError ? exc.detail : "خطا", "warn");
  } finally { busy.value = false; }
}
</script>

<template>
  <div class="p-6 lg:p-8 max-w-6xl mx-auto">
    <header class="mb-6">
      <h1 class="text-2xl font-bold text-white">محتوا و برند</h1>
      <p class="text-sm text-slate-400 mt-1">
        نام، لوگو، رنگ مینی‌اپ و متن‌های کلیدی ربات
      </p>
    </header>

    <div class="flex gap-2 mb-4 border-b border-bg-border">
      <button
        v-for="t in [
          { id: 'brand' as Tab, label: '🎨 برند' },
          { id: 'texts' as Tab, label: '✍️ متن‌ها' },
        ]"
        :key="t.id"
        class="px-4 py-2 text-sm border-b-2 -mb-px"
        :class="tab === t.id ? 'border-accent text-accent' : 'border-transparent text-slate-400 hover:text-slate-200'"
        @click="tab = t.id"
      >{{ t.label }}</button>
    </div>

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

    <!-- ── Brand tab ─────────────────────────────────────────────── -->
    <section v-else-if="tab === 'brand'" class="card space-y-4">
      <h3 class="font-bold text-white">برند مینی‌اپ</h3>
      <div class="text-[11px] text-slate-400">
        این مقادیر تو هدر مینی‌اپ و پیام‌های کلیدی ربات نمایش داده می‌شن.
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div>
          <label class="label">نام عمومی برند</label>
          <input v-model="brand.name" class="input" placeholder="TelegramSellBot" />
        </div>
        <div>
          <label class="label">آی‌دی پشتیبانی</label>
          <input v-model="brand.support_handle" class="input ltr font-mono" placeholder="SupportUser" />
          <div class="text-[10px] text-slate-500 mt-1">بدون @</div>
        </div>
        <div>
          <label class="label">URL لوگو</label>
          <input v-model="brand.logo_url" class="input ltr font-mono text-xs" placeholder="https://..." />
        </div>
        <div>
          <label class="label">رنگ اصلی</label>
          <div class="flex gap-2 items-center">
            <input v-model="brand.accent_color" type="text" class="input font-mono ltr flex-1" placeholder="#3b82f6" />
            <input v-model="brand.accent_color" type="color" class="w-12 h-10 rounded bg-bg-elev border border-bg-border" />
          </div>
        </div>
      </div>

      <div class="flex justify-end pt-2">
        <button class="btn btn-primary" :disabled="busy" @click="saveBrand">ذخیره</button>
      </div>
    </section>

    <!-- ── Texts tab ─────────────────────────────────────────────── -->
    <section v-else-if="tab === 'texts'" class="space-y-4">
      <div class="card flex flex-wrap items-center justify-between gap-2">
        <div class="text-xs text-slate-400">
          <span class="text-slate-200 font-bold">{{ dirty.size }}</span> تغییر در صف ذخیره.
          متن‌های خالی به پیش‌فرض کد بر می‌گردن.
        </div>
        <button class="btn btn-primary" :disabled="busy || !dirty.size" @click="saveTexts">
          ذخیره {{ dirty.size > 0 ? `(${dirty.size})` : "" }}
        </button>
      </div>

      <div
        v-for="(rows, groupKey) in groups"
        :key="groupKey"
        class="card space-y-3"
      >
        <h3 class="text-sm font-bold text-white border-b border-bg-border pb-2">
          {{ GROUP_LABEL[groupKey] || groupKey }}
        </h3>
        <div
          v-for="row in rows"
          :key="row.key"
          class="space-y-1 border-b border-bg-border/30 pb-3 last:border-0"
        >
          <div class="flex items-start justify-between gap-2">
            <div class="flex-1">
              <div class="text-sm text-slate-200">{{ row.label }}</div>
              <div class="text-[10px] text-slate-500 font-mono">{{ row.key }}</div>
            </div>
            <button
              v-if="dirty.has(row.key) || (draft[row.key]?.length || 0) > 0"
              class="btn btn-ghost btn-sm text-rose-300"
              @click="resetToDefault(row)"
              title="پاک کن تا به پیش‌فرض برگرده"
            >پیش‌فرض</button>
          </div>
          <textarea
            v-if="row.multiline"
            v-model="draft[row.key]"
            @input="onTextEdit(row.key)"
            class="input min-h-[80px]"
            :placeholder="row.default"
          />
          <input
            v-else
            v-model="draft[row.key]"
            @input="onTextEdit(row.key)"
            class="input"
            :placeholder="row.default"
          />
          <div v-if="row.notes" class="text-[10px] text-slate-500 mt-1">{{ row.notes }}</div>
          <div class="text-[10px] text-slate-600 mt-1">
            <span class="text-slate-500">پیش‌فرض:</span>
            <code class="font-mono">{{ row.default }}</code>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>
