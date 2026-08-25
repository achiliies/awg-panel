import * as React from "react";
import { useTranslation } from "react-i18next";

import { TrafficHistoryChart } from "@/components/TrafficHistoryChart";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { useClientTraffic } from "@/api/hooks";
import { ArrowCounterClockwise } from "@/lib/icons";
import { utcToday, type TrafficRange } from "@/lib/trafficRange";
import { formatBytes } from "@/lib/utils";
import type { Client, TrafficHistory, TrafficPoint } from "@/api/types";

/*
 * One client's traffic history, opened from its row menu.
 *
 * The question this answers is the one the table cannot: the Total column says
 * what a client has moved since it was created, which is a single number that
 * has been growing for months, and no amount of staring at it says whether the
 * client was busy last week or has not connected since March. Here that is the
 * shape of the chart.
 *
 * Three figures above the plot, and they are chosen to be the three the number
 * in the table cannot answer: today, this month, and the all-time total it can.
 * The last is included precisely because it is the row's own figure - it is what
 * the operator was looking at when they opened this, and leaving it out would
 * make them close the dialog to check whether the two agree.
 *
 * They will not always agree, and that is honest rather than a fault: the
 * all-time total is a counter kept in traffic.db since the client was created,
 * while the history is a record of days that the server sweeps up after
 * thirteen months. A client older than that has moved more than its history can
 * account for.
 *
 * Resetting is offered here as well as in the row menu, and it is the same
 * reset: this dialog is where an operator is actually looking at what they are
 * about to clear, so making them close it, find the row again and open a menu
 * would be asking them to act from memory. The button hands the client back to
 * the page rather than calling the endpoint itself, the way the bulk dialog
 * hands back a selection - one confirmation, worded once, wherever the reset
 * was started from. The dialog stays open behind it, so the three figures and
 * the chart empty out in place and the operator sees the reset happen rather
 * than being told it did.
 */

interface FigureProps {
  label: string;
  /** Null when the selected range does not reach the period this reports on. */
  value: string | null;
  loading: boolean;
}

function Figure({ label, value, loading }: FigureProps): JSX.Element {
  const { t } = useTranslation();

  return (
    <div className="min-w-0 rounded-lg border border-border bg-muted/30 px-3 py-2.5">
      <p className="truncate text-xs text-muted-foreground">{label}</p>
      {loading ? (
        <Skeleton className="mt-1.5 h-5 w-20" />
      ) : value === null ? (
        // Not a zero and not a dash. A zero would be a claim about today that
        // this answer does not contain, and a dash would leave the reader to
        // guess which of the two things went missing.
        <p className="mt-1.5 truncate text-xs text-muted-foreground">{t("chart.outsideRange")}</p>
      ) : (
        <p className="mt-1 truncate text-xl font-semibold tracking-tight tabular-nums">{value}</p>
      )}
    </div>
  );
}

/**
 * One named bucket out of a series, or null if the window does not hold it.
 *
 * By period rather than by position. It used to be "the last bucket", which was
 * the same thing back when every answer ended at today - it stopped being the
 * same thing the moment a range could end in March, and the figure labelled
 * "today" would have quietly become "the last day of whatever is on screen".
 */
function bucket(series: TrafficPoint[] | undefined, period: string): number | null {
  const point = series?.find((entry) => entry.period === period);
  return point ? point.rx + point.tx : null;
}

export interface ClientTrafficDialogProps {
  /** The client whose history to show, or null when the dialog is closed. */
  client: Client | null;
  onOpenChange: (open: boolean) => void;
  /** Ask the page to confirm and then reset this client's traffic. */
  onResetUsage: (client: Client) => void;
  /** Whether that reset is in flight, which is the page's to know. */
  resetting: boolean;
}

export function ClientTrafficDialog({
  client,
  onOpenChange,
  onResetUsage,
  resetting,
}: ClientTrafficDialogProps): JSX.Element {
  const { t } = useTranslation();
  const [range, setRange] = React.useState<TrafficRange>({ scale: "daily", window: null });
  // Keyed off the name, so the query is only made while a dialog is open and is
  // made again for the next client rather than showing the last one's history
  // under a new title.
  const traffic = useClientTraffic(client?.name ?? null, range.window);
  const history: TrafficHistory | undefined = traffic.data;
  const label = client ? client.name.trim() || client.publicKey.slice(0, 12) : "";

  // A range is a question about one client. Opening the next client's history
  // with the last one's March still selected would answer a question nobody
  // asked, so the dialog starts where it always starts; closing it resets too,
  // because `client` is null between one opening and the next.
  const name = client?.name ?? null;
  React.useEffect(() => {
    setRange({ scale: "daily", window: null });
  }, [name]);

  const today = utcToday();
  const todayBytes = bucket(history?.daily, today);
  const monthBytes = bucket(history?.monthly, today.slice(0, 7));

  return (
    <Dialog open={client !== null} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>{t("clients.history")}</DialogTitle>
          <DialogDescription>{t("clients.historyFor", { name: label })}</DialogDescription>
        </DialogHeader>

        <div className="grid grid-cols-3 gap-3">
          <Figure
            label={String(t("dashboard.todayTraffic"))}
            value={todayBytes === null ? null : formatBytes(todayBytes)}
            loading={traffic.isPending}
          />
          <Figure
            label={String(t("chart.thisMonth"))}
            value={monthBytes === null ? null : formatBytes(monthBytes)}
            loading={traffic.isPending}
          />
          <Figure
            label={String(t("dashboard.allTime"))}
            // From the row rather than from the series: it is the counter the
            // table is showing, and the two answer different questions. It is
            // also the one of the three a range cannot take away.
            value={formatBytes(client ? client.rxBytes + client.txBytes : 0)}
            loading={false}
          />
        </div>

        <TrafficHistoryChart
          history={history}
          range={range}
          onRangeChange={setRange}
          loading={traffic.isPending}
          error={traffic.isError ? traffic.error : undefined}
          onRetry={() => void traffic.refetch()}
          height="h-[220px] sm:h-[260px]"
        />

        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" variant="outline">
              {t("common.close")}
            </Button>
          </DialogClose>
          <Button
            type="button"
            // Nothing is reset from here: the click hands the client back and
            // the page puts the same question to the operator that the row menu
            // does. Guarded on the client only because the dialog renders once
            // with none while it animates shut.
            onClick={() => client && onResetUsage(client)}
            disabled={client === null}
            loading={resetting}
          >
            {resetting ? (
              <Spinner size="sm" decorative />
            ) : (
              <ArrowCounterClockwise aria-hidden="true" />
            )}
            {t("clients.resetUsage")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
