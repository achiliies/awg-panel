#!/bin/bash
#
# tests/ports.sh - the port probe the installer warns off a busy port with.
#
# Two ports are chosen during an install and either of them can already belong
# to something else: the panel's TCP port, and the tunnel's UDP one. Neither
# failure says so afterwards - gunicorn writes "address already in use" into a
# journal nobody is reading yet, and awg-quick leaves an interface that never
# answers a packet - so the warning at the moment the number is chosen is the
# only place an admin is told.
#
# Which makes a probe that quietly matched nothing indistinguishable from a
# machine where every port happens to be free. Real sockets are bound here for
# that reason, rather than ss being faked.
#
# The last check is the one that is not about ports at all. Every caller writes
# `holder=$(port_holder ...)` under `set -euo pipefail`, so a non-zero return
# from it ends the install - and a host without iproute2 is exactly where that
# would happen, on the machine least able to explain why.
#
# Usage: tests/ports.sh             (exit 0 = all checks passed)
#
set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=lib/common.sh
. "$REPO/lib/common.sh"

PASS=0 FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }
is()  { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "got '$2', wanted '$3'"; fi; }
busy() { port_busy "$1" "$2" && echo yes || echo no; }

command -v ss >/dev/null || { echo "skip: iproute2 is not installed"; exit 0; }
command -v python3 >/dev/null || { echo "skip: no python3 to bind sockets with"; exit 0; }

# Sockets held open by a helper for as long as the checks take, on ports picked
# high enough to be nobody else's. Both protocols at once, on one number: that
# a TCP listener does not make the UDP port busy is half of what is being
# tested, and a second number would let a stale answer pass for a right one.
PORT=39917
FREE=39918
python3 - "$PORT" <<'EOF' &
import socket, sys, time

port = int(sys.argv[1])
tcp = socket.socket()
tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
tcp.bind(("127.0.0.1", port))
tcp.listen(1)
udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp.bind(("127.0.0.1", port))
print("bound", flush=True)
time.sleep(30)
EOF
HELPER=$!
trap 'kill "$HELPER" 2>/dev/null' EXIT
# The sockets are bound before the first check, or every one of them passes for
# the wrong reason.
for _ in $(seq 1 50); do
    [[ "$(busy tcp "$PORT")" == yes ]] && break
    sleep 0.1
done

printf '== a port that is held ==\n'
is "a bound TCP port is busy" "$(busy tcp "$PORT")" "yes"
is "a bound UDP port is busy" "$(busy udp "$PORT")" "yes"
# awg-quick's own failure never names the process, and neither does a tunnel
# that comes up and answers nothing. This is the whole value of the warning.
HOLDER=$(port_holder udp "$PORT")
if [[ -n "$HOLDER" ]]; then
    ok "the process holding it is named ($HOLDER)"
else
    bad "the process holding it is named" "port_holder said nothing"
fi

printf '\n== a port that is not ==\n'
is "an unbound TCP port is free" "$(busy tcp "$FREE")" "no"
is "an unbound UDP port is free" "$(busy udp "$FREE")" "no"
is "nothing is named for a free port" "$(port_holder tcp "$FREE")" ""

printf '\n== one number, two ports ==\n'
# 443/tcp is a website and 443/udp is where somebody wants their tunnel. A
# probe that confused the two would refuse the ports most worth choosing.
kill "$HELPER" 2>/dev/null
wait "$HELPER" 2>/dev/null
python3 - "$PORT" <<'EOF' &
import socket, sys, time

tcp = socket.socket()
tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
tcp.bind(("127.0.0.1", int(sys.argv[1])))
tcp.listen(1)
print("bound", flush=True)
time.sleep(30)
EOF
HELPER=$!
for _ in $(seq 1 50); do
    [[ "$(busy tcp "$PORT")" == yes ]] && break
    sleep 0.1
done
is "TCP is held" "$(busy tcp "$PORT")" "yes"
is "the same number on UDP is free" "$(busy udp "$PORT")" "no"

printf '\n== a prefix is not a port ==\n'
# ss is given a filter rather than a pattern to grep, so 3991 must not answer
# for 39917. A substring match here would report half the machine as busy.
is "a prefix of a busy port is free" "$(busy tcp "${PORT:0:4}")" "no"

