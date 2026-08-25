#!/bin/bash
#
# tests/power.sh - stopping the tunnel has to stop the tunnel.
#
# awg-menu's stop and start are driven here against stubs for systemctl,
# awg-quick and ip, because the substance of both is which of those gets run,
# and against what the machine says rather than against what it was assumed to
# say.
#
# The bug this exists for: install.sh runs `systemctl enable awg-quick@<iface>`
# and then brings the interface up with `awg-quick up`, and restart_iface does
# the same, so on an ordinary server the unit sits `enabled` and `inactive` over
# a tunnel that is running. `systemctl stop` against an inactive unit runs no
# ExecStop and exits 0 - a stop that reports success while every client stays
# connected, which is the one failure an operator has no way to see.
#
# Both directions are therefore judged on the interface, not on an exit code,
# and that is what is checked here: which tool ran, and what state the interface
# was left in.
#
# Usage: tests/power.sh          (exit 0 = all checks passed)
#
set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

PASS=0 FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok    %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"; [[ -n "${2:-}" ]] && printf '        %s\n' "$2"; }

# The real functions, lifted out of bin/awg-menu so this tests what ships. The
# block runs from the first of them to the screen that follows it.
lift() {
    awk '/^iface_present\(\) /,/^# -+ web panel$/' "$REPO/bin/awg-menu" \
        | grep -v '^# -\+ web panel$'
}
lift > "$WORK/power.sh"
grep -q '^stop_iface() {' "$WORK/power.sh" || {
    printf '  FAIL  could not lift stop_iface out of bin/awg-menu\n'; exit 1; }

# ------------------------------------------------------------------ stubs
STUB="$WORK/stub"
mkdir -p "$STUB"

cat > "$STUB/systemctl" <<'STUBEOF'
#!/bin/bash
printf 'systemctl %s\n' "$*" >> "$CALLS"
case "$1 ${3:-}" in
    "show LoadState")   printf '%s\n' "$UNIT_LOAD";   exit 0 ;;
    "show ActiveState") printf '%s\n' "$UNIT_ACTIVE"; exit 0 ;;
esac
running=0
case "$UNIT_ACTIVE" in active|activating|reloading|deactivating) running=1 ;; esac
case "$1" in
    reset-failed) exit 0 ;;
    # The heart of it: systemd runs ExecStop only for a unit it believes is up,
    # and ExecStart only for one it believes is down. Either way it exits 0.
    stop)  [[ $running == 1 && $RC == 0 ]] && rm -f "$LINK"
           [[ $RC == 0 ]] || echo "Job for $2 failed. See 'journalctl -xeu $2'." >&2
           exit "$RC" ;;
    start) [[ $running == 0 && $RC == 0 ]] && : > "$LINK"
           [[ $RC == 0 ]] || echo "Job for $2 failed. See 'journalctl -xeu $2'." >&2
           exit "$RC" ;;
esac
exit 0
STUBEOF

cat > "$STUB/awg-quick" <<'STUBEOF'
#!/bin/bash
printf 'awg-quick %s\n' "$*" >> "$CALLS"
case "$1" in
    up)   : > "$LINK" ;;
    down) [[ -e $LINK ]] || { echo "$2 is not a WireGuard interface" >&2; exit 1; }
          rm -f "$LINK" ;;
esac
exit 0
STUBEOF

cat > "$STUB/ip" <<'STUBEOF'
#!/bin/bash
[[ $1 == link && $2 == show ]] || exit 0
[[ -e $LINK ]]
STUBEOF

cat > "$STUB/journalctl" <<'STUBEOF'
#!/bin/bash
printf 'journalctl %s\n' "$*" >> "$CALLS"
printf '%s\n' "${JOURNAL:-}"
STUBEOF

