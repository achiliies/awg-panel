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

# ------------------------------------------------ IPv6 switched off in the kernel
#
# Against a directory standing in for / rather than this machine's own /proc,
# /sys and /etc, for the same reason as the stubbed ip above. Each case starts
# from a stock kernel and changes one thing. reasons is what install.sh acts on,
# one reason to a line, joined here with "|" so that a case reads on one line.
ROOT="$WORK/root"
export AWG_ROOT_DIR="$ROOT"
sysroot() {
    rm -rf "$ROOT"
    mkdir -p "$ROOT/proc/sys/net/ipv6/conf/default" "$ROOT/sys/module/ipv6/parameters" \
             "$ROOT/etc/sysctl.d"
    printf '%s\n' "${1:-0}" > "$ROOT/proc/sys/net/ipv6/conf/default/disable_ipv6"
    printf '0\n' > "$ROOT/sys/module/ipv6/parameters/disable"
    printf '0\n' > "$ROOT/sys/module/ipv6/parameters/disable_ipv6"
}
param()   { printf '%s\n' "$2" > "$ROOT/sys/module/ipv6/parameters/$1"; }
sysfile() { mkdir -p "$(dirname "$ROOT$1")"; cat > "$ROOT$1"; }
reasons() { local r; r=$(ipv6_off_reasons awg0) || r=none; printf '%s' "${r//$'\n'/|}"; }

printf '\n== a stock kernel ==\n'
sysroot
is "has IPv6"                              "$(ipv6_in_kernel && echo yes || echo no)" "yes"
is "and nothing keeps the tunnel from it"  "$(reasons)" "none"

printf '\n== a kernel without IPv6 ==\n'
sysroot; rm -rf "$ROOT/proc/sys/net/ipv6"; param disable 1
is "booted with ipv6.disable=1"            "$(reasons)" "kernel disabled"
sysroot; rm -rf "$ROOT/proc/sys/net/ipv6" "$ROOT/sys/module/ipv6"
is "built or loaded without it"            "$(reasons)" "kernel absent"
sysroot; rm -rf "$ROOT/proc/sys/net/ipv6"; param disable 1
sysfile /etc/sysctl.conf <<<'net.ipv6.conf.all.disable_ipv6 = 1'
is "and the sysctl line waiting behind it, in the same message" \
   "$(reasons)" "kernel disabled|at /etc/sysctl.conf:1"

printf '\n== switched off in the running system ==\n'
sysroot 1
is "is off now"                            "$(reasons)" "now"
sysroot -1
is "and -1 is off too"                     "$(reasons)" "now"
sysroot 1
sysfile /etc/sysctl.d/90-on.conf <<<'net.ipv6.conf.default.disable_ipv6 = 0'
is "unless the install's own sysctl --system switches it back on" "$(reasons)" "none"

printf '\n== the lines a "disable IPv6" VPS image ships ==\n'
vps() {
    sysfile /etc/sysctl.conf <<'EOF'
net.ipv4.ip_forward = 0
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
EOF
}
sysroot 1; vps
is "every line named, and the running system as well" \
   "$(reasons)" "at /etc/sysctl.conf:2|at /etc/sysctl.conf:3|now"
sysroot 0; vps
is "switched back on with sysctl -w alone, and off at the next sysctl --system" \
   "$(reasons)" "at /etc/sysctl.conf:2|at /etc/sysctl.conf:3"
sysroot 0; vps; ln -s ../sysctl.conf "$ROOT/etc/sysctl.d/99-sysctl.conf"
is "reached through Ubuntu's 99-sysctl.conf too, and named once" \
   "$(reasons)" "at /etc/sysctl.conf:2|at /etc/sysctl.conf:3"

printf '\n== started with ipv6.disable_ipv6=1 ==\n'
sysroot 1; param disable_ipv6 1
is "off now, and at every boot"            "$(reasons)" "now|boot"
sysroot 0; param disable_ipv6 1
is "switched on with sysctl -w, and off again at the next boot" "$(reasons)" "boot"
sysroot 0; param disable_ipv6 1
sysfile /etc/sysctl.d/60-ipv6.conf <<<'net.ipv6.conf.default.disable_ipv6 = 0'
is "unless systemd-sysctl switches it back on as the machine boots" "$(reasons)" "none"
sysroot 0; param disable_ipv6 1
sysfile /etc/sysctl.conf <<<'net.ipv6.conf.default.disable_ipv6 = 0'
is "which it does not do from /etc/sysctl.conf - only procps reads that" "$(reasons)" "boot"

