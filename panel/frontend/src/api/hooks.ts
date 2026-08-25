/**
 * TanStack Query bindings for every endpoint the panel exposes.
 *
 * Two rules run through the whole file:
 *
 * - Every key comes from `queryKeys`, so a mutation can invalidate exactly what
 *   it changed. Creating a client touches the client list and the dashboard
 *   totals; it has no business throwing away the parameter catalog.
 * - Every mutation is typed with ApiError, so callers get `detail` for a toast
 *   and `errors` for the field that was rejected instead of a bare Error.
 *
 * The panel writes those files, but it is not the only process doing so - two
 * workers and a collector all do - so the list queries poll slowly in the
 * background: a change made in another tab, or by the collector enforcing a
 * quota, must show up here without a reload.
 */

import * as React from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { api, type ApiError } from "./client";
import {
  SETTINGS_DEFAULTS,
  type ApiAuth,
  type ApiGroup,
  type ApiOperation,
  type ApiParam,
  type ApiSpec,
  type ApiToken,
  type ApiTokenIssued,
  type BulkLimit,
  type BulkLimitResult,
  type BulkRemoveOptions,
  type ChangeCredentialsInput,
  type Client,
  type ClientsQuery,
  type ClientsResponse,
  type ConfigEndpointMode,
  type CreateApiTokenInput,
  type CreateClientInput,
  type EventPage,
  type EventsQuery,
  type LiveStats,
  type LogSource,
  type LoginInput,
  type LoginSession,
  type PanelEvent,
  type ParamPreview,
  type ParamPreviewResult,
  type ParamSpec,
  type ReconfigureInput,
  type RestoreResult,
  type ServerConfig,
  type ServerSaveResult,
  type ServerStatus,
  type ServerUpdateInput,
  type ServiceLog,
  type Session,
  type Settings,
  type SettingsInput,
  type SettingsSaveResult,
  type StatsSummary,
  type ThemePreference,
  type TlsCertificate,
  type TrafficHistory,
  type TrafficPoint,
  type TrafficResetResult,
  type TrafficWindow,
  type TwoFactorEnableResult,
  type UpdateApiTokenInput,
  type UpdateClientInput,
  type UpdateCheck,
  type UpdateStatus,
} from "./types";

/* -------------------------------------------------------------------------- */
/* Keys                                                                        */
/* -------------------------------------------------------------------------- */

export const queryKeys = {
  session: () => ["session"] as const,
  /** Every page and narrowing of the list, and every client's detail. */
  clientsAll: () => ["clients"] as const,
  /**
   * One page of the list, as asked for. This is a cache key, not a filter to
   * invalidate by: `["clients", query]` does not prefix-match `["clients",
   * name]`, because TanStack compares position by position and an object is
   * never a partial match for a string. So invalidating this reaches every
   * narrowing of the list and no client's detail view.
   *
   * That is what three mutations here used to do, having read the name as "the
   * clients key". They left the open detail dialog showing the key that had
   * just been rotated, or the config that had just been rewritten under it.
   * Reaching for clientsAll - which is what CLIENT_LIST_KEYS is built on - is
   * the only spelling that covers both.
   *
   * The query argument has no default for that reason. There is no meaning for
   * `clientsPage()` with nothing passed, and a default was what let three call
   * sites ask for one and get a key that looked like it covered everything.
   */
  clientsPage: (query: ClientsQuery) => ["clients", query] as const,
  client: (name: string) => ["clients", name] as const,
  server: () => ["server"] as const,
  serverStatus: () => ["server", "status"] as const,
  serverParams: () => ["server", "params"] as const,
  liveStats: () => ["stats", "live"] as const,
  statsSummary: () => ["stats", "summary"] as const,
  /**
   * The prefix, not the key: both traffic queries append the window they asked
   * for, so one range is not served from another's cache entry. Invalidating
   * either of these still covers every window that has been fetched.
   */
  serverTraffic: () => ["stats", "traffic"] as const,
  clientTraffic: (name: string) => ["clients", name, "traffic"] as const,
  /**
   * Every narrowing and page of the event log, and one of them.
   *
   * The prefix exists for exactly one mutation. Nothing the panel does puts a
   * row in this table from a tab that is looking at it - the log is read on a
   * tab that is unmounted while anything worth recording is being done, and it
   * comes back on the list clock and on its own refresh button. Clearing it is
   * the exception, and it is the whole table rather than a page of it: every
   * cached narrowing is wrong the moment it returns.
   */
  eventsAll: () => ["events"] as const,
  /** One narrowing of the log. No default, for the reason clientsPage has none. */
  events: (query: EventsQuery) => ["events", query] as const,
  serviceLog: (source: LogSource, lines: number) => ["logs", source, lines] as const,
  settings: () => ["settings"] as const,
  /** Every certificate the page has read, whichever path it named. */
  certificateAll: () => ["settings", "certificate"] as const,
  certificate: (path: string, key: string) => ["settings", "certificate", path, key] as const,
  loginSessions: () => ["settings", "sessions"] as const,
  apiTokens: () => ["settings", "tokens"] as const,
  /** The API's own description. Fetched once; only an upgrade can change it. */
  apiSpec: () => ["openapi"] as const,
  updateCheck: () => ["update", "check"] as const,
  updateStatus: () => ["update", "status"] as const,
} as const;

/** How often the dashboard's live blob is polled while the tab is visible. */
const LIVE_POLL_MS = 2000;
/** Ceiling for the backoff after consecutive failures. */
const LIVE_POLL_MAX_MS = 10_000;
/** Background refresh for lists another tool can change behind our back. */
const LIST_POLL_MS = 30_000;
/**
 * Every query on that clock, and the reason each of them names it rather than
 * leaving the default alone.
 *
 * A hidden tab does not poll: the interval goes on firing and every firing is
 * skipped, so a tab left alone for ten minutes comes back having missed twenty
 * of them and waits up to thirty seconds more for the next. That would be
 * unremarkable if the whole page aged at the same rate - but these sit beside
 * the live blob, which is polled every two seconds and does refetch the moment
 * the tab is looked at again. The rates tick, the lamps move, and the usage
 * figures, the data limits and the status words beside them are ten minutes
 * old with nothing on screen to say so. What that reads as is a panel that
 * needs reloading before it can be believed.
 *
 * `staleTime` still applies, so glancing away and back costs nothing: the
 * refetch only happens if what is cached is older than that.
 */
const LIST_QUERY = {
  refetchInterval: LIST_POLL_MS,
  refetchIntervalInBackground: false,
  refetchOnWindowFocus: true,
} as const;
/**
 * How long the collector's blob may sit unchanged before nothing inside it
 * describes the present. A couple of missed writes is a busy box; this much
 * silence is a collector that has stopped.
 */
const LIVE_STALE_MS = 15_000;

/**
 * A 4xx is an answer, not an outage: retrying a 401 or a validation error only
 * delays the message the user needs to see.
 */
function retryTransportOnly(failureCount: number, error: ApiError): boolean {
  if (error.status >= 400 && error.status < 500) {
    return false;
  }
  return failureCount < 1;
}

function invalidate(client: QueryClient, keys: readonly (readonly unknown[])[]): void {
  for (const key of keys) {
    void client.invalidateQueries({ queryKey: key });
  }
}

/* -------------------------------------------------------------------------- */
/* Coercion helpers                                                            */
/* -------------------------------------------------------------------------- */

type Raw = Record<string, unknown>;

function asRecord(value: unknown): Raw {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Raw) : {};
}

function asString(value: unknown, fallback = ""): string {
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return fallback;
}

function asNumber(value: unknown, fallback = 0): number {
  const n = typeof value === "string" ? Number(value) : value;
  return typeof n === "number" && Number.isFinite(n) ? n : fallback;
}

function asBoolean(value: unknown, fallback = false): boolean {
  if (typeof value === "boolean") {
    return value;
  }
  if (typeof value === "string") {
    return ["1", "true", "yes", "on"].includes(value.toLowerCase());
  }
  return fallback;
}

function asStringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

/** First key that is actually present, so a renamed field still lands somewhere sane. */
function pick(raw: Raw, names: readonly string[]): unknown {
  for (const name of names) {
    if (raw[name] !== undefined && raw[name] !== null) {
      return raw[name];
    }
  }
  return undefined;
}

/* -------------------------------------------------------------------------- */
/* Auth                                                                        */
/* -------------------------------------------------------------------------- */

/**
 * Who am I. The endpoint answers 200 with `authenticated: false` when there is
 * no session, so a logged-out visitor is a normal result and not an error, and
 * it is also what issues the CSRF cookie the first mutation needs.
 */
export function useSession(): UseQueryResult<Session, ApiError> {
  return useQuery<Session, ApiError>({
    queryKey: queryKeys.session(),
    queryFn: () => api.get<Session>("auth/session"),
    retry: retryTransportOnly,
    staleTime: 30_000,
  });
}

export function useLogin(): UseMutationResult<Session, ApiError, LoginInput> {
  const client = useQueryClient();
  return useMutation<Session, ApiError, LoginInput>({
    mutationFn: (input) => api.post<Session>("auth/login", input),
    onSuccess: (session) => {
      client.setQueryData(queryKeys.session(), session);
      // Identity changed: everything already cached was fetched as someone
      // else (or as nobody), so mark it all stale rather than guessing.
      void client.invalidateQueries();
    },
  });
}

export function useLogout(): UseMutationResult<void, ApiError, void> {
  const client = useQueryClient();
  return useMutation<void, ApiError, void>({
    mutationFn: () => api.post<void>("auth/logout"),
    // onSettled, not onSuccess. Peer names, addresses and traffic are not for
    // the next visitor to read out of a cache, and a logout POST that came
    // back 500 - or never came back - is not evidence that the session behind
    // it survived. The failing case is the one where the cache mattered most:
    // the session is gone server-side, the browser is still holding a
    // dashboard's worth of client data, and nothing is going to come and
    // collect it.
    onSettled: () => {
      client.clear();
    },
  });
}

/* -------------------------------------------------------------------------- */
/* Clients                                                                     */
/* -------------------------------------------------------------------------- */

/**
 * A query object as a search string, leaving out everything the server defaults.
 *
 * Generic because two lists use it - clients and events - and both have the same
 * rule: a field that is absent or empty is a field the server chooses, so it does
 * not travel. That also keeps the default request free of parameters, which is
 * what lets every page opening a list share one cache entry.
 */
function searchString<T extends object>(query: T): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== "") {
      params.set(key, String(value));
    }
  }
  const search = params.toString();
  return search ? `?${search}` : "";
}

/**
 * One page of clients, narrowed and ordered by the server.
 *
 * Every part of that used to happen in the browser, over a list of every client
 * on the server. It stopped scaling for the obvious reason: a panel with a few
 * thousand clients sent all of them every thirty seconds to draw twenty rows,
 * and the search box could not begin filtering until the last one had arrived.
 *
 * `placeholderData` keeps the previous page on screen while the next one is
 * being fetched. Without it every keystroke in the search box and every page
 * step blanks the table to a skeleton and back, which reads as a much slower
 * panel than the same requests feel like when the old rows simply stay put.
 */
export function useClients(query: ClientsQuery = {}): UseQueryResult<ClientsResponse, ApiError> {
  return useQuery<ClientsResponse, ApiError>({
    queryKey: queryKeys.clientsPage(query),
    queryFn: () => api.get<ClientsResponse>(`clients${searchString(query)}`),
    retry: retryTransportOnly,
    ...LIST_QUERY,
    placeholderData: (previous) => previous,
  });
}

/**
 * One client by name, with the fields a row does not carry.
 *
 * The list is a page of rows and a row is not quite a whole client: `dns` is
 * read out of the client's own config file, so serving it on every row cost the
 * server a file open per client on every poll, for something no column shows.
 * Anything that edits a client fetches it here first.
 *
 * Deliberately not polled, unlike the list beside it. It backs a form that is
 * open because somebody is typing into it, and a background refetch under a
 * half-filled form is a race to overwrite what they have typed rather than
 * freshness anybody asked for. It is fetched again when the dialog is next
 * opened, which is the only moment the answer is wanted.
 */
export function useClient(name: string | null): UseQueryResult<Client, ApiError> {
  return useQuery<Client, ApiError>({
    queryKey: queryKeys.client(name ?? ""),
    queryFn: () => api.get<Client>(`clients/${encodeURIComponent(name ?? "")}`),
    enabled: name !== null,
    retry: retryTransportOnly,
  });
}

/** Keys that change when the set of clients, their quotas or their state changes. */
const CLIENT_LIST_KEYS = [queryKeys.clientsAll(), queryKeys.statsSummary()] as const;

export function useCreateClient(): UseMutationResult<Client, ApiError, CreateClientInput> {
  const client = useQueryClient();
  return useMutation<Client, ApiError, CreateClientInput>({
    mutationFn: (input) => api.post<Client>("clients", input),
    onSuccess: () => invalidate(client, CLIENT_LIST_KEYS),
  });
}

export interface UpdateClientVars {
  /** Current name; it is the identifier in the URL. */
  name: string;
  /** Any subset of the editable fields. `name` inside renames the client. */
  changes: UpdateClientInput;
}

export function useUpdateClient(): UseMutationResult<Client, ApiError, UpdateClientVars> {
  const client = useQueryClient();
  return useMutation<Client, ApiError, UpdateClientVars>({
    mutationFn: ({ name, changes }) =>
      api.put<Client>(`clients/${encodeURIComponent(name)}`, changes),
    onSuccess: (_data, { name }) => {
      invalidate(client, [...CLIENT_LIST_KEYS, queryKeys.client(name)]);
    },
  });
}

export function useDeleteClient(): UseMutationResult<void, ApiError, string> {
  const client = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (name) => api.del<void>(`clients/${encodeURIComponent(name)}`),
    onSuccess: (_data, name) => {
      // The peer is gone; anything still holding it would 404.
      client.removeQueries({ queryKey: queryKeys.client(name) });
      invalidate(client, CLIENT_LIST_KEYS);
    },
  });
}

export interface RemovedClients {
  /** The clients the server actually removed, by the names it had for them. */
  removed: string[];
}

/**
 * Delete whole categories of client in one request: the expired, the stopped,
 * or both together.
 *
 * One request rather than one per client because each removal rewrites the
 * server config and re-applies the interface: clearing thirty clients a row at
 * a time is thirty of each, and thirty windows in which the config on disk is
 * halfway through the sweep.
 *
 * The selection is sent as categories rather than as the names the page happens
 * to be showing, because the server holds the list and the browser holds a copy
 * that was already up to half a minute old when the operator clicked.
 *
 * `removed` is what the server actually took, which need not be what the page
 * counted: a client removed over SSH in between is simply not in the answer,
 * and asking for both categories removes a client that is in both of them once.
 *
 * `clients/remove-expired` is the same sweep with `expired` alone. The panel
 * has no separate binding for it - one control covers both categories now - but
 * it stays part of the API for anything scripted against it.
 */
export function useBulkRemoveClients(): UseMutationResult<
  RemovedClients,
  ApiError,
  BulkRemoveOptions
> {
  const client = useQueryClient();
  return useMutation<RemovedClients, ApiError, BulkRemoveOptions>({
    mutationFn: async (options) => {
      const raw = asRecord(await api.post<Raw>("clients/bulk-remove", options));
      return { removed: asStringList(pick(raw, ["removed"])) };
    },
    onSuccess: ({ removed }) => {
      for (const name of removed) {
        client.removeQueries({ queryKey: queryKeys.client(name) });
      }
      invalidate(client, CLIENT_LIST_KEYS);
    },
  });
}

