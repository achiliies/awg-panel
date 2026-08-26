/**
 * Wire types for the panel API.
 *
 * Every shape mirrors what Django actually serialises (build contract 5.5 and
 * docs/API.md): camelCase keys, RFC 3339 strings for dates, Unix seconds
 * wherever the field name says so because that is what the kernel reports.
 *
 * Traffic is named from the server's point of view, exactly as `awg show dump`
 * and traffic.db report it: `rx` is what the client uploaded, `tx` is what it
 * downloaded. Anything that renders these must label them that way round.
 */

/* -------------------------------------------------------------------------- */
/* Errors                                                                      */
/* -------------------------------------------------------------------------- */

/** How a request failed. `http` means the server answered; the rest never reached it. */
export type ApiErrorKind = "http" | "network" | "timeout" | "parse";

/** Per-field validation messages, keyed by the field the API named. */
export type FieldErrors = Readonly<Record<string, string>>;

/**
 * Every failed request throws one of these, including transport failures, so a
 * caller only ever has to handle one error type.
 *
 * The API's error body is always `{detail, errors}` (docs/API.md), so `detail`
 * is a finished sentence meant for a human and `errors` maps a field name to
 * the message that belongs under that field.
 */
export class ApiError extends Error {
  /** HTTP status, or 0 when the request never got an answer. */
  readonly status: number;
  readonly kind: ApiErrorKind;
  /** Human-readable sentence from the server, or a fallback for transport failures. */
  readonly detail: string;
  readonly errors: FieldErrors;
  /** The path that failed, relative to the API base. Useful in logs, not in the UI. */
  readonly path: string;
  /**
   * The API's machine-readable reason, when it sends one, for the failures that
   * share a status with something else: `not_configured` on a 503 that is not a
   * fault, and `name_in_use` on a 409 that is about a name rather than about an
   * exhausted address pool. Empty for everything else, including anything a
   * proxy answered.
   */
  readonly code: string;

  constructor(init: {
    status: number;
    detail: string;
    kind?: ApiErrorKind;
    errors?: FieldErrors;
    path?: string;
    code?: string;
  }) {
    super(init.detail);
    this.name = "ApiError";
    this.status = init.status;
    this.kind = init.kind ?? "http";
    this.detail = init.detail;
    this.errors = init.errors ?? {};
    this.path = init.path ?? "";
    this.code = init.code ?? "";
  }

  /** Not logged in. The router sends the user to /login instead of reloading. */
  get unauthorized(): boolean {
    return this.status === 401;
  }

  /** The account has 2FA and the login attempt carried no code yet. */
  get totpRequired(): boolean {
    return this.status === 401 && this.detail.trim().toLowerCase() === "totp_required";
  }

  /**
   * django-axes blocked this source IP. Both a lockout and a missing CSRF token
   * are 403, and only the message distinguishes them, so this is a hint for
   * choosing wording - never a security decision.
   */
  get lockedOut(): boolean {
    return this.status === 403 && /lock|attempt|blocked/i.test(this.detail);
  }

  /** Duplicate client name, or the address pool is full. */
  get conflict(): boolean {
    return this.status === 409;
  }

  /**
   * The narrower half of that: a name something else already answers to. Told
   * apart from a full address pool by the code rather than by the wording,
   * because a form that suggested the name acts on this one - it draws another
   * - and a server with no addresses left would make that a nonsense reply.
   */
  get nameInUse(): boolean {
    return this.status === 409 && this.code === "name_in_use";
  }

  /** At least one field was rejected; `errors` says which. */
  get validation(): boolean {
    return this.status === 400 || Object.keys(this.errors).length > 0;
  }

  /** The request never reached the panel (offline, service restarting, timeout). */
  get offline(): boolean {
    return this.kind === "network" || this.kind === "timeout";
  }

  /**
   * Whether repeating this request is the same request rather than a second one.
   *
   * True only for the 503 the API answers when the config lock was still held
   * by another writer after its budget - a `awg-panel manage` run over SSH, a panel
   * operation on the other worker. That lock is taken before a mutation writes
   * anything, and the one nested lock a mutation waits for afterwards (the
   * client index's) is caught where it is taken and costs a rebuild rather than
   * the mutation, so nothing reached the files. Repeating it is safe even for
   * the operations that are not idempotent, `reset-keys` above all.
   *
   * Deliberately not any 5xx. A 502 is the AmneziaWG tools having failed, and
   * by then the config has been written and only the kernel refused it, so a
   * repeat would be a second, different attempt at a half-applied change.
   *
   * `not_configured` shares the status and means the opposite: there is no
   * server config to lock, and waiting will not produce one.
   */
  get retryable(): boolean {
    return this.status === 503 && this.code !== "not_configured";
  }

  /** Message for one field, or undefined when the API did not complain about it. */
  fieldError(field: string): string | undefined {
    return this.errors[field];
  }
}

/** Narrow an unknown catch value, which is what error boundaries and hooks get. */
export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

/* -------------------------------------------------------------------------- */
/* Session and auth                                                            */
/* -------------------------------------------------------------------------- */

export type ThemePreference = "light" | "dark" | "system";

/** GET auth/session - unauthenticated-safe, so it also bootstraps the SPA. */
export interface Session {
  authenticated: boolean;
  username: string;
  /** True when the account has a confirmed TOTP device. */
  otpRequired: boolean;
  version: string;
  theme: ThemePreference;
  language: string;
  /** Secret prefix the panel is mounted under, e.g. "/ab12cd34/". */
  basePath: string;
  /** The panel is running against MockController, not a real interface. */
  mock: boolean;
  /**
   * The API token that authenticated the request, or null for a browser.
   *
   * Never set for the SPA, which signs in with a cookie: it is here because
   * this is the one route a script may ask "which credential am I holding, and
   * when does it stop working". Optional, because a panel a version behind does
   * not send it.
   */
  token?: CallingToken | null;
}

