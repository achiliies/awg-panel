# Review findings

**A closed record, not a description of the code as it stands.** Everything here
was fixed at the time, and the tree has moved since: `bin/awg-client` was retired
and its jobs became `awg-panel manage` commands, and `tests/compat.sh` and
`tests/crosscheck.sh` went with it — both existed to hold that CLI and the
panel's Python core to the same file formats, and with one implementation left
there is nothing to cross-check. The file names in the tables below are where
each defect was, which is the useful thing about them; several of those files no
longer exist. Nothing on this page is open, and nothing on it needs acting on.

An adversarial review of the panel and the bash tools raised 23 defects. The
first pass fixed 11 of them; the remaining 12 were recorded here as leads,
unconfirmed, because a finding nobody wrote down is a finding nobody acts on.

Each of those has since been checked against the code by a reader who was asked
to refute it first. Two did not survive that; the other ten did, and are fixed
below. Nothing on this page is open.

## Confirmed and fixed in the second pass

| What | Where | Test |
|---|---|---|
| The collector re-folded its last dump into `traffic.db` on every cycle where the interface was unreadable. `awg.traffic` reads a raw value below the stored one as a counter reset, and the PreDown hook has always just stored a higher one — so a stop added the peer's **whole counter epoch** to its all-time total, permanently, and could trip a quota on bytes nobody sent | `apps/stats/management/commands/collector.py` | `panel/tests/test_collector.py` |
| `[Peer] # phone` was not read as a section header, so the block merged into the peer above it and the next panel write dropped one of the two clients. `awg-quick` cuts every line at the first `#` before looking for a bracket, and `bin/awg-client`'s awk matches on the prefix — the panel was the only one of the three reading it differently | `awg/conf.py` | `panel/tests/test_conf.py` |
| `AXES_IPWARE_PROXY_COUNT` did nothing, because `django-ipware` is deliberately not a dependency and django-axes silently falls back to `REMOTE_ADDR`. Behind the documented reverse proxy every failed login in the world shared one lockout bucket, so any stranger could lock the admin out of their own server | `awgui/settings.py`, `awgui/middleware.py` | `panel/tests/test_api_auth.py` |
| Restore wrote `awg0.conf` in place. `bin/awg-client` re-reads it on every command and `awg-quick` reads it at boot, neither under the lock a restore holds, so either could see a config with half its peers in it | `apps/panel/backup.py`, `awg/paths.py` | `panel/tests/test_api_settings.py` |
| `secret.key` was created empty and filled a moment later, so the web and collector units starting together on first boot could have one read the other's zero-byte file, keep a key of its own, and reject every session the other issued — with no state either could recover from | `awgui/settings.py` | — |
| Restore only stopped the panel when the archive happened to contain panel data. Restoring an older archive left the collector running, and it rewrites `traffic.db` every ten seconds and re-applies the config every minute, over what was just put back. It also extracted without the config lock | `bin/awg-menu` | — |
| First-run detection keyed on `/etc/awg-panel.env` while the account lives in `/var/lib/awg-panel`, so removing only the env file printed a fresh password that did not work — `seedadmin` is idempotent and had left the existing account alone | `panel/install-panel.sh` | — |
| An upgrade deleted the installed tree before the new one was proven, so a `pip install` that ran out of disk left nothing that could serve. Now staged, proven and swapped, with the previous tree and virtualenv put back on any failure | `panel/install-panel.sh` | — |
| `--endpoint` was ignored on an upgrade while the summary printed the value that had been asked for. It is honoured now, and the client configs are rebuilt so the change actually reaches the devices | `install.sh` | — |
| A client `PUT` that changed several things took the config lock once per store call, so a CLI change landing in a gap left half the edit applied and answered with a 404 about a name that no longer existed | `apps/clients/views.py` | `panel/tests/test_api_clients.py` |
| The config lock was held across the IMDS probe, up to six seconds on a box that black-holes the metadata address, and `awg syncconf` could hold it for the full 30s tool timeout — three times `bin/awg-client`'s `flock -w 10` budget | `awg/store.py`, `awg/controller.py` | `panel/tests/test_store.py`, `panel/tests/test_controller.py` |

