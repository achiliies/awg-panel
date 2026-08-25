#!/bin/bash
#
# tests/panel.sh - panel_env_set writes the value it was given.
#
# /etc/awg-panel.env is the single source of truth for where the panel listens,
# what path it answers on and which certificate it serves. Four callers write to
# it: the menu's port, bind-address and secret-path actions, and lib/acme.sh
# after it has obtained or dropped a certificate.
#
# The function used to write through `sed -i "s|^${key}=.*|${key}=${val}|"`, and
# the replacement half of an s expression is not a literal. `&` stands for the
# whole match, `\1` for a group, a backslash is eaten, and a `|` closes the
# expression and leaves sed reading the rest of the value as flags. install.sh
# has already lost an upgrade to exactly that - env_set_kv carries the note - so
# what is checked here is not that today's callers happen to be safe. It is that
# the next caller does not have to know.
#
# Nothing here needs root or a panel. The env file is a temporary one.
#
# Usage: tests/panel.sh          (exit 0 = all checks passed)
#
set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

PASS=0 FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }
is()  { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "got '$2', wanted '$3'"; fi; }

TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT

# Pointed at the temporary file before the library is sourced: PANEL_ENV is
# resolved at source time, and a test run must not be able to write to a real
# installation on the machine it happens to be running on.
export AWG_PANEL_ENV="$TMPD/awg-panel.env"
# shellcheck source=lib/panel.sh
. "$REPO/lib/panel.sh"

# A fresh env file in the shape panel/install-panel.sh writes one.
fixture() {
    cat > "$AWG_PANEL_ENV" <<'ENV'
AWG_IFACE=awg0
AWG_PANEL_LISTEN=0.0.0.0
AWG_PANEL_PORT=2097
AWG_PANEL_BASE_PATH=/awg/k2p9x4mt7wq1bz8n5rv3/
AWG_PANEL_TLS=0
AWG_PANEL_TLS_CERT=
AWG_PANEL_TLS_KEY=
ENV
    chmod 600 "$AWG_PANEL_ENV"
}

# The line as it stands in the file, with nothing stripped. panel_env_get is not
# used for this: it deletes quote characters, which is the one thing that would
# hide a value having been mangled on the way in.
line_for() { sed -n "s/^${1}=//p" "$AWG_PANEL_ENV"; }

# --------------------------------------------------------------- the ordinary
printf '== the ordinary case ==\n'
fixture
panel_env_set AWG_PANEL_PORT 8443
is "an existing key takes the new value"  "$(line_for AWG_PANEL_PORT)" "8443"
is "the file is still seven lines"        "$(wc -l < "$AWG_PANEL_ENV")" "7"
is "the line next to it is untouched"     "$(line_for AWG_PANEL_LISTEN)" "0.0.0.0"
is "and so is the one before it"          "$(line_for AWG_IFACE)" "awg0"

panel_env_set AWG_PANEL_WORKERS 4
is "a key that was not there is appended" "$(line_for AWG_PANEL_WORKERS)" "4"
is "and the file grew by exactly one"     "$(wc -l < "$AWG_PANEL_ENV")" "8"

fixture
panel_env_set AWG_PANEL_TLS_CERT ""
is "a value can be cleared"               "$(line_for AWG_PANEL_TLS_CERT)" ""
is "clearing it does not remove the line" \
   "$(grep -c '^AWG_PANEL_TLS_CERT=$' "$AWG_PANEL_ENV")" "1"

# ------------------------------------------------------------- prefixes
#
# lib/acme.sh sets AWG_PANEL_TLS and AWG_PANEL_TLS_CERT one after the other, and
# the first is a prefix of the second. A match on the key alone would rewrite
# whichever line came first and leave the panel with a certificate path of "1".
printf '\n== one key is a prefix of another ==\n'
fixture
panel_env_set AWG_PANEL_TLS 1
is "the shorter key took the value"       "$(line_for AWG_PANEL_TLS)" "1"
is "the longer key was left alone"        "$(line_for AWG_PANEL_TLS_CERT)" ""
panel_env_set AWG_PANEL_TLS_CERT /etc/letsencrypt/live/vpn.example.org/fullchain.pem
is "the longer key took its own value" \
   "$(line_for AWG_PANEL_TLS_CERT)" "/etc/letsencrypt/live/vpn.example.org/fullchain.pem"
