/* eslint-disable react-refresh/only-export-components -- the sort model and the
   row helpers describe this table; the page filters on the same derived status
   the rows show, and a second file would only let the two drift. */
import { useTranslation } from "react-i18next";
import {
  ArrowCounterClockwise,
  ArrowDown,
  ArrowsDownUp,
  ArrowUp,
  CaretDown,
  CaretUp,
  ChartBar,
  Clock,
  DotsThree,
  DownloadSimple,
  Key,
  Note,
  PencilSimple,
  Power,
  Prohibit,
  QrCode,
  Trash,
} from "@/lib/icons";

import { StatusDot } from "@/components/StatusDot";
import { UsageBar } from "@/components/clients/UsageBar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Spinner } from "@/components/ui/spinner";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useSettings } from "@/api/hooks";
import { cn, expiryParts, formatBytes, relativeParts } from "@/lib/utils";
import type { Client, ClientSortKey, ClientStatus } from "@/api/types";

/*
 * The client list, as a table from md up and as stacked cards below it.
 *
 * Both layouts render from the same row model and the same action menu, so a
 * phone and a laptop can do exactly the same things. The component is
 * presentational: it sorts what it is given and reports what was clicked. Every
 * confirmation, mutation and toast belongs to the page.
 *
 * Rows are keyed by public key rather than by name, because renaming a client
 * keeps its keys and its address: keying by name would throw away and rebuild
 * the row for what is really an edit.
 *
 * The table scrolls sideways rather than dropping columns once there are more
 * of them than fit, with the actions column pinned to the end of every row: a
 * QR code, an edit form and a delete confirmation that have scrolled out of
 * reach are worse than a column the operator has to scroll to see.
 */

export type { ClientSortKey };

export type SortDirection = "asc" | "desc";

export interface ClientSort {
  key: ClientSortKey;
  direction: SortDirection;
}

/**
 * Oldest first, which is the order the API already answers in: the list reads
 * as a history, and a client added a minute ago is at the bottom, where it was
 * appended to the config and where the operator who just added it watched it
 * appear.
 */
export const DEFAULT_CLIENT_SORT: ClientSort = { key: "created", direction: "asc" };

/** Expiry inside this many days is coloured rather than left as a quiet count. */
const EXPIRY_SOON_DAYS = 7;

/**
 * Presentation state for one peer, from the fields the clients endpoint returns.
 *
 * Never "expired", which the API no longer says either: the row answers "has
 * this client's date gone" one column to the right, in the words that question
 * deserves - a countdown, a time today, or Expired - and a status column
 * repeating the last of those would spend the only place on the row that can say
 * whether a dark client was switched off by hand or stopped by its data limit.
 * So a lapsed client reads as disabled here, which is what it is, and one an
 * admin has switched back on reads as whatever it is doing, which is also what
 * it is.
 *
 * This is the word the status filters read as well, so a client the table calls
 * disabled is a client "Disabled" finds. What "Expired" finds is the date, not
 * this - see isExpired on the page.
 */
export function clientStatus(client: Client): ClientStatus {
  if (client.status) {
    return client.status;
  }
  if (!client.enabled) {
    return client.disabledReason === "quota" ? "quota" : "disabled";
  }
  return client.online ? "online" : "offline";
}

/** An unnamed peer is legal in the config; identify it by the head of its key. */
export function clientLabel(client: Client): string {
  return client.name.trim() || client.publicKey.slice(0, 12);
}

/**
 * Whether this row is faded rather than drawn at full strength.
 *
 * A list of twenty peers is mostly peers doing nothing, and every one of them
 * is spelled out at the same weight as the two that are actually carrying
 * traffic - the same black name, the same address, the same total. The eye has
 * no way in, so finding the live clients means reading the status column top to
 * bottom and holding the answers.
 *
 * Fading the rest gives the page a foreground. Nothing is hidden and nothing
 * moves: a dimmed row is still readable, still sortable, still one click from
 * its own QR code. It has simply stopped competing for attention with the rows
 * that have something to say.
 *
 * The rule is the whole of it: online is drawn, everything else is faded. Idle,
 * offline, disabled and over quota all read as one thing here, because the
 * question this page is scanned for is which clients are on the tunnel right
 * now, and to that question every one of those four is a no. A rule with
 * exceptions in it would also be a rule the reader has to learn before the fade
 * tells them anything, and the value of a foreground is that it needs no
 * explaining.
 *
 * The states that want attention do not lose it, which is what makes that
 * affordable. Fading by opacity keeps every colour on the row as the colour it
 * was - see DIMMED below - so a disabled client's lamp and name are still the
 * only red in a column of greys, and an exhausted quota is still a red bar. A
 * client whose date has gone is one of these: enforcement switches it off, so
 * the row reads as disabled and fades with the rest while its expiry chip stays
 * the one red chip in that column. The alarm survives the fade; it is the
 * twenty ordinary rows around it that stop shouting.
 */
function isDimmed(status: ClientStatus): boolean {
  return status !== "online";
}

/*
 * The fade itself.
 *
 * Opacity rather than a muted text colour, because the row is not all text: it
 * carries an expiry chip, a usage bar and two arrow glyphs, and each of those
 * would need its own quiet variant to be dimmed by colour. Opacity fades the
 * whole cell at once, and keeps every one of those signals in its own hue on
 * the way down, so an over-quota bar on a sleeping client is still red.
 *
 * 60 percent is a compromise with the contrast floor the palette sets in
 * index.css. Fainter separates the live rows more sharply, but this text is
 * meant to stay readable - the operator who dims a row is often the one about
 * to go looking for its address.
 *
 * The transition is for the poll rather than for the first paint. Statuses flip
 * under a list that is already on screen, and a row that fades over a moment
 * reads as that client going quiet, where a row that changes between two frames
 * reads as the page having redrawn itself.
 */
const DIMMED = "opacity-60 transition-opacity duration-300";

/**
 * Ascending reads naturally for the columns that have a natural order - a name,
 * an address, a date. For the two that measure a magnitude the interesting end
 * is the top, so those open descending.
 */
