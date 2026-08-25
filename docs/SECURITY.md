# Security model

This document describes what the installer, the shell tools and the web panel
can reach, what they store, and where the sharp edges are. Read it before
putting the panel on a public address.

## Trust model in one paragraph

Everything here runs as root on the VPN server and shares one directory,
`/etc/amnezia/amneziawg`, which holds the server's private key and every
client's private key in plaintext. Anyone who can read that directory can
impersonate the server and every client. Anyone who can log into the web panel
can read it too, because issuing a client config *is* handing out a private key.
The panel's admin password is therefore equivalent to root over the VPN, and
should be treated that way.

## What runs with what privilege

| Component | Runs as | Why it needs it |
|---|---|---|
| `install.sh` | root | builds and installs a kernel module, writes `/etc`, manages systemd and the firewall |
| `awg-menu`, `awg-panel` | root (re-exec via sudo) | every path they touch is root-owned 0600; `awg-quick` manipulates interfaces and iptables |
| `awg-panel-web` (gunicorn) | root | reads and rewrites the 0600 configs, runs `awg syncconf` and `awg-quick` |
| `awg-panel-collector` | root | reads kernel counters via `awg show`, rewrites `traffic.db`, enforces quotas |

`CAP_NET_ADMIN` alone is not enough: the tools also need to read and write
0600 root-owned files and to shell out to `awg-quick`, which itself expects
root. The systemd units restrict what they can reach instead —
`NoNewPrivileges`, `ProtectHome`, `ProtectSystem=full`, `PrivateTmp`, and
`ReadWritePaths` limited to `/etc/amnezia`, `/var/lib/awg-panel`,
`/etc/awg-panel.env` and `/run`. A certificate the units cannot reach is copied
into `/etc/ssl/awg-panel/` rather than the sandbox being opened up to reach it.

## Where secrets live

| Secret | Location | Mode |
|---|---|---|
| Server private key | `/etc/amnezia/amneziawg/<iface>.conf` | 0600 |
| Client private keys and PSKs | `/etc/amnezia/amneziawg/clients/<name>.conf` | 0600 |
| Imported TLS private key | `/etc/ssl/awg-panel/panel.key` | 0600 |
| Panel admin password | `/var/lib/awg-panel/db.sqlite3`, Argon2 hash | 0600 dir 0700 |
| Panel database sidecars | `/var/lib/awg-panel/db.sqlite3-wal`, `-shm` | 0600 dir 0700 |
| Panel session/CSRF signing key | `/var/lib/awg-panel/secret.key` | 0600 |
| TOTP seeds | `/var/lib/awg-panel/db.sqlite3` | as above |

Rules the code follows, and which a change must not break:

- Private keys and preshared keys are never written to a log, never included in
  an error message, and never returned by a list or status endpoint. They appear
  only in the client-config download and QR endpoints, which is their purpose.
- `GET /api/v1/server` redacts the server private key; only its public key is
  exposed.
- Every file the tools create is written to a temp file in the same directory
  with mode 0600 and then renamed, so a reader never sees a half-written config
  and a key never briefly exists world-readable.
- `awg-menu`'s config viewer filters `PrivateKey` before displaying it, because
  people screenshot that screen.

## Backups contain everything

A backup, made from the panel or fetched from `GET api/v1/backup`, is a
`.tar.gz` of `/etc/amnezia/amneziawg` plus `/var/lib/awg-panel`. That includes
the server private key, every client private key, the panel's password hash and
the hash of every API token — restoring one brings back the tokens that existed
when it was taken, including any revoked since.

Nothing of it is left on the server: it is built into a 0600 temp file under the
unit's own `PrivateTmp`, streamed to the caller, and unlinked. Where it lands is
therefore wherever the browser or the script put it, and that is the copy to
look after. Treat the file exactly like the server's private key: store it
encrypted, and delete copies you no longer need.

Restoring replaces the current configuration entirely, including the server
key — every client config issued since the backup stops working.

## The web panel

**Authentication.** Argon2 password hashing, Django sessions with
`HttpOnly`, `SameSite=Strict` cookies (and `Secure` when TLS is on), CSRF
tokens required on every mutation, and `django-axes` locking an IP out for 15
minutes after 5 failed attempts. TOTP two-factor is optional and recommended;
if the device is lost, reset it from the server shell with
`sudo awg-panel reset-2fa`.

**Password policy.** There is none: `AUTH_PASSWORD_VALIDATORS` is empty, so any
password the account's owner picks is accepted, by the settings page and by
`awg-panel passwd` alike. The panel has a single account belonging to the person
who administers the server, and rules refusing their choice would be protecting
them from themselves; the lockout above and the second factor are what stand
between a weak password and an attacker. A deployment that wants a floor back
adds validators to that setting in `panel/awgui/settings.py` — every path that
sets a password runs `validate_password` against it, so nothing else changes.