/** What GET auth/session says about the bearer token that made the request. */
export interface CallingToken {
  name: string;
  /** RFC 3339, or null for a token that never expires. */
  expiresAt: string | null;
  renewOnUse: boolean;
}

export interface LoginInput {
  username: string;
  password: string;
  /** Only sent after the API answered 401 with detail "totp_required". */
  totp?: string;
}

/**
 * One browser signed in to the panel: a row of GET settings/sessions.
 *
 * `id` is an opaque identifier minted for this listing and is the only handle
 * the UI ever has on a session. The session key itself - the value in the
 * cookie - never leaves the server, so nothing rendered from this object can be
 * copied into a browser to become that session.
 */
export interface LoginSession {
  id: string;
  /** The session making this very request: shown first, and cannot be revoked. */
  current: boolean;
  /** RFC 3339. When the password was accepted, or when the panel first saw it. */
  createdAt: string;
  /** RFC 3339, to the minute: the panel does not rewrite this on every poll. */
  lastSeenAt: string;
  /** RFC 3339. Rolling: using the panel pushes it out by the session length. */
  expiresAt: string;
  /** "" when the address could not be read, or came from a header we do not trust. */
  ip: string;
  /** Read out of the user agent; "" when it says nothing recognisable. */
  browser: string;
  platform: string;
  /** The raw header, which is the evidence behind the two guesses above. */
  userAgent: string;
}

/**
 * One API token as the panel lists it. The secret is not here and cannot be:
 * the server keeps a hash, so it exists in exactly one response ever - the one
 * that created it, below.
 */
export interface ApiToken {
  id: string;
  /** What the admin called it, and what the activity log shows it as. */
  name: string;
  /** The last few characters of the secret, for matching a row to a copy of it. */
  hint: string;
  /** RFC 3339. */
  createdAt: string;
  /** RFC 3339, or null for a token that does not expire. */
  expiresAt: string | null;
  /** The window in seconds that `renewOnUse` restores, or null with no expiry. */
  expiresIn: number | null;
  /** Whether each use pushes the expiry back out to the full window. */
  renewOnUse: boolean;
  /** RFC 3339, to the minute, or null for a token never yet presented. */
  lastUsedAt: string | null;
  /** "" when the address could not be read, or is not one we trust. */
  lastUsedIp: string;
  /** Decided against the server's clock, which is the one it is checked by. */
  expired: boolean;
}

/** The answer to creating one: the row, and the only sight of the secret. */
export interface ApiTokenIssued {
  token: ApiToken;
  secret: string;
}

export interface CreateApiTokenInput {
  /** Blank, and the server names it - the same nine characters a client gets. */
  name?: string;
  /** Seconds. 0 - or left out - means it never expires, which is a choice. */
  expiresIn?: number;
  renewOnUse?: boolean;
}

export interface UpdateApiTokenInput {
  id: string;
  /** Both optional, and a request that carries neither is refused. */
  name?: string;
  renewOnUse?: boolean;
}

/* -------------------------------------------------------------------------- */
/* The API's description of itself                                             */
/* -------------------------------------------------------------------------- */

/*
 * GET openapi.json is an OpenAPI 3.1 document, which is the right thing to
 * serve a code generator and the wrong thing to render a page from: the
 * information a reader wants is spread across `paths`, `security`, `responses`
 * and `content`, keyed by strings, three levels down. So the hook flattens it
 * once into the shapes below and the components read those.
 *
 * Nothing here is the whole of OpenAPI. It is the subset apps/panel/apidocs.py
 * emits, and a field the panel does not send is simply absent - this is a
 * renderer for one document, not a general one.
 */

/** Which credentials an operation accepts. */
export type ApiAuth = "any" | "session" | "none";

export interface ApiParam {
  name: string;
  where: "query" | "path";
  required: boolean;
  description: string;
  /** "string", "integer", "boolean" - what a value has to be. */
  type: string;
  /** What the API does when the parameter is left out, "" when that is nothing. */
  fallback: string;
  /** The closed set of values, empty when anything of the type will do. */
  values: string[];
}

/** One documented answer: a status and what it means. */
export interface ApiStatus {
  code: string;
  description: string;
}

export interface ApiOperation {
  /** Stable across releases; what a generated client names its method after. */
  id: string;
  /** Upper case, for the badge. */
  method: string;
  /** Relative to the API root, e.g. "clients/{name}". */
  path: string;
  /** The tag it was filed under, which is the group heading it appears beneath. */
  group: string;
  summary: string;
  /** Markdown-ish: paragraphs, `code` and **bold**. See lib/apiText. */
  description: string;
  params: ApiParam[];
  /** Pretty-printed example request, "" for an operation that takes no body. */
  request: string;
  /** "application/json" for everything but the one upload, which is multipart. */
  requestType: string;
  requestNote: string;
  requestRequired: boolean;
  successCode: string;
  successNote: string;
  /** "application/json", "text/plain", "image/png", ... */
  responseType: string;
  /** Pretty-printed example answer, "" for one with no body or a binary one. */
  response: string;
  /** Failures worth branching on. The success code is not repeated here. */
  statuses: ApiStatus[];
  auth: ApiAuth;
}

export interface ApiGroup {
  name: string;
  description: string;
  operations: ApiOperation[];
}

export interface ApiSpec {
  title: string;
  /** The panel's version, so the reference can say which release it describes. */
  version: string;
  /** Absolute URL of the API root, base path included, as the document names it. */
  serverUrl: string;
  groups: ApiGroup[];
  /** Every operation, flat and in document order, for searching. */
  operations: ApiOperation[];
}