/**
 * Give every client the same bandwidth limit, overwriting whatever each of them
 * had. There is no undo, so the caller asks first.
 *
 * The whole client list is invalidated rather than patched: every row's limit
 * has just changed, and reconstructing that in the cache would be re-deriving
 * what one refetch already says.
 */
export function useBulkLimitClients(): UseMutationResult<BulkLimitResult, ApiError, BulkLimit> {
  const client = useQueryClient();
  return useMutation<BulkLimitResult, ApiError, BulkLimit>({
    mutationFn: async (limit) => {
      const raw = asRecord(await api.post<Raw>("clients/bulk-limit", limit));
      return {
        changed: asNumber(pick(raw, ["changed"])),
        applied: asBoolean(pick(raw, ["applied"]), true),
        reason: asString(pick(raw, ["reason"])),
      };
    },
    onSuccess: () => invalidate(client, CLIENT_LIST_KEYS),
  });
}

export interface ToggleClientVars {
  name: string;
  enabled: boolean;
}

/**
 * Disable keeps the peer in the config and its address reserved, and only
 * removes the key from the kernel, so re-enabling needs no re-import.
 */
export function useToggleClient(): UseMutationResult<Client, ApiError, ToggleClientVars> {
  const client = useQueryClient();
  return useMutation<Client, ApiError, ToggleClientVars>({
    mutationFn: ({ name, enabled }) =>
      api.put<Client>(`clients/${encodeURIComponent(name)}`, { enabled }),
    onSuccess: (_data, { name }) => {
      invalidate(client, [...CLIENT_LIST_KEYS, queryKeys.client(name)]);
    },
  });
}

/** New keypair and preshared key. The old config stops working immediately. */
export function useResetKeys(): UseMutationResult<Client, ApiError, string> {
  const client = useQueryClient();
  return useMutation<Client, ApiError, string>({
    mutationFn: (name) => api.post<Client>(`clients/${encodeURIComponent(name)}/reset-keys`),
    onSuccess: (_data, name) => {
      invalidate(client, [...CLIENT_LIST_KEYS, queryKeys.client(name)]);
    },
  });
}

/**
 * Zeroes the counters the panel reports by storing an offset, and deletes the
 * client's stored days and months outright. traffic.db is untouched either way.
 *
 * The history query goes with the list keys because the dialog it feeds can be
 * open while this runs - the button is in its footer - and a chart still
 * showing the months that have just been deleted is the one place the panel
 * could contradict itself about what happened.
 */
export function useResetUsage(): UseMutationResult<void, ApiError, string> {
  const client = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (name) => api.post<void>(`clients/${encodeURIComponent(name)}/reset-usage`),
    onSuccess: (_data, name) => {
      invalidate(client, [
        ...CLIENT_LIST_KEYS,
        queryKeys.client(name),
        queryKeys.clientTraffic(name),
      ]);
    },
  });
}

/* -------------------------------------------------------------------------- */
/* Server                                                                      */
/* -------------------------------------------------------------------------- */

export function useServer(): UseQueryResult<ServerConfig, ApiError> {
  return useQuery<ServerConfig, ApiError>({
    queryKey: queryKeys.server(),
    queryFn: () => api.get<ServerConfig>("server"),
    retry: retryTransportOnly,
  });
}

export function useSaveServer(): UseMutationResult<ServerSaveResult, ApiError, ServerUpdateInput> {
  const client = useQueryClient();
  return useMutation<ServerSaveResult, ApiError, ServerUpdateInput>({
    mutationFn: (changes) => api.put<ServerSaveResult>("server", changes),
    onSuccess: () => {
      // A DNS, endpoint or allowed-IPs change rewrites every client config, so
      // the client list is stale too, not just the server view - and so is any
      // client's detail, which is why this is clientsAll rather than a page of
      // the list.
      invalidate(client, [queryKeys.server(), queryKeys.serverStatus(), queryKeys.clientsAll()]);
    },
  });
}

/**
 * Status shells out to awg, so it is polled far more slowly than the live blob
 * and never in a hidden tab.
 */
export function useServerStatus(): UseQueryResult<ServerStatus, ApiError> {
  return useQuery<ServerStatus, ApiError>({
    queryKey: queryKeys.serverStatus(),
    queryFn: () => api.get<ServerStatus>("server/status"),
    retry: retryTransportOnly,
    ...LIST_QUERY,
  });
}

/**
 * The parameter catalog: labels, help text, ranges and badges for every
 * obfuscation setting. It only changes when the tools are upgraded, so it is
 * fetched once and kept.
 */
export function useServerParams(): UseQueryResult<ParamSpec[], ApiError> {
  return useQuery<ParamSpec[], ApiError>({
    queryKey: queryKeys.serverParams(),
    queryFn: () => api.get<ParamSpec[]>("server/params"),
    retry: retryTransportOnly,
    staleTime: 5 * 60_000,
  });
}

/**
 * Draw a whole new set, from the band the chosen profile names. This is a
 * preview: nothing is written until the user saves the form, because applying
 * it costs every client a re-import.
 *
 * The warnings come back with it and are worth showing. For the obfuscation
 * scope they are usually empty by construction; for the AmneziaWG 3.0 group
 * they are the point, because every value in it needs 3.0 at the far end.
 */
export function useReconfigureObfuscation(): UseMutationResult<
  ParamPreviewResult,
  ApiError,
  ReconfigureInput
> {
  return useMutation<ParamPreviewResult, ApiError, ReconfigureInput>({
    mutationFn: async (input) => {
      const raw = await api.post<Raw>("server/reconfigure", input);
      const body = asRecord(pick(raw, ["params"]) ?? raw);
      const params: ParamPreview = {};
      for (const [key, value] of Object.entries(body)) {
        if (typeof value === "string" || typeof value === "number") {
          params[key] = String(value);
        }
      }
      return { params, warnings: asStringList(raw.warnings) };
    },
  });
}

/** awg-quick down and up. Every session drops for a moment. */
export function useRestartServer(): UseMutationResult<void, ApiError, void> {
  return useTunnelAction("server/restart");
}

/** Stop the tunnel and leave it stopped. Every session drops and none come back. */
export function useStopServer(): UseMutationResult<void, ApiError, void> {
  return useTunnelAction("server/stop");
}

/** Start a stopped tunnel. */
export function useStartServer(): UseMutationResult<void, ApiError, void> {
  return useTunnelAction("server/start");
}

/*
 * The three of them differ only in the path.
 *
 * What has to happen afterwards is the same in every case and is the part worth
 * not writing three times: the interface has changed state, so the status page,
 * the live poll and the client list are all describing a server that no longer
 * exists. The live poll would catch up within two seconds by itself; the other
 * two would not, and the gap is exactly long enough for somebody to press the
 * button again because nothing appeared to happen.
 */
function useTunnelAction(path: string): UseMutationResult<void, ApiError, void> {
  const client = useQueryClient();
  return useMutation<void, ApiError, void>({
    mutationFn: () => api.post<void>(path),
    onSuccess: () => {
      invalidate(client, [queryKeys.serverStatus(), queryKeys.liveStats(), queryKeys.clientsAll()]);
    },
  });
}

/* -------------------------------------------------------------------------- */
/* Stats                                                                       */
/* -------------------------------------------------------------------------- */

/**
 * The 2 s poll that drives the dashboard and the live columns of the client
 * table. Reading it costs the server a file read, not an awg call.
 *
 * After a failure the interval backs off towards 10 s so a restarting panel is
 * not hammered twice a second, and snaps back to 2 s on the first success
 * because TanStack resets the failure counter.
 */
/**
 * Pass as `peers` to read the aggregate block and nothing else.
 *
 * Named rather than written as a bare `[]` at each call site because the two
 * are not interchangeable with the parameter left out: omitting it asks for
 * every peer on the server, which is the one thing these callers must not do.
 */
export const NO_PEERS: readonly string[] = [];