**API tokens.** Anything that is not a browser authenticates with a bearer token
instead of the password — issued on the panel's API page, named, and
optionally given an expiry. Treat one exactly like the password: it authenticates
as the account and can change the server. What it cannot do is change the
credentials that authorise it — no route under `settings/account`,
`settings/2fa`, `settings/sessions` or `settings/tokens` will answer a token, and
neither will a backup **restore**, because the archive carries the password hash
and the token table with it. So a leaked token cannot have quietly minted a
replacement for itself, which is what makes revoking one worth doing at all.

`POST update/apply` is refused a token too, for a reason none of those share. It
installs a release fetched over the internet, replacing every line of code on the
server including the rules in this paragraph, so pressing it is not an
administrative action on the panel's data — it decides which software the box
runs next. That is a decision for a person at the login form. Reading `GET
update/status` is allowed: watching a deployment is a fair thing for the script
that started it, and it discloses a version number and a build log.

What that release is checked against matters as much as who may ask for it. The
bundle is downloaded over HTTPS and its SHA-256 compared with the `SHA256SUMS`
published beside it in the same release; a mismatch, an absent checksum, or a
file that is not a bundle stops the update before anything is executed. The feed
naming the release must itself be `https://` — a plaintext `AWG_PANEL_UPDATE_URL`
is ignored — because the bytes it eventually points at are run as root.
[UPDATES.md](UPDATES.md) has the whole sequence.

Downloading a backup is allowed, and that is the limit of the rule above rather
than an exception to it. The archive carries `db.sqlite3` and the panel's signing
key, and that database holds the session table — where a session key *is* the
`awgsessionid` cookie and not a hash of it — beside the TOTP secret and the
password hash. Anything that has fetched one archive can set that cookie and is
then signed in as the account, past every `403` in the paragraph above. So the
honest reading is that a token which can reach `GET backup` **is** the account,
and if one leaks after it has fetched an archive, deleting the row is not the
end of it: change the password, sign every other browser out, and treat every
private key on the server as copied. The download is not silent — Activity
records it under the token's own name, which is what makes "has this one ever
fetched a backup" a question with an answer.

The panel stores a SHA-256 of each token and never the token itself, so the
secret is shown once at creation and is not recoverable from the database or
from a backup of it; a lost one is replaced rather than looked up. There is no
lockout on a wrong token and none is needed: it is 256 bits from the system's
random source, so there is nothing to guess.
Nothing is written to the activity log for a refused one, deliberately — that
path can be driven by anyone, and an audit trail an outsider can grow at will is
not an audit trail. Every successful use *is* recorded, in the token's own row,
as the time and the address it was last presented from.

**What the activity log records.** Statistics and logs → Activity keeps a row
per administrative action: the account that made it — or the API token, by name —
the address the request came from, what it was about, and a handful of named
values — never key material, never a password, never a whole request. A refused sign-in is a row
too, carrying the username that was typed, which is a claim rather than an
account. So the table holds admin addresses and attempted usernames, and it
lives in `db.sqlite3`, which means it is in every backup. Rows are swept after
180 days, with a 20 000-row ceiling behind that so a hammered login page cannot
grow the file without bound. Client IP addresses are **not** recorded: the panel
logs what was done to a client, not where that client connected from.

**Clearing it.** The log can be emptied from the panel, which is the reasonable
thing to be able to do with a table that holds addresses and attempted
usernames — and it is not a silent operation. The clear itself is recorded, so
what is left behind is one row naming the account, the address and how many
events went, rather than a blank page that could equally mean nothing ever
happened. An API token is refused: a token is the credential handed out on the
understanding that it can be taken back, and that only holds while the panel can
be asked afterwards what it did. The same caveat as everywhere on this page
applies — a token that can fetch a backup has the database and everything in it,
including this table — so the rule keeps the ledger out of a script's ordinary
reach rather than out of an attacker's.

**The secret base path.** The panel is served under `/awg/` followed by a
random segment, such as `/awg/k2p9x4mt7wq1bz8n5rv3/`. Anything outside it
returns a bare 404 with no hint that a panel exists. The `/awg/` in front is
fixed and public — it tells a scanner walking it what kind of thing is hiding
behind, so all 20 characters of the guessing work sit in the second segment
and none of it in the first. This keeps the login page out of internet-wide
scans and the resulting credential-stuffing traffic. It is defence in depth
and nothing more — the path travels in the URL, appears in browser history and
in any proxy log, and is not a substitute for a strong password.

**Transport.** The panel speaks plain HTTP unless TLS is configured. On a
public address that means the admin password and session cookie cross the
network in the clear. Do one of:

- terminate TLS in gunicorn — set the certificate and key in Settings → TLS,
  which sets `AWG_PANEL_TLS=1` in `/etc/awg-panel.env`;
- put nginx or Caddy in front, bind the panel to `127.0.0.1`
  (`--panel-listen 127.0.0.1`) and let the proxy hold the certificate;
- or do not expose it at all — bind to localhost and reach it over an SSH
  tunnel: `ssh -L 2097:127.0.0.1:2097 server`, then open
  `http://127.0.0.1:2097/<path>/`.

