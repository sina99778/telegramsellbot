import { api } from "./client";

export interface AllSettings {
  general: {
    sales_enabled: boolean;
    renewals_enabled: boolean;
    delete_enabled: boolean;
    refund_enabled: boolean;
  };
  pricing: {
    price_per_gb: number;
    price_per_10_days: number;
    toman_rate: number;
    display_currency: "USD" | "IRT";
  };
  custom_buy: {
    enabled: boolean;
    price_per_gb: number;
    price_per_day: number;
  };
  security: {
    xui_limit_ip: number;
    max_distinct_ips: number;
    auto_disable_ip_abuse: boolean;
  };
  backup: {
    interval_hours: number;
    channel_chat_id: number | null;
    sales_channel_chat_id: number | null;
    last_run_at: string | null;
  };
  premium_emoji: {
    enabled: boolean;
    emoji_map: Record<string, string>;
  };
  button_style: {
    enabled: boolean;
    confirm: ButtonStyle;
    destructive: ButtonStyle;
    navigation: ButtonStyle;
    info: ButtonStyle;
  };
}

// Each token maps to a colored-circle emoji prefix on the button (shown on
// every client). "primary"/"success"/"danger" ALSO map to the native Bot API
// 9.4 style; "violet"/"amber"/"orange" are emoji-only; "" = no color.
export type ButtonStyle = "primary" | "success" | "danger" | "violet" | "amber" | "orange" | "";

export function fetchAllSettings(): Promise<AllSettings> {
  return api.get<AllSettings>("/settings");
}

export function patchGeneral(body: Partial<AllSettings["general"]>): Promise<{ ok: boolean }> {
  return api.patch<{ ok: boolean }>("/settings/general", body);
}

export function patchPricing(body: Partial<AllSettings["pricing"]>): Promise<{ ok: boolean }> {
  return api.patch<{ ok: boolean }>("/settings/pricing", body);
}

export function patchCustomBuy(body: Partial<AllSettings["custom_buy"]>): Promise<{ ok: boolean }> {
  return api.patch<{ ok: boolean }>("/settings/custom_buy", body);
}

export function patchSecurity(body: Partial<AllSettings["security"]>): Promise<{ ok: boolean }> {
  return api.patch<{ ok: boolean }>("/settings/security", body);
}

export interface BackupPatchBody {
  interval_hours?: number;
  channel_chat_id?: number;
  clear_channel?: boolean;
}
export function patchBackup(body: BackupPatchBody): Promise<{ ok: boolean }> {
  return api.patch<{ ok: boolean }>("/settings/backup", body);
}

export function runBackupNow(): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>("/settings/backup/run-now");
}

export interface PremiumEmojiPatchBody {
  enabled?: boolean;
  emoji_map?: Record<string, string>;
}
export function patchPremiumEmoji(body: PremiumEmojiPatchBody): Promise<{ ok: boolean }> {
  return api.patch<{ ok: boolean }>("/settings/premium_emoji", body);
}

export interface ButtonStylePatchBody {
  enabled?: boolean;
  confirm?: ButtonStyle;
  destructive?: ButtonStyle;
  navigation?: ButtonStyle;
  info?: ButtonStyle;
}
export function patchButtonStyle(body: ButtonStylePatchBody): Promise<{ ok: boolean }> {
  return api.patch<{ ok: boolean }>("/settings/button_style", body);
}
