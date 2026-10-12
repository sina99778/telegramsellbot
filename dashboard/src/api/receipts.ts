import { api } from "./client";

export interface ReceiptUserSummary {
  id: string;
  telegram_id: number;
  first_name: string | null;
  username: string | null;
}

export interface ReceiptListItem {
  id: string;
  created_at: string | null;
  price_amount_usd: number;
  pay_amount: number;
  pay_currency: string;
  status: string;
  receipt_file_id: string | null;
  card_number: string | null;
  card_holder: string | null;
  card_bank: string | null;
  user: ReceiptUserSummary | null;
}

export interface ReceiptDetail extends ReceiptListItem {
  context: {
    account_age_days: number | null;
    rejected_recent: number;
    paid_lifetime: number;
  };
  // OCR fraud verdict: ok=true matched the operator's card, false=not found
  // (possible "paid someone else"), null=couldn't read / OCR unavailable.
  ocr?: { ok: boolean | null; summary: string } | null;
}

export type ReceiptFilter =
  | "pending"
  | "approved"
  | "rejected"
  | "history"
  | "all";

export function listReceipts(
  status: ReceiptFilter = "pending",
  signal?: AbortSignal,
  params: { page?: number; page_size?: number; q?: string } = {},
): Promise<{ items: ReceiptListItem[]; total: number; total_pages: number }> {
  const qs = new URLSearchParams({
    status,
    page: String(params.page || 1),
    page_size: String(params.page_size || 50),
  });
  if (params.q) qs.set("q", params.q);
  return api.get<{
    items: ReceiptListItem[];
    total: number;
    total_pages: number;
  }>(`/receipts?${qs}`, signal);
}

export function getReceipt(
  id: string,
  signal?: AbortSignal,
): Promise<ReceiptDetail> {
  return api.get<ReceiptDetail>(`/receipts/${id}`, signal);
}

// We don't use the api wrapper here — we just need the URL for an <img src=...>.
export function receiptPhotoUrl(id: string): string {
  return `/api/dashboard/receipts/${id}/photo`;
}

export function approveReceipt(id: string): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>(`/receipts/${id}/approve`);
}

export function rejectReceipt(id: string): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>(`/receipts/${id}/reject`);
}
