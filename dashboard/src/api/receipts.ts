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
}

export type ReceiptFilter = "pending" | "approved" | "rejected" | "history" | "all";

export function listReceipts(
  status: ReceiptFilter = "pending",
): Promise<{ items: ReceiptListItem[]; total: number }> {
  return api.get<{ items: ReceiptListItem[]; total: number }>(
    `/receipts?status=${encodeURIComponent(status)}`,
  );
}

export function getReceipt(id: string): Promise<ReceiptDetail> {
  return api.get<ReceiptDetail>(`/receipts/${id}`);
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
