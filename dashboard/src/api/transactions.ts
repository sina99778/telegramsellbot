import { api } from "./client";

export interface OrderRow {
  id: string;
  user_id: string;
  user_telegram_id: number | null;
  user_first_name: string | null;
  plan_name: string;
  amount: number;
  currency: string;
  status: string;
  source: string;
  created_at: string | null;
}

export interface PaymentRow {
  id: string;
  user_telegram_id: number | null;
  user_first_name: string | null;
  provider: string;
  kind: string;
  status: string;
  pay_currency: string | null;
  pay_amount: number | null;
  price_currency: string;
  price_amount: number;
  actually_paid: number | null;
  created_at: string | null;
}

export interface WalletTxnRow {
  id: string;
  user_telegram_id: number | null;
  user_first_name: string | null;
  type: string;
  direction: "credit" | "debit";
  amount: number;
  currency: string;
  description: string | null;
  balance_after: number | null;
  created_at: string | null;
}

export interface PendingRow {
  id: string;
  user_telegram_id: number | null;
  user_first_name: string | null;
  provider: string;
  kind: string;
  status: string;
  pay_currency: string | null;
  pay_amount: number | null;
  price_amount: number;
  created_at: string | null;
}

export interface Page<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
}

export function listOrders(params: {
  status?: string;
  user_telegram_id?: number;
  from_date?: string;
  to_date?: string;
  page?: number;
  page_size?: number;
}): Promise<Page<OrderRow>> {
  const qs = new URLSearchParams();
  if (params.status) qs.set("status", params.status);
  if (params.user_telegram_id) qs.set("user_telegram_id", String(params.user_telegram_id));
  if (params.from_date) qs.set("from_date", params.from_date);
  if (params.to_date) qs.set("to_date", params.to_date);
  qs.set("page", String(params.page ?? 1));
  qs.set("page_size", String(params.page_size ?? 25));
  return api.get<Page<OrderRow>>(`/transactions/orders?${qs}`);
}

export function listPayments(params: {
  status?: string;
  provider?: string;
  user_telegram_id?: number;
  page?: number;
  page_size?: number;
}): Promise<Page<PaymentRow>> {
  const qs = new URLSearchParams();
  if (params.status) qs.set("status", params.status);
  if (params.provider) qs.set("provider", params.provider);
  if (params.user_telegram_id) qs.set("user_telegram_id", String(params.user_telegram_id));
  qs.set("page", String(params.page ?? 1));
  qs.set("page_size", String(params.page_size ?? 25));
  return api.get<Page<PaymentRow>>(`/transactions/payments?${qs}`);
}

export function listWalletTxns(params: {
  user_telegram_id?: number;
  direction?: "credit" | "debit";
  page?: number;
  page_size?: number;
}): Promise<Page<WalletTxnRow>> {
  const qs = new URLSearchParams();
  if (params.user_telegram_id) qs.set("user_telegram_id", String(params.user_telegram_id));
  if (params.direction) qs.set("direction", params.direction);
  qs.set("page", String(params.page ?? 1));
  qs.set("page_size", String(params.page_size ?? 25));
  return api.get<Page<WalletTxnRow>>(`/transactions/wallet?${qs}`);
}

export function listPending(): Promise<{ items: PendingRow[]; total: number }> {
  return api.get<{ items: PendingRow[]; total: number }>("/transactions/pending");
}

export function ordersExportUrl(params: {
  status?: string;
  from_date?: string;
  to_date?: string;
}): string {
  const qs = new URLSearchParams();
  if (params.status) qs.set("status", params.status);
  if (params.from_date) qs.set("from_date", params.from_date);
  if (params.to_date) qs.set("to_date", params.to_date);
  return `/api/dashboard/transactions/orders.csv?${qs}`;
}
