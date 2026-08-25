import * as React from "react";
import { useTranslation } from "react-i18next";
import {
  ArrowsClockwise,
  Broom,
  CaretLeft,
  CaretRight,
  Funnel,
  ListDots,
  MagnifyingGlass,
  X,
} from "@/lib/icons";

import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
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
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { useToast } from "@/components/ui/toast";
import { useClearEvents, useEvents } from "@/api/hooks";
import { EVENT_CATEGORIES, type EventCategory, type PanelEvent } from "@/api/types";
import { useDebounced } from "@/lib/debounce";
import { settingLabel, settingValue } from "@/lib/settingLabels";
import { MBIT, cn, formatDateTime, formatDuration, relativeParts } from "@/lib/utils";

/*
 * The panel's own log: what was done to this server, and by whom.
 *
 * Every row is a kind and a few named values, and the sentence is assembled
 * here - see apps.events.kinds for why the server stores neither. That has one
 * consequence worth stating up front: a kind this build has no string for is
 * drawn as its own name rather than skipped. A panel reading a database written
 * by a newer one shows "client.something-new" against the right time and the
 * right actor, which is a worse line than the translated one and an immeasurably
 * better one than a blank row or a crash.
 *
 * Who did it is drawn under the sentence rather than inside it, and that is a
 * deliberate constraint on the catalog. Half of these events have no actor at
 * all - the collector switching a client off acts on nobody's behalf - so a
 * sentence built around a name would need a second phrasing for every kind, in
 * every language, to say the same thing about the panel itself.
 *
 * The one thing that can be done from here rather than read is emptying the
 * log, which is why this card carries a confirmation and the two beside it do
 * not. Traffic is measurements and the journal is journald's; this is the only
 * list in the panel whose rows exist nowhere else, so the dialog spells out that
 * they are not coming back - and the server leaves one row behind saying who
 * pressed it, which is what keeps a cleared log honest.
 */

/** How long the search box waits after the last keystroke, in milliseconds. */
const SEARCH_DEBOUNCE_MS = 300;

/** Rows per page. Fixed, unlike the client list - see the pager at the foot. */
const PAGE_SIZE = 25;

/** What the API writes in front of a token's name in the actor column. */
const API_ACTOR_PREFIX = "api:";

/** The severity filter, as the select trades in it. */
type SeverityFilter = "all" | "warning";

/** The category filter, which is the four the API knows plus "no filter". */
type CategoryFilter = "all" | EventCategory;

const SEVERITY_DOT: Record<string, string> = {
  info: "bg-muted-foreground/40",
  warning: "bg-warning",
};

/**
 * The values the sentence for this event may interpolate.
 *
 * `detail` is spread first so that a kind can carry a value of its own under any
 * name, and `target` is given a name of its own because it is the one value
 * every kind has in the same place.
 *
 * What follows the spread is the detail said in words rather than as it is
 * stored. `fields` and `keys` are lists, and i18next interpolating a list gives
 * "a,b,c" - no spaces, no translation, and the API's own spelling in the middle
 * of an English sentence. The rest are numbers in the unit the API keeps them
 * in: bits per second for a limit an operator set in megabits, seconds for a
 * window they chose as "30 days". Each is rendered by a helper below, and each
 * comes out empty when this kind carries nothing under that name - which is
 * every kind but the one or two whose sentence asks for it.
 */
function sentenceParams(
  t: ReturnType<typeof useTranslation>["t"],
  event: PanelEvent,
): Record<string, unknown> {
  const detail = event.detail;
  return {
    ...detail,
    target: event.target,
    fields: fieldNames(t, detail.fields),
    keys: settingChanges(t, detail.keys, detail.values),
    down: megabits(t, detail.down),
    up: megabits(t, detail.up),
    life: lifetime(detail.seconds),
    device: deviceName(t, detail),
    // Set last, so a kind that happens to carry a `context` of its own in the
    // detail cannot pick a sentence by accident.
    context: sentenceContext(event),
  };
}

/**
 * The i18next context for this event, or undefined for the kinds that have one
 * sentence - which is most of them.
 *
 * The catalog is one line per kind where one line is the truth, and a branch
 * wherever the same kind covers two things an admin would not have called the
 * same thing. Renaming a token and turning its renewal switch on are both
 * `auth.token-updated`, and "Changed the API token deploy-v2" throws away the
 * only fact worth keeping about a rename: what the thing used to be called, and
 * therefore which row in anybody's memory it is. A save of the server config
 * that dropped every session and one that changed a comment are both
 * `server.saved`. A sweep that took the expired clients and one that took the
 * switched-off ones are both `client.bulk-deleted`.
 *
 * Every branch is taken on a value the API already records, and every one of
 * them falls back to the plain sentence: a detail written before the panel
 * recorded that value, and a build with no string for the context, both end up
 * on the line this kind has always drawn. So none of this can leave a row blank,
 * which is the constraint the whole catalog is built under.
 */