/* -------------------------------------------------------------------------- */
/* Clients                                                                     */
/* -------------------------------------------------------------------------- */

/** Why a peer is switched off. Empty string means it is enabled. */
export type DisabledReason = "" | "manual" | "quota" | "expired";

/**
 * Presentation state: whether the peer is working, and if it is, whether it is
 * there. Derived by the API from the config marker, the quota and the handshake.
 *
 * Not from the expiry, which is the one thing this deliberately says nothing
 * about. A date that has passed is answered by `expiresAt` and its own column,
 * and a client switched off because of one is "disabled" here with the why in
 * `disabledReason` - so the two never say the same thing twice, and never
 * contradict each other when an admin keeps a lapsed client on.
 */
export type ClientStatus = "online" | "idle" | "offline" | "disabled" | "quota";

/**
 * One `[Peer]` block plus its panel metadata and whatever the collector last
 * saw for it.
 */
export interface Client {
  name: string;
  publicKey: string;
  ip: string;
  allowedIps: string;
  enabled: boolean;
  disabledReason: DisabledReason;
  createdAt: string | null;
  expiresAt: string | null;
  /**
   * This client is past its date and switched on regardless, because somebody
   * switched it on knowing that. It keeps working until the date is changed,
   * and the bulk sweep leaves it alone.
   */
  expiryOverridden: boolean;
  /** 0 means unlimited. */
  quotaBytes: number;
  email: string;
  note: string;
  online: boolean;
  /** Unix seconds; 0 when the peer has never completed a handshake. */
  lastHandshake: number;
  /**
   * Unix seconds at which anything was last heard from this peer: the later of
   * its handshake and the moment its receive counter last moved. 0 when it has
   * never been heard from at all.
   *
   * This is what "last seen" means to somebody reading the list, and it is not
   * `lastHandshake`. A client transferring right now rekeys about every two
   * minutes and nothing about that is visible to it, so a column drawn from the
   * handshake alone counts steadily up to "2m ago" on a connection that is
   * plainly working, then drops back to zero - while the dot beside it stays
   * green, because `online` was always decided on this field instead.
   */
  lastSeen: number;
  endpoint: string;
  /** All-time bytes the client uploaded, less anything cleared by reset-usage. */
  rxBytes: number;
  /** All-time bytes the client downloaded, less anything cleared by reset-usage. */
  txBytes: number;
  /**
   * What reset-usage cleared, and therefore what has already been taken off the
   * two figures above.
   *
   * Only interesting alongside `LivePeer.rx` and `.tx`, which are the same
   * all-time counters two seconds fresh and with nothing taken off them: the
   * collector has no way of knowing a counter was ever cleared. Subtracting
   * these is what turns the blob's totals back into the panel's.
   */
  offsetRx: number;
  offsetTx: number;
  /** Live bytes per second, from the collector's last cycle. */
  rateRx: number;
  rateTx: number;
  /** rxBytes + txBytes measured against the quota. */
  quotaUsed: number;
  /** 0..100, and 0 when no quota is set. */
  quotaPercent: number;
  /**
   * How fast this client may go, in bits per second, and 0 for no ceiling.
   *
   * What an operator asked for rather than what the kernel is currently doing.
   * The two agree except in the window between an interface coming up and
   * whichever pass notices, and that is a fact about the server rather than
   * about any one client - so it is not carried here per row.
   *
   * Bits per second, like the API stores them, because that is what `tc` takes:
   * the megabits an operator types are converted at the form and nowhere else.
   */
  downBps: number;
  upBps: number;
  /**
   * This client's own DNS line, and `""` when it has none and takes the
   * server's.
   *
   * Only ever present on a client fetched by name. The list omits it: it is
   * read out of the client's config file, so carrying it on rows cost the
   * server one file open per client on every poll, for a value no column shows.
   * `undefined` therefore means "this came from the list and nobody asked",
   * which is not the same as `""` - anything that has to edit the field must
   * fetch the client first rather than read it off a row.
   */
  dns?: string;
  /** Derived by the API when it merges config, metadata and live state. */
  status?: ClientStatus;
  /**
   * The IPv6 address the server routes to this client. Empty when the tunnel
   * carries no IPv6, and also when this particular peer has not been given one
   * yet - which is the case worth noticing, not hiding.
   */
  ip6: string;
  /**
   * This client's config still routes 0.0.0.0/0 and not ::/0, on a server that
   * can carry IPv6. Its device reaches every dual-stack destination outside the
   * tunnel, unencrypted and with its own address, while the VPN reports itself
   * connected. Cleared once the device re-imports the config the server has
   * already rewritten for it.
   *
   * Computed by the API rather than here: deriving it needs the server's prefix,
   * the client's route list and the rule relating them, and a copy of that rule
   * in the browser is a copy that can drift into silently under-reporting.
   */
  leaksIpv6: boolean;
}

export interface ClientsResponse {
  /** One page of clients, already narrowed and ordered by the server. */
  clients: Client[];
  /** The whole tunnel network, prefix included, e.g. "10.13.13.0/24". */
  subnetCidr: string;
  /** Addresses still available in the subnet. */
  freeIps: number;
  /**
   * Clients matching the search and the filter, before the page was cut. This
   * is what sizes the pagination and what "N found" reports.
   */
  total: number;
  /** Named clients on the server, whatever this request asked for. */
  totalAll: number;
  /**
   * Of those, the ones `clients/remove-expired` would actually take - counted
   * over every client, because that is what the sweep acts on. A button
   * counting only the rows on screen would promise a number the server then
   * declines to match.
   */
  expiredCount: number;
  /**
   * The clients the panel calls disabled: switched off by an operator, switched
   * off by the collector, or still switched on and already past their data
   * limit. What `clients/bulk-remove` takes when it is asked for `disabled`.
   */
  disabledCount: number;
  /**
   * What it takes when asked for both, which is the union and not the sum: a
   * lapsed client the collector has switched off is in each of the two numbers
   * above and is removed once. Adding them would promise a removal bigger than
   * the server.
   */
  expiredOrDisabledCount: number;
  page: number;
  pageSize: number;
}

