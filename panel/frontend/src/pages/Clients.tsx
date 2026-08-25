import * as React from "react";
import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Funnel, MagnifyingGlass, Plus, Trash, Users, X } from "@/lib/icons";

import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { PageHeader } from "@/components/PageHeader";
import { Pagination } from "@/components/Pagination";
import { ClientDialog } from "@/components/clients/ClientDialog";
import { BulkRemoveDialog, type BulkRemoveCounts } from "@/components/clients/BulkRemoveDialog";
import { ClientTrafficDialog } from "@/components/clients/ClientTrafficDialog";
import { QrDialog, useConfigDownload } from "@/components/clients/QrDialog";
import {
  ClientTable,
  DEFAULT_CLIENT_SORT,
  clientStatus,
  type ClientSort,
} from "@/components/clients/ClientTable";
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
import { useDebounced } from "@/lib/debounce";
import {
  LARGE_PAGE,
  LARGE_PAGE_FALLBACK,
  rowsPerPage,
  usePageSizeChoice,
  type PageSizeChoice,
} from "@/lib/pageSize";
import { liveUsage, usageFloor, type UsageFloor, type UsageFloors } from "@/lib/usage";
import { cn, expiryParts } from "@/lib/utils";
import {
  useBulkRemoveClients,
  useClients,
  useDeleteClient,
  useEnforcementCatchup,
  useLiveFreshness,
  useLiveStats,
  useResetKeys,
  useResetUsage,
  useServer,
  useToggleClient,
} from "@/api/hooks";
import type { ApiError } from "@/api/client";
import type {
  BulkRemoveOptions,
  Client,
  ClientStatus,
  ClientStatusFilter,
  LiveStats,
} from "@/api/types";

/*
 * Every device that can reach this server, and everything an operator does to
 * one of them.
 *
 * The list itself is polled slowly, because it reads and parses awg0.conf; the
 * rates and the online lamps come from the collector's 2 s blob and are merged
 * on top. That split is why a busy table does not cost the server a config
 * parse twice a second, and why a client added over SSH still appears here on
 * its own.
 *
 * Anything destructive asks first, and every confirmation says what actually
 * happens on the server rather than "are you sure".
 */

type StatusFilter = ClientStatusFilter;

/**
 * How long the search box waits before asking the server.
 *
 * Long enough that typing a name is one request rather than one per letter,
 * short enough to feel like the list is keeping up. The narrowing is not local
 * any more, so this is the whole of the difference between a search that costs
 * one config parse and one that costs six.
 */
const SEARCH_DEBOUNCE_MS = 250;

const STATUS_FILTERS: readonly { value: StatusFilter; labelKey: string }[] = [
  { value: "all", labelKey: "clients.filterAll" },
  { value: "active", labelKey: "clients.filterActive" },
  { value: "online", labelKey: "clients.filterOnline" },
  { value: "offline", labelKey: "clients.filterOffline" },
  { value: "disabled", labelKey: "clients.filterDisabled" },
  { value: "expired", labelKey: "clients.filterExpired" },
];

type ConfirmKind = "delete" | "disable" | "enable" | "resetKeys" | "resetUsage" | "bulkRemove";

interface PendingConfirm {
  kind: ConfirmKind;
  /** Snapshot taken when the menu item was chosen, not a live row. Null for a sweep. */
  client: Client | null;
  /** How many clients the sweep covers; 0 for everything that names one client. */
  count: number;
  /** Which categories the bulk removal was asked for. Absent for everything else. */
  options?: BulkRemoveOptions;
}

/** Title, body and button wording for each confirmation, all from the catalog. */
const CONFIRM_TEXT: Record<
  ConfirmKind,
  { title: string; body: string; action: string; destructive: boolean }
> = {
  delete: {
    title: "clients.removeConfirm",
    body: "clients.removeConfirmBody",
    action: "common.delete",
    destructive: true,
  },
  disable: {
    title: "clients.disableConfirm",
    body: "clients.disableConfirmBody",
    action: "common.disable",
    destructive: false,
  },
  enable: {
    title: "clients.enableConfirm",
    body: "clients.enableConfirmBody",
    action: "common.enable",
    destructive: false,
  },
  resetKeys: {
    title: "clients.resetKeysConfirm",
    body: "clients.resetKeysConfirmBody",
    action: "clients.resetKeys",
    destructive: true,
  },
  resetUsage: {
    title: "clients.resetUsageConfirm",
    body: "clients.resetUsageConfirmBody",
    action: "clients.resetUsage",
    destructive: false,
  },
  bulkRemove: {
    // The count is the title here rather than a detail inside it: what makes a
    // sweep reviewable is the number of clients it takes, and that number is
    // the one thing the operator did not type in themselves.
    title: "clients.bulkRemoveConfirm",
    // Replaced by confirmBody with the sentence for the categories actually
    // chosen; this is the one for both of them.
    body: "clients.bulkRemoveConfirmBothBody",
    action: "common.delete",
    destructive: true,
  },
};

