import * as React from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  ArrowDown,
  ArrowsDownUp,
  ArrowUp,
  Clock,
  Plugs,
  Prohibit,
  SquaresFour,
  Users,
  WifiHigh,
} from "@/lib/icons";

import { PageHeader } from "@/components/PageHeader";
import { ErrorState } from "@/components/ErrorState";
import { InstallSummary } from "@/components/InstallSummary";
import { PowerButton } from "@/components/PowerButton";
import { RestartButton } from "@/components/RestartButton";
import { StatCard } from "@/components/StatCard";
import { SystemGauges } from "@/components/SystemGauges";
import { ThroughputChart } from "@/components/ThroughputChart";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  NO_PEERS,
  useClients,
  useEnforcementCatchup,
  useLiveFreshness,
  useLiveStats,
  useServer,
  useStatsSummary,
} from "@/api/hooks";
import {
  cn,
  formatBytes,
  formatDateTime,
  formatDuration,
  percent,
  relativeParts,
} from "@/lib/utils";

/*
 * The dashboard answers one question: is the tunnel doing its job right now.
 *
 * Everything here is derived from polls that also feed other pages, so opening
 * this page costs the server one file read every two seconds, and one call out
 * to `awg` every thirty for the module the install card names. No number on
 * this page is computed twice from two sources: each has a preferred origin and
 * a stated fallback, so a card can never disagree with the chart beside it.
 *
 * What is not here is the other half of the question. How much has gone through
 * over a quarter, and which clients moved it, are on the statistics page: they
 * are read deliberately rather than glanced at, they are the two things on this
 * screen worth scrolling for, and while they were at the bottom of this page
 * the page that has to be legible in one look ended in two cards that are not.
 * The all-time total stayed, as a card among the other totals, because it is
 * one number rather than a picture.
 */

/** The installer's interface name, and the default the whole project uses. */
const DEFAULT_IFACE = "awg0";

/**
 * Notices the page shows instead of, or above, live numbers. Calm on purpose:
 * a stopped tunnel is a state to explain, not an alarm to sound.
 */
interface NoticeProps {
  icon: typeof Prohibit;
  title: string;
  children: React.ReactNode;
  tone?: "warning" | "info";
  action?: React.ReactNode;
  className?: string;
}

function Notice({
  icon: Icon,
  title,
  children,
  tone = "warning",
  action,
  className,
}: NoticeProps): JSX.Element {
  return (
    <div
      role="status"
      className={cn(
        "rounded-lg border p-5 sm:p-6",
        tone === "warning" ? "border-warning/40 bg-warning/5" : "border-info/40 bg-info/5",
        className,
      )}
    >
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 gap-3">
          <span
            className={cn(
              "flex h-9 w-9 shrink-0 items-center justify-center rounded-md",
              tone === "warning" ? "bg-warning/15 text-warning" : "bg-info/15 text-info",
            )}
          >
            <Icon weight="duotone" className="h-5 w-5" aria-hidden="true" />
          </span>
          <div className="min-w-0 space-y-1.5">
            <p className="text-base font-medium leading-tight">{title}</p>
            <div className="space-y-1.5 text-sm leading-relaxed text-muted-foreground">
              {children}
            </div>
          </div>
        </div>
        {action ? <div className="flex shrink-0 flex-wrap gap-2">{action}</div> : null}
      </div>
    </div>
  );
}

interface SplitProps {
  icon: typeof ArrowDown;
  label: string;
  bytes: number;
  share: number;
}

/**
 * One direction of a traffic card's sub-line: an arrow, a figure and its share.
 *
 * The share used to be a two-segment bar, and the bar was the wrong instrument
 * for it. A stacked bar states a proportion of a known whole, which this is -
 * but the whole is already the number above it in 24px type, and both segments
 * are named directly underneath. The bar restated the two percentages a third
 * time, in the one channel a colour-blind reader cannot separate, and it was
 * the reason this had to be a card of its own instead of a figure among the
 * other figures.
 */