function defaultDirection(key: ClientSortKey): SortDirection {
  return key === "usage" || key === "lastHandshake" ? "desc" : "asc";
}

/*
 * The comparisons themselves are in apps.clients.merge, which orders every
 * client on the server before cutting the page this table draws. There is no
 * copy of them here on purpose: two implementations of "busiest first" is two
 * places for it to mean something slightly different, and the one that decides
 * which rows reach the browser has to win.
 */

/*
 * The menu hands focus back to its trigger while it unmounts. Opening a dialog
 * in the same tick lets that restore land after the dialog has trapped focus,
 * which leaves the dialog open with nothing focused inside it. One turn of the
 * event loop puts the two in order.
 */
function afterMenuClose(run: () => void): void {
  window.setTimeout(run, 0);
}

/*
 * Every moment this table shows, in the reader's own locale and time zone. The
 * timestamps arrive in UTC, so a panel open in two countries shows each of them
 * the moment they would have named.
 */

/**
 * A "# Created" comment as the moment it names, or null if it is not one.
 *
 * Only the exact spelling this panel writes is accepted -
 * the same rule the server applies before copying one into a database column.
 * Anything else is somebody's hand edit, and the danger in a hand edit is not
 * that it fails to parse but that it succeeds: `new Date("2026-08-06 12:00")`
 * is a valid date, read in the *browser's* zone rather than in UTC, so a
 * comment written on the server came out hours off with nothing to show for
 * it. Rejected here, the cell says the date is unknown, which is true.
 *
 * The shape is checked and then the reading is checked back against it, because
 * matching the shape is not enough on its own: `new Date` rolls a date past the
 * end of its month over into the next one rather than refusing it, so a
 * "# Created = 2026-02-30T00:00:00Z" that never existed would otherwise have
 * been shown, quite confidently, as the second of March.
 */
const CREATED_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

function parseMoment(iso: string | null): Date | null {
  if (!iso || !CREATED_RE.test(iso)) {
    return null;
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime()) || `${date.toISOString().slice(0, 19)}Z` !== iso) {
    return null;
  }
  return date;
}

function formatDate(date: Date, locale: string): string {
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium" }).format(date);
}

function formatDateTime(date: Date, locale: string): string {
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

/** "18:30", or "6:30 PM" where that is how the language writes a clock. */
function formatTime(date: Date, locale: string): string {
  return new Intl.DateTimeFormat(locale, { timeStyle: "short" }).format(date);
}

/* -------------------------------------------------------------------------- */
/* Cells                                                                       */
/* -------------------------------------------------------------------------- */

interface NameCellProps {
  client: Client;
}

function NameCell({ client }: NameCellProps): JSX.Element {
  const { t } = useTranslation();
  const label = clientLabel(client);
  const note = client.note.trim();
  const email = client.email.trim();

  const name = (
    <span className="flex min-w-0 items-center gap-1.5">
      <span className="truncate font-medium">{label}</span>
      {note ? (
        <Note
          weight="bold"
          className="h-3.5 w-3.5 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
      ) : null}
    </span>
  );

  return (
    <div className="min-w-0">
      {note ? (
        <Tooltip>
          <TooltipTrigger asChild>
            {/* Focusable so the note is reachable without a pointer: the icon is
                the only hint that there is one. */}
            <span
              tabIndex={0}
              className="inline-flex max-w-full rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              {name}
              <span className="sr-only">
                {t("clients.note")}: {note}
              </span>
            </span>
          </TooltipTrigger>
          {/* A note runs to 2000 characters and keeps whatever line breaks
              were typed into it, so it is the one tooltip that can outgrow the
              screen. Keep those breaks, wrap the rest downwards, and give the
              remainder a scroll instead of letting the box keep growing. */}
          <TooltipContent className="max-h-[min(var(--radix-tooltip-content-available-height),12rem)] whitespace-pre-line">
            {note}
          </TooltipContent>
        </Tooltip>
      ) : (
        name
      )}
      {email ? <p className="truncate text-xs text-muted-foreground">{email}</p> : null}
    </div>
  );
}

interface AddressCellProps {
  client: Client;
}

/**
 * Both of a client's addresses, and a warning when its IPv6 is not going
 * through the tunnel.
 *
 * The warning is the point of the cell. A client whose config still routes only
 * IPv4 keeps working perfectly - it connects, it carries traffic, its counters
 * move - while everything it reaches over IPv6 travels outside the tunnel with
 * its own address on it. There is no symptom to notice, so the only place it
 * can be noticed is here, next to the address it should have had.
 *
 * It clears itself once that device re-imports the config the server has
 * already rewritten, which is also what the tooltip has to say: the fix is on
 * the device, not on the server.
 */
function AddressCell({ client }: AddressCellProps): JSX.Element {
  const { t } = useTranslation();

  return (
    <div className="min-w-0">
      <span className="block truncate">{client.ip}</span>
      {client.ip6 ? (
        <span className="block truncate text-muted-foreground" title={client.ip6}>
          {client.ip6}
        </span>
      ) : null}
      {client.leaksIpv6 ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <span
              tabIndex={0}
              className="mt-0.5 inline-flex rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              <Badge variant="warning">{t("clients.ipv6Leak")}</Badge>
            </span>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs">{t("clients.ipv6LeakHelp")}</TooltipContent>
        </Tooltip>
      ) : null}
    </div>
  );
}

/**
 * The address a peer is reaching the server from, without the port it is
 * reaching it on.
 *
 * `awg show` reports an endpoint as host and port together, and the port is the
 * least useful thing on the row: it is whatever ephemeral number the client's
 * NAT picked this time, it changes on its own, and it doubles the length of the
 * one column that has to hold an IPv6 address. What an operator is reading this
 * column for is where the client is - so the column shows that, and the whole
 * endpoint stays a hover away in the title.
 *
 * Two shapes to take apart, because the two families are written differently:
 * an IPv6 endpoint is bracketed, `[2001:db8::1]:51820`, precisely so its own
 * colons cannot be mistaken for the port separator. A bare address with several
 * colons and no brackets is therefore an IPv6 with no port on it at all, and is
 * returned whole rather than cut at its last group.
 */
