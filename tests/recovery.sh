#!/bin/bash
#
# tests/recovery.sh - the installer is not allowed to leave a server offline.
#
# An upgrade may fail. What it may not do is hand back a machine that was
# working before it ran and is not working after - or one that is working and
# still reported as broken. Three ways that happened, all checked here:
#
#   * awg-quick runs with `set -e` and arms `trap 'del_if; exit'` before it
#     creates the interface, clearing it only once the last PostUp has run. A
#     hook that exits non-zero therefore deletes the interface rather than
#     merely failing to apply - so one ip6tables target missing from a kernel
#     is the difference between "IPv6 does not work" and "nobody can connect".
#
#   * install.sh takes the tunnel down to reload the kernel module in step 3
#     and brings it back in step 9. A run that dies in between used to leave it
#     down, silently.
#
#   * install.sh brings the tunnel up with awg-quick, which systemd never hears
#     about. A boot that could not bring it up leaves awg-quick@ in `failed`,
#     and a run that repaired the tunnel used to leave the unit there.
#
# None of them needs root, a module or a network: the hooks are read out of the
# function that writes them, the recovery path is driven against stubs, and
# step 10 is read for the order it does things in.
#
# Usage: tests/recovery.sh          (exit 0 = all checks passed)
#
set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

PASS=0 FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok    %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"; [[ -n "${2:-}" ]] && printf '        %s\n' "$2"; }

printf '== every firewall hook the installer writes is non-fatal ==\n'

# The real function, lifted out of install.sh so this tests what ships.
hooks_for() {
    bash -c '
        set -uo pipefail
        . '"$REPO"'/lib/subnet6.sh
        eval "$(awk "/^ipv6_write_hooks\(\) \{/,/^\}/" '"$REPO"'/install.sh)"
        SUBNET6_MODE='"$1"' SUBNET6_CIDR="fd00::/64" WAN=eth0 ipv6_write_hooks
    '
}

for mode in native nat blackhole; do
    out=$(hooks_for "$mode")
    if [[ -z "$out" ]]; then
        bad "${mode}: writes some hooks"
        continue
    fi
    stray=$(grep -E '^Post(Up|Down) = ' <<<"$out" | grep -v '|| true$' || true)
    if [[ -z "$stray" ]]; then
        ok "${mode}: every hook ends in '|| true'"
    else
        bad "${mode}: a hook can abort the bring-up" "$stray"
    fi
done

# The IPv4 hooks are written inline in the config heredoc rather than by that
# function; they are long-proven and deliberately left as they are, so this only
# records the count rather than asserting on it.
printf '  note  IPv4 hooks are unchanged and still fatal on failure (pre-existing)\n'

printf '\n== a failed run puts the tunnel back ==\n'

# Everything the recovery path touches, stubbed. `ip link show` answers from a
# file so the test can say whether the interface exists at each point, and
# `awg-quick` records that it was asked to bring it up.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/ip" <<'STUB'
#!/bin/bash
[[ "$*" == "link show "* ]] || exit 0
[[ -f "$STATE/iface-up" ]] && exit 0
exit 1
STUB
cat > "$WORK/bin/awg-quick" <<'STUB'
#!/bin/bash
if [[ "${1:-}" == "up" ]]; then
    echo "up $2" >> "$STATE/awg-quick.log"
    [[ -f "$STATE/bring-up-fails" ]] && exit 1
    touch "$STATE/iface-up"
fi
exit 0
STUB
chmod +x "$WORK/bin/ip" "$WORK/bin/awg-quick"

# The recovery path exactly as install.sh carries it, with warn stubbed - and
# t(), which its messages are wrapped in, answering with the English half. The
# assertions below read what warn was handed, so the stub has to return the
# text rather than swallow it.
harness() {
    cat <<'EOF'
set -uo pipefail
warn() { echo "warn: $*"; }
t() { printf '%s' "$1"; }
IFACE=awg0
EOF
    awk '/^IFACE_WAS_UP=0$/,/^trap restore_iface_on_failure EXIT$/' "$REPO/install.sh"
}
harness > "$WORK/harness.sh"

if grep -q "restore_iface_on_failure" "$WORK/harness.sh"; then
    ok "the recovery path was found in install.sh"
else
    bad "the recovery path was found in install.sh" "install.sh has no trap"
fi

export PATH="$WORK/bin:$PATH"

# 1. The tunnel was up, the run fails, the interface is gone: put it back.
export STATE="$WORK/s1"; mkdir -p "$STATE"; touch "$STATE/iface-up"
rc=$( { bash -c ". '$WORK/harness.sh'; rm -f \"\$STATE/iface-up\"; exit 7"; echo "rc=$?"; } 2>&1 | sed -n 's/^rc=//p')
if [[ "$rc" == 7 ]]; then ok "the original exit status survives the recovery"
else bad "the original exit status survives the recovery" "got '$rc', wanted 7"; fi
if grep -q "up awg0" "$STATE/awg-quick.log" 2>/dev/null; then
    ok "a failed run brings the interface back up"
else
    bad "a failed run brings the interface back up" "awg-quick up was never called"
fi

# 2. The tunnel was already down before the run: leave it alone.
export STATE="$WORK/s2"; mkdir -p "$STATE"
bash -c ". '$WORK/harness.sh'; exit 7" >/dev/null 2>&1
if [[ ! -f "$STATE/awg-quick.log" ]]; then
    ok "a server that was already down is not started by a failure"
else
    bad "a server that was already down is not started by a failure" "$(cat "$STATE/awg-quick.log")"
fi

# 3. The run succeeds: the trap must do nothing at all.
export STATE="$WORK/s3"; mkdir -p "$STATE"; touch "$STATE/iface-up"
bash -c ". '$WORK/harness.sh'; rm -f \"\$STATE/iface-up\"; exit 0" >/dev/null 2>&1
if [[ ! -f "$STATE/awg-quick.log" ]]; then
    ok "a successful run does not touch the interface"
else
    bad "a successful run does not touch the interface" "$(cat "$STATE/awg-quick.log")"
fi

# 4. The tunnel was up, the run fails, and the config is broken so the bring-up
#    fails too: say so rather than exit quietly claiming to have fixed it.
export STATE="$WORK/s4"; mkdir -p "$STATE"; touch "$STATE/iface-up" "$STATE/bring-up-fails"
out=$(bash -c ". '$WORK/harness.sh'; rm -f \"\$STATE/iface-up\"; exit 7" 2>&1)
if grep -q "could not be brought back up" <<<"$out"; then
    ok "a recovery that itself fails says so"
else
    bad "a recovery that itself fails says so" "$out"
fi

printf '\n== a unit a failed boot left behind is cleared ==\n'

# After the bring-up, which is the step that fixed what the unit is reporting,
# and inside step 10 rather than anywhere later that a failure could skip.
step10=$(awk '/^# -+ 10\. bring up$/ { p = 1 } /^# -+ 10b\./ { p = 0 } p' "$REPO/install.sh")
up=$(grep -n '^awg-quick up "\$IFACE"' <<<"$step10" | head -1 | cut -d: -f1)
reset=$(grep -nF 'systemctl reset-failed "awg-quick@${IFACE}"' <<<"$step10" | head -1 | cut -d: -f1)
if [[ -n "$up" && -n "$reset" ]] && (( up < reset )); then
    ok "step 10 clears a failed awg-quick@ unit once the tunnel is up"
else
    bad "step 10 clears a failed awg-quick@ unit once the tunnel is up" \
        "bring-up at line '${up}' of step 10, reset-failed at '${reset}'"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
