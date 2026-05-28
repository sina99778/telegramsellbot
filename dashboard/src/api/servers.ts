import { api } from "./client";

export interface ServerListItem {
  id: string;
  name: string;
  base_url: string;
  panel_type: string;
  is_active: boolean;
  priority: number;
  health_status: string;
  subscription_port: number;
  config_domain: string | null;
  sub_domain: string | null;
  max_clients: number | null;
  inbound_count: number;
  active_inbound_count: number;
  client_count: number;
}

export interface ServerInbound {
  id: string;
  xui_inbound_remote_id: number;
  remark: string | null;
  protocol: string | null;
  port: number | null;
  tag: string | null;
  is_active: boolean;
  client_count: number;
}

export interface ServerDetail {
  server: {
    id: string;
    name: string;
    base_url: string;
    panel_type: string;
    is_active: boolean;
    priority: number;
    health_status: string;
    subscription_port: number;
    config_domain: string | null;
    sub_domain: string | null;
    max_clients: number | null;
    credentials_username: string | null;
    created_at: string | null;
  };
  inbounds: ServerInbound[];
}

export interface ServerCreateBody {
  name: string;
  base_url: string;
  panel_username: string;
  panel_password: string;
  config_domain?: string | null;
  sub_domain?: string | null;
  subscription_port?: number;
  max_clients?: number | null;
  priority?: number;
}

export interface ServerUpdateBody {
  name?: string;
  base_url?: string;
  panel_username?: string;
  panel_password?: string;
  is_active?: boolean;
  config_domain?: string | null;
  sub_domain?: string | null;
  subscription_port?: number;
  max_clients?: number | null;
  priority?: number;
}

export function listServers(): Promise<{ items: ServerListItem[]; total: number }> {
  return api.get<{ items: ServerListItem[]; total: number }>("/servers");
}

export function getServer(id: string): Promise<ServerDetail> {
  return api.get<ServerDetail>(`/servers/${id}`);
}

export function createServer(body: ServerCreateBody): Promise<{ ok: boolean; id: string }> {
  return api.post<{ ok: boolean; id: string }>("/servers", body);
}

export function updateServer(id: string, body: ServerUpdateBody): Promise<{ ok: boolean }> {
  return api.patch<{ ok: boolean }>(`/servers/${id}`, body);
}

export function deleteServer(id: string): Promise<{ ok: boolean }> {
  return api.delete<{ ok: boolean }>(`/servers/${id}`);
}

export function testServer(id: string): Promise<{ ok: boolean; inbound_count?: number; error?: string }> {
  return api.post<{ ok: boolean; inbound_count?: number; error?: string }>(`/servers/${id}/test`);
}