/**
 * The statuses that describe connectivity, and therefore go out of date between
 * one 30 s poll of the list and the next. The other two - disabled and quota -
 * are enforcement, which only the API can decide and which outranks whether the
 * peer happens to be handshaking.
 */
const CONNECTIVITY: readonly ClientStatus[] = ["online", "idle", "offline"];

/**
 * The status this row would have if the API had answered a moment ago.
 *
 * The list is polled every 30 seconds and the live blob every two, so the
 * status word the API sent is by far the oldest thing on the row. Taking the
 * fresher one is what keeps the lamp, the rate and the last-seen time
 * describing the same instant instead of three instants up to half a minute
 * apart.
 *
 * Nothing is derived here, deliberately. This function once compared the
 * handshake against its own copy of the idle window; the API's copy of that
 * figure changed and this one did not, so a client the server had long since
 * called offline went on reading "idle" in the browser for a whole day. The
 * collector sends the word now, and the only decision left on this side is
 * which of two sources to believe - which is a question about the sources
 * rather than about the peer.
 */
function liveStatus(client: Client, peerStatus: ClientStatus | undefined): ClientStatus {
  // Switched off, for whatever reason: the table's own reading of that is the
  // one to keep, and a peer with no key in the kernel has no connectivity to
  // report anyway.
  if (!client.enabled) {
    return clientStatus(client);
  }
  // Enforcement outranks connectivity and the collector does not know about it,
  // so an over-quota client keeps that word however lively it looks.
  if (client.status && !CONNECTIVITY.includes(client.status)) {
    return client.status;
  }
  if (peerStatus) {
    return peerStatus;
  }
  // Enabled, but a reporting collector is saying nothing about it: the peer is
  // not on the interface, whatever a list poll from up to thirty seconds ago
  // still claims. How long it has been gone is the API's to say, not ours.
  return client.status === "online" ? "idle" : clientStatus(client);
}

/**
 * Fold the collector's live view over the list.
 *
 * Everything the collector knows better is taken: rates, the online lamp, the
 * handshake, the endpoint and the byte totals - see lib/usage for what makes
 * the last of those the same accounting rather than a second opinion. The list
 * behind them is polled every thirty seconds, so without this a client pulling
 * a hundred megabits sat at one figure for half a minute and then jumped most
 * of a gigabyte, next to a throughput reading that had been moving smoothly the
 * whole time.
 *
 * Nothing at all is taken from a blob that has stopped moving. The API applies
 * that same rule before it answers - it zeroes the rates of a blob it considers
 * too old to quote - and folding an unchecked live view back over the row was
 * putting exactly those numbers back on the screen: a client whose VPN had been
 * switched off for minutes, sitting at "idle" with a throughput figure beside
 * it, because the collector had stopped and the last blob it wrote said so
 * forever. A stale blob leaves the row as the API sent it, which is the same
 * file read honestly.
 */