Once either of the first two is set up, plain HTTP stops being answered. With
the certificate in gunicorn the port speaks nothing else; behind a proxy, where
gunicorn still listens in the clear, the panel checks the scheme of every
request and redirects a page to HTTPS while refusing anything that writes. That
last part is what stops a proxy misconfiguration, or a connection made straight
to the panel's own port, from serving the login form in cleartext.

**Firewall.** `install-panel.sh` opens the panel's TCP port in an active ufw or
firewalld. Cloud security groups are not reachable from the server and stay
manual — which also means the default on AWS and similar is closed, and you
have to open it deliberately.

**Content security.** The SPA is served with a strict CSP
(`default-src 'self'`), `X-Frame-Options: DENY`, `Referrer-Policy:
same-origin`, and HSTS when TLS is enabled. Nothing is loaded from a CDN, so
the panel works on an air-gapped network and cannot be affected by a third-party
script.

## Shared-state hazards

The panel is the only thing that writes `/etc/amnezia/amneziawg`, which removes
most of this hazard rather than managing it: there is no second tool whose edit
could undo a revocation. What is left is that one writer is not one process —
two gunicorn workers with several threads each, plus the collector — and that
the files are still ordinary files anyone with root can edit. Two guarantees
cover both:

1. **One lock.** Every mutation takes an exclusive `flock` on
   `/etc/amnezia/amneziawg/.lock`, with a 30-second timeout. Two concurrent
   client creations cannot be handed the same address, and a config can never be
   half-written while something else reads it — which matters beyond the panel's
   own processes, because `awg-quick` reads that file at boot without taking any
   lock at all. Every write lands through a temp file and a rename for the same
   reason.
2. **Nothing is trusted that was not re-read.** The panel keeps an indexed copy
   of the config for speed, and treats it as a cache and never as the truth:
   every request stats `awg0.conf`, `clients/` and `traffic.db`, and rebuilds
   the copy if any of them has moved. A hand edit over SSH, a restore, or a
   `PreDown` hook writing counters is therefore picked up rather than silently
   overwritten.

The one deliberate exception is quota and expiry enforcement, which the
collector re-asserts once per cycle. A peer that crosses its limit can stay live
for up to one poll interval (2 seconds by default) before being removed, and a
bring-up re-admits every disabled peer until the config's `PostUp` hook —
`awg-panel manage enforce` — takes them off again.

## Obfuscation is not confidentiality

The `Jc`/`S`/`H`/`I` parameters change what the traffic *looks like* to a
network observer. They are a censorship-resistance feature, not an extra layer
of encryption — WireGuard's cryptography is what protects the payload, and it is
unchanged. A wrong obfuscation parameter degrades reachability, never secrecy.

They are also not secret, and are not meant to be. Every one of them is copied
into every client config, and those files are emailed, screenshotted and pasted
into group chats — assume an adversary who wants them has them. What protects a
server is not that its profile is unknown but that it is *unshared*: a profile
this project shipped to everybody would be one filter rule away from matching
every install at once, so the whole set is drawn per server and none of it is a
default. Getting hold of one server's configs teaches an adversary nothing about
the next server, and that is the property being defended.

The corollary is what **Reconfigure** is for. If a config has gone somewhere it
should not have, the profile in it is spent: draw a new one, hand out the new
client files, and the old ones stop being useful to anybody.

The reverse matters too: `HeaderProtectionKey`, `ContentPaddingAddition` and the
timer overrides are supported by this server but silently dropped by the Amnezia
mobile app's `.conf` importer. What that costs depends on which one. A client
without the header protection key cannot read a protected header at all, so it
fails to handshake with no error message at either end. The padding and the
timers are each end's own business: a client that never received them keeps the
protocol's defaults and the tunnel still works, but only the end that has them
set is hiding anything. The panel marks these fields accordingly and leaves them
unset unless an admin turns them on deliberately.

## Reporting a problem

**A vulnerability goes to [Security → Report a vulnerability][advisory], not to
the issue tracker.** An issue is public from the moment it is filed, and every
server running this software is a server somebody else is relying on: a report
filed in the open is a working exploit handed to anyone watching the repository
before there is a release to update to. A private advisory is read by the
maintainers only, and becomes public when a fix exists.

[advisory]: https://github.com/achiliies/awg-panel/security/advisories/new

Say what an attacker gets and how to reproduce it, and name the version —
`awg-panel version`, or the panel's Settings page. Expect an answer within a
week. Once a fix is released the advisory is published with credit, unless you
would rather it was not.

Anything that is not a vulnerability — a crash, a wrong number on a page, an
install that stops — belongs in an ordinary issue.

Either way: **do not attach a config file, a backup archive, a `.conf` a client
imported or anything else carrying a key.** Every one of those is a private key
in plaintext, and a backup archive is all of them at once. A log excerpt with
the keys cut out of it says as much and costs nobody their tunnel.