printf '\n== how the files are read ==\n'
sysroot
sysfile /etc/sysctl.d/10-noipv6.conf <<<'-net/ipv6/conf/all/disable_ipv6=1'
is "the slash spelling and a leading '-'"  "$(reasons)" "at /etc/sysctl.d/10-noipv6.conf:1"

sysroot
sysfile /etc/sysctl.d/10-noipv6.conf <<<'- net.ipv6.conf.all.disable_ipv6 = 1'
is "a space after the '-' names no key"    "$(reasons)" "none"

sysroot
sysfile /etc/sysctl.conf <<'EOF'
#net.ipv6.conf.all.disable_ipv6 = 1
; net.ipv6.conf.default.disable_ipv6 = 1
EOF
is "a commented line is not a setting"     "$(reasons)" "none"

sysroot
sysfile /usr/lib/sysctl.d/10-off.conf <<<'net.ipv6.conf.all.disable_ipv6 = 1'
sysfile /etc/sysctl.d/90-on.conf      <<<'net.ipv6.conf.default.disable_ipv6 = 0'
is "a later file that switches it back on wins" "$(reasons)" "none"

sysroot
sysfile /etc/sysctl.d/90-on.conf      <<<'net.ipv6.conf.all.disable_ipv6 = 0'
sysfile /usr/lib/sysctl.d/95-off.conf <<<'net.ipv6.conf.default.disable_ipv6 = 1'
is "files are applied by name, not by directory" "$(reasons)" "at /usr/lib/sysctl.d/95-off.conf:1"

sysroot
sysfile /usr/lib/sysctl.d/50-net.conf <<<'net.ipv6.conf.all.disable_ipv6 = 1'
sysfile /etc/sysctl.d/50-net.conf     <<<'net.ipv4.ip_forward = 1'
is "a name in /etc hides the same name under /usr/lib" "$(reasons)" "none"

sysroot
sysfile /usr/lib/sysctl.d/50-noipv6.conf <<<'net.ipv6.conf.all.disable_ipv6 = 1'
ln -s /dev/null "$ROOT/etc/sysctl.d/50-noipv6.conf"
is "and so does a link to /dev/null, which is how a package's file is switched off" \
   "$(reasons)" "none"

sysroot
sysfile /etc/sysctl.conf            <<<'net.ipv6.conf.all.disable_ipv6 = 1'
sysfile /etc/sysctl.d/99-zz-on.conf <<<'net.ipv6.conf.all.disable_ipv6 = 0'
is "/etc/sysctl.conf is applied last"      "$(reasons)" "at /etc/sysctl.conf:1"

sysroot
sysfile /etc/sysctl.d/.99-noipv6.conf <<<'net.ipv6.conf.all.disable_ipv6 = 1'
is "a hidden file counts, because procps reads it" "$(reasons)" "at /etc/sysctl.d/.99-noipv6.conf:1"

sysroot
sysfile /etc/sysctl.d/10-x.conf <<'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = yes
EOF
is "a value the kernel refuses leaves the one before it" "$(reasons)" "at /etc/sysctl.d/10-x.conf:1"

sysroot
sysfile /etc/sysctl.d/10-x.conf <<<'net.ipv6.conf.default.disable_ipv6 = 0x1'
is "and 0x1 is a number to the kernel"     "$(reasons)" "at /etc/sysctl.d/10-x.conf:1"

printf "\n== globs, and the tunnel's own key ==\n"
sysroot
sysfile /etc/sysctl.d/90-noipv6.conf <<<'net.ipv6.conf.*.disable_ipv6 = 1'
is "a glob switches off every key it matches" "$(reasons)" "at /etc/sysctl.d/90-noipv6.conf:1"

# udev runs systemd-sysctl for each interface as it appears, so a glob that
# spares "all" and "default" still reaches awg0 - after awg-quick has created it
# and while it is adding the address.
sysroot
sysfile /etc/sysctl.d/10-on.conf <<'EOF'
net.ipv6.conf.all.disable_ipv6 = 0
net.ipv6.conf.default.disable_ipv6 = 0
EOF
sysfile /etc/sysctl.d/90-noipv6.conf <<<'net.ipv6.conf.*.disable_ipv6 = 1'
is "keys named outright are not the glob's, but the tunnel's own still is" \
   "$(reasons)" "at /etc/sysctl.d/90-noipv6.conf:1"