/**
 * Which categories `clients/bulk-remove` should take. Both default to false on
 * the server, and asking for neither is refused rather than answered with an
 * empty removal.
 */
export interface BulkRemoveOptions {
  expired: boolean;
  disabled: boolean;
}

/**
 * What `clients/bulk-limit` writes over every client. Both directions are
 * required: the call overwrites, so half a body must not read as a request to
 * clear the other half.
 */
export interface BulkLimit {
  downBps: number;
  upBps: number;
}

/** What it did. `applied` is false when the numbers were stored but nothing is
 * enforcing them yet - a host without iproute2, a tunnel that is down. */
export interface BulkLimitResult {
  changed: number;
  applied: boolean;
  reason: string;
}

/**
 * The query string of GET clients. Every field is optional and the server
 * ignores what it cannot parse, so an old bookmark still opens the list.
 */
export interface ClientsQuery {
  q?: string;
  status?: ClientStatusFilter;
  sort?: ClientSortKey;
  direction?: "asc" | "desc";
  page?: number;
  pageSize?: number;
}

export type ClientStatusFilter = "all" | "active" | "online" | "offline" | "disabled" | "expired";

/**
 * The columns the list can be ordered by. Declared here rather than beside the
 * table because the ordering is the server's now: these are the words
 * apps.clients.merge.SORT_KEYS accepts, and the table only names them.
 */
export type ClientSortKey = "created" | "name" | "ip" | "usage" | "lastHandshake";

export interface CreateClientInput {
  /**
   * Left out - or sent blank - and the server draws a nine-character name of
   * its own, which is what the add form does when its suggestion is cleared.
   * The created client comes back carrying whichever name it ended up with.
   */
  name?: string;
  allowedIps?: string;
  dns?: string;
  quotaBytes?: number;
  /** Bits per second. Absent is the same as 0, which is no limit. */
  downBps?: number;
  upBps?: number;
  expiresAt?: string | null;
  email?: string;
  note?: string;
}

/** Any subset. Sending `name` renames the client and keeps its keys and address. */
export interface UpdateClientInput {
  name?: string;
  allowedIps?: string;
  dns?: string;
  quotaBytes?: number;
  /** 0 clears a limit; leaving the key out keeps whatever the client had. */
  downBps?: number;
  upBps?: number;
  expiresAt?: string | null;
  email?: string;
  note?: string;
  enabled?: boolean;
}

/* -------------------------------------------------------------------------- */
/* Server configuration                                                        */
/* -------------------------------------------------------------------------- */

/** GET server. The server private key is never part of this payload. */
export interface ServerConfig {
  iface: string;
  address: string;
  /** The whole tunnel network the server's address sits on, e.g. "10.13.13.0/24". */
  subnetCidr: string;
  /** How many clients the network holds: everything but the network, the server and broadcast. */
  subnetCapacity: number;
  /**
   * The tunnel's IPv6 network, e.g. "fd7a:1e5f:22::/64", and what this server
   * does with it: "native", "nat" or "blackhole". Both "" when the tunnel
   * carries no IPv6.
   *
   * Read-only. Which mode applies follows from what the host actually has, so
   * offering it in a form would only let an admin pick one the machine cannot
   * do.
   */
  subnet6Cidr: string;
  subnet6Mode: string;
  listenPort: number;
  mtu: number;
  publicKey: string;
  dns: string;
  endpointHost: string;
  endpointPort: string;
  allowedIpsDefault: string;
  keepalive: string;
  /** Obfuscation parameters as written in the config: {"Jc": "4", "H1": "5-500000000"}. */
  params: Record<string, string>;
  postUp: string[];
  postDown: string[];
  preDown: string[];
}

/** PUT server - send only what changed. */
export interface ServerUpdateInput {
  listenPort?: number;
  address?: string;
  /** Moves the tunnel network. The server's address is derived from it, so send one or the other. */
  subnetCidr?: string;
  mtu?: number;
  dns?: string;
  endpointHost?: string;
  endpointPort?: string;
  allowedIpsDefault?: string;
  keepalive?: string;
  params?: Record<string, string>;
}

export interface ServerSaveResult {
  /** The interface has to go down and up; every session drops for a moment. */
  needsRestart: boolean;
  /** Client configs were regenerated and the devices need the new file. */
  mustReimport: boolean;
  /** The change reached the running interface. False means it is saved but not live. */
  applied: boolean;
  warnings: string[];
}

/**
 * What the installed module and tools can actually do.
 *
 * The five booleans are the documented set; the index signature exists because
 * ParamSpec.feature names one of these keys by string, and because the
 * controller also folds its version strings into the same object.
 */
export interface ServerFeatures {
  headerRanges?: boolean;
  imitationPackets?: boolean;
  headerProtectionKey?: boolean;
  contentPadding?: boolean;
  timers?: boolean;
  toolsVersion?: string | null;
  moduleVersion?: string | null;
  moduleLoaded?: boolean;
  [feature: string]: boolean | string | null | undefined;
}

