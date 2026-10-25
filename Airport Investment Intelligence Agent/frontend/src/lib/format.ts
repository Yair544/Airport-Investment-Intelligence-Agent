// Display formatting. Every formatter turns a missing value (null, undefined, NaN) into NO_DATA so
// a gap is never shown as zero. Percent inputs are already percent values (84.2 means 84.2%).

export const NO_DATA = "No data";

const LOCALE = "en-US";

export function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

const numberFormats = new Map<string, Intl.NumberFormat>();

function numberFormat(digits: number, signed = false): Intl.NumberFormat {
  const key = `${digits}:${signed}`;
  let format = numberFormats.get(key);
  if (!format) {
    format = new Intl.NumberFormat(LOCALE, {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
      signDisplay: signed ? "exceptZero" : "auto",
    });
    numberFormats.set(key, format);
  }
  return format;
}

/** Fixed number of decimals with thousands separators: 1234.5 -> "1,234.5". */
export function formatNumber(value: number | null | undefined, digits = 0): string {
  return isNumber(value) ? numberFormat(digits).format(value) : NO_DATA;
}

export function formatInteger(value: number | null | undefined): string {
  return formatNumber(value, 0);
}

/** Signed change: 4512345 -> "+4,512,345", 0 -> "0". */
export function formatSigned(value: number | null | undefined, digits = 0): string {
  return isNumber(value) ? numberFormat(digits, true).format(value) : NO_DATA;
}

export function formatPercent(value: number | null | undefined, digits = 1): string {
  return isNumber(value) ? `${numberFormat(digits).format(value)}%` : NO_DATA;
}

export function formatSignedPercent(value: number | null | undefined, digits = 1): string {
  return isNumber(value) ? `${numberFormat(digits, true).format(value)}%` : NO_DATA;
}

export function formatMinutes(value: number | null | undefined, digits = 1): string {
  return isNumber(value) ? `${numberFormat(digits).format(value)} min` : NO_DATA;
}

export function formatMiles(value: number | null | undefined): string {
  return isNumber(value) ? `${numberFormat(0).format(value)} mi` : NO_DATA;
}

export function formatNauticalMiles(value: number | null | undefined): string {
  return isNumber(value) ? `${numberFormat(0).format(value)} nmi` : NO_DATA;
}

/** Sample size suffix such as "n = 1,234"; empty when unknown. */
export function formatSample(value: number | null | undefined): string {
  return isNumber(value) ? `n = ${numberFormat(0).format(value)}` : "";
}

export type UnitKind = "percent" | "minutes" | "other";

export function unitKind(unit: string | null | undefined): UnitKind {
  const u = (unit ?? "").toLowerCase();
  if (u.includes("%") || u.includes("percent") || u.includes("pct")) return "percent";
  if (u.startsWith("min") || u.includes("minute")) return "minutes";
  return "other";
}

/** A value in the unit reported by the server (percent, minutes or a free-form unit). */
export function formatWithUnit(value: number | null | undefined, unit: string, digits = 1): string {
  if (!isNumber(value)) return NO_DATA;
  switch (unitKind(unit)) {
    case "percent":
      return formatPercent(value, digits);
    case "minutes":
      return formatMinutes(value, digits);
    default:
      return unit ? `${formatNumber(value, digits)} ${unit}` : formatNumber(value, digits);
  }
}

function roundTo(value: number, digits: number): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

/**
 * "value vs reference" in one unit, e.g. "84.2% vs 83.1%". When both round to the same text but
 * differ, up to two more decimals are shown so a pass/fail badge never sits next to equal numbers.
 */
export function formatComparison(
  value: number | null | undefined,
  reference: number | null | undefined,
  unit: string,
  digits = 1,
): string {
  let shown = digits;
  if (isNumber(value) && isNumber(reference) && value !== reference) {
    while (shown < digits + 2 && roundTo(value, shown) === roundTo(reference, shown)) shown += 1;
  }
  return `${formatWithUnit(value, unit, shown)} vs ${formatWithUnit(reference, unit, shown)}`;
}

const dateTimeFormats = new Map<string, Intl.DateTimeFormat>();

function dateTimeFormat(options: Intl.DateTimeFormatOptions, timeZone?: string): Intl.DateTimeFormat {
  const key = JSON.stringify([options, timeZone ?? ""]);
  let format = dateTimeFormats.get(key);
  if (!format) {
    format = new Intl.DateTimeFormat(LOCALE, timeZone ? { ...options, timeZone } : options);
    dateTimeFormats.set(key, format);
  }
  return format;
}

function parseDate(value: string): Date | null {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** ISO timestamp -> "Oct 23, 2026, 1:45 PM" in the viewer's time zone (or `timeZone`). */
export function formatDateTime(value: string | null | undefined, timeZone?: string): string {
  if (!value) return NO_DATA;
  const date = parseDate(value);
  if (!date) return value;
  return dateTimeFormat({ dateStyle: "medium", timeStyle: "short" }, timeZone).format(date);
}

/** Calendar date ("2026-10-01") -> "Oct 1, 2026". Date-only values are read as UTC so they never shift. */
export function formatDate(value: string | null | undefined): string {
  if (!value) return NO_DATA;
  const date = parseDate(/^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T00:00:00Z` : value);
  if (!date) return value;
  return dateTimeFormat({ dateStyle: "medium" }, "UTC").format(date);
}

/** Compact timestamp for lists: time today, "Oct 21" this year, "Oct 21, 2025" otherwise. */
export function formatShortDateTime(
  value: string | null | undefined,
  options: { now?: Date; timeZone?: string } = {},
): string {
  if (!value) return "";
  const date = parseDate(value);
  if (!date) return value;
  const now = options.now ?? new Date();
  const day = dateTimeFormat({ year: "numeric", month: "2-digit", day: "2-digit" }, options.timeZone);
  if (day.format(date) === day.format(now)) {
    return dateTimeFormat({ hour: "numeric", minute: "2-digit" }, options.timeZone).format(date);
  }
  const year = dateTimeFormat({ year: "numeric" }, options.timeZone);
  const sameYear = year.format(date) === year.format(now);
  return dateTimeFormat(
    sameYear ? { month: "short", day: "numeric" } : { month: "short", day: "numeric", year: "numeric" },
    options.timeZone,
  ).format(date);
}

/** Elapsed seconds -> "0:07", "2:05". */
export function formatElapsed(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function monthLabel(month: number, year?: number): string {
  const name = MONTHS[month - 1] ?? String(month);
  return year === undefined ? name : `${name} ${year}`;
}

/** "threshold_miles" -> "Threshold miles". */
export function humanize(key: string): string {
  const text = key.replace(/[_-]+/g, " ").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : key;
}

/** Tool parameters are shown verbatim (no thousands separators, so years stay "2025"). */
export function formatParameterValue(value: string | number | boolean | string[] | null): string {
  if (value === null) return "None";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "None";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}
