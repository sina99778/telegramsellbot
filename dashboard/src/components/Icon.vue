<script setup lang="ts">
withDefaults(defineProps<{ name: string; size?: number }>(), { size: 18 });
const paths: Record<string, string> = {
  home: "M3 10 12 3l9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z",
  chart: "M3 3v18h18M8 16v-4m5 4V7m5 9v-6",
  users:
    "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m20 0v-2a4 4 0 0 0-3-3.9M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0m4-4a4 4 0 0 1 0 8",
  server:
    "M5 3h14a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm0 10h14a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2ZM7 7h.01M7 17h.01M15 7h3M15 17h3",
  plans: "M5 3h14v18H5ZM9 7h6M9 11h6M9 15h4",
  tag: "M3 3h9l9 9-9 9-9-9ZM8 8h.01",
  money: "M3 5h18v14H3ZM15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0M6 9h.01M18 15h.01",
  receipt: "M6 3h12v18l-3-2-3 2-3-2-3 2ZM9 8h6M9 12h6M9 16h3",
  megaphone: "M3 10v4h4l10 5V5L7 10ZM7 14l2 7h3M21 9v6",
  pencil: "m16 3 5 5L8 21H3v-5ZM13 6l5 5",
  settings:
    "M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1m8.6 8.6 2.1 2.1M5.6 18.4l2.1-2.1m8.6-8.6 2.1-2.1M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0",
  search: "M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0m-2 5 6 6",
  chevron: "m14 7-5 5 5 5",
  close: "m6 6 12 12M18 6 6 18",
  menu: "M4 6h16M4 12h16M4 18h16",
  sun: "M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0",
  moon: "M20 15.5A9 9 0 0 1 8.5 4 9 9 0 1 0 20 15.5Z",
  density: "M4 5h16M4 9h16M4 13h16M4 17h16M4 21h16",
  logout: "M9 3H4v18h5m7-5 4-4-4-4M9 12h11",
  refresh: "M20 7a9 9 0 1 0 1 6M20 3v5h-5",
  arrow: "M5 12h14m-6-6 6 6-6 6",
  download: "M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5",
  check: "m5 12 4 4L19 6",
  alert: "m12 3 10 18H2ZM12 9v4m0 4h.01",
  clock: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M12 7v5l3 2",
  traffic: "M3 12h4l3-8 4 16 3-8h4",
  bell: "M6 8a6 6 0 0 1 12 0v8l2 2H4l2-2ZM10 21h4",
  plus: "M12 5v14M5 12h14",
};
</script>
<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.7"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path :d="paths[name] || paths.home" />
  </svg>
</template>
