<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";
import { useRoute, RouterLink } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import { usePreferences } from "@/stores/preferences";
import { useOperations } from "@/stores/operations";
import { navigation, navigationItems } from "@/utils/navigation";
import Icon from "@/components/Icon.vue";
import CommandPalette from "@/components/CommandPalette.vue";
const auth = useAuthStore();
const route = useRoute();
const preferences = usePreferences();
const ops = useOperations();
const commandOpen = ref(false);
const mobileOpen = ref(false);
const isMobile = ref(window.matchMedia("(max-width:1050px)").matches);
const mobileMedia = window.matchMedia("(max-width:1050px)");
function onMediaChange() {
  isMobile.value = mobileMedia.matches;
  if (!isMobile.value) mobileOpen.value = false;
}
const sidebar = ref<HTMLElement | null>(null);
const menuButton = ref<HTMLButtonElement | null>(null);
const current = computed(
  () =>
    navigationItems.find(
      (item) =>
        item.name === route.name ||
        (item.name === "users" && route.name === "user-detail"),
    ) || navigationItems[0],
);
const initials = computed(
  () => (auth.profile?.display_name || auth.profile?.username || "A").trim()[0],
);
function closeMenu() {
  mobileOpen.value = false;
  nextTick(() => menuButton.value?.focus());
}
function keydown(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    mobileOpen.value = false;
    commandOpen.value = !commandOpen.value;
  }
  if (e.key === "Escape" && mobileOpen.value) closeMenu();
  if (e.key === "Tab" && mobileOpen.value) {
    const els = Array.from(
      sidebar.value?.querySelectorAll<HTMLElement>("a,button") || [],
    ).filter((e) => e.offsetParent !== null);
    const first = els[0],
      last = els[els.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last?.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first?.focus();
    }
  }
}
watch(
  () => route.fullPath,
  () => {
    mobileOpen.value = false;
    nextTick(() => document.getElementById("workspace")?.focus());
  },
);
watch(mobileOpen, (open) => {
  if (open)
    requestAnimationFrame(() =>
      sidebar.value?.querySelector<HTMLElement>("button")?.focus(),
    );
});
let interval: ReturnType<typeof setInterval>;
onMounted(() => {
  mobileMedia.addEventListener("change", onMediaChange);
  ops.refresh();
  window.addEventListener("keydown", keydown);
  interval = setInterval(() => {
    if (document.visibilityState === "visible") ops.refresh();
  }, 60000);
});
onBeforeUnmount(() => {
  mobileMedia.removeEventListener("change", onMediaChange);
  clearInterval(interval);
  window.removeEventListener("keydown", keydown);
  ops.reset();
});
</script>
<template>
  <div class="admin-shell">
    <a href="#workspace" class="skip-link">رفتن به محتوای صفحه</a>
    <header class="shell-topbar" :inert="mobileOpen">
      <div class="topbar-brand">
        <button
          ref="menuButton"
          class="icon-button mobile-menu-button"
          aria-label="بازکردن منو"
          :aria-expanded="mobileOpen"
          @click="mobileOpen = true"
        >
          <Icon name="menu" /></button
        ><span class="brand-symbol"><Icon name="server" :size="19" /></span
        ><strong>SELLBOT<span>ADMIN CONSOLE</span></strong>
      </div>
      <button class="global-search" @click="commandOpen = true">
        <Icon name="search" :size="16" /><span>جست‌وجوی سریع…</span
        ><kbd dir="ltr">Ctrl K</kbd>
      </button>
      <div class="topbar-tools">
        <button
          class="icon-button"
          :aria-label="
            preferences.theme === 'dark'
              ? 'فعال‌کردن حالت روشن'
              : 'فعال‌کردن حالت تاریک'
          "
          :title="preferences.theme === 'dark' ? 'حالت روشن' : 'حالت تاریک'"
          @click="preferences.toggleTheme()"
        >
          <Icon :name="preferences.theme === 'dark' ? 'sun' : 'moon'" /></button
        ><button
          class="icon-button density-button"
          :aria-label="
            preferences.density === 'compact' ? 'نمایش راحت' : 'نمایش فشرده'
          "
          :aria-pressed="preferences.density === 'compact'"
          title="تغییر تراکم اطلاعات"
          @click="preferences.toggleDensity()"
        >
          <Icon name="density" /></button
        ><RouterLink
          to="/receipts"
          class="icon-button notification-button"
          aria-label="رسیدهای در انتظار بررسی"
          ><Icon name="bell" /><span
            v-if="ops.receiptCount"
            class="notification-dot"
          ></span></RouterLink
        ><span class="topbar-divider"></span
        ><span
          class="topbar-avatar"
          :title="auth.profile?.display_name || auth.profile?.username"
          >{{ initials }}</span
        >
      </div>
    </header>
    <button
      v-if="mobileOpen"
      class="sidebar-backdrop"
      aria-label="بستن منو"
      tabindex="-1"
      @click="closeMenu"
    ></button>
    <aside
      ref="sidebar"
      class="shell-sidebar"
      :inert="isMobile && !mobileOpen"
      :class="{ 'is-open': mobileOpen }"
      aria-label="منوی مدیریت"
    >
      <div class="sidebar-heading">
        <span>فضای مدیریت</span
        ><button
          class="icon-button mobile-menu-button"
          aria-label="بستن منو"
          @click="closeMenu"
        >
          <Icon name="close" /></button
        ><span class="sidebar-version">TS</span>
      </div>
      <nav class="sidebar-navigation">
        <section
          v-for="section in navigation"
          :key="section.group"
          class="nav-group"
        >
          <h2>{{ section.group }}</h2>
          <RouterLink
            v-for="item in section.items"
            :key="item.name"
            :to="item.to"
            class="nav-item"
            :class="{ 'is-active': current.name === item.name }"
            :aria-current="current.name === item.name ? 'page' : undefined"
            ><Icon :name="item.icon" :size="17" /><span>{{ item.label }}</span
            ><span
              v-if="item.name === 'receipts' && ops.receiptCount"
              class="nav-count"
              >{{ ops.receiptCount }}</span
            ><span
              v-if="item.name === 'servers' && ops.unhealthy.length"
              class="nav-count danger"
              >{{ ops.unhealthy.length }}</span
            ></RouterLink
          >
        </section>
      </nav>
      <div class="sidebar-bottom">
        <div class="sidebar-account">
          <span class="account-dot"></span>
          <div>
            <strong>{{
              auth.profile?.display_name || auth.profile?.username
            }}</strong
            ><small>حساب مدیر</small>
          </div>
          <button
            class="icon-button"
            aria-label="خروج از حساب"
            title="خروج"
            @click="auth.logout()"
          >
            <Icon name="logout" :size="17" />
          </button>
        </div>
      </div>
    </aside>
    <main
      id="workspace"
      class="shell-workspace"
      :inert="mobileOpen"
      tabindex="-1"
    >
      <div class="workspace-breadcrumb">
        <span>فضای مدیریت</span><Icon name="chevron" :size="12" /><strong>{{
          current.label
        }}</strong
        ><span class="breadcrumb-meta">مدیریت فروش و خدمات</span>
      </div>
      <router-view v-slot="{ Component, route: r }"
        ><component :is="Component" :key="r.fullPath"
      /></router-view>
      <footer class="workspace-footer">
        <span>TelegramSellBot · پنل مدیریت</span
        ><span>اطلاعات سلامت بر اساس آخرین بررسی ثبت‌شده</span>
      </footer>
    </main>
    <CommandPalette :open="commandOpen" @close="commandOpen = false" />
  </div>
</template>
