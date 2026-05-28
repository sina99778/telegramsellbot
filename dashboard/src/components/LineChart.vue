<script setup lang="ts">
// Tiny Chart.js line-chart wrapper. We register only the controllers /
// elements we actually use so the bundle stays under 100 KB.

import { onMounted, onBeforeUnmount, ref, watch } from "vue";
import {
  Chart,
  LineController,
  LineElement,
  PointElement,
  CategoryScale,
  LinearScale,
  Filler,
  Tooltip,
} from "chart.js";

Chart.register(LineController, LineElement, PointElement, CategoryScale, LinearScale, Filler, Tooltip);

export interface ChartPoint {
  date: string;
  value: number;
}

interface Props {
  title: string;
  points: ChartPoint[];
  /** "#36D1E0" or any CSS colour. Default = accent (cyan). */
  color?: string;
  /** Optional currency prefix for tooltip values ("$" or "تومان"). */
  unit?: string;
  /** When true, format big numbers in tooltip with `,` thousands. */
  thousands?: boolean;
}
const props = withDefaults(defineProps<Props>(), {
  color: "#36D1E0",
  thousands: true,
});

const canvas = ref<HTMLCanvasElement | null>(null);
let chart: Chart | null = null;

function buildConfig() {
  return {
    type: "line" as const,
    data: {
      labels: props.points.map((p) => p.date.slice(5)), // MM-DD
      datasets: [
        {
          label: props.title,
          data: props.points.map((p) => p.value),
          borderColor: props.color,
          backgroundColor: props.color + "33",
          fill: true,
          tension: 0.35,
          pointRadius: 0,
          pointHoverRadius: 4,
          pointHoverBackgroundColor: props.color,
          borderWidth: 2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#0b0f17",
          borderColor: "#243149",
          borderWidth: 1,
          titleColor: "#cbd5e1",
          bodyColor: "#e2e8f0",
          padding: 10,
          displayColors: false,
          callbacks: {
            title: (items: any[]) => {
              const idx = items[0]?.dataIndex ?? 0;
              return props.points[idx]?.date ?? "";
            },
            label: (item: any) => {
              const v = Number(item.raw);
              const num = props.thousands
                ? Math.round(v).toLocaleString("en-US")
                : v.toFixed(2);
              return props.unit ? `${props.unit}${num}` : num;
            },
          },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { color: "#64748b", maxTicksLimit: 6, font: { size: 10 } },
        },
        y: {
          grid: { color: "rgba(36,49,73,0.5)" },
          ticks: {
            color: "#64748b",
            font: { size: 10 },
            callback: (v: any) =>
              typeof v === "number" && v >= 1000
                ? `${Math.round(v / 1000)}k`
                : v,
          },
          beginAtZero: true,
        },
      },
    },
  };
}

function buildOrUpdate() {
  if (!canvas.value) return;
  if (chart) {
    chart.destroy();
    chart = null;
  }
  chart = new Chart(canvas.value, buildConfig());
}

onMounted(buildOrUpdate);
watch(() => props.points, buildOrUpdate, { deep: true });
onBeforeUnmount(() => {
  if (chart) {
    chart.destroy();
    chart = null;
  }
});
</script>

<template>
  <div class="card">
    <div class="flex items-baseline justify-between mb-3">
      <h3 class="text-sm font-bold text-white">{{ title }}</h3>
      <span class="text-[11px] text-slate-500">۳۰ روز اخیر</span>
    </div>
    <div class="relative h-56">
      <canvas ref="canvas" />
    </div>
  </div>
</template>
