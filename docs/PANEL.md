# AWG Panel

The management interface for an AmneziaWG server installed by this repo's
`install.sh`, and the only writer of `/etc/amnezia/amneziawg`. Clients, quotas,
expiry, obfuscation and traffic are all here; the shell tools that remain
(`awg-panel`, `awg-menu`) cover the panel's own service and the box it runs on.

Those files are still ordinary files and still the truth about the server —
`awg-quick` reads `awg0.conf` at boot with nothing else running, which is why
the tunnel comes up whether or not the panel does.

- [What it is](#what-it-is)
- [Install, upgrade, remove](#install-upgrade-remove)
- [Updating](#updating)
- [How it owns the config files](#how-it-owns-the-config-files)
- [Disabled clients, quotas and expiry](#disabled-clients-quotas-and-expiry)
- [Settings](#settings)
- [TLS and reverse proxies](#tls-and-reverse-proxies)
- [Docker](#docker)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

## What it is

Two systemd services and a static SPA:

| | |
|---|---|
| `awg-panel-web` | gunicorn serving a Django + DRF API and the built React app |
| `awg-panel-collector` | polls the interface every 2 s, accumulates traffic, enforces quotas and expiry on that same poll |

Pages: a live dashboard (online peers, throughput chart, CPU/RAM, today's and
all-time traffic), client management with QR codes and per-client quotas and
expiry dates, the interface's own network settings, an obfuscation editor that
explains every parameter in plain language and draws whole sets from a chosen
profile, statistics and logs (traffic by day and by month over a range you
choose, the busiest clients, everything that has been done to this server, and
what its services have written to the journal), panel settings including 2FA,
TLS, backup/restore and an update check, and [the API](#the-api-page) — a token,
a first request against this installation's own address, and a searchable
reference to every endpoint the panel serves. Two more sit under those in the
rail: **About**, which reports what this installation is actually running — the
panel's version, the module's and the tools', which obfuscation features that
build supports, and where every file it owns lives — and **Support**, which is
the donation page and sits at the foot of the rail rather than in the list, so
an ask for money is not read as one of the places to do the job.

The split between the first of those and the fifth is deliberate: the dashboard
is what the tunnel is doing now and is meant to be legible in one look, while
anything that has to be scrolled through or asked a question of is on the
statistics page.

### The sidebar has an edge you can drag

The navigation rail is not a fixed 16rem any more. The border between it and
the page follows the pointer, between 14rem and 25rem, and the page moves with
it rather than after it — there is no moment where the rail is over the content
instead of beside it. Drag it well past the narrow end and it goes away
altogether, giving the page the whole width of the window; the caret at the
left of the top bar brings it back, at the width it had. That caret is also
there when the rail is showing, and puts it away in one press for anyone who
would rather not drag anything at all.

Double-clicking the edge returns it to the width it ships at. The edge can be
tabbed to as well, and then moved with the arrow keys, or sent to either end
with Home and End.

Both the width and whether the rail is showing are remembered by the browser
they were set in — like the theme, the language and the rows-per-page choice,
they describe the screen the panel is being read on rather than the server
being administered, so neither is sent to the API. None of it applies below
768px, where there is no rail to size: that layout has the menu button and the
drawer it opens, as it did before.

### What the dashboard's memory split means

The System load card shows two figures side by side: what the tunnel costs, and
what the panel that manages it costs. They are read from `/proc` and `/sys`, and
what each counts is worth knowing before acting on it.

| | Counted | Not counted |
|---|---|---|
| **VPN core** | the kernel module's code and data (`/sys/module/amneziawg/coresize`), or a userspace tunnel daemon if one is running instead of the module | everything the module allocates while running — packet queues, per-peer structs, one route node per allowed IP — which the kernel does not attribute to any module |
| **Panel** | PSS of the gunicorn workers and the collector, so pages a forked worker shares with its parent are counted once rather than per worker | nothing it does not own |

Both are lower bounds, which is what makes comparing them fair, and neither is a
slice of the memory bar above them. The comparison is normally lopsided by three
orders of magnitude — a few hundred kilobytes of kernel module against a couple
of hundred megabytes of Python — which is the honest answer to "what is using
the RAM on this box" and the reason `--standalone` and a localhost-only panel
are supported at all.

**VPN core does not move, and that is the correct reading.** It is the module's
compiled size, so it changes when the module is rebuilt or reloaded and at no
other time — adding clients or pushing traffic will not shift it. The part that
does grow is unmeasurable from userspace rather than merely unmeasured: those
allocations come from slab caches, and SLUB merges caches by object size, so the
tunnel's `allowedips_node` and `wg_peer` caches are billed to a root cache named
for its size. `/proc/slabinfo` lists only that root cache; the tunnel's own names
survive as symlinks under `/sys/kernel/slab` and nowhere a total can be read
from. The panel counts what it can name and leaves the rest out rather than
estimating it.

### Disk space and swap, on the same card

Below the memory figures the card carries the two readings that say whether the
box is about to stop working rather than merely get slow.

**Disk space** is the root filesystem, measured with `statvfs`: used, free, and
the size of the disk. It is the root one because that is where everything this
project installs ends up — the database under `/var/lib`, the live state under
`/run`, the tunnel's configuration under `/etc` — and a server that fills it up
stops recording traffic and stops accepting configuration changes while every
other figure on the page still looks healthy. Used and free do not add up to the
total, and nothing is wrong with that: a filesystem keeps a reserve only root
may write into, 5% of the disk on a default ext4. The bar is used over
used-plus-free, so it reads the same as `df` rather than a few points kinder.

**Swap** is the disk being used as memory the machine did not have. Some
swapping is ordinary and free — the kernel will page out something untouched
for a week and be right to — but a swap area filling up on a small server is
the last quiet warning before the OOM killer starts choosing processes. A
machine with no swap says **None** rather than showing an empty bar, which would
read as room to spare when there is none.

| Piece | Path |
|---|---|
| Code | `/opt/awg-panel` |
| Virtualenv | `/opt/awg-panel/.venv` |
| Database, master key | `/var/lib/awg-panel` (0700) |
| Live state | `/run/awg-panel` (0700, tmpfs — see below) |
| Port, bind address, base path, TLS | `/etc/awg-panel.env` (0600) |
| Units | `/etc/systemd/system/awg-panel-{web,collector}.service` |
| Control CLI | `/usr/local/bin/awg-panel` |

## Install, upgrade, remove

```bash
# installed with the server; there is no flag to skip it
sudo ./install.sh

# put it back on a server that has somehow lost it
sudo panel/install-panel.sh

# on a different port, bound to localhost only (put a proxy in front)
sudo panel/install-panel.sh --port 9000 --listen 127.0.0.1

# upgrade to the newest release, with a backup taken first. Also the
# panel's Settings > Updates tab, and awg-menu > Update. See "Updating".
sudo awg-update apply

# the same upgrade by hand: same command again. Code and dependencies are
# refreshed; the database, accounts and settings are kept.
sudo ./install.sh

# removes all of it: tunnel, module, tools, panel, certificate,
# configuration and data. Take a backup first if you still need it.
sudo awg-uninstall

# same, but keeps /etc/amnezia/amneziawg, /var/lib/awg-panel and the
# certificate, so a later install.sh picks the server back up
sudo awg-uninstall --keep-config
```

`awg-uninstall` deletes the server key, every client's key, the panel database
and the traffic history, and none of it is kept anywhere else — every config
already handed out stops working. It says so and asks before it starts, and the
menu asks once more before it gets that far, but the backup has to be taken
while the panel is still running: **Settings → Backup**, or `GET
api/v1/backup`.

Removing the panel on its own is refused while a tunnel is present. It is the
only thing that can revoke a client, and the config's hooks call it on every
bring-up, so a server without it has clients that cannot be switched off and
revocations that come undone at the next boot.

The first install prints the URL, a random 10-character username and a
generated 16-character password **once**. Write both down; only the Argon2 hash
is stored. Lost them:

```bash
sudo awg-panel passwd            # interactive; finds the account itself
sudo awg-panel passwd someone    # when the panel has more than one
```

The name is drawn per install rather than being `admin` on every one of them,
because a constant username is half a credential that never has to be guessed.
The installer offers to let you pick it, `install-panel.sh --admin-user` sets it
outright, and Settings › **Authentication** renames it afterwards — which is why
the first form above asks the panel what the account is called rather than
assuming.

Day-to-day control:

```bash
sudo awg-panel status            # services, URL, version, listening sockets
sudo awg-panel url               # just the login URL
sudo awg-panel logs -n 200
sudo awg-panel restart | stop | start | enable | disable
sudo awg-panel reset-2fa         # when a TOTP device is lost
sudo awg-panel manage <cmd>      # any manage.py command, in the venv
```

Everything above is also reachable from `sudo awg-menu` → **Web panel**.

## Updating

The panel updates the whole installation, itself included, from the project's
GitHub releases. **Settings → Updates** checks for a newer stable release, shows
what changed, and installs it on one press: a full backup first, then the
release bundle downloaded and checked against its published `SHA256SUMS`, then
the installer. `sudo awg-menu` → **Update** is the same operation from a
terminal, and so is `sudo awg-update apply`.

```bash
sudo awg-update check            # is there a newer stable release
sudo awg-update apply            # install it, printing the log as it happens
sudo awg-update status           # where a running or finished update got to
sudo awg-update log -n 400
```

The work runs in its own transient systemd unit, so neither closing the browser
nor dropping the SSH session stops it — and the update restarts `awg-panel-web`
partway through, which is exactly why it cannot be a child of the request that
asked for it. The panel's page rides that restart out and picks the log back up
when the service answers again.

Everything is kept: the server key, every client, the panel's port and secret
path, its certificate, the account, quotas and the whole traffic history. The
backup taken on the way in lands in `/var/lib/awg-panel/backups/` and is the same
archive **Settings → Backup** produces. Only stable releases are offered — a tag
with a pre-release suffix is never installed.

Which repository is checked is `AWG_PANEL_UPDATE_REPO` in
`/etc/awg-panel.env`; setting it to something that is not a repository turns the
check off. **[UPDATES.md](UPDATES.md)** covers all of it — the phases, what is
verified before anything is run, forks and offline mirrors, and what to do when
an update goes wrong.

### What the installer touches

Only these: `/opt/awg-panel`, `/var/lib/awg-panel`, `/etc/awg-panel.env`, the
two unit files, `/usr/local/bin/awg-panel`, `/usr/local/bin/awg-update`, and one
firewall rule for the panel's TCP port. It installs `python3-venv`, `python3-dev` and a compiler if they are
missing. It never modifies `awg0.conf`, the tunnel, or the DKMS module — the
four hooks that call back into the panel (`manage trafficsync` on `PreDown`,
`manage enforce` and `manage shape` on `PostUp`, `manage shape --detach` on
`PostDown`) are written by `install.sh`, not by this script.

### Node is not needed on the server

If `panel/frontend/dist` exists it is used as-is. That is what the release
tarball ships and what `./build.sh --with-panel` bundles. Only when the directory
is missing does the installer build the UI on the server, installing Node first
if necessary.

That fallback is the one memory-hungry step in the whole install, and a 512 MB
or 1 GB VPS — exactly the kind a VPN runs on — will have the bundler killed by
the OOM reaper. The installer checks RAM plus swap first and warns, then caps
Node's heap so the failure is a readable error rather than a dead box. Avoid the
situation entirely by building where you have memory:

```bash
make -C panel build      # -> panel/frontend/dist
./build.sh --with-panel  # -> dist/awg-panel.sh, one file, UI included
```

## How it owns the config files

The panel writes `/etc/amnezia/amneziawg` and nothing else does. It is still not
a system of record that happens to write configs — the files remain the truth,
readable and hand-editable, and the kernel takes them directly.

```
                     /etc/amnezia/amneziawg/
                       awg0.conf         <---- awg-panel-web
   awg-quick   read -> clients/*.conf    <---- awg-panel-collector
   (boot)              clients.env       <---- awg-panel manage ...
                       traffic.db              (hooks + a root shell)
                       .lock  (one flock, shared by every panel process)
```

- **The config files are the source of truth**, and the panel is the only thing
  that writes them. That is what makes a quota or an expiry mean something:
  there is no second tool whose edit could undo one silently. The kernel and
  `awg-quick` still read them directly, so the tunnel does not depend on the
  panel being up.
- **The panel keeps an indexed copy of them, and it is only ever a copy.** Every
  request stats `awg0.conf`, the `clients/` directory and `traffic.db`, and
  rebuilds the copy if any of them has moved since it was last built — so the
  panel reads the files once per *change* rather than once per request, which is
  what lets a page of fifty clients cost the same on a server holding four
  thousand. A change the panel made itself skips even that: adding, removing,
  renaming, disabling or re-scoping a client updates the row directly, since a
  config the panel just wrote is one it has already parsed. The same goes for
  usage, which is the one figure that moves without anybody editing anything:
  the collector writes `traffic.db` every ten seconds and hands over the peers
  whose totals changed as it does, so the copy is updated rather than re-derived
  by whichever request notices next. It is re-derived when somebody *else* wrote
  that file — the `PreDown` hook catching the counters on the way down, a
  restore putting an older one back — which the stamps catch exactly as they
  catch a config edit. Nothing is stored
  there that is not recomputable from those files: the tables can be dropped at
  any moment and cost one rebuild, and the first read after a restart rebuilds
  regardless. The one thing this cannot see is a client config rewritten in
  place by a text editor, which changes no directory entry; that client's routes
  are stale until anything else changes.

  What this costs is measured rather than asserted — see [Numbers, on four
  thousand clients](#numbers-on-four-thousand-clients) below.
- **One lock.** One writer is not one process: two gunicorn workers, several
  threads each, and the collector all mutate these files. Every mutation takes
  an exclusive `flock` on `.lock` with a 30-second timeout, so two concurrent
  creations can never be handed the same address. The file is the one the bash
  tools used to take, kept rather than renamed because `awg-quick` hooks and
  anything else left on a box from before still expect it there.
- **Stable formats.** The peer block, the client config layout with its
  parameter order, and the five-column `traffic.db` are all asserted against
  fixtures captured from a real server, because the files outlive the panel that
  wrote them: `awg-quick` reads the server config at boot, an admin reads the
  client configs by hand, and a restore puts an old `traffic.db` back.
- **The panel's database holds only what a config file cannot express**: the
  admin account, per-client quotas, expiry dates, notes and email, and traffic
  history. It is keyed by public key rather than by name, so a rename keeps its
  metadata — including a rename made in the config file by hand. The indexed copy above is the one thing in there that
  duplicates a file, and it is a cache with the files' own timestamps stored
  beside it rather than a second opinion about what a client is.
- **Backups cover both.** Settings → Backup writes one archive containing
  `amneziawg/` and, when present, `awg-panel/`. Restoring an older archive that
  predates the panel still works, which is why the layout is fixed: the archives
  `awg-menu` wrote before backups moved into the panel are still restorable here.

The one deliberate deviation from a "database is truth" design is exactly this,
and it is why removing the panel leaves a fully working VPN behind.

## Numbers, on four thousand clients

Four thousand is the ceiling of the network a default install hands out — a
`/20`, which holds 4093 — so it is the largest server this project sets up
without being asked for something else, and that is the point of measuring
there. What matters is not the figures themselves but which of them grow with
the size of the server, because the ones that do are the ones that decide
whether the panel is usable at all.

Reproduce with:

```bash
cd panel && AWG_BENCH=1 .venv/bin/pytest tests/test_benchmark.py -s
```

`AWG_BENCH_CLIENTS` sets the size. It is skipped without that variable: building
the fixture writes four thousand config files, which is the wrong thing to do on
every commit.

Everything is timed end to end through the HTTP API, on the real view and
serialiser stack, because that is what a browser waits for. Each figure is the
mean of twelve runs after a warm-up.

Read them as orders of magnitude, not measurements. Taken on an ordinary Linux
machine with other work on it, the same row varies by up to a factor of two
between runs, so the figures below are rounded and the ones separated by less
than that are not meaningfully different. What is robust across every run is the
part that matters: which of them grow with the size of the server.

**Reads, with nothing having changed** — the overwhelmingly common case, and
what a dashboard polling every two seconds actually pays:

| | before the indexed copy | now |
|---|---|---|
| a page of fifty clients | ~440 ms | ~20 ms |
| the dashboard summary | ~430 ms | ~10 ms |
| one client | ~440 ms | ~5 ms |
| a page, searched | ~450 ms | ~15 ms |
| a page, ordered by usage | ~460 ms | ~20 ms |
| the freshness check on its own | — | ~1 ms |

The two columns differ in kind and not only in size. The first grew with the
server; the second is three `stat()` calls and one indexed query, so the same
page costs the same on forty clients as on four thousand. That flatness is the
claim worth trusting, and it is asserted exactly rather than timed — see the
query counts below.

**Writes, and the read that follows** — what provisioning over the API costs per
client, since each one arrives in a request of its own:

| | before | with the copy | folding changes in | keeping the parse |
|---|---|---|---|---|
| add a client, then read the list | ~690 ms | ~600 ms | ~200 ms | ~115 ms |

Five hundred clients added one request at a time went from roughly five minutes
to under one. The server config is a single file, so appending a peer rewrites
all of it however the copy is kept — but it no longer *re-reads* all of it. A
mutation leaves its parse in memory, and the next one takes it back after a
`stat()` confirms the file is still the one that parse came from. Anything else
having written — a hand edit, a restore, `awg-panel manage` from a root shell
— moves that stamp and is read from disk exactly as before, which is what keeps
the cache honest about a file that anything with root can still change. What is handed back is always a copy,
because every mutation edits the structure in place; copying it costs about a
third of what parsing it costs, and that difference is the saving.

**Enforcement** — the writes the collector makes on its own, with nobody waiting
on it. These time the call that changes the configuration, not the whole
enforcement pass around it: that pass also re-reads every client's limits and
every client's config file, which is work these three rows do not include.

| | batched | and keeping the parse |
|---|---|---|
| disable one client | ~103 ms | ~87 ms |
| disable a thousand, in one call | ~99 ms | ~66 ms |
| asking for a change that is already applied | ~28 ms | ~12 ms |

The row that matters is the second. Switching clients one at a time — which is
what this did before the batch existed — meant one full parse and one full
rewrite of the server config each, sequentially, with the config lock held
across the lot. Nobody measured a thousand of those, because a thousand times
the first row is a minute and a half, and a minute and a half is not a pause
anyone would attribute to a quota; it is every panel request on the box
waiting. Batched, a thousand costs less than one
used to.

The last row is the common one: every pass re-asserts a decision the previous
one already applied, so what it usually asks the store for is a change that has
already been made — and that no longer opens the config to find out.

**After a change made outside the panel** — a hand edit, a restore, a
`awg-panel manage` run from a root shell — the copy has to be rebuilt from the files, and the read that
triggers it costs ~420-470 ms. That is the standing price of the config files
being the source of truth. It is paid once per outside change rather than once
per request, and it is why the first read after a panel restart is slower than
the rest.

Two things the benchmark does not reproduce, both of which flatter it slightly:
the kernel call is the mock controller rather than `awg syncconf`, so an add is
missing one subprocess, and the fixture's client configs are minimal. Both are
constant per client and neither grows with the size of the server.

The benchmark asserts nothing, which is why the variance above is tolerable: a
timing threshold on a shared machine either never fires or fails on a busy
laptop. What guards against a performance regression in CI is the query counts in
`panel/tests/test_client_index.py`, which require a page to cost the *same number
of queries* on one client as on forty-one. That is the property the figures above
are evidence for, it holds exactly on any machine, and it fails the build the day
a `select_related` is dropped and a page starts costing a query per row.

## Server settings: what saving does

Saving on **Server** or **Obfuscation** applies the change. There is no second
button to press, and no state where the file on disk and the running tunnel
disagree because somebody closed the tab. Each page saves only the settings it
shows, so an unsaved edit left on the other one is neither sent nor silently
discarded.

| Changed | How it is applied | Sessions drop | Clients re-import |
|---|---|---|---|
| `DNS`, endpoint, default `AllowedIPs`, keepalive | client configs are regenerated; the server is not touched | no | yes |
| any obfuscation parameter (`Jc`, `S1`–`S4`, `H1`–`H4`, `I1`–`I5`, `RandomTrailers`, …) | `awg-quick down` + `up` | yes, a second or two | yes |
| `ListenPort`, `Address`, `MTU` | `awg-quick down` + `up` | yes, a second or two | port and address: yes |
| `DisableCookies` | `awg-quick down` + `up` | yes, a second or two | no — no client config carries it |
| adding, removing, enabling or disabling a client | `awg syncconf`, live | no | no |
| a client's own routes or DNS | its config file is rewritten, live | no | that client |

The restart is the same down/up `awg-menu` has always run, and it happens inside
the save. Two things follow from that:

- **Traffic counters survive it.** The `PreDown` hook folds the kernel counters
  into `traffic.db` before the interface goes away, which is why the hook is
  there and why removing it costs you accuracy, not connectivity.
- **A moved listen port takes the firewall rule with it.** If ufw or firewalld
  is active, the panel allows the new port and removes the old one —
  `awg/firewall.py`, which is `fw_move` from the `lib/firewall.sh` the installer
  opens the first port with, in Python and inside the same save.
  **Cloud security groups are not reachable from the server**, so an
  AWS/GCP/Azure rule still has to be edited by hand — the save says so in as
  many words.

If the tunnel was already down, or the restart fails, the save still succeeds
(the config on disk is correct either way) and the page says the interface is
one restart behind, with the button to do it.

### Stopping the tunnel on purpose

The dashboard header carries two controls: **Restart the tunnel**, and beside it
one entry that says **Stop the tunnel** while it is running and **Start the
tunnel** while it is not. One control rather than a pair with one of them always
dead — a stopped tunnel cannot be stopped, and a running one does not need
starting. `awg-menu` carries that second entry on its main menu, and both of
them together in Diagnostics.

In `awg-menu` it sits on the main menu because taking the service down for a
maintenance window is an operation on the server, like updating it, rather than
troubleshooting; Diagnostics keeps its copy because that is where an admin
already is when a check has just told them the tunnel has to go.

They are not the same act. A restart is something done *to* a tunnel that is
meant to be running: it ends with every client back on and nothing changed. A
stop ends with a server that serves nobody until somebody says otherwise, which
is why it gets its own row in the activity log rather than sharing the restart's.

Restart is offered only while the tunnel is up. Restarting a stopped tunnel is
starting it, under a name that does not say so.

**The interface decides whether it worked, not the exit code.** Three things can
disagree here and routinely do: whether the interface exists, whether systemd has
the unit, and whether systemd believes it is holding the interface up. The unit
is `Type=oneshot` with `RemainAfterExit=yes`, so systemd reads "active" off
whether it ran `ExecStart` rather than off whether the interface is there — and
`install.sh` enables `awg-quick@awg0` and then brings the tunnel up with
`awg-quick` itself, as every restart since has done. On an ordinary server the
unit is therefore `enabled` and `inactive` over a tunnel that is running, and
`systemctl stop` against an inactive unit runs no `ExecStop` and exits `0`.

So both directions ask the machine rather than assume. A stop goes through the
unit when systemd believes it is up, because that is the path that keeps
systemd's view true and runs `ExecStop` under its own timeout, and then falls to
`awg-quick down` if the interface is still there afterwards. A start goes through
the unit where there is one — which is what makes the next stop work at all,
since systemd runs `ExecStop` only for a unit it started — and falls to
`awg-quick up` if nothing came up. Where the unit does not exist, which is an
AmneziaWG somebody set up by hand, `awg-quick` is all there is and there is
nothing systemd believes that using it could contradict.

A failure is judged the same way round: a `systemctl stop` that exited non-zero
after the tunnel had gone got the operator what they asked for, and one that
exited `0` without touching it did not. A unit parked in `failed` — by an
`ExecStop` that ran against an interface already taken away — is cleared with
`reset-failed` before the next start, because nothing else clears it and that
start is the one an operator reaches for precisely because the tunnel is down.

**The unit stays enabled, so a reboot starts the tunnel again.** A stop that
outlived a reboot would be a second, quieter piece of state with nothing on the
dashboard to show it. `systemctl disable awg-quick@awg0` is how to ask for that,
and the status page's `serviceEnabled` is where the answer shows up.

**Stopping is orderly, not a kill.** Whichever of the two paths it takes, the
hooks run — that is the whole reason neither reaches for `ip link del`: `PreDown`
folds the kernel's per-peer counters into `traffic.db`, and
`PostDown` takes the upload shaper's HTB root off the WAN interface — which
matters, because that queue sits on the path this machine sends everything
through, the panel and an SSH session included. Starting again runs the `PostUp`
hooks, which is what re-revokes the clients quota or expiry switched off; the
panel re-asserts that itself as well, for a config old enough not to carry the
hook.

**The panel does not go down with it.** It listens on `0.0.0.0` by default, on
its own port, and nothing about it runs over the tunnel. An admin who has bound
`webListen` to the tunnel's own address, or who reaches SSH through the VPN, is
the exception — `awg-menu` on the console is the way back.

While it is stopped the dashboard shows every client offline and no throughput,
which is correct rather than a fault, and the collector logs `no live interface`
once per cooldown for the same reason.

### Ports

Any UDP port from 1 to 65535 is accepted, including privileged ones. On a
network that only lets 443 or 53 out, that is exactly where the tunnel needs to
sit, and `awg-quick` runs as root so binding there costs nothing extra. Two
things to check first:

- **Nothing else is on it.** 443/udp is QUIC, 53/udp is a local resolver on many
  hosts. A collision makes the interface fail to come up rather than fall back.
- **51820 is a fingerprint.** It is WireGuard's default, so scanning for it is
  the cheapest way to find a VPN. The installer picks a random port in
  20000–59999 for that reason; a deliberate 443 is a different trade, not a
  worse one.

The panel's own HTTP port has the same range, for the same reason.

### Flood protection

One switch, `DisableCookies`, on its own card below the network one. It is not
obfuscation, which is why it is not on that page: it changes what this server
does under attack rather than what its traffic looks like, and no client ever
sees it.

Verifying a handshake costs real work, so forged ones sent from addresses that
do not exist are a cheap way to load a server. WireGuard's answer is the cookie:
while the server is under load it stops doing the work and replies with a
challenge instead, and only a peer really at the address it claims receives the
reply and can send it back. A genuine client passes that and connects; a flood
from spoofed addresses never sees the challenge and gets no further.

Switching it on takes that answer away. Under load the handshakes are dropped
where the challenge would have gone out, so a real client gets silence — no
error at either end — and no way back in until the flood stops. **Off is the
right default**, and the panel says so on the save when you turn it on.

Two things it is not:

- **Not a way to stop the server being recognised.** A cookie only ever goes to
  someone who already holds the server's public key *and* is flooding it; a
  stranger's junk fails the MAC check and is dropped in silence either way.
  What the reply looks like on the wire is `H3` and `S3`'s job, and both are on
  the Obfuscation page.
- **Not something a client needs.** It is a device setting, never a peer one.
  Nothing is written into a client config, no peer has to be new enough, and
  nothing has to be re-imported — the only 3.1 setting on either page of which
  that is true. The interface still restarts, because it is an `[Interface]`
  value like any other.

## IPv6, and who is still leaking

Each client row carries the IPv6 address the server routes to it, beneath the
IPv4 one. Blank means the server routes it none — which on a dual-stack tunnel
is a peer that has not been migrated, not a cosmetic gap.

Some rows also carry an **IPv6 outside the tunnel** badge. It means that
client's config routes the whole of IPv4 and none of IPv6, on a server that can
carry IPv6. There is nothing wrong with that client from the outside: it
connects, it moves traffic, its counters climb, and its device reaches every
dual-stack destination — most large sites — over its own connection, with its
own address, while the VPN reports itself connected. No error is raised
anywhere, which is exactly why the badge exists. It is the only place that
failure is visible.

Two things it deliberately does not flag. A split tunnel is not leaking: it
routes what somebody chose to route. And the question is coverage rather than
spelling — `0.0.0.0/1, 128.0.0.0/1` is the split default route, covers all of
IPv4 just as `0.0.0.0/0` does, and is flagged the same way. Reading that pair
as a split tunnel is a real mistake this project made once, and it left a
server leaking after it had been told it was fixed.

**The badge tracks the config the server issued, not what the device holds.**
That is the honest limit of it. Rewriting a client's config clears the badge
immediately, but nothing changes on a phone until it re-imports — and the
server has no way to know whether it has. So the badge answers "has this client
been re-issued?", and the only way to answer "has this device picked it up?" is
to look at the device: with the VPN connected, a what-is-my-IP page should
report an address inside the tunnel's own `/64`.

Re-importing is also not enough on its own. The client apps merge into an
existing tunnel of the same name rather than replacing it, so an old
IPv4-only `AllowedIPs` can survive the import. Delete the tunnel on the device
first, then import.

What the server does with IPv6 — `native`, `nat` or `blackhole` — is decided by
the installer from what the host actually has, and is reported by
`GET api/v1/server` and on `awg-menu`'s status screen. It is not something to
choose in a form: the only choices are what the machine can already do.

## Obfuscation

Five groups of settings — junk packets, packet sizes, header markers, imitation
packets and the advanced group behind them — sit in one card on the
**Obfuscation** page, because they are one thing. Nobody adjusts the padding
without also thinking about the header ranges; they are saved together, they
invalidate every client config together, and above all they are *generated*
together.

The advanced group was a card of its own below this one until the clients caught
up. It sat apart because it was a beta: header protection, content padding and
the timers all arrived in AmneziaWG 3.0, and a peer that did not speak 3.0
failed silently — so the group started closed, carried its warning on its face,
and had a *Generate* and a *Clear* of its own. A current client speaks 3.0. Two
generators for one server then meant a page that could be left half drawn, or
drawn from two different bands, so there is one *Reconfigure* and the group is a
section like the four above it.

The page is its own entry in the sidebar, between Server and Statistics. That
split is about how often each page is touched rather than about where the values
live — they are one config behind one `PUT api/v1/server`. Where the tunnel
listens is decided at install time and revisited when something about the
network changes; what it looks like on the wire is redrawn, handed out and
redrawn again, and costs every client a new config each time. Each page
therefore saves only its own groups, and its save bar only ever states the cost
of those.

### Nothing here is a default

There is no value in this group that this project ships the same to everybody,
and that is the whole design. A shared value is not obfuscation. It is a
signature with an extra step.

Take `S1`, the padding in front of a handshake initiation. Plain WireGuard's
initiation is always 148 bytes, and that number alone identifies the protocol —
one filter rule, `udp && len == 148`, and every WireGuard server on the network
is found. Ship a fixed `S1` of 86 and the rule becomes `udp && len == 234`. It
is exactly as cheap to write, has the same near-zero false-positive rate, and
now matches every server that ever ran this installer, all at once. The
obfuscation moved the target; it did not raise the cost of hitting it.

These parameters are not secret in any deployment, either. Every one of them is
copied into every client config, and those files get emailed, screenshotted and
pasted into group chats. The only thing that keeps one leaked config from
burning every server is that no two servers share a profile.

So `install.sh` draws the whole set at random on the machine it is installing,
the panel's *Reconfigure* draws it the same way, and the values below are
bands rather than numbers.

### Four bands, not four flavours

The panel's *Reconfigure* offers a profile, and every one of them draws every
value at random — that is not what separates them. What separates them is the
band, and every band is the same trade written down twice: how much of the
protocol's shape a setting hides, against what sending it costs.

| Profile | What it spends |
|---|---|
| **Standard** | the bands in the table below, which is what `install.sh` has always drawn and what every parameter's help text quotes |
| **DPI-resistant** | more of everything — 8–12 junk packets, 64–160 byte junk floors, 160–900 byte padding, a four or five packet decoy session |
| **Fast** | the smallest draw that still hides the protocol — 1–3 junk packets, 12–96 byte padding, a one or two packet decoy session |
| **Random** | all three at once |

Random is not a fourth flavour. The other three are published, so a server whose
`Jc` is 2 has said which profile built it; spanning all three is what stops the
choice itself from being a fingerprint. The cost is that it is the only one that
will not tell you in advance what the draw is going to cost.

No profile is allowed to produce a set the save bar then complains about, which
is what keeps `Jc` at 12 even at the top: past that the panel starts telling
the admin the handshake is needlessly slow on a mobile link, and a generator
that argues with the page it fills in is a generator nobody trusts.

| Setting | Group | Drawn from |
|---|---|---|
| `Jc` | junk | 3–8 packets |
| `Jmin` | junk | 24–80 bytes |
| `Jmax` | junk | `Jmin` + 40–240 bytes |
| `S1`, `S2`, `S3` | sizes | 24–320 bytes each, redrawn if `S1 + 56 = S2` |
| `S4` | sizes | 12–40 bytes, never more than `1440 − MTU` |
| `H1`–`H4` | headers | a narrow random range placed anywhere inside one quarter of 5–2147483647, then shuffled between the four |
| `I1`–`I5` | imitation | three to five packets of one protocol: a WebRTC call, a QUIC connection or a run of DNS lookups |
| `HeaderProtectionKey` | advanced | 32 bytes from `/dev/urandom`, and only when `S1`–`S4` can carry its 12-byte nonce |
| `ContentPaddingAddition` | advanced | a range: top 32–96 bytes, floor anywhere under a third of it |
| `RekeyAfterTime` | advanced | a range placed inside 120–180 s |
| `RekeyTimeout` | advanced | a range placed inside 4–7 s |
| `KeepaliveTimeout` | advanced | a range placed inside 8–15 s |
| `MaxHandshakeAttempts` | advanced | a range placed inside 16–24 |
| `RejectAfterTime` | advanced | derived, not drawn: the **tops** of `RekeyAfterTime` + `KeepaliveTimeout` + `RekeyTimeout`, plus 60–180 s, with its own width drawn from that same band |
| `RandomTrailers` | advanced | on — a switch, not a draw, and the only row here with no band |

The advanced rows are `install.sh` drawing from the second preset table, the one
the panel calls `ADVANCED_PROFILES`, at its standard band. It used to leave that
group empty, and `RandomTrailers` stayed off for a release after the rest of it
was drawn; the reasoning for both, and what the second change costs, is under
[the advanced group](#the-advanced-group) below. The bands are not the
obfuscation bands, because nothing in them trades bandwidth — the timers trade
how often a handshake happens at all.

The network group is not part of this and does have fixed values:

| Setting | Group | Value |
|---|---|---|
| `ListenPort` | network | a random port in 20000–59999 |
| `Address` | network | `10.13.0.1/20` |
| `MTU` | network | 1400 |
| `DNS` | network | `8.8.8.8, 8.8.4.4`, plus the two IPv6 resolvers where the tunnel carries IPv6 out |
| `AllowedIPs` | network | `0.0.0.0/0, ::/0`, and `0.0.0.0/0` alone only where IPv6 is off |
| `PersistentKeepalive` | network | 25 |

The rules behind the bands, in short: `Jmin` < `Jmax`; `S1 + 56 ≠ S2`, or the
two handshake packets come out the same size and become a recognisable pair;
`H1`–`H4` must be distinct and their ranges must not overlap. `RandomTrailers`
is a switch rather than a value, so there is nothing to copy between the two
ends — but both ends still need it, and it does nothing to data packets while
`ContentPaddingAddition` is set.

### The per-packet budget

`S4` is the only obfuscation setting charged against the MTU. It is pushed onto
the front of an already finished packet, so every byte of it comes out of what
the path leaves after the tunnel: `1440 − MTU` inside an ordinary 1500-byte one.
The panel **refuses** a combination that exceeds it — the save is rejected, not
warned about, and the error appears on `MTU` and `S4` alike, because the first is
edited on the Server page and the second on Obfuscation, and an error on a field
you cannot see is one you cannot act on.

`ContentPaddingAddition` rides on every data packet too and is deliberately
*not* in this sum, which is worth stating because the arithmetic invites the
opposite conclusion. It goes inside the encrypted payload rather than in front
of it, and the sender clamps each packet's share of it to what that packet
leaves below the MTU — a full-size packet simply gets none. Charging it here
would refuse configurations that work and suppress the setting almost entirely
at the default MTU.

It is enforced rather than advised because it is the one overrun with no symptom
at the moment it is made. The interface comes up, the handshake completes, small
requests work — and only full-size packets are dropped, so what the operator
sees weeks later is large downloads hanging and some websites never finishing.
Nothing in any log connects that to a number typed into a form.

The sum is what matters, which is why a per-field bound could not do the job:
`S4 = 30` fits at the default MTU and 30 bytes of content padding fits, and the
two together do not. Everything the generator draws is drawn against what is
actually left after the other one, so a drawn set always saves; when there is no
room, the field comes back at `0` or empty with a sentence saying which setting
took the space, rather than a value the save would then reject.

### The advanced group

These settings were added in AmneziaWG 3.0 — and 3.1 for `RandomTrailers`; 3.1's
other addition, `DisableCookies`, is server-side and sits on the Server page
instead.

Both generators used to leave the whole group empty. The reason was never that
the values are hard to pick: it was that a peer which does not honour one of them
fails *silently* — it negotiates without the setting, the server refuses it, and
neither end says why — and while most clients were on 2.x that was most peers.
The Amnezia app made it worse, because its `.conf` importer drops these lines
whatever version is reading them. So the group was a beta an operator opted into
behind a second button, and it was eight empty boxes they were expected to fill
in from the protocol specification — which is the same mistake a fixed
obfuscation profile would be. A value everybody copies out of one document is a
constant, not a setting, and the timers say precisely how often this server
handshakes.

3.0 is what a current AmneziaWG speaks, so both generators draw the group now:
*Reconfigure* with everything else on the page, and `install.sh` from the same
bands at install time, so a server has its strongest settings from the first
minute rather than from whenever somebody finds the button. What *DPI-resistant*
buys on this side is a handshake that happens less often — the one event on the
wire that obfuscation cannot make cheap — so it stretches the timers rather than
shortening them.

`RandomTrailers` held out one release longer, because it needs 3.1 rather than
3.0, and it is drawn now for the same reason the rest of the group is: a switch
the generator leaves off is one nobody turns on. It has its own paragraph at the
end of this section, including what it turns away.

**What this costs, stated plainly.** A peer that is not on AmneziaWG 3.1 will not
connect to a server installed or reconfigured this way, and neither will one set
up by importing a `.conf` into the Amnezia app, if that importer still discards
the lines. Both fail with no error at either end. The 3.1 in that sentence is
`RandomTrailers` alone; everything else in the group needs 3.0. Clearing the
group — empty every field and save, or delete the lines from `awg0.conf` — puts
the server back where it was, and clearing `RandomTrailers` by itself puts back
the 3.0 behaviour without giving up the rest.

**The five timers are ranges, not numbers.** `awg setconf` parses each of them
with `u16_range_from_string`, and the kernel calls `u16_range_pick_one` every
time it arms the timer — a fresh value per event, not one per server. That
matters more than it sounds. Randomising a timer per server means a flow is
periodic at an unknown period; periodicity detection does not need to know the
period, so an observer watching one flow for an hour still sees a metronome.
Randomising it per event is what removes the beat. Handshake cadence is the one
feature that survives every byte-level disguise — no amount of junk, padding or
header randomisation touches it — so this is the cheapest strengthening on the
page: it costs nothing on the wire and nothing in compatibility beyond the 3.0
the group already needs.

A range is drawn *inside* the band rather than being the band, because a range
every server on a profile shares would be a per-profile constant in place of a
per-server one — the same argument the bands themselves rest on. A plain number
is still accepted everywhere, since that is what every server installed before
this wrote and what `awg showconf` prints back for a range of width zero.

Expect the effect to differ across the five. `KeepaliveTimeout` and
`RekeyTimeout` gain the most: the kernel arms each of those with a single fresh
draw, and they shape the two patterns an observer can actually measure — the
beat of an idle tunnel and the retry burst while a filter is dropping
handshakes. `RekeyAfterTime` gains the least, and not for an obvious reason: the
kernel re-asks that question on every batch of packets it sends rather than once
per cycle, drawing again each time, so on a busy tunnel the first low draw wins
and the rekey lands within seconds of the bottom of the range. The spread is
real on a quiet tunnel and thin on a loaded one. It is still worth writing as a
range — the bottom is then not the same number on two servers — but it is not
where the win is.

`RejectAfterTime` is derived rather than drawn, because it has to outlast a
whole rekey cycle rather than the longest single timer in it — a peer starts a
handshake at `RekeyAfterTime` and may then spend `KeepaliveTimeout` +
`RekeyTimeout` waiting for the answer, so the three add up. It is also the
number the far end measures its own key against, and a responder that reaches
the threshold first starts handshaking on top of the initiator.

With ranges the sum is taken from the **top** of each of those three and cleared
by the **bottom** of the reject range. The kernel is less demanding — `receive.c`
subtracts the bottoms of `KeepaliveTimeout` and `RekeyTimeout` rather than the
tops — but a derivation that leaned on that would make the unluckiest draw in a
few thousand a stalled tunnel on a server nobody is watching, and the room costs
nothing. The save bar checks that same bound at those same ends, so a set
typed in by hand is held to what the kernel will actually do with it rather than
to what it does on average.

The ceilings on these fields are not cosmetic. The kernel stores each range as
two `u16` packed into a `u32`, and the tools' parser truncates to that without
checking the high end first: `RekeyAfterTime = 70000` is not refused, it is
silently 4464. The per-field maximums are what keeps a number nobody chose off
the wire.

`ContentPaddingAddition` comes back as a range, never a number. The kernel uses
it *instead of* the padding it does anyway — without it every packet is rounded
up to a multiple of 16 bytes, which hides the last four bits of its length for
free — so a constant addition trades a length known to within 16 bytes for one
that tracks the packet inside byte for byte. Only a range whose top reaches past
that rounding buys it back, and the generator will not draw one that does not.
Anything the installed module cannot do is left empty instead: it would be an
error at save time, not a silent drop.

`RandomTrailers` is drawn **on**, and was the last member of the group to be. It
covers the one thing the rest of the page does not: a handshake is the same
length every time it is sent, which is a pattern to match on however random the
bytes inside it are, and padding drawn per server does not change that — it
moves the constant, it does not remove it. With the switch on, the kernel
appends a trailer of random length to each packet, sized against what the path
has already carried, so it can never push one over the MTU and there is no
budget to charge it against.

It is safe to leave on only because `H1`–`H4` are drawn narrow. A trailer makes
a handshake's length unbounded, so the kernel stops testing an arriving one for
an exact length and tests it for a minimum instead — and `H1`–`H3` are then the
only thing separating a handshake from a data packet. The share of the header
space those ranges cover becomes the share of data packets misfiled as
handshakes and dropped, in each direction, with nothing in any log. The two
settings cannot be reasoned about apart: turning trailers on over hand-typed
quarter-wide ranges loses about a quarter of every data packet over roughly 470
bytes each way, and reads as collapsed throughput on a tunnel whose ping is
fine, because small packets stay under the length floors and survive. The save
warns if the ranges on a server are wide enough for it to matter.

**What the switch costs, and it is not nothing.** Trailers arrived in 3.1 where
the rest of the group arrived in 3.0, and a peer without them measures an
arriving handshake, finds it longer than expected and drops it with no error at
either end. How much that costs depends on whether `HeaderProtectionKey` is set
beside it, and the two cases are not close.

**With a key**, this turns away a peer on exactly 3.0 and nothing more. Anything
older than that, and anything set up by importing a `.conf` into the Amnezia app
whose importer drops these lines, was already turned away by the key and turned
away the same silent way.

**Without one**, it turns away every client below 3.1. A server has no key when
its MTU left `S4` too short to carry the nonce — `install.sh --mtu 1429` or
higher, and *Reconfigure* on a server built that way — and nothing else in the
group fails outright to cover for it: a client too old for the padding or the
timers ignores those lines and stays connected, as the table in
[A client stopped connecting](#troubleshooting) says. On that server this switch
is the only hard stop in the config. If you run one, that is the case to think
about before saving.

What was left either way was the newest setting on the page waiting for an
operator to know the fleet was there and go and find the switch — which is the
argument that used to leave this whole group empty, and it is answered the same
way. It is still a call about your own fleet: the module and tools `install.sh`
builds are 3.1, and so is every build pinned under
[Connect Your Devices](../README.md#connect-your-devices) — WG Tunnel gained 3.1
in 5.6.0 and the Amnezia VPN client in 5.0.1.5, which is why those pins are
versions and not bare links. The App Store has no version to pin, so an iOS
client is the one you check by hand. Clearing the field puts it back.

It is also the one setting on the page with nothing to copy — there is no value
to agree on, only on or off — and it does nothing to data packets while
`ContentPaddingAddition` is set: that one already decides their padding, and the
two do not stack. What it covers there is the handshake.

The key the button draws puts a floor under `S1`–`S4`, in the section above it.
The nonce header protection is applied with is read off the front of the padding
on each packet, so all four have to be at least 12 bytes or the kernel refuses
the entire configuration — `awg setconf` fails, and since an obfuscation save
restarts the interface, the tunnel goes down to apply a combination that will
not bring it back. One draw cannot produce that pairing, which is the quiet
argument for one button: the padding and the key come out of the same press, and
every band starts at or above the floor. The save still refuses a set that is
under it, for the config an API client assembles by halves.

There is no *Clear* beside it any more. It was the way back out of a beta — the
group off, every client connecting again — and turning off a group the generator
now fills in as a matter of course is not a button, it is emptying the fields.
An empty value still removes the line rather than blanking it, the same way an
unused imitation slot does, so `PUT api/v1/server` with `""` is the way back.

### Why the bands stop where they do

Every bound is a trade between cover and cost, and none of them is allowed to
cost anything measurable.

- **`Jc` at 3–8.** Junk packets are the cheapest cover there is, but each one is
  a packet that has to arrive. On a lossy mobile link a burst of thirty makes
  reconnecting visibly slower and buys nothing the first eight did not.
- **`Jmax` at most `Jmin` + 240.** A handshake costs up to `Jc × Jmax` bytes of
  junk. At the top of both bands that is under 3 kB, once every two minutes per
  client — under 25 bytes a second. Bigger junk would also risk fragmenting,
  which is a signature of its own.
- **`S1`–`S3` at 24–320.** Enough spread that two servers almost never land on
  the same handshake size, and small enough that the padded packet never
  approaches the path MTU. These ride on handshakes only, so they cost nothing
  in steady state.
- **`S4` at 12–40, capped at `1440 − MTU`.** This is the only one that touches
  data packets, so it is the only one that could cost throughput. At the default
  MTU of 1400 the cap is 40 bytes, which is the headroom an ordinary 1500-byte
  path leaves after the outer IP and UDP headers, the transport header and the
  authentication tag. Exceed it and full-size packets are silently dropped and
  pages hang half-loaded — so the generator does not.
- **Nothing below 12 on `S1`–`S4`, in any profile.** A header protection key is
  applied with a nonce read off the front of the padding on each packet, so a
  shorter prefix makes the kernel refuse the whole configuration. The two are
  drawn by different buttons that cannot see what the other was asked for, so
  the only way they cannot contradict each other is for no profile to go below
  the floor at all. Twelve bytes on a data packet is what that costs, out of a
  headroom that is never less than twenty.
- **Header ranges a few thousand values wide, placed anywhere in their
  quarter.** The quarters guarantee the four ranges cannot overlap; the shuffle
  is what stops the quarter a value lands in from revealing the packet type,
  which is the one thing `H1`–`H4` exist to hide; and *where* each range sits is
  drawn across its whole quarter, about 29 bits, which is where the entropy
  lives. The width is deliberately small, and is coupled to `RandomTrailers`:
  with trailers on the kernel accepts a handshake by minimum length rather than
  exact length, so `H1`–`H3` become the only thing separating a handshake from a
  data packet, and the share of the header space a range covers is the share of
  data packets misfiled as handshakes and dropped — in both directions, with
  nothing in any log. These were quarter-wide until that switch was drawn on,
  which cost about a quarter of every data packet over roughly 470 bytes per
  direction. Header values do repeat at this width; that is the trade, and on
  any server that drew a `HeaderProtectionKey` the type field is encrypted on
  the wire anyway, so the width is not observable there at all. Widening these
  by hand while `RandomTrailers` is set is the same bug, and the save warns.
- **Decoys of one protocol, three to five of them.** A decoy only works on a
  filter that parses it, and no real host speaks DNS, NTP, STUN and QUIC down a
  single UDP socket pair — a set that does is *more* distinctive than sending
  nothing. So one family is drawn per server and the whole set is built from it.
  The count varies because a fixed five is a marker too.

### Reconfigure

The **Reconfigure** button in the card header draws the whole set again. The
info button beside it says the same thing in two sentences.

It is worth pressing if the current settings may have been observed — a client
config that went somewhere it should not, a server that was blocked and moved.
Nothing about how fast the tunnel runs changes: the values come from the same
bands, with `S4` drawn against the MTU the interface actually has.

It fills the form and stops there. Applying it costs every client a new config,
which is not something to do on one click, so the save bar states the cost and
the save is a separate press. Afterwards the tunnel restarts and every device
has to re-import before it can talk again — export the new configs first.

`install.sh` draws the first set the same way, and it writes the same shapes:
`lib/obfs.sh` and `panel/awg/validate.py` are two implementations of one
generator, and `tests/obfs.sh` checks the shell's output against the panel's
validator so they cannot drift apart in silence. Redrawing one is the panel's
alone — `awg-menu` covers the box rather than the VPN's contents, and a second
generator writing these files is exactly the arrangement the panel replaced.

The catalog, band descriptions included, is served at
`GET api/v1/server/params`, so the tables above can be regenerated rather than
maintained by hand.

## Disabled clients, quotas and expiry

Disabling a client is a shared concept, not a panel-only feature. It writes a
comment into the peer block:

```ini
[Peer]
# Client = phone
# Created = 2026-08-04T18:00:00Z
# Disabled = 2026-08-05T09:12:44Z
PublicKey = ...
AllowedIPs = 10.13.13.4/32
```

The peer stays in the config, so its address stays reserved and its config file
is kept — but it is filtered out of what is handed to the kernel, so it cannot
connect. Re-enabling is instant and needs no re-import.

The client's row in the panel has the switch, and that is the only place the
decision is made: it is recorded alongside why, and there is no shell
equivalent. What the shell has is the hook that re-applies it —
`awg-panel manage enforce`, which a bring-up runs because the kernel does not
remember a revocation across one.

The marker is a comment rather than a flag because the file has other readers.
`awg-quick` and an admin's editor both see a peer that is plainly still there
and plainly switched off, and neither has to understand the convention for the
config to keep working.

The collector uses the same mechanism to enforce limits:

- **Quota** — bytes over the client's all-time total (upload + download, minus
  any usage reset). `0` means unlimited. The panel sets it and shows every
  traffic figure beside it in decimal units, so a 10 GB limit is ten thousand
  million bytes and `awg show` over SSH, which counts in binary, reports the
  same traffic about seven percent lower. Exceeded: the client is disabled with
  reason `quota`, **on the next 2 s poll**. Each client's remaining allowance is
  held in memory and spent from the deltas the collector is already computing,
  so a poll costs one subtraction per client that is actually transferring and
  nothing for the rest; only a client that runs its allowance down is looked up
  and checked properly. The alternative — checking once a minute — let a client
  on a 100 Mbit link finish some 750 MB past its limit, and a "10 GB" that in
  practice means eleven is not a limit worth setting.
- **Expiry** — a date and a time of day. The panel reads both in the browser's
  own time zone and stores the moment in UTC, so an expiry set for 18:30 happens
  at 18:30 where it was typed. The client list then says how much longer each
  client has rather than on which date it runs out — a countdown under the hour,
  then "Today at 18:30" and "Tomorrow at 09:00", then "in 30d" — with the exact
  moment on hover. Passed: disabled with reason `expired`. The earliest date
  still to come is worked out once per pass and the next pass is booked for it,
  so a date is acted on when it arrives rather than at the next multiple of the
  interval — and no client is asked about its expiry in between.

Raising the quota or moving the expiry re-enables the client **in the request
that changed it**, not on the next pass; the collector applies the same rule to
anything it missed. Enforcement stops when the collector is stopped; the web UI
keeps working, it just stops policing.

`enforceIntervalSec` is what remains on a timer, and it is no longer the
enforcement clock: it is how often the collector re-reads the config and every
client's limits, which is what picks up a hand edit made over SSH, a
hand-edited file or a restore. Widening it costs freshness against those and
nothing against a client transferring past its quota.

### Speed limits

A data limit says how much a client may move and an expiry says until when.
Neither says how fast, and AmneziaWG has no answer of its own — the module
encrypts and forwards, and every byte it forwards moves as fast as the link
allows. So a speed limit comes from the one place in Linux that can hold a
packet back without leaving kernel space, `tc`, and the panel drives it.

Turn them on in **Server → Bandwidth limits** first. While the switch is off
nothing is shaped, no client's limit is enforced, and the tunnel keeps exactly
the queueing the kernel gave it — the limits themselves are kept, so switching
it back on puts every one of them back. Each client then gets its own limit in
the add or edit dialog, in megabits.

There is no server link speed to set, and there used to be. It bought nothing:
HTB holds a class to its own ceiling whatever sits above it, so the figure only
ever decided whether a client's number would be accepted — and it could not be
detected, since a virtio NIC claims 10 Gbit while sitting behind a 200 Mbit
allowance, so it was a guess that capped the whole tunnel whenever the guess was
low. A limit above what the uplink can really deliver is not an error now; it is
simply one that never gets reached.

Two things on that card set limits in bulk. **Default download limit** is what a
newly added client is given, and it reaches nothing that already exists.
**Apply to all clients** does reach backwards: it writes that same limit over
every client on the server, replacing whatever each of them had, and there is no
undo — so it asks first, and says how many clients it is about to change.

Download is shaped on the tunnel itself. **Upload is off by default and is a
separate decision**, because it cannot be shaped there. A queue only slows
traffic on its way *out* of an interface, and a client's upload arrives on the
tunnel rather than leaving by it; by the time it does leave, on the server's
outgoing interface, NAT has rewritten the source to the server's own address and
there is nothing left to tell one client from another. So the packet is tagged
as it comes in, and the tag is what the outgoing queue sorts on.

What that costs is worth being exact about, because "a queue on the interface
this machine sends everything through" sounds worse than it measures. The tag is
`skb->mark`, a 32-bit field on the kernel's own wrapper around the packet rather
than on the packet: it never reaches the wire, so no bytes are added, the MTU is
untouched and nothing outside this machine can see it. Reading it back is a
`cls_fw` classifier, which hashes the mark into 256 buckets and compares an
integer already in cache — single-digit nanoseconds, against the hundreds it
costs to move the packet at all, and not a walk through one rule per client.
Traffic belonging to no client is not slowed either: it lands in a default class
rated at 100 Gbit with `fq_codel` under it, which is the queueing discipline it
would have been given anyway.

The one real cost is structural, and it only shows at rates most servers never
reach. A multi-queue NIC is normally driven through `mq`, which gives each
hardware transmit queue its own qdisc and its own lock — that is how a machine
spreads transmit across cores. A single HTB root replaces that with one qdisc
and one lock for the whole device, so every core sending serializes through it.
A single HTB root begins to bind somewhere around 4–6 Gbit/s on one modern core.
On a 1 Gbit server that is not measurable. On a 10 Gbit server it could be, but
a VPN server's ceiling is usually its cipher rather than its qdisc, and
AmneziaWG on four cores is unlikely to reach that figure in the first place. If
a server does run near line rate, this is the first setting to test with
`iperf3` on and off.

Either switch can be thrown at any time and takes effect at once, in both
directions. Turning upload shaping on puts its half up on a server that has been
shaping download for months; turning it off takes that half back off the
outgoing interface, so the queue does not outlive the decision and no client is
left held to an upload limit the panel no longer shows. Moving it to another
interface clears the one it came from.

That interface is `shaperWanIface`, the outgoing interface field beside the
upload switch on the Server page. It is shown only while upload shaping is on,
because a download queue lives on the tunnel and has nothing to point it at.
Left empty — which is how it should be left on a server with one network card —
it means whichever interface holds the default route, and this server's own
tunnel is never taken for it.

Naming one is for multi-homed servers, and there are three shapes of them:

* two uplinks, where the default route is one link and a policy route sends the
  VPN's NAT'd traffic out the other — auto-detection shapes the wrong wire and
  uploads go unlimited;
* a box that chains out through a second tunnel, so the default route is a `wg`
  interface rather than a physical one, and the queue lands on a device whose
  own traffic is already inside somebody else's encryption;
* a bridge or a bond, where the route lookup can land on a member interface and
  a queue on the member does nothing.

There is a fourth case the panel refuses rather than gets wrong: when the
default route is *this* server's AmneziaWG interface, upload shaping is not
applied at all and the log says so, because shaping both directions on one
device would have each pass rewrite every client's download class with its
upload rate and then read it back as the other. Naming the real uplink is what
fixes it.

It can also be set without the UI, with the session and CSRF cookies from a
normal login — [the worked example in API.md](API.md#a-worked-example) is how
`$J`, `$CSRF` and `$BASE` here are obtained:

```bash
curl -sb "$J" -H "X-CSRFToken: $CSRF" -H 'Content-Type: application/json' \
     -X PUT "$BASE/api/v1/settings" -d '{"shaperWanIface": "eth1"}'
```

Either way it takes effect the same way the switches do — the shaping structure
is rebuilt inside that request, and the queue moves off whatever interface it
was on.

Both address families are shaped into one class, so a limit is a total across
IPv4 and IPv6 rather than one allowance each. Shaping only IPv4 would not be a
limit on this project at all: every client here is given an IPv6 address, and
Happy Eyeballs would send most of its traffic down the unmetered path without
anyone choosing to.

Three things apply a limit, and the third is what makes it stick:

* the request that sets it, so the effect is immediate;
* the `PostUp` hook at bring-up, because `tc` rules belong to the network device
  and every one of them goes when `awg-quick` deletes it — a reboot would
  otherwise leave a server whose panel knows every limit and whose kernel
  enforces none;
* the collector's reconcile pass, which reads back what the kernel is actually
  holding and writes only the differences. That is what catches a hook that
  failed, a config too old to carry one, or a `tc qdisc del` run by hand.

Nothing is attached until a client needs it, and the structure comes off again
when the last limit is cleared. That matters more than it sounds: the kernel
gives a WireGuard interface no queueing discipline at all, so a server with no
limits set is exactly the server it was before this existed.

What it costs when it is on is a shaping queue on the tunnel's egress, which is
a single lock where there was none. On a server pushing more than a couple of
gigabits in total that is measurable, and it is the reason this is a deliberate
switch rather than something turned on by default.

Where the limits live and what moves them:

* **In the panel's database**, beside quotas and expiry dates, keyed by public
  key. They are in the backup archive, they survive a rename, a re-key and a
  restore, and they outlive any edit to the config files. What the kernel is
  enforcing is never stored — it is read back from the kernel, and the
  difference between the two is what the reconcile pass exists to close.
* **A subnet change renumbers them.** A client's place in the structure is its
  offset into the tunnel network, so moving or widening the subnet gives every
  client a different class. Nothing needs re-applying: the next pass reads back
  what the kernel holds, finds the old classes describing addresses no client
  has, removes them and writes the new ones. A client left *outside* the new
  subnet is skipped, which is the same verdict the server already reaches about
  it — such a client does not route at all until it is re-added.
* **A subnet of `/16` or wider cannot be shaped at all.** A kernel class id is
  sixteen bits and the last addresses of a `/16` need more than that. The panel
  accepts `/16` to `/30` for a tunnel, so this is an ordinary server rather than
  a mistake, and it reads as "off" rather than as a failure: the structure comes
  down, the server page refuses to switch speed limits on and says why, and
  the client dialog does not offer the field. `/17` is the widest that can be
  shaped, and it still holds 32765 clients.

### Switched off and expired are two facts

They overlap constantly and they are not the same question, so the panel keeps
them apart everywhere. Each of the four combinations exists: a client switched
off by hand whose date is months away, a client whose date has gone and which is
still passing traffic, both at once, neither.

The client list says the two things in two columns, and the filter above it asks
about them separately. The **status** column answers whether the client is
working, and how it is connected if it is: Online, Idle, Offline, Disabled or
Limit reached. **Expires** answers the date, as a countdown and then as Expired.
The status column never says "expired" — the API does not send that word at all
— because the expiry column beside it already says it, in the words that
question deserves, and the one word the status column has is better spent on
which of the two switch-offs happened. Which one it was is still on the wire, as
`disabledReason`.

In the filter, **Disabled** and **Expired** therefore both list a client that is
both, and **Active** means working: a lapsed client that is still switched on is
active, because it is.

### The name, and the one the panel suggests

A client's name is not a label on it. It is the name of the file its private key
is written into, the word in the peer block's `# Client` comment, and the
identifier every per-client route is addressed by — so one has to be chosen
before a client can exist at all, and two clients cannot share one.

**New client** therefore opens with a name already in the box: nine characters
drawn at random, from lower-case letters and digits with `l`, `1`, `o` and `0`
left out, because these names are read off a screen and typed back into a shell
or a support message and those four are the ones that get read wrong. It is a
value and not a placeholder, which is the whole point — it can be saved as it
stands, or typed straight over, and both are one action. The button beside the
box draws another. Empty the box and the server names the client instead, which
is also what `POST api/v1/clients` does for a request that leaves `name` out.

Nine characters out of thirty-two is forty-five bits, so a suggestion landing on
a name this server already has is not something an operator will meet. It is
still checked, because the name is an identifier and "very unlikely" is not the
same as looked at: the server checks under the config lock, against the peers in
the config *and* against any `clients/<name>.conf` left behind by a client
removed while the directory was unwritable — writing over that file would
destroy the only copy of somebody's key. A suggestion the server refuses is
replaced by another and the dialog says so; a name that was typed is refused
plainly, because that one was a decision.

API tokens work the same way and for the same reason — see [API
tokens](#api-tokens).

### The list is a page

Searching, filtering, sorting and paging all happen on the server, and the list
comes back one page at a time. They had to move together: the filters read
words that only exist once a client's config, counters, metadata and live
reading have been merged, and there is no way to narrow on that in a browser
without first sending every client to it.

Which is what used to happen, and what stopped working. A server with four
thousand clients sent all four thousand every thirty seconds to draw twenty
rows, polled a live blob holding four thousand peers on top of that every two
seconds, and could not begin filtering until the last row had arrived. Both
requests are now the size of what is on screen.

Two consequences worth knowing. The search box waits a moment after you stop
typing, because each settled value is a request rather than a pass over a list
already in the browser. And the count above the table is how many clients the
search and the filter **found**, not how many rows are showing — on a paged list
those are different numbers, and the first is the useful one. The counts the
bulk removal offers are taken the same way, over every client rather than the
page, because that is what a sweep acts on.

How big a page is, is yours. Under the table, **Rows per page** offers 10, 25,
50 and All, and the choice is remembered by the browser it was made in — like the
theme and the language, it describes the screen the panel is being read on
rather than the server being administered, so it is neither sent to the API nor
written into the URL. Ten is what a browser that has never chosen gets, because
every visible row also costs a live sample every two seconds.

All means all of them, on one page, however many there are. It briefly meant
five hundred — the most rows the API will build for a caller that names a size —
and an operator with 1072 clients picked All and got three pages of it, which is
the one thing the word rules out. It is sent as `pageSize=0`, a request for
every row rather than a request for a large number of them, because the panel
cannot name the count when it asks: the count is in the answer.

What that costs is real, so past five hundred rows the page says so above the
table, with a button back to fifty at a time beside it — the rows-per-page
control is at the foot of a table that is by then some thousands of rows tall,
and the way out of a page too long to scroll cannot be at the bottom of it. The
warning is not a refusal and there is no confirmation in front of the choice:
whether a long page is slow is a property of the machine reading it, and the
operator is about to be able to see for themselves.

Beside it are the pages: at most seven buttons however many there are, the first
and the last always among them, and an ellipsis over whatever the middle cannot
reach. Past seven pages a box appears to type one straight in — it takes any
number and clamps it, so 99 in a list of twelve pages means the last one. On a
phone the numbers give way to the two arrows and a "Page 4 of 12" line, which is
the same control in the width there is.

### Keeping a lapsed client on

Switching an expired client back on used to last until the collector's next
pass, one minute later. Now it is a decision the panel records, so the client
keeps working until its date is changed — which is how an admin gives somebody a
few more days without editing the date they are supposed to have run out on. The
confirmation says as much before it happens, and the expiry column then reads
"Expired · kept on" in amber rather than a red Expired the row would otherwise be
contradicting.

The record is tied to the exact date it forgives, so anything that changes the
date — moving it, clearing it — puts enforcement straight back, as does switching
the client off again. A quota has no equivalent: data used is a measurement
rather than a decision, so a client re-enabled over its limit is switched off
again on the next pass, and the way to keep that one going is to raise the limit
or reset its usage.

### Removing in bulk

After **New client** is **Bulk remove**, which opens a dialog with two boxes:
all expired clients, and all disabled ones — the latter covering both the
clients that reached their data limit and the ones somebody switched off by
hand. Each box carries the number it would take, and the two are not added
together when both are ticked: a lapsed client the collector has already
switched off is in each of them and goes once. Removing asks for confirmation
first, with that number as the title, and the sentence under it names the
categories actually chosen rather than describing all three cases at once.

The expired box goes by the date alone, so a client that still carries the
`expired` marker with a date now in the future is left where it is. The one
lapsed client it skips is one that was switched back on: honouring that decision
in the collector while deleting the client here would be the worse half of both
behaviours. That client is still shown as expired and can still be deleted on
its own.

It goes through `POST api/v1/clients/bulk-remove`, which takes `expired` and
`disabled` as booleans, so the same tidy-up can be scripted. The counts the
dialog shows come from the client list — `expiredCount`, `disabledCount` and
`expiredOrDisabledCount`, each over every client rather than the page, because
the sweep removes the clients a filter is hiding as readily as the ones on
screen.

Because a full `awg syncconf` from another tool re-applies the whole config, a
disabled peer can be live for up to one poll interval before the collector
removes it again. Two seconds by default.

## Settings

Stored in the panel database, editable from Settings — except the five `shaper`
keys, which are edited on the Server page, beside the tunnel they shape:

| Key | Default | Notes |
|---|---|---|
| `webListen` | `0.0.0.0` | bind address; also written to `/etc/awg-panel.env` |
| `webPort` | `2097` | |
| `webBasePath` | `/`, and `/awg/<20 chars>/` on anything `install-panel.sh` set up | secret URL prefix |
| `tlsCertPath`, `tlsKeyPath` | empty | gunicorn terminates TLS when both are set |
| `configEndpointMode` | `ip` | `domain` swaps the host in client configs as they are handed out |
| `configEndpointHost` | empty | the domain to use; empty follows the certificate's name, if it has one |
| `sessionMaxAge` | `86400` | seconds |
| `loginRateLimit` | `5` | failures before a 15-minute IP lockout |
| `theme`, `language` | `system`, `en` | |
| `trafficPollSec` | `2` | collector cycle and SPA poll interval |
| `onlineThresholdSec` | `180` | handshake age that still counts as online |
| `enforceIntervalSec` | `60` | how often limits are re-read from the files; not how fast they are applied |
| `shaperOn` | `0` | the switch for per-client speed limits: `0` shapes nothing at all, and every client's stored limit is kept for when it goes back on |
| `shaperDefaultDownMbps` | `0` | what a newly added client's download limit is set to; `0` is no limit, and existing clients are never touched by it |
| `shaperDefaultUpMbps` | `0` | the same for upload, and only where `shaperUpload` is on |
| `shaperUpload` | `0` | also limit upload, which means shaping the server's own outgoing interface |
| `shaperWanIface` | empty | which interface that is; empty means whichever holds the default route, which is right unless the server is [multi-homed](#speed-limits) |

Changing the port, bind address, base path or TLS rewrites `/etc/awg-panel.env`
and restarts `awg-panel-web` two seconds later, so the browser still receives
the response. The UI then waits for the new URL and redirects there.

### The address in client configs

A server is set up at an IP address, so that is what the installer wrote into
every `Endpoint` line and what sits in every file under `clients/`. Once a
certificate is added the panel also has a name, and the name is usually what
people should be given from then on — but re-issuing every client to change how
one line spells the same host would hand every device new keys.

So `configEndpointMode: domain` does the swap on the way out. The download, the
QR code and the export archive are rendered from the file with the host replaced
by the certificate's domain, or by `configEndpointHost` when that is set. The
port is kept, the files on disk are never written, and the tunnel goes on
reporting the address the server was actually configured with. Turning it off
restores the previous behaviour exactly; nothing has to be re-issued either way.

A certificate issued for an address rather than a name — which is what Let's
Encrypt hands back for a panel administered at its IP — counts as carrying no
name here. The address on it is the address the configs already carry, so there
is nothing for the swap to do, and the Settings page says so instead of offering
an IP under a box labelled **Domain**. Pinning `configEndpointHost` still works:
that name comes from the operator, not from the certificate.

Two consequences worth knowing. A config already on a device keeps the address
it was issued with until it is sent again — this only decides what the *next*
download says. And if the certificate is later removed or replaced with one
carrying no usable name, configs quietly go back to the address in the server
config rather than the download failing.

### The account

Settings › **Authentication** holds the credentials the login page checks: the
username, the password, and the second factor. The first two are one form and
one request — the current password proves who is asking, and the username box
and the password boxes are each filled in only if that half is being changed. A
rename alone leaves the password as it was; a new password alone leaves the name
as it was; a request that changes neither is refused rather than reported as
saved.

A username may hold letters, digits and `. _ @ + -`, and must begin with a
letter or a digit — a name starting with a space or a dot is one somebody will
fail to type back at the login form. A name another account already holds is
refused, case and all, so a panel cannot end up with `admin` and `Admin` on it.

The password has no rules: whatever you type is accepted, short or obvious or
all digits, and the panel neither measures it nor argues with it. There is one
account here and it belongs to whoever runs the server, so a length floor would
be protecting that person from their own choice. What a guessable password is
actually exposed to is guessing over the network, and that is answered where
guessing happens — five failed attempts lock the address out for fifteen
minutes, and the second factor below means a guessed password is not enough on
its own.

Either change signs every other browser out, because either one replaces what
those browsers signed in with; the one making the change stays signed in. The
tunnel is untouched by all of this — no client is disconnected by a rename, and
nothing under `clients/` is rewritten.

Renaming the account does not move the second factor: the TOTP secret belongs to
the device, not to the name, and the code the app shows goes on working. What
changes is the label the app displays, and only for a device enrolled after the
rename.

The way back into a panel whose password has been lost is the shell, as it has
always been: `sudo awg-panel passwd` looks the account up rather than assuming
it is called `admin`, so a renamed panel is recovered the same way as any other.

### Signed-in devices

The same tab lists every browser that currently holds a valid session for the
admin account, and ends any of them on the spot. Django's own session table
records a key, a blob and an expiry and nothing else, so the panel keeps one row
beside each session with when it started, the address it was signed in from,
what its `User-Agent` claims to be, and when it was last used. The session
remains the authority on what is alive: a row whose session has expired or been
deleted is pruned rather than listed.

Ending one deletes the session itself, so the cookie the other browser is
holding names nothing and its next request is anonymous. There is no waiting for
an expiry and nothing to re-import — the tunnel is not involved at all, and no
VPN client is disconnected by any of this.

The session you are reading with is listed first, marked, and cannot be ended
from here: this browser would go on sending a cookie for a session that no
longer exists, and every page would fail with nothing to explain it. Sign out,
in the account menu, is the operation that does that properly. **Sign out
everywhere else** ends all the others at once, which is the thing to reach for
when a laptop goes missing — followed by a password change, which ends other
sessions as well.

Two details worth knowing. `lastSeenAt` is written at most once a minute, so an
open dashboard does not rewrite the row twice a second; the address is written
the moment it changes, because that is the one field an admin might act on. And
the address comes from the socket unless `AWG_PANEL_TRUST_PROXY=1` says there is
a proxy in front, exactly like the login lockout — an unproxied panel that
believed `X-Forwarded-For` would report whatever address the client typed.

### API tokens

The same tab, one card further down, is the credential for everything that is
not a person: a deployment script, a monitoring check, a cron job that adds a
client when somebody gets a new laptop. Before this existed the only way to let
a script in was to give it the admin password, which is the account's whole
identity — it cannot be given to two scripts separately, it cannot be taken back
from one of them, and a script using it leaves a session in the list saying
nothing about what created it.

A token is the same access with the three properties a password does not have.
It is **named**, and that name is what the activity log writes beside everything
the token does: a row that says `API · nightly backup` is a different thing from
one that says `admin`, and the difference is exactly the question an operator is
asking when they read the log. It can be given an **expiry**, so a token issued
for a migration in March is not still working in October. And it can be
**revoked** on its own, without changing what a person signs in with.

The name matters enough that the dialog does not start by asking for it. It
opens with the same nine random characters a new client is offered — see [The
name, and the one the panel
suggests](#the-name-and-the-one-the-panel-suggests) — because a required field
that has to be invented before anything else can happen is answered with "test",
and "test" in the log a month later is worth no more than nothing. Type over it
with the name of the thing that will hold the token, or clear the box and the
server draws one; a token may be renamed later, and the secret is untouched by
that.

Issuing one shows the secret once, and once is the whole of it: the panel keeps
a SHA-256 of the token and not the token, so a secret that was not copied out of
that dialog is replaced rather than looked up. The list afterwards shows the
last four characters — enough to tell which row is the copy sitting in a CI
configuration, and nothing an attacker can use against 256 bits of randomness.
Send it as `Authorization: Bearer awgp_...`; no cookie and no CSRF token are
involved, because there is no cookie for a forged page to abuse.

**Reset the expiry each time it is used** is off by default and is the one
option worth thinking about. Off, the expiry means what it says: the token stops
working on that date whatever is still using it. On, every use puts the expiry
back to the full window, so the token in daily use keeps working and the one
whose script was decommissioned lapses on schedule — which is the opposite of
the usual outcome, where the credential that is still needed is the one that
quietly runs out at three in the morning. It is not the default because a token
that renews itself for as long as anybody keeps calling it has an expiry only on
paper, and that should be a decision rather than a surprise. It cannot be
switched on for a token with no expiry at all: there would be nothing to renew.

A token may do everything the panel can **except** change the credentials that
authorise it. It cannot issue another token, rename the account, change the
password, touch two-factor, or read and end signed-in sessions; all of those
answer `403`. Restoring a backup is on that list too, and for the same reason
rather than out of caution: the archive carries `db.sqlite3`, which is where the
password hash and the tokens themselves live, so a token that could upload one
could hand the account to whoever chose the file. That is what makes revoking a
leaked token mean something: if holding one were enough to mint a second, the
answer to a leak would be theatre.

Emptying the activity log is the last thing on that list, and it is the same
argument one step along. Revoking a token works because the panel can be asked
afterwards what that token did; a token that could clear the log could erase the
answer along with everything else it had written. Reading the log stays open —
exporting it somewhere is a reasonable thing for a script to want — and the
clear is a person at the login form.

Downloading a backup is open, and it is worth knowing exactly what that means,
because it is where the list above ends. The same `db.sqlite3` goes out in the
archive, and it holds the session table — where a session key is the
`awgsessionid` cookie itself and not a hash of it — beside the second factor's
secret. Anything that has fetched one archive can put on a signed-in browser's
cookie, and is then past every `403` in the paragraph above. It stays open
because a nightly job copying a backup off the box is worth having and the same
file already carries every private key on the server; what it is not is silent,
since Activity records the download under the token's own name. So a token that
has ever fetched a backup should be treated as the password: if it leaks,
change the password and sign every browser out rather than only deleting the row.

Expired tokens stay in the list rather than being swept, because the row is the
answer to "why did my deployment start failing on Tuesday". Revoking is what
removes one, and it takes effect on that credential's very next request.

The same card is on the [API page](#the-api-page), which is where a token is
usually wanted: in the middle of writing the script, not in the middle of
administering the panel. It is one component and one list, so a token issued
from either place is the same row.

### What the traffic accounting costs

The kernel's counters can only be read, never subscribed to, so the collector
polls: `awg show <iface> dump` every `trafficPollSec`, differenced in memory,
written to `live.json` for the dashboard. The database sees none of the per-poll
readings, and none of the per-client ones either. Every client's delta is summed
into two integers as it arrives, and **once every ten seconds** that pair is
written as a single row holding the whole server's traffic for the current UTC
day. One row a day, whether the server has three clients or four thousand.

That is the whole of what is stored about traffic over time. There is no
per-client history and no chart of one: what a client has used all-time lives in
`traffic.db`, and what the server has moved today is the row above.

Losing the collector abruptly — `kill -9`, a power cut — costs at most the ten
seconds since the last write, because the running total is in memory in between.
An orderly stop flushes on the way out and loses nothing, and a restart reads
the row back before it resumes, so today's figure survives one.

`live.json` is in `/run/awg-panel`, not the data directory, and that is the one
place the panel writes on a timer for as long as it is installed. On a server
with a few thousand clients the blob is most of a megabyte, rewritten whole
every couple of seconds with the two `fsync`s every other write here gets — tens
of gigabytes a day against an SD card or a cheap VPS SSD, for a file whose
entire value expires before the next poll. On a tmpfs it costs no disk at all.
Nothing is lost: `/run` survives a restart of either unit and is cleared only by
a reboot, and the one field in the blob that outlives a cycle — when the tunnel
came up — is already refused by the collector when it predates the current boot.
It is not in a backup either, which is correct: a restored set of rates would be
a claim about a machine somewhere else, at a time that has passed.

The one other thing that flush writes is each client's last handshake and the
endpoint it came from, and only when one of them changed since the last write —
about once every two minutes per connected client, nothing at all for the rest.
The kernel drops both when the interface goes down, so without the copy a
reboot, a settings change that restarts the tunnel, or a restore from backup
would leave every client reading "never seen". The live dump still wins wherever
it has an answer, and whether a client is *online* is decided on the live value
alone — a stored handshake is a memory, not a connection.

So the database no longer scales with the poll rate. It holds the accounts, the
sessions, one metadata row and one index row per client, one traffic row per day
for the server, and one per client per day for the clients that transferred
something that day.

That last table is the only one that grows with both time and the client count,
so it is the one worth sizing. A row is about 50 bytes and is written only for a
client that actually moved bytes, so a 30-client panel holds a few thousand rows
a year — under a megabyte. The ceiling is clients × 400, reached only if every
client transfers every day: a four thousand client server at full activity would
reach 1.6 million rows and a couple of hundred megabytes. Rows past 400 days are
swept by the collector once a day. The server's own daily rows are never swept —
one a day is 3,650 in a decade, and they are the only record of what this server
has carried.

This used to be far larger, and the difference is the sample rate rather than the
per-client keying. Traffic was kept per client at ten-second resolution and
rolled into per-client-hour totals, against retention settings of 48 hours and
365 days: about 5 MB per always-connected client, so roughly 250 MB at 50 clients
and 1.2 GB at 250. At four thousand it was four hundred inserts a second and some
20 GB. All of it existed for per-client charts that were never built. The charts
exist now, and they read a row per client per *day* — rewritten in place as the
day goes on, so a client transferring all afternoon stays one row instead of
becoming three thousand.

Upgrading drops both tables, and dropping them does not shrink the file. SQLite
keeps freed pages for reuse rather than returning them, so the database stays at
its high-water mark and simply stops growing. To reclaim the disk, with the
services stopped:

```bash
sudo systemctl stop awg-panel-web awg-panel-collector
# the panel's own Python, so this needs no sqlite3 command on the server
sudo /opt/awg-panel/.venv/bin/python -c \
  "import sqlite3; sqlite3.connect('/var/lib/awg-panel/db.sqlite3').execute('VACUUM')"
sudo systemctl start awg-panel-collector awg-panel-web
```

### Reading the traffic charts

Statistics and logs draws those rows, and the same chart appears per client
under **Clients → ⋯ → Traffic history**. Two controls sit above it and they
answer different questions:

| | |
|---|---|
| Daily / Monthly | whether one bar is a day or a month |
| The range | which days or months are drawn — a preset, or two dates |

A range applies to both, because a month is a sum of days: pick 1–31 March and
the daily view has 31 bars and the monthly view has one. The chart opens on the
last 90 days and the last 36 months.

Those are wider than the box, on purpose. The plot **scrolls sideways**, and
every bar keeps the same width and the same value axis whichever part of the
range is in view — so a quiet fortnight in May is drawn against the same scale
as a busy one in August, and comparing the two is a matter of looking at them.
It opens at the newest end; older is to the left, and the edge fades where there
is more. The line under the controls always reports the range actually on
screen, which is not always the one that was asked for:

- A window is cut at today, and at 400 days or 36 months, whichever comes first.
  A chart with four thousand bars in it is not a chart.
- It never starts before the first day there is a record for. A panel installed
  last month opens on last month, not on three years of axis with two bars at
  the end of it, and a client added in June opens on June.
- A **client's** history is swept after 400 days, so its range stops there too,
  and its months start at the first whole one — a month half of which has been
  pruned would be a short bar for a reason that is not traffic. The server's own
  days are never swept: given a panel that has been up that long, its monthly
  view really does go back three years.

A range with nothing in it is a normal answer — a client that stopped
connecting in the spring, a fortnight nobody used — and the chart says so in
words rather than drawing a flat axis, with a button back to the range it opened
on.

The totals beside the legend are for the whole range rather than for the part in
view — scrolling moves the view and asks nothing new; changing the range asks
something new and gets a different answer.

### Starting the numbers again

**Remove all traffic**, above the chart, does what it says: every figure the
panel holds goes to zero at once. Each client's all-time upload and download,
each client's stored days and months, the server's own history, and today's
card. It asks first, and the confirmation is worth reading, because this is the
one thing in the panel with no copy anywhere. A backup holds these counters as
they were when it was taken, and `traffic.db` — the file that lets an all-time
total survive a reboot, and which clearing a *single* client's usage is careful
not to touch — is cleared with the rest. What is left afterwards is one line in
the activity log saying who did it and how much there was.

It is about numbers and nothing else. No client is disconnected, no key,
address, quota or expiry date changes, and nobody has to re-import anything.
Anyone who had been stopped by a data limit starts again with the whole of it,
switched back on by the collector within a minute.

The figures read zero the moment it returns, but the file behind them is
finished by the collector on its next flush — ten seconds later, or when it next
starts if it happens to be stopped. Nothing on screen moves when that happens;
it is worth knowing only if you go looking at `traffic.db` with a text editor in
between.

For one client rather than all of them, there is **Clients → ⋯ → Reset traffic**,
which is the narrower operation in every sense: it leaves the server's own days
standing and keeps the true lifetime figure in `traffic.db`.

### Activity: what was done to this server

The second tab of **Statistics and logs** is the panel's own record. One line
per thing that happened, newest first, each with who did it and where from:

- **Clients** — added, edited, renamed, switched on or off, removed, given new
  keys, had their counters cleared, exported.
- **Server** — the configuration saved, the interface restarted, stopped or
  started, and the two the collector notices on its own: the tunnel going away
  and coming back. Stopped and gone away are kept apart on purpose: from the
  outside a tunnel somebody switched off and one that fell over look the same,
  and which of the two it was is the question asked in front of this log.
- **Panel** — settings changed (by name, never by value), a backup downloaded,
  a configuration restored from one, the activity log emptied, every traffic
  figure on the server removed.
- **Sign-in** — signed in and out, a refused attempt, an address locked out, the
  password changed, the account renamed, two-factor turned on or off, sessions
  ended.

The lines nobody else can produce are the collector's. When a client crosses its
data limit or passes its expiry date, the panel switches it off with nobody
pressing anything — and this is the only place that decision is written down
with a date on it. Those rows have no account against them, which is the point:
they read as the panel rather than as somebody. So does a client that came back
on by itself, and the row above it usually says why.

Filter by kind, by whether it is a warning, or by typing a client name. The
search matches the client an event was about and the account that caused it, so
`phone` is that client's history and `admin` is that account's.

Events are swept after 180 days, and a ceiling of 20 000 rows applies after
that — the age window is what bounds an ordinary panel, and the ceiling is what
bounds one whose login page is being hammered. Neither is a setting, for the
same reason the traffic retention is not: getting it wrong is either a log with
holes in it or a database growing for no benefit. The table lives in
`db.sqlite3`, so it travels with a backup — and a restored configuration arrives
carrying whatever history was in the archive, with the restore itself as the
first line on the other side of it.

**Clearing it.** The button beside Refresh empties the log, after a
confirmation that says what goes and what does not. It takes the whole table
rather than whatever the filters are currently showing — a clear that depended
on a search box three fields away is one the operator would find out about
afterwards — and nothing else moves with it: no client, key or setting is
touched, and the service log in the next tab belongs to journald.

What is left is not a blank page. The panel writes one row immediately after the
clear, naming the account that pressed it, the address it came from and how many
events went, and that row is the whole point: a ledger that can be emptied
without trace is not a ledger. It is also the row the next clear takes, so this
does not build up. An API token is refused — see [the API page](#the-api-page)
for where that line is drawn and why — so this is something a person does at the
panel, having signed in.

### The service log

The third tab is `journalctl` without the ssh: the last few hundred lines from
`awg-panel-web`, `awg-panel-collector` or `awg-quick@<iface>`, exactly what
`sudo awg-panel logs` prints. Nothing is stored — journald already holds these
lines and already rotates them.

It is the other half of the answer. The activity log says a save happened and
that the tunnel had to restart for it; this is where the sentence `awg-quick`
printed while failing to come back up is. Errors and warnings are coloured,
lines are wrapped rather than cut, and the whole block copies to the clipboard
for pasting into a bug report.

Not live. Every read runs `journalctl`, so the tab reads once when it opens and
again when the refresh button is pressed. On a host with no systemd — a
container, a development tree — it says so and the activity log beside it is
unaffected.

If a change locks you out — wrong bind address, broken certificate — fix it on
the server:

```bash
sudo nano /etc/awg-panel.env
sudo systemctl restart awg-panel-web
sudo awg-panel url
```

## The API page

Everything the panel does, it does through its HTTP API, and the panel's own
interface is a client of it with nothing reserved for that client. That was true
before this page existed and was written down only in [API.md](API.md) — prose,
in the repository, on a machine that is not the one being administered. The page
brings it to where the server is, in four tabs.

**Getting started** is the path from "I have a panel" to "I have a script that
works": issue a token, copy this installation's own API address, make one call.
Every address and command on it is built from the panel the page is running on,
secret base path included, so nothing has to be assembled from three places on
the page and one from the browser's address bar — which is where the mistakes
are. It ends with what every answer looks like: camelCase, RFC 3339 in UTC, the
`{"detail", "errors"}` shape of a failure, and the `503 not_configured` that
every config-reading route answers on a server with no tunnel.

**Authentication** is the tokens card from Settings, unchanged, with the part a
script author needs around it: which of the two credentials to use, what a token
may not touch and why the line is drawn at the credentials rather than at the
pages they are edited on, and what the activity log will say about work the
token does — `api:` and the token's name, which is why naming one is worth
doing.

**Endpoints** is the reference: every route the panel serves, grouped, searchable
by path, by verb or by a word from what you are trying to do, and each one
opening onto its parameters, a real request body, the answer that comes back,
the failures worth handling, and a `curl` command with this panel's address
already in it.

**Recipes** is the half-dozen jobs people actually automate, written out in full:
provisioning a batch of clients, sweeping the ones nobody uses, copying a backup
off the box nightly, noticing a change without polling the client list, checking
that the tunnel rather than the panel is up, and re-rating everybody at once.

### It cannot go out of date

The reference is not written on the page. It is fetched from
[`GET api/v1/openapi.json`](API.md#get-apiv1openapijson), which the panel
renders from a catalog kept beside its own URL map, and the test suite walks
that map against the catalog in both directions: a route added without an entry
fails, and so does an entry left behind by a route that was removed. So the
words an admin reads describe the panel serving them.

That endpoint is also the answer for anything that is not a person reading a
page. It is an OpenAPI 3.1 document with this panel's address already in
`servers`, so it imports into Postman, Insomnia or Bruno and feeds a client
generator without being edited first, and the button on the page downloads it. A
token may fetch it, which is deliberate: a script being written against this
panel is exactly who wants it.

The page's own words are translated; the per-endpoint prose comes from the
document in English, and the UI overrides what it has a translation for, keyed
by operation id. That is the same arrangement the obfuscation parameter catalog
uses, and for the same reason — the panel picks its language in the browser and
never tells the server which one, so the API has no language to answer in.

## TLS and reverse proxies

**Getting a certificate.** The installer offers to obtain one at the end of a
first install, and `sudo awg-menu` → Web panel → Certificates runs the same
thing later. Either way it drives certbot, writes the two paths into
`/etc/awg-panel.env`, restarts the service, and installs a deploy hook under
`/etc/letsencrypt/renewal-hooks/deploy/` so a renewal actually reaches the
running panel — gunicorn reads the certificate once at startup, so without that
hook a renewed certificate sits on disk while the old one stays on the wire.

**The certificate screen.** `sudo awg-menu` → Web panel → Certificates is one
screen for all of it, and it opens by saying what the panel is serving right
now — the names on the certificate, who issued it, and how long is left — or
that there is none. When TLS is on against a certificate that has been deleted,
has expired, or no longer matches its key, it says that instead, because each
of those is a panel refusing connections at that moment. From there:

| | |
|---|---|
| Show | the full certificate: names, issuer, expiry, key type, chain length, and whether the key matches |
| Get one | the issuing conversation below, for a name or an address |
| Import | point the panel at certificate and key files you already hold |
| Pick one found here | anything valid already on the machine, listed with what it covers |
| Renew | force a reissue of the panel's own lineage, now — offered only while certbot still holds it |
| Delete | remove an issued certificate, its archive and its renewal config |
| Turn HTTPS off | back to plain HTTP, keeping both paths for next time |
| Renewal | which certbot, which schedule, and whether the deploy hook is there |

Import and the found-certificate list both check the pair before anything is
written: that the certificate parses, that the key parses and is not
passphrase-protected, that the two actually belong together, and that it has
not expired. gunicorn refuses to start on any of those, and a refusal there
costs the panel rather than the change — so the reason is given while the
change can still be declined. A file holding the leaf alone, with no
intermediate, is accepted after a warning: it works for whoever set it up and
fails for clients that have not cached the chain elsewhere, which is the
hardest TLS failure to see from the server. A self-signed certificate is
warned about in its own right rather than for the chain it was never going to
have — no browser trusts one, so it is the single case where turning HTTPS on
makes the panel look broken rather than safe.

Deleting the certificate the panel is serving is allowed and takes the panel
back to plain HTTP first, since gunicorn restarted onto a certificate that is
no longer there does not come back.

Three answers are offered, and the differences between them are not only
convenience:

- **A domain name** pointed at this server. Ninety days, renewed on certbot's
  own timer. The better answer wherever a name is available.
- **This server's IP address.** Let's Encrypt issues these for 160 hours — just
  over six days — under its `shortlived` profile, and renews every couple of
  days unattended. It needs certbot 5.3 or newer — `--ip-address` and the
  six-day profile it rests on are newer than anything distributions package
  today. **The address is published to public certificate transparency logs**
  within minutes of issuance and again at every renewal, so a server that took
  this route can be found by reading those logs instead of by scanning for it.
  Against a censor enumerating VPN endpoints that is a real loss; against
  somebody reading the admin password off a café network it is a clear win. It
  is offered rather than defaulted to for that reason.
- **A certificate already on this machine.** Looked for first, because a server
  that has run another panel usually has one, and reusing it issues nothing,
  opens no port and publishes nothing new.

**Which certbot.** Whatever is already installed is used untouched when it is
new enough for the job, so a Debian box holding certbot 2.x issues a domain
certificate with it and downloads nothing. When one does have to be installed,
it is the current release from PyPI, into a virtualenv at `/opt/certbot`, the
way certbot's own instructions install it — the packaged certbot is left where
it is, nothing under `/usr/bin` is written, and `/usr/local/bin/certbot` is
symlinked at it only when that name is free or already a symlink. It says so
and asks first.

That one is installed for a name as well as for an address, and the reason is
what happened otherwise. The arrangement before it apt-installed an old certbot
for a name, and then, when the same admin came back for an address certificate,
found that certbot too old for `--ip-address` and installed a second one beside
it: two certbots on the box, the question of which holds the panel's lineage
settled by whichever ran last, and the renewal timer pointing at whichever the
installer happened to be holding. Installing the current one the first time
removes the second install and the ambiguity with it. If PyPI cannot be reached
the packaged certbot is still installed as a fallback, which covers a name and
never an address.

**What renews it.** A packaged certbot brings a renewal timer with it, and
that is left to do the work. One installed here from PyPI brings nothing —
upstream leaves the cron entry to the reader — so `awg-certbot-renew.timer`
goes in beside it and checks four times a day. The same timer is installed on a
distribution whose certbot package schedules nothing at all, which is how
certbot arrives on Alpine and Arch. A packaged timer is never disabled, and for
an address certificate it is not enough on its own: an older certbot renewing
that lineage reads the SANs off the certificate, keeps the names and drops the
addresses.

Both ACME routes validate over http-01, so inbound TCP 80 has to reach this
server at issuance **and at every renewal** — the firewall rule is left in
place for that reason. Nothing listens on 80 between renewals; certbot binds it
for the few seconds one takes. A cloud security group is the usual reason a
renewal that worked once stops working.

The whole step is best-effort: it runs after the install has otherwise
finished, and a certificate that cannot be issued leaves the panel on HTTP with
the summary saying so. If the certificate turns out to be one gunicorn will not
start on, the panel is put back on HTTP rather than left unreachable.

**Direct TLS.** Settings → TLS, or by hand:

```bash
sudo sed -i 's|^AWG_PANEL_TLS=.*|AWG_PANEL_TLS=1|' /etc/awg-panel.env
sudo sed -i 's|^AWG_PANEL_TLS_CERT=.*|AWG_PANEL_TLS_CERT=/etc/ssl/panel.crt|' /etc/awg-panel.env
sudo sed -i 's|^AWG_PANEL_TLS_KEY=.*|AWG_PANEL_TLS_KEY=/etc/ssl/panel.key|' /etc/awg-panel.env
sudo systemctl restart awg-panel-web
```

Both files have to live outside `/root`, `/home`, `/run/user` and `/tmp`. The
units run with systemd's `ProtectHome=yes` and `PrivateTmp=yes`, so those
directories are empty or private for the service — a certificate there is
invisible to it however plainly `sudo cat` shows it. Setting `AWG_PANEL_TLS_CERT`
to such a path by hand still does not work and the Settings page still refuses
it; that behaviour is unchanged. `/etc/ssl/` is still the usual place for a
certificate put there by hand, and a Let's Encrypt path under
`/etc/letsencrypt/live/` works as it is, untouched and uncopied.

What is new is that `awg-menu` (Certificates → "Pick one of the certificates
found here") now handles certificates found in those protected paths. Rather
than widening the sandbox, it installs the pair into `/etc/ssl/awg-panel/` and
points the panel at that copy. Where the certificate came from `acme.sh` it uses
`acme.sh --install-cert` with a `--reloadcmd "systemctl restart awg-panel-web"`
so renewals refresh the copy automatically; where only a plain copy was possible,
`awg-menu` announces it as such because that copy goes stale at the next renewal.

The installer's own HTTPS offer goes the same way. It lists the certificates it
finds, `/root/.acme.sh` included, and accepting one of those installs it into
`/etc/ssl/awg-panel/` first — it used to hand the protected path straight to
gunicorn, which then would not start, so the offer could not be accepted at all.
Where that leaves a plain copy the summary at the end of the install says so.

**HTTP stops working, which is the point.** With the certificate held here the
port only speaks TLS, so a plain `http://` request gets an alert and no answer
at all. Behind a proxy gunicorn is still listening in the clear, and there the
panel checks the scheme itself: a page asked for over HTTP is redirected to the
same URL over HTTPS, and anything that writes is refused outright with a line in
the journal. So a proxy that stops forwarding `X-Forwarded-Proto` presents as a
panel that saves nothing and says why, rather than as an admin password crossing
the network in the clear. Paths outside the secret base path are untouched by
this — they are the same bare 404 they are on a panel with no TLS at all, since
a redirect would tell a scanner there is something here to redirect.

**Turning it off again** moves the browser back to the server's address rather
than to the domain, and `sudo awg-panel url` says the same. While TLS was on,
every response carried `Strict-Transport-Security`, so the browser was told to
use HTTPS for that name for a year and will keep turning `http://<name>/` into
`https://<name>/` on a panel that no longer answers there — which looks exactly
like a service that failed to restart. The address is never pinned that way, so
it is the one that still works. With `ENDPOINT_HOST` left blank in
`clients.env` there is no address to offer and the name stands; clear the HSTS
entry in the browser, or reach the panel by IP.

**Behind nginx.** Bind the panel to localhost
(`--panel-listen 127.0.0.1`) and proxy the secret path through:

```nginx
location /awg/k2p9x4mt7wq1bz8n5rv3/ {
    proxy_pass         http://127.0.0.1:2097;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
}
```

Then, in `/etc/awg-panel.env`:

```
AWG_PANEL_TRUST_PROXY=1
AWG_PANEL_TRUSTED_ORIGINS=https://panel.example.com
```

`AWG_PANEL_TRUST_PROXY=1` is not optional in this setup. It tells the panel to
believe `X-Forwarded-Proto`, which makes three things correct at once: the
login lockout counts the real client address instead of the proxy's, session
and CSRF cookies are marked `Secure`, and HSTS is sent. Without it gunicorn
sees plain HTTP and ships cookies that a downgrade could read. Only set it when
a proxy really is in front: the flag decides that forwarded headers are read at
all, and `AWG_PANEL_FORWARDED_ALLOW_IPS` below decides whose are worth reading.

`AWG_PANEL_TRUSTED_ORIGINS` is needed whenever the browser reaches the panel
under a different scheme or host than gunicorn sees; it is a comma-separated
list of full origins.

`AWG_PANEL_FORWARDED_ALLOW_IPS` (default `127.0.0.1`) says which peers may send
those forwarded headers, and it is read by the panel as well as by gunicorn: a
request that arrives from anything else has its `X-Forwarded-*` headers removed
before Django reads them, so the login lockout counts the socket the request
actually came from and the scheme is the one gunicorn saw. It takes addresses,
CIDR ranges, or `*` for any peer. Leave it at the default unless the proxy is
on another host — a proxy elsewhere needs its address listed here, and `*`
means the panel believes whatever it is told, which is only safe if nothing but
the proxy can reach the port at all.

**SSH tunnel, nothing exposed.** The safest option for a single admin:

```bash
ssh -L 2097:127.0.0.1:2097 user@server
# then open http://127.0.0.1:2097/<path>/
```

## Docker

The image carries the panel and its dependencies but not the kernel module: the
host must already run AmneziaWG, and the container needs its config directory
and network.

```bash
docker compose up -d
```

`docker-compose.yml` runs web and collector with `network_mode: host`,
`cap_add: [NET_ADMIN]`, and bind mounts for `/etc/amnezia` and a named volume
for `/var/lib/awg-panel`. `awg` and `awg-quick` must be visible inside the
container — the compose file mounts them from the host, which is why the image
does not try to build the tools itself.

## Development

No VPN server, no kernel module, no root:

```bash
make -C panel dev      # loopback, hot reload, keeps what you leave behind
make -C panel demo     # one port, every interface, a seeded year of traffic
```

`AWG_MOCK=1` swaps in an in-memory controller that fabricates an interface, a
few peers and plausible traffic, and bootstraps a demo config in a temp
directory. The whole UI — dashboard charts included — works against it.

`demo` is the one to reach for when the panel is running somewhere other than
the machine with the browser on it, or when the traffic history charts need
looking at: they describe months, so a sandbox set up ten minutes ago draws them
as rows of zeroes. It seeds eleven clients and a year of days, prints a freshly
generated password, and deletes everything it wrote when it stops. It is served
by gunicorn, as the installed panel is, but the two things that would make it
look like one are both off by default: `DEMO_TLS` turns on TLS, with either a
certificate generated for the run or one you already hold, and `DEMO_PATH=random`
mounts it under a generated base path instead of the root.

Ports, the dataset, every Makefile variable and every environment variable are
in [DEVELOPMENT.md](DEVELOPMENT.md).

```bash
make -C panel test     # pytest, mock controller
make -C panel lint     # ruff check + ruff format --check
make -C panel build    # frontend -> dist, then collectstatic
tests/hooks.sh         # the tunnel's hooks survive an upgrade
tests/obfs.sh          # the shell's profiles pass the panel's validator
tests/subnet6.sh       # the two IPv6 derivations agree
tests/recovery.sh      # a failed install leaves the tunnel up
tests/deps.sh          # the dependency pins still say what they claim
```

### Dependencies move when you move them

Nothing here updates itself. Every dependency is pinned to one exact version,
and for Python to the sha256 of every file pip is allowed to download:

| | Declared as | Installed from |
|---|---|---|
| Python | ranges in `panel/requirements.txt` and `pyproject.toml` | `requirements.lock`, `requirements-dev.lock`, `requirements-bootstrap.lock` |
| Frontend | caret ranges in `panel/frontend/package.json` | `package-lock.json`, via `npm ci` and nothing else |
| Node | — | `panel/frontend/.nvmrc` |
| Kernel module and tools | `KMOD_REF` / `TOOLS_REF` in `install.sh` | `vendor/`, packed into the bundle by `fetch-sources.sh`; a checkout without one clones those tags instead. Either way the commit each stood at is verified |
| Container base | — | `python:3.12-slim` by digest in `panel/Dockerfile` |

The ranges are permission for where the next resolve may land, not what any
server gets. A range like `django-axes>=6.5,<9` spans three major versions, and
the transitive packages — most of what actually gets installed — were named
nowhere at all, so before the locks existed they arrived at whatever version
PyPI happened to be serving that morning. Two servers installed a month apart
ran different code, and no test suite anywhere had seen either combination.

To take newer versions:

```bash
make -C panel lock     # re-resolve within the ranges, rewrite the locks
make -C panel test     # then prove the new resolution actually works
make -C panel wheels   # refresh the bundled wheels to match
```

Upstream's kernel module and tools move by hand, since they are named by tag
rather than resolved from a range. Edit `KMOD_REF`/`TOOLS_REF` and the
`KMOD_SHA`/`TOOLS_SHA` beside them in `install.sh`, then re-vendor — the fetch
reads the pins back out of that file and refuses a tag that does not resolve
to the commit recorded for it, so a bad pin fails here rather than on a
server:

```bash
./fetch-sources.sh     # re-fetch vendor/ at the new pins, verifying both
```

Commit the locks with the change. `tests/deps.sh` runs in CI and fails if a
range was widened without regenerating, if the two copies of the dependency
list disagree, if a lock entry lost its hashes, or if the development lock
pins a different version than servers get. It deliberately does not re-resolve
against PyPI: a check that did would go red the morning upstream published
anything, which is the failure the locks exist to end.

### A release installs without an index

`fetch-wheels.sh` downloads every wheel the locks name — for both
architectures and every interpreter `install-panel.sh` accepts — and
`build.sh` packs them into the with-panel bundle. `install-panel.sh` then
installs with `--no-index`, so the bytes a server runs are the bytes in the
file it downloaded. Air-gapped installs work, a PyPI outage is irrelevant, and
nothing that happens to an index between the release and the install can reach
the server. If the bundle has no wheel for that platform, the install says so
and falls back to PyPI — still hash-checked against the lock, so what arrives
is still exactly what was tested.

`fetch-sources.sh` does the same for the half that is not Python. Both bundles
carry the kernel module and tools source at the pinned commits, so `install.sh`
builds without cloning — including the VPN-only installer, which is the one
whose whole job is building that module and which was until then the first
thing to fail on a network that blocks GitHub. It is the reason that installer
is ~800 KB rather than 60 KB, and the reason a release is worth carrying to a
server on a USB stick at all: between the two, the only network an install
still needs is `apt`, for `build-essential`, `dkms` and the kernel headers.

That is why the dependency list is kept small and, where there is a choice,
pure Python. A pure-Python wheel is one file that covers every architecture and
interpreter at once; a compiled one needs a separate build for each. QR codes
came from `qrcode[pil]`, whose Pillow dependency is a C image parser that
needed a 7 MB build per Python version per architecture — 50 MB of the 79 MB
set, and 64% of it, to draw black squares. `segno` does the same job in one
73 KB pure-Python file with no dependencies at all, and the images it produces
are the same pixel dimensions and decode identically. The wheel set is 31 MB
because of that swap.

Upstream's kernel-module and tools tags are checked by commit, not by name —
`git clone --branch v3.1.x` follows a tag wherever it has been moved to, and
what gets compiled from it is loaded into the kernel as root. A mismatch stops
the install before anything is built, and says which commit it wanted. Release
bundles carry that source in `vendor/` rather than cloning it, so the check
there is `vendor/PINNED` against the pins in `install.sh`: it catches a release
assembled from a stale `vendor/`, which would otherwise build a different
module than the one the install prints on screen.

`crosscheck.sh` used to be the one to run after touching anything that reads
or writes `/etc/amnezia/amneziawg`. It alternated between `bin/awg-client` and
the panel's Python core step by step, so a format drift in either direction
failed there rather than on a server. It went with the CLI: there is no second
implementation left for the panel to disagree with, and the format is now
pinned by the panel's own suite, which asserts the peer block, the client
config and the 0600 modes on every file involved.

Three seams have a test each, because nothing else covers them:

| Seam | Test | What it would otherwise cost you |
|---|---|---|
| installer ↔ the config in the field | `tests/hooks.sh` | an upgrade that leaves `awg0.conf` calling a command that is gone, so counters vanish on restart and revoked clients return at boot |
| SPA ↔ API field names | `panel/tests/test_api_contract.py` | a renamed serializer field, invisible to both `tsc` and pytest, showing as an empty column |
| shell ↔ its own CSP | `panel/tests/test_spa_csp.py` | inline scripts silently dropped by the browser, so the SPA never learns its base path |

`hooks.sh` is there for the same reason `crosscheck.sh` was: it is the one
piece that runs unattended on somebody else's machine, against the file
holding every client's key. It extracts the awk program from `install.sh`
rather than copying it — a copy would pass forever while the installer drifted
away from it — and runs it under mawk, gawk, original-awk and busybox, because
mawk is what those servers have and gawk is what a developer has.

The contract test reads `frontend/src/api/types.ts` and asserts every field the
SPA declares is actually present in a live response, which is the one thing
neither type-checking nor the Python tests can see.

## Troubleshooting

**The panel will not start.**

```bash
sudo awg-panel status
sudo journalctl -u awg-panel-web -n 60 --no-pager
```

A migration failure or a bad TLS path is the usual cause; both are reported in
full in the journal.

**"Cannot reach the server" in the browser.** The port is not open. `ufw` and
`firewalld` are handled by the installer; a cloud security group is not.

```bash
sudo ss -tlnp | grep awg-panel      # is it listening at all
sudo awg-panel url                  # what URL it thinks it serves
```

**404 on every path.** The base path is secret and wrong paths get a bare 404 on
purpose. `sudo awg-panel url` prints the right one.

**Live rates are always zero.** The collector is not running, or `awg` is not on
`PATH` for it:

```bash
systemctl status awg-panel-collector
sudo awg show awg0 dump | head
```

**Traffic totals look wrong after a restart.** A restart with the collector
down misses a counter epoch: the kernel's counters go back to zero and
nothing was watching to record what they held. Nothing is lost going forward,
and `sudo awg-panel manage trafficsync` resynchronises the raw counters
against what the interface reports now. The `PreDown` hook is what normally
prevents this, so check it is still in `awg0.conf` if it keeps happening.

**A speed limit is set and the client goes faster than it.** Check
`Server → Bandwidth limits` first: while the switch is off no limit is enforced
at all, and the client dialog does not offer the field. Otherwise the structure
is missing from the interface, which happens when
`awg-quick` brought the tunnel up without running its `PostUp` hooks - a config
predating them, or `iproute2` not installed. `sudo awg-panel manage shape` puts
every limit back immediately and says how many it applied; the collector does
the same by itself within `enforceIntervalSec`. `tc class show dev awg0` is what
the kernel is really holding, and one class per limited client is what it should
show.

**A client stopped connecting after a config change.** Obfuscation parameters
must match on both ends. Changing `S1`-`S4`, `H1`-`H4`, `I1`-`I5` or
`RandomTrailers` requires every device to re-import; the panel says so and
offers an "Export all configs" link, and the shell equivalent is
`sudo awg-panel manage resync`.

**A client config works on a desktop client but not in the app.** Check whether the config
carries `HeaderProtectionKey`, `ContentPaddingAddition`, a timer override or
`RandomTrailers`. Those are AmneziaWG 3.0 and 3.1 settings: a client below
those versions ignores them, and the Amnezia app's `.conf` importer drops the
lines silently even where the client would understand them.
`HeaderProtectionKey` and `RandomTrailers` are the ones that fail outright — a
client that never received the key cannot read a protected header, and one
without random trailers drops a handshake packet it finds longer than expected,
so the handshake fails with no error at either end. The padding and the timers
leave a working tunnel with one end doing less than the operator thinks. The
panel badges them all "Needs AmneziaWG 3.0+" for exactly this reason.