function Split({ icon: Icon, label, bytes, share }: SplitProps): JSX.Element {
  const { t } = useTranslation();

  return (
    <span className="inline-flex items-center gap-1">
      <Icon weight="bold" className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <span className="sr-only">{label}</span>
      <span className="tabular-nums">{formatBytes(bytes)}</span>
      <span className="text-xs tabular-nums text-muted-foreground/80">
        {t("units.percent", { value: share.toFixed(0) })}
      </span>
    </span>
  );
}

export default function Dashboard(): JSX.Element {
  const { t, i18n } = useTranslation();

  /*
   * One page of clients, for two figures no other endpoint carries: how many
   * addresses are still free, and the client count as the list itself counts
   * it. One row is asked for because no row is drawn here - the busiest-clients
   * card moved to the statistics page, and with it the only reason this page
   * ever wanted client rows.
   */
  const clients = useClients({ pageSize: 1 });
  // Every card here reads a total the blob already carries, so the peer block
  // is left out of the request entirely: on a server with a few thousand
  // clients that is most of a megabyte every two seconds per open tab.
  const live = useLiveStats({ peers: NO_PEERS });
  const summary = useStatsSummary();
  // Only for the interface name in the recovery instructions; it is cached and
  // never polled, so it costs one request per visit.
  const server = useServer();

  // A failing request is stale too, but the chart's own error state explains
  // that case properly; this banner is only for a collector that stopped
  // writing while the API kept serving its last blob - which is exactly the
  // distinction useLiveFreshness draws.
  const { stale, changedAt } = useLiveFreshness(live);
  // The counts below come off the client list on its thirty second clock, and a
  // client switched off by enforcement is one the list has stopped describing.
  useEnforcementCatchup(live);
  const staleParts = relativeParts(Math.floor(changedAt / 1000));
  const staleAgo =
    staleParts.unit === "minute"
      ? String(t("time.minutesAgo", { count: staleParts.value }))
      : staleParts.unit === "hour"
        ? String(t("time.hoursAgo", { count: staleParts.value }))
        : staleParts.unit === "day"
          ? String(t("time.daysAgo", { count: staleParts.value }))
          : String(t("time.secondsAgo", { count: staleParts.value }));

  const online = live.data?.online ?? summary.data?.onlineClients ?? 0;
  // Both of these are counts over every client, so they come from the endpoint
  // that counts rather than from any page of rows. The list's own `totalAll`
  // answers the first as well and is used where it has arrived first, which on
  // a cold load it usually has.
  const total = clients.data?.totalAll ?? summary.data?.totalClients ?? 0;
  const disabled = summary.data?.disabledClients ?? 0;
  /*
   * How long the tunnel has been serving, which is not how long the box has
   * been on: a reboot resets both, a `awg-quick down && up` resets only this
   * one, and it is the second number that explains why everybody dropped ten
   * minutes ago. Host uptime keeps its place among the other host figures in
   * the system card.
   *
   * Only the collector can answer this - it is the process that watches the
   * interface - so there is no second source to fall back to. When it has not
   * reported, the card says so rather than borrowing the host's uptime, which
   * would be a different number wearing this one's label.
   *
   * Both ends of the subtraction come from the blob, so the figure is entirely
   * the server's arithmetic and a browser whose clock is off by an hour does
   * not read an hour of extra uptime. It advances on the poll rather than on a
   * timer, which also means it stops when the collector does - and the banner
   * above already explains that case.
   */
  const tunnelSince = live.data?.ifaceSince ?? 0;
  const tunnelUptime = tunnelSince > 0 ? Math.max(0, (live.data?.ts ?? 0) - tunnelSince) : 0;
  const todayDown = summary.data?.todayTx ?? 0;
  const todayUp = summary.data?.todayRx ?? 0;
  /*
   * Everything the tunnel has carried, from the same totals the client table
   * shows: traffic.db's counters, less any per-client usage reset, summed over
   * the clients that still exist. A client that was deleted took its history
   * with it, so this can read lower than the sum of the days on the statistics
   * page - the server's daily rows keep the bytes of clients that have since
   * been removed, and a counter cannot.
   *
   * Download and upload are named from the client's point of view, as they are
   * everywhere else in this UI: the server's rx is somebody's upload. The
   * remainder rather than its own percentage for the second of the two, so the
   * pair always reads as a hundred.
   */
  const allDown = summary.data?.totalTx ?? 0;
  const allUp = summary.data?.totalRx ?? 0;
  const allBytes = allDown + allUp;
  const downShare = percent(allDown, allBytes);
  const upShare = allBytes > 0 ? 100 - downShare : 0;

  // Assume up until something says otherwise: flashing "the tunnel is down"
  // during the first paint of every visit would train people to ignore it.
  const ifaceUp = live.data?.ifaceUp ?? summary.data?.ifaceUp ?? true;
  const iface = server.data?.iface ?? DEFAULT_IFACE;

  const upSince = tunnelSince > 0 ? new Date(tunnelSince * 1000).toISOString() : null;

  /*
   * The one thing this page can do to what it is describing.
   *
   * It used to sit on the server config page, beside the parameters, which is
   * where it belonged when it was the button that put an edit into force. It is
   * not that any more - a save restarts the interface itself - so what is left
   * of it is the answer to "everyone dropped and I do not know why", and that
   * question gets asked in front of this page rather than in front of a form.
   * Here it is one confirmation away from the numbers that prompted it, instead
   * of a page and a scroll away.
   *
   * Kept as quiet as an action that drops every session should be: outlined
   * rather than filled, at the far end of the header from the title, and behind
   * a dialog that says what it costs. The save banner keeps its own copy for
   * the one case where a save wrote the config and could not apply it.
   *
   * There is nothing to restart before there is a configuration to restart, and
   * the server endpoint answers 503 until then - so a box where the tunnel was
   * never set up gets no button rather than one that can only fail.
   *
   * The power button beside it is the other half: this page is also where
   * somebody comes to take the tunnel down on purpose, and where they come back
   * to put it up again. Restart goes while the tunnel is down, because there is
   * nothing to restart - starting it is what the button next door already says,
   * and offering both would leave two ways to do one thing, one of which drives
   * awg-quick behind systemd's back and leaves it believing the unit is still
   * stopped.
   */
  const tunnelActions = server.data ? (
    <div className="flex items-center gap-2">
      <PowerButton running={ifaceUp} />
      {ifaceUp ? <RestartButton /> : null}
    </div>
  ) : undefined;

  const retryAll = (): void => {
    void live.refetch();
    void clients.refetch();
    void summary.refetch();
  };

  // Nothing answered: a page of empty cards would look like a server with no
  // clients rather than a panel that cannot reach its own API.
  if (live.isError && clients.isError && summary.isError) {
    return (
      <>
        <PageHeader
          title={String(t("dashboard.title"))}
          description={String(t("dashboard.subtitle"))}
          icon={SquaresFour}
        />
        <ErrorState error={live.error} onRetry={retryAll} />
      </>
    );
  }

  const statsLoading = summary.isPending && live.isPending;

  return (
    <>
      <PageHeader
        title={String(t("dashboard.title"))}
        description={String(t("dashboard.subtitle"))}
        icon={SquaresFour}
        badge={
          !ifaceUp ? <Badge variant="warning">{t("dashboard.interfaceDown")}</Badge> : undefined
        }
        actions={tunnelActions}
      />

      <div className="space-y-4">
        {stale ? (
          <Notice icon={Plugs} tone="info" title={String(t("dashboard.liveStale"))}>
            <p>{t("dashboard.liveStaleHint", { ago: staleAgo })}</p>
          </Notice>
        ) : null}

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
          <StatCard
            label={String(t("dashboard.onlineClients"))}
            value={online}
            sub={t("dashboard.ofTotalClients", { count: total })}
            icon={WifiHigh}
            loading={statsLoading && clients.isPending}
            to="/clients"
          />
          <StatCard
            label={String(t("dashboard.totalClients"))}
            value={total}
            sub={
              disabled > 0
                ? t("dashboard.disabledCount", { count: disabled })
                : clients.data
                  ? t("clients.freeIps", { count: clients.data.freeIps })
                  : undefined
            }
            icon={Users}
            loading={clients.isPending && summary.isPending}
            to="/clients"
          />
          <StatCard
            label={String(t("dashboard.todayTraffic"))}
            value={formatBytes(todayDown + todayUp)}
            sub={
              <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="inline-flex items-center gap-1">
                  <ArrowDown weight="bold" className="h-3.5 w-3.5" aria-hidden="true" />
                  <span className="sr-only">{t("dashboard.download")}</span>
                  <span className="tabular-nums">{formatBytes(todayDown)}</span>
                </span>
                <span className="inline-flex items-center gap-1">
                  <ArrowUp weight="bold" className="h-3.5 w-3.5" aria-hidden="true" />
                  <span className="sr-only">{t("dashboard.upload")}</span>
                  <span className="tabular-nums">{formatBytes(todayUp)}</span>
                </span>
              </span>
            }
            icon={ArrowsDownUp}
            loading={summary.isPending}
          />
          <StatCard
            label={String(t("dashboard.tunnelUptime"))}
            value={
              upSince ? (
                formatDuration(tunnelUptime)
              ) : (
                // A dash would leave the operator guessing which of the two
                // things is missing, and both have a fix worth naming.
                <span className="text-muted-foreground">
                  {t(ifaceUp ? "dashboard.tunnelUptimeUnknown" : "dashboard.tunnelStopped")}
                </span>
              )
            }
            sub={
              upSince
                ? t("dashboard.uptimeSince", { date: formatDateTime(upSince, i18n.language) })
                : t(ifaceUp ? "dashboard.tunnelUptimeUnknownHint" : "dashboard.tunnelStoppedHint")
            }
            icon={Clock}
            loading={live.isPending}
          />
          {/* Beside the uptime rather than at the bottom of the page, because
              it is the same kind of thing as the four cards it now sits with:
              one number, already added up, that says how much this server has
              done. What it took to get here is the statistics page. */}
          <StatCard
            label={String(t("dashboard.totalTraffic"))}
            value={formatBytes(allBytes)}
            sub={
              <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <Split
                  icon={ArrowDown}
                  label={String(t("dashboard.download"))}
                  bytes={allDown}
                  share={downShare}
                />
                <Split
                  icon={ArrowUp}
                  label={String(t("dashboard.upload"))}
                  bytes={allUp}
                  share={upShare}
                />
              </span>
            }
            icon={ArrowsDownUp}
            loading={summary.isPending}
            to="/statistics"
          />
        </div>

        {/* The left column is two cards rather than one, and that is the whole
            reason the second exists. The chart draws at a fixed height and the
            system card is half again as tall, so a chart stretched to match it
            was a card two-thirds white space - the emptiest thing on the page,
            in the place the eye lands first. The install card takes what is
            left over, and grows or shrinks with it. */}
        <div className="grid gap-4 lg:grid-cols-3">
          <div className="flex flex-col gap-4 lg:col-span-2">
            {ifaceUp ? (
              <ThroughputChart
                live={live.data}
                loading={live.isPending}
                error={live.isError ? live.error : undefined}
                onRetry={() => void live.refetch()}
              />
            ) : (
              <Notice
                icon={Prohibit}
                title={String(t("dashboard.interfaceDown"))}
                action={
                  <Button asChild variant="outline" size="sm">
                    <Link to="/server">{t("dashboard.openServer")}</Link>
                  </Button>
                }
              >
                <p>{t("dashboard.interfaceDownHint", { iface })}</p>
                <p>{t("dashboard.interfaceDownMenu")}</p>
              </Notice>
            )}

            <InstallSummary className="flex-1" iface={iface} ifaceUp={ifaceUp} />
          </div>

          <SystemGauges system={live.data?.system} loading={live.isPending} />
        </div>
      </div>
    </>
  );
}
