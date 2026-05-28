<script setup lang="ts">
// Single KPI tile: icon + label on top, big formatted value, optional
// trend chip + hint at the bottom. Tones come from the tailwind palette
// so we can drop in `tone="success" | "warn" | "danger" | "accent"`.

interface Props {
  label: string;
  value: string;
  hint?: string;
  trendLabel?: string;
  trendTone?: "ok" | "warn" | "down";
  tone?: "accent" | "success" | "warn" | "danger" | "neutral";
  icon?: string; // material-style svg name we hardcode below
}
const props = withDefaults(defineProps<Props>(), {
  tone: "neutral",
  trendTone: "ok",
});

const TONE_CLASSES: Record<NonNullable<Props["tone"]>, string> = {
  accent:   "from-accent/20 to-accent/0 border-accent/30",
  success:  "from-emerald-400/20 to-emerald-400/0 border-emerald-400/30",
  warn:     "from-amber-400/20 to-amber-400/0 border-amber-400/30",
  danger:   "from-rose-400/20 to-rose-400/0 border-rose-400/30",
  neutral:  "from-slate-500/15 to-slate-500/0 border-bg-border",
};

const ICON_TONE: Record<NonNullable<Props["tone"]>, string> = {
  accent:  "text-accent",
  success: "text-emerald-300",
  warn:    "text-amber-300",
  danger:  "text-rose-300",
  neutral: "text-slate-300",
};

const TREND_TONE: Record<NonNullable<Props["trendTone"]>, string> = {
  ok:   "bg-emerald-500/15 text-emerald-300",
  warn: "bg-amber-500/15 text-amber-300",
  down: "bg-rose-500/15 text-rose-300",
};
</script>

<template>
  <div
    :class="['relative overflow-hidden rounded-xl2 p-5 border bg-gradient-to-br', TONE_CLASSES[props.tone]]"
  >
    <!-- Top: icon + label -->
    <div class="flex items-center gap-2 mb-3">
      <span :class="['inline-flex w-7 h-7 items-center justify-center rounded-md bg-bg-elev', ICON_TONE[props.tone]]">
        <!-- prettier-ignore -->
        <svg v-if="icon === 'users'"  class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M16 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2M21 21v-2a4 4 0 0 0-3-3.87M9 7a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm7 0a4 4 0 0 1 0 8" />
        </svg>
        <svg v-else-if="icon === 'service'" class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="3" y="4" width="18" height="6" rx="2" />
          <rect x="3" y="14" width="18" height="6" rx="2" />
          <circle cx="7" cy="7" r="0.5" fill="currentColor" />
          <circle cx="7" cy="17" r="0.5" fill="currentColor" />
        </svg>
        <svg v-else-if="icon === 'money'" class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="2" y="6" width="20" height="12" rx="2" />
          <circle cx="12" cy="12" r="3" />
        </svg>
        <svg v-else-if="icon === 'traffic'" class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M3 12h4l3-7 4 14 3-7h4" />
        </svg>
        <svg v-else-if="icon === 'server'" class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="3" y="4" width="18" height="7" rx="2" />
          <rect x="3" y="13" width="18" height="7" rx="2" />
        </svg>
        <svg v-else class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="9" />
        </svg>
      </span>
      <span class="text-xs uppercase tracking-wide text-slate-400">{{ label }}</span>
      <span
        v-if="trendLabel"
        :class="['ms-auto text-[10px] font-bold px-1.5 py-0.5 rounded', TREND_TONE[props.trendTone]]"
      >
        {{ trendLabel }}
      </span>
    </div>

    <!-- Big value -->
    <div class="text-3xl font-extrabold text-white leading-none tracking-tight tabular-nums">
      {{ value }}
    </div>

    <!-- Hint -->
    <div v-if="hint" class="mt-2 text-[11px] text-slate-400">{{ hint }}</div>

    <!-- Decorative gradient accent in the corner — pure CSS, no asset -->
    <div
      :class="['pointer-events-none absolute -end-8 -top-8 w-24 h-24 rounded-full blur-2xl opacity-50',
               props.tone === 'accent' ? 'bg-accent' :
               props.tone === 'success' ? 'bg-emerald-400' :
               props.tone === 'warn' ? 'bg-amber-400' :
               props.tone === 'danger' ? 'bg-rose-400' :
               'bg-slate-400']"
    />
  </div>
</template>