function mergeLive(
  clients: Client[],
  live: LiveStats | undefined,
  fresh: boolean,
  floors: UsageFloors,
): [rows: Client[], floors: UsageFloors] {
  if (!live || !fresh) {
    // The rows are the API's own, so there is nothing to remember about them
    // that the endpoint is not already saying. What was already held is kept
    // rather than cleared, and is still true: these are counters that only
    // climb, so a blob that comes back cannot come back lower than one of them.
    return [clients, floors];
  }
  // Rebuilt from the rows being shown rather than updated in place, so a client
  // that has left the page - filtered out, paged past, deleted - leaves with it
  // instead of sitting in a map that only ever grows.
  const next = new Map<string, UsageFloor>();
  const rows = clients.map((client) => {
    // A peer the collector is reporting nothing for is switched off or has
    // never handshaken. Its rates are zero, not stale.
    const peer = live.peers[client.publicKey];
    const rateRx = peer?.rateRx ?? 0;
    const rateTx = peer?.rateTx ?? 0;
    // A disabled peer has no key in the kernel, so it cannot be online whatever
    // the sample says.
    const online = (peer?.online ?? false) && client.enabled;
    const lastHandshake = peer?.handshake || client.lastHandshake;
    // All three are "the last time X happened" and none of them can move
    // backwards, so the newest is the answer whichever source it came from.
    // Taking the row's own figure into the comparison rather than falling back
    // to it covers the peer the blob has stopped mentioning - switched off since
    // this page was fetched - whose last handshake is still older than the last
    // thing heard from it.
    const lastSeen = Math.max(peer?.lastRx ?? 0, lastHandshake, client.lastSeen);
    const endpoint = peer?.endpoint || client.endpoint;
    const status = liveStatus(client, peer?.status);
    const [rxBytes, txBytes] = liveUsage(client, peer, floors.get(client.publicKey));
    const quotaUsed = rxBytes + txBytes;
    next.set(client.publicKey, usageFloor(client, rxBytes, txBytes));

    // Same row, same object: the list is re-derived every two seconds, and a
    // fresh object per client per poll would re-sort and re-render a table
    // where nothing had moved.
    if (
      client.rateRx === rateRx &&
      client.rateTx === rateTx &&
      client.online === online &&
      client.lastHandshake === lastHandshake &&
      client.lastSeen === lastSeen &&
      client.endpoint === endpoint &&
      client.status === status &&
      client.rxBytes === rxBytes &&
      client.txBytes === txBytes
    ) {
      return client;
    }
    return {
      ...client,
      rateRx,
      rateTx,
      online,
      lastHandshake,
      lastSeen,
      endpoint,
      status,
      rxBytes,
      txBytes,
      // Nothing on the row reads these two - every column adds the pair above
      // up for itself - but a row that carried one of the two answers would be
      // a trap for whatever reads them next.
      quotaUsed,
      quotaPercent:
        client.quotaBytes > 0
          ? Math.min(100, Math.floor((quotaUsed * 100) / client.quotaBytes))
          : 0,
    };
  });
  return [rows, next];
}

/**
 * Whether this client's date has run out - the date, and nothing else.
 *
 * The status word will not do, and this is worth spelling out because it is the
 * obvious thing to reach for. The API does not send "expired" at all any more:
 * the status column answers whether a client is working, the expiry column
 * answers whether its date has gone, and neither borrows the other's word. A
 * lapsed client therefore reads as "disabled" if the collector switched it off
 * and as "online" if an admin has since switched it back on, and neither of
 * those tells you anything about the date.
 *
 * The date is also the server's own rule: `clients/remove-expired` sweeps on the
 * timestamp. Anything counting or selecting the clients that sweep will take
 * therefore has to read what the sweep reads, or the button promises one number
 * and the server acts on another.
 *
 * The cost is that a browser whose clock is wrong disagrees with the server near
 * the boundary. That is a few seconds either way on a date the operator set in
 * days, against a whole category of client no other reading can see at all.
 */
function isExpired(client: Client): boolean {
  return expiryParts(client.expiresAt).tense === "expired";
}

/*
 * Selecting by status, matching the search text and counting what the sweep
 * would take all happen on the server now - see apps.clients.merge, which does
 * each of them against the same merged row this page used to. They moved
 * together because they had to: they read words that only exist after the
 * merge, and the page they narrow is cut before it is sent.
 *
 * `isExpired` stayed, because the confirmation dialog below still asks the
 * question about one client in front of it.
 */

/**
 * The sentence under a confirmation title, which is one sentence for every
 * action but two.
 *
 * Switching on a client whose date has gone is not the same act as switching on
 * any other client. It outlasts the server's next enforcement pass and holds
 * until somebody moves the date, which is a rule an operator cannot guess from a
 * switch that looks exactly like the one on the row above. The moment to say so
 * is the moment they are doing it.
 *
 * A bulk removal has three sentences for the same reason in reverse: the title
 * says how many clients go, and which clients those are is exactly what the
 * operator has to be able to check before agreeing to it. One wording covering
 * all three selections would have to be vague about the only thing that
 * matters.
 */
function confirmBody(confirm: PendingConfirm, fallback: string): string {
  if (confirm.kind === "bulkRemove" && confirm.options) {
    if (confirm.options.expired && confirm.options.disabled) {
      return "clients.bulkRemoveConfirmBothBody";
    }
    return confirm.options.expired
      ? "clients.bulkRemoveConfirmExpiredBody"
      : "clients.bulkRemoveConfirmDisabledBody";
  }
  const enablingLapsed =
    confirm.kind === "enable" && confirm.client !== null && isExpired(confirm.client);
  return enablingLapsed ? "clients.enableExpiredConfirmBody" : fallback;
}

