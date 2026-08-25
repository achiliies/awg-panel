import * as React from "react";
import { Link } from "react-router-dom";
import { ArrowRight, TrendDown, TrendUp, type Icon } from "@/lib/icons";

import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/*
 * One number with the words that make it mean something.
 *
 * The label always renders, including while the value is loading, so the four
 * cards never collapse into a row of anonymous grey blocks and a screen reader
 * is told what it is waiting for rather than just "busy".
 *
 * Callers pass finished strings: this component neither formats bytes nor
 * translates. It has no idea what it is showing, which is why the Dashboard can
 * use it for a client count and a transfer rate without a variant for each.
 */

export type StatTrendDirection = "up" | "down" | "flat";

/**
 * Whether the movement is good news. Most numbers on this panel are neither:
 * traffic going up is just traffic going up, so "neutral" is the default and
 * colour is spent only where direction really means better or worse.
 */
export type StatTrendTone = "neutral" | "positive" | "negative";

export interface StatTrend {
  direction: StatTrendDirection;
  /** Already translated and formatted, e.g. "18% more than yesterday". */
  label: string;
  tone?: StatTrendTone;
}

export interface StatCardProps {
  /** Sentence case, no trailing colon. */
  label: string;
  /** The headline figure, pre-formatted. */
  value: React.ReactNode;
  /** One line under the value: the denominator, a split, a timestamp. */
  sub?: React.ReactNode;
  trend?: StatTrend;
  icon?: Icon;
  /** Swaps the value and sub-line for placeholders; the label stays. */
  loading?: boolean;
  /** Turns the whole card into a link to the page that owns this number. */
  to?: string;
  className?: string;
}

const TREND_ICON: Record<StatTrendDirection, Icon> = {
  up: TrendUp,
  down: TrendDown,
  flat: ArrowRight,
};

const TREND_TONE: Record<StatTrendTone, string> = {
  neutral: "text-muted-foreground",
  positive: "text-success",
  negative: "text-destructive",
};

export function StatCard({
  label,
  value,
  sub,
  trend,
  icon: Icon,
  loading = false,
  to,
  className,
}: StatCardProps): JSX.Element {
  const TrendIcon = trend ? TREND_ICON[trend.direction] : null;

  const card = (
    <Card
      aria-busy={loading || undefined}
      className={cn(
        "h-full p-5 transition-colors sm:p-6",
        // Only a linked card reacts to the pointer; a static one that lit up
        // would promise an action it does not have.
        to && "group-hover:border-primary/50 group-focus-visible:border-primary/50",
        className,
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm font-medium leading-tight text-muted-foreground">{label}</p>
        {Icon ? (
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
            <Icon weight="duotone" className="h-4 w-4" aria-hidden="true" />
          </span>
        ) : null}
      </div>

      {loading ? (
        <Skeleton className="mt-3 h-9 w-28" />
      ) : (
        // Proportional figures: the page sets tabular-nums for columns that must
        // not jitter, but at display size a tabular "121" reads loose and gappy.
        <p className="mt-3 text-3xl font-semibold leading-none tracking-tight proportional-nums">
          {value}
        </p>
      )}

      {loading ? (
        <Skeleton className="mt-3 h-4 w-36" />
      ) : sub ? (
        <div className="mt-2 text-sm leading-snug text-muted-foreground">{sub}</div>
      ) : null}

      {!loading && trend && TrendIcon ? (
        <p
          className={cn(
            "mt-3 inline-flex items-center gap-1.5 text-xs font-medium",
            TREND_TONE[trend.tone ?? "neutral"],
          )}
        >
          {/* 14px, and the only thing carrying the direction: bold, or the
              stroke disappears into the label beside it. */}
          <TrendIcon weight="bold" className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          {trend.label}
        </p>
      ) : null}
    </Card>
  );

  if (!to) {
    return card;
  }

  return (
    <Link
      to={to}
      className="group block rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
    >
      {card}
    </Link>
  );
}