/**
 * The most keys this hook will put in a URL, matching the server's
 * MAX_LIVE_PEERS.
 *
 * Enforced here rather than at the call sites because this is the function that
 * builds the request line, and the limit is a property of the request line: the
 * keys are 44 base64 characters that percent-encode to about 51 bytes each, and
 * the panel's web server refuses a request line past 8190 bytes outright.
 * Beyond this many the whole poll fails and every visible row loses its rates.
 *
 * Past the cap the parameter is dropped and the whole blob is read instead, and
 * that is a correction rather than a preference. The keys over the cap used to
 * be sliced off, which was described as costing those rows their rates - it cost
 * them rather more than that. A peer the blob says nothing about is a peer that
 * is not on the interface, so the rows past the cap did not go blank: they read
 * as offline, at zero, next to a client that was pulling a hundred megabits.
 * Answering with more than was asked for is the only reading of this that is
 * not simply wrong, and it now has a caller - the client list offers "all",
 * which is five hundred rows on a big enough server.
 */
const MAX_LIVE_PEERS = 150;

export function useLiveStats(
  options: { enabled?: boolean; peers?: readonly string[] } = {},
): UseQueryResult<LiveStats, ApiError> {
  const { enabled = true, peers } = options;
  // Sorted so that the same set of rows in a different order is the same cache
  // entry: the table re-sorts on every poll, and a key that moved with it would
  // refetch the identical page under a new name twice a second.
  const named = peers === undefined ? undefined : [...peers].sort();
  const wanted =
    named !== undefined && named.length <= MAX_LIVE_PEERS ? named.join(",") : undefined;
  return useQuery<LiveStats, ApiError>({
    queryKey: [...queryKeys.liveStats(), wanted ?? "*"],
    queryFn: () =>
      api.get<LiveStats>(
        wanted === undefined ? "stats/live" : `stats/live?peers=${encodeURIComponent(wanted)}`,
      ),
    enabled,
    // One retry would double every failure count and blur the backoff.
    retry: false,
    refetchInterval: (query) => {
      const failures = query.state.fetchFailureCount;
      if (failures <= 0) {
        return LIVE_POLL_MS;
      }
      return Math.min(LIVE_POLL_MS * 2 ** failures, LIVE_POLL_MAX_MS);
    },
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    staleTime: 0,
  });
}

export interface LiveFreshness {
  /** The blob is still moving: its rates and online flags describe this moment. */
  fresh: boolean;
  /** The blob has stopped moving: the collector is not running. */
  stale: boolean;
  /** Local milliseconds at which the collector's timestamp last changed. */
  changedAt: number;
}

/**
 * Whether the collector is still writing, and since when it has not been.
 *
 * Measured by whether its timestamp *moves*, not by how old that timestamp
 * looks. Comparing `ts` against the browser's clock would make the answer
 * depend on two things it has no business depending on: the poll interval, which
 * an admin can set anywhere between 1 and 60 seconds, and the gap between the
 * server's clock and the reader's. A blob that stops changing is a collector
 * that stopped, whatever the numbers in it say.
 *
 * `fresh` and `stale` are not opposites: while the first blob is still on its
 * way, and after the request fails, neither is true. Nothing may be quoted from
 * the blob then, but nothing may be said about the collector either - that is
 * the caller's own error state to explain.
 *
 * No timer: the live query re-renders its caller every couple of seconds even
 * while the blob sits still, which is what makes the comparison fire.
 */
export function useLiveFreshness(live: UseQueryResult<LiveStats, ApiError>): LiveFreshness {
  const lastChange = React.useRef<{ ts: number; at: number }>({ ts: 0, at: Date.now() });
  if (live.data && live.data.ts !== lastChange.current.ts) {
    lastChange.current = { ts: live.data.ts, at: Date.now() };
  }
  const known = live.data !== undefined && !live.isError;
  const aged = Date.now() - lastChange.current.at > LIVE_STALE_MS;
  return { fresh: known && !aged, stale: known && aged, changedAt: lastChange.current.at };
}

/**
 * Read the list again as soon as the server configuration underneath it moves.
 *
 * The panel reads the tunnel on two clocks, and the gap between them is visible
 * in exactly one place. Usage and rates come off the live blob every two
 * seconds; whether a client is switched on, and the status word beside it, come
 * off the client list every thirty. A client that crosses its data limit is
 * taken off the interface within one collector poll - but for the rest of that
 * half minute the row goes on reading "idle" and enabled beneath a bar the same
 * poll has just filled to the end. What that looks like is a limit that was not
 * enforced, and the only thing actually wrong is which clock the row is on.
 *
 * The blob already comes back every two seconds, so the cheapest place to learn
 * that something moved is the answer already in flight: `confStamp` changes when
 * the configuration is rewritten, and one refetch then costs less than shortening
 * the list's own interval by any amount. Fifteen times the requests, forever, to
 * catch something that happens to a client about as often as it exhausts an
 * allowance.
 *
 * Edge triggered, and quiet by construction. The reconcile pass re-asserts a
 * decision it has already applied on every run, and `set_clients_enabled` writes
 * nothing when nothing moved - so a steady server never touches the file and
 * this never fires. What does move it is a client added, removed, switched off
 * by hand, over SSH, or by enforcement, which is the whole set of events that
 * make the list wrong.
 *
 * The first stamp is recorded rather than acted on: arriving somewhere is not a
 * change, and a refetch on mount would undo the `staleTime` that makes glancing
 * at this page free.
 */
export function useEnforcementCatchup(live: UseQueryResult<LiveStats, ApiError>): void {
  const client = useQueryClient();
  const seen = React.useRef<string | null>(null);
  const stamp = live.data?.confStamp ?? "";
  React.useEffect(() => {
    // Empty is "there is no configuration to stamp", which is not news, and
    // treating it as one would refetch the list every time the collector's
    // answer arrived without a server behind it.
    if (!stamp) {
      return;
    }
    const previous = seen.current;
    seen.current = stamp;
    if (previous === null || previous === stamp) {
      return;
    }
    invalidate(client, CLIENT_LIST_KEYS);
  }, [client, stamp]);
}

function normalizeSummary(raw: Raw): StatsSummary {
  return {
    totalClients: asNumber(pick(raw, ["totalClients", "clients", "total"])),
    onlineClients: asNumber(pick(raw, ["onlineClients", "online"])),
    disabledClients: asNumber(pick(raw, ["disabledClients", "disabled"])),
    todayRx: asNumber(pick(raw, ["todayRx", "rxToday", "todayUpload"])),
    todayTx: asNumber(pick(raw, ["todayTx", "txToday", "todayDownload"])),
    totalRx: asNumber(pick(raw, ["totalRx", "rxTotal", "rxBytes"])),
    totalTx: asNumber(pick(raw, ["totalTx", "txTotal", "txBytes"])),
    uptime: asNumber(pick(raw, ["uptime", "uptimeSec"])),
    ifaceUp: asBoolean(pick(raw, ["ifaceUp", "interfaceUp"]), true),
  };
}

/**
 * Dashboard cards. Coerced through a normalizer because these totals are the
 * one payload the contract does not pin field for field, and a card showing
 * "NaN" is worse than one showing zero.
 */
export function useStatsSummary(): UseQueryResult<StatsSummary, ApiError> {
  return useQuery<StatsSummary, ApiError>({
    queryKey: queryKeys.statsSummary(),
    queryFn: async () => normalizeSummary(await api.get<Raw>("stats/summary")),
    retry: retryTransportOnly,
    ...LIST_QUERY,
  });
}

/**
 * A traffic history, from whichever endpoint serves it.
 *
 * Coerced rather than trusted, like the summary beside it and for the same
 * reason: these are the numbers a chart is drawn from, and one `undefined` in a
 * series does not produce a missing bar - it produces a bar of the wrong height,
 * or a y-axis that has silently rescaled around a NaN. A bucket that arrives
 * malformed is drawn as the zero it cannot be distinguished from.
 */
function normalizeTraffic(raw: Raw): TrafficHistory {
  return {
    daily: normalizeSeries(raw.daily),
    monthly: normalizeSeries(raw.monthly),
    // Null when nothing has been recorded, which is a state rather than a
    // failure; it arrives here as the empty string either way.
    earliest: asString(raw.earliest),
  };
}