function endpointHost(endpoint: string): string {
  if (endpoint.startsWith("[")) {
    const close = endpoint.indexOf("]");
    return close > 0 ? endpoint.slice(1, close) : endpoint;
  }
  const colon = endpoint.indexOf(":");
  return colon > 0 && colon === endpoint.lastIndexOf(":") ? endpoint.slice(0, colon) : endpoint;
}

interface LastSeenProps {
  /**
   * Unix seconds of the last thing heard from the peer - `client.lastSeen`, not
   * `client.lastHandshake`. The handshake is a key exchange and happens about
   * every two minutes on a client in constant use, so a column fed from it
   * spends most of its time claiming a working connection was last seen minutes
   * ago. 0 means the peer has never been heard from at all.
   */
  timestamp: number;
}

function LastSeen({ timestamp }: LastSeenProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const { unit, value } = relativeParts(timestamp);

  if (unit === "never") {
    return <span className="text-muted-foreground">{t("clients.neverConnected")}</span>;
  }

  const text =
    unit === "now"
      ? String(t("time.justNow"))
      : unit === "second"
        ? String(t("time.secondsAgo", { count: value }))
        : unit === "minute"
          ? String(t("time.minutesAgo", { count: value }))
          : unit === "hour"
            ? String(t("time.hoursAgo", { count: value }))
            : String(t("time.daysAgo", { count: value }));

  return (
    <span
      className="tabular-nums"
      title={formatDateTime(new Date(timestamp * 1000), i18n.language)}
    >
      {text}
    </span>
  );
}

interface RateCellProps {
  client: Client;
}

/**
 * What this client is moving right now. Named from the client's point of view
 * because that is what an operator is asked about: `rateRx` is the server
 * receiving, so it is the client uploading.
 *
 * It used to carry the client's speed limit as well, as a "1.2 / 10" beside
 * each figure, and that was one number too many for a cell 5rem wide: the
 * megabits of the ceiling and the megabytes of the rate are different units,
 * they were set in the same size and colour, and the slash between them was
 * doing all the work of saying so. The limit has a column of its own now - see
 * SpeedLimitCell - and this cell is back to one number per direction.
 */
function RateCell({ client }: RateCellProps): JSX.Element {
  const { t } = useTranslation();
  const idle = client.rateRx === 0 && client.rateTx === 0;

  return (
    <div className={cn("space-y-0.5 text-xs leading-tight", idle && "text-muted-foreground")}>
      <p className="flex items-center gap-1">
        <CaretDown weight="bold" className="h-3 w-3 shrink-0" aria-hidden="true" />
        <span className="sr-only">{t("dashboard.download")}</span>
        <span className="tabular-nums">
          {t("units.perSecond", { value: formatBytes(client.rateTx) })}
        </span>
      </p>
      <p className="flex items-center gap-1">
        <CaretUp weight="bold" className="h-3 w-3 shrink-0" aria-hidden="true" />
        <span className="sr-only">{t("dashboard.upload")}</span>
        <span className="tabular-nums">
          {t("units.perSecond", { value: formatBytes(client.rateRx) })}
        </span>
      </p>
    </div>
  );
}

/**
 * Whether this server shapes at all, and in which directions.
 *
 * The same answer for every row, so the table works it out once and hands it
 * down rather than each cell asking.
 */
interface Shaping {
  /** Speed limits are switched on. While they are off there is no column. */
  shaping: boolean;
  /** Upload is shaped too, so an upload ceiling is a figure being enforced. */
  uploadOn: boolean;
}

interface SpeedLimitCellProps extends Pick<Shaping, "uploadOn"> {
  client: Client;
}

/** A ceiling in bits per second as the megabits an operator set it in, or "". */
function ceiling(bps: number): string {
  return bps > 0 ? `${Number((bps / 1e6).toFixed(3))}` : "";
}

/**
 * How fast this client is allowed to go.
 *
 * The two directions are named in words here rather than by an arrow, which is
 * the one column on the row that does. Rate and Usage carry a caret and an
 * arrow because they are measurements, three of them deep, and a row that
 * spelled out "download" six times would be a wall of the same two words. This
 * column is at most two lines, it appears on a minority of servers, and it is
 * the one an operator reads when they are about to change something - so it
 * says which direction it means instead of asking for a third glyph to be
 * learned and told apart from the other two at 12px.
 *
 * The whole column is conditional on the server shaping at all, which is also
 * why it can afford to spell out the clients that are uncapped: on a server
 * with the switch off there is no column here, and on one with it on, "which of
 * these did I forget to cap" is a real question that a blank cell answers
 * badly. So an unlimited client says Unlimited, and a client capped in one
 * direction only shows a dash against the other.
 *
 * Upload follows the same switch. A server that shapes download alone is not
 * enforcing an upload figure, whatever a client has stored against it from
 * before, so that row is left out rather than shown as a limit that is not
 * being applied.
 */
function SpeedLimitCell({ client, uploadOn }: SpeedLimitCellProps): JSX.Element {
  const { t } = useTranslation();
  const down = ceiling(client.downBps);
  const up = uploadOn ? ceiling(client.upBps) : "";

  if (!down && !up) {
    return <span className="text-xs text-muted-foreground">{t("common.unlimited")}</span>;
  }

  const line = (label: string, limit: string): JSX.Element => (
    <p className="flex items-baseline gap-1">
      <span className="text-muted-foreground">{label}</span>
      {limit ? (
        <span className="tabular-nums">{`${limit} ${String(t("units.megabitsPerSecond"))}`}</span>
      ) : (
        <>
          <span aria-hidden="true" className="text-muted-foreground">
            &mdash;
          </span>
          <span className="sr-only">{t("common.unlimited")}</span>
        </>
      )}
    </p>
  );

  return (
    <div className="space-y-0.5 text-xs leading-tight">
      {line(String(t("clients.speedDownShort")), down)}
      {uploadOn ? line(String(t("clients.speedUpShort")), up) : null}
    </div>
  );
}

interface ExpiryCellProps {
  expiresAt: string | null;
  /** Past its date and switched on anyway, because an admin switched it on. */
  overridden: boolean;
}

