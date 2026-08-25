import * as React from "react";
import { useTranslation } from "react-i18next";
import { Circuitry, Lock, Network, Package, type Icon } from "@/lib/icons";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { bootstrap } from "@/api/client";
import { useServerStatus, useSession } from "@/api/hooks";
import { cn } from "@/lib/utils";

/*
 * What is running, in four facts.
 *
 * These were rows on the About page, which is the right place for the things
 * nobody reads twice - where the files live, what the shell tools are called -
 * and the wrong place for the four an operator glances at: which build is
 * running, whether the interface is up, whether the module underneath it is
 * loaded, and whether the browser reached the panel over TLS. Those are the
 * same kind of question as everything else on the dashboard, and the dashboard
 * had the room: the throughput chart draws at a fixed height beside a system
 * card half again as tall, so the foot of the left column was blank.
 *
 * Three of the four cost nothing to know. The panel's version and the scheme
 * the page arrived over are already in the document the server rendered, and
 * the interface comes in as a prop from the page, so this card cannot disagree
 * with the badge in the header above it. Only the module needs a request of its
 * own, and it is the one that shells out to `awg`: one call every thirty
 * seconds while this page is open, which is what the About and Server pages
 * already spend on the same answer.
 *
 * They read down the card one to a line, rather than as tiles two across. Two
 * across, each fact was a box whose label wrapped and whose reading sat under
 * it, and four boxes of shaded background made a block that had to be looked
 * at rather than read - four different shapes with nothing in a column. A list
 * puts every label at the same left edge and every reading at the same right
 * one, which is how the rest of the panel states a fact and its value.
 *
 * The order is the install, then the tunnel, then the connection: the build
 * that is serving the page, the two things it is serving the page about, and
 * last the one fact that is about this browser rather than about the server at
 * all. TLS was second while it looked like a fact of the same kind, and it
 * isn't one - a panel behind a proxy is reached over HTTPS by every browser
 * whatever the server thinks it is doing.
 *
 * Three of the four rows say nothing at all when nothing is wrong. A tunnel
 * that is up and a module that is loaded are the way every working server on
 * earth looks, and a card that prints "Up" and "Loaded" every second of every
 * day teaches the reader to skip the column those words are in - which is the
 * column the one day it says "Down" appears in. So the state is only ever
 * written when it is worth reading, and an operator can take a blank right-hand
 * side as the all-clear. HTTPS is the exception: on or off is the whole of what
 * that row has to say, so it says it either way.
 */

interface StateProps {
  /** Green when the state is the good one, red otherwise. */
  good?: boolean;
  children: React.ReactNode;
}

/*
 * A state in one word and one colour, which is how the Clients table has shown
 * one since it was written - and not a badge. Badges are for the thing an
 * operator acts on, and stacked down a card they read as buttons that do
 * nothing; worse, a badge and the version beside it carry the same weight, so
 * the eye has nothing to go to first.
 *
 * Colour is not the only carrier: the word says the state, and a row that has
 * nothing wrong with it prints no word at all rather than a green one.
 */
function State({ good = false, children }: StateProps): JSX.Element {
  return (
    <span
      className={cn(
        "shrink-0 text-sm font-semibold leading-none",
        good ? "text-success" : "text-destructive",
      )}
    >
      {children}
    </span>
  );
}

interface FactProps {
  icon: Icon;
  label: string;
  /**
   * The reading itself, in monospace. Omit it entirely for a fact that is only
   * a state - HTTPS is on or it is not, and there is no version of that.
   * Present but empty renders the "not available" wording rather than a blank.
   */
  value?: string | null;
  /** The state, on the rows and in the conditions that have one worth saying. */
  state?: React.ReactNode;
  loading?: boolean;
}

function Fact({ icon: Icon, label, value, state, loading = false }: FactProps): JSX.Element {
  const { t } = useTranslation();
  const shown = value?.trim() ?? "";

  return (
    <div className="flex flex-1 flex-wrap items-center justify-between gap-x-6 gap-y-2 py-3 first:pt-0 last:pb-0">
      <dt className="flex min-w-0 items-center gap-3">
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground">
          <Icon weight="duotone" className="h-4 w-4" aria-hidden="true" />
        </span>
        <span className="min-w-0 truncate text-sm font-medium">{label}</span>
      </dt>
      <dd className="flex min-w-0 flex-wrap items-center justify-end gap-x-3 gap-y-1">
        {loading ? (
          <Skeleton className="h-4 w-32" />
        ) : (
          <>
            {value === undefined ? null : (
              <span
                className={cn(
                  "min-w-0 break-all leading-none",
                  shown === "" ? "text-sm text-muted-foreground" : "font-mono text-xs tabular-nums",
                )}
              >
                {shown === "" ? t("common.notAvailable") : shown}
              </span>
            )}
            {state}
          </>
        )}
      </dd>
    </div>
  );
}

export interface InstallSummaryProps {
  /** The interface name the server config carries, already defaulted. */
  iface: string;
  /** Whether it is up, from whichever source the page settled on. */
  ifaceUp: boolean;
  className?: string;
}

export function InstallSummary({ iface, ifaceUp, className }: InstallSummaryProps): JSX.Element {
  const { t } = useTranslation();

  const session = useSession();
  const status = useServerStatus();

  // The session carries it once signed in; the bootstrap block carries it from
  // the first paint, and is what a page rendered before the session answers has
  // to go on.
  const version = session.data?.version ?? bootstrap.version;
  /*
   * Whether this browser is talking to the panel over TLS, which is a fact
   * about the connection rather than about the install: a panel behind a proxy
   * that terminates HTTPS is served plainly and is still reached securely, and
   * this says what the address bar says. Red when it is not, because a panel
   * reachable over plain HTTP is handing its session cookie to the network.
   */
  const secure = typeof window !== "undefined" && window.location.protocol === "https:";
  // Undefined while the request is in flight and after one that failed, and the
  // second of those is why it is not defaulted: a panel that cannot reach its
  // own API does not thereby know the module is missing.
  const moduleLoaded = status.data?.moduleLoaded;

  return (
    <Card className={cn("flex flex-col", className)}>
      <CardHeader>
        <CardTitle>{t("dashboard.whatsRunning")}</CardTitle>
      </CardHeader>
      <CardContent className="flex-1">
        <dl className="flex h-full flex-col divide-y divide-border">
          <Fact icon={Package} label={String(t("about.version"))} value={version} />
          <Fact
            icon={Network}
            label={String(t("about.iface"))}
            value={iface}
            state={ifaceUp ? undefined : <State>{t("status.down")}</State>}
          />
          <Fact
            icon={Circuitry}
            label={String(t("about.module"))}
            value={status.data?.moduleVersion ?? ""}
            loading={status.isPending}
            // Silent when it is loaded, and silent when the request failed as
            // well: not knowing is not the same as knowing it is missing, and
            // the reading beside it already reads "not available" in that case.
            state={moduleLoaded === false ? <State>{t("about.notLoaded")}</State> : undefined}
          />
          <Fact
            icon={Lock}
            label={String(t("settings.tls"))}
            state={<State good={secure}>{t(secure ? "common.on" : "common.off")}</State>}
          />
        </dl>
      </CardContent>
    </Card>
  );
}
