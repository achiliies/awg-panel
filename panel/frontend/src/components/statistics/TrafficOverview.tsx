import * as React from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowDown, ArrowUp, ArrowsDownUp, Broom, Users } from "@/lib/icons";

import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { TrafficHistoryChart } from "@/components/TrafficHistoryChart";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { useToast } from "@/components/ui/toast";
import {
  useClients,
  useEnforcementCatchup,
  useLiveFreshness,
  useLiveStats,
  useResetTraffic,
  useServerTraffic,
  useStatsSummary,
} from "@/api/hooks";
import { liveUsage, usageFloor, type UsageFloor, type UsageFloors } from "@/lib/usage";
import { type TrafficRange } from "@/lib/trafficRange";
import { cn, formatBytes, percent } from "@/lib/utils";
import type { Client, ClientStatus } from "@/api/types";

/*
 * What the tunnel has been doing, as opposed to what it is doing.
 *
 * The dashboard answers "is it working", on a two second clock, and everything
 * on it is about this moment. These two cards are the other question - how much
 * has gone through, and through whom - and they are the ones somebody opens
 * deliberately rather than glances at. They sat at the bottom of the dashboard
 * until this page existed, which meant the page that has to be readable at a
 * glance ended in two cards nobody glances at, and the history that is worth
 * scrolling through was in the one place a scroll was already spent.
 *
 * Both cards are on the same clock as the client list rather than the live
 * blob's: the collector settles today's row every ten seconds and every other
 * bucket was settled at midnight, so polling this every two seconds would be
 * fifteen times the requests to redraw the same picture.
 *
 * A component of its own rather than the body of the page, because the page has
 * three tabs now and only one of them is this. The tab that is not showing is
 * unmounted, so the polls below stop the moment somebody switches to the event
 * log - which is the whole reason the traffic view is not simply inlined there.
 *
 * It is also where the whole of it can be thrown away. That button belongs on
 * this tab and nowhere else: this is the one page that shows every traffic
 * figure the panel holds at once, so it is the one place where "remove all of
 * it" names something the reader is looking at rather than something they have
 * to be told the extent of. The confirmation still tells them, because the
 * clients page has totals on it too and they go as well.
 */

/** How many clients the busiest-clients card lists. */
const TOP_CLIENTS = 10;

/**
 * Presentation state for one peer, from the fields the clients endpoint returns.
 *
 * A client switched off because its date passed is disabled here, like one
 * switched off by hand: this page has no expiry column to put the difference in,
 * and a peer that is off is off whichever of the two did it.
 */
function statusOf(client: Client): ClientStatus {
  if (client.status) {
    return client.status;
  }
  if (!client.enabled) {
    return client.disabledReason === "quota" ? "quota" : "disabled";
  }
  return client.online ? "online" : "offline";
}

const DOT_CLASSES: Record<ClientStatus, string> = {
  online: "bg-success",
  idle: "bg-info",
  offline: "bg-muted-foreground/50",
  disabled: "border border-muted-foreground/70 bg-transparent",
  quota: "bg-warning",
};

interface TopClientRowProps {
  client: Client;
  /** This client's share of all traffic every client has moved, 0..100. */
  share: number;
}

function TopClientRow({ client, share }: TopClientRowProps): JSX.Element {
  const { t } = useTranslation();
  const status = statusOf(client);
  // An unnamed peer is legal in the config; identify it by key rather than by
  // an invented name, so a peer added by hand reads the same here as in the file.
  const label = client.name.trim() || client.publicKey.slice(0, 12);

  return (
    <li>
      <Link
        to={`/clients?q=${encodeURIComponent(client.name)}`}
        className="-mx-2 block rounded-md px-2 py-2 transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
      >
        <div className="flex items-baseline justify-between gap-3">
          <span className="flex min-w-0 items-center gap-2">
            <span
              aria-hidden="true"
              className={cn("h-2 w-2 shrink-0 rounded-full", DOT_CLASSES[status])}
            />
            <span className="sr-only">{t(`status.${status}`)}</span>
            <span className="truncate text-sm font-medium">{label}</span>
          </span>
          <span className="shrink-0 text-sm font-medium tabular-nums">
            {formatBytes(client.rxBytes + client.txBytes)}
          </span>
        </div>

        <Progress
          value={share}
          aria-hidden="true"
          className="mt-2 h-1.5 bg-chart-1/15"
          indicatorClassName="bg-chart-1"
        />

        <p className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          <span className="tabular-nums">{t("units.percent", { value: share.toFixed(1) })}</span>
          <span className="inline-flex items-center gap-1">
            <ArrowDown weight="bold" className="h-3 w-3" aria-hidden="true" />
            <span className="sr-only">{t("dashboard.download")}</span>
            <span className="tabular-nums">{formatBytes(client.txBytes)}</span>
          </span>
          <span className="inline-flex items-center gap-1">
            <ArrowUp weight="bold" className="h-3 w-3" aria-hidden="true" />
            <span className="sr-only">{t("dashboard.upload")}</span>
            <span className="tabular-nums">{formatBytes(client.rxBytes)}</span>
          </span>
        </p>
      </Link>
    </li>
  );
}