is "and the shorter one still reads 1"    "$(line_for AWG_PANEL_TLS)" "1"

# ------------------------------------------------------------ what sed ate
#
# Every one of these is a value sed's replacement half would have changed or
# choked on. None of them is a value a caller passes today, which is the point:
# the guarantee has to hold before somebody needs it, not after.
printf '\n== the value is never syntax ==\n'
for val in \
    'a&b' \
    '&' \
    'a|b' \
    'a\1b' \
    'a\b' \
    'a\nb' \
    'C:\path\to\cert' \
    '/etc/ssl/a&b\c|d' \
    'a"b' \
    "a'b" \
    'a b' \
    '  leading and trailing  ' \
    '$HOME' \
    '`id`' \
    '%s' \
    '/awg/../etc/'
do
    fixture
    if panel_env_set AWG_PANEL_TLS_CERT "$val"; then
        is "kept verbatim: ${val}" "$(line_for AWG_PANEL_TLS_CERT)" "$val"
    else
        bad "kept verbatim: ${val}" "panel_env_set returned non-zero"
    fi
    # A value that broke the write would take the rest of the file with it.
    is "  ...and the file survived it" "$(line_for AWG_IFACE)" "awg0"
done

# ------------------------------------------------------------- duplicates
printf '\n== a key that appears twice ==\n'
fixture
printf 'AWG_PANEL_PORT=9999\n' >> "$AWG_PANEL_ENV"
panel_env_set AWG_PANEL_PORT 2098
is "leaves once"                          "$(grep -c '^AWG_PANEL_PORT=' "$AWG_PANEL_ENV")" "1"
is "with the value that was asked for"    "$(line_for AWG_PANEL_PORT)" "2098"

# ---------------------------------------------------------- the other writer
#
# panel/install-panel.sh writes the same file, and it cannot source this
# library: it runs on a box that has no checkout on it, which is why there are
# two copies of the base-path generator as well (tests/basepath.sh says the
# same thing at more length). Its env_put was the copy that still went through
# `sed -i "s|^${key}=.*|${key}=${val}|"` after panel_env_set was moved off it,
# so a `|` or an `&` in a value broke the install rather than the panel.
#
# Extracted rather than reimplemented here, so the checks cannot go on passing
# against a copy of the function that no longer resembles the one that ships.
printf '\n== install-panel.sh writes the same file ==\n'
INSTALLER="$REPO/panel/install-panel.sh"
INST_ENV="$TMPD/installer-env.sh"
sed -n '/^ENV_SAFE=/,/^}/p' "$INSTALLER" > "$INST_ENV"
if [[ ! -s "$INST_ENV" ]] || ! grep -q '^env_put()' "$INST_ENV"; then
    echo "  FAIL  could not find env_put in panel/install-panel.sh" >&2
    exit 1
fi

# The installer's own die and t, cut down to what the function uses. die exits
# there and only returns here, which is why env_put has to refuse by returning
# rather than by trusting die never to come back.
inst_put() (
    # shellcheck disable=SC2034  # read by the extracted env_put, not by this file
    ENV_FILE="$AWG_PANEL_ENV"
    die() { printf 'die: %s\n' "$1" >&2; return 1; }
    t()   { printf '%s' "$1"; }
    # shellcheck source=/dev/null
    . "$INST_ENV"
    env_put "$1" "$2"
)

fixture
inst_put AWG_PANEL_PORT 8443
is "an existing key takes the new value"  "$(line_for AWG_PANEL_PORT)" "8443"
is "the file is still seven lines"        "$(wc -l < "$AWG_PANEL_ENV")" "7"
inst_put AWG_PANEL_WORKERS 4
is "a key that was not there is appended" "$(line_for AWG_PANEL_WORKERS)" "4"