function normalizeSeries(value: unknown): TrafficPoint[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((entry) => {
    const point = asRecord(entry);
    return {
      period: asString(point.period),
      rx: asNumber(point.rx),
      tx: asNumber(point.tx),
    };
  });
}

/**
 * A window as the query string spells it, and as a cache key.
 *
 * One function for both, so a window can never be fetched under a key that
 * describes a different one. The default window is the empty string at both
 * ends of that: no parameters on the request, and one cache entry that every
 * page opening the chart shares.
 */
function windowSearch(wanted: TrafficWindow | null | undefined): string {
  return wanted ? `?from=${wanted.from}&to=${wanted.to}` : "";
}

/**
 * What the whole server has carried, by day and by month.
 *
 * On the list clock rather than the live blob's, because that is the rate the
 * data behind it actually moves at: the collector rewrites today's row every ten
 * seconds and every other bar in the series was settled at midnight. Polling
 * this every two seconds would be fifteen times the requests to redraw the same
 * picture.
 *
 * A window is part of the key rather than a refetch of the same one, so moving
 * the range back and forth between two spans redraws from cache instead of
 * asking twice. `placeholderData` keeps the previous window's bars on screen
 * while the next window is in flight: the alternative is a chart that blanks to
 * a skeleton every time a date input is touched, which reads as a much slower
 * panel than the same request feels like when the picture simply stays put.
 */
export function useServerTraffic(
  wanted?: TrafficWindow | null,
): UseQueryResult<TrafficHistory, ApiError> {
  const search = windowSearch(wanted);
  return useQuery<TrafficHistory, ApiError>({
    queryKey: [...queryKeys.serverTraffic(), search],
    queryFn: async () => normalizeTraffic(asRecord(await api.get<Raw>(`stats/traffic${search}`))),
    retry: retryTransportOnly,
    ...LIST_QUERY,
    placeholderData: (previous) => previous,
  });
}

/**
 * The same two series for one client, over the same windows.
 *
 * Fetched only while the dialog naming a client is open, which is what `enabled`
 * is for: this is a per-client query and a table of fifty rows must not turn
 * into fifty of them. It keeps the list's polling once it is open, so a dialog
 * left up while a client is transferring shows today's bar growing.
 */
export function useClientTraffic(
  name: string | null,
  wanted?: TrafficWindow | null,
): UseQueryResult<TrafficHistory, ApiError> {
  const search = windowSearch(wanted);
  return useQuery<TrafficHistory, ApiError>({
    queryKey: [...queryKeys.clientTraffic(name ?? ""), search],
    queryFn: async () =>
      normalizeTraffic(
        asRecord(await api.get<Raw>(`clients/${encodeURIComponent(name ?? "")}/traffic${search}`)),
      ),
    enabled: name !== null,
    retry: retryTransportOnly,
    ...LIST_QUERY,
    placeholderData: (previous) => previous,
  });
}

/**
 * Clear every traffic figure on the server, and resolve with how much went.
 *
 * The server-wide sibling of `useResetUsage`, and it takes what that one leaves:
 * the server's own days as well as every client's. Nothing about the clients
 * themselves changes, so the list keys below are invalidated for their usage
 * columns rather than because a client moved.
 *
 * Reset rather than invalidated for the two histories, which is the difference
 * between the chart emptying now and emptying when the round trip answers. The
 * button is on the chart: leaving last month's bars up while the request comes
 * back would be the one place the panel showed the traffic it had just been
 * told to forget. Everything else is invalidated, because those are numbers
 * being corrected rather than answers that have stopped existing - and a
 * dashboard behind this page should not blink through a loading state.
 *
 * The live blob is invalidated too, though nothing in it moves: the peer totals
 * it carries are merged over the list's own, so a page holding a two second old
 * copy of them would re-add what has just gone until its next poll.
 */
export function useResetTraffic(): UseMutationResult<TrafficResetResult, ApiError, void> {
  const client = useQueryClient();
  return useMutation<TrafficResetResult, ApiError, void>({
    mutationFn: async () => {
      const raw = asRecord(await api.post<Raw>("stats/traffic/reset"));
      return {
        clients: asNumber(raw.clients),
        serverDays: asNumber(pick(raw, ["serverDays", "server_days"])),
        clientDays: asNumber(pick(raw, ["clientDays", "client_days"])),
      };
    },
    onSuccess: () => {
      void client.resetQueries({ queryKey: queryKeys.serverTraffic() });
      invalidate(client, [...CLIENT_LIST_KEYS, queryKeys.liveStats()]);
    },
  });
}

/* -------------------------------------------------------------------------- */
/* Events and logs                                                             */
/* -------------------------------------------------------------------------- */

function normalizeEvent(value: unknown): PanelEvent {
  const raw = asRecord(value);
  const detail = asRecord(raw.detail);
  return {
    id: asNumber(raw.id),
    at: asString(raw.at),
    kind: asString(raw.kind),
    severity: raw.severity === "warning" ? "warning" : "info",
    actor: asString(raw.actor),
    actorIp: asString(raw.actorIp),
    target: asString(raw.target),
    detail,
  };
}

/**
 * One page of the panel's own log, newest first.
 *
 * Coerced rather than trusted, like the summary and the traffic series, and for
 * a reason of its own: `detail` is the one payload in this API whose shape the
 * contract does not pin, because it differs per kind. Everything read out of it
 * is interpolated into a sentence, and a missing value has to arrive as
 * something a template can render rather than as `undefined` in the middle of a
 * line the operator is trying to read.
 *
 * On the list clock, because that is the rate the table behind it moves: events
 * appear when somebody does something or when the collector enforces something,
 * neither of which is a thing that happens twice a second. `placeholderData`
 * keeps the current page on screen while the next one is fetched, so paging and
 * filtering do not blank the list to a skeleton and back.
 */
export function useEvents(query: EventsQuery = {}): UseQueryResult<EventPage, ApiError> {
  return useQuery<EventPage, ApiError>({
    queryKey: queryKeys.events(query),
    queryFn: async () => {
      const raw = asRecord(await api.get<Raw>(`events${searchString(query)}`));
      const rows = pick(raw, ["events"]);
      return {
        events: (Array.isArray(rows) ? rows : []).map(normalizeEvent),
        total: asNumber(pick(raw, ["total"])),
        page: asNumber(pick(raw, ["page"]), 1),
        pageSize: asNumber(pick(raw, ["pageSize"]), 25),
      };
    },
    retry: retryTransportOnly,
    ...LIST_QUERY,
    placeholderData: (previous) => previous,
  });
}

/**
 * Empty the log, and resolve with how many rows went.
 *
 * The whole table, whatever the list is currently filtered to - the server
 * ignores the query string here, so nothing about the filters is sent. What
 * comes back is the count, because the page already knows the log is empty now
 * and does not know what was in it: the list it was showing was one page of
 * twenty-five.
 *
 * Every cached page and narrowing is reset rather than invalidated, which is the
 * difference between the rows going now and the rows going when the refetch
 * answers. Invalidating would leave the log on screen for as long as the round
 * trip takes, on the one page that has just been told it is empty - and reset
 * refetches whatever is being looked at, so the list comes back by itself.
 */
export function useClearEvents(): UseMutationResult<number, ApiError, void> {
  const client = useQueryClient();
  return useMutation<number, ApiError, void>({
    mutationFn: async () => {
      const raw = asRecord(await api.del<Raw>("events"));
      return asNumber(pick(raw, ["removed"]));
    },
    onSuccess: () => void client.resetQueries({ queryKey: queryKeys.eventsAll() }),
  });
}