function sentenceContext(event: PanelEvent): string | undefined {
  const detail = event.detail;
  switch (event.kind) {
    case "auth.token-updated":
      return typeof detail.name === "string" && detail.name !== "" ? "renamed" : undefined;

    // A token that never expires is the plain sentence; one that does is worth
    // saying so, and whether it renews itself decides whether that date means
    // anything at all a year later.
    case "auth.token-created":
      if (!(Number(detail.seconds) > 0)) {
        return undefined;
      }
      return detail.renew === true ? "renews" : "expires";

    case "auth.signed-in":
      return detail.totp === true ? "totp" : undefined;

    // Which attempt this was, out of how many the address gets. Both are
    // recorded together or not at all, so one check covers the pair.
    case "auth.sign-in-failed":
    case "auth.locked-out":
      return Number(detail.limit) > 0 ? "counted" : undefined;

    // The browser that was ended and where it was. Either half can be missing -
    // an agent nothing recognised, an address behind an untrusted proxy - so
    // there is a wording for each of the four ways this row can arrive.
    case "auth.session-revoked": {
      const named = typeof detail.browser === "string" && detail.browser !== "";
      const located = event.target !== "";
      if (named && located) {
        return "deviceAt";
      }
      return named ? "device" : located ? "at" : undefined;
    }

    // What the save cost, which is the half of it that is nowhere else. A save
    // that could not be applied comes first: it is the one that leaves the
    // running server disagreeing with the file, and saying it restarted the
    // tunnel on top of that would be worse than saying nothing.
    case "server.saved": {
      if (detail.applied === false) {
        return "notApplied";
      }
      const restart = detail.restart === true;
      const reimport = detail.reimport === true;
      if (restart && reimport) {
        return "restartReimport";
      }
      return restart ? "restart" : reimport ? "reimport" : undefined;
    }

    // The numbers are the whole of what this event is about, and zero is not a
    // missing value here - it is the operator taking every limit off. Which is
    // why a row without both of them falls back rather than reading one: absent
    // and zero mean opposite things, and "took the limit off every client" is
    // too strong a sentence to infer from a gap.
    case "client.bulk-limited": {
      const down = Number(detail.down);
      const up = Number(detail.up);
      if (!Number.isFinite(down) || !Number.isFinite(up)) {
        return undefined;
      }
      if (down > 0 && up > 0) {
        return "both";
      }
      return down > 0 ? "down" : up > 0 ? "up" : "none";
    }

    // Which sweep this was. An older row carries neither flag and stays on the
    // sentence it was written under.
    case "client.bulk-deleted": {
      const expired = detail.expired === true;
      const disabled = detail.disabled === true;
      if (expired && disabled) {
        return "both";
      }
      return expired ? "expired" : disabled ? "disabled" : undefined;
    }

    default:
      return undefined;
  }
}

/** "the quota and the expiry date", from the field names the API uses. */
function fieldNames(t: ReturnType<typeof useTranslation>["t"], value: unknown): string {
  if (!Array.isArray(value)) {
    return "";
  }
  return (
    value
      .filter((item): item is string => typeof item === "string")
      // A field this build has no name for is shown as the API's own spelling,
      // which is at least the word that appears in the request that changed it.
      .map((field) => (t(`events.fields.${field}`, { defaultValue: field }) as string) || field)
      .join(", ")
  );
}

/**
 * "Speed limits (off), Live update interval (5s)", from the settings a save moved.
 *
 * Two lists on the wire and one sentence here. `keys` names every setting that
 * changed and `values` carries `key=value` for the ones whose value the API is
 * willing to quote - never the secret path, never the address the panel answers
 * on, which is why this is a lookup rather than a pair of parallel lists walked
 * together. A setting with no quoted value keeps the name it has always had.
 *
 * Both halves are said the way the settings page says them, because that page is
 * where the admin has just been: `shaperOn` names nothing they have seen, and
 * being a key that ends in "On" it reads as saying shaping was switched on -
 * which is exactly the fact this line is supposed to settle.
 */