chmod +x "$STUB"/*

# run <verb> <LoadState> <ActiveState> <iface up: 1|0> [rc]
#
# Prints the tool calls the run made, then a last line of "iface=up|down rc=N".
run() {
    local verb="$1" load="$2" active="$3" up="$4" rc="${5:-0}"
    rm -f "$WORK/calls" "$WORK/link"
    : > "$WORK/calls"
    [[ $up == 1 ]] && : > "$WORK/link"
    PATH="$STUB:$PATH" LINK="$WORK/link" CALLS="$WORK/calls" \
    UNIT_LOAD="$load" UNIT_ACTIVE="$active" RC="$rc" JOURNAL="${JOURNAL:-}" \
    bash -c '
        set -uo pipefail
        IFACE=awg0
        TMP='"$WORK"'
        # The two screens the failure paths reach for. A test has no terminal,
        # so what a failure would have shown is printed where it can be read.
        page()       { printf "PAGE %s\n" "$1"; sed "s/^/PAGE| /" "$2"; }
        strip_ansi() { cat; }
        . '"$WORK"'/power.sh
        rc=0
        '"${verb}"'_iface || rc=$?
        cat '"$WORK"'/calls
        printf "iface=%s rc=%s\n" \
            "$([[ -e '"$WORK"'/link ]] && echo up || echo down)" "$rc"
    '
}

# The calls that change something, without the questions asked to decide.
acted() { grep -v ' show \|^journalctl' <<<"$1" | grep '^systemctl\|^awg-quick'; }

expect() {
    local what="$1" want="$2" got="$3"
    if [[ "$got" == "$want" ]]; then ok "$what"
    else bad "$what" "wanted: ${want//$'\n'/ | }   got: ${got//$'\n'/ | }"; fi
}

printf '== stopping ==\n'

out=$(run stop loaded inactive 1)
expect "a unit systemd never started still loses its interface" \
    "awg-quick down awg0" "$(acted "$out")"
expect "  and the interface is gone afterwards" "iface=down rc=0" "$(tail -1 <<<"$out")"

out=$(run stop loaded active 1)
expect "a unit systemd is holding up is stopped through systemd" \
    "systemctl stop awg-quick@awg0" "$(acted "$out")"
expect "  and the interface is gone afterwards" "iface=down rc=0" "$(tail -1 <<<"$out")"

out=$(run stop loaded inactive 0)
expect "stopping a tunnel that is already down runs nothing" "" "$(acted "$out")"
expect "  and is not an error" "iface=down rc=0" "$(tail -1 <<<"$out")"

out=$(run stop not-found inactive 1)
expect "an AmneziaWG with no unit is stopped with awg-quick" \
    "awg-quick down awg0" "$(acted "$out")"

out=$(run stop loaded active 1 1)
expect "a stop systemd could not finish is reported, not swallowed" \
    "iface=up rc=1" "$(tail -1 <<<"$out")"

printf '== starting ==\n'

out=$(run start loaded inactive 0)
expect "a start goes through the unit, so the next stop can too" \
    "systemctl start awg-quick@awg0" "$(acted "$out")"
expect "  and the interface is there afterwards" "iface=up rc=0" "$(tail -1 <<<"$out")"

# RemainAfterExit=yes leaves the unit "active" after an awg-quick down behind
# its back, and `systemctl start` on an active unit runs nothing.
out=$(run start loaded active 0)
expect "a start systemd would sleep through still brings the interface up" \
    "$(printf 'systemctl start awg-quick@awg0\nawg-quick up awg0')" "$(acted "$out")"
expect "  and the interface is there afterwards" "iface=up rc=0" "$(tail -1 <<<"$out")"

out=$(run start loaded active 1)
expect "starting a tunnel that is already up runs nothing" "" "$(acted "$out")"

out=$(run start loaded failed 0)
if grep -q '^systemctl reset-failed awg-quick@awg0$' <<<"$out"; then
    ok "a unit parked in failed is cleared before it is started"
else
    bad "a unit parked in failed is cleared before it is started" "${out//$'\n'/ | }"
fi

JOURNAL="Line unrecognized: \`Jc=4'" out=$(JOURNAL="Line unrecognized: \`Jc=4'" run start loaded inactive 0 1)
if grep -q "PAGE| Line unrecognized" <<<"$out"; then
    ok "a failed start shows what the unit logged, not just that it failed"
else
    bad "a failed start shows what the unit logged, not just that it failed" "${out//$'\n'/ | }"
fi
if grep -q 'journalctl _SYSTEMD_UNIT=awg-quick@awg0.service' <<<"$out"; then
    ok "the journal is matched on the unit rather than asked for with -u"
else
    bad "the journal is matched on the unit rather than asked for with -u" "${out//$'\n'/ | }"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