## The pass over those fixes

The CIDR-subnet change and the ten fixes above were then read the same way, by
readers told to refute first and to run the code rather than reason about it.
Seventeen defects were reported, sixteen survived verification, and all sixteen
are fixed. The ones worth knowing about, because they are the shapes this
codebase keeps producing:

- **The collector fix above was half a fix.** It stopped a reading being folded
  twice, but not a reading taken *before* the interface went down being folded
  after `PreDown` had already written a better one — which is four polls in
  five, and the common case. A cycle that cannot read the interface now drops
  the pending reading, which is the same guard `bin/awg-client` has always had
  around its own fold.
- **mawk is the awk on the servers this targets**, and mawk 1.3.4 mis-evaluates
  an interval quantifier nested inside a repeated group. The octet regex in
  `bin/awg-menu` rejected roughly three quarters of legal addresses under it and
  passed under gawk, so the status screen named the server's own address as the
  network. Nothing in the subnet code uses `{n,m}` any more.
- **A bracketed line the header regex cannot read must still close the peer
  above it.** Tightening the regex to allow a trailing comment made
  `[Peer #2]` — which it now cannot match — fall through as an ordinary line,
  merging the block below into the peer above and deleting a client's only copy
  of its key on the next write. Anything that looks like a section is a section
  boundary now, whether or not it names one this understands.
- **Moving the tunnel has to move the split tunnel with it.** A client whose
  `AllowedIPs` is exactly the old network is not stating a preference, it is
  saying "the tunnel", and it followed nothing when the tunnel moved.
- **Holding the config flock across a SQLite write** makes `bin/awg-client` wait
  on a file it does not read. The metadata row is written after the lock is
  released.

## Checked and refuted

- **`awg/store.py` — the config lock held across `awg-quick`.** It is not, and
  must not be: `restart_iface` runs the tool outside the lock because
  awg-quick's own PreDown hook is `awg-client traffic sync`, which takes the
  same flock. Holding it there would deadlock the tunnel against its own hook.
  `panel/tests/test_store.py::test_restart_does_not_hold_the_config_lock`
  exists so that nobody "fixes" this. The IMDS half of the same finding was
  real and is in the table above.
- **`bin/awg-menu` — `iface_set` reporting success on a failed write.** Real
  when it was reported, fixed since; the port callers also refuse to move the
  firewall when the write did not land.

## Fixed and regression-tested in the first pass

| What | Where | Test |
|---|---|---|
| `emit_client_conf` truncated the client config before it could fail, destroying a private key that exists nowhere else | `bin/awg-client` | `tests/compat.sh` |
| `IFS=$'\t' read` swallowed an unnamed peer's empty name field, shifting the public key into the name | `bin/awg-client`, `bin/awg-menu` | `tests/compat.sh` |
| `awg-quick up` re-admitted disabled peers on every restart and at boot | `install.sh` PostUp hook, `awg-client enforce`, `store.restart_iface` | `tests/compat.sh` |
| A panel upgrade re-exposed a loopback-bound panel on `0.0.0.0` | `panel/install-panel.sh` | — |
| A listen-port change did not regenerate client configs and reported no re-import needed | `awg/store.py` | `panel/tests/` |
| Session and CSRF cookies were never `Secure` behind the documented reverse proxy | `awgui/settings.py` | — |
| The CSP blocked the panel's own bootstrap script, so the SPA never learned its base path | `awgui/views.py`, `awgui/middleware.py` | `panel/tests/test_spa_csp.py` |
| A missing VPN install returned 500 with a traceback instead of a handled 503 | `awg/errors.py`, `awgui/exceptions.py` | `panel/tests/test_api_not_configured.py` |
| The generated admin password was passed on a command line, visible in `/proc` | `panel/install-panel.sh` | — |
| `AWG_PANEL_TLS=1` with an empty certificate path silently served plain HTTP | `panel/deploy/gunicorn.conf.py` | — |
