// Typed client for the dashboard overview endpoint. Mirrors the shape
// returned by `apps/api/routes/dashboard/overview.py::get_overview`.

import { api } from "./client";

export interface OverviewKpis {
  total_users: number;
  active_subs: number;
  revenue_mtd_usd: number;
  traffic_delivered_bytes: number;
  active_servers: number;
  last_24h: {
    signups: number;
    purchases: number;
    revenue_usd: number;
  };
}

export interface SeriesPoint {
  date: string;
  value: number;
}

export interface ActivityEvent {
  kind: "order" | "signup";
  at: string;
  amount_usd?: number;
  status?: string;
  user_telegram_id?: number;
  user_first_name?: string | null;
}

export interface OverviewPayload {
  generated_at: string;
  kpis: OverviewKpis;
  charts: {
    revenue_30d: SeriesPoint[];
    signups_30d: SeriesPoint[];
  };
  recent_activity: ActivityEvent[];
}

export function fetchOverview(): Promise<OverviewPayload> {
  return api.get<OverviewPayload>("/overview");
}
