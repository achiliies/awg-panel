import * as React from "react";
import { useTranslation } from "react-i18next";
import {
  Bar,
  BarChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipProps,
} from "recharts";

import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { TrafficRangeControls } from "@/components/TrafficRangeControls";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ChartBar } from "@/lib/icons";
import { defaultPreset } from "@/lib/trafficRange";
import { cn, formatBytes } from "@/lib/utils";
import type { TrafficRange, TrafficScale } from "@/lib/trafficRange";
import type { TrafficHistory, TrafficPoint } from "@/api/types";

/*
 * How much has moved, day by day and month by month.
 *
 * The counterpart to ThroughputChart, and deliberately the opposite of it in
 * every choice the two do not share. That one draws a rate over a sliding
 * window of the last two minutes, kept in a ref and never fetched; this one
 * draws a volume over closed buckets that the server has already finished
 * adding up. Rate against volume is what settles the rest:
 *
 * - Areas there, bars here. An area implies a value between its points, which
 *   is true of a rate sampled every two seconds and false of a day: there is no
 *   moment "between" Tuesday and Wednesday to interpolate, and a curve drawn
 *   through two daily totals invents one.
 * - Stacked here, overlaid there. The stack is only legitimate when the sum is
 *   a quantity somebody reads, and here it is precisely the quantity the
 *   dashboard's "traffic today" card already shows: download plus upload. On
 *   the throughput chart the same sum is meaningless, which is why the two
 *   series are laid over each other instead.
 *
 * One component for the server's history and for a client's, because the two
 * endpoints answer the same shape. A second copy would be a second place for
 * the month labels, the empty state and the direction naming to drift.
 *
 * Naming follows the server's point of view exactly once, here: `tx` is what
 * the server transmitted, which is what the client downloaded. Everything below
 * this line is already in the reader's terms.
 */

/*
 * The same two tokens the throughput chart uses, so download is one colour and
 * upload the other everywhere in the panel. Recharts paints SVG through props
 * and cannot see a Tailwind class, so each series carries both forms: the
 * utility for the DOM swatches, the raw expression for the plot.
 */
const DOWN_COLOR = "hsl(var(--chart-1))";
const UP_COLOR = "hsl(var(--chart-2))";
const DOWN_SWATCH = "bg-chart-1";
const UP_SWATCH = "bg-chart-2";

/*
 * A hairline of the card's own colour between the two segments of a bar.
 *
 * Without it a dark download segment meets a dark upload segment at an edge the
 * eye has to find by hue alone, which is exactly the join a red-green or
 * blue-yellow reader cannot see. The gap is a second, uncoloured channel
 * carrying the same boundary - and it costs one pixel of a bar that is at least
 * six wide.
 */
const SEGMENT_GAP = "hsl(var(--card))";

/*
 * The plot's own geometry, in pixels, because three things have to agree about
 * it and only one of them is drawn by recharts.
 *
 * The bars are in an SVG inside a scrolling box; the value axis and the grid
 * are plain elements laid over the same box and deliberately outside the
 * scroll, so that scrolling back through a quarter moves the bars and leaves
 * the scale where it is. Nothing derives its position from the other's
 * rendering - both are placed from these numbers - so the lines cannot drift
 * from the labels beside them at some width nobody tested.
 */
const AXIS_WIDTH = 64;
const X_AXIS_HEIGHT = 22;
const PLOT_TOP = 8;

/**
 * How many buckets the box shows at once. Everything past this is scrolled to,
 * at the same width and the same scale as everything before it - which is the
 * point of scrolling rather than squeezing: ninety days across a card is a
 * picket fence, and thirty days is a chart.
 */
const VISIBLE: Record<TrafficScale, number> = { daily: 30, monthly: 12 };

/** Floor and ceiling on what one bucket may be given, so a phone still draws
 *  bars and a five day window does not draw five slabs. */
const MIN_SLOT: Record<TrafficScale, number> = { daily: 9, monthly: 24 };
const MAX_BAR = 56;

/** Horizontal grid lines, and so the number of value labels beside them. */
const TICKS = 4;

interface Bucket extends TrafficPoint {
  /** Client download, which is what the server transmitted. */
  down: number;
  /** Client upload. */
  up: number;
}

