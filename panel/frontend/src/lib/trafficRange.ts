import type { TrafficWindow } from "@/api/types";

/*
 * What "from X to Z" means to the traffic charts, in one place.
 *
 * Two things are being chosen and they are easy to confuse: the *scale* is
 * whether a bar is a day or a month, and the *window* is which days or months
 * are drawn. They are independent - the same fortnight can be read as fourteen
 * bars or as one - and the picker offers them as two controls for that reason.
 *
 * Every date here is a UTC day, spelled "2026-08-13", because that is what the
 * server buckets on: the collector cuts a day at midnight UTC, so a browser in
 * Auckland asking for "today" in its own zone would ask for a day the server has
 * not started yet. Nothing in this file reads a local date component, and that
 * is deliberate rather than accidental.
 *
 * The windows built here are what the request carries. They are not what comes
 * back: the server caps a series at what a chart can draw and cuts a client's
 * window down to what its retention still holds, so the range a reader is shown
 * is always read off the answer rather than off the question.
 */

export type TrafficScale = "daily" | "monthly";

export const SCALES: readonly TrafficScale[] = ["daily", "monthly"];

export interface TrafficRange {
  scale: TrafficScale;
  /**
   * The window to ask for, or null for the one the endpoint defaults to at this
   * scale - which is the same span the default preset names. Kept as null
   * rather than spelled out so the first request of a page load carries no
   * query string, and so the answer to "what does this chart open on" lives at
   * one end of the wire instead of two.
   */
  window: TrafficWindow | null;
}

/** A span the picker offers by name, counted in the scale's own buckets. */
export interface RangePreset {
  /** Suffix under `chart.preset` in the translations. */
  id: string;
  span: number;
}

export const PRESETS: Record<TrafficScale, readonly RangePreset[]> = {
  daily: [
    { id: "days30", span: 30 },
    { id: "days90", span: 90 },
    { id: "days180", span: 180 },
    { id: "days365", span: 365 },
  ],
  monthly: [
    { id: "months6", span: 6 },
    { id: "months12", span: 12 },
    { id: "months24", span: 24 },
    { id: "months36", span: 36 },
  ],
};

/**
 * The span each scale opens on, matching apps/stats/history.py.
 *
 * Duplicated across the wire on purpose: the browser needs it to show which
 * preset is selected before any answer has arrived, and the server needs it to
 * answer a request that names no window at all. The two are pinned together by
 * the shape of the first response rather than by a constant either could edit
 * alone - if they ever disagree, the picker says "custom" over a default
 * window, which is wrong but harmless.
 */
export const DEFAULT_SPAN: Record<TrafficScale, number> = { daily: 90, monthly: 36 };

/** The value the preset select carries when the window is nobody's preset. */
export const CUSTOM = "custom";

/**
 * The earliest day any of this will offer, matching EARLIEST_DAY in
 * apps/stats/history.py.
 *
 * It reads like a formality and it is not. A date input is finished the moment
 * it holds a whole date, and somebody typing 2026 into the year passes through
 * 0002, 0020 and 0202 on the way - three complete, valid, entirely serious
 * requests for the year two. A floor is what stops each of those keystrokes
 * becoming a fetch and a redraw.
 */
export const EARLIEST_DAY = "2000-01-01";

/**
 * The preset a scale opens on, by name.
 *
 * So that the one control which has to say the default out loud - the way back
 * out of a window with nothing in it - says exactly what the select will read
 * once it has been pressed, rather than a second copy of the same sentence
 * drifting from it.
 */
export function defaultPreset(scale: TrafficScale): RangePreset {
  const span = DEFAULT_SPAN[scale];
  return PRESETS[scale].find((preset) => preset.span === span) ?? PRESETS[scale][0];
}

/** Today, as the server cuts days. */
export function utcToday(now: Date = new Date()): string {
  return now.toISOString().slice(0, 10);
}

/** A day as a UTC instant, so arithmetic on it cannot land in the wrong zone. */
function parseDay(day: string): Date {
  const [year, month, dayOfMonth] = day.split("-").map(Number);
  return new Date(Date.UTC(year || 1970, (month || 1) - 1, dayOfMonth || 1));
}

function formatDay(date: Date): string {
  return date.toISOString().slice(0, 10);
}

/** `days` before `day`, which may be negative to go forwards. */
export function shiftDays(day: string, days: number): string {
  const moment = parseDay(day);
  moment.setUTCDate(moment.getUTCDate() + days);
  return formatDay(moment);
}

/** The first of the month `months` before the one `day` falls in. */
export function monthStart(day: string, months = 0): string {
  const moment = parseDay(day);
  return formatDay(new Date(Date.UTC(moment.getUTCFullYear(), moment.getUTCMonth() - months, 1)));
}

/**
 * The window a preset names, ending today.
 *
 * A monthly span is whole months and a daily one is days, counting the current
 * bucket in both cases: "6 months" is this month and the five before it, which
 * is six columns, not six columns and a fragment.
 */
export function presetWindow(scale: TrafficScale, span: number, today = utcToday()): TrafficWindow {
  return {
    from: scale === "daily" ? shiftDays(today, 1 - span) : monthStart(today, span - 1),
    to: today,
  };
}

/**
 * Which preset the current window is, or CUSTOM.
 *
 * Compared by the window each preset would produce rather than by remembering
 * which button was pressed, so a window that arrives in the page's state from
 * anywhere else - a reload, a link, the scale being switched under it - is
 * still recognised as the preset it happens to equal.
 */
export function presetOf(range: TrafficRange, today = utcToday()): string {
  if (range.window === null) {
    return String(DEFAULT_SPAN[range.scale]);
  }
  for (const preset of PRESETS[range.scale]) {
    const window = presetWindow(range.scale, preset.span, today);
    if (window.from === range.window.from && window.to === range.window.to) {
      return String(preset.span);
    }
  }
  return CUSTOM;
}

/**
 * The same span at the other scale, so switching between days and months keeps
 * the window the reader chose instead of throwing it away.
 *
 * A default window stays a default window: the two scales default to different
 * spans on purpose, and carrying 90 days across to the monthly tab would put
 * three columns on a chart sized for thirty-six.
 */
export function withScale(range: TrafficRange, scale: TrafficScale): TrafficRange {
  return { scale, window: range.window };
}

/** The window a range actually asks for, spelled out even when it is the default. */
export function windowOf(range: TrafficRange, today = utcToday()): TrafficWindow {
  return range.window ?? presetWindow(range.scale, DEFAULT_SPAN[range.scale], today);
}