function LoadingRows(): JSX.Element {
  return (
    <div className="space-y-3" aria-busy="true">
      {Array.from({ length: 4 }, (_, index) => (
        <div key={index} className="rounded-lg border border-border bg-card p-4">
          <div className="flex items-center gap-4">
            <Skeleton className="h-4 w-20" />
            <Skeleton className="h-4 w-32" />
            <Skeleton className="hidden h-4 w-28 sm:block" />
            <Skeleton className="ms-auto h-4 w-24" />
          </div>
          <Skeleton className="mt-3 h-2 w-full" />
        </div>
      ))}
    </div>
  );
}

export default function Clients(): JSX.Element {
  const { t } = useTranslation();
  const { toast } = useToast();

  const [searchParams, setSearchParams] = useSearchParams();
  // Seeded from the URL so the dashboard can link straight to one client.
  const [query, setQuery] = React.useState(() => searchParams.get("q") ?? "");
  const [filter, setFilter] = React.useState<StatusFilter>("all");
  const [sort, setSort] = React.useState<ClientSort>(DEFAULT_CLIENT_SORT);
  const [page, setPage] = React.useState(1);
  // Whatever this browser last chose, which is the size the first request asks
  // for rather than something applied after one.
  const [pageSize, setPageSize] = usePageSizeChoice();
  const rows = rowsPerPage(pageSize);

  // The narrowing happens on the server now, so the search box is a request per
  // settled value rather than a filter over a list already here.
  const needle = useDebounced(query.trim(), SEARCH_DEBOUNCE_MS);

  // Back to the first page whenever the list being paged through changes
  // underneath. Staying on page nine of a result set that now has two is how an
  // operator ends up looking at an empty table wondering what they broke.
  React.useEffect(() => {
    setPage(1);
  }, [needle, filter, sort.key, sort.direction]);

  const clients = useClients({
    q: needle,
    status: filter,
    sort: sort.key,
    direction: sort.direction,
    page,
    pageSize: rows,
  });

  // Only the peers on this page. The blob holds one entry per client on the
  // server and the table draws ten of them by default, which on a big server is
  // the difference between a poll costing a few kilobytes and one costing most
  // of a megabyte - twice a second, for as long as the tab is open. A page
  // larger than the request line can name is the hook's business, not this
  // page's: see useLiveStats for what it does instead of dropping the surplus.
  const pageKeys = React.useMemo(
    () => (clients.data?.clients ?? []).map((client) => client.publicKey),
    [clients.data],
  );
  const live = useLiveStats({ peers: pageKeys });
  // A blob that has stopped moving is not folded over the list at all: see
  // mergeLive for what showing its rates anyway looked like.
  const { fresh: liveFresh } = useLiveFreshness(live);
  // The rows above are thirty seconds old, and the one event that makes them
  // wrong faster than that is a client being switched off - which is what the
  // blob this page is already polling can say within two.
  useEnforcementCatchup(live);
  // Only for the defaults the new-client form offers; cached, not polled.
  const server = useServer();
  const download = useConfigDownload();

  const remove = useDeleteClient();
  const bulkRemove = useBulkRemoveClients();
  const toggle = useToggleClient();
  const resetKeys = useResetKeys();
  const resetUsage = useResetUsage();

  const [formOpen, setFormOpen] = React.useState(false);
  const [editing, setEditing] = React.useState<Client | null>(null);
  const [bulkOpen, setBulkOpen] = React.useState(false);
  const [qrOpen, setQrOpen] = React.useState(false);
  // Both of these are kept after closing so the dialog can animate out with its
  // content intact instead of emptying itself first.
  const [qrClient, setQrClient] = React.useState<Client | null>(null);
  // Null is the whole of "closed" here, unlike the QR dialog beside it: the
  // history query is keyed off this name and is only made while it is set, so
  // holding a client after closing would go on polling for a dialog nobody is
  // looking at. The dialog fades out over an empty body, which for a chart is
  // the same picture either way.
  const [trafficClient, setTrafficClient] = React.useState<Client | null>(null);
  const [confirm, setConfirm] = React.useState<PendingConfirm | null>(null);
  const [confirmOpen, setConfirmOpen] = React.useState(false);

  // The search box is mirrored into the URL so the view can be shared and
  // survives a reload. Replace, not push: typing must not fill the back stack.
  React.useEffect(() => {
    const current = searchParams.get("q") ?? "";
    if (current === query) {
      return;
    }
    const next = new URLSearchParams(searchParams);
    if (query) {
      next.set("q", query);
    } else {
      next.delete("q");
    }
    setSearchParams(next, { replace: true });
  }, [query, searchParams, setSearchParams]);

  // This page, with the live blob folded over it. The list arrives narrowed and
  // ordered, so nothing here selects or sorts - the only thing left to do in the
  // browser is put two-second-old rates onto rows that are thirty seconds old.
  const floors = React.useRef<UsageFloors>(new Map());
  const visible = React.useMemo(() => {
    const [rows, next] = mergeLive(
      clients.data?.clients ?? [],
      live.data,
      liveFresh,
      floors.current,
    );
    floors.current = next;
    return rows;
  }, [clients.data, live.data, liveFresh]);

  // The history dialog's client, taken from the page rather than from the
  // snapshot that opened it. The snapshot is what decides *which* client the
  // dialog is about - it survives the row leaving the page - but the all-time
  // figure it shows has to be the row's current one: the reset in that dialog's
  // footer takes that figure to zero, and a frozen copy would go on showing the
  // total that was just cleared for as long as the dialog stayed open.
  const trafficRow = React.useMemo(() => {
    if (trafficClient === null) {
      return null;
    }
    return visible.find((row) => row.publicKey === trafficClient.publicKey) ?? trafficClient;
  }, [trafficClient, visible]);

  // All counted over the whole server rather than over this page: a sweep
  // removes the clients a filter is hiding as readily as the ones on screen,
  // and the pagination needs to know how many rows it is a page of.
  const total = clients.data?.total ?? 0;
  const totalAll = clients.data?.totalAll ?? 0;
  // The three figures the bulk dialog puts on its options. The last is the
  // union of the other two and is counted by the server rather than added up
  // here, because a lapsed client that has also been switched off is in both.
  const bulkCounts = React.useMemo<BulkRemoveCounts>(
    () => ({
      expired: clients.data?.expiredCount ?? 0,
      disabled: clients.data?.disabledCount ?? 0,
      either: clients.data?.expiredOrDisabledCount ?? 0,
    }),
    [clients.data],
  );
  // The size the server actually paged by, which is not always the one asked
  // for: a query it rejects is answered with the default page. Counting pages by
  // anything other than the number in the answer draws a pager that disagrees
  // with the table under it. Zero is not a small page but no page at all, and
  // everything the list selected is on it - so it is one page however long.
  const servedSize = clients.data?.pageSize ?? rows;
  const pageCount = servedSize > 0 ? Math.max(1, Math.ceil(total / servedSize)) : 1;
  // The server clamps a page past the end to the last one with rows on it, and
  // says which that was; following it keeps the controls agreeing with the table.
  const currentPage = clients.data?.page ?? page;
  // Long enough to be worth saying so before it is drawn. Counted on the rows
  // this page will actually hold rather than on the server's client count: a
  // thousand clients read ten at a time cost nothing, and it is the drawing of
  // them that is slow, not the having of them.
  const longPage = servedSize === 0 && total > LARGE_PAGE;

  const choosePageSize = React.useCallback(
    (choice: PageSizeChoice) => {
      // Keep the top row of the screen on screen. Jumping back to page one is
      // the usual answer and it is the wrong one here: an operator who has
      // walked to page nine of ten-row pages and then asks for fifty is asking
      // to see more of what they are looking at, not to start the walk again.
      // Asking for the whole list is the one case with nowhere else to land.
      const next = rowsPerPage(choice);
      setPage(next > 0 ? Math.floor(((currentPage - 1) * servedSize) / next) + 1 : 1);
      setPageSize(choice);
    },
    [currentPage, servedSize, setPageSize],
  );

  const subnetCidr = clients.data?.subnetCidr ?? "";
  const freeIps = clients.data?.freeIps ?? 0;
  const poolFull = clients.data !== undefined && freeIps <= 0;

  /*
   * Every failure here names the action that failed rather than reporting a
   * bare "something went wrong": these mutations all fire from the same row of
   * buttons, and a toast that does not say which one it is about leaves the
   * operator guessing whether the client was removed, switched off, or neither.
   */
  const notifyFailure = React.useCallback(
    (titleKey: string, name?: string) =>
      (error: ApiError): void => {
        toast({
          title: String(t(titleKey, { name })),
          description: error.detail,
          variant: "destructive",
        });
      },
    [t, toast],
  );

  const openQr = React.useCallback((client: Client) => {
    setQrClient(client);
    setQrOpen(true);
  }, []);

  const openCreate = React.useCallback(() => {
    setEditing(null);
    setFormOpen(true);
  }, []);

  const openEdit = React.useCallback((client: Client) => {
    setEditing(client);
    setFormOpen(true);
  }, []);

  const ask = React.useCallback((kind: ConfirmKind, client: Client) => {
    setConfirm({ kind, client, count: 0 });
    setConfirmOpen(true);
  }, []);

  const askBulkRemove = React.useCallback((options: BulkRemoveOptions, count: number) => {
    // The options dialog closes as the confirmation opens: the choice has been
    // made, and what is left to answer is whether that many clients should
    // really go. The count comes from the dialog rather than being re-derived
    // here, and it is frozen at the moment the button was pressed rather than
    // read live: the list polls behind this, and a confirmation whose number
    // changes while it is on screen is asking about something other than what
    // was clicked.
    setBulkOpen(false);
    setConfirm({ kind: "bulkRemove", client: null, count, options });
    setConfirmOpen(true);
  }, []);

  const handleCreated = React.useCallback(
    (client: Client) => {
      setFormOpen(false);
      toast({
        title: String(t("clients.createdToast", { name: client.name })),
        description: String(t("clients.createdToastBody")),
        variant: "success",
      });
      // The config is only useful once it is on the device, so the QR comes up
      // immediately instead of waiting to be asked for.
      openQr(client);
    },
    [openQr, t, toast],
  );

  const handleSaved = React.useCallback(
    (client: Client) => {
      setFormOpen(false);
      toast({
        title: String(t("clients.updatedToast", { name: client.name })),
        variant: "success",
      });
    },
    [t, toast],
  );

  const runConfirmed = React.useCallback(() => {
    if (!confirm) {
      return;
    }
    const { kind, client, options } = confirm;
    setConfirmOpen(false);

    if (kind === "bulkRemove") {
      if (!options) {
        return;
      }
      bulkRemove.mutate(options, {
        onSuccess: ({ removed }) => {
          // The server's own count again, not the one the button promised: the
          // list behind it is up to half a minute old, and a client removed
          // over SSH in the meantime is not in this number.
          toast({
            title: String(
              removed.length > 0
                ? t("clients.bulkRemovedToast", { count: removed.length })
                : t("clients.bulkRemovedNone"),
            ),
            variant: removed.length > 0 ? "success" : "default",
          });
        },
        onError: notifyFailure("clients.bulkRemoveFailed"),
      });
      return;
    }
    if (!client) {
      return;
    }
    const name = client.name;

    switch (kind) {
      case "delete":
        remove.mutate(name, {
          onSuccess: () =>
            toast({ title: String(t("clients.removedToast", { name })), variant: "success" }),
          onError: notifyFailure("clients.removeFailed", name),
        });
        return;
      case "disable":
        toggle.mutate(
          { name, enabled: false },
          {
            onSuccess: () =>
              toast({ title: String(t("clients.disabledToast", { name })), variant: "success" }),
            onError: notifyFailure("clients.disableFailed", name),
          },
        );
        return;
      case "enable":
        toggle.mutate(
          { name, enabled: true },
          {
            onSuccess: () =>
              toast({ title: String(t("clients.enabledToast", { name })), variant: "success" }),
            onError: notifyFailure("clients.enableFailed", name),
          },
        );
        return;
      case "resetKeys":
        resetKeys.mutate(name, {
          onSuccess: (updated) => {
            toast({
              title: String(t("clients.keysRotated", { name })),
              description: String(t("clients.keysRotatedBody")),
              variant: "success",
            });
            // The old config is dead the moment this returns, so the new one is
            // put on screen rather than left to be hunted for.
            openQr(updated);
          },
          onError: notifyFailure("clients.keysFailed", name),
        });
        return;
      case "resetUsage":
        resetUsage.mutate(name, {
          onSuccess: () =>
            toast({ title: String(t("clients.usageReset", { name })), variant: "success" }),
          onError: notifyFailure("clients.usageResetFailed", name),
        });
        return;
    }
  }, [bulkRemove, confirm, notifyFailure, openQr, remove, resetKeys, resetUsage, t, toast, toggle]);

  // Whichever client is mid-change: its row menu is held shut until it settles.
  const busyName =
    (remove.isPending ? remove.variables : undefined) ??
    (toggle.isPending ? toggle.variables.name : undefined) ??
    (resetKeys.isPending ? resetKeys.variables : undefined) ??
    (resetUsage.isPending ? resetUsage.variables : undefined) ??
    download.pending;

  const confirmText = confirm ? CONFIRM_TEXT[confirm.kind] : null;

  const filterRow = (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
      <div className="relative min-w-0 flex-1">
        <MagnifyingGlass
          className="pointer-events-none absolute start-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
          aria-hidden="true"
        />
        <Input
          // Not type="search": WebKit and Chrome draw their own clear button on
          // the wrong side under RTL, next to the one below.
          type="text"
          inputMode="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={String(t("clients.searchPlaceholder"))}
          aria-label={String(t("clients.search"))}
          className="ps-9 pe-9"
        />
        {query ? (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="absolute end-0.5 top-1/2 h-8 w-8 -translate-y-1/2"
            aria-label={String(t("common.clear"))}
            onClick={() => setQuery("")}
          >
            <X aria-hidden="true" />
          </Button>
        ) : null}
      </div>

      <Select value={filter} onValueChange={(value) => setFilter(value as StatusFilter)}>
        <SelectTrigger className="sm:w-48" aria-label={String(t("clients.status"))}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {STATUS_FILTERS.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {t(option.labelKey)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );

  return (
    <>
      <PageHeader
        title={String(t("clients.title"))}
        description={String(t("clients.subtitle"))}
        icon={Users}
        actions={
          <>
            <Button onClick={openCreate} disabled={poolFull || clients.isError}>
              <Plus aria-hidden="true" />
              {t("clients.new")}
            </Button>
            {/* After the button that adds clients: the same kind of act in the
                other direction, and the one an operator reaches for far less
                often, so it sits on the far side of the one they came for.

                Outlined rather than filled, and it deletes nothing by itself:
                it opens a dialog to choose in, which opens a confirmation that
                names a number, and the red is spent on the button at the end of
                that - the one that actually removes. A destructive colour on
                the way in would have every visit to this page read a warning
                about something the operator has not asked for yet.

                Hidden on a server with no clients on it at all, where the empty
                state is already saying the only thing there is to say. */}
            {totalAll > 0 ? (
              <Button
                type="button"
                variant="outline"
                disabled={bulkRemove.isPending || clients.isError}
                onClick={() => setBulkOpen(true)}
              >
                {bulkRemove.isPending ? (
                  <Spinner size="sm" decorative />
                ) : (
                  <Trash aria-hidden="true" />
                )}
                {t("clients.bulkRemove")}
              </Button>
            ) : null}
          </>
        }
      >
        {filterRow}
      </PageHeader>

      <div className="space-y-4">
        {poolFull ? (
          <div role="status" className="rounded-lg border border-warning/40 bg-warning/5 p-4">
            <p className="text-sm font-medium">{t("clients.noFreeIps")}</p>
            <p className="mt-1 text-sm leading-relaxed text-muted-foreground">
              {t("clients.noFreeIpsHint", { subnet: subnetCidr })}
            </p>
          </div>
        ) : null}

        {/* Said once the page is on screen rather than asked before it is drawn.
            A confirmation in front of a page size would stand between the
            operator and the thing they asked for every time they asked for it,
            to report a cost that is a property of their machine and that they
            are about to be able to see for themselves. */}
        {longPage ? (
          <div
            role="status"
            className="flex flex-col gap-3 rounded-lg border border-warning/40 bg-warning/5 p-4 sm:flex-row sm:items-center sm:justify-between"
          >
            <div>
              <p className="text-sm font-medium">{t("clients.longPage", { count: total })}</p>
              <p className="mt-1 text-sm leading-relaxed text-muted-foreground">
                {t("clients.longPageHint")}
              </p>
            </div>
            {/* The rows-per-page control is at the foot of the table this is
                warning about, which is a long way to scroll to undo it. */}
            <Button
              variant="outline"
              className="shrink-0 self-start sm:self-auto"
              onClick={() => choosePageSize(LARGE_PAGE_FALLBACK)}
            >
              {t("clients.longPageAction", { count: Number(LARGE_PAGE_FALLBACK) })}
            </Button>
          </div>
        ) : null}

        {clients.isPending ? (
          <LoadingRows />
        ) : clients.isError ? (
          <ErrorState error={clients.error} onRetry={() => void clients.refetch()} />
        ) : totalAll === 0 ? (
          <EmptyState
            icon={Users}
            title={String(t("clients.empty"))}
            description={String(t("clients.emptyHint"))}
            action={
              <Button onClick={openCreate} disabled={poolFull}>
                <Plus aria-hidden="true" />
                {t("clients.new")}
              </Button>
            }
          />
        ) : total === 0 ? (
          <EmptyState
            // An empty table has two causes and they need different words:
            // nothing matched what was typed, or nothing is in the state the
            // select names. The second is an ordinary answer rather than a
            // failed search - a healthy server asked for its expired clients
            // has none, and telling that operator to try another spelling
            // would be answering a question they did not ask.
            icon={needle ? MagnifyingGlass : Funnel}
            title={String(t(needle ? "clients.emptySearch" : "clients.emptyFilter"))}
            description={String(t(needle ? "clients.emptySearchHint" : "clients.emptyFilterHint"))}
            action={
              <Button
                variant="outline"
                onClick={() => {
                  // Both, whichever of the two emptied the table: a button
                  // offering to show everything again that left the other one
                  // in force would be a dead end.
                  setQuery("");
                  setFilter("all");
                }}
              >
                {t("common.clear")}
              </Button>
            }
          />
        ) : (
          <>
            {/* Shown at every width now, not just from md up. It used to
                report the rows on screen, which a phone could see for itself;
                it reports how many the search and the filter found, which on a
                paged list is a different number and the one worth the line. */}
            <p className="text-xs text-muted-foreground">
              {t("clients.count", { count: total })}
              {clients.data ? ` • ${String(t("clients.freeIps", { count: freeIps }))}` : ""}
            </p>

            {/* Paging, sorting, filtering and searching all land here as a new
                page of `visible` swapped in for the old one between two
                frames, with nothing on screen to say a request happened at
                all. `isPlaceholderData` is true for exactly that gap - the
                rows on screen are the previous page's, held over while this
                one's are in flight - so the table dims for it and un-dims
                the instant the real rows for this page arrive, which reads
                as the list turning a page rather than jump-cutting to a
                different one. A background poll of the *same* page never
                sets this: it already has this page's data, so nothing here
                fires and nothing flickers every thirty seconds. */}
            <div
              className={cn(
                "transition-opacity duration-200",
                clients.isPlaceholderData && "opacity-50",
              )}
            >
              <ClientTable
                clients={visible}
                sort={sort}
                onSortChange={setSort}
                busyName={busyName}
                onShowQr={openQr}
                onDownload={(client) => download.start(client.name)}
                onShowTraffic={setTrafficClient}
                onEdit={openEdit}
                onResetKeys={(client) => ask("resetKeys", client)}
                onResetUsage={(client) => ask("resetUsage", client)}
                onToggleEnabled={(client) => ask(client.enabled ? "disable" : "enable", client)}
                onDelete={(client) => ask("delete", client)}
              />
            </div>

            <Pagination
              page={currentPage}
              pageCount={pageCount}
              pageSize={pageSize}
              onChange={setPage}
              onPageSizeChange={choosePageSize}
            />
          </>
        )}
      </div>

      <ClientDialog
        open={formOpen}
        onOpenChange={setFormOpen}
        client={editing}
        subnetCidr={subnetCidr}
        serverDns={server.data?.dns ?? ""}
        defaultAllowedIps={server.data?.allowedIpsDefault ?? "0.0.0.0/0"}
        onCreated={handleCreated}
        onSaved={handleSaved}
      />

      <BulkRemoveDialog
        open={bulkOpen}
        onOpenChange={setBulkOpen}
        counts={bulkCounts}
        pending={bulkRemove.isPending}
        onRemove={askBulkRemove}
      />

      <QrDialog client={qrClient} open={qrOpen} onOpenChange={setQrOpen} />

      <ClientTrafficDialog
        client={trafficRow}
        onOpenChange={(open) => {
          if (!open) {
            setTrafficClient(null);
          }
        }}
        onResetUsage={(client) => ask("resetUsage", client)}
        resetting={resetUsage.isPending && resetUsage.variables === trafficRow?.name}
      />

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          {confirm && confirmText ? (
            <>
              <AlertDialogHeader>
                <AlertDialogTitle>
                  {t(confirmText.title, {
                    name: confirm.client?.name ?? "",
                    count: confirm.count,
                  })}
                </AlertDialogTitle>
                <AlertDialogDescription>
                  {t(confirmBody(confirm, confirmText.body))}
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
                <AlertDialogAction
                  className={
                    confirmText.destructive ? buttonVariants({ variant: "destructive" }) : undefined
                  }
                  onClick={runConfirmed}
                >
                  {t(confirmText.action)}
                </AlertDialogAction>
              </AlertDialogFooter>
            </>
          ) : null}
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
