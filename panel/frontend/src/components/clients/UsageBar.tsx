import { useTranslation } from "react-i18next";

import { Progress } from "@/components/ui/progress";
import { clamp, cn, formatBytes, percent } from "@/lib/utils";

/*
 * How much a client has moved, against its data limit when it has one.
 *
 * Colour is never the only signal. The percentage is tinted the same way the
 * bar is, and a client past its limit also says so in words, so the state
 * survives a colour-blind reader and a monochrome screenshot.
 *
 * Without a limit there is no bar at all: an unbounded meter would either
 * invent a maximum or sit near empty forever, and both read as information the
 * panel does not have.
 */

/** Where the bar turns amber. Early enough to act on before a client is cut off. */
const WARN_PERCENT = 80;

export interface UsageBarProps {
  /** Bytes counted against the limit: upload plus download, less anything reset. */
  used: number;
  /** Limit in bytes. 0 or less means unlimited. */
  quota: number;
  /** Table rows: tighter bar and smaller type. */
  compact?: boolean;
  className?: string;
}

export function UsageBar({ used, quota, compact = false, className }: UsageBarProps): JSX.Element {
  const { t } = useTranslation();
  const usedText = formatBytes(used);
  const limited = Number.isFinite(quota) && quota > 0;

  if (!limited) {
    return (
      <div className={cn("min-w-0", className)}>
        <p className={cn("font-medium tabular-nums", compact ? "text-sm" : "text-base")}>
          {usedText}
        </p>
        <p className="text-xs text-muted-foreground">{t("clients.quotaNone")}</p>
      </div>
    );
  }

  const filled = percent(used, quota);
  // The counter can outrun the limit between two enforcement passes, so "over"
  // is measured on the raw bytes rather than on the clamped bar value.
  const over = used > quota;
  const warn = !over && filled >= WARN_PERCENT;
  // Uncapped, so a client at 140 percent reads as 140 percent and not as 100.
  const exact = clamp((used / quota) * 100, 0, 9999);

  const label = String(t("clients.quotaUsedOf", { used: usedText, total: formatBytes(quota) }));

  return (
    <div className={cn("min-w-0 space-y-1", className)}>
      <div className="flex items-baseline justify-between gap-2">
        <span className={cn("min-w-0 truncate tabular-nums", compact ? "text-sm" : "text-sm")}>
          {label}
        </span>
        <span
          className={cn(
            "shrink-0 text-xs font-medium tabular-nums",
            over ? "text-destructive" : warn ? "text-warning" : "text-muted-foreground",
          )}
        >
          {t("units.percent", { value: exact.toFixed(0) })}
        </span>
      </div>

      <Progress
        value={filled}
        aria-label={label}
        className={cn(compact ? "h-1.5" : "h-2")}
        indicatorClassName={over ? "bg-destructive" : warn ? "bg-warning" : "bg-primary"}
      />

      {over ? (
        <p className="text-xs font-medium text-destructive">{t("clients.quotaExceeded")}</p>
      ) : null}
    </div>
  );
}