function buckets(series: TrafficPoint[] | undefined): Bucket[] {
  return (series ?? []).map((point) => ({ ...point, down: point.tx, up: point.rx }));
}

/**
 * A period label as the reader's own calendar writes it.
 *
 * Parsed by hand rather than with `new Date(period)`. A bare "2026-08-13" is
 * read by the browser as UTC midnight and then rendered in the reader's zone,
 * so west of Greenwich every bar in the chart would be labelled with the day
 * before the one it holds - and the panel would disagree with its own "traffic
 * today" card at the one moment somebody was comparing them. The parts are
 * therefore fed to a *local* date, which is a lie about the instant and the
 * truth about the label, which is all the label is.
 *
 * `long` is for the two ends of the range, which carry the year because they
 * are the sentence that says which quarter of which year is on screen. The axis
 * leaves it off: thirty ticks that all say 2026 spend width on the one thing
 * every one of them agrees about.
 */
function usePeriodFormat(scale: TrafficScale): (period: string, long?: boolean) => string {
  const { i18n } = useTranslation();

  return React.useMemo(() => {
    const day = new Intl.DateTimeFormat(i18n.language, { month: "short", day: "numeric" });
    const fullDay = new Intl.DateTimeFormat(i18n.language, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
    const month = new Intl.DateTimeFormat(i18n.language, { month: "short", year: "numeric" });
    return (period: string, long = false) => {
      const [year, monthIndex, dayOfMonth] = period.split("-").map(Number);
      if (!year || !monthIndex) {
        return period;
      }
      const local = new Date(year, monthIndex - 1, dayOfMonth || 1);
      if (scale !== "daily") {
        return month.format(local);
      }
      return (long ? fullDay : day).format(local);
    };
  }, [i18n.language, scale]);
}

/** A short stroke of the series colour: the identity channel beside the words. */
function SeriesKey({ swatch }: { swatch: string }): JSX.Element {
  return (
    <span
      aria-hidden="true"
      className={cn("inline-block h-0.5 w-3.5 shrink-0 rounded-full", swatch)}
    />
  );
}

interface ReadoutProps {
  swatch: string;
  label: string;
  value: string;
}

/**
 * Legend and window total in one row.
 *
 * Two series always need a legend, and the number a reader wants first is what
 * the whole window came to - so the legend carries it rather than making them
 * add ninety bars up by eye. It is the sum of the range, not of the part of it
 * on screen: scrolling moves the view and asks no new question, while changing
 * the range does ask one and gets a different answer.
 */
function Readout({ swatch, label, value }: ReadoutProps): JSX.Element {
  return (
    <div className="min-w-0">
      <p className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
        <SeriesKey swatch={swatch} />
        <span className="truncate">{label}</span>
      </p>
      {/* A step below the figures beside the chart, and deliberately: this is
          the sum of whatever range happens to be selected, while those are the
          three fixed questions somebody opened the history to answer. Set
          larger, it was the loudest number on screen and the one that means the
          least on its own. */}
      <p className="mt-1 text-base font-semibold leading-none tracking-tight proportional-nums">
        {value}
      </p>
    </div>
  );
}

interface HistoryTooltipProps extends TooltipProps<number, string> {
  format: (period: string, long?: boolean) => string;
}

/**
 * One bucket, both directions and their total.
 *
 * The total is spelled out rather than left to be added up, because the stack
 * is what puts it on screen: a bar whose height is the sum is a bar whose
 * height a reader will try to read, and the tooltip is where that number can be
 * given exactly.
 */
function HistoryTooltip({
  active,
  payload,
  label,
  format,
}: HistoryTooltipProps): JSX.Element | null {
  const { t } = useTranslation();

  if (!active || !payload || payload.length === 0) {
    return null;
  }

  const value = (key: string): number => {
    const item = payload.find((entry) => entry.dataKey === key);
    return typeof item?.value === "number" ? item.value : 0;
  };
  const down = value("down");
  const up = value("up");

  return (
    <div className="rounded-md border border-border bg-popover px-3 py-2 text-popover-foreground shadow-md">
      <p className="mb-1.5 text-xs text-muted-foreground">
        {typeof label === "string" ? format(label, true) : ""}
      </p>
      <ul className="space-y-1">
        <li className="flex items-center gap-2 text-sm leading-tight">
          <SeriesKey swatch={DOWN_SWATCH} />
          <span className="font-semibold tabular-nums">{formatBytes(down)}</span>
          <span className="text-xs text-muted-foreground">{t("dashboard.download")}</span>
        </li>
        <li className="flex items-center gap-2 text-sm leading-tight">
          <SeriesKey swatch={UP_SWATCH} />
          <span className="font-semibold tabular-nums">{formatBytes(up)}</span>
          <span className="text-xs text-muted-foreground">{t("dashboard.upload")}</span>
        </li>
      </ul>
      <p className="mt-1.5 border-t border-border pt-1.5 text-sm font-semibold tabular-nums">
        {formatBytes(down + up)}
        <span className="ms-2 text-xs font-normal text-muted-foreground">{t("chart.total")}</span>
      </p>
    </div>
  );
}

/**
 * A round number at or above `rough`, in the units traffic is counted in.
 *
 * The value axis is fixed for the whole range rather than for the part of it in
 * view - that is what makes two bars a quarter apart comparable at a glance -
 * so its top has to be a number a reader can divide by four in their head. Left
 * to itself an axis topped at the tallest bar labels itself 1.37 GB, 2.74 GB,
 * 4.11 GB, which is arithmetic rather than a scale.
 *
 * Powers of ten, because formatBytes counts in thousands: a step of 1e9 reads
 * as "1 GB" and a step of 2^30 reads as "1.07 GB".
 *
 * The ladder is finer than the usual 1-2-5 because the top of the axis is four
 * of these and everything below it is drawn against that: rounding a step of
 * 27 GB up to 50 leaves the tallest bar at half the height of its own plot, and
 * a chart that only uses half of itself is a chart that reads as flat.
 */
const STEPS = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];

