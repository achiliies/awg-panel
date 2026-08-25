import * as React from "react";
import { useTranslation } from "react-i18next";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipProps,
} from "recharts";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { ErrorState } from "@/components/ErrorState";
import { cn, formatRate } from "@/lib/utils";
import type { LiveStats } from "@/api/types";

/*
 * Live throughput.
 *
 * There is no history endpoint behind this: the collector publishes one blob of
 * current rates, and the chart is the last N of those blobs kept in a ref. The
 * ref matters. Rebuilding the window from a query cache would make the series
 * identity change on every poll, and asking the API for two minutes of history
 * every two seconds would cost more than the tunnel it is measuring.
 *
 * Download and upload are overlaid, not stacked. They are two directions of the
 * same link, so their sum is not a quantity anyone reads; stacking would invent
 * one and put the upload series on a floating baseline nobody can compare.
 *
 * Naming follows the server's point of view exactly once, here: the collector
 * reports totalRateTx as what the server transmits, which is what clients
 * download. Everything below this line is already in the user's terms.
 */

/*
 * Series colours come from the theme's chart ramp, so they follow light and
 * dark without a branch here. Recharts paints SVG through props and cannot see
 * a Tailwind class, so each series carries both forms of the same token: the
 * utility for the DOM swatches, the raw expression for the plot.
 */
const DOWN_COLOR = "hsl(var(--chart-1))";
const UP_COLOR = "hsl(var(--chart-2))";
const DOWN_SWATCH = "bg-chart-1";
const UP_SWATCH = "bg-chart-2";

/** 60 samples at the collector's 2 s cadence is the last two minutes. */
const DEFAULT_WINDOW = 60;

/** Below this a chart is a single point or a straight line: say so instead. */
const MIN_POINTS = 2;

export interface ThroughputSample {
  /** Collector timestamp, Unix seconds. Doubles as the X value. */
  ts: number;
  /** Bytes per second the clients are downloading. */
  down: number;
  /** Bytes per second the clients are uploading. */
  up: number;
}

export interface ThroughputChartProps {
  /** The latest poll. A repeated `ts` is ignored, so a stalled collector does not flat-line the chart with fabricated points. */
  live: LiveStats | undefined;
  /** First load, before any blob has arrived. */
  loading?: boolean;
  error?: unknown;
  onRetry?: () => void;
  /** Points kept in the rolling window. */
  windowSize?: number;
  className?: string;
}

/**
 * Appends each distinct poll to a rolling buffer held in a ref, and mirrors it
 * into state so React re-renders. The ref is the buffer of record: state only
 * ever receives the array the ref already holds.
 */
function useRollingWindow(live: LiveStats | undefined, windowSize: number): ThroughputSample[] {
  const bufferRef = React.useRef<ThroughputSample[]>([]);
  const lastTsRef = React.useRef<number>(0);
  const [samples, setSamples] = React.useState<ThroughputSample[]>([]);

  React.useEffect(() => {
    // Re-rendering for some other reason, or reading a blob the collector has
    // not refreshed, must not add a point: the window is a clock, not a queue.
    if (!live || live.ts === lastTsRef.current) {
      return;
    }
    lastTsRef.current = live.ts;

    const next = bufferRef.current.concat({
      ts: live.ts,
      down: live.totalRateTx,
      up: live.totalRateRx,
    });
    bufferRef.current = next.length > windowSize ? next.slice(next.length - windowSize) : next;
    setSamples(bufferRef.current);
  }, [live, windowSize]);

  return samples;
}

/** Short clock time for the axis and the tooltip heading. */
function useClockFormat(): (unixSeconds: number) => string {
  const { i18n } = useTranslation();
  const formatter = React.useMemo(
    () =>
      new Intl.DateTimeFormat(i18n.language, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      }),
    [i18n.language],
  );
  return React.useCallback(
    (unixSeconds: number) => formatter.format(new Date(unixSeconds * 1000)),
    [formatter],
  );
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
  hint: string;
  value: string;
}

/**
 * Legend and current value in one row. Two series always need a legend, and the
 * number a reader wants most is the one happening now, so the legend carries it
 * rather than making them hover the right edge of the plot.
 */
function Readout({ swatch, label, hint, value }: ReadoutProps): JSX.Element {
  return (
    <div className="min-w-0">
      <p className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
        <SeriesKey swatch={swatch} />
        <span className="truncate">{label}</span>
      </p>
      <p className="mt-1 text-lg font-semibold leading-none tracking-tight proportional-nums">
        {value}
      </p>
      <p className="sr-only">{hint}</p>
    </div>
  );
}

/**
 * Every series at the hovered instant, value first: the reader already knows
 * which line they are on and came for the number.
 */
