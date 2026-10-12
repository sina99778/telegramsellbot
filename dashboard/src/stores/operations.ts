import { defineStore } from "pinia";
import { computed, ref } from "vue";
import { fetchOperations, type OperationsServer } from "@/api/overview";
export const healthyStatuses = new Set(["healthy", "ok", "online"]);
export function serverHealth(s: { is_active: boolean; health_status: string }) {
  if (!s.is_active) return "disabled";
  if (healthyStatuses.has(s.health_status)) return "healthy";
  if (["unhealthy", "error", "offline", "failed"].includes(s.health_status))
    return "unhealthy";
  return "unknown";
}
export const useOperations = defineStore("operations", () => {
  const servers = ref<OperationsServer[] | null>(null);
  const receiptCount = ref<number | null>(null);
  const pendingCount = ref<number | null>(null);
  const errors = ref<string[]>([]);
  const updatedAt = ref<Date | null>(null);
  const loading = ref(false);
  let generation = 0;
  let controller: AbortController | null = null;
  let inflight: Promise<void> | null = null;
  const unhealthy = computed(
    () => servers.value?.filter((s) => serverHealth(s) === "unhealthy") || [],
  );
  const unknown = computed(
    () => servers.value?.filter((s) => serverHealth(s) === "unknown") || [],
  );
  function refresh(): Promise<void> {
    if (inflight) return inflight;
    const id = ++generation;
    controller = new AbortController();
    const signal = controller.signal;
    loading.value = true;
    inflight = (async () => {
      try {
        const result = await fetchOperations(signal);
        if (id !== generation) return;
        servers.value = result.servers;
        receiptCount.value = result.pending_receipts;
        pendingCount.value = result.pending_payments;
        errors.value = [];
        updatedAt.value = new Date(result.generated_at);
      } catch {
        if (id === generation) errors.value = ["پایش عملیات"];
      }
    })().finally(() => {
      if (id === generation) {
        loading.value = false;
        inflight = null;
        controller = null;
      }
    });
    return inflight;
  }
  function reset() {
    generation++;
    controller?.abort();
    inflight = null;
    loading.value = false;
    servers.value = null;
    receiptCount.value = null;
    pendingCount.value = null;
    errors.value = [];
    updatedAt.value = null;
  }
  return {
    servers,
    receiptCount,
    pendingCount,
    errors,
    updatedAt,
    loading,
    unhealthy,
    unknown,
    refresh,
    reset,
  };
});