/**
 * When this client stops working, said the way somebody would say it out loud.
 *
 * A date on its own is a poor answer to "when does this run out": it reads the
 * same whether it is this afternoon or in eight months, it is not even the
 * whole truth now that an expiry carries a time of day, and working out how
 * long "12 Aug" leaves is arithmetic the reader has to do in their head on
 * every row. So the answer is how much time is left - the countdown under an
 * hour, then today and tomorrow with the clock time, then a count of days for
 * everything after that. The exact moment is always one hover away.
 *
 * Small type and a single line on purpose. This sits in a column between two
 * that already carry two lines each, and it is the widest thing that can be
 * said about a client in the fewest words - a wrapped chip here would set the
 * height of every row in the table.
 *
 * A client kept on past its date is the one case that spends a few more
 * characters. Left saying only "Expired", it would sit in a red chip beside a
 * status column reading Online, and the honest reading of that pair is that one
 * of them is broken. It is the chip that has more to say, because the status
 * column is right: the client is working, and this is the column that knows why.
 */
function ExpiryCell({ expiresAt, overridden }: ExpiryCellProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const parts = expiryParts(expiresAt);
  const locale = i18n.language;

  if (parts.tense === "none") {
    return <span className="text-xs text-muted-foreground">{t("common.never")}</span>;
  }
  if (parts.date === null) {
    return <span className="text-xs text-muted-foreground">{t("common.unknown")}</span>;
  }

  const expired = parts.tense === "expired";
  // Only meaningful once the date has gone: the API leaves the flag set for as
  // long as the date it was set against, and an admin who then moves the expiry
  // into the future has an ordinary client again.
  const kept = expired && overridden;
  const at = formatTime(parts.date, locale);
  const text = expired
    ? String(t(kept ? "clients.expiredKept" : "common.expired"))
    : parts.tense === "minutes"
      ? String(t("time.inMinutes", { count: parts.value }))
      : parts.tense === "today"
        ? String(t("clients.expiresToday", { time: at }))
        : parts.tense === "tomorrow"
          ? String(t("clients.expiresTomorrow", { time: at }))
          : String(t("time.inDays", { count: parts.daysLeft }));

  // Inside a day is the point at which an operator can still do something about
  // it, so that is where the chip stops being a note and starts being a signal.
  const urgent = parts.msLeft <= 86_400_000;
  const soon = parts.daysLeft <= EXPIRY_SOON_DAYS;

  return (
    <Badge
      size="sm"
      // Red is for a client this stopped. One that was kept on was not stopped
      // by anything, so it carries the same amber a date about to go carries:
      // worth seeing, not worth alarming anybody.
      variant={kept ? "outline" : expired ? "destructive" : urgent ? "warning" : "outline"}
      className={cn(
        "whitespace-nowrap tabular-nums",
        // A week out is worth colouring but not worth shouting: the border and
        // the text carry it, the fill stays out of the row.
        ((soon && !urgent && !expired) || kept) && "border-warning/40 text-warning",
        !soon && "text-muted-foreground",
      )}
      title={String(
        t(kept ? "clients.expiredKeptOn" : expired ? "clients.expiredOn" : "clients.expiresOn", {
          date: formatDateTime(parts.date, locale),
        }),
      )}
    >
      {urgent || expired ? <Clock weight="fill" aria-hidden="true" /> : null}
      {text}
    </Badge>
  );
}

interface CreatedCellProps {
  /** RFC 3339 in UTC, exactly as the config's "# Created" comment spells it. */
  createdAt: string | null;
}

/**
 * When the client was added.
 *
 * The date alone, because that is what an operator scans a column for; the time
 * of day only matters when two clients were added the same afternoon, and it is
 * one hover away. Rendered in the reader's own locale and time zone from a
 * timestamp the server states in UTC, so a panel open in two countries shows
 * each of them the moment they would have called it.
 */
function CreatedCell({ createdAt }: CreatedCellProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const created = parseMoment(createdAt);

  if (created === null) {
    // A peer whose "# Created" comment was edited away before the panel ever
    // saw it. Saying nothing was recorded beats showing a date that would then
    // look like a fact.
    return <span className="text-xs text-muted-foreground">{t("clients.createdUnknown")}</span>;
  }

  return (
    <span
      className="whitespace-nowrap text-xs tabular-nums text-muted-foreground"
      title={String(t("clients.createdOn", { date: formatDateTime(created, i18n.language) }))}
    >
      {formatDate(created, i18n.language)}
    </span>
  );
}

interface UsageCellProps {
  client: Client;
}

/**
 * Cumulative transfer, split per direction and laid out exactly like RateCell
 * so the two columns read as the same gauge at two time scales. Directions
 * follow the same client-eye view: `txBytes` left the server, so the client
 * downloaded it; `rxBytes` is what the client uploaded.
 *
 * The layout is shared and the glyph is not, which is the point: these two
 * columns and the limit beside them all say "down" and "up" about the same
 * client, so the only thing separating three pairs of figures on one row is
 * which arrow they carry. See the note in lib/icons.ts for what each shape
 * means.
 */
function UsageCell({ client }: UsageCellProps): JSX.Element {
  const { t } = useTranslation();
  const untouched = client.rxBytes === 0 && client.txBytes === 0;

  return (
    <div className={cn("space-y-0.5 text-xs leading-tight", untouched && "text-muted-foreground")}>
      <p className="flex items-center gap-1">
        <ArrowDown weight="bold" className="h-3 w-3 shrink-0" aria-hidden="true" />
        <span className="sr-only">{t("clients.downloaded")}</span>
        <span className="tabular-nums">{formatBytes(client.txBytes)}</span>
      </p>
      <p className="flex items-center gap-1">
        <ArrowUp weight="bold" className="h-3 w-3 shrink-0" aria-hidden="true" />
        <span className="sr-only">{t("clients.uploaded")}</span>
        <span className="tabular-nums">{formatBytes(client.rxBytes)}</span>
      </p>
    </div>
  );
}

interface TotalCellProps {
  client: Client;
  compact?: boolean;
}