printf '\n== refusal policy: post-answer check ==\n'
# Slices the post-answer check out of install.sh so this tests what ships.
POST_CHECK_CODE=$(awk '/5b'\''\. ports already held/,/5c\. the IPv6 side/' "$REPO/install.sh")
[[ -n "$POST_CHECK_CODE" && "$POST_CHECK_CODE" == *"port_busy"* ]] || {
    echo "FAIL: could not extract post-answer check from install.sh" >&2
    exit 1
}

run_post_check() {
    local p_udp="$1" p_tcp="$2" existing="${3:-0}" panel_live="${4:-}" path_env="${5:-}"
    local script="
        # Matches install.sh's own flags: exercising the extracted snippet under
        # -e is what catches a non-zero return ending the run, which is the failure
        # mode this harness exists to hold in place.
        set -euo pipefail
        . '$REPO/lib/common.sh'
        EXISTING=$existing
        PORT='$p_udp'
        PANEL_PORT='$p_tcp'
        panel_env_get() { printf '%s\n' '$panel_live'; }
        $POST_CHECK_CODE
        echo survived
    "
    if [[ -n "$path_env" ]]; then
        "$(command -v bash)" -c "export PATH='$path_env'; $script" 2>&1
    else
        "$(command -v bash)" -c "$script" 2>&1
    fi
}

# Rebind both TCP and UDP on PORT so both refusal paths can be exercised.
kill "$HELPER" 2>/dev/null
wait "$HELPER" 2>/dev/null
python3 - "$PORT" <<'EOF' &
import socket, sys, time

port = int(sys.argv[1])
tcp = socket.socket()
tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
tcp.bind(("127.0.0.1", port))
tcp.listen(1)
udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp.bind(("127.0.0.1", port))
print("bound", flush=True)
time.sleep(30)
EOF
HELPER=$!
for _ in $(seq 1 50); do
    [[ "$(busy tcp "$PORT")" == yes && "$(busy udp "$PORT")" == yes ]] && break
    sleep 0.1
done

# A held UDP port must abort the install immediately with die.
out=$(run_post_check "$PORT" "$FREE" 0 "") && rc=0 || rc=$?
if [[ $rc -ne 0 && "$out" == *"${PORT}/udp"* ]]; then
    ok "post-answer check dies on an occupied UDP tunnel port"
else
    bad "post-answer check dies on an occupied UDP tunnel port" "rc=$rc output: $out"
fi

# A held TCP port for the panel must abort the install when moving onto it.
out=$(run_post_check "$FREE" "$PORT" 0 "") && rc=0 || rc=$?
if [[ $rc -ne 0 && "$out" == *"${PORT}/tcp"* ]]; then
    ok "post-answer check dies on an occupied TCP panel port"
else
    bad "post-answer check dies on an occupied TCP panel port" "rc=$rc output: $out"
fi

# Unoccupied ports must allow the install to proceed without error.
out=$(run_post_check "$FREE" "$FREE" 0 "") && rc=0 || rc=$?
if [[ $rc -eq 0 && "$out" == *survived* ]]; then
    ok "post-answer check proceeds when chosen ports are free"
else
    bad "post-answer check proceeds when chosen ports are free" "rc=$rc output: $out"
fi

# An upgrade keeping an existing tunnel configuration is exempt from the UDP check.
out=$(run_post_check "$PORT" "$FREE" 1 "") && rc=0 || rc=$?
if [[ $rc -eq 0 && "$out" == *survived* ]]; then
    ok "post-answer check exempts existing tunnel port on upgrade"
else
    bad "post-answer check exempts existing tunnel port on upgrade" "rc=$rc output: $out"
fi

# An upgrade keeping the panel on its current port is exempt from the TCP check.
out=$(run_post_check "$FREE" "$PORT" 0 "$PORT") && rc=0 || rc=$?
if [[ $rc -eq 0 && "$out" == *survived* ]]; then
    ok "post-answer check exempts unchanged panel port on upgrade"
else
    bad "post-answer check exempts unchanged panel port on upgrade" "rc=$rc output: $out"
fi

printf '\n== a host with no iproute2 ==\n'
# PATH stripped to a directory holding everything but ss, which is a real
# minimal image and not a hypothetical one. "Cannot tell" is the answer; ending
# the install is not.
STUB=$(mktemp -d)
trap 'kill "$HELPER" 2>/dev/null; rm -rf "$STUB"' EXIT
# dirname because common.sh sources i18n.sh beside itself, sed and head because
# port_holder pipes through them. Everything else the library needs is a
# builtin, and ss is the one thing deliberately left out.
for tool in dirname sed head; do ln -s "$(command -v "$tool")" "$STUB/$tool"; done
OUT=$("$(command -v bash)" -euo pipefail -c "
    export PATH='$STUB'
    . '$REPO/lib/common.sh'
    port_busy udp 53 && echo busy || echo free
    holder=\$(port_holder udp 53)
    echo \"holder=[\$holder]\"
    echo survived
" 2>&1)
case "$OUT" in
    *survived*) ok "port_holder does not end a run under set -e when ss is missing" ;;
    *)          bad "port_holder does not end a run under set -e when ss is missing" "$OUT" ;;
esac
case "$OUT" in
    free*) ok "an unanswerable port reads as free rather than as busy" ;;
    *)     bad "an unanswerable port reads as free rather than as busy" "$OUT" ;;
esac

# A missing ss tool must never cause the post-answer check to abort the install.
out=$(run_post_check "$PORT" "$PORT" 0 "" "$STUB") && rc=0 || rc=$?
if [[ $rc -eq 0 && "$out" == *survived* ]]; then
    ok "post-answer check proceeds when ss is missing instead of refusing"
else
    bad "post-answer check proceeds when ss is missing instead of refusing" "rc=$rc output: $out"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
