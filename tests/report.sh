#!/bin/bash
#
# tests/report.sh - the credentials install-panel.sh hands back to install.sh.
#
# The two scripts are separate processes, so the account name and the password
# that were settled in one have to reach the summary the other prints several
# steps later. They travel through a file: panel/install-panel.sh writes it,
# install.sh reads it and deletes it.
#
# install.sh matched each line against a list of known keys and then eval'd it,
# which is sourcing one line at a time - the key was checked and everything
# after the "=" was still run as root. The file sits in a 0700 root-owned
# directory, so nothing short of root could ever have put a line there and this
# was not a way in; but the comment above it says the file is parsed rather
# than sourced precisely because "a file written by one script and read by
# another is a file that can be replaced by a third", and that has to be true
# rather than nearly true.
#
# So the values are base64 now. What is checked here is both halves of that: a
# password with quotes, spaces and a `$(...)` in it comes back byte for byte,
# and a line that is not what the writer would have written is dropped instead
# of run.
#
# Both sides are extracted from the scripts they live in, so neither can drift
# away from the other while this goes on passing.
#
# Usage: tests/report.sh          (exit 0 = all checks passed)
#
set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
INSTALLER="$REPO/panel/install-panel.sh"
MAIN="$REPO/install.sh"

PASS=0 FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }
is()  { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "got '$2', wanted '$3'"; fi; }

TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT

# ---------------------------------------------------------------- extraction
WRITER="$TMPD/writer.sh"
READER="$TMPD/reader.sh"
sed -n '/^report_put()/,/^}/p' "$INSTALLER" > "$WRITER"
sed -n '/^PANEL_REPORT_USER="" PANEL_REPORT_PASS=""$/,/^fi$/p' "$MAIN" > "$READER"
[[ -s "$WRITER" ]] || { echo "  FAIL  no report_put in panel/install-panel.sh" >&2; exit 1; }
[[ -s "$READER" ]] || { echo "  FAIL  no report reader in install.sh" >&2; exit 1; }
# shellcheck source=/dev/null
. "$WRITER"

PANEL_REPORT="$TMPD/report"
# The marker every check below looks for: if any part of the file is ever
# executed rather than decoded, this file appears and says so.
CANARY="$TMPD/executed"

read_report() {
    PANEL_REPORT_USER="" PANEL_REPORT_PASS=""
    PANEL_REPORT_CREATED=0 PANEL_REPORT_RESET=0
    # shellcheck source=/dev/null
    . "$READER"
}

# --------------------------------------------------------- the ordinary case
printf '== what the installer writes comes back ==\n'
: > "$PANEL_REPORT"
{
    report_put PANEL_REPORT_USER    "kj38fhq2mx"
    report_put PANEL_REPORT_PASS    "Vb7-tq2N_kd93Pxs"
    report_put PANEL_REPORT_CREATED 1
    report_put PANEL_REPORT_RESET   0
} > "$PANEL_REPORT"
read_report
is "the account name"     "$PANEL_REPORT_USER" "kj38fhq2mx"
is "the password"         "$PANEL_REPORT_PASS" "Vb7-tq2N_kd93Pxs"
is "the created flag"     "$PANEL_REPORT_CREATED" "1"
is "the reset flag"       "$PANEL_REPORT_RESET" "0"

# A key the reader does not know is ignored rather than assigned: the writer
# puts three more in the file than the summary has ever read.
: > "$PANEL_REPORT"
{
    report_put PANEL_REPORT_USER      "kj38fhq2mx"
    report_put PANEL_REPORT_BASE_PATH "/awg/k2p9x4mt7wq1bz8n5rv3/"
    report_put PANEL_REPORT_PORT      "2097"
    report_put PANEL_REPORT_LISTEN    "0.0.0.0"
} > "$PANEL_REPORT"
read_report
is "keys the summary does not use are skipped" "$PANEL_REPORT_USER" "kj38fhq2mx"

# ----------------------------------------------------------- awkward values
#
# The password is generated, but $AWG_PANEL_ADMIN_PASSWORD lets an operator
# supply their own, and there is nothing to stop that one holding any of this.
printf '\n== a password survives whatever is in it ==\n'
for val in \
    'p@ss w0rd' \
    "it's mine" \
    'say "hello"' \
    'a$(id)b' \
    'a`id`b' \
    'a|b&c;d' \
    'a\b\c' \
    '  spaces at both ends  ' \
    'наш пароль' \
    'ᛖᛗᛟᛃᛁ 🔑' \
    '$HOME/%s/${x}' \
    '=====' \
    'a
b'
do
    : > "$PANEL_REPORT"
    report_put PANEL_REPORT_PASS "$val" > "$PANEL_REPORT"
    read_report
    is "round-trips: ${val}" "$PANEL_REPORT_PASS" "$val"
done

# ------------------------------------------------------------ a hostile file
#
# Every one of these was a command the old reader ran. The check is not only
# that nothing executes - it is that the value is dropped, so the summary says
# the password could not be read rather than printing something made up.
printf '\n== a line nobody wrote is not obeyed ==\n'
for line in \
    "PANEL_REPORT_PASS=\$(touch ${CANARY})" \
    "PANEL_REPORT_PASS=\`touch ${CANARY}\`" \
    "PANEL_REPORT_USER=x\$(touch ${CANARY})" \
    "PANEL_REPORT_CREATED=a[\$(touch ${CANARY})]" \
    "PANEL_REPORT_RESET=1;touch ${CANARY}" \
    "PANEL_REPORT_PASS='literal quotes'" \
    "PANEL_REPORT_PASS=plain-but-not-base64!"
do
    rm -f "$CANARY"
    printf '%s\n' "$line" > "$PANEL_REPORT"
    read_report
    if [[ -e "$CANARY" ]]; then
        bad "not executed: ${line}" "the line ran"
    else
        ok "not executed: ${line}"
    fi
    is "  ...and nothing was assigned" \
       "${PANEL_REPORT_USER}${PANEL_REPORT_PASS}${PANEL_REPORT_CREATED}${PANEL_REPORT_RESET}" "00"
done

# The counts reach `(( ))`, which is an interpreter of its own: an arithmetic
# context expands a command substitution inside an array subscript. They are
# read as text and then required to be digits.
printf '\n== the counts are digits or they are nothing ==\n'
rm -f "$CANARY"
: > "$PANEL_REPORT"
report_put PANEL_REPORT_CREATED "a[\$(touch ${CANARY})]" > "$PANEL_REPORT"
read_report
is "a decoded count that is not a number is refused" "$PANEL_REPORT_CREATED" "0"
(( PANEL_REPORT_CREATED )) || true
if [[ -e "$CANARY" ]]; then bad "and the arithmetic does not run it" "the canary appeared"
else                        ok  "and the arithmetic does not run it"; fi

: > "$PANEL_REPORT"
report_put PANEL_REPORT_RESET 1 > "$PANEL_REPORT"
read_report
(( PANEL_REPORT_RESET )) && ok "a real count still counts" || bad "a real count still counts" "got '$PANEL_REPORT_RESET'"

# ------------------------------------------------------------- an empty file
printf '\n== nothing to read ==\n'
: > "$PANEL_REPORT"
read_report
is "an empty file leaves the defaults" \
   "${PANEL_REPORT_USER}|${PANEL_REPORT_PASS}|${PANEL_REPORT_CREATED}|${PANEL_REPORT_RESET}" "||0|0"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
