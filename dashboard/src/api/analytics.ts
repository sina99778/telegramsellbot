import { api } from "./client";

export interface AnalyticsKpis {
  total_revenue: number;
  revenue_30d: number;
  revenue_7d: number;
  orders_count: number;
  paying_users: number;
  arpu: number;
  avg_order_value: number;
}

export interface AnalyticsChurn {
  active_subscribers: number;
  paying_users: number;
  churned_users: number;
  retention_rate: number;
  new_users_30d: number;
}

export interface TopCustomer {
  user_id: string;
  name: string;
  telegram_id: number;
  total_spent: number;
  orders: number;
}

export interface PlanRevenue {
  plan: string;
  revenue: number;
  orders: number;
}

export interface AnalyticsPayload {
  kpis: AnalyticsKpis;
  churn: AnalyticsChurn;
  top_customers: TopCustomer[];
  revenue_by_plan: PlanRevenue[];
}

export function fetchAnalytics(): Promise<AnalyticsPayload> {
  return api.get<AnalyticsPayload>("/analytics");
}
