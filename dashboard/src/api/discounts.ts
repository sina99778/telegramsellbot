import { api } from "./client";

export interface DiscountItem {
  id: string;
  code: string;
  discount_percent: number;
  max_uses: number;
  used_count: number;
  is_active: boolean;
  expires_at: string | null;
  plan_id: string | null;
  created_at: string | null;
}

export interface DiscountCreateBody {
  code: string;
  discount_percent: number;
  max_uses?: number;
  expires_at?: string | null;
  plan_id?: string | null;
}

export interface DiscountUpdateBody {
  discount_percent?: number;
  max_uses?: number;
  is_active?: boolean;
  expires_at?: string | null;
  plan_id?: string | null;
}

export function listDiscounts(): Promise<{ items: DiscountItem[]; total: number }> {
  return api.get<{ items: DiscountItem[]; total: number }>("/discounts");
}

export function createDiscount(body: DiscountCreateBody): Promise<{ ok: boolean; id: string }> {
  return api.post<{ ok: boolean; id: string }>("/discounts", body);
}

export function updateDiscount(id: string, body: DiscountUpdateBody): Promise<{ ok: boolean }> {
  return api.patch<{ ok: boolean }>(`/discounts/${id}`, body);
}

export function deleteDiscount(id: string): Promise<{ ok: boolean }> {
  return api.delete<{ ok: boolean }>(`/discounts/${id}`);
}