function TotalCell({ client, compact = false }: TotalCellProps): JSX.Element {
  const { t } = useTranslation();
  const used = client.rxBytes + client.txBytes;

  return (
    <div
      // The per-direction column hides below lg, so the split also rides along
      // here as a hover hint rather than disappearing with it.
      title={`${String(t("clients.downloaded"))}: ${formatBytes(client.txBytes)} • ${String(
        t("clients.uploaded"),
      )}: ${formatBytes(client.rxBytes)}`}
    >
      <UsageBar used={used} quota={client.quotaBytes} compact={compact} />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Actions                                                                     */
/* -------------------------------------------------------------------------- */

export interface ClientActionHandlers {
  onShowQr: (client: Client) => void;
  onDownload: (client: Client) => void;
  onShowTraffic: (client: Client) => void;
  onEdit: (client: Client) => void;
  onResetKeys: (client: Client) => void;
  onResetUsage: (client: Client) => void;
  onToggleEnabled: (client: Client) => void;
  onDelete: (client: Client) => void;
}

interface RowActionsProps extends ClientActionHandlers {
  client: Client;
  busy: boolean;
}

function RowActions({
  client,
  busy,
  onShowQr,
  onDownload,
  onShowTraffic,
  onEdit,
  onResetKeys,
  onResetUsage,
  onToggleEnabled,
  onDelete,
}: RowActionsProps): JSX.Element {
  const { t } = useTranslation();
  const label = clientLabel(client);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          disabled={busy}
          aria-label={String(t("clients.actionsFor", { name: label }))}
        >
          {busy ? <Spinner size="sm" decorative /> : <DotsThree aria-hidden="true" />}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>{label}</DropdownMenuLabel>
        <DropdownMenuItem onSelect={() => afterMenuClose(() => onShowQr(client))}>
          <QrCode aria-hidden="true" />
          {t("clients.showQr")}
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => afterMenuClose(() => onDownload(client))}>
          <DownloadSimple aria-hidden="true" />
          {t("clients.downloadConfig")}
        </DropdownMenuItem>

        <DropdownMenuSeparator />

        {/* Its own group, between getting the config out and changing the
            client. The menu reads as a gradient - deliver, inspect, edit,
            reset, remove - and this is the only entry that asks the client a
            question rather than doing something to it. */}
        <DropdownMenuItem onSelect={() => afterMenuClose(() => onShowTraffic(client))}>
          <ChartBar aria-hidden="true" />
          {t("clients.history")}
        </DropdownMenuItem>

        <DropdownMenuSeparator />

        <DropdownMenuItem onSelect={() => afterMenuClose(() => onEdit(client))}>
          <PencilSimple aria-hidden="true" />
          {t("clients.edit")}
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => afterMenuClose(() => onToggleEnabled(client))}>
          {client.enabled ? <Prohibit aria-hidden="true" /> : <Power aria-hidden="true" />}
          {client.enabled ? t("clients.disable") : t("clients.enable")}
        </DropdownMenuItem>

        <DropdownMenuSeparator />

        <DropdownMenuItem onSelect={() => afterMenuClose(() => onResetKeys(client))}>
          <Key aria-hidden="true" />
          {t("clients.resetKeys")}
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => afterMenuClose(() => onResetUsage(client))}>
          <ArrowCounterClockwise aria-hidden="true" />
          {t("clients.resetUsage")}
        </DropdownMenuItem>

        <DropdownMenuSeparator />

        <DropdownMenuItem destructive onSelect={() => afterMenuClose(() => onDelete(client))}>
          <Trash aria-hidden="true" />
          {t("clients.remove")}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/* -------------------------------------------------------------------------- */
/* Sorting controls                                                            */
/* -------------------------------------------------------------------------- */

const SORT_LABEL_KEYS: Record<ClientSortKey, string> = {
  name: "clients.sortName",
  ip: "clients.sortIp",
  // The "usage" key orders by rx + tx, which is what the Total column shows.
  usage: "clients.total",
  lastHandshake: "clients.sortLastSeen",
  created: "clients.created",
};

const SORT_KEYS: readonly ClientSortKey[] = ["created", "name", "ip", "usage", "lastHandshake"];

interface SortHeaderProps {
  labelKey: string;
  sortKey: ClientSortKey;
  sort: ClientSort;
  onSortChange: (sort: ClientSort) => void;
  className?: string;
}

function SortHeader({
  labelKey,
  sortKey,
  sort,
  onSortChange,
  className,
}: SortHeaderProps): JSX.Element {
  const { t } = useTranslation();
  const active = sort.key === sortKey;
  const Icon = active ? (sort.direction === "asc" ? ArrowUp : ArrowDown) : ArrowsDownUp;

  return (
    <TableHead
      aria-sort={active ? (sort.direction === "asc" ? "ascending" : "descending") : "none"}
      className={className}
    >
      <button
        type="button"
        onClick={() =>
          onSortChange({
            key: sortKey,
            direction: active
              ? sort.direction === "asc"
                ? "desc"
                : "asc"
              : defaultDirection(sortKey),
          })
        }
        className={cn(
          "inline-flex items-center gap-1.5 rounded-sm text-xs font-semibold uppercase tracking-wide",
          "transition-colors hover:text-foreground",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          "focus-visible:ring-offset-2 focus-visible:ring-offset-background",
          active && "text-foreground",
        )}
      >
        {t(labelKey)}
        {/* 14px inside an uppercase column head: bold keeps the arrow as
            readable as the label it belongs to. */}
        <Icon
          weight="bold"
          className={cn("h-3.5 w-3.5", !active && "opacity-50")}
          aria-hidden="true"
        />
      </button>
    </TableHead>
  );
}

interface SortMenuProps {
  sort: ClientSort;
  onSortChange: (sort: ClientSort) => void;
}

