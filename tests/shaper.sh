#!/bin/bash
#
# tests/shaper.sh - the shaping commands have to be ones the kernel accepts.
#
# panel/tests/test_shaper.py asserts which commands awg.shaper builds, with tc
# faked, because that is all that can be checked on a CI runner with no root and
# no tunnel. It cannot check the half that matters most on a real server:
# whether the kernel takes them. A u32 hashkey read at the wrong header offset,
# an htb class hung under a parent that does not exist, a handle written in the
# wrong base - every one of those passes a string comparison and fails on the
# box, and fails there as a client whose traffic quietly stops being classified
# rather than as anything that looks like an error.
#
# So this drives the real module against real interfaces and then asks the
# kernel what it ended up with. It runs in a network namespace of its own, so
# nothing it does can reach the host's own qdiscs - which matters more here than
# in most tests, because the thing under test attaches to the WAN interface and
# the developer's box is reachable over it.
#
# Root is needed to make a namespace, and for nothing else.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PANEL="$HERE/../panel"
NS="awg-shaper-test"
TUN="shtun0"
WAN="shwan0"

R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; N=$'\e[0m'
fail=0

note() { printf '%s\n' "$*"; }
bad()  { printf '%s✗%s %s\n' "$R" "$N" "$*"; fail=1; }
good() { printf '%s✓%s %s\n' "$G" "$N" "$*"; }

if [[ $EUID -ne 0 ]]; then
    printf '%sThis needs root%s - it makes a network namespace. Run:\n\n' "$Y" "$N"
    printf '    sudo %s\n\n' "${BASH_SOURCE[0]}"
    exit 2
fi

for tool in ip tc; do
    command -v "$tool" >/dev/null || { bad "$tool is not installed (iproute2)"; exit 1; }
done

PYTHON="$PANEL/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"
[[ -x "$PYTHON" ]] || { bad "no python3 to drive the module with"; exit 1; }

cleanup() { ip netns del "$NS" >/dev/null 2>&1; }
trap cleanup EXIT

# A namespace left over from a run that died would make every check below a lie
# about state this one did not create.
ip netns del "$NS" >/dev/null 2>&1

note "== a namespace with a stand-in tunnel and a stand-in WAN =="
ip netns add "$NS" || { bad "could not create the namespace"; exit 1; }
ip -n "$NS" link add "$TUN" type dummy || { bad "could not create $TUN"; exit 1; }
ip -n "$NS" link add "$WAN" type dummy || { bad "could not create $WAN"; exit 1; }
ip -n "$NS" link set "$TUN" up
ip -n "$NS" link set "$WAN" up
ip -n "$NS" addr add 10.13.0.1/20 dev "$TUN"
ip -n "$NS" addr add 10.13.13.129/25 dev "$TUN"
ip -n "$NS" -6 addr add fd00:1:2:3::1/64 dev "$TUN" nodad
good "$TUN and $WAN are up in $NS"

note
note "== the module builds it, the kernel keeps it =="
# PYTHONPATH rather than a cd, so the driver is importable wherever this is run
# from. AWG_MOCK is cleared explicitly: a developer with it exported would
# otherwise get a pass out of a shaper that refused to do anything at all.
#
# PYTHONDONTWRITEBYTECODE because this half runs as root and the rest of the
# repository does not. Importing awg.shaper leaves a __pycache__ beside it owned
# by root, which nothing afterwards can delete or overwrite - the next ordinary
# `pytest` cannot refresh it, and `git worktree remove` refuses to take the tree
# down at all. Not writing it is better than deleting it afterwards: there is
# nothing to clean up, nothing to get wrong about which files were ours, and no
# `rm -rf` running as root inside a source tree.
if env -u AWG_MOCK PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PANEL" \
     ip netns exec "$NS" "$PYTHON" "$HERE/shaper_check.py" "$TUN" "$WAN"; then
    good "every command was accepted and every readback agreed"
else
    bad "see above"
fi

note
if [[ $fail -eq 0 ]]; then
    printf '%sAll shaper checks passed.%s\n' "$G" "$N"
else
    printf '%sShaper checks failed.%s\n' "$R" "$N"
fi
exit $fail