export interface ServerStatus {
  ifaceUp: boolean;
  moduleLoaded: boolean;
  toolsVersion: string | null;
  moduleVersion: string | null;
  features: ServerFeatures;
  /** systemd words, e.g. "active" / "inactive", passed through unchanged. */
  serviceActive: string;
  serviceEnabled: string;
  /** Something is bound to the listen port. */
  listening: boolean;
  /** Ready-to-show sentences: module missing, interface down, unsafe params set. */
  warnings: string[];
}

/* -------------------------------------------------------------------------- */
/* Parameter catalog                                                           */
/* -------------------------------------------------------------------------- */

export type ParamGroup =
  "network" | "junk" | "sizes" | "headers" | "imitation" | "advanced" | "hooks";

export type ParamKind =
  "int" | "range" | "imitation" | "key" | "text" | "cidr" | "port" | "iplist" | "bool";

/**
 * One row of GET server/params: the single source of truth for both the
 * validation rules and the words the Server Config page shows. Nothing in the
 * UI may hardcode a parameter name or its explanation.
 */
export interface ParamSpec {
  /** Config key as it appears in awg0.conf, e.g. "Jc". */
  key: string;
  group: ParamGroup;
  /** Plain-language name, e.g. "Junk packets per handshake". */
  label: string;
  kind: ParamKind;
  /** One line, always visible under the field. */
  helpShort: string;
  /** Full explanation for the info popover: what it does, and what breaks if wrong. */
  helpLong: string;
  /** Changing it invalidates every config already handed out. */
  mustMatchClient: boolean;
  /**
   * False for the settings AmneziaWG 3.0 added, which need 3.0 at both ends:
   * an older client ignores them, and the Amnezia app's .conf importer discards
   * them silently even where the client would understand them. Either way the
   * client negotiates without the setting and the handshake fails with no error
   * at all. The UI badges these amber - early, not broken.
   */
  importerSafe: boolean;
  min: number | null;
  max: number | null;
  recommended: string | null;
  default: string | null;
  optional: boolean;
  /** False when the installed module or tools lack the feature: grey out, do not hide. */
  supported?: boolean;
  /** Which ServerFeatures flag this parameter needs, or null when it always works. */
  feature?: string | null;
}

/**
 * A freshly drawn obfuscation set from POST server/reconfigure, returned as a
 * preview: nothing is saved until PUT server. An empty value is meaningful and
 * has to be kept - the generated decoy session is one to five packets long, so
 * the unused imitation slots come back blank so the save removes them.
 */
export type ParamPreview = Record<string, string>;

/**
 * Which band the generator draws from. Every profile is random; they differ in
 * what a draw costs and how much of the protocol's shape it hides. `random`
 * spans the other three, so that the choice of profile is not itself something
 * to fingerprint a server by.
 */
export type ObfuscationProfile = "standard" | "dpi" | "fast" | "random";

/** The obfuscation every client speaks, or the advanced group. */
export type ReconfigureScope = "obfuscation" | "advanced";

/**
 * Nothing here describes the form. An `mtu` and an `s4` were each passed once so
 * a preview could be drawn against a value the form held and the disk did not,
 * and both were the same mistake: a draw is checked at save time against the
 * saved config, so a number that only exists in a form is never the one in force
 * when that happens. The API reads what it needs from the config.
 */
export interface ReconfigureInput {
  profile: ObfuscationProfile;
  scope?: ReconfigureScope;
}

/** POST server/reconfigure: the drawn values, and what setting them will cost. */
export interface ParamPreviewResult {
  params: ParamPreview;
  warnings: string[];
}

/* -------------------------------------------------------------------------- */
/* Live stats                                                                  */
/* -------------------------------------------------------------------------- */

export interface LivePeer {
  /** Bytes per second the client is uploading / downloading right now. */
  rateRx: number;
  rateTx: number;
  /** All-time totals as the collector knows them. */
  rx: number;
  tx: number;
  /** Unix seconds of the last handshake; 0 when there has never been one. */
  handshake: number;
  /**
   * Unix seconds at which this peer's receive counter last moved - the client
   * proving it is still there. A keepalive client moves it every 25 s, long
   * before it rekeys, so this is what `online` is really decided on. 0 while
   * the collector has not yet seen the peer send anything.
   */
  lastRx?: number;
  endpoint: string;
  online: boolean;
  /**
   * Connectivity as one word, decided by the collector against its own clock
   * and thresholds. Connectivity only: whether the peer is disabled, over quota
   * or expired is not a fact about the connection and is not known here, so the
   * client list layers that on top. Absent from a collector too old to send it.
   */
  status?: ClientStatus;
}

