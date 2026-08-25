import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Merge conditional class lists, letting later Tailwind utilities win. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/*
 * Formatting helpers.
 *
 * Everything here is deliberately locale-light: the unit words that differ per
 * language ("never", "ago") are injectable so a component can hand in t()
 * output, while the byte units stay symbols, which are the same in every
 * language.
 *
 * Traffic is counted in decimal units - a kilobyte is a thousand bytes - because
 * a data limit is sold in gigabytes and set in gigabytes, and the usage figure
 * beside it is the same quantity measured. Counting the limit one way and the
 * usage against it the other would put "9.3 GiB of 10 GB" on a row and make an
 * operator do a conversion to find out whether a client is near its limit. The
 * cost is that `awg show` over SSH reports the same peer's traffic in binary and
 * reads about seven percent lower; the bytes are identical and this is the
 * surface the limit is set on.
 *
 * Memory keeps its binary units below, because that is the unit memory is sold,
 * reported and reasoned about in - a 16 GiB box would otherwise read as 17.2 GB
 * and look misreported.
 */

const BYTE_UNITS = ["B", "kB", "MB", "GB", "TB", "PB"] as const;
const MEMORY_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"] as const;

function scale(
  bytes: number | null | undefined,
  units: readonly string[],
  step: number,
  digits?: number,
): string {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n === 0) {
    return "0 B";
  }
  const sign = n < 0 ? "-" : "";
  let value = Math.abs(n);
  let unit = 0;
  while (value >= step && unit < units.length - 1) {
    value /= step;
    unit += 1;
  }
  // Whole bytes never need decimals; above that one decimal is enough to see a
  // transfer move without the column jittering on every 2 s poll.
  const places = digits ?? (unit === 0 ? 0 : value < 100 ? 1 : 0);
  return `${sign}${value.toFixed(places)} ${units[unit]}`;
}

/**
 * A speed limit is stored in bits per second, because that is what `tc` takes,
 * and typed in megabits, because that is what an operator sells. Decimal, like
 * every other rate in networking.
 *
 * Here rather than in either of the two forms that need it - the client dialog
 * and the settings page's default - because they have to agree: a megabit that
 * meant 1e6 in one and 2^20 in the other would show the operator a limit they
 * had not set.
 */
export const MBIT = 1e6;

/** "1.4 GB", counted in thousands. Non-finite input and 0 both render as "0 B". */
export function formatBytes(bytes: number | null | undefined, digits?: number): string {
  return scale(bytes, BYTE_UNITS, 1000, digits);
}

/** "1.4 GiB", counted in 1024s. For memory, and nothing else. */
export function formatMemory(bytes: number | null | undefined, digits?: number): string {
  return scale(bytes, MEMORY_UNITS, 1024, digits);
}

/** "12.3 kB/s". Same rules as formatBytes. */
export function formatRate(bytesPerSecond: number | null | undefined, digits?: number): string {
  return `${formatBytes(bytesPerSecond, digits)}/s`;
}

export type RelativeUnit = "never" | "now" | "second" | "minute" | "hour" | "day";

export interface RelativeParts {
  unit: RelativeUnit;
  /** Whole units elapsed; 0 for "never" and "now". */
  value: number;
}

/**
 * Split an epoch-seconds timestamp into a unit plus a count, so a component can
 * translate it properly (t('time.minutesAgo', { count })) instead of splicing
 * English into a number.
 *
 * 0, null and undefined all mean "no handshake yet" -> "never".
 */
export function relativeParts(
  unixSeconds: number | null | undefined,
  nowMs: number = Date.now(),
): RelativeParts {
  const ts = Number(unixSeconds);
  if (!Number.isFinite(ts) || ts <= 0) {
    return { unit: "never", value: 0 };
  }
  // A peer's clock is not ours: a handshake stamped in the future is still
  // "now", not a negative age.
  const seconds = Math.max(0, Math.floor(nowMs / 1000 - ts));
  if (seconds < 10) {
    return { unit: "now", value: 0 };
  }
  if (seconds < 60) {
    return { unit: "second", value: seconds };
  }
  if (seconds < 3600) {
    return { unit: "minute", value: Math.floor(seconds / 60) };
  }
  if (seconds < 86400) {
    return { unit: "hour", value: Math.floor(seconds / 3600) };
  }
  return { unit: "day", value: Math.floor(seconds / 86400) };
}

export interface RelativeLabels {
  never: string;
  now: string;
  /** "{n}" is replaced by the count. */
  second: string;
  minute: string;
  hour: string;
  day: string;
}

const DEFAULT_RELATIVE_LABELS: RelativeLabels = {
  never: "never",
  now: "just now",
  second: "{n}s ago",
  minute: "{n}m ago",
  hour: "{n}h ago",
  day: "{n}d ago",
};

export interface RelativeOptions {
  /** Epoch milliseconds to measure against; defaults to Date.now(). */
  now?: number;
  /** Translated overrides; anything omitted falls back to English. */
  labels?: Partial<RelativeLabels>;
}

/** "2m ago" / "never". Pass translated labels to keep it out of English. */
export function formatRelative(
  unixSeconds: number | null | undefined,
  options: RelativeOptions = {},
): string {
  const parts = relativeParts(unixSeconds, options.now);
  const labels = { ...DEFAULT_RELATIVE_LABELS, ...options.labels };
  return labels[parts.unit].replace("{n}", String(parts.value));
}