/**
 * What one service has written to the journal, oldest last.
 *
 * The one read in the panel that is deliberately not polled. Every fetch forks
 * journalctl, and a transcript that reprinted itself every thirty seconds would
 * also scroll under whoever was reading it - so this is fetched when the tab is
 * opened and again when the operator asks, which is what the card's refresh
 * button is for. `staleTime: 0` is what makes that button actually re-read
 * rather than hand back what is already cached.
 *
 * The query is keyed by the line count as well as the source, so asking for more
 * lines is a different question rather than a refetch of the same one.
 */
export function useServiceLog(
  source: LogSource,
  lines: number,
): UseQueryResult<ServiceLog, ApiError> {
  return useQuery<ServiceLog, ApiError>({
    queryKey: queryKeys.serviceLog(source, lines),
    queryFn: async () => {
      const raw = asRecord(
        await api.get<Raw>(`logs?source=${encodeURIComponent(source)}&lines=${lines}`),
      );
      const rows = pick(raw, ["lines"]);
      return {
        source,
        unit: asString(raw.unit),
        lines: (Array.isArray(rows) ? rows : []).map((entry) => {
          const line = asRecord(entry);
          return {
            at: asString(line.at),
            priority: asNumber(line.priority, 6),
            message: asString(line.message),
          };
        }),
        available: asBoolean(raw.available, true),
        reason: asString(raw.reason),
      };
    },
    retry: retryTransportOnly,
    staleTime: 0,
  });
}

/* -------------------------------------------------------------------------- */
/* Panel settings                                                              */
/* -------------------------------------------------------------------------- */

function asTheme(value: unknown): ThemePreference {
  return value === "light" || value === "dark" || value === "system" ? value : "system";
}

/** Anything unrecognised means "leave client configs as the server wrote them". */
function asEndpointMode(value: unknown): ConfigEndpointMode {
  return value === "domain" ? "domain" : "ip";
}

function normalizeSettings(raw: unknown): Settings {
  const body = asRecord(raw);
  const source = asRecord(pick(body, ["settings"]) ?? body);
  const out = { ...SETTINGS_DEFAULTS };
  for (const key of Object.keys(SETTINGS_DEFAULTS) as (keyof Settings)[]) {
    if (key === "theme") {
      out.theme = asTheme(source.theme ?? SETTINGS_DEFAULTS.theme);
    } else if (key === "configEndpointMode") {
      out.configEndpointMode = asEndpointMode(source.configEndpointMode);
    } else {
      out[key] = asString(source[key], SETTINGS_DEFAULTS[key]);
    }
  }
  return out;
}

/**
 * What a certificate covers, for the address picker in Settings and for the
 * address the page promises to reconnect at - and whether the panel can reach
 * the two files at all, which is what stands between a save and a confirmation
 * dialog for a restart that is never going to happen. Read from the files on
 * every fetch, so a renewal that changed the names shows up without anything
 * being saved.
 *
 * Both arguments ask about files the operator has typed rather than the ones
 * stored, which is the only way any of that can describe the save that is about
 * to happen instead of the one before it. The answer carries the two paths it
 * is about; a caller that cares whether it describes the form has to check.
 */
export function useCertificate(path = "", key = ""): UseQueryResult<TlsCertificate, ApiError> {
  const asked = path.trim();
  const askedKey = key.trim();
  return useQuery<TlsCertificate, ApiError>({
    queryKey: queryKeys.certificate(asked, askedKey),
    queryFn: async () => {
      const query = new URLSearchParams();
      if (asked) {
        query.set("path", asked);
      }
      if (askedKey) {
        query.set("key", askedKey);
      }
      const search = query.toString();
      const raw = asRecord(await api.get<Raw>(`settings/certificate${search ? `?${search}` : ""}`));
      return {
        path: asString(raw.path),
        names: asStringList(raw.names),
        domain: asString(raw.domain),
        // Not asNumber: its fallback is 0, and 0 here reads as "expires today"
        // - the loudest possible answer to "there is no certificate to ask
        // about", which is what null actually means.
        expiresInDays: typeof raw.expiresInDays === "number" ? raw.expiresInDays : null,
        problem: asString(raw.problem),
        keyPath: asString(raw.keyPath),
        keyProblem: asString(raw.keyProblem),
      };
    },
    retry: retryTransportOnly,
    staleTime: 60_000,
  });
}

export function useSettings(): UseQueryResult<Settings, ApiError> {
  return useQuery<Settings, ApiError>({
    queryKey: queryKeys.settings(),
    queryFn: async () => normalizeSettings(await api.get<Raw>("settings")),
    retry: retryTransportOnly,
    staleTime: 60_000,
  });
}

/**
 * Saving the port, listen address, base path or TLS paths rewrites
 * /etc/awg-panel.env and restarts the web service a couple of seconds later, so
 * this response is the last one the current URL will deliver. `needsRestart`
 * and `url` are what the reconnect overlay needs.
 */
export function useSaveSettings(): UseMutationResult<SettingsSaveResult, ApiError, SettingsInput> {
  const client = useQueryClient();
  return useMutation<SettingsSaveResult, ApiError, SettingsInput>({
    mutationFn: async (changes) => {
      const raw = await api.put<Raw>("settings", changes);
      return {
        settings: normalizeSettings(raw),
        needsRestart: asBoolean(pick(raw, ["needsRestart", "restarting"])),
        url: asString(pick(raw, ["url", "panelUrl"])),
        warnings: asStringList(pick(raw, ["warnings"])),
      };
    },
    onSuccess: (result) => {
      client.setQueryData(queryKeys.settings(), result.settings);
      // Panel name, theme and language are part of the session bootstrap the
      // shell renders from, and the certificate path may have just changed to
      // one covering entirely different names.
      invalidate(client, [queryKeys.session(), queryKeys.certificateAll()]);
    },
  });
}

/** Other sessions are invalidated by the server, so the user stays signed in here only. */
export function useChangeCredentials(): UseMutationResult<
  Session,
  ApiError,
  ChangeCredentialsInput
> {
  const client = useQueryClient();
  return useMutation<Session, ApiError, ChangeCredentialsInput>({
    mutationFn: (input) => api.post<Session>("settings/account", input),
    onSuccess: (session) => {
      // The answer is the bootstrap payload, so a rename reaches the account
      // menu in the top bar without a round trip of its own.
      client.setQueryData(queryKeys.session(), session);
      // Every other browser was just signed out, and the session list on the
      // same page would otherwise go on offering them until something
      // refetched it.
      invalidate(client, [queryKeys.loginSessions()]);
    },
  });
}

function normalizeLoginSession(value: unknown): LoginSession {
  const raw = asRecord(value);
  return {
    id: asString(raw.id),
    current: asBoolean(raw.current),
    createdAt: asString(raw.createdAt),
    lastSeenAt: asString(raw.lastSeenAt),
    expiresAt: asString(raw.expiresAt),
    ip: asString(raw.ip),
    browser: asString(raw.browser),
    platform: asString(raw.platform),
    userAgent: asString(raw.userAgent),
  };
}

/**
 * Every browser signed in to this account, the current one first.
 *
 * Polled slowly rather than read once: another device signing in is exactly the
 * event this page exists to show, and "last active" ages while the tab is open.
 * A session that has ended elsewhere is already absent from the answer, so no
 * row here is ever an offer to revoke something that is gone.
 */
export function useLoginSessions(): UseQueryResult<LoginSession[], ApiError> {
  return useQuery<LoginSession[], ApiError>({
    queryKey: queryKeys.loginSessions(),
    queryFn: async () => {
      const raw = asRecord(await api.get<Raw>("settings/sessions"));
      const rows = pick(raw, ["sessions"]);
      return (Array.isArray(rows) ? rows : []).map(normalizeLoginSession);
    },
    retry: retryTransportOnly,
    ...LIST_QUERY,
  });
}

/**
 * End one other session. The API refuses the caller's own with a 400, which the
 * UI prevents by not offering the button - the check is the server's either way.
 */
export function useRevokeSession(): UseMutationResult<void, ApiError, string> {
  const client = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.del<void>(`settings/sessions/${encodeURIComponent(id)}`),
    onSuccess: () => invalidate(client, [queryKeys.loginSessions()]),
  });
}

