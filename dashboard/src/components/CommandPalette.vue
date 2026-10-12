<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { useRouter } from "vue-router";
import Icon from "./Icon.vue";
import { navigationItems, normalizeSearch } from "@/utils/navigation";
import { listUsers, type UserListItem } from "@/api/users";
import { latestOnly } from "@/utils/latest";
const props = defineProps<{ open: boolean }>();
const emit = defineEmits<{ close: [] }>();
const router = useRouter();
const dialog = ref<HTMLDialogElement | null>(null);
const input = ref<HTMLInputElement | null>(null);
const query = ref("");
const selected = ref(0);
const users = ref<UserListItem[]>([]);
const loading = ref(false);
const failed = ref(false);
const run = latestOnly();
let timer: ReturnType<typeof setTimeout> | undefined;
const results = computed(() => [
  ...navigationItems
    .filter((item) =>
      normalizeSearch(item.label + " " + item.hint + " " + item.name).includes(
        normalizeSearch(query.value),
      ),
    )
    .map((item) => ({
      key: item.name,
      title: item.label,
      hint: item.hint,
      icon: item.icon,
      to: item.to,
    })),
  ...users.value.map((user) => ({
    key: user.id,
    title: user.first_name || user.username || String(user.telegram_id),
    hint: `کاربر · ${user.telegram_id}${user.username ? " · @" + user.username : ""}`,
    icon: "users",
    to: "/users/" + encodeURIComponent(user.id),
  })),
]);
watch(
  () => props.open,
  async (open) => {
    if (open) {
      query.value = "";
      users.value = [];
      failed.value = false;
      selected.value = 0;
      await nextTick();
      dialog.value?.showModal();
      input.value?.focus();
    } else {
      dialog.value?.close();
      run.cancel();
      clearTimeout(timer);
      loading.value = false;
    }
  },
);
watch(query, () => {
  selected.value = 0;
  users.value = [];
  failed.value = false;
  run.cancel();
  clearTimeout(timer);
  const q = query.value.trim();
  loading.value = q.length >= 2;
  if (q.length < 2) return;
  timer = setTimeout(
    () =>
      run((signal) => listUsers({ q, page_size: 6 }, signal), {
        commit: (r) => {
          users.value = r.items;
        },
        fail: () => {
          failed.value = true;
        },
        settle: () => {
          loading.value = false;
        },
      }),
    250,
  );
});
async function choose(index: number) {
  const item = results.value[index];
  if (!item) return;
  emit("close");
  await router.push(item.to);
}
function keydown(e: KeyboardEvent) {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const n = results.value.length;
    selected.value = n
      ? (selected.value + (e.key === "ArrowDown" ? 1 : -1) + n) % n
      : 0;
    nextTick(() =>
      document
        .getElementById("command-result-" + selected.value)
        ?.scrollIntoView({ block: "nearest" }),
    );
  } else if (e.key === "Enter") {
    e.preventDefault();
    choose(selected.value);
  }
}
onBeforeUnmount(() => {
  run.cancel();
  clearTimeout(timer);
  dialog.value?.close();
});
</script>
<template>
  <dialog
    ref="dialog"
    class="command-dialog"
    aria-labelledby="command-title"
    @cancel="emit('close')"
    @close="emit('close')"
  >
    <h2 id="command-title" class="sr-only">جست‌وجوی سریع پنل و کاربران</h2>
    <div class="command-input">
      <Icon name="search" :size="21" /><input
        ref="input"
        v-model="query"
        maxlength="128"
        placeholder="صفحه، نام کاربر یا شناسهٔ تلگرام…"
        aria-label="جست‌وجوی سریع"
        role="combobox"
        aria-autocomplete="list"
        aria-expanded="true"
        aria-controls="command-results"
        :aria-activedescendant="
          results.length ? 'command-result-' + selected : undefined
        "
        @keydown="keydown"
      /><button
        class="icon-button"
        aria-label="بستن جست‌وجو"
        @click="emit('close')"
      >
        <Icon name="close" />
      </button>
    </div>
    <div
      class="command-results"
      id="command-results"
      role="listbox"
      aria-label="نتایج جست‌وجو"
    >
      <div class="eyebrow px-4 py-3">
        {{ query ? "نتایج جست‌وجو" : "دسترسی سریع به بخش‌ها" }}
      </div>
      <button
        v-for="(item, index) in results"
        :key="item.key"
        :id="'command-result-' + index"
        class="command-item"
        :class="{ 'is-selected': selected === index }"
        role="option"
        :aria-selected="selected === index"
        tabindex="-1"
        @click="choose(index)"
        @mousemove="selected = index"
      >
        <span class="command-icon"><Icon :name="item.icon" /></span
        ><span class="flex-1 text-start min-w-0"
          ><strong>{{ item.title }}</strong
          ><small>{{ item.hint }}</small></span
        ><Icon name="chevron" :size="15" />
      </button>
      <p v-if="!results.length && !loading" class="empty-state">
        نتیجه‌ای پیدا نشد. نام یا شناسهٔ کامل کاربر را امتحان کنید.
      </p>
      <p v-if="loading" role="status" class="command-status">
        در حال جست‌وجوی کاربران…
      </p>
      <p v-if="failed" role="alert" class="command-status text-danger">
        جست‌وجوی کاربران انجام نشد؛ اتصال را بررسی کنید.
      </p>
    </div>
    <footer class="command-footer">
      <span>↑ ↓ انتخاب <span class="mx-2">·</span> Enter بازکردن</span
      ><span>Esc بستن</span>
    </footer>
  </dialog>
</template>