/** Sorting for the stacked-card layout, which has no column headers to click. */
function SortMenu({ sort, onSortChange }: SortMenuProps): JSX.Element {
  const { t } = useTranslation();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm">
          <ArrowsDownUp aria-hidden="true" />
          {t(SORT_LABEL_KEYS[sort.key])}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>{t("common.sortBy")}</DropdownMenuLabel>
        {SORT_KEYS.map((key) => (
          <DropdownMenuCheckboxItem
            key={key}
            checked={sort.key === key}
            onCheckedChange={() => onSortChange({ key, direction: defaultDirection(key) })}
          >
            {t(SORT_LABEL_KEYS[key])}
          </DropdownMenuCheckboxItem>
        ))}
        <DropdownMenuSeparator />
        <DropdownMenuCheckboxItem
          checked={sort.direction === "asc"}
          onCheckedChange={() => onSortChange({ ...sort, direction: "asc" })}
        >
          {t("common.sortAscending")}
        </DropdownMenuCheckboxItem>
        <DropdownMenuCheckboxItem
          checked={sort.direction === "desc"}
          onCheckedChange={() => onSortChange({ ...sort, direction: "desc" })}
        >
          {t("common.sortDescending")}
        </DropdownMenuCheckboxItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/* -------------------------------------------------------------------------- */
/* Card layout                                                                 */
/* -------------------------------------------------------------------------- */

interface ClientCardProps extends ClientActionHandlers, Shaping {
  client: Client;
  busy: boolean;
}

function ClientCard({ client, busy, shaping, uploadOn, ...actions }: ClientCardProps): JSX.Element {
  const { t } = useTranslation();
  const endpoint = client.endpoint.trim();
  const status = clientStatus(client);
  // The lamp and the action menu are the two things that stay at full strength:
  // one says why the card is faded, and the other has to look pressable.
  const dim = isDimmed(status) ? DIMMED : undefined;

  return (
    <li className="rounded-lg border border-border bg-card p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-1.5">
          <div className={cn("min-w-0", dim)}>
            <NameCell client={client} />
          </div>
          <StatusDot status={status} />
        </div>
        <RowActions client={client} busy={busy} {...actions} />
      </div>

      <dl className={cn("mt-4 grid grid-cols-2 gap-x-4 gap-y-3 text-sm", dim)}>
        <div className="min-w-0">
          <dt className="text-xs text-muted-foreground">{t("clients.ip")}</dt>
          <dd className="truncate font-mono text-sm">
            <AddressCell client={client} />
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="text-xs text-muted-foreground">{t("clients.lastSeen")}</dt>
          <dd className="truncate">
            <LastSeen timestamp={client.lastSeen} />
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="text-xs text-muted-foreground">{t("clients.endpoint")}</dt>
          <dd className="truncate font-mono text-xs" title={endpoint || undefined}>
            {endpoint ? (
              endpointHost(endpoint)
            ) : (
              <span className="font-sans text-muted-foreground">{t("common.none")}</span>
            )}
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="text-xs text-muted-foreground">{t("clients.expiry")}</dt>
          <dd className="truncate">
            <ExpiryCell expiresAt={client.expiresAt} overridden={client.expiryOverridden} />
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="text-xs text-muted-foreground">{t("clients.rate")}</dt>
          <dd>
            <RateCell client={client} />
          </dd>
        </div>
        {shaping ? (
          <div className="min-w-0">
            <dt className="text-xs text-muted-foreground">{t("clients.speed")}</dt>
            <dd>
              <SpeedLimitCell client={client} uploadOn={uploadOn} />
            </dd>
          </div>
        ) : null}
        <div className="min-w-0">
          <dt className="text-xs text-muted-foreground">{t("clients.usage")}</dt>
          <dd>
            <UsageCell client={client} />
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="text-xs text-muted-foreground">{t("clients.created")}</dt>
          <dd className="truncate">
            <CreatedCell createdAt={client.createdAt} />
          </dd>
        </div>
        <div className="col-span-2 min-w-0">
          <dt className="text-xs text-muted-foreground">{t("clients.total")}</dt>
          <dd>
            <TotalCell client={client} compact />
          </dd>
        </div>
      </dl>
    </li>
  );
}

/* -------------------------------------------------------------------------- */
/* Table                                                                       */
/* -------------------------------------------------------------------------- */

/*
 * The actions column, pinned to the end of the row while the rest of the table
 * scrolls under it.
 *
 * `end-0` rather than `right-0`, so it pins to the correct edge whichever way
 * the language reads, and an opaque background because the cells it overlaps
 * would otherwise show through. The hairline down its inner edge is what tells
 * the eye that the column is pinned rather than merely last.
 *
 * The opaque background is only half of what keeps the row from showing through,
 * and it was for a while the only half there was. The other is in ui/table.tsx:
 * a table whose borders are collapsed paints its cells in an order that has no
 * room for a z-index, so this column had the background and still lost, with the
 * columns to its left riding over it as they scrolled underneath. The rule
 * between the rows comes from the cell borders that change set up, which is why
 * there is none named here.
 */
const PINNED_END =
  "sticky end-0 bg-card " +
  "before:absolute before:inset-y-0 before:start-0 before:w-px " +
  "before:bg-border before:content-['']";

/*
 * How wide each column is, and why a table full of variable-length text has to
 * be told.
 *
 * A table works out its own columns from what is in them, and what is in this
 * one changes under it: the rates are redrawn every two seconds and every row
 * is replaced when somebody turns a page. Both re-measure the grid against the
 * text that happens to be there, so a page whose endpoints run a couple of
 * characters shorter than the last one's is a page drawn on a slightly
 * different grid. Measured, it was seven pixels: every column from Last seen
 * rightwards stepped left on the way to page two and back again on the way
 * home. Not enough to read as a fault, exactly enough to see, and it happened
 * on every page turn because the page turn was what caused it.
 *
 * So each cell states a width, and the column is at least that wide whatever
 * the rows say. Not a width on the `<th>`, which a table treats as a suggestion
 * and overrules from the cells below it - the one that used to be on Total was
 * being ignored - and not `table-fixed`, which would take the header's own size
 * out of the reckoning too: the Russian catalog writes "Last seen" as
 * "Последняя активность", and a column sized for the English word would have to
 * break that across lines or cut it off. The header still asks for the room it
 * needs; what it no longer competes with is the data.
 *
 * The figures are about the width each column already had with English text in
 * it, so the page an operator is looking at now is the page they get back.
 * They are floors rather than sizes - see CellBox - which is what leaves the
 * Russian columns as wide as their own headers make them.
 */
const COLUMN = {
  // The one column sized for its longest word rather than its usual one, and
  // the only one where that is worth the space it wastes. The rest of the row
  // is numbers and names, which lose their tail to an ellipsis and stay
  // perfectly readable; this column holds five fixed words, and the longest of
  // them - "Limit reached" - names precisely the row an operator opened the
  // page to find. Sizing it for "Online" would cut that one down on every
  // server that has one. The cost is a hand's width of quiet space on the
  // pages where nothing has hit its limit, which is the cheaper mistake.
  status: "w-[7.25rem]",
  name: "w-28",
  rate: "w-20",
  address: "w-36",
  // Narrower since the port came off it: an IPv4 is fifteen characters at its
  // longest, and an IPv6 was going to be truncated at any width this table can
  // spare.
  endpoint: "w-28",
  lastSeen: "w-[5.5rem]",
  usage: "w-20",
  total: "w-24",
  // The widest of the three traffic columns, because it is the one spelling
  // things out: a direction in words and a limit that states its unit, where
  // Rate and Usage each get a 12px glyph and a bare figure. "Down 1000 Mbit/s"
  // is the longest line it can be asked to hold on one row.
  speed: "w-[7.5rem]",
  // And what it asks for when no client on the page has a limit at all, which
  // is the ordinary state of a server that shapes two of its clients. Every row
  // then says No limit, in half the room, and the column would otherwise hold
  // open a hand's width of nothing between Total and Expires for the sake of
  // figures that are not there. Sized for the words that are - the header is
  // wider than they are, so what the column actually settles at is its own
  // title rather than this.
  speedNone: "w-[4.5rem]",
  expiry: "w-20",
  created: "w-[5.5rem]",
} as const;

interface CellBoxProps {
  /** One of COLUMN: the width this cell asks its column for. */
  width: string;
  className?: string;
  children: React.ReactNode;
}

/**
 * A cell's content, held to a width that does not depend on the text in it.
 *
 * The pair of widths is the whole trick and neither half works alone. A fixed
 * `w-*` is what the column is sized from, because a box with a stated width
 * contributes that width and nothing else - the text inside it stops being able
 * to widen the table. `min-w-full` then lets the box grow back out to whatever
 * the column actually became, which matters wherever the header is the wider of
 * the two: a percentage means nothing while the column is being measured, so it
 * adds nothing to the figure above, and by the time anything is drawn the
 * column is settled and the box fills it. Without it a Russian column two
 * hundred pixels wide would truncate its rows at eighty and leave the rest
 * empty.
 */
function CellBox({ width, className, children }: CellBoxProps): JSX.Element {
  return <div className={cn(width, "min-w-full overflow-hidden", className)}>{children}</div>;
}

export interface ClientTableProps extends ClientActionHandlers {
  clients: Client[];
  sort: ClientSort;
  onSortChange: (sort: ClientSort) => void;
  /** Name of a client with a mutation in flight: its menu is held shut. */
  busyName?: string | null;
  className?: string;
}

export function ClientTable({
  clients,
  sort,
  onSortChange,
  busyName,
  className,
  ...actions
}: ClientTableProps): JSX.Element {
  const { t } = useTranslation();
  // Drawn in the order they arrived. The sort is the server's - it has to be,
  // because it orders every client before the page is cut, and a second one
  // here could only ever re-order the fifty rows that survived that.
  const rows = clients;
  // Read here rather than passed in from the page, because it is the same
  // cached query the server page and the client dialog already made, and
  // because what it decides is this table's own: a speed-limit column on a
  // server that shapes nothing is a column of "Unlimited" fifty rows deep.
  // Until the answer arrives there is no column, which is the right way round -
  // a column that appears a moment after the page has settled is worse than one
  // that was never there, and most servers shape nothing.
  const settings = useSettings();
  const shaping = settings.data?.shaperOn === "1";
  const uploadOn = shaping && settings.data?.shaperUpload === "1";
  // Whether the speed column has any figures to hold, or is fifty rows of "No
  // limit". A width is a floor and the cells cannot widen past it, so a column
  // sized for the longest line it might ever hold is a column that keeps that
  // room whether or not anything is in it - see COLUMN.speedNone.
  //
  // Off the page's own rows rather than off the whole list, because it is this
  // page that is being drawn. That does mean the column can be one width on
  // page one and another on page two, which is the sort of thing the widths
  // above exist to prevent. The difference is that this one is a fact about the
  // page rather than an accident of it: it moves when the answer to "is
  // anything here capped" changes, which is a thing the reader can see, and not
  // by seven pixels because one endpoint was shorter.
  const anyLimit =
    shaping && rows.some((client) => client.downBps > 0 || (uploadOn && client.upBps > 0));

  return (
    <div className={cn("space-y-3", className)}>
      {/* Cards have no header row to click, so sorting gets its own control.
          No count beside it: the page above carries one now at every width, and
          this one could only report the rows on this page - a different, less
          useful number sitting a few pixels from the real one. */}
      <div className="flex items-center justify-end gap-2 md:hidden">
        <SortMenu sort={sort} onSortChange={onSortChange} />
      </div>

      <ul className="space-y-3 md:hidden">
        {rows.map((client) => (
          <ClientCard
            key={client.publicKey}
            client={client}
            busy={busyName === client.name}
            shaping={shaping}
            uploadOn={uploadOn}
            {...actions}
          />
        ))}
      </ul>

      <div className="hidden overflow-hidden rounded-lg border border-border bg-card md:block">
        <Table>
          <caption className="sr-only">{t("clients.title")}</caption>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>{t("clients.status")}</TableHead>
              <SortHeader
                labelKey="clients.name"
                sortKey="name"
                sort={sort}
                onSortChange={onSortChange}
              />
              {/* Directly after the name, because it is the column that changes
                  while the page is open: an operator watching who is on the
                  tunnel reads a name and a rate, and those two used to have the
                  address, the endpoint and the last handshake sitting between
                  them. */}
              <TableHead className="hidden lg:table-cell">{t("clients.rate")}</TableHead>
              <SortHeader
                labelKey="clients.ip"
                sortKey="ip"
                sort={sort}
                onSortChange={onSortChange}
              />
              <TableHead className="hidden xl:table-cell">{t("clients.endpoint")}</TableHead>
              <SortHeader
                labelKey="clients.lastSeen"
                sortKey="lastHandshake"
                sort={sort}
                onSortChange={onSortChange}
              />
              <TableHead className="hidden lg:table-cell">{t("clients.usage")}</TableHead>
              <SortHeader
                labelKey="clients.total"
                sortKey="usage"
                sort={sort}
                onSortChange={onSortChange}
              />
              {shaping ? (
                <TableHead className="hidden lg:table-cell">{t("clients.speed")}</TableHead>
              ) : null}
              <TableHead className="hidden lg:table-cell">{t("clients.expiry")}</TableHead>
              <SortHeader
                labelKey="clients.created"
                sortKey="created"
                sort={sort}
                onSortChange={onSortChange}
              />
              <TableHead className={cn(PINNED_END, "z-20 text-end")}>
                <span className="sr-only">{t("clients.actions")}</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((client) => {
              const endpoint = client.endpoint.trim();
              const status = clientStatus(client);
              // Cell by cell rather than once on the row, so the two that are
              // left out stay out: the status lamp, which is the row's own
              // explanation of why the rest is faded, and the pinned actions
              // column, whose menu button has to look as pressable here as it
              // does on a live client - it is exactly the row an operator
              // reaches for when a client has stopped connecting.
              //
              // Opacity on a cell also makes it a stacking context, which is
              // why the fade is kept off the pinned column for a second reason:
              // that column wins the paint order on a z-index, and the cells
              // scrolling under it have to keep losing it.
              const dim = isDimmed(status) ? DIMMED : undefined;
              return (
                // "group" so the pinned cell can repaint the row's hover tint
                // over its own opaque background; without it the column the
                // rest of the row scrolls under would stay unhighlighted.
                <TableRow key={client.publicKey} className="group">
                  <TableCell>
                    {/* The one cell that must not clip. A lamp's halo is a
                        `ring`, which CSS paints outside the dot's box, and the
                        dot sits flush against the start of the box - so the
                        default `overflow-hidden` sliced the ring off down its
                        leading edge and left online and over-quota clients
                        wearing three quarters of a circle. Nothing here can
                        spill in its place: the state names truncate on their
                        own span, and there are only ever five of them. */}
                    <CellBox width={COLUMN.status} className="overflow-visible">
                      <StatusDot status={status} />
                    </CellBox>
                  </TableCell>
                  <TableCell className={dim}>
                    <CellBox width={COLUMN.name}>
                      <NameCell client={client} />
                    </CellBox>
                  </TableCell>
                  <TableCell className={cn("hidden whitespace-nowrap lg:table-cell", dim)}>
                    <CellBox width={COLUMN.rate}>
                      <RateCell client={client} />
                    </CellBox>
                  </TableCell>
                  <TableCell className={cn("font-mono text-xs", dim)}>
                    <CellBox width={COLUMN.address}>
                      <AddressCell client={client} />
                    </CellBox>
                  </TableCell>
                  <TableCell className={cn("hidden xl:table-cell", dim)}>
                    <CellBox width={COLUMN.endpoint}>
                      {endpoint ? (
                        <span className="block truncate font-mono text-xs" title={endpoint}>
                          {endpointHost(endpoint)}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">{t("common.none")}</span>
                      )}
                    </CellBox>
                  </TableCell>
                  <TableCell className={cn("text-sm", dim)}>
                    <CellBox width={COLUMN.lastSeen} className="truncate">
                      <LastSeen timestamp={client.lastSeen} />
                    </CellBox>
                  </TableCell>
                  <TableCell className={cn("hidden whitespace-nowrap lg:table-cell", dim)}>
                    <CellBox width={COLUMN.usage}>
                      <UsageCell client={client} />
                    </CellBox>
                  </TableCell>
                  <TableCell className={dim}>
                    <CellBox width={COLUMN.total}>
                      <TotalCell client={client} compact />
                    </CellBox>
                  </TableCell>
                  {shaping ? (
                    <TableCell className={cn("hidden whitespace-nowrap lg:table-cell", dim)}>
                      <CellBox width={anyLimit ? COLUMN.speed : COLUMN.speedNone}>
                        <SpeedLimitCell client={client} uploadOn={uploadOn} />
                      </CellBox>
                    </TableCell>
                  ) : null}
                  <TableCell className={cn("hidden whitespace-nowrap lg:table-cell", dim)}>
                    <CellBox width={COLUMN.expiry}>
                      <ExpiryCell
                        expiresAt={client.expiresAt}
                        overridden={client.expiryOverridden}
                      />
                    </CellBox>
                  </TableCell>
                  <TableCell className={dim}>
                    <CellBox width={COLUMN.created} className="truncate">
                      <CreatedCell createdAt={client.createdAt} />
                    </CellBox>
                  </TableCell>
                  <TableCell
                    className={cn(
                      PINNED_END,
                      // The row's hover tint has to be painted again here,
                      // because the opaque background this cell needs is
                      // exactly what the tint on the row behind cannot reach
                      // through. It goes on as a layer of its own rather than
                      // as a second background colour, and that is the whole
                      // point: `bg-muted/50` does not sit on top of `bg-card`,
                      // it replaces it, so the column turned half transparent
                      // for precisely as long as the pointer was on the row -
                      // the one moment the operator is certainly looking at it.
                      //
                      // The layer is the cell's own ::after, held behind the
                      // content by a negative z-index. The cell is a stacking
                      // context already, being pinned, so "behind the content"
                      // there still means in front of the cell's background.
                      "z-10 text-end",
                      "after:absolute after:inset-0 after:-z-10 after:content-['']",
                      "group-hover:after:bg-muted/50",
                    )}
                  >
                    <RowActions client={client} busy={busyName === client.name} {...actions} />
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
