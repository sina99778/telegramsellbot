import { api } from "./client";

export interface BroadcastJobItem {
  id: string;
  status: string;
  message_type: string;
  text_preview: string;
  total: number;
  processed: number;
  failed: number;
  created_at: string | null;
  finished_at: string | null;
  via: string;
}

export interface BroadcastCreateBody {
  text: string;
  audience: "all" | "active" | "inactive";
}

export function listBroadcasts(): Promise<{ items: BroadcastJobItem[]; total: number; total_users: number }> {
  return api.get<{ items: BroadcastJobItem[]; total: number; total_users: number }>("/broadcast");
}

export function createBroadcast(body: BroadcastCreateBody): Promise<{ ok: boolean; id: string }> {
  return api.post<{ ok: boolean; id: string }>("/broadcast", body);
}
