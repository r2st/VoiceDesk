/** Display formatting. Money is integer paise everywhere in the API. */

/** `399900` -> `"₹3,999.00"`. Never do rupee arithmetic in floats. */
export function rupees(paise: number, { compact = false } = {}): string {
  const value = paise / 100;
  if (compact && Math.abs(value) >= 100000) {
    return `₹${(value / 100000).toFixed(2)}L`;
  }
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

export function number(value: number): string {
  return new Intl.NumberFormat("en-IN").format(value);
}

/** `125` -> `"2m 5s"`. */
export function duration(seconds: number): string {
  if (!seconds) return "0s";
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  if (!mins) return `${secs}s`;
  return secs ? `${mins}m ${secs}s` : `${mins}m`;
}

export function percent(fraction: number, digits = 1): string {
  return `${(fraction * 100).toFixed(digits)}%`;
}

/** Signed percentage for a delta badge: `12.4` -> `"+12.4%"`. */
export function signedPercent(value: number, digits = 1): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}%`;
}

const IST = "Asia/Kolkata";

/** Timestamps are rendered in IST — every tenant and every TRAI rule is Indian. */
export function dateTime(iso: string | null): string {
  if (!iso) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: IST,
  }).format(new Date(iso));
}

export function shortDate(iso: string | null): string {
  if (!iso) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    day: "2-digit",
    month: "short",
    timeZone: IST,
  }).format(new Date(iso));
}

/** `"2026-08"` -> `"August 2026"`. */
export function monthLabel(month: string): string {
  const [year, mon] = month.split("-");
  if (!year || !mon) return month;
  const date = new Date(Number(year), Number(mon) - 1, 1);
  return new Intl.DateTimeFormat("en-IN", {
    month: "long",
    year: "numeric",
  }).format(date);
}

export function titleCase(value: string): string {
  return value
    .split(/[_\s]+/)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

/** `+919876543210` -> `+91 98765 43210`. */
export function phone(value: string): string {
  const match = /^\+91(\d{5})(\d{5})$/.exec(value);
  return match ? `+91 ${match[1]} ${match[2]}` : value;
}

export const LANGUAGE_NAMES: Record<string, string> = {
  hi: "Hindi",
  en: "English",
  ta: "Tamil",
  te: "Telugu",
  mr: "Marathi",
  bn: "Bengali",
  kn: "Kannada",
};

export function languageName(code: string | null): string {
  if (!code) return "—";
  return LANGUAGE_NAMES[code] ?? code.toUpperCase();
}

/** ISO `YYYY-MM-DD` for a date N days back, in IST. */
export function isoDaysAgo(days: number): string {
  const now = new Date();
  now.setDate(now.getDate() - days);
  return now.toLocaleDateString("en-CA", { timeZone: IST });
}
