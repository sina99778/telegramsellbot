import { api } from "./client";

export interface BrandSettings {
  name: string;
  logo_url: string;
  accent_color: string;
  support_handle: string;
}

export interface TextTemplateRow {
  key: string;
  default: string;
  label: string;
  group: string;
  multiline: boolean;
  notes: string;
}

export interface TextTemplatesResponse {
  catalogue: TextTemplateRow[];
  overrides: Record<string, string>;
}

export function fetchBrand(): Promise<BrandSettings> {
  return api.get<BrandSettings>("/brand");
}

export function patchBrand(body: Partial<BrandSettings>): Promise<{ ok: boolean }> {
  return api.patch<{ ok: boolean }>("/brand", body);
}

export function fetchTextTemplates(): Promise<TextTemplatesResponse> {
  return api.get<TextTemplatesResponse>("/text_templates");
}

export function patchTextTemplates(templates: Record<string, string | null>): Promise<{ ok: boolean }> {
  return api.patch<{ ok: boolean }>("/text_templates", { templates });
}