function settingChanges(
  t: ReturnType<typeof useTranslation>["t"],
  keys: unknown,
  values: unknown,
): string {
  if (!Array.isArray(keys)) {
    return "";
  }
  const quoted = settingPairs(values);
  return keys
    .filter((item): item is string => typeof item === "string")
    .map((key) => {
      const raw = quoted.get(key);
      const label = settingLabel(t, key);
      return raw === undefined ? label : `${label} (${settingValue(t, key, raw)})`;
    })
    .join(", ");
}

/** The `key=value` list the API records, as a lookup. Split on the first "=" only. */
function settingPairs(values: unknown): Map<string, string> {
  const pairs = new Map<string, string>();
  if (!Array.isArray(values)) {
    return pairs;
  }
  for (const item of values) {
    if (typeof item !== "string") {
      continue;
    }
    const split = item.indexOf("=");
    // Not `> -1`: an entry beginning with "=" names no setting, and an entry
    // with no "=" at all is not a pair. Neither can be written by this panel,
    // and both are cheaper to skip than to reason about later.
    if (split > 0) {
      pairs.set(item.slice(0, split), item.slice(split + 1));
    }
  }
  return pairs;
}

/** A speed in bit/s as the megabits it was set in, or "" when there is no limit. */
function megabits(t: ReturnType<typeof useTranslation>["t"], value: unknown): string {
  const bps = Number(value);
  if (!Number.isFinite(bps) || bps <= 0) {
    return "";
  }
  // Trimmed the way the client list trims it, so 1.5 Mbit/s does not arrive as
  // 1.500 and 20 Mbit/s does not arrive as 20.000.
  return `${Number((bps / MBIT).toFixed(3))} ${String(t("units.megabitsPerSecond"))}`;
}

/** "30d" from the window a token was given, or "" for one that never expires. */
function lifetime(value: unknown): string {
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds > 0 ? formatDuration(seconds) : "";
}

/**
 * "Firefox on Linux" for the browser an event is about, or "" for one nothing
 * recognised.
 *
 * The platform alone is not a device - "Ended Linux" says less than "ended
 * another signed-in browser" does - so it is only ever the second half of a
 * name, and an agent with no browser in it has no name here at all.
 */
function deviceName(
  t: ReturnType<typeof useTranslation>["t"],
  detail: Record<string, unknown>,
): string {
  const browser = typeof detail.browser === "string" ? detail.browser : "";
  const platform = typeof detail.platform === "string" ? detail.platform : "";
  if (!browser) {
    return "";
  }
  return platform ? String(t("settings.sessionDeviceOn", { browser, platform })) : browser;
}

interface EventRowProps {
  event: PanelEvent;
}

function EventRow({ event }: EventRowProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const key = `events.kinds.${event.kind}`;
  // exists() rather than a defaultValue, because a default has to be a string
  // and the fallback here is the kind itself - which is the one thing the
  // catalog cannot know and the row always has.
  const line = i18n.exists(key) ? (t(key, sentenceParams(t, event)) as string) : event.kind;

  const at = Date.parse(event.at);
  const stamp = Number.isFinite(at) ? formatDateTime(event.at, i18n.language) : "";

  return (
    <li className="flex items-start gap-3 py-2.5">
      <span
        aria-hidden="true"
        className={cn(
          "mt-1.5 h-2 w-2 shrink-0 rounded-full",
          SEVERITY_DOT[event.severity] ?? SEVERITY_DOT.info,
        )}
      />
      {event.severity === "warning" ? <span className="sr-only">{t("common.warning")}</span> : null}

      <div className="min-w-0 flex-1">
        <p className="text-sm leading-snug text-foreground">{line}</p>
        <p className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
          {/* The account, an API token, or the panel itself. An empty actor is a
              fact rather than a gap: nobody pressed anything. */}
          <Actor actor={event.actor} />
          {event.actorIp ? (
            <>
              <span aria-hidden="true">·</span>
              <span className="tabular-nums">{event.actorIp}</span>
            </>
          ) : null}
        </p>
      </div>

      <span className="shrink-0 whitespace-nowrap text-xs tabular-nums text-muted-foreground">
        <Ago iso={event.at} title={stamp} />
      </span>
    </li>
  );
}

/**
 * Who did it: an account, an API token, or the panel acting on nobody's behalf.
 *
 * The API writes a token's actor as "api:<name>", which is a spelling no account
 * can have - the username rules refuse a colon - so the prefix is a reliable
 * marker rather than a guess about somebody's choice of name. It is drawn as a
 * label and the name beside it, because "api:nightly backup" in the middle of a
 * log is a string to decode and "API · nightly backup" is a sentence.
 */
