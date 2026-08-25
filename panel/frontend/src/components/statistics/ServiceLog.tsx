import * as React from "react";
import { useTranslation } from "react-i18next";
import { ArrowsClockwise, TerminalWindow, Tray } from "@/lib/icons";

import { CopyButton } from "@/components/CopyButton";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useServiceLog } from "@/api/hooks";
import { LOG_SOURCES, type JournalLine, type LogSource } from "@/api/types";
import { cn } from "@/lib/utils";

/*
 * What the services have been saying about themselves, out of journald.
 *
 * The other half of the page's answer, and the one that is not the panel's own
 * record: the event log says a save happened and the tunnel would not come back
 * up, and this is where the sentence awg-quick printed while failing is. It is
 * the same text `sudo awg-panel logs` prints, which is the point - the moment
 * somebody needs it is the moment the panel may be the only thing they can
 * still reach.
 *
 * A transcript, so it is drawn in the order it was written and in a monospace
 * block: a traceback is several lines that mean one thing, and reversing them or
 * reflowing them would destroy the only thing worth reading. The newest is at
 * the bottom, and the box opens scrolled to it.
 *
 * Not polled. Every read forks journalctl, and a log that reprinted itself every
 * few seconds would also scroll out from under whoever was reading it - so it is
 * fetched when the tab opens and again when the button is pressed.
 */

/** How many lines the box asks for. The API caps this at a thousand. */
const LINE_CHOICES = [100, 200, 500, 1000] as const;

const DEFAULT_LINES = 200;

/**
 * Syslog levels, coloured only where the colour means something.
 *
 * 3 and below is err, crit, alert and emerg - all of them "this did not work".
 * 4 is warning. Everything from 5 down to debug is the service talking about
 * its ordinary day, and colouring that would leave the block a wall of noise
 * with nothing standing out of it.
 */
function toneOf(priority: number): string {
  if (priority <= 3) {
    return "text-destructive";
  }
  if (priority === 4) {
    return "text-warning";
  }
  return "text-foreground/80";
}

/** "14:03:22" in the reader's own zone, or spaces where there was no timestamp. */
function clockOf(iso: string, locale: string): string {
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) {
    return "";
  }
  return new Intl.DateTimeFormat(locale, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(at));
}

/** The whole block as text, for the copy button and for pasting into a report. */
function asText(lines: JournalLine[], locale: string): string {
  return lines.map((line) => `${clockOf(line.at, locale)} ${line.message}`.trim()).join("\n");
}

export function ServiceLog(): JSX.Element {
  const { t, i18n } = useTranslation();
  const [source, setSource] = React.useState<LogSource>("panel");
  const [lines, setLines] = React.useState<number>(DEFAULT_LINES);

  const log = useServiceLog(source, lines);
  const box = React.useRef<HTMLDivElement>(null);
  // Memoised so that the fallback is not a fresh array on every render, which
  // would make the scroll-to-bottom effect below fire on renders where nothing
  // arrived - and would drag the box back down under a reader who had scrolled
  // up to look at something.
  const rows = React.useMemo(() => log.data?.lines ?? [], [log.data]);

  // The newest entry is the one somebody came for, and it is at the bottom.
  // Jumping there after every answer - not only the first - is what makes the
  // refresh button useful: a reload that left the box where it was would look
  // like nothing had arrived.
  React.useEffect(() => {
    const node = box.current;
    if (node && rows.length > 0) {
      node.scrollTop = node.scrollHeight;
    }
  }, [rows]);

  return (
    <Card className="flex flex-col">
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle>{t("serviceLog.title")}</CardTitle>
            <CardDescription className="mt-1.5">
              {log.data?.unit
                ? t("serviceLog.unitHint", { unit: log.data.unit })
                : t("serviceLog.subtitle")}
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            {rows.length > 0 ? (
              <CopyButton
                value={asText(rows, i18n.language)}
                label={String(t("serviceLog.copy"))}
                variant="ghost"
                size="sm"
              />
            ) : null}
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void log.refetch()}
              disabled={log.isFetching}
            >
              <ArrowsClockwise
                aria-hidden="true"
                className={cn(log.isFetching && "animate-spin")}
              />
              {t("common.refresh")}
            </Button>
          </div>
        </div>
      </CardHeader>

      <CardContent className="flex-1 space-y-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <Select value={source} onValueChange={(value) => setSource(value as LogSource)}>
            <SelectTrigger className="sm:w-56" aria-label={String(t("serviceLog.service"))}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {LOG_SOURCES.map((option) => (
                <SelectItem key={option} value={option}>
                  {t(`serviceLog.sources.${option}`)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={String(lines)} onValueChange={(value) => setLines(Number(value))}>
            <SelectTrigger className="sm:w-40" aria-label={String(t("serviceLog.lines"))}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {LINE_CHOICES.map((choice) => (
                <SelectItem key={choice} value={String(choice)}>
                  {t("serviceLog.lineCount", { count: choice })}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {log.isPending ? (
          <div className="space-y-2 rounded-md border border-border bg-muted/30 p-3">
            {Array.from({ length: 8 }, (_, index) => (
              <Skeleton key={index} className="h-3 w-full" />
            ))}
          </div>
        ) : log.isError ? (
          <ErrorState variant="inline" error={log.error} onRetry={() => void log.refetch()} />
        ) : !log.data?.available ? (
          <EmptyState
            variant="inline"
            icon={TerminalWindow}
            title={String(t("serviceLog.unavailable"))}
            description={log.data?.reason || String(t("serviceLog.unavailableHint"))}
          />
        ) : rows.length === 0 ? (
          <EmptyState
            variant="inline"
            icon={Tray}
            title={String(t("serviceLog.empty"))}
            description={String(t("serviceLog.emptyHint", { unit: log.data.unit }))}
          />
        ) : (
          /* One scroll of its own rather than a block that makes the page
             taller: the tab above it is a filter bar the reader keeps using,
             and a thousand lines would put it a long way off screen. */
          <div
            ref={box}
            tabIndex={0}
            role="log"
            aria-label={String(t("serviceLog.title"))}
            className={cn(
              "max-h-[26rem] overflow-auto rounded-md border border-border bg-muted/30 p-3",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            )}
          >
            <ol className="space-y-0.5 font-mono text-xs leading-relaxed">
              {rows.map((line, index) => (
                <li key={index} className="flex gap-3">
                  <span className="shrink-0 tabular-nums text-muted-foreground">
                    {clockOf(line.at, i18n.language)}
                  </span>
                  {/* Wrapped rather than scrolled sideways: a long line here is
                      a traceback or a command awg-quick echoed, and both are
                      read to the end. */}
                  <span
                    className={cn("min-w-0 whitespace-pre-wrap break-words", toneOf(line.priority))}
                  >
                    {line.message}
                  </span>
                </li>
              ))}
            </ol>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
