import { createRouter, createWebHistory, RouteRecordRaw } from "vue-router";
import { useAuthStore } from "@/stores/auth";

const routes: RouteRecordRaw[] = [
  {
    path: "/login",
    name: "login",
    component: () => import("@/views/Login.vue"),
    meta: { public: true },
  },
  {
    path: "/",
    component: () => import("@/views/AppShell.vue"),
    children: [
      {
        path: "",
        name: "overview",
        component: () => import("@/views/Overview.vue"),
      },
      {
        path: "users",
        name: "users",
        component: () => import("@/views/Users.vue"),
      },
      {
        path: "users/:id",
        name: "user-detail",
        component: () => import("@/views/UserDetail.vue"),
        props: true,
      },
      {
        path: "servers",
        name: "servers",
        component: () => import("@/views/Servers.vue"),
      },
      {
        path: "transactions",
        name: "transactions",
        component: () => import("@/views/Transactions.vue"),
      },
      {
        path: "plans",
        name: "plans",
        component: () => import("@/views/Plans.vue"),
      },
      {
        path: "discounts",
        name: "discounts",
        component: () => import("@/views/Discounts.vue"),
      },
      {
        path: "broadcast",
        name: "broadcast",
        component: () => import("@/views/Broadcast.vue"),
      },
      {
        path: "content",
        name: "content",
        component: () => import("@/views/Content.vue"),
      },
      {
        path: "receipts",
        name: "receipts",
        component: () => import("@/views/Receipts.vue"),
      },
      {
        path: "settings",
        name: "settings",
        component: () => import("@/views/Settings.vue"),
      },
    ],
  },
  {
    path: "/:pathMatch(.*)*",
    redirect: { name: "overview" },
  },
];

export const router = createRouter({
  // SPA lives under /dashboard/, so the history base must match.
  history: createWebHistory("/dashboard/"),
  routes,
});

// Global guard: every non-public route requires a hydrated profile.
router.beforeEach(async (to) => {
  if (to.meta.public) return true;
  const auth = useAuthStore();
  // Always await hydration. hydrate() is idempotent (shared cached promise), so
  // this is instant after the first call but guarantees we never read isAuthed
  // before /auth/me has resolved — which previously bounced logged-in operators
  // to /login on a hard deep-link load.
  await auth.hydrate();
  if (!auth.isAuthed) {
    return {
      name: "login",
      query: { next: to.fullPath },
    };
  }
  return true;
});
