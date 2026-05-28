// Tiny formatting helpers shared by every view. Kept dependency-free so
// the bundle stays small.

const _PERSIAN_TO_LATIN_DIGITS: Record<string, string> = {
  "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
  "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
};

export function toWesternDigits(s: string): string {
  return s.replace(/[۰-۹]/g, (d) => _PERSIAN_TO_LATIN_DIGITS[d] ?? d);
}

export function fmtMoney(usd: number, opts?: { sign?: boolean }): string {
  const sign = (opts?.sign && usd > 0 ? "+" : "");
  return `${sign}$${usd.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",")}`;
}

export function fmtNumber(n: number): string {
  return Math.round(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

export function fmtBytes(bytes: number): string {
  if (bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : v >= 10 ? 1 : 2)} ${units[i]}`;
}

export function fmtRelativeTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  const diffSec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (diffSec < 60) return "لحظاتی پیش";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} دقیقه پیش`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} ساعت پیش`;
  const days = Math.floor(diffSec / 86400);
  if (days < 7) return `${days} روز پیش`;
  return d.toLocaleDateString("en-US");
}