sysroot
sysfile /etc/sysctl.d/90-noipv6.conf <<<'net.ipv6.conf.eth*.disable_ipv6 = 1'
is "a glob that matches none of them does not count" "$(reasons)" "none"

sysroot
sysfile /etc/sysctl.d/90-awg.conf <<<'net.ipv6.conf.awg0.disable_ipv6 = 1'
is "and the tunnel's own key written outright does" "$(reasons)" "at /etc/sysctl.d/90-awg.conf:1"

unset AWG_ROOT_DIR

# ------------------------------------------------- what install.sh says about it
#
# ipv6_refuse out of install.sh, with die printing rather than leaving and t()
# taking the English half: which fixes a refusal offers is under test here, not
# how they are worded.
printf '\n== what install.sh says ==\n'
INSTALL="$REPO/install.sh"
REFUSE=$(awk '/^ipv6_refuse\(\) \{/,/^\}/' "$INSTALL")
said() {
    bash -c "
        t() { printf '%s' \"\$1\"; }
        die() { printf '%s\n' \"\$*\"; }
        conf_has_ipv6() { return $2; }
        $REFUSE
        ipv6_refuse \"\$1\"" _ "$1"
}
has()   { if [[ "$2" == *"$3"* ]]; then ok "$1"; else bad "$1" "no '$3' in: $2"; fi; }
hasnt() { if [[ "$2" != *"$3"* ]]; then ok "$1"; else bad "$1" "'$3' in: $2"; fi; }

if [[ "$REFUSE" != *"ipv6_refuse()"* ]]; then
    bad "install.sh defines ipv6_refuse" "the anchor in this test needs updating"
else
    msg=$(said $'at /etc/sysctl.conf:2\nat /etc/sysctl.conf:3\nnow' 1)
    has   "each line under /etc is named to comment out" \
          "$msg" $'\n         /etc/sysctl.conf:2\n         /etc/sysctl.conf:3'
    has   "and the running system is switched on after them" \
          "$msg" "sysctl -w net.ipv6.conf.default.disable_ipv6=0"
    has   "the way out is --ipv6 off"               "$msg" "Or install with --ipv6 off"
    hasnt "with no --fresh when no IPv6 config is in the way" "$msg" "--fresh"

    msg=$(said 'at /usr/lib/sysctl.d/50-noipv6.conf:1' 1)
    has   "a package's file is masked"              "$msg" "ln -s /dev/null /etc/sysctl.d/50-noipv6.conf"
    hasnt "rather than edited"                      "$msg" "comment out"
    hasnt "and nothing is switched on that is not off" "$msg" "sysctl -w"

    msg=$(said $'kernel disabled\nat /etc/sysctl.conf:1' 1)
    has   "ipv6.disable=1 comes off the command line" "$msg" "update-grub && reboot"
    has   "together with the line waiting behind it" "$msg" $'\n         /etc/sysctl.conf:1'

    msg=$(said boot 1)
    has   "and so does ipv6.disable_ipv6=1"         "$msg" "'ipv6[. ]disable_ipv6=1'"

    msg=$(said now 0)
    has   "a config with IPv6 in it needs --fresh, whether or not this is an upgrade" \
          "$msg" "--fresh --ipv6 off"
fi

# And asked where it saves anything: after 1b, at the top level where no branch
# can skip it, and before the first step that builds.
after=$(grep -n '^# -* 1b\. existing install?' "$INSTALL" | head -1 | cut -d: -f1)
asked=$(grep -nE '^[^#[:space:]].*ipv6_off_reasons' "$INSTALL" | head -1 | cut -d: -f1)
built=$(grep -nF 'step "$(t "Installing build dependencies"' "$INSTALL" | head -1 | cut -d: -f1)
is "install.sh asks after 1b, outside any branch, before it builds anything" \
   "$(( ${after:-999999} < ${asked:-0} && ${asked:-999999} < ${built:-0} ))" "1"
typo=$(grep -nF "die \"--ipv6 '" "$INSTALL" | head -1 | cut -d: -f1)
is "a mistyped --ipv6 is reported as a typo before that" "$(( ${typo:-999999} < ${asked:-0} ))" "1"
is "a key this kernel lacks does not stop sysctl --system" \
   "$(grep -c '^sysctl -q -e --system$' "$INSTALL")" "1"
is "and the kernel is asked again once it has run" \
   "$(grep -A20 '^sysctl -q -e --system$' "$INSTALL" | grep -c 'ipv6_refuse')" "1"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