export interface SystemStats {
  /** Percent, 0..100. */
  cpu: number;
  /** Mebibytes. */
  memUsed: number;
  memTotal: number;
  /**
   * Bytes - not mebibytes like the two above, because the tunnel's own
   * footprint is measured in hundreds of kilobytes. `memCore` is the kernel
   * module's compiled size, or a userspace tunnel daemon's RSS where one runs
   * instead; `memPanel` is the PSS of the panel's web workers and collector.
   * Absent from a blob written by an older collector, which is why both are
   * optional.
   *
   * `memCore` holding the same value poll after poll is correct, not stale: a
   * module's footprint is its text and data, and what the tunnel allocates per
   * peer lands in slab caches the kernel merges away rather than bills to the
   * module. Do not render it as though it were live.
   */
  memCore?: number;
  memPanel?: number;
  /**
   * Swap in bytes, as `free` reports it: used is total minus free, with pages
   * that are in RAM as well as on the disk still counted as used.
   *
   * A total of 0 means the machine has no swap at all, which is not the same
   * claim as an empty swap device and must not be drawn as an empty bar. Both
   * are absent from a blob written by an older collector.
   */
  swapUsed?: number;
  swapTotal?: number;
  /** Seconds since boot. */
  uptime: number;
  /** 1, 5 and 15 minute load averages. */
  load: number[];
  /**
   * Disk activity over the collector's last poll: bytes a second read and
   * written, and the percentage of that stretch the busiest device spent with
   * a request in flight.
   *
   * Rates already, unlike `wanRx` and `wanTx` below: the counters behind them
   * are since boot, and a reader polling this has no second reading to
   * subtract. Partitions and stacked devices - LVM, RAID, loop - are left out
   * of the sum so a single write is not counted on each layer it passes
   * through. Absent from a blob written by an older collector.
   */
  diskRead?: number;
  diskWrite?: number;
  diskBusy?: number;
  /**
   * How full the root filesystem is, in bytes. A different question from the
   * three above - capacity rather than activity - and the one that says whether
   * the server is about to stop working.
   *
   * `diskUsed` and `diskFree` do not add up to `diskTotal`: a filesystem keeps a
   * reserve only root may write into, so the space an ordinary process may still
   * use is `diskFree` and nothing wider. Percentages drawn from these should be
   * used over used-plus-free, which is what `df` prints. Absent from a blob
   * written by an older collector.
   */
  diskUsed?: number;
  diskFree?: number;
  diskTotal?: number;
  /** Bytes seen on the WAN interface since boot. */
  wanRx: number;
  wanTx: number;
}

/**
 * The collector's blob, served verbatim. `ts` is when it was written: a stale
 * timestamp means the collector is not running, which the UI must say rather
 * than showing zeroes as if the server were idle.
 */
export interface LiveStats {
  ts: number;
  ifaceUp: boolean;
  /**
   * Unix seconds since which the tunnel has been up, and the kernel index of
   * the netdev that figure is about. Both 0 while it is down.
   *
   * The kernel stamps no creation time on an interface, so this is the
   * collector's observation rather than a reading: it is a floor, and a tunnel
   * that was up before the panel was installed reads as young rather than as
   * older than it is. `ifaceIndex` is bookkeeping - it changes on every
   * `down && up`, which is how the collector knows its memory has expired.
   */
  ifaceSince: number;
  ifaceIndex: number;
  online: number;
  total: number;
  totalRateRx: number;
  totalRateTx: number;
  /** Keyed by peer public key. */
  peers: Record<string, LivePeer>;
  system: SystemStats;
  /**
   * An opaque token that changes whenever the server configuration is
   * rewritten - a client added, removed, or switched off by hand, by
   * `awg-panel manage` from a shell, or by the collector enforcing a data limit.
   *
   * Never parsed, only compared with the last one seen. It is how a page
   * polling this every two seconds learns that the client list it is reading
   * on a thirty second clock has just gone out of date; see
   * `useEnforcementCatchup`. Empty when there is no configuration to stamp,
   * which reads as "no opinion" rather than as a change.
   */
  confStamp: string;
}

export interface StatsSummary {
  totalClients: number;
  onlineClients: number;
  disabledClients: number;
  /** Bytes since local midnight. */
  todayRx: number;
  todayTx: number;
  /** All-time bytes across every client. */
  totalRx: number;
  totalTx: number;
  /** Server uptime in seconds. */
  uptime: number;
  ifaceUp: boolean;
}

/**
 * One bucket of a traffic series: what moved, and what to call it.
 *
 * `period` is the bucket's own name and not a timestamp - "2026-08-13" for a
 * day, "2026-08" for a month. Deliberately not a moment: a month is not an
 * instant, and converting one into the reader's zone would relabel August as
 * July for anybody far enough west of the server. Every day here is the UTC day
 * the collector accounted it to, which is the same day the dashboard's "traffic
 * today" card is cut on.
 *
 * `rx` is what the clients uploaded and `tx` what they downloaded, in the
 * server's own directions, exactly as everywhere else in this API. The labels
 * are swapped for display and nowhere else.
 */
export interface TrafficPoint {
  period: string;
  rx: number;
  tx: number;
}

/**
 * A traffic history, for the whole server or for one client.
 *
 * Both windows arrive together because a month is a sum of days: the request
 * that fetched three years of days to build the second series had already
 * fetched the ninety the first one needs. Switching between the two views costs
 * nothing and cannot show two answers cut on different sides of midnight.
 *
 * Every period in the window is present, including the ones nothing moved on. A
 * chart drawn from only the days with rows would space Monday next to Thursday
 * as though they were consecutive, and the quiet stretch is usually the thing
 * somebody opened the chart to find.
 *
 * How long each series is, is the server's answer and not a promise made here.
 * Left alone it is 90 days and 36 months; a window narrows it; a ceiling caps
 * it; and a client's own history is swept after thirteen months, so the same
 * request answers with fewer months for a client than for the server. Anything
 * drawn from this reads its range off the first and last period it was given.
 */
export interface TrafficHistory {
  /** Oldest first. */
  daily: TrafficPoint[];
  /** Oldest first, counting the current month. */
  monthly: TrafficPoint[];
  /**
   * The first day this history has anything on, whatever window was asked for,
   * and empty when it has nothing at all.
   *
   * The window is cut to it at the older end, so a panel installed last month
   * draws last month rather than three years of axis with two bars at the end
   * of it. It travels separately because it is the one fact the series cannot
   * carry: a reader looking at an empty March needs to be told that nothing was
   * recorded before June, and an empty March says nothing about June.
   */
  earliest: string;
}

/**
 * The window a history is asked for, both ends included.
 *
 * Days at both ends whatever the scale, because a month is picked up whole by
 * the monthly series if the window touches it at all - which is what lets one
 * range control drive both tabs of the chart instead of asking the reader which
 * kind of date they meant.
 */
