import { defineStore } from "pinia";
import { ref, watch } from "vue";
function read(key: string, fallback: string) {
  try {
    return localStorage.getItem(key) || fallback;
  } catch {
    return fallback;
  }
}
export function applyPreferences() {
  document.documentElement.dataset.theme =
    read("ts.dashboard.theme", "dark") === "light" ? "light" : "dark";
  document.documentElement.dataset.density =
    read("ts.dashboard.density", "compact") === "comfortable"
      ? "comfortable"
      : "compact";
}
export const usePreferences = defineStore("dashboard-preferences", () => {
  const theme = ref(
    read("ts.dashboard.theme", "dark") === "light" ? "light" : "dark",
  );
  const density = ref(
    read("ts.dashboard.density", "compact") === "comfortable"
      ? "comfortable"
      : "compact",
  );
  watch(
    [theme, density],
    () => {
      try {
        localStorage.setItem("ts.dashboard.theme", theme.value);
        localStorage.setItem("ts.dashboard.density", density.value);
      } catch {
        /* private mode */
      }
      applyPreferences();
    },
    { immediate: true },
  );
  return {
    theme,
    density,
    toggleTheme: () => {
      theme.value = theme.value === "dark" ? "light" : "dark";
    },
    toggleDensity: () => {
      density.value = density.value === "compact" ? "comfortable" : "compact";
    },
  };
});
