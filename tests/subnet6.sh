#!/bin/bash
#
# tests/subnet6.sh - the bash half of the IPv6 parity check.
#
# Reads the corpus tests/subnet6_check.py generates, runs every spelling
# through lib/subnet6.sh, and hands the answers back for comparison against
# awg/subnet6.py and against the standard library. Nothing here decides what is
# correct; it only makes the bash side answer, so the checker can hold the two
# implementations to the same string.
#
# Usage: tests/subnet6.sh          (exit 0 = the two sides agree)
#
set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PANEL="$REPO/panel"

PY="$PANEL/.venv/bin/python"
[[ -x "$PY" ]] || PY=$(command -v python3)
[[ -x "$PY" ]] || { echo "skip: no python3" >&2; exit 0; }
[[ -f "$PANEL/awg/subnet6.py" ]] || { echo "skip: panel/awg/subnet6.py is not present" >&2; exit 0; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# needs_ipv6() works in IPv4 address arithmetic, so it needs ip2int from the
# v4 module; every tool that sources one sources both.
# shellcheck source=lib/subnet.sh
. "$REPO/lib/subnet.sh"
# shellcheck source=lib/subnet6.sh
. "$REPO/lib/subnet6.sh"

echo "== generating the corpus =="
"$PY" "$REPO/tests/subnet6_check.py" generate "$WORK" || exit 1

mapfile -t OFFSETS < "$WORK/offsets.txt"

echo "== running it through lib/subnet6.sh =="
: > "$WORK/accepted.tsv"
while IFS=$'\t' read -r spelling expected; do
    if parse_cidr6 "$spelling"; then
        line="${spelling}"$'\t'"${expected}"$'\t'"$(subnet6_cidr)"$'\t'"$(subnet6_server_addr)"
        for offset in "${OFFSETS[@]}"; do
            line+=$'\t'"$(subnet6_host_addr "$offset")"
        done
        printf '%s\n' "$line" >> "$WORK/accepted.tsv"
    else
        printf '%s\t%s\tPARSE_FAIL\t-\n' "$spelling" "$expected" >> "$WORK/accepted.tsv"
    fi
done < "$WORK/accept.tsv"

: > "$WORK/rejected.tsv"
while IFS= read -r value; do
    if parse_cidr6 "$value"; then
        printf '%s\tACCEPTED:%s\n' "$value" "$SUBNET6_PREFIX" >> "$WORK/rejected.tsv"
    else
        printf '%s\tREJECTED\n' "$value" >> "$WORK/rejected.tsv"
    fi
done < "$WORK/reject.txt"

: > "$WORK/routes-out.tsv"
while IFS= read -r route; do
    if needs_ipv6 "$route"; then verdict=NEEDS; else verdict=LEAVE; fi
    printf '%s\t%s\t%s\n' "$route" "$verdict" "$(with_ipv6 "$route")" >> "$WORK/routes-out.tsv"
done < "$WORK/routes.txt"

echo "== comparing with awg/subnet6.py =="
PYTHONPATH="$PANEL" "$PY" "$REPO/tests/subnet6_check.py" verify "$WORK" || exit 1

# ------------------------------------------------------------- host detection
#
# The half that decides whether a server leaks. Which mode an install lands in
# follows entirely from what these read off the host, so they are driven here
# against a stubbed `ip` rather than against whatever the machine running the
# tests happens to have - the interesting cases are the VPS shapes this
# machine is not.

PASS=0 FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }
is()  { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "got '$2', wanted '$3'"; fi; }

STUB="$WORK/stub"
mkdir -p "$STUB"
cat > "$STUB/ip" <<'STUB'
#!/bin/bash
# Enough of iproute2 for lib/subnet6.sh: addresses, per-device routes, default.
case "$*" in
    *"-o addr show dev"*)  cat "$FIXTURE/addrs" 2>/dev/null ;;
    *"route show dev"*)    cat "$FIXTURE/routes" 2>/dev/null ;;
    *"route show default"*) cat "$FIXTURE/default" 2>/dev/null ;;
esac
exit 0
STUB
chmod +x "$STUB/ip"
export PATH="$STUB:$PATH"