/** Sign out every other browser at once, keeping this one. Returns how many ended. */
export function useRevokeOtherSessions(): UseMutationResult<number, ApiError, void> {
  const client = useQueryClient();
  return useMutation<number, ApiError, void>({
    mutationFn: async () => {
      const raw = asRecord(await api.post<Raw>("settings/sessions/revoke-others"));
      return asNumber(pick(raw, ["ended"]));
    },
    onSuccess: () => invalidate(client, [queryKeys.loginSessions()]),
  });
}

function normalizeApiToken(value: unknown): ApiToken {
  const raw = asRecord(value);
  return {
    id: asString(raw.id),
    name: asString(raw.name),
    hint: asString(raw.hint),
    createdAt: asString(raw.createdAt),
    // Null is a value here rather than a missing field: it is how the API says
    // "this one does not expire", which is a different thing from "unknown".
    expiresAt: raw.expiresAt == null ? null : asString(raw.expiresAt),
    expiresIn: raw.expiresIn == null ? null : asNumber(raw.expiresIn),
    renewOnUse: asBoolean(raw.renewOnUse),
    lastUsedAt: raw.lastUsedAt == null ? null : asString(raw.lastUsedAt),
    lastUsedIp: asString(raw.lastUsedIp),
    expired: asBoolean(raw.expired),
  };
}

/**
 * Every token that can reach this panel without a browser.
 *
 * On the list clock like the sessions beside it, and for the same reason: what
 * changes here without this tab doing anything is a token being used, and "last
 * used" is the column somebody has this page open to watch.
 */
export function useApiTokens(): UseQueryResult<ApiToken[], ApiError> {
  return useQuery<ApiToken[], ApiError>({
    queryKey: queryKeys.apiTokens(),
    queryFn: async () => {
      const raw = asRecord(await api.get<Raw>("settings/tokens"));
      const rows = pick(raw, ["tokens"]);
      return (Array.isArray(rows) ? rows : []).map(normalizeApiToken);
    },
    retry: retryTransportOnly,
    ...LIST_QUERY,
  });
}

/**
 * Issue one. The secret comes back in this response and in no other, so the
 * caller has to put it in front of somebody before it is thrown away - which is
 * why the mutation resolves with it rather than only invalidating the list.
 */
export function useCreateApiToken(): UseMutationResult<
  ApiTokenIssued,
  ApiError,
  CreateApiTokenInput
> {
  const client = useQueryClient();
  return useMutation<ApiTokenIssued, ApiError, CreateApiTokenInput>({
    mutationFn: async (input) => {
      const raw = asRecord(
        await api.post<Raw>("settings/tokens", {
          // Blank rather than absent, which the server reads the same way: the
          // form's own name box, cleared, asking to be given a name.
          name: input.name ?? "",
          // Always sent, both of them: "never expires" is a decision the form
          // makes explicitly, and an absent field would leave the server
          // guessing which of the two silences it was.
          expiresIn: input.expiresIn ?? 0,
          renewOnUse: input.renewOnUse ?? false,
        }),
      );
      return { token: normalizeApiToken(raw.token), secret: asString(raw.secret) };
    },
    onSuccess: () => invalidate(client, [queryKeys.apiTokens()]),
  });
}

/** Rename a token, or change whether using it renews it. Never touches the secret. */
export function useUpdateApiToken(): UseMutationResult<ApiToken, ApiError, UpdateApiTokenInput> {
  const client = useQueryClient();
  return useMutation<ApiToken, ApiError, UpdateApiTokenInput>({
    mutationFn: async ({ id, ...changes }) =>
      normalizeApiToken(await api.put<Raw>(`settings/tokens/${encodeURIComponent(id)}`, changes)),
    onSuccess: () => invalidate(client, [queryKeys.apiTokens()]),
  });
}

/** Revoke one. Whatever holds the secret is anonymous on its next request. */
export function useRevokeApiToken(): UseMutationResult<void, ApiError, string> {
  const client = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.del<void>(`settings/tokens/${encodeURIComponent(id)}`),
    onSuccess: () => invalidate(client, [queryKeys.apiTokens()]),
  });
}

export interface Enable2faInput {
  /** Omit for step one (enrolment); send the code from the app for step two. */
  code?: string;
}

/**
 * Called twice: once with an empty object to get the secret, provisioning URI
 * and QR, then again with the code the app shows to confirm the device.
 */
export function useEnable2fa(): UseMutationResult<TwoFactorEnableResult, ApiError, Enable2faInput> {
  const client = useQueryClient();
  return useMutation<TwoFactorEnableResult, ApiError, Enable2faInput>({
    mutationFn: (input) =>
      api.post<TwoFactorEnableResult>(
        "settings/2fa/enable",
        input.code ? { code: input.code } : {},
      ),
    onSuccess: (result) => {
      // Only the confirming call changes whether a code is required at login.
      if (result.confirmed || result.enabled) {
        invalidate(client, [queryKeys.session()]);
      }
    },
  });
}

export interface Disable2faInput {
  password: string;
}

export function useDisable2fa(): UseMutationResult<void, ApiError, Disable2faInput> {
  const client = useQueryClient();
  return useMutation<void, ApiError, Disable2faInput>({
    mutationFn: (input) => api.post<void>("settings/2fa/disable", { password: input.password }),
    onSuccess: () => invalidate(client, [queryKeys.session()]),
  });
}

/* -------------------------------------------------------------------------- */
/* Backup, restore, update                                                     */
/* -------------------------------------------------------------------------- */

/**
 * Downloads the archive through the API layer rather than a plain link, so an
 * error arrives as a message instead of a browser error page. The archive holds
 * the server private key and every client key: say so before offering it.
 */
export function useBackup(): UseMutationResult<{ filename: string; size: number }, ApiError, void> {
  return useMutation<{ filename: string; size: number }, ApiError, void>({
    // Archiving the config plus the panel data outruns the default timeout on a
    // small VPS, and a half-written backup is worse than a slow one.
    mutationFn: () => api.download("backup", "awg-panel-backup.tar.gz", { timeoutMs: 0 }),
  });
}

/**
 * Replaces the configuration and every client from an archive. The whole cache
 * describes the old server afterwards, so all of it is dropped.
 */
export function useRestore(): UseMutationResult<RestoreResult, ApiError, File> {
  const client = useQueryClient();
  return useMutation<RestoreResult, ApiError, File>({
    mutationFn: (file) => {
      const form = new FormData();
      form.append("file", file, file.name);
      return api.post<RestoreResult>("restore", form, { timeoutMs: 0 });
    },
    onSuccess: () => {
      void client.invalidateQueries();
    },
  });
}

/**
 * Compares the installed version against the latest release. A network failure
 * is not an error here: the endpoint answers `checked: false` with a reason.
 */
export function useUpdateCheck(): UseMutationResult<UpdateCheck, ApiError, void> {
  const client = useQueryClient();
  return useMutation<UpdateCheck, ApiError, void>({
    mutationFn: () => api.post<UpdateCheck>("update/check"),
    onSuccess: (result) => {
      // Kept in the cache so the answer survives a tab switch on the Update tab.
      client.setQueryData(queryKeys.updateCheck(), result);
    },
  });
}

/** How often the update's own state file is read while one is running. */
const UPDATE_POLL_MS = 2000;

/**
 * Where the update on this server got to.
 *
 * Polled only while one is running, and deliberately not given up on when the
 * request fails. Halfway through an update the panel restarts itself, so every
 * poll for the next twenty seconds or so fails at the transport - and that is
 * the *expected* path, not a problem to report. `retry: false` keeps each poll
 * to a single attempt and the interval simply tries again, so the page rides
 * the restart out and picks the story back up from the same state file
 * afterwards. Anything that treated those failures as an error would show
 * "update failed" on precisely the successful runs.
 */