function ThroughputTooltip(props: TooltipProps<number, string>): JSX.Element | null {
  const { active, payload, label } = props;
  const { t } = useTranslation();
  const clock = useClockFormat();

  if (!active || !payload || payload.length === 0) {
    return null;
  }

  const rows = [
    { key: "down", swatch: DOWN_SWATCH, name: String(t("dashboard.download")) },
    { key: "up", swatch: UP_SWATCH, name: String(t("dashboard.upload")) },
  ];
  const stamp = typeof label === "number" ? label : 0;

  return (
    <div className="rounded-md border border-border bg-popover px-3 py-2 text-popover-foreground shadow-md">
      <p className="mb-1.5 text-xs text-muted-foreground">{stamp > 0 ? clock(stamp) : ""}</p>
      <ul className="space-y-1">
        {rows.map((row) => {
          const item = payload.find((entry) => entry.dataKey === row.key);
          const value = typeof item?.value === "number" ? item.value : 0;
          return (
            <li key={row.key} className="flex items-center gap-2 text-sm leading-tight">
              <SeriesKey swatch={row.swatch} />
              <span className="font-semibold tabular-nums">{formatRate(value)}</span>
              <span className="text-xs text-muted-foreground">{row.name}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function ThroughputChart({
  live,
  loading = false,
  error,
  onRetry,
  windowSize = DEFAULT_WINDOW,
  className,
}: ThroughputChartProps): JSX.Element {
  const { t } = useTranslation();
  const samples = useRollingWindow(live, windowSize);
  const clock = useClockFormat();
  // Two <linearGradient> definitions per chart instance; ids are global in SVG.
  const gradientId = React.useId().replace(/:/g, "");

  const latest = samples.length > 0 ? samples[samples.length - 1] : undefined;
  const down = latest?.down ?? live?.totalRateTx ?? 0;
  const up = latest?.up ?? live?.totalRateRx ?? 0;

  return (
    <Card className={cn("flex flex-col", className)}>
      <CardHeader className="gap-3">
        <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
          <div className="min-w-0">
            <CardTitle>{t("dashboard.throughput")}</CardTitle>
            <CardDescription className="mt-1.5">{t("dashboard.throughputHint")}</CardDescription>
          </div>
          <div className="flex shrink-0 gap-6">
            <Readout
              swatch={DOWN_SWATCH}
              label={String(t("dashboard.download"))}
              hint={String(t("dashboard.downloadHint"))}
              value={formatRate(down)}
            />
            <Readout
              swatch={UP_SWATCH}
              label={String(t("dashboard.upload"))}
              hint={String(t("dashboard.uploadHint"))}
              value={formatRate(up)}
            />
          </div>
        </div>
      </CardHeader>

      <CardContent className="flex-1">
        {error ? (
          <ErrorState variant="inline" error={error} onRetry={onRetry} />
        ) : loading && samples.length === 0 ? (
          <Skeleton className="h-[220px] w-full sm:h-[260px]" />
        ) : samples.length < MIN_POINTS ? (
          <div className="flex h-[220px] w-full flex-col items-center justify-center rounded-lg border border-dashed border-border bg-muted/30 px-4 text-center sm:h-[260px]">
            <Spinner size="sm" className="text-muted-foreground" decorative />
            <p className="mt-3 text-sm font-medium">{t("dashboard.throughputWaiting")}</p>
            <p className="mt-1 max-w-xs text-xs leading-relaxed text-muted-foreground">
              {t("dashboard.throughputWaitingHint")}
            </p>
          </div>
        ) : (
          <div
            role="img"
            aria-label={`${String(t("dashboard.throughput"))}. ${String(t("dashboard.throughputHint"))}`}
            className="h-[220px] w-full sm:h-[260px]"
          >
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={samples} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                <defs>
                  {/* A wash under each line, not a block: the fill hints at
                      volume while the 2px stroke carries the value. */}
                  <linearGradient id={`${gradientId}-down`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={DOWN_COLOR} stopOpacity={0.18} />
                    <stop offset="100%" stopColor={DOWN_COLOR} stopOpacity={0.01} />
                  </linearGradient>
                  <linearGradient id={`${gradientId}-up`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={UP_COLOR} stopOpacity={0.18} />
                    <stop offset="100%" stopColor={UP_COLOR} stopOpacity={0.01} />
                  </linearGradient>
                </defs>

                {/* Recharts styles its SVG through props; these objects are the
                    only way to reach the axis text and the grid stroke. */}
                <CartesianGrid vertical={false} stroke="hsl(var(--border))" />
                <XAxis
                  dataKey="ts"
                  type="number"
                  domain={["dataMin", "dataMax"]}
                  tickFormatter={clock}
                  tickLine={false}
                  axisLine={false}
                  minTickGap={64}
                  tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }}
                />
                <YAxis
                  width={72}
                  domain={[0, "auto"]}
                  tickFormatter={(value: number) => formatRate(value, 0)}
                  tickLine={false}
                  axisLine={false}
                  tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }}
                />
                <Tooltip
                  content={<ThroughputTooltip />}
                  cursor={{ stroke: "hsl(var(--muted-foreground))", strokeWidth: 1 }}
                />

                <Area
                  type="monotone"
                  dataKey="down"
                  stroke={DOWN_COLOR}
                  strokeWidth={2}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  fill={`url(#${gradientId}-down)`}
                  // The window slides once every two seconds. Re-animating the
                  // whole path each time reads as a flicker, not as motion.
                  isAnimationActive={false}
                  dot={false}
                  activeDot={{ r: 4, strokeWidth: 2, stroke: "hsl(var(--card))" }}
                />
                <Area
                  type="monotone"
                  dataKey="up"
                  stroke={UP_COLOR}
                  strokeWidth={2}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  fill={`url(#${gradientId}-up)`}
                  isAnimationActive={false}
                  dot={false}
                  activeDot={{ r: 4, strokeWidth: 2, stroke: "hsl(var(--card))" }}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