# The prefix case again, because this writer had it too: AWG_PANEL_TLS is a
# prefix of AWG_PANEL_TLS_CERT, and the panel reading a certificate path of "1"
# is a service that does not come back up.
fixture
inst_put AWG_PANEL_TLS 1
is "the shorter key took the value"       "$(line_for AWG_PANEL_TLS)" "1"
is "the longer key was left alone"        "$(line_for AWG_PANEL_TLS_CERT)" ""

# A value bash would read as syntax is refused outright rather than written and
# sourced. install-panel.sh sources this file itself a few lines after writing
# it, both units take it as an EnvironmentFile, and the panel's own save path
# checks the same expression (apps/panel/defaults.py, _ENV_SAFE) - so the one
# writer that did not check it was the way around every other one.
printf '\n== a value bash would run is refused ==\n'
for val in \
    '$(id)' \
    '`id`' \
    'a|b' \
    'a&b' \
    'a b' \
    'a;id' \
    'a$HOME' \
    "a'b" \
    'a"b' \
    'a\b'
do
    fixture
    if inst_put AWG_PANEL_TLS_CERT "$val" 2>/dev/null; then
        bad "refused: ${val}" "env_put accepted it"
    else
        ok "refused: ${val}"
    fi
    is "  ...and the line is unchanged" "$(line_for AWG_PANEL_TLS_CERT)" ""
    is "  ...and nothing was left beside the file" \
       "$(find "$TMPD" -name 'awg-panel.env.*' | wc -l)" "0"
done

# What the installer actually passes, none of which may be caught by the above.
printf '\n== what the installer passes is not refused ==\n'
fixture
for pair in \
    'AWG_IFACE=awg0' \
    'AWG_CONF_DIR=/etc/amnezia/amneziawg' \
    'AWG_PANEL_DATA=/var/lib/awg-panel' \
    'AWG_PANEL_LISTEN=0.0.0.0' \
    'AWG_PANEL_LISTEN=::1' \
    'AWG_PANEL_PORT=2097' \
    'AWG_PANEL_BASE_PATH=/awg/k2p9x4mt7wq1bz8n5rv3/' \
    'AWG_PANEL_TLS_CERT=/etc/letsencrypt/live/vpn.example.org/fullchain.pem' \
    'AWG_PANEL_UPDATE_REPO=owner/repo' \
    'DJANGO_SETTINGS_MODULE=awgui.settings'
do
    if inst_put "${pair%%=*}" "${pair#*=}" 2>/dev/null; then
        is "accepted: ${pair}" "$(line_for "${pair%%=*}")" "${pair#*=}"
    else
        bad "accepted: ${pair}" "env_put refused a value the installer passes"
    fi
done

# The whole point of the expression: the file is sourced, and what comes back
# out of it has to be what went in.
fixture
inst_put AWG_PANEL_BASE_PATH '/awg/k2p9x4mt7wq1bz8n5rv3/'
# shellcheck disable=SC1090  # the file under test, written a line above
SOURCED=$( set -a; . "$AWG_PANEL_ENV"; printf '%s' "$AWG_PANEL_BASE_PATH" )
is "and the file survives being sourced"  "$SOURCED" "/awg/k2p9x4mt7wq1bz8n5rv3/"

# --------------------------------------------------------------- the file
printf '\n== the file itself ==\n'
fixture
panel_env_set AWG_PANEL_PORT 8443
is "stays readable only by its owner"     "$(stat -c '%a' "$AWG_PANEL_ENV")" "600"

rm -f "$AWG_PANEL_ENV"
panel_env_set AWG_PANEL_PORT 8443
is "a missing env file is refused"        "$?" "1"
is "and is not created by the attempt"    "$([[ -e "$AWG_PANEL_ENV" ]] && echo yes || echo no)" "no"
# The temporary file is made beside the real one, so a failed write must not
# leave a half-written copy of the panel's settings sitting in /etc.
is "and leaves nothing behind"            "$(find "$TMPD" -name 'awg-panel.env.*' | wc -l)" "0"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
