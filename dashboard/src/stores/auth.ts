// Auth state: who's logged in, hydration on app boot, and login/logout
// actions. The session lives in an HTTP-only cookie — JS can't read it
// directly. We treat /api/dashboard/auth/me's success as proof of being
// logged in and store the returned profile.

import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { api, ApiError } from "@/api/client";
import { router } from "@/router";

export interface AdminProfile {
  id: string;
  username: string;
  display_name: string | null;
  last_login_at: string | null;
  is_active: boolean;
}

export const useAuthStore = defineStore("auth", () => {
  const profile = ref<AdminProfile | null>(null);
  const hydrating = ref(true);
  // Shared in-flight hydration promise. Both App.vue (onMounted) and the router
  // guard call hydrate() on a hard deep-link load; without sharing one promise
  // they raced — the guard could read isAuthed=false before /auth/me resolved
  // and bounce a logged-in operator to /login. Caching the promise makes
  // hydrate() idempotent.
  let hydratePromise: Promise<void> | null = null;

  const isAuthed = computed(() => profile.value !== null);

  async function hydrate(): Promise<void> {
    if (hydratePromise) return hydratePromise;
    const p = (async () => {
      try {
        profile.value = await api.get<AdminProfile>("/auth/me");
      } catch {
        profile.value = null;
        // Allow a retry after a transient failure (don't cache the failure).
        hydratePromise = null;
      } finally {
        hydrating.value = false;
      }
    })();
    hydratePromise = p;
    return p;
  }

  async function login(username: string, password: string): Promise<void> {
    const res = await api.post<{ ok: boolean; admin: AdminProfile }>(
      "/auth/login",
      { username, password },
    );
    profile.value = res.admin;
  }

  async function logout(): Promise<void> {
    try {
      await api.post<{ ok: boolean }>("/auth/logout");
    } catch (exc) {
      // Logout shouldn't really fail; swallow any error and proceed.
    }
    profile.value = null;
    await router.push({ name: "login" });
  }

  /** Called by the api client on a 401 response. */
  function clearAfter401(): void {
    profile.value = null;
    if (router.currentRoute.value.name !== "login") {
      router.replace({
        name: "login",
        query: { next: router.currentRoute.value.fullPath },
      });
    }
  }

  return { profile, hydrating, isAuthed, hydrate, login, logout, clearAfter401 };
});