export function useUpdateStatus(
  options: { enabled?: boolean } = {},
): UseQueryResult<UpdateStatus, ApiError> {
  const { enabled = true } = options;
  return useQuery<UpdateStatus, ApiError>({
    queryKey: queryKeys.updateStatus(),
    queryFn: () => api.get<UpdateStatus>("update/status"),
    enabled,
    retry: false,
    refetchInterval: (query) => (query.state.data?.status === "running" ? UPDATE_POLL_MS : false),
    // While an update runs, the operator's tab is very often not the focused
    // one: they went to read the release notes. This is the one poll in the
    // panel that has to carry on regardless.
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
    staleTime: 0,
  });
}

/**
 * Start the update, and answer as soon as it is running rather than when it is
 * done - it cannot answer when it is done, because the service handling this
 * request is one of the things it restarts.
 *
 * No timeout on the request for the same reason the restore has none: the
 * server is busy taking a backup before it replies, and a client-side deadline
 * would abandon a request the server is still honouring.
 */
export function useUpdateApply(): UseMutationResult<UpdateStatus, ApiError, void> {
  const client = useQueryClient();
  return useMutation<UpdateStatus, ApiError, void>({
    mutationFn: () => api.post<UpdateStatus>("update/apply", {}, { timeoutMs: 0 }),
    onSuccess: (result) => {
      // Seeded rather than invalidated, so the progress view has a "running"
      // state to render on the very first frame instead of an empty card while
      // the first poll is in flight.
      client.setQueryData(queryKeys.updateStatus(), result);
    },
  });
}

/* -------------------------------------------------------------------------- */
/* The API's description of itself                                             */
/* -------------------------------------------------------------------------- */

/**
 * What `GET openapi.json` describes, flattened into something a page can render.
 *
 * The document is OpenAPI, which is a format for tools: an operation's words,
 * its parameters, its example request and its example answer live in four
 * different places under two string keys each. Everything below is the walk
 * that collects them, done once here rather than in every component.
 *
 * Deliberately forgiving. A key the panel does not send, a response with no
 * example, a tag the document forgot to describe - each of them yields an empty
 * string rather than an exception, because a reference that renders with one
 * blank section is worth having and one that throws is not.
 */
function normalizeSpec(value: unknown): ApiSpec {
  const raw = asRecord(value);
  const info = asRecord(raw.info);
  const servers = Array.isArray(raw.servers) ? raw.servers : [];
  const paths = asRecord(raw.paths);

  const operations: ApiOperation[] = [];
  for (const [path, item] of Object.entries(paths)) {
    for (const [method, body] of Object.entries(asRecord(item))) {
      operations.push(normalizeOperation(path, method, asRecord(body)));
    }
  }

  // Tag order is the document's, which is the order the catalog is written in -
  // auth, then clients, then everything the clients page does not need. A group
  // the document tagged but did not describe still gets a heading; an operation
  // whose tag is not in the list would otherwise be rendered nowhere at all, so
  // it lands in a group of its own at the end.
  const described = Array.isArray(raw.tags) ? raw.tags.map(asRecord) : [];
  const order = described.map((tag) => asString(tag.name));
  for (const operation of operations) {
    if (!order.includes(operation.group)) {
      order.push(operation.group);
    }
  }

  const groups: ApiGroup[] = order
    .map((name) => ({
      name,
      description: asString(described.find((tag) => asString(tag.name) === name)?.description),
      operations: operations.filter((operation) => operation.group === name),
    }))
    .filter((group) => group.operations.length > 0);

  return {
    title: asString(info.title, "API"),
    version: asString(info.version),
    serverUrl: asString(asRecord(servers[0]).url),
    groups,
    operations: groups.flatMap((group) => group.operations),
  };
}

function normalizeOperation(path: string, method: string, raw: Raw): ApiOperation {
  const responses = asRecord(raw.responses);
  // The one 2xx the document declares. Written as a search rather than assumed
  // to be "200", because a create answers 201 and a delete answers 204, and the
  // status is what the reference prints beside the example.
  const successCode =
    Object.keys(responses).find((code) => code.startsWith("2")) ?? Object.keys(responses)[0] ?? "";
  const success = asRecord(responses[successCode]);
  const [responseType, responseBody] = firstContent(success.content);
  const [requestType, requestBody] = firstContent(asRecord(raw.requestBody).content);

  return {
    id: asString(raw.operationId),
    method: method.toUpperCase(),
    // The document's paths are rooted, the panel's fetch paths are not, and
    // every URL the page builds joins this onto a base that ends in a slash.
    path: path.replace(/^\//, ""),
    group: asString(Array.isArray(raw.tags) ? raw.tags[0] : ""),
    summary: asString(raw.summary),
    description: asString(raw.description),
    params: (Array.isArray(raw.parameters) ? raw.parameters : []).map(normalizeParam),
    request: requestBody,
    requestType,
    requestNote: asString(asRecord(raw.requestBody).description),
    requestRequired: asBoolean(asRecord(raw.requestBody).required, true),
    successCode,
    successNote: asString(success.description),
    responseType,
    response: responseBody,
    statuses: Object.entries(responses)
      .filter(([code]) => code !== successCode)
      .map(([code, body]) => ({ code, description: asString(asRecord(body).description) }))
      .sort((a, b) => a.code.localeCompare(b.code)),
    auth: securityOf(raw.security),
  };
}

function normalizeParam(value: unknown): ApiParam {
  const raw = asRecord(value);
  const schema = asRecord(raw.schema);
  return {
    name: asString(raw.name),
    where: raw.in === "path" ? "path" : "query",
    required: asBoolean(raw.required),
    description: asString(raw.description),
    type: asString(schema.type, "string"),
    fallback: schema.default === undefined ? "" : asString(schema.default),
    values: asStringList(schema.enum),
  };
}

/**
 * The one media type a body is documented with, and its example as JSON text.
 *
 * OpenAPI keys content by media type and the panel sends exactly one per body,
 * so the first entry is the entry. A binary answer - a QR code, an archive -
 * carries no example, and comes back as an empty string for the caller to
 * render as "a file" rather than as a code block with nothing in it.
 */
function firstContent(content: unknown): [string, string] {
  const entries = Object.entries(asRecord(content));
  if (entries.length === 0) {
    return ["", ""];
  }
  const [mediaType, body] = entries[0];
  const example = asRecord(body).example;
  if (example === undefined) {
    return [mediaType, ""];
  }
  if (typeof example === "string") {
    return [mediaType, example];
  }
  return [mediaType, JSON.stringify(example, null, 2)];
}

/** Which credentials an operation's `security` list allows. */
function securityOf(value: unknown): ApiAuth {
  if (!Array.isArray(value)) {
    return "any";
  }
  if (value.length === 0) {
    return "none";
  }
  const schemes = value.flatMap((entry) => Object.keys(asRecord(entry)));
  return schemes.includes("bearerAuth") ? "any" : "session";
}

/**
 * The reference itself.
 *
 * It describes the running panel, so it changes only when the panel is upgraded
 * - and an upgrade restarts the web service and reloads the page with it. That
 * makes it the one query here with nothing to poll for: fetched once, kept for
 * the life of the tab, and refetched only if something invalidates it.
 */
export function useApiSpec(): UseQueryResult<ApiSpec, ApiError> {
  return useQuery<ApiSpec, ApiError>({
    queryKey: queryKeys.apiSpec(),
    queryFn: async () => normalizeSpec(await api.get<Raw>("openapi.json")),
    retry: retryTransportOnly,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
}

/**
 * Save the same document to disk, for a tool that wants the file rather than
 * the page. Through the API layer and not a plain link, for the reason the
 * backup download is: a link sends the browser to a raw error page, and this
 * route needs a credential the browser would carry but a 401 would still land
 * as an unstyled JSON blob in a new tab.
 */
export function useDownloadApiSpec(): UseMutationResult<
  { filename: string; size: number },
  ApiError,
  void
> {
  return useMutation<{ filename: string; size: number }, ApiError, void>({
    mutationFn: () => api.download("openapi.json", "awg-panel-openapi.json"),
  });
}