export interface TrafficWindow {
  /** First day, as "2026-05-16". */
  from: string;
  /** Last day, inclusive. */
  to: string;
}

/**
 * What a wipe took, for the line that is shown after it.
 *
 * Three counts and not a total, because they are three different sentences and
 * their sum is a number of database rows rather than a fact about the server.
 * The page asking for this already knows what is left - nothing - so the only
 * thing worth saying afterwards is how much there had been.
 */
export interface TrafficResetResult {
  /** Clients that had anything to clear, not every client on the server. */
  clients: number;
  /** Stored days of the server's own history that went. */
  serverDays: number;
  /** Stored client-days that went, across every client. */
  clientDays: number;
}

/* -------------------------------------------------------------------------- */
/* Events and logs                                                             */
/* -------------------------------------------------------------------------- */

/**
 * The four halves-before-the-dot of an event kind, which is what the filter
 * narrows by.
 *
 * Listed here rather than derived from whatever the current page happens to
 * contain: a filter whose options came out of the rows on screen would offer
 * fewer chips on a quiet server than on a busy one, and would lose the chip that
 * selects the very thing somebody is looking for as soon as the first page
 * stopped containing one.
 */
export const EVENT_CATEGORIES = ["client", "server", "panel", "auth"] as const;

export type EventCategory = (typeof EVENT_CATEGORIES)[number];

export type EventSeverity = "info" | "warning";

/**
 * One line of the panel's own log.
 *
 * `kind` is the whole of what happened, as `<category>.<what-happened>`, and the
 * sentence around it is assembled in the browser from the translation catalog -
 * so the wording is in the reader's language and improves with the translation
 * rather than being frozen at whatever the server wrote. A kind this build does
 * not have a string for is shown as itself, which is what keeps a panel readable
 * against a database written by a newer one.
 *
 * `detail` carries the named values that sentence interpolates: a count, a
 * previous name, whether a save cost a restart. Its keys are single words and
 * are not renamed on the way here, unlike every other payload in this API -
 * they are data rather than field names.
 *
 * `actor` is empty for anything the panel decided on its own, which is a real
 * distinction and not a missing value: a client switched off for its quota was
 * switched off by nobody.
 */
export interface PanelEvent {
  id: number;
  /** ISO-8601, UTC, like every other moment the API sends. */
  at: string;
  kind: string;
  severity: EventSeverity;
  actor: string;
  actorIp: string;
  /** What it happened to - a client name, an interface - or empty. */
  target: string;
  detail: Record<string, unknown>;
}

/** GET events - one page of the log, newest first, and what it is a page of. */
export interface EventPage {
  events: PanelEvent[];
  /** Over the whole filtered set, which is what the pager counts pages from. */
  total: number;
  page: number;
  pageSize: number;
}

/** Everything the events endpoint will narrow by. All of it optional. */
export interface EventsQuery {
  category?: EventCategory;
  severity?: EventSeverity;
  /** Matched against the actor and the target. */
  q?: string;
  page?: number;
  pageSize?: number;
}

/**
 * The three services whose journal the panel will read.
 *
 * Names rather than systemd units, because the unit behind `tunnel` is
 * per-interface and because the browser is not the place to decide which units
 * on the machine may be read.
 */
export const LOG_SOURCES = ["panel", "collector", "tunnel"] as const;

export type LogSource = (typeof LOG_SOURCES)[number];

/** One entry as journald recorded it. */
export interface JournalLine {
  /** ISO-8601, or empty for an entry whose timestamp could not be read. */
  at: string;
  /** The syslog level, 0 (emergency) to 7 (debug). */
  priority: number;
  message: string;
}

/**
 * GET logs - what one service has been saying, oldest first.
 *
 * Oldest first is the opposite of the event log and is deliberate: this is a
 * transcript, and reversing it would scramble every multi-line traceback in it.
 *
 * `available` is false where there is no journal to read - a container without
 * systemd, the development tree - and `reason` says which. That is a state
 * rather than a failure, so it arrives as a 200.
 */
export interface ServiceLog {
  source: LogSource;
  /** The systemd unit the source resolved to, for the caption and for journalctl. */
  unit: string;
  lines: JournalLine[];
  available: boolean;
  reason: string;
}

/* -------------------------------------------------------------------------- */
/* Panel settings                                                              */
/* -------------------------------------------------------------------------- */

/**
 * Where a client config points. `ip` hands over the address the server config
 * already carries, whatever it is; `domain` substitutes the panel's certificate
 * domain as the config leaves the panel. Nothing on disk changes either way.
 */
export type ConfigEndpointMode = "ip" | "domain";

/**
 * Panel settings. Every value is a string because that is how the Setting table
 * stores them; the backend coerces on read and so does the UI.
 */
export interface Settings {
  webListen: string;
  webPort: string;
  /** Secret URL prefix. Defence in depth, not authentication. */
  webBasePath: string;
  tlsCertPath: string;
  tlsKeyPath: string;
  /** Which address a client config is handed out with. */
  configEndpointMode: ConfigEndpointMode;
  /** The domain to write in. Empty means "whichever name the certificate carries". */
  configEndpointHost: string;
  /** Seconds. */
  sessionMaxAge: string;
  loginRateLimit: string;
  theme: ThemePreference;
  language: string;
  trafficPollSec: string;
  onlineThresholdSec: string;
  enforceIntervalSec: string;
  /**
   * The switch for per-client bandwidth limits. "0" means nothing is shaped at
   * all and the tunnel keeps the queueing the kernel gives it, whatever any
   * client's own limit says - and the limits themselves are kept, so turning it
   * back on puts every one of them back.
   */
  shaperOn: string;
  /**
   * What a client created from now on is given, in megabits per second, when
   * nothing asks for a limit of its own. "0" is no limit. Only new clients: the
   * settings page has a separate button for writing over the ones already here.
   */
  shaperDefaultDownMbps: string;
  shaperDefaultUpMbps: string;
  /** "1" to shape upload as well as download. See the warning the save returns. */
  shaperUpload: string;
  /** Blank means whichever interface holds the default route. */
  shaperWanIface: string;
}

