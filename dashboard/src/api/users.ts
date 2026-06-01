import { api } from "./client";

export interface UserListItem {
  id: string;
  telegram_id: number;
  username: string | null;
  first_name: string | null;
  role: string;
  status: string;
  balance_usd: number;
  credit_limit_usd: number;
  created_at: string | null;
  last_seen_at: string | null;
}

export interface UserListResponse {
  items: UserListItem[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
}

export interface UserSubscription {
  id: string;
  status: string;
  source: string | null;
  name: string;
  volume_bytes: number;
  used_bytes: number;
  lifetime_used_bytes: number;
  starts_at: string | null;
  ends_at: string | null;
  created_at: string | null;
}

export interface WalletTxn {
  id: string;
  type: string;
  direction: "credit" | "debit";
  amount: number;
  currency: string;
  description: string | null;
  created_at: string | null;
  balance_after: number | null;
}

export interface UserDetail {
  id: string;
  telegram_id: number;
  username: string | null;
  first_name: string | null;
  last_name: string | null;
  language_code: string | null;
  role: string;
  status: string;
  is_bot_blocked: boolean;
  has_received_free_trial: boolean;
  ref_code: string | null;
  personal_discount_percent: number;
  created_at: string | null;
  last_seen_at: string | null;
  balance_usd: number;
  credit_limit_usd: number;
}

export interface UserDetailResponse {
  user: UserDetail;
  subscriptions: UserSubscription[];
  wallet_transactions: WalletTxn[];
}

export function listUsers(params: {
  q?: string;
  status?: string;
  page?: number;
  page_size?: number;
  sort?: string;
  order?: "asc" | "desc";
}): Promise<UserListResponse> {
  const qs = new URLSearchParams();
  if (params.q) qs.set("q", params.q);
  if (params.status) qs.set("status", params.status);
  qs.set("page", String(params.page ?? 1));
  qs.set("page_size", String(params.page_size ?? 25));
  if (params.sort) qs.set("sort", params.sort);
  if (params.order) qs.set("order", params.order);
  return api.get<UserListResponse>(`/users?${qs}`);
}

export function getUser(id: string): Promise<UserDetailResponse> {
  return api.get<UserDetailResponse>(`/users/${id}`);
}

export function adjustBalance(id: string, amount: number, description: string): Promise<{ ok: boolean; balance_usd: number }> {
  return api.patch<{ ok: boolean; balance_usd: number }>(`/users/${id}/balance`, { amount, description });
}

export function setCreditLimit(id: string, credit_limit: number): Promise<{ ok: boolean; credit_limit_usd: number }> {
  return api.patch<{ ok: boolean; credit_limit_usd: number }>(`/users/${id}/credit`, { credit_limit });
}

export function setUserStatus(id: string, status: "active" | "banned"): Promise<{ ok: boolean; status: string }> {
  return api.patch<{ ok: boolean; status: string }>(`/users/${id}/status`, { status });
}

export function sendMessage(id: string, text: string): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>(`/users/${id}/message`, { text });
}

export interface TransferConfigsResult {
  ok: boolean;
  message: string;
  count: number;
  target_name: string;
}

export function transferConfigs(
  id: string,
  payload: { target: string; all?: boolean; subscription_id?: string },
): Promise<TransferConfigsResult> {
  return api.post<TransferConfigsResult>(`/users/${id}/transfer-configs`, payload);
}