function Actor({ actor }: { actor: string }): JSX.Element {
  const { t } = useTranslation();

  if (!actor) {
    return <span className="italic">{t("events.byPanel")}</span>;
  }
  if (!actor.startsWith(API_ACTOR_PREFIX)) {
    return <span>{actor}</span>;
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="rounded bg-muted px-1.5 py-0.5 font-medium uppercase tracking-wide text-[10px] text-foreground/70">
        {t("events.byToken")}
      </span>
      {actor.slice(API_ACTOR_PREFIX.length)}
    </span>
  );
}

/** "2m ago", with the exact moment on hover. */
function Ago({ iso, title }: { iso: string; title: string }): JSX.Element {
  const { t } = useTranslation();
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) {
    return <span>{t("common.notAvailable")}</span>;
  }

  const { unit, value } = relativeParts(Math.floor(at / 1000));
  const label =
    unit === "now" || unit === "never"
      ? String(t("time.justNow"))
      : unit === "second"
        ? String(t("time.secondsAgo", { count: value }))
        : unit === "minute"
          ? String(t("time.minutesAgo", { count: value }))
          : unit === "hour"
            ? String(t("time.hoursAgo", { count: value }))
            : String(t("time.daysAgo", { count: value }));

  return <span title={title}>{label}</span>;
}

