<script setup lang="ts">
import { useAuthStore } from "@/stores/auth";
import { useRoute, RouterLink } from "vue-router";
import { computed } from "vue";

const auth = useAuthStore();
const route = useRoute();

// Sidebar items. The icon is plain SVG (no extra dep) so the dashboard
// stays under ~200 KB gzipped.
const navItems = [
  { name: "overview",     label: "نمای کلی",        to: "/",             icon: "home" },
  { name: "users",        label: "مدیریت کاربران",   to: "/users",        icon: "users" },
  { name: "servers",      label: "مدیریت سرورها",    to: "/servers",      icon: "server" },
  { name: "transactions", label: "تراکنش‌ها",         to: "/transactions", icon: "money" },
  { name: "settings",     label: "تنظیمات",          to: "/settings",     icon: "settings" },
] as const;

const currentRouteName = computed(() => route.name);

const initials = computed(() => {
  const n = auth.profile?.display_name || auth.profile?.username || "";
  return (n.trim()[0] || "A").toUpperCase();
});
</script>

<template>
  <div class="min-h-screen flex bg-bg-base">
    <!-- ── Sidebar ─────────────────────────────────────────── -->
    <aside
      class="w-60 shrink-0 bg-bg-panel border-l border-bg-border flex flex-col"
    >
      <!-- Brand -->
      <div class="px-5 py-5 border-b border-bg-border flex items-center gap-3">
        <div
          class="w-9 h-9 rounded-lg bg-accent flex items-center justify-center text-bg-base font-extrabold"
        >
          TS
        </div>
        <div>
          <div class="text-sm font-bold text-white leading-tight">پنل مدیریت</div>
          <div class="text-[11px] text-slate-400">TelegramSellBot</div>
        </div>
      </div>

      <!-- Nav -->
      <nav class="flex-1 p-3 space-y-1">
        <router-link
          v-for="item in navItems"
          :key="item.name"
          :to="item.to"
          class="flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-all"
          :class="
            currentRouteName === item.name ||
            (item.name === 'users' && currentRouteName === 'user-detail')
              ? 'bg-accent/15 text-accent border border-accent/30'
              : 'text-slate-300 hover:bg-bg-elev'
          "
        >
          <!-- inline SVG icons -->
          <svg v-if="item.icon === 'home'" class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
            <path stroke-linecap="round" stroke-linejoin="round" d="M3 9.75 12 3l9 6.75V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1V9.75Z" />
          </svg>
          <svg v-else-if="item.icon === 'users'" class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
            <path stroke-linecap="round" stroke-linejoin="round" d="M16 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2M21 21v-2a4 4 0 0 0-3-3.87M9 7a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm7 0a4 4 0 0 1 0 8" />
          </svg>
          <svg v-else-if="item.icon === 'server'" class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
            <rect x="3" y="4" width="18" height="6" rx="2" />
            <rect x="3" y="14" width="18" height="6" rx="2" />
            <circle cx="7" cy="7" r="0.5" fill="currentColor" />
            <circle cx="7" cy="17" r="0.5" fill="currentColor" />
          </svg>
          <svg v-else-if="item.icon === 'money'" class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
            <rect x="2" y="6" width="20" height="12" rx="2" />
            <circle cx="12" cy="12" r="3" />
            <path d="M6 10v.01M18 14v.01" stroke-linecap="round" />
          </svg>
          <svg v-else-if="item.icon === 'settings'" class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="3" />
            <path stroke-linecap="round" stroke-linejoin="round" d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.9 2.9l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.9l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1A2 2 0 0 1 7 4.7l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.9l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z" />
          </svg>
          <span class="font-medium">{{ item.label }}</span>
        </router-link>
      </nav>

      <!-- User chip -->
      <div class="p-3 border-t border-bg-border">
        <div class="flex items-center gap-3 px-2 py-2">
          <div
            class="w-9 h-9 rounded-full bg-accent text-bg-base font-bold flex items-center justify-center"
          >
            {{ initials }}
          </div>
          <div class="flex-1 min-w-0">
            <div class="text-sm text-white truncate">
              {{ auth.profile?.display_name || auth.profile?.username || "—" }}
            </div>
            <button
              class="text-[11px] text-slate-400 hover:text-danger transition-colors"
              @click="auth.logout()"
            >
              خروج
            </button>
          </div>
        </div>
      </div>
    </aside>

    <!-- ── Main outlet ─────────────────────────────────────── -->
    <main class="flex-1 overflow-x-hidden">
      <router-view v-slot="{ Component, route: r }">
        <transition name="fade" mode="out-in">
          <component :is="Component" :key="r.fullPath" />
        </transition>
      </router-view>
    </main>
  </div>
</template>

<style>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 120ms ease-out;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
