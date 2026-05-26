// Thin fetch wrapper that:
//   * always sends cookies (the session cookie is HTTP-only — we can't
//     read it from JS, but the browser attaches it for us via credentials),
//   * uses JSON content-type by default,
//   * surfaces non-2xx responses as thrown ApiError objects with the
//     server's `detail` so call sites can show the message verbatim,
//   * routes 401s to the global auth store so an expired cookie bounces
//     to /login automatically.

import { useAuthStore } from "@/stores/auth";

const API_BASE = "/api/dashboard";

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(init.headers as Record<string, string> | undefined),
  };
  if (init.body && !(init.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    ...init,
    headers,
  });

  if (res.status === 401) {
    // Auto-logout on session expiry so the operator gets bounced to
    // /login instead of seeing a blank screen.
    try {
      const auth = useAuthStore();
      auth.clearAfter401();
    } catch {
      // Pinia not ready (e.g. during initial hydrate) — caller handles.
    }
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.detail || detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) return undefined as T;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    return (await res.json()) as T;
  }
  return (await res.text()) as unknown as T;
}

export const api = {
  get:    <T>(p: string) => request<T>(p, { method: "GET" }),
  post:   <T>(p: string, body?: any) => request<T>(p, { method: "POST",  body: body == null ? null : JSON.stringify(body) }),
  patch:  <T>(p: string, body?: any) => request<T>(p, { method: "PATCH", body: body == null ? null : JSON.stringify(body) }),
  delete: <T>(p: string)             => request<T>(p, { method: "DELETE" }),
};