scenario() {
    FIXTURE="$WORK/fixture"
    export FIXTURE
    rm -rf "$FIXTURE"; mkdir -p "$FIXTURE"
    : > "$FIXTURE/addrs"; : > "$FIXTURE/routes"; : > "$FIXTURE/default"
}
addr()  { printf '2: eth0    inet6 %s scope global \\       valid_lft forever\n' "$1" >> "$FIXTURE/addrs"; }
route() { printf '%s proto kernel metric 256 pref medium\n' "$1" >> "$FIXTURE/routes"; }
defrt() { printf '%s\n' "$1" >> "$FIXTURE/default"; }

# What install.sh's auto branch decides, in one place so the test exercises the
# same rule the installer applies.
pick_mode() {
    local iface="$1" block cidr
    if block=$(ipv6_routed_block "$iface") && cidr=$(ipv6_carve "$block" "$iface"); then
        printf 'native %s' "$cidr"
    elif ipv6_host_online "$iface"; then
        printf 'nat %s' "$(subnet6_ula)"
    else
        printf 'blackhole %s' "$(subnet6_ula)"
    fi
}

printf '\n== a routed /48, the shape that can be carved ==\n'
scenario
addr "2a0a:8dc0:70b5::b/48"; addr "2a0a:8dc0:7000:b5::2/126"
route "2a0a:8dc0:70b5::/48"; route "2a0a:8dc0:7000:b5::/126"; route "fe80::/64"
defrt "default via 2a0a:8dc0:7000:b5::1 dev eth0 proto static metric 1024"
is "the /48 is found, not the /126 transfer network" "$(ipv6_routed_block eth0)" "2a0a:8dc0:70b5::/48"
is "a /64 is carved clear of the host's own subnet"  "$(ipv6_carve "$(ipv6_routed_block eth0)" eth0)" "2a0a:8dc0:70b5:1::/64"
is "the mode is native"                              "$(pick_mode eth0)" "native 2a0a:8dc0:70b5:1::/64"
is "a static default route needs no accept_ra fix"   "$(ipv6_default_from_ra && echo ra || echo static)" "static"

printf '\n== a single on-link /64, the ordinary VPS ==\n'
scenario
addr "2a01:4f8:1:2::2/64"
route "2a01:4f8:1:2::/64"; route "fe80::/64"
defrt "default via fe80::1 dev eth0 proto ra metric 1024"
is "nothing to carve from"           "$(ipv6_routed_block eth0 || echo none)" "none"
is "the mode is nat, on a ULA"        "$(pick_mode eth0)" "nat $(subnet6_ula)"
is "an RA default route needs the accept_ra fix" "$(ipv6_default_from_ra && echo ra || echo static)" "ra"

printf '\n== no IPv6 at all ==\n'
scenario
is "nothing to carve from"    "$(ipv6_routed_block eth0 || echo none)" "none"
is "the host is not online"   "$(ipv6_host_online eth0 && echo yes || echo no)" "no"
is "the mode is blackhole"    "$(pick_mode eth0)" "blackhole $(subnet6_ula)"

printf '\n== a routed /56 ==\n'
scenario
addr "2a02:1234:5678:ab00::1/56"
route "2a02:1234:5678:ab00::/56"
defrt "default via fe80::1 dev eth0 proto static metric 1024"
is "the /56 is found"                     "$(ipv6_routed_block eth0)" "2a02:1234:5678:ab00::/56"
is "the carve stays inside the /56"       "$(ipv6_carve "2a02:1234:5678:ab00::/56" eth0)" "2a02:1234:5678:ab01::/64"

printf '\n== the host already occupies the first two /64s ==\n'
scenario
addr "2a0a:8dc0:70b5::1/48"; addr "2a0a:8dc0:70b5:1::1/48"
route "2a0a:8dc0:70b5::/48"
defrt "default via fe80::1 dev eth0"
is "the carve skips both"  "$(ipv6_carve "2a0a:8dc0:70b5::/48" eth0)" "2a0a:8dc0:70b5:2::/64"

printf '\n== a unique-local block on the egress interface is not ours to carve ==\n'
scenario
addr "fd12:3456:789a::1/48"
route "fd12:3456:789a::/48"
is "a ULA block is not treated as routed" "$(ipv6_routed_block eth0 || echo none)" "none"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