/**
 * Mirrors apps/panel/defaults.py. A controlled input must never be handed
 * `undefined`, so a key the API has not stored yet still renders as itself.
 */
export const SETTINGS_DEFAULTS: Settings = {
  webListen: "0.0.0.0",
  webPort: "2097",
  webBasePath: "/",
  tlsCertPath: "",
  tlsKeyPath: "",
  configEndpointMode: "ip",
  configEndpointHost: "",
  sessionMaxAge: "86400",
  loginRateLimit: "5",
  theme: "system",
  language: "en",
  trafficPollSec: "2",
  onlineThresholdSec: "180",
  enforceIntervalSec: "60",
  shaperOn: "0",
  shaperDefaultDownMbps: "0",
  shaperDefaultUpMbps: "0",
  shaperUpload: "0",
  shaperWanIface: "",
};

export type SettingsInput = Partial<Settings>;

export interface SettingsSaveResult {
  settings: Settings;
  /** The web service is being restarted, so the browser has to reconnect. */
  needsRestart: boolean;
  /** Where to reconnect when the listen address, port or base path changed. */
  url: string;
  warnings: string[];
}

/**
 * What the panel's TLS certificate covers, read off the file rather than out of
 * the settings: a renewal can change the names without anything being saved.
 * Empty everywhere when there is no certificate, or none that can be parsed.
 */
export interface TlsCertificate {
  path: string;
  /**
   * Every name on the certificate, wildcards last. DNS names and IP addresses
   * together: one issued for an address carries no DNS name at all.
   */
  names: string[];
  /** The one the panel would hand out: the first that is not a wildcard. */
  domain: string;
  /**
   * Whole days left, negative once expired, null when the file cannot be read.
   * Worth watching because a certificate issued for an address lives six days
   * and is kept alive by a renewal timer, so a timer that has stopped shows up
   * here days before it shows up as a panel that will not open.
   */
  expiresInDays: number | null;
  /**
   * Why saving this path would be refused - it is not there, or the service
   * cannot read it - in the words the save itself would use. "" when it would
   * be accepted. A certificate that exists but cannot be parsed is not a
   * problem here: gunicorn decides that, and it says so by failing to start.
   */
  problem: string;
  /** The private key the verdict below is about, echoed like `path`. */
  keyPath: string;
  /** The same for the key, plus "this key is not from that certificate". */
  keyProblem: string;
}

/**
 * POST settings/account. The current password is the proof, and everything else
 * is optional: send a username to rename the account, a password to replace the
 * password, or both. Sending neither is refused by the API.
 */
export interface ChangeCredentialsInput {
  current: string;
  username?: string;
  new?: string;
}

/**
 * POST settings/2fa/enable is called twice. The first call has no body and
 * returns the enrolment material; the second carries the code and confirms it.
 */
export interface TwoFactorEnableResult {
  secret?: string;
  /** otpauth:// URI for an authenticator app. */
  provisioningUri?: string;
  /** data: URI PNG of the same URI. */
  qr?: string;
  confirmed?: boolean;
  enabled?: boolean;
}

export interface UpdateCheck {
  /** False when the network was unavailable; `reason` says why. It is never an error. */
  checked: boolean;
  reason?: string;
  current?: string;
  latest?: string | null;
  updateAvailable?: boolean;
  /** Release page, so the UI can link out instead of inventing a URL. */
  url?: string;
  notes?: string;
  publishedAt?: string | null;
}

/** Where a running or finished update got to. */
export type UpdatePhase =
  | "starting"
  | "resolve"
  | "preflight"
  | "download"
  | "verify"
  | "backup"
  | "install"
  | "confirm"
  | "done"
  | string;

export interface UpdateStatus {
  /**
   * The only field worth branching on. `phase` names the current step for a
   * progress line and gains new values as the updater does, so nothing here may
   * switch on it exhaustively.
   */
  status: "idle" | "running" | "succeeded" | "failed";
  phase?: UpdatePhase;
  /** A whole sentence, meant to be shown as it stands. */
  detail?: string;
  fromVersion?: string;
  toVersion?: string;
  started?: string;
  finished?: string;
  /** Where the pre-update archive went, which is what an operator needs first if it went wrong. */
  backup?: string;
  /** The tail of the update's log, not the whole of it. */
  log?: string;
  /**
   * Why this server cannot update itself, or "" when it can. Non-empty on a
   * panel installed without the updater, and the sentence to show in place of
   * the button rather than behind it.
   */
  unavailable?: string;
}

export interface RestoreResult {
  /** What the archive actually contained, e.g. ["amneziawg", "awg-panel"]. */
  restored?: string[];
  clients?: number;
  iface?: string;
  /**
   * Whether the web service is about to go down and come back, because the
   * archive carried a database and every worker is holding the file it replaced.
   */
  restarting?: boolean;
  /**
   * Everything the restore has to say for itself: settings it declined to
   * apply, client configs it removed, a tunnel that did not come back up. Not
   * decoration - it is the only account of what a restore actually did.
   */
  detail?: string;
}

/** GET health - the only unauthenticated route, used while waiting for a restart. */
export interface HealthResponse {
  ok: boolean;
  version: string;
}
