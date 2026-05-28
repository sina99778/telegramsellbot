import { api } from "./client";

export interface PlanItem {
  id: string;
  code: string;
  name: string;
  protocol: string;
  duration_days: number;
  volume_bytes: number;
  volume_gb: number;
  price: number;
  renewal_price: number;
  currency: string;
  is_active: boolean;
  inbound_id: string | null;
  inbound_label: string | null;
  server_name: string | null;
  subscription_count: number;
  created_at: string | null;
}

export interface InboundOption {
  id: string;
  label: string;
}

export interface PlanCreateBody {
  name: string;
  protocol: string;
  inbound_id: string | null;
  duration_days: number;
  volume_gb: number;
  price: number;
  renewal_price?: number | null;
  currency: string;
}

export interface PlanUpdateBody {
  name?: string;
  protocol?: string;
  inbound_id?: string | null;
  duration_days?: number;
  volume_gb?: number;
  price?: number;
  renewal_price?: number;
  currency?: string;
  is_active?: boolean;
}

export function listPlans(): Promise<{ items: PlanItem[]; total: number }> {
  return api.get<{ items: PlanItem[]; total: number }>("/plans");
}

export function listInboundOptions(): Promise<{ items: InboundOption[] }> {
  return api.get<{ items: InboundOption[] }>("/plans/_inbounds");
}

export function createPlan(body: PlanCreateBody): Promise<{ ok: boolean; id: string; code: string }> {
  return api.post<{ ok: boolean; id: string; code: string }>("/plans", body);
}

export function updatePlan(id: string, body: PlanUpdateBody): Promise<{ ok: boolean }> {
  return api.patch<{ ok: boolean }>(`/plans/${id}`, body);
}

export function deletePlan(id: string): Promise<{ ok: boolean }> {
  return api.delete<{ ok: boolean }>(`/plans/${id}`);
}
