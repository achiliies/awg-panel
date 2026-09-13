#!/bin/bash
#
# tests/ipv6_off.sh - a host with IPv6 switched off, and what install.sh says to it.
#
# Every IPv6 mode puts an address on the tunnel interface, blackhole included,
# and a kernel with IPv6 switched off refuses it. lib/subnet6.sh works out from
# the running kernel and the stored sysctl files whether that will happen, and
# install.sh turns the answer into a refusal before it builds anything. Both are
# driven here against a directory standing in for /, rather than this machine's
# own /proc, /sys and /etc: the interesting cases are the hosts this machine is
# not.
#
# A file of its own rather than the end of tests/subnet6.sh, which skips as a
# whole wherever python3 or the panel is missing. Nothing here needs either.
#
# Usage: tests/ipv6_off.sh        (exit 0 = all checks passed)
#
set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
INSTALL="$REPO/install.sh"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# shellcheck source=lib/subnet.sh
. "$REPO/lib/subnet.sh"
# shellcheck source=lib/subnet6.sh
. "$REPO/lib/subnet6.sh"

PASS=0 FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }
is()  { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "got '$2', wanted '$3'"; fi; }

# ------------------------------------------------ IPv6 switched off in the kernel
#
# Each case starts from a stock kernel and changes one thing. reasons is what
# install.sh acts on, one reason to a line, joined here with "|" so that a case
# reads on one line.
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

# A space after the "-" is where the two part: procps keeps it as part of the
# key, which then names nothing, and systemd-sysctl strips it and applies the
# line. /etc/sysctl.conf is procps's alone; /etc/sysctl.d is read by both.
sysroot
sysfile /etc/sysctl.conf <<<'- net.ipv6.conf.all.disable_ipv6 = 1'
is "a space after the '-' names no key to procps" "$(reasons)" "none"
sysroot
sysfile /etc/sysctl.d/10-noipv6.conf <<<'- net.ipv6.conf.all.disable_ipv6 = 1'
is "and systemd-sysctl applies the line anyway, at every boot" \
   "$(reasons)" "at /etc/sysctl.d/10-noipv6.conf:1"

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
    has   "with the reboot that taking it off needs" "$msg" "update-grub && reboot"
    # Or a file that switches it back on, and that alone has to be enough: the
    # line the message gives, written where it says, leaves the next run nothing
    # to refuse without a reboot - which the parameter itself would need.
    re="echo '([^']+)' > (/etc/sysctl\.d/[^[:space:]]+)"
    if [[ "$msg" =~ $re ]]; then
        line=${BASH_REMATCH[1]} file=${BASH_REMATCH[2]}
        export AWG_ROOT_DIR="$ROOT"
        sysroot 1; param disable_ipv6 1
        sysfile "$file" <<<"$line"
        is "or a sysctl.d file that switches it back on without one" "$(reasons)" "none"
        unset AWG_ROOT_DIR
    else
        bad "or a sysctl.d file that switches it back on without one" "no such line in: $msg"
    fi

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
# Which also stops that run from saying a key it wrote itself is missing, so the
# one naming an interface has to name one that exists: in slashes, where a
# VLAN's eth0.100 is still one name rather than eth0 and 100.
is "accept_ra is written with slashes, so a VLAN's dot stays in its name" \
   "$(grep -cF 'echo "net/ipv6/conf/${IPV6_ACCEPT_RA_FIX}/accept_ra = 2"' "$INSTALL")" "1"
# And the kernel is asked before either branch writes the server config. A
# refusal hands the tunnel to restore_iface_on_failure, which brings it up on
# whatever that file holds, and ipv6_refuse offers the way out the file allows.
# Asked after an upgrade had put IPv6 in it, the tunnel stayed down and the way
# out took --fresh.
refused=$(grep -nF "ipv6_refuse \"\$(printf 'applied" "$INSTALL" | head -1 | cut -d: -f1)
written=$(grep -nE '^[[:space:]]*(ipv6_migrate_conf$|cat > "\$CONF_DIR/\$\{IFACE\}\.conf")' "$INSTALL" \
          | head -1 | cut -d: -f1)
is "and before an upgrade or a fresh install writes the server config" \
   "$(( ${refused:-999999} < ${written:-0} ))" "1"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