function niceStep(rough: number): number {
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const scaled = rough / magnitude;
  return (STEPS.find((step) => scaled <= step) ?? 10) * magnitude;
}

/** The axis, from zero to a round number above the tallest bar in the range. */
function axisTicks(peak: number): number[] {
  if (!(peak > 0)) {
    return [0];
  }
  const step = niceStep(peak / TICKS);
  return Array.from({ length: TICKS + 1 }, (_, index) => step * index);
}

/**
 * The axis labels, at the coarsest precision that still tells them apart.
 *
 * Nothing guarantees that a round step stays round across a unit boundary:
 * ticks half a terabyte apart are 0.5, 1, 1.5, 2 TB, and formatted to whole
 * units the middle two both read "2 TB" - an axis with the same number printed
 * twice at different heights. So the labels are formatted together rather than
 * one at a time, and gain a decimal only when they need one to differ.
 */
function tickLabels(ticks: number[]): string[] {
  for (const digits of [0, 1]) {
    const labels = ticks.map((value) => formatBytes(value, digits));
    if (new Set(labels).size === labels.length) {
      return labels;
    }
  }
  return ticks.map((value) => formatBytes(value, 2));
}

interface PlotProps {
  rows: Bucket[];
  scale: TrafficScale;
  format: (period: string, long?: boolean) => string;
  /** What the whole thing is, for anybody who cannot see it. */
  label: string;
  /** Plot height. The dialog is shorter than the statistics card. */
  height: string;
}

/**
 * The bars, in a box that scrolls, under an axis that does not.
 *
 * The scroll is the whole reason this is not four lines of recharts. A range of
 * ninety days is more bars than a card is wide, and the two ways out of that
 * both lose something a reader needs: squeeze them and the bars stop being
 * legible individually, or resample into weeks and the answer to "which day was
 * that" stops being in the picture at all. Scrolling keeps every bucket at a
 * width that can be pointed at, and pays for it with a gesture.
 *
 * What has to survive the gesture is the scale. A chart that re-fitted its axis
 * to whatever is in view would redraw a quiet fortnight as a busy one the
 * moment the busy month scrolled off - the bars would be tall, the axis would
 * say something different, and nobody reads the axis twice. So the domain is
 * computed once over the whole range and pinned, the axis and its grid lines
 * are laid over the box rather than inside it, and scrolling moves the bars and
 * nothing else.
 *
 * Forced to left-to-right regardless of the reader's language, like every other
 * chart here: time runs one way in this panel, `scrollLeft` means one thing in
 * one direction, and a mirrored plot would put the newest bar where the axis is.
 */
