# AWG Panel HTTP API

Everything the SPA does, it does through this API. It is documented because the
panel is a thin client over it: scripting against it is a supported way to
manage a server, and reading it is the fastest way to understand what the panel
actually changes on disk.

## Conventions

**Base path.** Every route is served under the panel's secret base path, e.g.
`/awg/k2p9x4mt7wq1bz8n5rv3/api/v1/...`. `sudo awg-panel url` prints it. Anything outside the
base path returns a bare `404`, including `/api/...` at the root — that is
deliberate, so a scanner learns nothing.

**Authentication.** Either a Django session cookie, obtained from
`POST auth/login` and sent on every request (`credentials: same-origin` in a
browser, `-b/-c` with curl), or an API token in an `Authorization: Bearer
awgp_...` header. The panel's own UI uses the cookie; anything that is not a
browser should use a token, which is issued on the panel's API page (or under
Settings → Authentication, which shows the same card) or through
[`POST settings/tokens`](#post-apiv1settingstokens). A token
authenticates as the account and may do everything the panel can **except**
change the credentials that authorise it — every route under `settings/account`,
`settings/2fa`, `settings/sessions` and `settings/tokens`, plus `auth/logout` and
`POST restore`, answers `403` to one. (`GET backup` is open, and it is where that
rule ends: the archive carries `db.sqlite3`, whose session table holds the
`awgsessionid` cookie in the clear, so anything that can download one can sign in
as the account. See [`GET backup`](#get-apiv1backup) and
[docs/SECURITY.md](SECURITY.md) before pointing a token at it.)

**CSRF.** Every mutating request (`POST`, `PUT`, `PATCH`, `DELETE`) made with the
session cookie must carry `X-CSRFToken` matching the `awgcsrftoken` cookie.
`GET auth/session` sets it. A request authenticated by a bearer token carries no
cookie and needs no CSRF token: nothing can make a browser attach an
`Authorization` header it was not given.

**Case.** JSON is camelCase in both directions. Times are RFC 3339 UTC unless
the field name ends in a unit (`lastHandshake` is a Unix timestamp in seconds,
because that is what the kernel reports).

**Errors.** Always the same shape, plus an optional `code` when the client
needs to branch on the reason rather than the wording:

```json
{ "detail": "S1 must be between 15 and 150.", "errors": { "S1": "..." } }
```

| Status | Meaning |
|---|---|
| 400 | validation failed; `errors` maps field to message |
| 401 | not logged in, an unusable API token, or `detail: "totp_required"` |
| 403 | CSRF missing, locked out after repeated login failures, or an API token on a route it may not touch |
| 404 | no such client, or a path outside the base path |
| 409 | name already taken (`code: "name_in_use"`), or the address pool is exhausted |
| 502 | `awg` / `awg-quick` failed; `detail` summarises its stderr |
| 503 | `code: "not_configured"` — there is no server config here; or the config lock was held for longer than 30 s, in which case retry after the `Retry-After` seconds the answer carries |

`409` + `"code": "name_in_use"` is the one of the two conflicts that is about a
name — a client or a token already answers to it. The other 409 carries no code
and means the tunnel subnet has no free address left, which is a fact about the
server rather than about what was sent, so a caller that retries with a
different name would be retrying for ever.

`503` + `"code": "not_configured"` is what every config-reading endpoint
returns when `<conf_dir>/<iface>.conf` does not exist — the panel installed
`--standalone` before the VPN, a wrong `AWG_IFACE`, or a fresh container. It is
an expected state rather than a fault, so it is not logged as an error, and
`GET server/status` keeps answering `200` throughout because that is the screen
an operator uses to find out what is wrong.

Secrets are never returned by a listing or status route. Private keys appear
only in `clients/<name>/config`, `clients/<name>/qr` and `clients/export.zip`,
which exist to hand them out.

### A worked example

```bash
BASE=$(sudo awg-panel url)          # e.g. http://10.0.0.5:2097/awg/k2p9x4mt7wq1bz8n5rv3/
J=$(mktemp)

# session + CSRF cookie
curl -sc "$J" "$BASE/api/v1/auth/session" >/dev/null
CSRF=$(awk '$6=="awgcsrftoken"{print $7}' "$J")

curl -sb "$J" -c "$J" -H "X-CSRFToken: $CSRF" -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"..."}' \
     "$BASE/api/v1/auth/login"

curl -sb "$J" "$BASE/api/v1/clients" | jq '.clients[].name'
```

The same thing with a token, which is what a script should do — no cookie jar,
no CSRF dance, and one header on every call:

```bash
BASE=$(sudo awg-panel url)
TOKEN=awgp_...                      # issued on the panel's API page

curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/clients" | jq '.clients[].name'

curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"name":"laptop"}' "$BASE/api/v1/clients"
```

---

## Auth

### `POST api/v1/auth/login`

```json
{ "username": "admin", "password": "...", "totp": "123456" }
```

Omit `totp` on the first attempt. If the account has a confirmed TOTP device the
server answers `401` with `{"detail": "totp_required"}`; repeat the request with
the code. Five failures lock the source IP out for 15 minutes (`403`, with the
remaining time in `detail`). The response is the same session payload as below.

### `POST api/v1/auth/logout`

Clears the session. Always `204`.

### `GET api/v1/auth/session`

Unauthenticated-safe: it is how the SPA bootstraps and how the CSRF cookie is
issued.

```json
{
  "authenticated": true,
  "username": "admin",
  "otpRequired": false,
  "version": "1.0.0",
  "theme": "system",
  "language": "en",
  "basePath": "/awg/k2p9x4mt7wq1bz8n5rv3/",
  "mock": false,
  "token": null
}
```

`token` is `null` for a browser and for an anonymous caller. For a request
carrying a bearer token it names the credential that authenticated it:

```json
{ "name": "nightly backup", "expiresAt": "2026-09-04T09:14:02Z", "renewOnUse": true }
```

Three fields and no more — not the hint, not the address it was last used from,
and above all not any other token this account holds. This is a caller
identifying itself, not a listing, which is what keeps it on the right side of
the line the token rules draw: being told the expiry of the secret it is already
holding grants a token nothing it did not walk in with. `settings/tokens` stays
closed to it.

It is here because the alternative is that a job running at three in the morning
finds out its token lapsed by failing. Check it before a long run rather than
after.

---

## Clients

A client is a `[Peer]` block in `awg0.conf` plus a rendered
`clients/<name>.conf` — the files the panel owns outright. Metadata the
config cannot hold (quota, expiry, note, email) lives in the panel database,
keyed by public key so a rename keeps it, including one made in the config file
by hand. `lastHandshake` and
`endpoint` are stored there too, because the kernel forgets them whenever the
interface goes down; the live values are served whenever there are any, and the
stored ones only stand in for them.

Clients are addressed **by name** in the URL, because that is the identifier the
config file carries and the client's own file is named for. Names match
`^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$`.

### `GET api/v1/clients`

```json
{
  "clients": [
    {
      "name": "phone",
      "publicKey": "hE1v...=",
      "ip": "10.13.13.2",
      "ip6": "fd7a:1e5f:22::2",
      "allowedIps": "0.0.0.0/0, ::/0",
      "leaksIpv6": false,
      "enabled": true,
      "disabledReason": "",
      "createdAt": "2026-08-04T18:00:00Z",
      "expiresAt": null,
      "expiryOverridden": false,
      "quotaBytes": 0,
      "downBps": 0,
      "upBps": 0,
      "email": "",
      "note": "",
      "online": true,
      "status": "online",
      "lastHandshake": 1754332800,
      "lastSeen": 1754332799,
      "endpoint": "203.0.113.9:51234",
      "rxBytes": 8123456,
      "txBytes": 91234567,
      "offsetRx": 0,
      "offsetTx": 0,
      "rateRx": 1024,
      "rateTx": 40960,
      "quotaUsed": 99358023,
      "quotaPercent": 0
    }
  ],
  "subnetCidr": "10.13.13.0/24",
  "freeIps": 252,
  "total": 1,
  "totalAll": 1,
  "expiredCount": 0,
  "disabledCount": 0,
  "expiredOrDisabledCount": 0,
  "page": 1,
  "pageSize": 50
}
```

**This is one page.** `clients` holds at most `pageSize` rows; the counts beside
it are over the whole server, because none of them is answerable from a page.
`total` is what the search and the filter matched and is what sizes the
pagination; `totalAll` is how many named clients there are.

The last three are what a bulk removal would take, and they are here so a caller
can put a number on the sweep before running it. `expiredCount` is how many
`clients/remove-expired` would actually take, which is one short of every
expired client — a client an admin switched back on past its date is one the
sweep refuses. `disabledCount` is how many `clients/bulk-remove` takes when
asked for `disabled`, and `expiredOrDisabledCount` is how many it takes when
asked for both — the union rather than the sum, because a lapsed client the
collector has already switched off is in each of the other two numbers and is
removed once.

Every parameter is optional, and a value this version does not understand is
ignored rather than rejected: an old bookmark opens the list rather than a 400.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `q` | `""` | Substring, case-insensitive, over name, address, note, email and endpoint. |
| `status` | `all` | `all`, `active`, `online`, `offline`, `disabled` or `expired`. The first five read the row's `status` word; `expired` reads the date, which is a separate question a client can answer yes to while online. |
| `sort` | `created` | `created`, `name`, `ip`, `usage` or `lastHandshake`. |
| `direction` | `asc` | `asc` or `desc`. |
| `page` | `1` | 1-based. A page past the end answers the last page that has rows, and says so in `page`. |
| `pageSize` | `50` | Capped at 500, above which the whole query string is ignored and the default page answered. `0` is the exception and means every row the query selected, unpaged; the answer echoes `pageSize: 0` and `page: 1`. |

The narrowing and the ordering are applied to every client before the page is
cut, so page two of a name sort is the second fifty names on the server and not
the second fifty config entries sorted among themselves. They are applied by the
database, against an indexed copy of the config that the panel rebuilds whenever
the files it was built from change — so what a page costs to serve follows the
size of the page and not the size of the server.

`pageSize=0` is the one request that does not: it builds a row per selected
client, so its cost and its response follow the size of the server. It exists
because a caller asking for the whole list cannot name a size that means all of
it — the count is in the answer — and 500 is not that number written larger. The
panel sends it for the "All" rows-per-page choice and warns above the table when
what comes back is long enough to make the page slow.

A change made through this API does not cost that rebuild. Creating, deleting,
renaming, disabling or re-scoping a client updates the copy directly, since the
config the panel just wrote is one it has already parsed, so provisioning a
server one `POST /clients` at a time does not re-read the whole config per
client. A change made outside the panel still does: the copy carries the
timestamps, sizes and inode of the files it was built from, and a mutation that
finds them moved hands the work back to a full rebuild rather than folding into
a copy that is already behind.

Two consequences worth knowing. `q` searches the last endpoint the collector
recorded rather than the one in the current live dump, which differ only in the
seconds after a client moves networks. And `status=online` and `status=offline`
are the two filters that cannot be answered from a column, because whether a
peer is connected is the live dump's answer; they are settled against it, which
is why they are the only two whose cost still grows with the server.

`rxBytes` is what the client **uploaded**, `txBytes` what it **downloaded** —
the server's point of view, matching `awg show dump` and `traffic.db`. Both are
all-time totals that survive interface restarts, less anything cleared by
`reset-usage`. `disabledReason` is `""`, `manual`, `quota` or `expired`.

`lastHandshake` and `lastSeen` are both Unix seconds and both `0` for never.
The first is the kernel's own figure; the second is the later of it and the
moment the peer's receive counter last moved, and it is the one to show as
"last seen". Every config this project issues carries `PersistentKeepalive`, so
a connected client moves its receive counter on that interval, while a
handshake reads as minutes stale on a client that is transferring right now —
the kernel rekeys only every couple of minutes. See
[`stats/live`](#get-apiv1statslive) for what a module older than
`v3.0.20260805` does to that reading.

`offsetRx` and `offsetTx` are what `reset-usage` cleared, and are therefore what
has already been taken off the two figures above. They are here for a client
polling `stats/live` beside this endpoint: the blob carries the same all-time
counters out of the collector's memory, two seconds old rather than up to ten,
and with nothing subtracted — the collector has no way of knowing a counter was
ever cleared. `blobRx - offsetRx` is `rxBytes`, a few seconds fresher. A peer
the blob does not mention has no key on the interface, which is the case for
every disabled client and is why these rows remain the answer rather than a
fallback.

`ip6` is the IPv6 address the server routes to this client, and `""` when it
routes none. It is read back out of the peer entry rather than derived from
`ip`, so a peer that has not been given a route reports nothing rather than the
address it would have had — the gap is the information.

`leaksIpv6` is `true` when this client's config routes the whole of IPv4 and
none of IPv6 on a server that can carry IPv6. That client's device reaches
every dual-stack destination outside the tunnel, with its own address, while
the VPN reports itself connected — there is no error and no symptom, which is
why the field exists. It is computed here rather than left to the caller
because deriving it needs the server's prefix, the client's route list and the
rule relating them, and a second copy of that rule is one that can drift into
silently under-reporting.

Two things it deliberately does not say. A split tunnel is not leaking: it
routes what somebody chose to route. And the question is about coverage, not
spelling — `0.0.0.0/1, 128.0.0.0/1` is a full IPv4 tunnel and is reported as
leaking, exactly as `0.0.0.0/0` is.

The field tracks the config **the server issued**, which is the only thing the
server can see. It clears as soon as that file is rewritten, so it answers "has
this client been re-issued?" and not "has the device picked it up?" — nothing
changes on a phone until it re-imports, and no server-side field can know
whether it has.

Whether a client is switched off and whether its date has passed are two
independent facts, and two fields answer them. `status` is `online`, `idle`,
`offline`, `disabled` or `quota` — never `expired`, for any client: one the
collector switched off for its date is `disabled` with `disabledReason:
"expired"`, and one that has lapsed but is still switched on reads as whatever
it is doing. `expiresAt` is the date itself, and `expiryOverridden` says an
admin switched this client on after that date had already passed — see `PUT`.

Clients come back **oldest first**. `createdAt` is the peer's `# Created`
comment, RFC 3339 in UTC, which the panel writes on every add; the
panel keeps its own copy so the date survives a hand edit or a restore that
loses the comment, and serves `null` for a peer neither ever recorded.

A row carries no `dns`. Every other field is either a column of the indexed copy
or one read of the live blob, so a page costs the same whatever the server holds
— but `dns` is the `DNS` line inside each client's own config file, which no
index can be kept level with cheaply and which nothing on the list displays.
Serving it on rows cost one file open per client, on a list every open tab polls
every thirty seconds. Fetch the client by name for it, which is one file at the
moment something actually wants it.

### `GET api/v1/clients/<name>`

One client, in the same shape as a list row plus `dns`. Found by name across
every client on the server rather than within a page, so a name deep in the list
answers rather than 404s.

`dns` is the `DNS` line in `clients/<name>.conf` — what the device is actually
holding, which is normally the server default because that is what was written
into the file when the client was issued. `""` means the config has no `DNS`
line at all. **Absent and `""` are different answers**: absent is a list row,
which was never asked, and a caller that treats the two alike will show an
empty DNS box for a client that has one.

### `POST api/v1/clients`

```json
{ "name": "laptop", "allowedIps": "0.0.0.0/0", "dns": "1.1.1.1",
  "quotaBytes": 53687091200, "expiresAt": "2026-12-31T23:59:59Z",
  "downBps": 50000000, "upBps": 0,
  "email": "", "note": "" }
```

Nothing is required. `name` left out — or sent blank — has the server draw one:
nine characters of lower-case letters and digits, without `l`, `1`, `o` and `0`,
which are the four that get read wrong when a name is typed back somewhere else.
It is checked against the peers in the config and against the files under
`clients/`, under the config lock, so a drawn name never lands on a client that
already exists or on a config file left behind by one that used to; the created
client comes back carrying whichever name it ended up with. A name that *is*
sent must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$` and is a `409` if it is
taken.

Everything else falls back to `clients.env`. The address is the lowest free host
in the subnet, reusing gaps, exactly as the installer's own allocation does. The
subnet is whatever the server's `Address` says it is, prefix included, so a
`/16` keeps allocating past `.0.254` where a `/24` would have stopped. Applied
to the running interface with `awg syncconf`, so no existing session drops.
Returns the created client. `409` if the address pool is full.

### `PUT api/v1/clients/<name>`

Any subset of `name` (rename), `allowedIps`, `dns`, `quotaBytes`, `downBps`,
`upBps`, `expiresAt`, `email`, `note`, `enabled`. Renaming keeps the keys and the address, so no
re-import is needed. Setting `enabled: false` writes the shared `# Disabled`
marker and removes the key from the kernel; the address stays reserved and the
config file is kept. Re-enabling clears a `quota` or `expired` reason set by the
collector.

Setting `enabled: true` on a client whose `expiresAt` has already passed also
records that the admin overruled that date: `expiryOverridden` comes back true,
the collector stops enforcing the expiry, and `remove-expired` skips the client.
The record is tied to the date it forgives, so changing `expiresAt`, clearing it
or switching the client off again all end it. A quota has no equivalent — it is
a measurement rather than a decision, so a client re-enabled over its limit is
switched off again on the next pass, and the way to keep it on is to raise
`quotaBytes` or call `reset-usage`.

#### Speed limits

`downBps` and `upBps` are bits per second, `0` for no limit, and both default to
`0` — a client created without them behaves exactly as every client did before
they existed. The unit is bits rather than the megabits an operator quotes
because it is what `tc` takes; the panel converts at the form.

A client created without them is given the server's own default for new clients
(`shaperDefaultDownMbps` and `shaperDefaultUpMbps`, `0` on a server that has set
none). Sending `0` explicitly is different: it means no limit, and overrides the
default.

Three things are refused rather than stored:

* either field on a server whose `shaperOn` is `0`. Nothing would enforce it, so
  a stored number would be a promise the server never made.
* `upBps` on a server with `shaperUpload` off. A client's upload has been
  through NAT by the time it can be shaped, so the only place to enforce one is
  the server's own outgoing interface — see the setting for what that costs.
* either field on a server whose tunnel subnet is `/16` or wider. A client's
  place in the shaping structure is derived from its offset into the subnet and
  the kernel's class id is 16 bits, so a network that large has no id left to
  give its last addresses. `/17` is the widest that can be shaped and still
  holds 32765 clients.

Moving the tunnel subnet renumbers every client's place in that structure, and
the panel converges on its own: the next pass reads back what the kernel holds,
finds the old classes describing addresses nobody wants, removes them, and
writes the new ones. Nothing has to be re-applied by hand. Widening past `/17`
switches speed limits off rather than failing repeatedly — the structure comes
down and `manage shape` says why.

A limit is applied to the kernel by the request that set it, so the effect is
immediate rather than at the collector's next pass. That application is
best-effort: the row is stored either way and the collector reconciles what the
kernel actually holds against it every `enforceIntervalSec`, so a limit that
could not be applied - no `iproute2` on the box, an interface that is down -
comes back as soon as it can be. What the client list reports is always what was
asked for, never what the kernel is currently doing.

Removing a client clears its limit too, and that matters: the shaping structure
is keyed by tunnel address and the allocator reuses freed addresses, so a limit
left behind would be inherited by the next client given that address.

### `GET api/v1/clients/<name>/config`

`text/plain` attachment: the client's full configuration, **including its
private key**.

The file on disk is served verbatim except for one substitution: with
`configEndpointMode` set to `domain`, the host in the `Endpoint` line is
replaced with the panel's certificate domain (or `configEndpointHost`). The port
and everything else are untouched, and nothing is written back —
the backup archive and the files under `clients/` keep the address the server
was configured with.

### `GET api/v1/clients/<name>/qr`

`image/png`, `Cache-Control: no-store`. The same config, substitution included,
as a QR code for the AmneziaWG app.

### `POST api/v1/clients/<name>/reset-keys`

New keypair and preshared key; name, address and options are kept. The old
config stops working immediately — the client must re-import.

### `POST api/v1/clients/<name>/reset-usage`

Clears both of the client's traffic figures: the all-time totals and the stored
history behind `clients/<name>/traffic`.

The totals are zeroed by storing an offset, so `traffic.db` itself is left alone
and keeps showing the true all-time figure. The history has no second copy, so
its rows are deleted — every day and month the panel holds for this client, not
only the ones the charts draw. Nothing else is touched: the server's own daily
totals stand, so `stats/traffic` and the dashboard read the same after this as
before, and no other client's rows go with it.

The deletion happens in the request. The collector holds today's per-client
figure in memory in another process and would write it straight back, so the
request stamps the metadata row and the collector drops what it is holding on
its next reconcile — within one `enforceIntervalSec`, 60 s by default. A client
transferring through that window can have today's bar reappear until then.

For every client at once, and the server's own days with them, see
[`POST api/v1/stats/traffic/reset`](#post-apiv1statstrafficreset). It is not
this call in a loop: that one clears what this one deliberately leaves.

### `DELETE api/v1/clients/<name>`

Removes the peer, its config file, its metadata and its `traffic.db` row, and
applies live.

### `POST api/v1/clients/bulk-remove`

```json
{ "expired": true, "disabled": true }
```

The same removal as `DELETE clients/<name>`, for whole categories of client at
once. Worth using rather than a loop: every client in the sweep leaves in a
single config rewrite and a single `awg syncconf`, where thirty deletions are
thirty of each and thirty windows in which the config on disk is halfway through
the job.

| Field | What it takes |
| --- | --- |
| `expired` | Every client whose `expiresAt` has passed — the date and nothing else, so a stale `expired` marker on a client whose date has since been moved forward is not enough. One exception: a lapsed client an admin switched back on (`expiryOverridden`) is left alone, because that is a decision the collector honours and a sweep must not undo behind their back. |
| `disabled` | Every client that is not working: switched off by an admin, switched off by the collector for its quota or its date, or still switched on and already past `quotaBytes` — that last one is a client the panel already shows as stopped, and the collector will switch it off within a poll. |

Both default to `false`. A body naming neither is `400` rather than a removal of
nothing: "remove nothing" and "my flag never arrived" are different mistakes and
they deserve different answers. Asking for both is a union, not two passes — a
lapsed client the collector has already switched off is one client and is
removed once.

```json
{ "removed": ["old-phone", "laptop"], "count": 2 }
```

`removed` names what the server actually took, which need not be what the caller
expected: a client removed by something else since the caller last listed is
simply not in it. To put a number on a sweep before running it, read
`expiredCount`, `disabledCount` and `expiredOrDisabledCount` from
`GET api/v1/clients` — they are counted by the same rules, over every client.

### `POST api/v1/clients/remove-expired`

The `expired` half of the sweep above and nothing else, which is what it did
before `bulk-remove` existed. It takes no body and answers in the same shape.

### `POST api/v1/clients/bulk-limit`

```json
{ "downBps": 20000000, "upBps": 0 }
```

One speed limit, written over every client on the server. The operation the
per-client form cannot express: an admin who has decided everybody gets 20 Mbit
should not have to open four thousand dialogs, and `shaperDefaultDownMbps`
deliberately reaches only clients created after it was set.

It overwrites and there is no undo. Whatever any client had — a number set by
hand last week, no limit at all — is replaced. Both fields are required rather
than defaulted for that reason: a body that lost half of itself in transit must
not read as "and clear everybody's upload limit while you are here". The same
three refusals as a per-client limit apply, so `upBps` above zero needs
`shaperUpload` on, and either field needs `shaperOn`.

```json
{ "changed": 4093, "applied": true, "reason": "" }
```

`changed` counts the rows that actually moved, so calling it twice with the same
numbers answers `0` the second time rather than the size of the server. `applied`
is `false` with a `reason` when the numbers were stored but the kernel could not
be brought into line — no `iproute2` on the box, an interface that is down. That
split is the same one every limit-setting path here reports: what a client should
have is the panel's record and is stored whether or not anything is currently
enforcing it, and the collector's next pass applies it when it can.

One statement and one reconcile pass, not a request per client: a four thousand
client server takes about a second.

### `GET api/v1/clients/export.zip`

Every client config in one archive, with the same endpoint substitution as a
single download. Contains private keys.

---

## Server

### `GET api/v1/server`

```json
{
  "iface": "awg0",
  "address": "10.13.13.1/24, fd7a:1e5f:22::1/64",
  "subnetCidr": "10.13.13.0/24",
  "subnetCapacity": 253,
  "subnet6Cidr": "fd7a:1e5f:22::/64",
  "subnet6Mode": "native",
  "listenPort": 41820,
  "mtu": 1400,
  "publicKey": "Xq3...=",
  "dns": "8.8.8.8, 8.8.4.4",
  "endpointHost": "203.0.113.7",
  "endpointPort": "",
  "allowedIpsDefault": "0.0.0.0/0, ::/0",
  "keepalive": "25",
  "params": { "Jc": "5", "Jmin": "63", "S1": "199", "H1": "812431677-812434903", "I1": "<b 0xc3..." },
  "postUp": ["iptables -t nat -A POSTROUTING ..."],
  "postDown": ["..."],
  "preDown": ["/usr/local/bin/awg-panel manage trafficsync || true"]
}
```

The server private key is never included.

`subnet6Cidr` and `subnet6Mode` are read-only, and both are `""` on a server
that carries no IPv6. The mode is `native` (a routed `/64`, forwarded
untranslated), `nat` (a unique-local `/64` masqueraded behind the host's own
address) or `blackhole` (a unique-local `/64` claimed and refused, for a host
with no IPv6 upstream). Which one applies follows from what the machine
actually has, which the installer works out; offering it in a form would only
let an admin choose one the host cannot do.

### `PUT api/v1/server`

Send only what changed — any of the network fields above, or any key in
`params`.

`subnetCidr` moves the whole tunnel network (`"10.20.0.0/16"`). The server's
`address` is derived from it — the first host — so send one or the other; both
together is only accepted when they agree. The `-s <cidr>` in the MASQUERADE
`postUp`/`postDown` rules is rewritten to match, and a warning says so, because
a NAT rule left on the old network leaves every client hand-shaking and reaching
nothing. Prefixes from `/16` to `/30` are accepted, and a wider one is refused:
`/16` already holds 65533 clients, which is past what a single tunnel is
dimensioned for. Clients already holding an
address outside the new network are listed in `warnings`: widening a prefix
(`10.13.13.0/24` → `10.13.0.0/16`) strands nobody, moving the network strands
everybody. `subnetBase` — three octets, `/24` implied — is still accepted as the
name this field used to have.

`address` on a dual-stack server is one line with both families on it, exactly
as `GET` hands it back and as the installer wrote it: one IPv4 address and one
IPv6 address, comma-separated, the server at the first host of each and the
IPv6 half a `/64`. Send it back that way. That half is read-only in the same
sense `subnet6Cidr` is — moving it, dropping it or adding one to a tunnel
without one is refused with a message saying so, because the prefix is written
in the `ip6tables` hooks, in `clients.env` and in every peer's route as well as
here, and only the installer writes all of them together. `subnetCidr` moves the
IPv4 pool and leaves the IPv6 half exactly where it was.

```json
{ "needsRestart": true, "mustReimport": true, "applied": true,
  "warnings": ["ContentPaddingAddition is discarded by the Amnezia app importer."] }
```

How a change is applied:

| Changed | Applied by | Clients must re-import |
|---|---|---|
| peer add/remove/enable/disable | `awg syncconf`, hot | no |
| `listenPort`, `address`, `mtu` | interface restart | port: yes |
| any obfuscation parameter | interface restart | **yes** |
| `dns`, `endpoint*`, `allowedIpsDefault`, `keepalive` | client configs only | yes |

**The save performs the restart.** `needsRestart` is what the change cost, not a
job left for the caller: a change in either of the two restart rows takes the
interface down and back up before the response is written, so every session
drops for a second or two. `applied` is `true` when the running interface now
carries the change, and `false` only when it could not — the interface was
already down, or `awg-quick` failed — with a sentence in `warnings` saying
which. The config on disk is correct either way; `POST server/restart` is then
the way to catch the interface up. Hook changes (`postUp`, `postDown`,
`preDown`) are the one exception: they are written but not applied, because they
only run at the next bring-up and nothing is gained by dropping sessions for
them.

A changed `listenPort` also moves the rule in an active ufw or firewalld, the
same way the installer opens the first one. Cloud security groups cannot be reached from the
server and stay manual; `warnings` names whichever case applies. Any port from
1 to 65535 is accepted, privileged ones included, but a port another process is
currently bound to is refused with a `400` naming the holder.

When `mustReimport` is true the client configs on disk have already been
regenerated (the equivalent of `awg-panel manage resync`); the devices still need the
new file. `GET clients/export.zip` is the one-click way to collect them.

Validation failures return `400` with a message per field, e.g.
`{"errors": {"H2": "This range overlaps H1 (5-100). Each packet type needs a range of its own."}}`.

### `GET api/v1/server/params`

The parameter catalog — the single source of truth for validation rules *and*
the help text the UI renders, so the two cannot drift.

```json
[
  {
    "key": "S1",
    "group": "sizes",
    "label": "Junk in front of the handshake initiation (bytes)",
    "kind": "int",
    "helpShort": "Random bytes prepended to the initiation packet to move it off 148 bytes.",
    "helpLong": "A WireGuard handshake initiation is always exactly 148 bytes ...",
    "mustMatchClient": true,
    "importerSafe": true,
    "feature": null,
    "supported": true,
    "min": 0, "max": 1280,
    "recommended": "24-320, drawn per server",
    "default": null,
    "optional": true
  }
]
```

`mustMatchClient` means changing it invalidates every issued config.
`importerSafe: false` means the setting arrived in AmneziaWG 3.0 (3.1 for
`RandomTrailers`) and the Amnezia mobile app's `.conf` importer silently
discards it, so a client that imports the file negotiates without it and the
handshake fails with no error. It is a fact about the parameter, stated on the
field it belongs to; it is no longer a badge on every field of the advanced
group, and no longer an advisory on every save that sets one, because a current
AmneziaWG client speaks 3.0 and the generator draws that group by default.
`supported: false` means the installed module or tools do not have the feature.
`feature` names the flag in
[`GET server/status`](#get-apiv1serverstatus)'s `features` object that
`supported` was read from, in the same spelling, and is `null` for a parameter
every module version has. `default` is what the panel writes when the field
is left empty. It is `null` for every parameter that is drawn per server rather
than shipped — the whole of the junk, sizes, headers, imitation and advanced
groups, and `ListenPort` — for the reason below.

`recommended` is reference data, and for the obfuscation groups it names a
*band* rather than a value: there is no recommended `S1`, because a value this
project recommended to everybody would be one every server shared, and a shared
value is a signature. The reasoning and the full set of bands are in
[PANEL.md](PANEL.md#obfuscation). `POST server/reconfigure` is how to draw one.

### `POST api/v1/server/reconfigure`

Draws a complete, valid set for this server alone and returns it as a
**preview**; nothing is saved until you `PUT api/v1/server`. Every field of the
body is optional, so `{}` still means what it always did:

| Field     | Default      | Meaning                                                         |
| --------- | ------------ | --------------------------------------------------------------- |
| `profile` | `"standard"` | `standard`, `dpi`, `fast` or `random` — which band to draw from |

One draw covers the whole Obfuscation page: `Jc`…`I5` **and** the AmneziaWG 3.0
group behind them. There was a `scope` here — `obfuscation` or `advanced` —
while that second group was something an operator opted into rather than part of
what a server is configured with. The clients caught up, and two draws for one
server then meant a page that could be left half filled, or filled from two
different bands.

The profile is read in two preset tables, one per half, and the bands behind the
name are not the same in both: the obfuscation bands trade bandwidth for cover,
the advanced ones trade how often this server handshakes at all. One name, one
consistent set.

Nothing here describes the form. An `mtu` and an `s4` were each accepted once so
a preview could be drawn against a value the form held and the disk did not, and
both were the same mistake: a draw is checked at save time against the merged
config, so a number that exists only in a form is never the one in force when
that happens. Everything else comes from the config.

Every profile draws every value at random; they differ in the band, which is
the trade between how much of the protocol's shape a value hides and what
sending it costs. `random` spans the other three, so that the choice of profile
is not itself something to fingerprint a server by. An unknown name is a 400.

The response carries `Jc`, `Jmin`, `Jmax`, `S1`–`S4`, `H1`–`H4`, `I1`–`I5` and
the advanced group below them:

```json
{
  "params": {
    "Jc": "4", "Jmin": "44", "Jmax": "185",
    "S1": "48", "S2": "61", "S3": "211", "S4": "14",
    "H1": "1190147702-1190149813", "H2": "1615300653-1615304418",
    "H3": "136213748-136216915", "H4": "546936080-546937301",
    "I1": "<b 0x000100082112a442><r 12><b 0x00240004><r 4>",
    "I2": "<b 0x0101000c2112a442><r 12><b 0x002000080001><r 6>",
    "I3": "<b 0x8063><r 6><b 0x3f721fcb><r 154>",
    "I4": "", "I5": "",
    "HeaderProtectionKey": "xYbOANvXjB9HdbfeC92cefXNNd8U2iIXtvmnHMogkCs=",
    "ContentPaddingAddition": "12-64",
    "RekeyAfterTime": "140-150", "RekeyTimeout": "5-6", "RejectAfterTime": "305-372",
    "KeepaliveTimeout": "8-15", "MaxHandshakeAttempts": "17-20",
    "RandomTrailers": "on"
  },
  "warnings": []
}
```

That one is a WebRTC call: a STUN binding request, its success response, then a
packet of media. An empty `I` slot is meaningful, not missing — the generated
session is three to five packets long, and an empty value is how
`PUT api/v1/server` spells "remove this line", which matters because
`awg-quick` reads `I4 = ` as a malformed imitation packet and refuses to bring
the interface up. Send the whole `params` object back unchanged.

`S4` is added to the front of every data packet, so it is drawn against what the
MTU leaves free. That budget is enforced, not advised: `PUT api/v1/server`
rejects any `MTU + S4` above 1440, reported against both fields so it is
actionable from either page. It is the one overrun with no symptom at the moment
it is made — the tunnel comes up, small requests work, and only full-size
packets vanish. `ContentPaddingAddition` is deliberately *not* in that sum: it
goes inside the encrypted payload, and the sender clamps each packet's share of
it to what that packet leaves below the MTU, so it cannot make a full-size
packet larger however wide the range is.

Every draw also stays at or above 12 bytes on `S1`–`S4`, which is what a header
protection key needs (see below).

The five timers come back as `lo-hi` ranges rather than numbers. `awg setconf`
parses them with `u16_range_from_string` and the kernel redraws inside the range
every time it arms the timer, so a range makes the handshake cadence — the one
feature that survives every byte-level disguise — a distribution instead of a
constant. A plain number is still accepted on `PUT api/v1/server`: it is what
every server installed before this wrote, and what `awg showconf` prints back for
a range of width zero.

The advanced half is drawn as one consistent set — `RejectAfterTime` is derived
rather than drawn, because it has to outlast a whole rekey cycle: the **top** of
`RekeyAfterTime` plus the tops of the `KeepaliveTimeout` + `RekeyTimeout` a peer
may spend waiting for the answer, cleared by the **bottom** of its own range.
`PUT api/v1/server` enforces that same bound at those same ends, so a set
assembled by a caller is held to the worst draw the ranges allow rather than a
typical one. Every value in it needs AmneziaWG 3.0 at the far end,
which is what a current client is; a parameter the installed module cannot do
comes back empty with a sentence saying so, rather than producing a set that
`PUT api/v1/server` would then reject.

`RandomTrailers` comes back as `"on"`, and is the only member of the set that is
a switch rather than a value — there is nothing to copy between the two ends,
only on or off. It used to come back empty: it arrived in AmneziaWG 3.1 rather
than 3.0, and a peer without it measures an arriving handshake, finds it longer
than the one it expects and drops it with no error at either end. That is a real
cost and it is worth stating, and how much it costs depends on whether
`HeaderProtectionKey` came back with it.

Where it did, this newly turns away a peer on exactly 3.0 and nothing more:
anything older, and anything imported from a `.conf` through the Amnezia app,
already fails on the key beside it and fails the same silent way. Where it did
not — the draw empties the key when the server's MTU left `S4` too short to
carry its nonce — nothing else in the set fails outright, because a peer too old
for the padding or the timers ignores those lines and stays connected. On that
server `RandomTrailers` is the only hard stop in the config, and it turns away
every client below 3.1. Send `""` back to remove the line.

It does nothing to data packets while `ContentPaddingAddition` is set — that one
already decides their padding, and the two do not stack. What it covers is the
handshake, whose length is otherwise the same every time however random the bytes
in it are.

AmneziaWG 3.1's other addition, `DisableCookies`, is deliberately not drawn by
either generator. It is a server-side switch — it
suppresses the cookie challenge that answers a handshake flood — so it belongs
to no obfuscation profile, appears in no client config, and never sets
`mustReimport`. It is still an `[Interface]` value, so saving it sets
`needsRestart`, and `PUT api/v1/server` takes it under `params` like any other
key. Turning it on comes back with a warning saying what it costs.

`ContentPaddingAddition` comes back as a range rather than a number, and that is
not cosmetic. The kernel uses it *instead of* the padding it does anyway — every
packet is otherwise rounded up to a multiple of 16 bytes — so a single value
replaces a length known to within 16 bytes with one that tracks the packet
inside byte for byte. A range is the only form worth writing, and the generator
will not draw one whose top is below 16.

`HeaderProtectionKey` puts a floor under the padding drawn beside it. The nonce
the key is used with is read from the first 12 bytes of the `S1`–`S4` prefix on
each packet, so all four have to be at least 12 or the kernel refuses the whole
device configuration: `awg setconf` returns `EINVAL` and the interface does not
come up. `PUT api/v1/server` rejects that combination, reported against the key
and each short field. One draw cannot produce it — the padding and the key come
out of the same response, and every band starts at or above the floor — so the
only way left to reach it is to save half a preview. The one case the preview
still has to answer is an MTU so large that no `S4` fits at all: the key is left
empty with a sentence saying so, because the MTU is on another page.

To turn the group off again, `PUT api/v1/server` with every one of those eight
keys set to `""`. That removes the lines rather than blanking them.

### `POST api/v1/server/restart`

`awg-quick down && up`. Every session drops for a moment. A save that needs a
restart already does this, so this endpoint is for the cases it could not: an
interface that was down at the time, a restart that failed, or a hook change.

### `POST api/v1/server/stop`

Takes the interface down and leaves it down. Every session drops and none of
them come back until `server/start`.

`systemctl stop awg-quick@<iface>` when systemd believes it is holding the
interface up, then `awg-quick down` if the interface is still there afterwards.
Both halves are needed. The unit is `Type=oneshot` with `RemainAfterExit=yes`, so
systemd reads "active" off whether it ran `ExecStart` rather than off whether the
interface exists, and `install.sh` enables the unit and then brings the tunnel up
with `awg-quick` — so on an ordinary server it is `enabled` and `inactive` over a
running tunnel, and `systemctl stop` against that exits `0` having run nothing.
Going through the unit where systemd does think it is up is what keeps
`serviceActive` true afterwards, and what runs `ExecStop` under systemd's own
timeout.

The response is decided on the interface rather than on the exit code, in both
directions: a job that failed after the tunnel had gone got the caller what they
asked for and answers `204`, and one that exited `0` without touching a running
interface does not get to.

The unit is left **enabled**, so a reboot brings the tunnel up again. A stop that
outlived a reboot would be a second piece of state with nothing in
`server/status` to show it; `systemctl disable` is the way to ask for that, and
`serviceEnabled` is where the answer appears.

On a machine where the unit does not exist at all — an AmneziaWG somebody set up
by hand — this falls back to `awg-quick down`, since there is then nothing
systemd believes that using it could contradict.

Idempotent: stopping a tunnel that is already down answers `204`.

### `POST api/v1/server/start`

`systemctl start awg-quick@<iface>` where the unit exists, `awg-quick up`
otherwise or if the unit came back with no interface, then an `awg syncconf`.

The unit first, and not only for symmetry: systemd runs `ExecStop` only for a
unit it started, so a bring-up that went round it is what leaves the next
`server/stop` with nothing to do.

The sync is not decoration. `awg-quick up` loads the whole config file, and a
disabled peer is still in it on purpose — that is what reserves its address — so
a bring-up hands the tunnel back to every client quota, expiry or an admin took
away. The `PostUp` hook the installer writes undoes this on every path including
boot, but a config from an older install may not carry it.

Idempotent: starting a tunnel that is already up answers `204`.

### `GET api/v1/server/status`

```json
{
  "ifaceUp": true,
  "moduleLoaded": true,
  "toolsVersion": "amneziawg-tools v3.1.20260812 - https://amnezia.org",
  "moduleVersion": "3.1.20260812",
  "features": { "headerRanges": true, "imitationPackets": true,
                "headerProtectionKey": true, "contentPadding": true, "timers": true,
                "randomTrailers": true, "disableCookies": true },
  "serviceActive": "active",
  "serviceEnabled": "enabled",
  "listening": true,
  "warnings": []
}
```

`moduleVersion` is the module the kernel is **running**, read from
`/sys/module/amneziawg/version` — not what `modinfo` reports, which describes
the `.ko` file on disk. The two differ after an upgrade that installed a new
module but could not unload the one in use, which `install.sh` reports and
defers to the next reboot. When they differ, `warnings` carries a sentence
naming both versions: every other value on this page describes the running
module, and a fix that is on disk but not in the kernel has changed nothing
anybody can observe. With nothing loaded, `moduleVersion` falls back to the
installed version, so a stopped tunnel still reports what it would come up on.

---

## Stats

### `GET api/v1/stats/live`

The collector's blob. This route never shells out to `awg` — it reads a file the
collector wrote — so polling it every two seconds costs nothing.

```json
{
  "ts": 1754332800,
  "ifaceUp": true,
  "ifaceSince": 1754246400, "ifaceIndex": 12,
  "online": 2, "total": 5,
  "totalRateRx": 2048, "totalRateTx": 81920,
  "peers": {
    "hE1v...=": { "rateRx": 1024, "rateTx": 40960, "rx": 8123456, "tx": 91234567,
                  "handshake": 1754332788, "lastRx": 1754332799, "status": "online",
                  "endpoint": "203.0.113.9:51234", "online": true }
  },
  "system": { "cpu": 3.4, "memUsed": 812, "memTotal": 3936,
              "memCore": 233472, "memPanel": 187432960,
              "swapUsed": 134217728, "swapTotal": 1073741824,
              "uptime": 98123, "load": [0.1, 0.2, 0.3],
              "diskRead": 0, "diskWrite": 524288, "diskBusy": 1.5,
              "diskUsed": 8589934592, "diskFree": 30064771072,
              "diskTotal": 41072328704,
              "wanRx": 0, "wanTx": 0 },
  "confStamp": "1a2b3c4d5e-4f1-2c9"
}
```

If the collector is not running the blob goes stale; check `ts`.

`?peers=` narrows the peer table to the keys named, comma-separated, and is what
keeps this endpoint the size of a screen rather than the size of the server. The
blob holds one entry per peer the kernel does — the totals beside them are sums
over all of them — but a browser drawing one page of clients needs that page,
and on a server with a few thousand clients the difference is most of a megabyte
every two seconds per open tab. A key the collector is reporting nothing for is
left out rather than answered with a zeroed entry.

At most **150 keys** are honoured. The limit is the request line rather than the
page: a public key is 44 base64 characters and `+`, `/` and `=` all
percent-encode, so a key costs about 51 bytes of URL, and the panel's web server
refuses a request line past 8190 bytes without it ever reaching the application
— gunicorn as a bare `400`, a fronting nginx as `414`. Name more than 150 keys
and the extra ones are ignored rather than rejected —
the response is the first 150. Callers wanting more than that should ask for
every peer instead and narrow the answer themselves; one unnarrowed request is
cheaper than two long ones.

The three states are distinct: **absent** means every peer, which is what the
route has always answered; **present and empty** (`?peers=`) means none of them,
which is what the dashboard and the header indicator ask for since they read
only the totals; **present with keys** means those. Everything outside `peers`
is a fact about the server and is never narrowed.

`lastRx` is when the peer's **receive** counter last moved — the client proving
it is still there. Every config this project issues carries
`PersistentKeepalive`, so a connected client moves it on that interval whether
or not anybody is using the tunnel, and it therefore answers "has this client
gone" far sooner than the handshake does: the kernel only rekeys every couple of
minutes, so `handshake` stays fresh long after the device behind it stopped.

That holds from kernel module `v3.0.20260805` onward, and not before it. Every
profile this project generates sets `S4`, which pads data packets — and a
keepalive is a data packet of length zero, so padding made it a packet of
zeroes. Earlier modules recognised a keepalive only by its length being zero,
so a padded one fell through to the data path, failed the "is this IPv4 or
IPv6" check on its first byte, and was discarded as a malformed packet without
the receive counter ever moving. The effect was a connected but idle client
whose `lastRx` stood still, showing **offline** once it aged past
`onlineThresholdSec` while the tunnel was working the whole time, and a peer
`rx_errors` count that climbed once per keepalive interval. Re-run `install.sh`
to pick the fix up; on a server still running an older module, `online` for an
idle peer falls back to `handshake` and is correspondingly slower to react.
`online` is decided on `lastRx` once there is one, falling back to `handshake`
against `onlineThresholdSec` for a peer the collector has not yet seen send
anything, and on servers whose `clients.env` sets no keepalive at all.

A peer's `status` is that reasoning already done: **online** while its packets
are arriving, **idle** for 90 seconds after they stop — a device changing
networks may be back — and **offline** thereafter. It is served rather than left
to be worked out, because working it out means holding a copy of the thresholds
and a clock to measure them against, and a copy that stops agreeing with this
one is invisible until somebody reports a client that never goes offline.

It covers connectivity only. Whether a peer is disabled, over quota or expired
is not a fact about the connection and is not known here; `status` on a **client
row** is this word with those layered over it, and they outrank it.

`ifaceSince` is when the tunnel came up, in Unix seconds, and `ifaceIndex` is
the kernel index of the netdev that figure belongs to. Both are 0 while the
tunnel is down. Nothing in the kernel records when an interface was created, so
this is the collector's own observation, carried across its restarts through
this file and discarded when the index changes — which is what a `down && up`
does. It is therefore a **floor**: a tunnel that was already up before the
panel was installed reads as young, never as older than it is.

`memUsed` and `memTotal` are **mebibytes** of host memory. `memCore` and
`memPanel` are **bytes**, because the tunnel's own footprint would round to zero
in MiB: `memCore` is the kernel module's code and data (or a userspace tunnel
daemon's resident size, if this installation runs one instead of the module);
`memPanel` is the PSS of the panel's own gunicorn workers and collector, so pages
a forked worker shares with its parent are counted once. Kernel memory that is
not attributable to a module — the packet queues and per-peer structs, which
land in slab caches the kernel merges by object size and bills to whichever
cache of that size was created first — is in neither, so both are lower bounds
and comparable with each other. Neither adds up to `memUsed`. A blob written by
an older collector omits both.

`memCore` is therefore close to a constant: it changes when the module is
rebuilt or reloaded, and not with the client count or with traffic. Repeated
polls returning the same value mean the tunnel's footprint has not moved, not
that the collector has stopped — check `ts` for that.

`swapUsed` and `swapTotal` are **bytes**, read from `/proc/meminfo`. Used is
`SwapTotal - SwapFree`, which is what `free` reports: pages that are in RAM and
on the swap device both (`SwapCached`) are still counted as used, because the
question is how much of the device is spoken for rather than how much of it
would have to be read back. A `swapTotal` of 0 means the machine has **no swap
configured**, which is a different state from an empty swap device and is shown
differently. Both are absent from a blob written by an older collector.

`diskRead` and `diskWrite` are **bytes per second** and `diskBusy` is a
**percentage**, all three measured between the collector's last two polls. They
are rates already, unlike `wanRx` and `wanTx`, because the counters behind them
are since boot and a caller reading this file has no earlier reading to subtract
— the collector is the one process here that does. The first blob after the
collector starts reports zeros for the same reason.

What is added up is whole block devices with hardware behind them, which is
`/sys/block` filtered by the `device` link inside each entry. Partitions are
listed in `/proc/diskstats` beside the disk they are cut from, and stacked
devices — LVM, dm-crypt, md, loop — beside the hardware their writes land on, so
summing the file as it stands would count one write on every layer it passed
through. `diskBusy` is then the **busiest single device**, not the sum: two disks
each half loaded are not one saturated disk. A machine whose block devices this
process cannot see — a container without `/sys/block` — reports zeros. A blob
written by an older collector omits all three.

`diskUsed`, `diskFree` and `diskTotal` are **bytes**, and answer the other disk
question: how full it is, rather than how hard it is working. They come from
`statvfs` on the **root filesystem**, which on the installs this project makes
is where all of it lands — the panel's database under `/var/lib`, its live state
under `/run`, the tunnel's configuration under `/etc`. A box that separates
`/var` onto its own filesystem has a second answer this does not give.

The three do not add up: `diskUsed + diskFree` is normally a little under
`diskTotal`, because a filesystem keeps a reserve only root may write into —
5% of the whole disk on a default ext4. `diskUsed` counts every block that has
been written, `diskFree` counts only what an ordinary process may still write,
and both are as the kernel gives them rather than derived from each other. A
percentage drawn from these should be `used / (used + free)`, which is what
`df` prints; over `diskTotal` it would read a few points kinder than the
filesystem is about to behave. A `diskTotal` of 0 means the filesystem could not
be measured. A blob written by an older collector omits all three.

`confStamp` is the one field here that is not the collector's report: it is
observed when the request is answered, and it changes whenever the server
configuration is rewritten — a client added, removed, or switched off by hand,
by `awg-panel manage` from a shell, or by the collector enforcing a data limit. It is
**opaque**. Compare it with the last one seen and do not parse it; the contract
is that it differs after a change and does not otherwise, not what it is made
of. Empty means there is no configuration to stamp, which is not a change.

It exists because a caller reads this route on one clock and
`GET api/v1/clients` on another. Usage and rates here are two seconds old; a
client's `enabled` and `status` there are as old as that list's own polling. A
client that crosses its data limit loses its key within one collector poll, and
without this a caller has no way to learn that except by asking again on a timer
— which means either a stale row or a list poll fast enough to be wasteful for
an event this rare. Watching `confStamp` is one comparison per poll on an answer
already in flight, and it stays silent on a server where nothing is changing:
the enforcement pass writes the configuration only when something actually
moved.

Deliberately the server config alone, and not the client directory or
`traffic.db`. The collector rewrites `traffic.db` every ten seconds, so a stamp
covering it would say "something changed" on that timer, about figures this blob
is already carrying.

### `GET api/v1/stats/summary`

Totals, online count, today's traffic and uptime, for the dashboard cards.
`todayRx`/`todayTx` are bytes since **UTC** midnight, accumulated by the
collector across every client and written every ten seconds, so they can be a
few seconds behind. They are not affected by a client being deleted: bytes that
moved today moved today. `totalRx`/`totalTx` are all-time, from `traffic.db`
less any per-client usage reset, summed over the clients that still exist —
deleting a client does take its all-time figure with it. `rx` is what clients
uploaded, `tx` what they downloaded.

### `GET api/v1/stats/traffic`

```json
{
  "daily":    [{ "period": "2026-08-13", "rx": 3142857142, "tx": 22000000000 }],
  "monthly":  [{ "period": "2026-08", "rx": 41000000000, "tx": 310000000000 }],
  "earliest": "2025-11-04"
}
```

What the whole server has carried, by day and by month. Left alone, `daily` is
the last 90 days and `monthly` the last 36 months, counting today and this
month, both oldest first — the spans the panel's charts open on, which are
wider than either draws at once because they scroll.

`earliest` is the first day there is a row for, whatever window was asked for,
and `null` on a panel that has recorded nothing yet. Neither series starts
before it: three years back from today is a real date on a server installed last
month, and answering with thirty-four empty columns in front of two bars would
be a chart that reads as broken rather than as new. It is also the floor a date
picker should offer, since asking for anything earlier can only come back empty.

`?from=` and `?to=` name a window instead, as `YYYY-MM-DD`, and either may be
left out: `?from=2026-01-01` runs to today, `?to=2026-03-31` runs back as far as
the answer reaches. **Both** series are cut to it — `daily` gets the days in the
window and `monthly` every month the window touches at all, so one range control
can drive both views of the same question. A date that is not one, or a `from`
later than its `to`, is a 400.

Neither series will exceed what a chart can draw: 400 days and 36 months. A
wider window keeps its newest end and loses the older one, which is why the
range a reader is shown is worth taking from the first and last `period` in the
answer rather than from the request. A window running past today stops at today.

Every period in the window is present, including the ones nothing moved on — a
missing bucket would be a chart that spaced Monday next to Thursday as though
they were consecutive. `period` is a label rather than a timestamp: a month is
not an instant, and a client converting one into its own zone would relabel
August as July west of Greenwich.

The last `daily` entry is the same row `stats/summary` reports as
`todayRx`/`todayTx`, so the card and the chart beside it cannot disagree.
Everything here is cut on **UTC** days, like every other stored moment in the
panel.

### `POST api/v1/stats/traffic/reset`

```json
{ "clients": 12, "serverDays": 274, "clientDays": 3901 }
```

Clears every traffic figure on the server. Takes no body.

The server-wide sibling of `reset-usage`, and it takes the one thing that call
deliberately leaves: the server's own days. What goes is every client's all-time
totals, every client's stored days and months, and the whole of `stats/traffic`
— so `stats/summary` answers zero for `totalRx`, `totalTx`, `todayRx` and
`todayTx`, every client row reads `rxBytes: 0`, and both history endpoints
answer with empty series and a null `earliest`.

**There is no undo and no copy is kept anywhere.** A backup holds these counters
as they stand when it is taken, and `traffic.db` — the file that makes an
all-time total survive a reboot, and which a single client's reset is careful
not to touch — is cleared with the rest. The only record that the figures were
ever anything else is a `panel.traffic-cleared` row in the activity log.

Nothing about the clients themselves is involved: no key, address, quota,
expiry, limit or config file is read or written, and no peer is disconnected. A
client that was switched off for reaching its data limit is switched back on by
the collector's next enforcement pass, which finds nothing left to enforce —
within one `enforceIntervalSec`, and sooner in practice, because the pass is
booked for the cycle after the wipe completes.

`clients` counts the clients that had something to clear rather than every
client on the server; a peer that had never moved a byte is not in it.
`serverDays` and `clientDays` are the stored rows this call deleted.

It finishes in two places, which matters only if you are watching the file
rather than the API. This request stores an offset against every client and
empties the tables; the collector subtracts those same offsets out of
`traffic.db` itself on its next flush, within ten seconds. Every figure the API
reports is zero throughout — before the fold because the offset covers the
counter, after it because both are zero — so there is no window in which a
caller sees the old numbers come back. Bytes that cross the tunnel in between
are counted rather than lost: they moved after the wipe. A wipe asked for while
the collector is stopped is applied when it next starts.

Available to an API token, unlike `DELETE api/v1/events`. The rule there is that
a token must not be able to erase the record of what it did, and this cannot:
the record is the row it writes to that log, and the log is not what this
empties.

### `GET api/v1/clients/<name>/traffic`

The same two series, the same shape and the same `from`/`to` parameters, for one
client. Under `clients/` rather than `stats/` because it is addressed the way
every other per-client route is — by the name in the config, not by a public key
that would need percent-encoding to survive a path segment.

Its window stops where retention does, and this is the one place the two routes
answer differently. Ask the server for 36 months and you get 36; ask a client
and you get the thirteen its rows are kept for, starting at the first whole
month inside them. The months in between are not empty here, they are gone, and
a zero that means "swept" is indistinguishable once drawn from one that means
"quiet".

`earliest` is this client's first stored day, and cuts the window the same way
it does for the server — so a client added in June opens on June rather than on
thirteen months of which nine could only ever be empty. The two floors are not
the same rule: the swept one starts at the first *whole* month, because a month
half of which has been pruned is a short bar for a reason that is not traffic,
while the first recorded month is included as it stands, because a client that
joined on the 20th really did carry what it carried that June.

It can legitimately come out under the client's all-time `rxBytes`/`txBytes`: a
client older than the retention window has moved more than its history can
account for. A `reset-usage` call takes both, so the two go to zero together.

A client's history is deleted with the client, and follows a key rotation rather
than being orphaned by one. Per-client rows are swept after 400 days — thirteen
months plus a margin, which is what keeps the oldest column of its monthly chart
whole. The server's own daily rows are one a day whatever the client count and
are never swept.

This is not the endpoint that was removed. `GET api/v1/stats/client/<name>` read
per-client sample and hourly tables that cost a row per connected client every
ten seconds; it had no caller and both tables went with it. What is stored now is
one row per client per **day**, rewritten in place, so a client transferring all
afternoon is one row rather than three thousand.

---

## Events and logs

Two logs, kept apart because they are made of different things. `events` is the
panel's own ledger: a row written by the code that performed an action, stored
in the database beside everything else and surviving a restart, a rotation and a
reboot. `logs` is a window on journald — nothing is stored, and it answers with
whatever the services have written.

### `GET api/v1/events`

```json
{
  "events": [
    {
      "id": 412,
      "at": "2026-08-16T09:14:02Z",
      "kind": "client.quota-reached",
      "severity": "warning",
      "actor": "",
      "actorIp": "",
      "target": "phone",
      "detail": {}
    },
    {
      "id": 411,
      "at": "2026-08-16T09:02:55Z",
      "kind": "client.updated",
      "severity": "info",
      "actor": "admin",
      "actorIp": "203.0.113.10",
      "target": "phone",
      "detail": { "fields": ["quotaBytes"] }
    }
  ],
  "total": 412,
  "page": 1,
  "pageSize": 25
}
```

Newest first. `kind` is `<category>.<what-happened>` and is the whole of what an
event means: no sentence is stored, because a sentence written on the server is
a sentence in one language, and the panel ships two — the browser assembles the
line from the kind and `detail`. A client reading this should show a kind it
does not recognise as its own name rather than dropping the row, which is what
keeps an older client readable against a database written by a newer panel.

`actor` is the account that did it and is **empty for anything the panel decided
on its own** — a client switched off for its quota was switched off by nobody.
That is a value rather than a gap. For a request authenticated by an API token it
is `api:` followed by the token's name — every token on a panel authenticates as
the same single account, so recording that account would make the log say `admin`
for work nobody was present for. A colon is not allowed in a username, so the
prefix is a marker and never an ambiguity. `actorIp` is empty for events the
panel decided on its own, and for a request whose address could not be read;
`X-Forwarded-For` is honoured only where a proxy has been declared, by the same
rule as the session list.

`detail` is the one object in this API whose keys are **not** renamed, because
they are data rather than field names. They are single words, and what may be in
them is a string, a number, a boolean or a flat list of those — never key
material, never a password. What each kind carries:

| Kind | `detail` | Severity |
|---|---|---|
| `client.created`, `client.enabled`, `client.disabled`, `client.deleted`, `client.keys-reset`, `client.usage-reset`, `client.restored`, `client.quota-reached`, `client.expired` | — | `client.deleted`, `client.quota-reached` and `client.expired` are warnings |
| `client.updated` | `fields` — the changed fields, in the API's own spelling | info |
| `client.renamed` | `name` — what it was called before | info |
| `client.bulk-deleted` | `count`, `expired`, `disabled` | warning |
| `client.bulk-limited` | `count`, `down`, `up` | info |
| `client.exported` | `count` | info |
| `server.saved` | `restart`, `reimport`, `applied` | info |
| `server.restarted`, `server.stopped`, `server.started`, `server.iface-down`, `server.iface-up` | — (`target` is the interface) | restart, stop and iface-down are warnings |
| `panel.settings-saved` | `count`, `keys` — everything that changed — and `values` as `key=value` strings, for the settings whose value says nothing about how to reach this panel. The secret base path, the listen address, the port, the two certificate paths and the endpoint domain are recorded as having changed and never as what they changed to | info |
| `panel.backup-created` | — | info |
| `panel.restored` | `count`, `parts` | warning |
| `panel.events-cleared` | `count` — how many rows the clear took | warning |
| `panel.traffic-cleared` | `count` — clients whose figures went; `days` — server days deleted | warning |
| `auth.signed-in` | `totp` | info |
| `auth.sign-in-failed`, `auth.locked-out` | `name` — what was typed, which is a claim — plus `tries` and `limit`, which attempt this was and how many the address gets, so a run of them reads as a countdown | warning |
| `auth.signed-out`, `auth.password-changed`, `auth.totp-enabled`, `auth.totp-disabled` | — | info |
| `auth.session-revoked` | `browser` and `platform`, either of them `""` for an agent that said nothing recognisable (`target` is the address it was signed in from). The row it was chosen from is deleted a moment earlier, so this is the only description of it that survives | info |
| `auth.username-changed` | `name` — what the account was called before (`target` is the new name) | info |
| `auth.sessions-revoked` | `count` | info |
| `auth.token-created` | `seconds` — the lifetime it was given, `0` for none — and `renew` (`target` is the token's name) | info |
| `auth.token-updated` | `renew`, and `name` — what it was called before, `""` when it was not renamed | info |
| `auth.token-revoked` | — (`target` is the token's name) | info |

`server.stopped` and `server.iface-down` are not the same event and are kept
apart deliberately. The second is the collector noticing; the first is somebody
deciding. A tunnel that is down because it was stopped on purpose and one that
is down because it fell over look identical from the outside, and that is the
question asked in front of this log afterwards.

A bulk operation is **one** row with a count, not one per client: a sweep of
five hundred would otherwise bury every other event of that afternoon, and the
table has a ceiling.

Query parameters, all optional and none of them able to fail a request — a stale
bookmark gets the newest page rather than a 400:

| Parameter | Meaning |
|---|---|
| `category` | `client`, `server`, `panel` or `auth`; the half of `kind` in front of the dot |
| `severity` | `info` or `warning` |
| `q` | substring of the actor or the target |
| `page` | 1-based |
| `pageSize` | 1–200, default 25 |

**Retention.** Events are swept after 180 days by the collector's daily pass,
and a ceiling of 20 000 rows is applied after that as a backstop — the age window
bounds an ordinary panel, and the ceiling bounds one that is being flooded with
refused sign-ins. Neither is a setting.

A refused sign-in is recorded up to and including the attempt that locks the
address out, and nothing after it: an address inside its cool-off can keep
knocking for as long as it likes, and a row per knock would be a way to fill a
disk. django-axes keeps that count, and the journal below has a line for each.

### `DELETE api/v1/events`

```json
{ "removed": 412 }
```

Empties the table and answers with how many rows went. **Sign-in only** — a
`403` for an API token, by the same argument as the credential routes one step
earlier: a token is handed out expecting to be taken back, and that only works
while the panel can be asked afterwards what it did. A token that could empty
this could erase what it had done. It is not a wall — the same rows leave in
every backup a token *is* allowed to fetch — but it keeps the ledger out of the
ordinary reach of a script, so a cleared log is something somebody chose.

The whole table, never the filtered set: the query string is ignored here. A
clear whose effect depended on a `category` or a `q` three fields away would be
the one operation in the panel that does something different to the operator who
was searching it, and the one they would discover afterwards.

One row is written straight after the delete — `panel.events-cleared`, carrying
the count, the account and the address. So a cleared log is a log with a single
line in it rather than a blank one, which is the difference between tidying up
and an audit trail that can be made to have never existed. It is also the row
the next clear takes, so this cannot accumulate.

An already-empty table answers `{"removed": 0}` and writes nothing, by the same
rule as a client sweep that removed nothing. Nothing else is touched: the
journal below belongs to journald, and the traffic history is measurements
rather than a ledger.

### `GET api/v1/logs`

```json
{
  "source": "collector",
  "unit": "awg-panel-collector",
  "lines": [
    { "at": "2026-08-16T09:14:02Z", "priority": 4, "message": "client phone disabled: it has used its whole quota" }
  ],
  "available": true,
  "reason": ""
}
```

The last few lines one service wrote to the journal, **oldest first** — it is a
transcript, and reversing it would scramble every multi-line traceback in it.
The same text `sudo awg-panel logs` prints.

| Parameter | Meaning |
|---|---|
| `source` | `panel`, `collector` or `tunnel`; anything else answers `available: false` |
| `lines` | 1–1000, default 200 |

`source` names a service and never a unit. The systemd unit is worked out on the
server — `awg-panel-web`, `awg-panel-collector` and `awg-quick@<iface>` — so no
unit name from a query string reaches `journalctl`, and the tunnel's unit is
per-interface anyway.

`priority` is the syslog level, 0 (emergency) to 7 (debug); 3 and below is an
error, 4 a warning. `at` is `null` for an entry whose timestamp could not be
read.

`available: false` with a `reason` is the answer where there is no journal to
read at all — a container without systemd, a host that has replaced it, a
development tree. That is a state and not a failure, so it is a `200`: a 500 on
this route would read as the panel being broken at the moment somebody came to
find out what was.

---

## Panel settings

### `GET api/v1/settings` / `PUT api/v1/settings`

The keys in [PANEL.md](PANEL.md#settings). Changing `webListen`, `webPort`,
`webBasePath` or the TLS paths rewrites `/etc/awg-panel.env` and restarts the
web service about two seconds later, so this response still reaches the browser.
`url` is the whole address to reconnect at - scheme, host, port and the base
path the panel is mounted under - and every warning that sends you somewhere
quotes that same string rather than describing it.

The host in it is not always the one you asked on. A pinned `webListen` wins
over everything, since that is where the service will be. Otherwise turning TLS
on answers with the name on the certificate, because the address a panel is
administered at is usually one no certificate covers; turning it off answers
with the server's own address from `clients.env`, because the name has a year of
HSTS on it and this browser would silently turn `http://` back into `https://`.
Both fall back to the host in the request when there is nothing better to give.

### `GET api/v1/settings/certificate[?path=/etc/ssl/certs/panel.crt]`

```json
{ "path": "/etc/ssl/certs/panel.crt", "names": ["vpn.example.com", "*.example.com"], "domain": "vpn.example.com" }
```

What the certificate at `tlsCertPath` covers, read off the file rather than out
of the settings — a renewal changes the names without anything being saved.
`domain` is the name the panel would write into client configs: the first that
is neither a wildcard nor an address. A certificate issued for an address is
therefore listed in `names` and answers `""` here — the address on it is the one
the configs already carry, so there is nothing it could swap in. No certificate,
or one that cannot be parsed, answers with empty strings and an empty list
rather than an error.

`path` asks about a certificate that has been typed and not saved, which is how
the Settings page knows where it will reconnect before the save that moves it
there. A relative path, one there is no file at, and one that is not a
certificate all answer the same way as no certificate at all. The `path` in the
answer is the file it is about — empty when the request named one that could
never be loaded — so a caller can tell a reply about the certificate it is
replacing from a reply about the one it is installing.

### `POST api/v1/settings/account`

```json
{ "current": "...", "username": "...", "new": "..." }
```

The account's own credentials: `current` is the password proving who is asking,
`username` renames the account and `new` replaces the password. Either of the
last two may be left out or sent empty, meaning "leave that half alone"; a
request that asks for neither is a 400. A name already held by another account
is a 400 on `username`, and so is one outside `[A-Za-z0-9][A-Za-z0-9._@+-]*`.

The answer is the `auth/session` payload, so a rename comes back with the name
the panel now knows. Other sessions are invalidated by either change, and the
caller's own survives both.

Locked out afterwards, the way back is still a shell on the server: `sudo
awg-panel passwd` finds the account whatever it is now called.

### `POST api/v1/settings/2fa/enable`

Called twice. First without a body: returns `{ "secret": "...",
"provisioningUri": "otpauth://...", "qr": "data:image/png;base64,..." }`. Then
with `{ "code": "123456" }` to confirm. A lost device is recovered from the
server shell with `sudo awg-panel reset-2fa`.

### `POST api/v1/settings/2fa/disable`

Requires the current password.

### `GET api/v1/settings/sessions`

Every browser holding a live session for the signed-in account, the caller's own
first.

```json
{
  "sessions": [
    {
      "id": "0d6b6c3e-6f0e-4a8e-9a3e-6b1f2c5d4e7a",
      "current": true,
      "createdAt": "2026-08-05T09:14:02Z",
      "lastSeenAt": "2026-08-05T11:47:00Z",
      "expiresAt": "2026-08-06T11:47:00Z",
      "ip": "203.0.113.10",
      "browser": "Chrome",
      "platform": "Windows",
      "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ..."
    }
  ]
}
```

`id` is minted for this listing; the session key itself is never sent, because
it is the value in that browser's cookie. `browser` and `platform` are read out
of `userAgent` and are both `""` when it says nothing recognisable — a guess,
which is why the raw header comes with them. `ip` is `""` when the address could
not be read, and `X-Forwarded-For` is only believed when `AWG_PANEL_TRUST_PROXY`
says there is a proxy, exactly as for the login lockout. `lastSeenAt` is written
at most once a minute, so it is accurate to the minute and no finer.

Sessions that have expired or been ended elsewhere are absent, and their rows
are cleaned up as the list is built.

### `DELETE api/v1/settings/sessions/<id>`

Ends that session: the session itself is deleted, so the browser holding it is
anonymous on its next request. `204` on success, `404` for an id this account
does not own, and `400` for the caller's own session — use `auth/logout` for
that, which is what leaves this browser knowing it has been signed out.

### `POST api/v1/settings/sessions/revoke-others`

Ends every session of this account except the caller's own.

```json
{ "ended": 2 }
```

### `GET api/v1/settings/tokens`

Every API token this account holds, newest first. Expired ones are included:
the row is the answer to "why did my deployment start failing on Tuesday", and
one that vanished the moment it stopped working would leave nothing to read.

```json
{
  "tokens": [
    {
      "id": "9f1c6b2a-2d4e-4d7f-8a1b-3c5e7f9a0b2d",
      "name": "nightly backup",
      "hint": "x7Qa",
      "createdAt": "2026-08-05T09:14:02Z",
      "expiresAt": "2026-09-04T09:14:02Z",
      "expiresIn": 2592000,
      "renewOnUse": true,
      "lastUsedAt": "2026-08-17T03:00:11Z",
      "lastUsedIp": "203.0.113.10",
      "expired": false
    }
  ]
}
```

The secret is not here and cannot be: the panel stores a SHA-256 of it, so a
token that was not copied out of the create response is replaced rather than
recovered. `hint` is its last four characters, which is enough to match a row
against the copy in a CI configuration and nothing an attacker can use.

`expiresAt` and `expiresIn` are both `null` for a token that never expires.
`expiresIn` is the window in seconds, which is what `renewOnUse` restores on
each use — not what is left of it. `lastUsedAt` is written at most once a
minute, so it is accurate to the minute and no finer; the address is written the
moment it changes. `expired` is decided against the server's clock, which is the
clock the token is actually checked by.

### `POST api/v1/settings/tokens`

```json
{ "name": "nightly backup", "expiresIn": 2592000, "renewOnUse": false }
```

`expiresIn` is seconds: at least 300, at most 3650 days, and `0` (or omitted)
for a token that never expires. `renewOnUse` puts the expiry back to the full
window on every use, which needs an expiry to work on — asking for it with
`expiresIn: 0` is a `400` rather than a quiet `201`. A name another token
already holds is a `409`, case and all.

`name` left out or sent blank has the server draw one, the same nine characters
a client gets and checked the same way against the names this account is already
using. A token is never nameless: the activity log writes the name beside
everything the token does, and a row that says nothing about which credential
did the work is the one thing that list must not contain.

`201`, and the only response in this API that carries a secret:

```json
{ "token": { "id": "...", "name": "nightly backup", "...": "as above" },
  "secret": "awgp_S3cr3t..." }
```

Send it as `Authorization: Bearer awgp_...`. It is shown once.

### `PUT api/v1/settings/tokens/<id>`

```json
{ "name": "ci", "renewOnUse": true }
```

Both fields are optional and a request with neither is a `400`. The answer is
the token row. The secret is untouched, so whatever holds it goes on working.

Deliberately the whole of what may be changed: rotating the secret is a new
token and an old one to revoke, and the expiry cannot be pushed out by hand,
because an end date that can be moved whenever it approaches is not one.

### `DELETE api/v1/settings/tokens/<id>`

Revokes it. `204`, and the next request presenting that token is anonymous. The
row is deleted rather than flagged — a row kept for the record is a hash that
still matches a secret somebody holds. `404` for an id this account does not
own.

### `GET api/v1/backup`

A `.tar.gz` of `/etc/amnezia/amneziawg` plus `/var/lib/awg-panel`. The layout is
fixed rather than convenient: it is the one `awg-menu` wrote before backups
moved into the panel, so an archive taken off a server years ago still restores
here. **It contains the server private key and every client private key.**

It also contains `db.sqlite3` and `secret.key`, and that is what to understand
before a token is pointed at this route. The database holds the session table,
and a session key *is* the `awgsessionid` cookie rather than a hash of it — so
**whatever downloads an archive can sign in as the account**, whichever routes it
is refused elsewhere. This one is open to a token all the same: a nightly copy
off the box is worth having, and the archive already carries every key on the
server. What makes that bearable is that it is recorded — the
`panel.backup-created` row names the token that asked — so a token that has ever
fetched one should be treated like the password rather than revoked and
forgotten.

### `POST api/v1/restore`

`multipart/form-data` with the archive. Rejected unless it contains
`amneziawg/<iface>.conf`. Stops the interface, extracts, restarts, and reports
what was restored. The current configuration and every client are replaced.

Session only: an API token gets a `403` here, because the archive replaces
`db.sqlite3` and with it the password hash and the token table.

### `POST api/v1/update/check`

Compares `VERSION` against the newest GitHub release. Never fails on a network
error; returns `{"checked": false, "reason": "..."}` instead, because a server
that cannot reach GitHub is not a server that is broken.

Only stable releases count. A draft, a release the feed flags as a pre-release,
and a tag carrying a pre-release suffix are all reported with
`updateAvailable: false` and a reason saying so.

A `POST` for what is plainly a read, which is worth defending once: it reaches
out to a third party and takes seconds over a slow link, and a `GET` is what a
browser prefetches, a proxy caches and a query client re-runs on every window
focus. Making it a mutation is what keeps the check something an operator asked
for.

### `GET api/v1/update/status`

Where a running or finished update got to. `status` is one of `idle`, `running`,
`succeeded`, `failed` and is the only field worth branching on — `phase` names
the current step for a progress line and gains new values as the updater does.
`log` is the tail of the update's own log, `backup` the archive taken before
anything was replaced, and `unavailable` a sentence, non-empty only on a panel
with no updater installed, to show instead of an update button.

Read straight off the file `awg-update` writes, which is what makes it survive
its own subject: an update restarts this service partway through, so a poller
fails for a few seconds and then reads the rest of the story out of the same
file.

Open to a token. Watching a deployment is a fair thing for the script that
started it.

### `POST api/v1/update/apply`

Installs the newest stable release: a full backup, then the release bundle
downloaded and checked against its published `SHA256SUMS`, then the installer.
Config, keys, clients, accounts and traffic history are kept — it is the same
upgrade as re-running the installer by hand. `{"force": true}` reinstalls the
newest release even when it is the one already there.

Answers as soon as the updater is running, **not** when it has finished, and
cannot do otherwise: this request is served by the service the update restarts.
Poll `update/status` from there. The reply is that endpoint's shape.

`409` when an update is already running, `400` when the server has no updater
installed.

Session only, and for a wider reason than everything else on that list. The
others are about the credentials themselves; this replaces every line of code on
the server from a release fetched over the internet, so a leaked token that could
press it would not merely read the panel's data — it would decide which software
the box runs next.

[docs/UPDATES.md](UPDATES.md) covers the whole mechanism: the phases, what is
verified before anything is executed, and what to do when an update goes wrong.

---

## `GET api/v1/health`

The only unauthenticated route. `{"ok": true, "version": "1.0.0"}` — used by the
Docker healthcheck and by the settings page while waiting for a restarted
service to come back.

## `GET api/v1/openapi.json`

The whole API as an OpenAPI 3.1 document, served by the panel it describes.

This file is prose for a person; that one is the same thing for a tool. Import
it into Postman, Insomnia or Bruno, or point a client generator at it: `servers`
carries this panel's own address, base path and all, built from the request, so
an import needs no editing and works under a secret prefix or behind a proxy.
The panel's own API page is rendered from it, which is what keeps the reference
an admin reads and the document a script is generated from the same thing.

Authenticated like everything else, and **open to a token** — the caller most
likely to want it is a script being written against this panel. It describes
routes rather than this server, so a token reading it learns nothing it could
not learn by trying them.

There are no request or response schemas, only examples and prose. A schema per
body would be a second declaration of every serializer in the panel, maintained
by hand, and the first one to fall behind would be worse than none at all: a
generated client that compiles and is wrong.

The catalog behind it is `panel/apps/panel/apidocs.py`, and
`panel/tests/test_api_docs.py` walks the URL map against it in both directions —
a route added without an entry fails the suite, and so does an entry left behind
by a route that was removed.
