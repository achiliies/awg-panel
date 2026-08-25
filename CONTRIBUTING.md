# Contributing

Patches, bug reports and questions are all welcome. This file covers the parts
that are specific to this repository; [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)
covers getting the panel running, which is the bulk of what you need.

## Before you start

Open an issue first for anything larger than a fix. Much of this repository is
constrained by things that are not visible from the code: the Amnezia client's
`.conf` importer silently drops several obfuscation parameters, dependencies are
pinned and hashed on purpose, and the installer has to survive being re-run on a
server carrying live clients. A short exchange before the work saves rewriting
it afterwards.

Bug reports are more useful with the server's Ubuntu/Debian version, the output
of `awg-menu` diagnostics, and what you expected instead.

## Running it

Nothing here needs a VPN server, a kernel module or root:

```bash
cd panel
make venv          # .venv from the pinned, hashed locks
make dev           # API on mock data + the Vite dev server
```

`AWG_MOCK=1` fabricates an interface, its peers and their counters, and every
developer target writes to a throwaway config tree rather than
`/etc/amnezia/amneziawg`, so nothing you run in a checkout can reach a real
server by accident. [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) has the rest:
`make demo`, every Makefile and environment variable, and how to point a target
at a real installation when you genuinely need to.

The shell half needs no setup — `install.sh`, `lib/` and `bin/` are plain bash,
and the checks under `tests/` run directly.

## What CI will run

Run these before opening a pull request; CI runs the same things and nothing
else:

```bash
# shell
shellcheck -x -S warning install.sh build.sh fetch-sources.sh get.sh tests/*.sh \
  panel/install-panel.sh panel/lock-deps.sh panel/fetch-wheels.sh lib/*.sh \
  bin/awg-menu bin/awg-uninstall bin/awg-panel
tests/hooks.sh                     # and the other tests/*.sh you touched

# panel
cd panel
make test
make lint                          # ruff; `make fmt` applies the safe fixes
npm --prefix frontend run lint
npm --prefix frontend run format:check
npx --prefix frontend tsc -b frontend
```

`tests/shaper.sh` needs root, because it asks the kernel to accept the shaping
commands rather than trusting that it would. The rest do not.

If you change a Python dependency, change the range in `requirements.txt` or
`pyproject.toml` and re-resolve with `make lock` — never edit a `.lock` by hand.
The locks carry a sha256 per package and `tests/deps.sh` checks they still say
what they claim.

## Commits

Look at `git log` before writing a message; the existing history is the
standard.

- **Subject:** one line, imperative, under ~72 characters, no trailing period
  and no `type:` prefix. Say what the change does for whoever uses this, not
  which files moved — "Stop an IPv4-only server's upgrade dying before it gains
  IPv6", not "fix(install): add v6 guard".
- **Body:** wrapped at 72 columns, and it is the point of the message. Explain
  the problem that existed, why it happened, and why this is the fix — the
  reasoning that will not be recoverable from the diff in a year. Say what you
  deliberately did not do, and why, when there was a real choice.

One branch per piece of work, named plainly with no prefixes or slashes:
`panel-icons`, `session-timeout`. Keep it rebased on `main`.

## Licensing

This project is [AGPL-3.0](LICENSE), and contributions are accepted under the
same terms — opening a pull request means you are licensing your work that way
and that you have the right to. There is no CLA and no copyright assignment; you
keep the copyright in what you write.

The AGPL matters more than usual here because the panel is a network service.
Anyone who runs a modified version of it for other people has to offer those
people its source, whether or not they ever distribute a copy — see section 13
of the license. A plain GPL would let a modified panel be hosted as a closed
service, which is the case worth covering for software like this.

The AmneziaWG kernel module and tools that `install.sh` builds are upstream's
work and carry their own licenses; nothing here changes them.