/** "3d 4h", "12m 5s". At most the two largest non-zero units. */
export function formatDuration(seconds: number | null | undefined): string {
  const total = Math.floor(Number(seconds));
  if (!Number.isFinite(total) || total <= 0) {
    return "0s";
  }
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;

  const parts: string[] = [];
  if (days > 0) {
    parts.push(`${days}d`);
  }
  if (hours > 0) {
    parts.push(`${hours}h`);
  }
  if (minutes > 0 && parts.length < 2) {
    parts.push(`${minutes}m`);
  }
  if (secs > 0 && days === 0 && parts.length < 2) {
    parts.push(`${secs}s`);
  }
  return parts.slice(0, 2).join(" ") || "0s";
}

export type ExpiryTense =
  "none" | "invalid" | "expired" | "minutes" | "today" | "tomorrow" | "days";

export interface ExpiryParts {
  tense: ExpiryTense;
  /** Whole minutes left for the "minutes" tense and whole days for "days"; 0 for the rest. */
  value: number;
  /** Milliseconds left; negative once the moment has passed, 0 when there is none. */
  msLeft: number;
  /** Calendar days between today and the expiry day: 0 today, 1 tomorrow, negative once past. */
  daysLeft: number;
  /** The moment itself, for the caller to format in its own locale; null if there is none. */
  date: Date | null;
}

const MINUTE_MS = 60_000;
const HOUR_MS = 3_600_000;
const DAY_MS = 86_400_000;

/** Midnight local time: where one calendar day ends and the next begins. */
function dayStart(ms: number): number {
  const date = new Date(ms);
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/**
 * Split an expiry moment into the tense to say it in, so a component can pick
 * the sentence its language wants - "Today at 18:30", "Tomorrow at 09:00", "in
 * 12m", "in 90d" - instead of every caller re-deriving which of those applies.
 *
 * The distinction between today, tomorrow and later is made in calendar days
 * rather than in 24-hour blocks, because that is what the words mean: a client
 * expiring at one in the morning expires tomorrow, whether that is in two hours
 * or in twenty. Under an hour the clock time stops being the useful fact and
 * the countdown takes over.
 *
 * Past tomorrow it is a count of days and never a date. "12 Aug" is a fact the
 * reader then has to do arithmetic on, and the question a subscription list is
 * scanned for is how much longer this client has - which "in 30d" answers and a
 * date does not. The exact moment stays one hover away, so nothing is lost.
 *
 * Everything is measured in the reader's own time zone, against a timestamp the
 * server states in UTC, so two operators in two countries each see the moment
 * they would have named.
 */
export function expiryParts(
  iso: string | null | undefined,
  nowMs: number = Date.now(),
): ExpiryParts {
  if (!iso) {
    return { tense: "none", value: 0, msLeft: 0, daysLeft: 0, date: null };
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return { tense: "invalid", value: 0, msLeft: 0, daysLeft: 0, date: null };
  }

  const msLeft = date.getTime() - nowMs;
  // Counted from midnight to midnight rather than by dividing the milliseconds,
  // so the number agrees with the words beside it: a client expiring tomorrow
  // morning is "in 1d" and not "in 2d" because thirty hours rounded up. Rounded
  // because a calendar day spanning a daylight-saving change is 23 or 25 hours
  // long, and the division would otherwise land just short of the day it
  // belongs to.
  const daysLeft = Math.round((dayStart(date.getTime()) - dayStart(nowMs)) / DAY_MS);
  const parts = (tense: ExpiryTense, value = 0): ExpiryParts => ({
    tense,
    value,
    msLeft,
    daysLeft,
    date,
  });

  if (msLeft <= 0) {
    return parts("expired");
  }
  if (msLeft < HOUR_MS) {
    return parts("minutes", Math.max(1, Math.ceil(msLeft / MINUTE_MS)));
  }
  if (daysLeft <= 0) {
    return parts("today");
  }
  if (daysLeft === 1) {
    return parts("tomorrow");
  }
  return parts("days", daysLeft);
}

/** Locale-aware date + time for ISO-8601 strings; "" when there is no date. */
export function formatDateTime(iso: string | null | undefined, locale?: string): string {
  if (!iso) {
    return "";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

/** Keys and endpoints are too long for a table cell: "abcdefgh...uvwxyz". */
export function truncateMiddle(value: string, head = 8, tail = 6): string {
  if (value.length <= head + tail + 1) {
    return value;
  }
  return `${value.slice(0, head)}…${value.slice(-tail)}`;
}

/** Never returns NaN, so a computed width can go straight into a style. */
export function clamp(value: number, min: number, max: number): number {
  const n = Number(value);
  if (!Number.isFinite(n)) {
    return min;
  }
  if (min > max) {
    return min;
  }
  return Math.min(Math.max(n, min), max);
}

/** 0..100. A zero or missing total is 0 percent, not Infinity. */
export function percent(
  value: number | null | undefined,
  total: number | null | undefined,
): number {
  const v = Number(value);
  const t = Number(total);
  if (!Number.isFinite(v) || !Number.isFinite(t) || t <= 0) {
    return 0;
  }
  return clamp((v / t) * 100, 0, 100);
}

/** Ready-to-use CSS length for a progress bar or meter. */
export function percentWidth(
  value: number | null | undefined,
  total: number | null | undefined,
): string {
  return `${percent(value, total).toFixed(2)}%`;
}
