# Developing the panel

Everything in `panel/` runs on a laptop with no VPN server, no kernel module, no
`awg` binary and no root. `AWG_MOCK=1` swaps in an in-memory controller that
fabricates an interface, its peers and their counters, and every developer
target points the panel at a throwaway config tree instead of
`/etc/amnezia/amneziawg`. So a checkout is a working panel a minute after
`make venv`, and nothing you run here can reach a real server by accident.

- [Getting set up](#getting-set-up)
- [The two ways to run it](#the-two-ways-to-run-it)
- [`make dev` — editing](#make-dev--editing)
- [`make demo` — showing](#make-demo--showing)
- [The demo dataset](#the-demo-dataset)
- [Every Makefile variable](#every-makefile-variable)
- [Every environment variable](#every-environment-variable)
- [Tests, lint and the locks](#tests-lint-and-the-locks)
- [Pointing a target at a real installation](#pointing-a-target-at-a-real-installation)

## Getting set up

```bash
cd panel
make venv          # .venv from the pinned, hashed locks
make dev           # or: make demo
```

`make venv` installs the runtime plus pytest and ruff, `--require-hashes`
against `requirements-bootstrap.lock` and `requirements-dev.lock`. Node is
fetched on demand: any target that needs the frontend depends on
`frontend/node_modules`, which is built with `npm ci` from the committed
lockfile and nothing else. If that fails, `package.json` and
`package-lock.json` genuinely disagree — run `npm --prefix frontend install`
yourself, look at what it changed, and commit it.

`make help` lists every target.

## The two ways to run it

|  | `make dev` | `make demo` |
|---|---|---|
| Frontend | Vite dev server, hot reload | built bundle, served by WhiteNoise |
| Server | `manage.py runserver` | gunicorn, as on a real box |
| Ports | two — API 8088, UI 5173 | one — 8099 |
| Bound to | `127.0.0.1` | `0.0.0.0`, every interface |
| Scheme | `http` | `http`, or `https` when `DEMO_TLS` asks for it |
| URL | the root | the root, or a path when `DEMO_PATH` asks for one |
| Django debug page | on | **off** |
| Data | whatever you leave behind | reseeded every run |
| Sandbox | `.dev/`, persists | `.demo/`, deleted on exit |
| Admin account | yours to create | generated, printed, new each run |

Use `dev` while editing and `demo` to look at the result or show somebody —
particularly when the panel is running on a VM or a remote box and the browser
is somewhere else.

## `make dev` — editing

```bash
make dev
make dev API_PORT=8102 UI_PORT=5192     # a second panel alongside the first
```

Runs `manage.py runserver` and Vite side by side. Vite proxies `/api` to the
Django port, so the browser only ever talks to the UI port. Both are on
loopback; Ctrl-C stops both.

The sandbox is `.dev/`, and it persists: the clients you add, the settings you
save and the traffic the collector accumulates are all still there next time.
Delete the directory to start over. `make collector` runs the collector against
the same sandbox in the foreground, which is worth doing in a second terminal —
without it there are no live rates, no online lamps and no system gauges.

On first run the mock writes a demo server config and three clients
(`laptop`, `phone`, `tablet`) so the panel has something to show.

## `make demo` — showing

```bash
make demo
```

One command, one port, no arguments. It migrates, builds the SPA, collects the
static files, seeds a year of traffic across eleven clients, generates an admin
password, and prints what to open:

```
  demo data seeded, as of 2026-08-13
    11 clients, 1852 client-days
    399 server-days, back to 400 days ago
      alice-laptop   Weekdays only
      ...

    sign in as   admin / PSR7NelaaIkSAwCJ4

  open         http://10.0.0.2:8099/
               http://192.168.1.40:8099/

  Ctrl-C stops the panel and the collector and deletes .demo
  no TLS: the password above, the session behind it and a private key
  per client all cross the network in the clear
  DEMO_TLS=self serves a certificate made for this run alone;
  DEMO_TLS=<cert> DEMO_TLS_KEY=<key> serves one you already have
  reachable from any machine that can route here; not for a public interface
```

The port is **fixed at 8099** on purpose: the point of this target is a URL that
is the same on every machine it is ever run on, so it can be put in a message to
somebody else.

### A base path, if you ask for one

The panel answers at the root. `DEMO_PATH` moves it:

```bash
make -C panel demo DEMO_PATH=random     # /demo/ka9v3ujqr2t7wm1xf/
make -C panel demo DEMO_PATH=/demo/     # /demo/
```

`random` is seventeen characters from thirty-six, new on every run and gone with
it. Anything else is taken literally and normalised to one slash at each end, so
`demo`, `/demo` and `demo/` all mean the same directory.

The root is the default because it is the URL that can be read out over a phone,
and because this target is meant to work with nothing typed after it. What
`random` is for is the layout a real server actually runs: `install-panel.sh`
generates a base path into `/etc/awg-panel.env`, and a root-mounted demo is the
one part of the deployment shape you cannot see.

What a path buys beyond that is worth stating plainly. Everything outside it
404s empty — no redirect, no body, nothing to fingerprint — so a port scanner is
not handed a login form, and the URL cannot be guessed by somebody who knows
only the port. It is not authentication; the login form is. And it is no help at
all once the URL has been sent to anybody.

### TLS, which is off unless you ask

`make demo` serves plain HTTP. The target's job is to be typed without arguments
and looked at, and both ways of turning TLS on ask for something first — a
warning to click through, or a certificate that has to already exist. Neither
belongs in front of somebody who wanted to see the dashboard, and the thing
being served is mock data behind a password valid for one run.

The network it is shown over is a different matter, and that password is printed
on one machine and typed on another. Over a wire with anyone else on it, the
password, the session cookie behind it and a private key per client on the
clients page are all readable by anything in between. So a plain run says so
when it starts, and there are two ways out of it.

**A certificate you already have** — the better one, wherever it exists. Nothing
is generated and nothing is deleted; the files stay where they are when the
sandbox goes:

```bash
make demo DEMO_TLS=/etc/letsencrypt/live/dev.example.com/fullchain.pem \
          DEMO_TLS_KEY=/etc/letsencrypt/live/dev.example.com/privkey.pem \
          DEMO_ADDRS=dev.example.com
```

`DEMO_ADDRS` matters here: it is what the printed URLs and the CSRF origins are
built from, and pointing them at an IP the certificate does not cover produces
exactly the name-mismatch warning the real certificate was meant to avoid. Both
files are checked for readability before anything else happens, so a typo fails
in a second rather than after the SPA build.

**A certificate for this run alone**, when there is no name to get a real one
for:

```bash
make demo DEMO_TLS=self
```

`manage.py democert` writes an EC pair into `.demo/tls/`, covering every address
in `DEMO_ADDRS` plus loopback, and prints its SHA-256 fingerprint:

```
  cert         self-signed, this run only, 7 days
               sha-256  4C:1B:9E:0A:7D:33:F2:58:6B:C4:11:9A:0E:57:D8:24
                        A9:36:70:EE:12:BD:4F:81:C5:2A:63:97:08:DA:35:76
```

It signs itself, so the browser warns once. That warning is why the fingerprint
is printed: it is the one shown behind *this connection is not private*, and
comparing the two is the difference between a demo that is encrypted and one
that is also the panel you started. Nothing forces you to look; nothing can, on
a sandbox with no name. The pair is deleted with the rest of `.demo/`.

Either way the certificate is held by gunicorn rather than by a proxy — and it
is gunicorn in the plain mode too, though nothing there needs it: `runserver`
speaks no TLS, and a target with two process models is one where the mode you
debugged is the other one. One consequence worth knowing: with TLS on, `http://`
to that port gets no HTTP answer at all, not a redirect. Type `https://`.

### It leaves nothing behind

Ctrl-C, a closed terminal or a dropped SSH session stops both processes and
deletes `.demo/` — the database with the admin password hash, the config tree
with a private key per demo client, the certificate and the key it was serving,
and the seeded history all go with it. A run
killed with `-9`, or one that went down with the machine, leaves a directory
that the next run clears before it starts.

It gets its own directory rather than sharing `.dev/`, so interrupting a demo
never takes away the clients of whoever is using `make dev`.

### Reachable, and what that costs

`make demo` binds every interface, because a panel running on a VM cannot be
looked at from your own browser otherwise. Four things follow:

- **The debug page is off.** `make dev` leaves `AWG_PANEL_DEBUG=1`, which is
  fine when only the person who started it can connect; Django's debug page
  carries source, local variables and settings values, and is the most useful
  thing a panel could hand a stranger. WhiteNoise serves the built SPA either
  way, so nothing here needs it.
- **The password is generated per run**, never baked into the repository, so the
  target cannot ship the same credentials to every machine it runs on.
- **The wire is in the clear unless you say otherwise** — see above. When you
  do, `AWG_PANEL_TLS=1` goes with it and the rest of the panel agrees with the
  wire: `Secure` on both cookies, HSTS, every address the UI shows as `https`.
- **It is served by gunicorn**, not by `runserver`, which is what makes TLS
  possible at all and has the side effect of exercising the deployment shape
  `install-panel.sh` produces.

None of it makes the target safe on a public interface: a sandbox is a sandbox,
and with `DEMO_TLS=self` the certificate vouches for nobody. Put it on a network
you trust.

If you would rather not expose it at all, leave it on loopback and tunnel in —
which is also the case where plain HTTP costs nothing, since the wire is then
the ssh connection:

```bash
make demo DEMO_HOST=127.0.0.1
ssh -L 8099:127.0.0.1:8099 you@the-box      # from your own machine
```

### Reaching it from another machine

Django's CSRF check compares the `Origin` a browser sent against a trusted list,
so a login posted from `http://192.168.1.40:8099/` is refused unless that origin
is named. The target works this out from `hostname -I` and passes it as
`AWG_PANEL_TRUSTED_ORIGINS`, with the scheme following whether TLS was asked
for. It is also what `DEMO_TLS=self` issues the certificate for. If you reach
the panel by a **name** rather than an address, say so once and all of it
follows:

```bash
make demo DEMO_ADDRS='dev.example.internal 127.0.0.1 localhost'
```

Through a proxy, where the port a browser uses is not the port gunicorn holds,
the origins have to be given separately:

```bash
make demo DEMO_ORIGINS=https://dev.example.internal
```

Symptom of getting the origins wrong: the page loads, and signing in fails with
a CSRF error. Symptom of getting the certificate's addresses wrong: the browser
warns that the certificate is for somewhere else, which is a harder warning to
click past and in some browsers cannot be.

## The demo dataset

Seeded by `manage.py seeddemo`, which `make demo` runs for you. It refuses
outright unless `AWG_MOCK` is set — it rewrites the server config and deletes
both history tables, which is right for a sandbox and destructive on a box
carrying real clients.

Eleven clients, in two halves. Seven are written out because each shows
something in particular; four are generated from ranges to fill the table out:

| Client | What it is there for |
|---|---|
| `alice-laptop` | Weekdays only — a visible 5-on/2-off rhythm in the daily chart |
| `bob-phone` | A steady trickle, every day |
| `carol-tv` | Heavy, and younger than the retention window |
| `dave-old` | Stopped connecting in the spring — months of zeroes after real traffic |
| `erin-new` | Added last week — zeroes before it existed, bars after |
| `frank-unused` | Never moved a byte. Disabled |
| `grace-trial` | 2 MB for its whole life, beside neighbours in terabytes. Disabled |
| `henry-desktop`, `iris-ipad`, `jack-router`, `karen-phone` | Generated habits |

The two edge cases are **disabled deliberately**. The mock interface fabricates
traffic for every peer the kernel holds and the collector folds it in every
couple of seconds, so an enabled client seeded at 2 MB reads 40 MB by the time
you have logged in, and one seeded at zero never shows zero. A disabled peer is
left out of the configuration the interface is given, so its figures hold still.

Everything comes off one seeded generator, so **it is reproducible**: two people
running this are looking at the same panel, and a figure quoted from it is still
there tomorrow. `--seed` changes which panel that is.

The figures across the three stores are kept consistent, and monotonic:
all-time ≥ the twelve-month chart ≥ the thirty-day chart, and the dashboard's
"traffic today" card is the same row as the last bar of the daily chart. That is
not automatic — the history lives in the database and the all-time totals live
in `traffic.db`, which only the collector writes, so `seeddemo` writes both.

```bash
manage.py seeddemo --seed 42                 # a different panel, still reproducible
manage.py seeddemo --password hunter2        # instead of a generated one
manage.py seeddemo --username someone-else
```

## Every Makefile variable

Override on the command line: `make demo DEMO_PORT=9000`.

| Variable | Default | What it does |
|---|---|---|
| `PY` | `python3` | Interpreter used to build the virtualenv |
| `VENV` | `.venv` | Virtualenv location; targets prefer it over the system Python when it exists |
| `PYTEST_ARGS` | `-q` | Passed to pytest by `make test` |
| `DEV_DIR` | `.dev` | `make dev` sandbox — config tree and database |
| `API_PORT` | `8088` | `make dev` Django port |
| `UI_PORT` | `5173` | `make dev` Vite port |
| `DEMO_DIR` | `.demo` | `make demo` sandbox. Deleted on exit; refuses to be empty |
| `DEMO_PORT` | `8099` | The demo's only port |
| `DEMO_HOST` | `0.0.0.0` | Set `127.0.0.1` to keep the demo on loopback |
| `DEMO_ADDRS` | every address `hostname -I` reports, plus loopback | What `DEMO_ORIGINS` is built from, and what `DEMO_TLS=self` covers |
| `DEMO_ORIGINS` | `DEMO_ADDRS` as origins on `DEMO_PORT`, in `DEMO_SCHEME` | CSRF-trusted origins |
| `DEMO_PATH` | empty — the root | `random` for `/demo/<17 chars>/`, or a path to use literally |
| `DEMO_SECRET` | 17 random characters | Only set by `DEMO_PATH=random`; the generated segment |
| `DEMO_BASE_PATH` | `/` | Derived. The base path itself, one slash at each end |
| `DEMO_TLS` | empty — plain HTTP | `self` for a certificate per run, or the path to one you hold |
| `DEMO_TLS_KEY` | empty | The key, required when `DEMO_TLS` is a path |
| `DEMO_SCHEME` | `http`, or `https` when `DEMO_TLS` is set | Derived. What the printed URLs and `DEMO_ORIGINS` use |
| `DEV_ENV` / `DEMO_ENV` | see below | The whole environment each target runs under. Clear to target a real install |

## Every environment variable

The panel reads these directly, so they work with `manage.py` as well as through
the Makefile. On an installed server they come from `/etc/awg-panel.env`.

| Variable | Default | What it does |
|---|---|---|
| `AWG_MOCK` | unset | `1` swaps in the in-memory controller. Required by `seeddemo` |
| `AWG_IFACE` | `awg0` | The tunnel the panel manages; names the config file it reads |
| `AWG_CONF_DIR` | `/etc/amnezia/amneziawg` | Where `<iface>.conf`, `clients/` and `traffic.db` live |
| `AWG_PANEL_DATA` | `/var/lib/awg-panel` | Database and master key |
| `AWG_PANEL_RUN` | `/run/awg-panel` | Where the collector writes `live.json` |
| `AWG_PANEL_ENV` | `/etc/awg-panel.env` | Settings file the panel reads and rewrites |
| `AWG_PANEL_BASE_PATH` | `/` | Secret prefix the panel is mounted under |
| `AWG_PANEL_DEBUG` | off | Django debug. **Never on anything reachable** |
| `AWG_PANEL_LOG_LEVEL` | `DEBUG` with debug on, else `INFO` | Log verbosity |
| `AWG_PANEL_SECRET_KEY` | generated into `AWG_PANEL_DATA` | Django secret key |
| `AWG_PANEL_ALLOWED_HOSTS` | `*` | Comma-separated `Host` allowlist |
| `AWG_PANEL_TRUSTED_ORIGINS` | empty | Comma-separated CSRF-trusted origins, scheme included |
| `AWG_PANEL_TLS` | off | Marks cookies secure. On when the panel itself serves HTTPS. `1`, `true`, `yes` and `on` all mean on; a value that is none of those stops the service rather than being guessed at |
| `AWG_PANEL_TLS_CERT` / `AWG_PANEL_TLS_KEY` | empty | The pair gunicorn serves. Both required when `AWG_PANEL_TLS=1`, or it refuses to start |
| `AWG_PANEL_LISTEN` / `AWG_PANEL_PORT` | `0.0.0.0`, `2097` | What gunicorn binds. Read by `deploy/gunicorn.conf.py`, not by Django |
| `AWG_PANEL_WORKERS` / `AWG_PANEL_THREADS` | `2`, `4` | gunicorn's worker and thread counts. Same file, same caveat |
| `AWG_PANEL_ACCESS_LOG` | off | `1` sends gunicorn's access log to stdout |
| `AWG_PANEL_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Which peers may send `X-Forwarded-*`. Read by gunicorn and by the panel: headers from anyone else are dropped before Django sees them. Addresses, CIDR, or `*` |
| `AWG_PANEL_TRUST_PROXY` | off | Read `X-Forwarded-For` and `X-Forwarded-Proto` from the peers above. Only behind a proxy you control |
| `AWG_PANEL_UPDATE_REPO` | `achiliies/awg-panel` | GitHub repository the update check reads releases from. Anything that is not `owner/repo` turns the check off |
| `AWG_PANEL_UPDATE_URL` | empty | A whole release-feed URL, overriding the repository. Must be `https://` |
| `AWG_UPDATE_TOOL` | `/usr/local/bin/awg-update` | The updater the panel starts. Repointed by the tests |
| `AWG_UPDATE_DIR` | `$AWG_PANEL_DATA/update` | Where the updater keeps its state file and log |
| `AWG_UPDATE_FEED` | empty | A release description on local disk, read instead of asking GitHub. For an offline mirror; see [UPDATES.md](UPDATES.md) |

A dev run sets `AWG_PANEL_ENV` to a path that does not exist, deliberately: a
laptop that also has a panel installed must not drag `/etc/awg-panel.env` into a
sandbox.

## Tests, lint and the locks

```bash
make test                          # the whole suite
make test PYTEST_ARGS="-q -k traffic"
make lint                          # ruff check + format --check, changes nothing
make fmt                           # format and apply the safe fixes
```

The suite runs against the mock and needs no server. The frontend is checked
separately:

```bash
npm --prefix frontend run lint
npm --prefix frontend run format:check
npx --prefix frontend tsc -b frontend
```

`tests/test_api_contract.py` is the seam between the two: it reads
`frontend/src/api/types.ts` and asserts every field the SPA expects is really in
a live response, which nothing else covers.

Dependencies are pinned and hashed. `make lock` re-resolves them from the ranges
in `requirements.txt` and `pyproject.toml` — it needs network, and the result is
committed. `make wheels` downloads every wheel the locks name so an offline
bundle installs without PyPI.

## Pointing a target at a real installation

Clearing the sandbox environment makes a target use the real paths and
`/etc/awg-panel.env`:

```bash
sudo make migrate DEV_ENV=
```

Rarely what you want — `awg-panel` on the server drives the installed copy in
`/opt/awg-panel` and is the supported route. There is no `DEMO_ENV=` equivalent
worth using: `seeddemo` refuses without `AWG_MOCK`, which is the point of it.