function Plot({ rows, scale, format, label, height }: PlotProps): JSX.Element {
  const scroller = React.useRef<HTMLDivElement>(null);
  const [viewport, setViewport] = React.useState(0);
  // How much of the box the horizontal scrollbar eats. Nothing on macOS, a dozen
  // pixels on a Linux desktop - and the axis overlay has to end where the plot
  // does or every grid line sits a dozen pixels below its own label.
  const [gutter, setGutter] = React.useState(0);

  React.useLayoutEffect(() => {
    const element = scroller.current;
    if (!element) {
      return;
    }
    const measure = (): void => {
      setViewport(element.clientWidth);
      setGutter(element.offsetHeight - element.clientHeight);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const slot = Math.max(MIN_SLOT[scale], viewport / VISIBLE[scale]);
  // Never narrower than the box: a range of five days fills the card and is
  // laid out by recharts, rather than huddling at one end of an empty plot.
  const content = Math.max(viewport, Math.round(slot * rows.length));
  const scrollable = content > viewport + 1;

  const peak = rows.reduce((most, row) => Math.max(most, row.down + row.up), 0);
  const ticks = axisTicks(peak);
  const labels = tickLabels(ticks);
  const top = ticks[ticks.length - 1] || 1;

  /*
   * Which way there is more.
   *
   * A scrollbar is the obvious answer and not a sufficient one: it is thin, it
   * is at the bottom of a box full of bars, and on the platforms that draw it as
   * an overlay it is not there at all until something moves. Meanwhile the one
   * thing this chart must not do is read as a complete picture of ninety days
   * when it is showing thirty of them. So each end that has more behind it
   * fades, which is the same cue a table with columns off-screen gives.
   */
  const [more, setMore] = React.useState({ older: false, newer: false });
  const onScroll = React.useCallback(() => {
    const element = scroller.current;
    if (element) {
      const room = element.scrollWidth - element.clientWidth;
      setMore({ older: element.scrollLeft > 1, newer: element.scrollLeft < room - 1 });
    }
  }, []);

  /*
   * Newest first, because the newest bucket is what somebody came for and the
   * older end is where they go looking. Re-anchored when the range changes and
   * not when the numbers do: this polls every thirty seconds, and a chart that
   * jumped back to today twice a minute would be unusable to anybody reading
   * last March.
   */
  const anchor = `${scale}:${rows.length}:${rows[0]?.period ?? ""}`;
  React.useLayoutEffect(() => {
    const element = scroller.current;
    if (element) {
      element.scrollLeft = element.scrollWidth;
    }
    onScroll();
  }, [anchor, content, onScroll]);

  return (
    <div dir="ltr" className={cn("relative w-full", height)}>
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0"
        style={{ top: PLOT_TOP, bottom: X_AXIS_HEIGHT + gutter }}
      >
        {ticks.map((value, index) => (
          <div
            key={value}
            // No gap: the line has to start exactly where the plot does, which
            // is where the axis gutter ends, or the grid and the bars are two
            // pictures a few pixels apart.
            className="absolute inset-x-0 flex -translate-y-1/2 items-center"
            style={{ top: `${(1 - value / top) * 100}%` }}
          >
            <span
              style={{ width: AXIS_WIDTH }}
              className="shrink-0 pe-2 text-end text-[11px] leading-none tabular-nums text-muted-foreground"
            >
              {labels[index]}
            </span>
            <span className="h-px flex-1 bg-border" />
          </div>
        ))}
      </div>

      {/*
        Taken out of the flow, which is not a layout preference but the thing
        that stops this measuring itself.

        The box inside it is given an explicit pixel width, and that width is
        worked out from the box's own. In normal flow those two are the same
        loop: a container that sizes to its content asks the scroller how wide
        its content is, is told, grows, and is asked again. Nothing here is
        wrong on a page whose width is settled before it arrives - and inside a
        dialog, which is as wide as what it holds, the same chart drove itself
        to a width of thirty-three million pixels in a handful of frames.
        Positioned, it contributes nothing to what contains it, and the arrow
        only points one way again.

        Being positioned also settles the painting: it comes after the grid and
        before the fades here, and that is the order the three appear in.
      */}
      <div
        ref={scroller}
        role="img"
        aria-label={label}
        onScroll={onScroll}
        // Focusable only when there is something to scroll to, so a chart that
        // fits does not become a tab stop that does nothing.
        tabIndex={scrollable ? 0 : -1}
        style={{ left: AXIS_WIDTH }}
        className={cn(
          "absolute inset-y-0 right-0 overflow-x-auto overscroll-x-contain",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          "focus-visible:ring-offset-2 focus-visible:ring-offset-background",
        )}
      >
        {viewport > 0 ? (
          <div style={{ width: content }} className="h-full">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={rows} margin={{ top: PLOT_TOP, right: 0, bottom: 0, left: 0 }}>
                <XAxis
                  dataKey="period"
                  height={X_AXIS_HEIGHT}
                  tickFormatter={(period: string) => format(period)}
                  tickLine={false}
                  axisLine={false}
                  // Recharts drops the labels that would collide rather than
                  // turning them sideways: a rotated axis costs height the plot
                  // needs more, and at this width most days are unlabelled
                  // anyway - the tooltip is where a bar says which day it is.
                  minTickGap={24}
                  tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }}
                />
                {/* Hidden, and the only reason it is here: the domain. The
                    visible axis is the overlay above, which does not scroll. */}
                <YAxis hide domain={[0, top]} />
                <Tooltip
                  content={<HistoryTooltip format={format} />}
                  // The whole bar, not the segment under the pointer: the
                  // tooltip reports both directions, so highlighting one of them
                  // would point at half of what it is about to say.
                  cursor={{ fill: "hsl(var(--muted-foreground))", fillOpacity: 0.12 }}
                />

                <Bar
                  dataKey="down"
                  stackId="traffic"
                  fill={DOWN_COLOR}
                  stroke={SEGMENT_GAP}
                  strokeWidth={1}
                  maxBarSize={MAX_BAR}
                  isAnimationActive={false}
                />
                <Bar
                  dataKey="up"
                  stackId="traffic"
                  fill={UP_COLOR}
                  stroke={SEGMENT_GAP}
                  strokeWidth={1}
                  maxBarSize={MAX_BAR}
                  // Only the top of the stack is rounded: the cap belongs to the
                  // bar, not to the segment that happens to be at the top of it.
                  radius={[4, 4, 0, 0]}
                  isAnimationActive={false}
                />
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : null}
      </div>

      {/* Over the plot rather than over the whole box, so the value labels are
          never behind a fade and the scrollbar is not dimmed at both ends of
          itself. */}
      {(["older", "newer"] as const).map((side) => (
        <div
          key={side}
          aria-hidden="true"
          className={cn(
            "pointer-events-none absolute w-8 transition-opacity duration-200",
            side === "older"
              ? "bg-gradient-to-r from-card to-transparent"
              : "right-0 bg-gradient-to-l from-card to-transparent",
            more[side] ? "opacity-100" : "opacity-0",
          )}
          style={{
            top: PLOT_TOP,
            bottom: gutter,
            ...(side === "older" ? { left: AXIS_WIDTH } : {}),
          }}
        />
      ))}
    </div>
  );
}

export interface TrafficHistoryChartProps {
  history: TrafficHistory | undefined;
  range: TrafficRange;
  onRangeChange: (range: TrafficRange) => void;
  loading?: boolean;
  error?: unknown;
  onRetry?: () => void;
  /** Plot height. The dialog is shorter than the statistics card. */
  height?: string;
  className?: string;
}

/**
 * The plot, its range controls and its legend, without a card around it.
 *
 * Unwrapped on purpose: it is drawn inside a Card on the statistics page and
 * inside a Dialog beside a client's name, and a component that brought its own
 * heading would be fighting one of the two.
 *
 * The range on the line under the controls is read off the answer rather than
 * off the question, and that is not a detail. What comes back is not always
 * what was asked for: the server stops a window at today, caps it at what a
 * chart can draw, and cuts a client's window down to the days its rows are kept
 * for. Printing the request would tell a confident lie about the picture beside
 * it in every one of those cases.
 */
export function TrafficHistoryChart({
  history,
  range,
  onRangeChange,
  loading = false,
  error,
  onRetry,
  height = "h-[240px] sm:h-[300px]",
  className,
}: TrafficHistoryChartProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const format = usePeriodFormat(range.scale);
  const rows = React.useMemo(
    () => buckets(range.scale === "daily" ? history?.daily : history?.monthly),
    [history, range.scale],
  );

  const down = rows.reduce((sum, row) => sum + row.down, 0);
  const up = rows.reduce((sum, row) => sum + row.up, 0);
  // A window of nothing but zeroes is a real answer - a new panel, a client
  // that has never connected, a quarter somebody has scrolled back to - and it
  // is not the same as having no answer. It gets a message instead of a flat
  // axis, which reads as a chart that failed.
  const empty = rows.length === 0 || down + up === 0;

  const covered = rows.length
    ? t("chart.covers", {
        from: format(rows[0].period, true),
        to: format(rows[rows.length - 1].period, true),
      })
    : "";
  const counted = t(range.scale === "daily" ? "chart.dayCount" : "chart.monthCount", {
    count: rows.length,
  });

  /*
   * Whether the window is looking at a stretch that predates the records.
   *
   * The server already stops a window at the first day it has anything on, so
   * this can only be true when the whole window is older than that - which is
   * exactly the case where "no traffic in this period" is true and useless.
   */
  const recordsFrom = history?.earliest ?? "";
  const newest = rows.length ? rows[rows.length - 1].period : "";
  // Compared at the scale being drawn: "2026-06" is not before "2026-06-20",
  // it is the month that day falls in.
  const startsBefore = Boolean(
    recordsFrom &&
    newest &&
    newest < (range.scale === "daily" ? recordsFrom : recordsFrom.slice(0, 7)),
  );
  const recordsStart = recordsFrom
    ? new Intl.DateTimeFormat(i18n.language, { dateStyle: "medium", timeZone: "UTC" }).format(
        new Date(`${recordsFrom}T00:00:00Z`),
      )
    : "";

  return (
    <div className={cn("space-y-4", className)}>
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0 space-y-2">
          <TrafficRangeControls
            range={range}
            onRangeChange={onRangeChange}
            // Asking for anything before the first day on record can only come
            // back empty, so the calendar does not offer it.
            earliest={history?.earliest || undefined}
          />
          {rows.length ? (
            <p className="text-xs text-muted-foreground">
              <span className="font-medium text-foreground">{covered}</span>
              <span aria-hidden="true" className="mx-1.5">
                ·
              </span>
              <span>{counted}</span>
            </p>
          ) : null}
        </div>
        <div className="flex shrink-0 gap-6">
          <Readout
            swatch={DOWN_SWATCH}
            label={String(t("dashboard.download"))}
            value={formatBytes(down)}
          />
          <Readout
            swatch={UP_SWATCH}
            label={String(t("dashboard.upload"))}
            value={formatBytes(up)}
          />
        </div>
      </div>

      {error ? (
        <ErrorState variant="inline" error={error} onRetry={onRetry} />
      ) : loading ? (
        <Skeleton className={cn("w-full", height)} />
      ) : empty ? (
        /*
         * As tall as what it has to say, and no taller.
         *
         * It used to fill the plot's own height, on the reasoning that a card
         * should not change size when the answer does. What that produced was a
         * three hundred pixel dashed box with two lines of grey text floating in
         * the middle of it - the panel's largest single piece of nothing, and
         * easy to reach now that a range can be moved anywhere. The card is
         * allowed to shrink instead, which is the same empty state every other
         * card in the panel uses.
         */
        <EmptyState
          variant="inline"
          icon={ChartBar}
          title={String(t("chart.noData"))}
          description={String(
            // "Nothing here" and "nothing before here" are different answers,
            // and only the second one tells a reader where to look instead.
            startsBefore ? t("chart.recordsStart", { date: recordsStart }) : t("chart.noDataHint"),
          )}
          action={
            range.window !== null ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => onRangeChange({ ...range, window: null })}
              >
                {t(`chart.preset.${defaultPreset(range.scale).id}`)}
              </Button>
            ) : undefined
          }
        />
      ) : (
        <Plot
          rows={rows}
          scale={range.scale}
          format={format}
          height={height}
          label={`${String(t("chart.traffic"))}. ${String(t(`chart.scale.${range.scale}`))}. ${covered}`}
        />
      )}
    </div>
  );
}