export function TrafficOverview(): JSX.Element {
  const { t } = useTranslation();
  const { toast } = useToast();
  const [clearing, setClearing] = React.useState(false);

  /*
   * The scale and the window, held here rather than in the chart, because they
   * are what the request is made of: the component below draws whatever it is
   * handed, and the page is what decides what to ask for. `null` is the span
   * the endpoint defaults to, which is what the first request of a visit uses
   * and what every other page opening this chart shares a cache entry with.
   */
  const [range, setRange] = React.useState<TrafficRange>({ scale: "daily", window: null });
  const traffic = useServerTraffic(range.window);

  // The busiest clients, asked for as such: the server ranks over the same rows
  // it would send, and sends ten.
  const clients = useClients({
    sort: "usage",
    direction: "desc",
    pageSize: TOP_CLIENTS,
  });
  // These rows are the one thing on this page that is about individual clients,
  // and their usage figures move on the collector's clock rather than on the
  // list's - so the live blob is asked for them by name, which is ten keys and
  // not the peer-per-client the whole blob holds.
  const topKeys = React.useMemo(
    () => (clients.data?.clients ?? []).map((client) => client.publicKey),
    [clients.data],
  );
  const live = useLiveStats({ peers: topKeys });
  const summary = useStatsSummary();

  const { fresh } = useLiveFreshness(live);
  // The rows below carry a usage figure on the blob's clock over a name and a
  // state on the list's, so the same catch-up the client table needs applies
  // here: a client switched off is one the ranking has stopped describing.
  useEnforcementCatchup(live);

  // Memoised because the fallback would otherwise be a fresh array on every
  // 2 s poll, which would re-rank the busiest clients for nothing.
  //
  // The ranking itself stays the server's, on the list's thirty second clock:
  // these are the biggest as of the last page, with their totals kept current
  // in between. Re-ordering them here every two seconds would make the list
  // swap rows under the cursor over a few kilobytes of difference, and the
  // order is not what somebody reads this card for.
  const floors = React.useRef<UsageFloors>(new Map());
  const clientList = React.useMemo(() => {
    const rows = clients.data?.clients ?? [];
    if (!fresh || !live.data) {
      return rows;
    }
    // Rebuilt from the rows being shown, so a client that has dropped out of
    // the ranking is not remembered for the life of the page.
    const next = new Map<string, UsageFloor>();
    const merged = rows.map((client) => {
      const [rxBytes, txBytes] = liveUsage(
        client,
        live.data.peers[client.publicKey],
        floors.current.get(client.publicKey),
      );
      next.set(client.publicKey, usageFloor(client, rxBytes, txBytes));
      return client.rxBytes === rxBytes && client.txBytes === txBytes
        ? client
        : { ...client, rxBytes, txBytes };
    });
    floors.current = next;
    return merged;
  }, [clients.data, live.data, fresh]);

  const total = clients.data?.totalAll ?? summary.data?.totalClients ?? 0;

  /*
   * The rows arrive ranked, so this only drops the ones with nothing to show
   * and works out each share.
   *
   * The denominator is the summary's total rather than a sum of these rows: the
   * share each row reports is its share of everything the server has carried,
   * and summing the visible rows would make ten clients account for 100% of a
   * server where they account for a fifth of it.
   */
  const ranked = React.useMemo(() => {
    const allBytes = (summary.data?.totalRx ?? 0) + (summary.data?.totalTx ?? 0);
    return {
      allBytes,
      rows: clientList
        .map((client) => ({ client, bytes: client.rxBytes + client.txBytes }))
        .filter((entry) => entry.bytes > 0),
    };
  }, [clientList, summary.data]);

  const reset = useResetTraffic();
  /*
   * Offered while there is a figure anywhere to take, which is deliberately not
   * the same question as whether this chart has bars in it. The history starts
   * the day the panel is upgraded to a version that records it, so a server
   * that has carried a terabyte can show an empty chart and still have every
   * client's all-time total sitting on the clients page - and that total is
   * most of what this button is for.
   *
   * Hidden rather than disabled on a server with genuinely nothing, because a
   * greyed-out destructive button invites the reader to work out what would
   * make it live, and the answer here is "carry some traffic first".
   */
  const anyTraffic =
    (summary.data?.totalRx ?? 0) + (summary.data?.totalTx ?? 0) > 0 ||
    (traffic.data?.daily ?? []).some((point) => point.rx > 0 || point.tx > 0);

  const removeEverything = (): void => {
    reset.mutate(undefined, {
      onSuccess: (result) => {
        setClearing(false);
        // The range control keeps whatever window it was on: the chart is empty
        // at every window now, and moving it would suggest the traffic might be
        // somewhere else in the series.
        toast({
          title: String(t("traffic.cleared")),
          description: String(
            t("traffic.clearedBody", {
              count: result.clients,
              days: result.serverDays,
            }),
          ),
          variant: "success",
        });
      },
      onError: (error) => {
        setClearing(false);
        toast({
          title: String(t("traffic.clearFailed")),
          description: error.detail,
          variant: "destructive",
        });
      },
    });
  };

  return (
    <div className="space-y-4">
      <Card className="flex flex-col">
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <CardTitle>{t("dashboard.trafficHistory")}</CardTitle>
              <CardDescription className="mt-1.5">
                {t("dashboard.trafficHistoryHint")}
              </CardDescription>
            </div>
            {anyTraffic ? (
              <Button
                variant="ghost"
                size="sm"
                className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                disabled={reset.isPending}
                onClick={() => setClearing(true)}
              >
                {reset.isPending ? <Spinner aria-hidden="true" /> : <Broom aria-hidden="true" />}
                {t("traffic.clear")}
              </Button>
            ) : null}
          </div>
        </CardHeader>
        <CardContent className="flex-1">
          <TrafficHistoryChart
            history={traffic.data}
            range={range}
            onRangeChange={setRange}
            loading={traffic.isPending}
            error={traffic.isError ? traffic.error : undefined}
            onRetry={() => void traffic.refetch()}
          />
        </CardContent>
      </Card>

      <Card className="flex flex-col">
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <CardTitle>{t("dashboard.topClients")}</CardTitle>
              <CardDescription className="mt-1.5">{t("dashboard.allTime")}</CardDescription>
            </div>
            <Button asChild variant="ghost" size="sm">
              <Link to="/clients">{t("dashboard.viewAll")}</Link>
            </Button>
          </div>
        </CardHeader>
        <CardContent className="flex-1">
          {clients.isPending ? (
            <ul className="grid gap-4 lg:grid-cols-2 lg:gap-x-10">
              {Array.from({ length: 4 }, (_, index) => (
                <li key={index} className="space-y-2">
                  <Skeleton className="h-4 w-40" />
                  <Skeleton className="h-1.5 w-full" />
                  <Skeleton className="h-3 w-32" />
                </li>
              ))}
            </ul>
          ) : clients.isError ? (
            <ErrorState
              variant="inline"
              error={clients.error}
              onRetry={() => void clients.refetch()}
            />
          ) : total === 0 ? (
            <EmptyState
              variant="inline"
              icon={Users}
              title={String(t("dashboard.noClients"))}
              description={String(t("dashboard.noClientsHint"))}
              action={
                <Button asChild size="sm">
                  <Link to="/clients">{t("dashboard.addClient")}</Link>
                </Button>
              }
            />
          ) : ranked.rows.length === 0 ? (
            <EmptyState
              variant="inline"
              icon={ArrowsDownUp}
              title={String(t("dashboard.noTraffic"))}
              description={String(t("dashboard.noTrafficHint"))}
            />
          ) : (
            /* Two columns where there is room for them. The list is ten rows
               now that the card has the page to itself, and ten stacked in
               one column is a scroll for something every row of which is one
               line of numbers. */
            <ul className="grid gap-1 lg:grid-cols-2 lg:gap-x-10">
              {ranked.rows.map((entry) => (
                <TopClientRow
                  key={entry.client.publicKey}
                  client={entry.client}
                  share={percent(entry.bytes, ranked.allBytes)}
                />
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {/* Every other destructive thing in the panel takes one client, one key
          or one log. This takes a number off every client on the server and the
          server's own record of what it has ever carried, and there is no copy
          of any of it anywhere - not in a backup, which holds the counters as
          they are now, and not in traffic.db, which is cleared with the rest.
          So the confirmation spells out all four things that go and the one
          thing that does not: the clients themselves, which keep working
          through it without noticing. */}
      <AlertDialog
        open={clearing}
        onOpenChange={(open) => {
          if (!open && !reset.isPending) {
            setClearing(false);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("traffic.clearConfirm")}</AlertDialogTitle>
            <AlertDialogDescription>{t("traffic.clearConfirmBody")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={reset.isPending}>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              className={cn(
                buttonVariants({ variant: "destructive" }),
                // Disabled while the request runs, but at full strength: dimming
                // it would hide the spinner that is the only sign of progress.
                reset.isPending && "cursor-progress disabled:opacity-100",
              )}
              disabled={reset.isPending}
              aria-busy={reset.isPending || undefined}
              onClick={(event) => {
                // The dialog closes itself on click, and a failure has to be
                // reportable - so it is held open until the request answers.
                event.preventDefault();
                removeEverything();
              }}
            >
              {reset.isPending ? <Spinner aria-hidden="true" /> : null}
              {t("traffic.clear")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