export function EventLog(): JSX.Element {
  const { t } = useTranslation();
  const { toast } = useToast();
  const [search, setSearch] = React.useState("");
  const [category, setCategory] = React.useState<CategoryFilter>("all");
  const [severity, setSeverity] = React.useState<SeverityFilter>("all");
  const [page, setPage] = React.useState(1);
  const [clearing, setClearing] = React.useState(false);

  const needle = useDebounced(search.trim(), SEARCH_DEBOUNCE_MS);

  // Any narrowing makes the current page number meaningless: page four of the
  // unfiltered log is not page four of the warnings in it, and landing on an
  // empty page reads as "there is nothing" rather than as "you are past the end".
  React.useEffect(() => {
    setPage(1);
  }, [needle, category, severity]);

  const events = useEvents({
    page,
    pageSize: PAGE_SIZE,
    q: needle || undefined,
    category: category === "all" ? undefined : category,
    severity: severity === "all" ? undefined : severity,
  });

  const rows = events.data?.events ?? [];
  const total = events.data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const narrowed = Boolean(needle) || category !== "all" || severity !== "all";

  const clear = useClearEvents();
  // Offered while there is something to take. A narrowed list that matches
  // nothing is not proof of an empty log - it is proof of a filter - and this
  // button is about the whole log either way, so it stays.
  const clearable = !events.isPending && !events.isError && (narrowed || total > 0);

  const emptyTheLog = (): void => {
    clear.mutate(undefined, {
      onSuccess: (removed) => {
        setClearing(false);
        // The log the pager was walking no longer exists; page four of it would
        // read as "there is nothing here" rather than as "you are past the end".
        setPage(1);
        toast({
          title: String(t("events.cleared")),
          description: String(t("events.clearedBody", { count: removed })),
          variant: "success",
        });
      },
      onError: (error) => {
        setClearing(false);
        toast({
          title: String(t("events.clearFailed")),
          description: error.detail,
          variant: "destructive",
        });
      },
    });
  };

  return (
    <Card className="flex flex-col">
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle>{t("events.title")}</CardTitle>
            <CardDescription className="mt-1.5">{t("events.subtitle")}</CardDescription>
          </div>
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void events.refetch()}
              disabled={events.isFetching || clear.isPending}
            >
              <ArrowsClockwise
                aria-hidden="true"
                className={cn(events.isFetching && "animate-spin")}
              />
              {t("common.refresh")}
            </Button>
            {clearable ? (
              <Button
                variant="ghost"
                size="sm"
                className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                disabled={clear.isPending}
                onClick={() => setClearing(true)}
              >
                {clear.isPending ? <Spinner aria-hidden="true" /> : <Broom aria-hidden="true" />}
                {t("events.clear")}
              </Button>
            ) : null}
          </div>
        </div>
      </CardHeader>

      <CardContent className="flex-1 space-y-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <div className="relative min-w-0 flex-1">
            <MagnifyingGlass
              className="pointer-events-none absolute start-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              type="text"
              inputMode="search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder={String(t("events.searchPlaceholder"))}
              aria-label={String(t("events.search"))}
              className="ps-9 pe-9"
            />
            {search ? (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="absolute end-0.5 top-1/2 h-8 w-8 -translate-y-1/2"
                aria-label={String(t("common.clear"))}
                onClick={() => setSearch("")}
              >
                <X aria-hidden="true" />
              </Button>
            ) : null}
          </div>

          <Select value={category} onValueChange={(value) => setCategory(value as CategoryFilter)}>
            <SelectTrigger className="sm:w-44" aria-label={String(t("events.category"))}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t("events.categories.all")}</SelectItem>
              {EVENT_CATEGORIES.map((option) => (
                <SelectItem key={option} value={option}>
                  {t(`events.categories.${option}`)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={severity} onValueChange={(value) => setSeverity(value as SeverityFilter)}>
            <SelectTrigger className="sm:w-44" aria-label={String(t("events.severity"))}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t("events.severities.all")}</SelectItem>
              <SelectItem value="warning">{t("events.severities.warning")}</SelectItem>
            </SelectContent>
          </Select>
        </div>

        {events.isPending ? (
          <ul className="divide-y divide-border">
            {Array.from({ length: 6 }, (_, index) => (
              <li key={index} className="space-y-2 py-3">
                <Skeleton className="h-4 w-3/5" />
                <Skeleton className="h-3 w-32" />
              </li>
            ))}
          </ul>
        ) : events.isError ? (
          <ErrorState variant="inline" error={events.error} onRetry={() => void events.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState
            variant="inline"
            icon={narrowed ? Funnel : ListDots}
            title={String(t(narrowed ? "events.noMatches" : "events.empty"))}
            description={String(t(narrowed ? "events.noMatchesHint" : "events.emptyHint"))}
            action={
              narrowed ? (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    setSearch("");
                    setCategory("all");
                    setSeverity("all");
                  }}
                >
                  {t("events.clearFilters")}
                </Button>
              ) : undefined
            }
          />
        ) : (
          <>
            <ul className="divide-y divide-border">
              {rows.map((event) => (
                <EventRow key={event.id} event={event} />
              ))}
            </ul>

            {/* Prev and next and a position, rather than the numbered pager the
                client list has. A log is walked backwards from now, not
                navigated: at twenty-five rows a page the table's ceiling is
                eight hundred pages, and a row of numbers over that many is a
                control that can only ever offer the first few and the last few
                - neither of which is where anybody is going. */}
            <nav
              aria-label={String(t("pagination.label"))}
              className="flex items-center justify-between gap-3 pt-1"
            >
              <p className="text-xs text-muted-foreground" aria-live="polite">
                {t("events.showing", {
                  from: (page - 1) * PAGE_SIZE + 1,
                  to: (page - 1) * PAGE_SIZE + rows.length,
                  total,
                })}
              </p>
              <div className="flex items-center gap-1">
                <Button
                  variant="outline"
                  size="sm"
                  className="w-8 px-0"
                  disabled={page <= 1}
                  onClick={() => setPage(page - 1)}
                  aria-label={String(t("pagination.previous"))}
                >
                  <CaretLeft aria-hidden="true" className="rtl:rotate-180" />
                </Button>
                <span className="px-2 text-xs tabular-nums text-muted-foreground">
                  {t("pagination.position", { page, pageCount })}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  className="w-8 px-0"
                  disabled={page >= pageCount}
                  onClick={() => setPage(page + 1)}
                  aria-label={String(t("pagination.next"))}
                >
                  <CaretRight aria-hidden="true" className="rtl:rotate-180" />
                </Button>
              </div>
            </nav>
          </>
        )}
      </CardContent>

      {/* The log is the only thing in the panel that cannot be reconstructed
          from anything else: a client comes back from a backup, a setting comes
          back from a form, and what was done last Tuesday comes back from
          nowhere. So the confirmation says what goes, says what does not, and
          says what will be left - one row naming whoever pressed this. */}
      <AlertDialog
        open={clearing}
        onOpenChange={(open) => {
          if (!open && !clear.isPending) {
            setClearing(false);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("events.clearConfirm")}</AlertDialogTitle>
            <AlertDialogDescription>
              {narrowed ? t("events.clearConfirmFiltered") : t("events.clearConfirmBody")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={clear.isPending}>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              className={cn(
                buttonVariants({ variant: "destructive" }),
                // Disabled while the request runs, but at full strength: dimming
                // it would hide the spinner that is the only sign of progress.
                clear.isPending && "cursor-progress disabled:opacity-100",
              )}
              disabled={clear.isPending}
              aria-busy={clear.isPending || undefined}
              onClick={(event) => {
                // The dialog closes itself on click, and a failure has to be
                // reportable - so it is held open until the request answers.
                event.preventDefault();
                emptyTheLog();
              }}
            >
              {clear.isPending ? <Spinner aria-hidden="true" /> : null}
              {t("events.clear")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  );
}
