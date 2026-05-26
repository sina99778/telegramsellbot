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

  const isAuthed = computed(() => profile.value !== null);

  async function hydrate(): Promise<void> {
    try {
      profile.value = await api.get<AdminProfile>("/auth/me");
    } catch {
      profile.value = null;
    } finally {
      hydrating.value = false;
    }
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
