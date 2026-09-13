#!/bin/bash
#
# tests/detect.sh - what install.sh works out about a machine it did not set up.
#
# Two answers the installer reads off the box rather than being told, both of
# which it then acts on without asking anybody, and neither of which reports
# being wrong.
#
# The first is the egress interface, taken from the routing table. Every rule
# the tunnel installs names it, and iptables accepts an interface name that
# matches no device - a rule is allowed to name one that has yet to appear - so
# reading it wrongly does not fail. The tunnel comes up, the handshake
# completes, the panel shows the peer connected, and nothing the client sends
# is translated on its way out. There is no error anywhere in that.
#
# The second is the IPv6 an existing configuration already carries. An upgrade
# decides from it whether --subnet6 and --ipv6 can be acted on at all: a tunnel
# already numbered out of a prefix cannot be moved to another one from here,
# because the prefix is in the interface's address, in every peer's AllowedIPs
# and in every config already handed out, and an upgrade rewrites none of that.
# What it did rewrite was clients.env, so the panel was left allocating out of a
# network the server was not on. Silently, on a machine that was working.
#
# Everything under test is extracted from install.sh rather than copied here. A
# copy would keep passing while the installer drifted away from it, which is the
# only failure mode a test like this one has.
#
# Usage: tests/detect.sh        (exit 0 = all checks passed)
#
set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
INSTALLER="$REPO/install.sh"
COMMON="$REPO/lib/common.sh"
PANEL_CLI="$REPO/bin/awg-panel"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok    %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"
        [[ -n "${2:-}" ]] && printf '        got: %s\n' "$2"; }
ck()  { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "$2 (wanted $3)"; fi; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# ------------------------------------------------------- the egress interface
# The awk on its own, without the `ip route show default` in front of it, so
# routing tables this machine does not have can be fed to it.
ROUTE_AWK=$(grep -F 'if ($i == "dev")' "$INSTALLER" | head -1)
ROUTE_AWK=${ROUTE_AWK#*\'}
ROUTE_AWK=${ROUTE_AWK%\'*}
[[ -n "$ROUTE_AWK" && "$ROUTE_AWK" == *'"dev"'* ]] || {
    echo "  FAIL  could not find the default-route awk in install.sh" >&2
    echo "        (the anchor in this test needs updating)" >&2
    exit 1; }

# ---------------------------------------------------------- the source address
# The same trick against the same output, for the field the panel reads rather
# than the one the installer does: route_src_addr, out of lib/common.sh.
#
# Two copies of it exist. bin/awg-panel cannot source lib/ - a --standalone
# panel install puts that script on a machine with no lib/ to source, which is
# why it carries its own t() as well - so the awk is extracted from both files
# and the two are required to be the same string. A fix applied to one and not
# the other is the failure this pair of checks exists to catch.
src_awk_of() {
    local line
    line=$(grep -F 'if ($i == "src")' "$1" | head -1)
    line=${line#*\'}
    printf '%s' "${line%\'*}"
}
SRC_AWK=$(src_awk_of "$COMMON")
SRC_AWK_CLI=$(src_awk_of "$PANEL_CLI")
[[ -n "$SRC_AWK" && "$SRC_AWK" == *'"src"'* ]] || {
    echo "  FAIL  could not find route_src_addr's awk in lib/common.sh" >&2
    echo "        (the anchor in this test needs updating)" >&2
    exit 1; }

# ------------------------------------------------- what a config already has
# conf_has_ipv6 and conf_ipv6_mode, with SERVER_CONF pointed at a fixture. Run
# in a subshell per case because that is how the installer has them: two
# readers over one file, with nothing else of the installer around them.
conf_reader() {
    printf 'SERVER_CONF=%s\n' "$1"
    awk '/^conf_has_ipv6\(\) \{/,/^\}/'  "$INSTALLER"
    awk '/^conf_ipv6_mode\(\) \{/,/^\}/' "$INSTALLER"
}
for fn in conf_has_ipv6 conf_ipv6_mode; do
    grep -q "^${fn}() {" "$INSTALLER" || {
        echo "  FAIL  ${fn} is no longer defined in install.sh" >&2; exit 1; }
done

fixture() { printf '%s\n' "$2" > "$WORK/$1.conf"; }

# A server installed before the tunnel carried IPv6 at all.
fixture v4 '[Interface]
Address = 10.13.0.1/20
ListenPort = 51820
PostUp = iptables -t nat -A POSTROUTING -s 10.13.0.0/20 -o eth0 -j MASQUERADE

[Peer]
# Client = client1
AllowedIPs = 10.13.0.2/32'

# The same, after the migration reached the peers but not the interface. There
# is no such state, but a hand-edited file is not something to assume about,
# and the colon in the peer must not read as the interface having an address.
fixture v4peer '[Interface]
Address = 10.13.0.1/20

[Peer]
AllowedIPs = 10.13.0.2/32, fd7a:1e5f:22::2/128'

fixture blackhole '[Interface]
Address = 10.13.0.1/20, fd7a:1e5f:22::1/64
PostUp = ip6tables -A FORWARD -i %i -o %i -j ACCEPT || true
PostUp = ip6tables -A FORWARD -i %i -j REJECT --reject-with icmp6-adm-prohibited || true

[Peer]
AllowedIPs = 10.13.0.2/32, fd7a:1e5f:22::2/128'

fixture nat '[Interface]
Address = 10.13.0.1/20, fd7a:1e5f:22::1/64
PostUp = ip6tables -t nat -A POSTROUTING -s fd7a:1e5f:22::/64 -o eth0 -j MASQUERADE || true
PostUp = ip6tables -A FORWARD -i %i -j ACCEPT || true

[Peer]
AllowedIPs = 10.13.0.2/32, fd7a:1e5f:22::2/128'

fixture native '[Interface]
Address = 10.13.0.1/20, 2a01:4f8:c17:1::1/64
PostUp = ip6tables -A FORWARD -i %i -j ACCEPT || true

[Peer]
AllowedIPs = 10.13.0.2/32, 2a01:4f8:c17:1::2/128'

has6()  { bash -c "$(conf_reader "$WORK/$1.conf"); conf_has_ipv6"; }
mode6() { bash -c "$(conf_reader "$WORK/$1.conf"); conf_ipv6_mode"; }

# ------------------------------------------------------- what an upgrade does
# The preflight that decides whether the two flags can be acted on, lifted out
# of the existing-install branch with the fixture standing in for the config.
# Everything it reads is set here; what it prints is what the rest of the run
# would go on to use.
PREFLIGHT=$(sed -n '/# The IPv6 pair is ignored too/,/^    fi$/p' "$INSTALLER")
[[ -n "$PREFLIGHT" && "$PREFLIGHT" == *"conf_has_ipv6"* ]] || {
    echo "  FAIL  could not find the --subnet6/--ipv6 preflight in install.sh" >&2
    echo "        (the anchors in this test need updating)" >&2
    exit 1; }

preflight() {
    local conf="$WORK/$1.conf" given="$2" subnet6="$3" mode="$4"
    bash -c "
        warn() { :; }
        # The messages in the lifted region go through t(), which lives in
        # lib/i18n.sh. Sourcing that here would work and would also be the
        # only reason this harness needed a library at all; the English half
        # is what an untranslated run prints, and nothing below reads the
        # text anyway.
        t() { printf '%s' \"\$1\"; }
        $(conf_reader "$conf")
        SUBNET6_GIVEN=$given
        SUBNET6='$subnet6'
        IPV6_MODE='$mode'
        SUBNET6_FLAGS=(--ipv6)
        $PREFLIGHT
        printf '%s %s %s\n' \"\$SUBNET6_GIVEN\" \"\$SUBNET6\" \"\$IPV6_MODE\""
}

# And the read that follows it: the network an upgrade keeps. Needs the two
# subnet libraries, the config reader from lib/conf.sh, and a clients.env to
# disagree with.
EXISTING_READ=$(sed -n '/^# An upgrade keeps the network it already handed out/,/^fi$/p' "$INSTALLER")
[[ -n "$EXISTING_READ" && "$EXISTING_READ" == *"conf_ipv6_mode"* ]] || {
    echo "  FAIL  could not find the existing-IPv6 read in install.sh" >&2
    echo "        (the anchors in this test need updating)" >&2
    exit 1; }

existing_read() {
    local conf="$WORK/$1.conf" env="$WORK/clients.env"
    printf '%s\n' "$2" > "$env"
    bash -c "
        AWG_CONF_DIR='$WORK' AWG_IFACE=x
        . '$REPO/lib/common.sh'
        . '$REPO/lib/conf.sh'
        . '$REPO/lib/subnet.sh'
        . '$REPO/lib/subnet6.sh'
        CONF_DIR='$WORK'; SERVER_CONF='$conf'
        $(awk '/^conf_ipv6_mode\(\) \{/,/^\}/' "$INSTALLER")
        EXISTING=1 SUBNET6_GIVEN=0 SUBNET6=auto IPV6_MODE='${3:-auto}'
        $EXISTING_READ
        printf '%s %s\n' \"\$SUBNET6\" \"\$IPV6_MODE\""
}

# And the "off" 1b takes up out of clients.env on an upgrade that types no
# flags - which is every upgrade awg-update and awg-menu run.
KEPT_OFF=$(sed -n '/# And an "off" is kept/,/^    fi$/p' "$INSTALLER")
[[ -n "$KEPT_OFF" && "$KEPT_OFF" == *"IPV6_MODE=off"* ]] || {
    echo "  FAIL  could not find where 1b keeps a recorded --ipv6 off in install.sh" >&2
    echo "        (the anchors in this test need updating)" >&2
    exit 1; }

kept_off() {
    local conf="$WORK/$1.conf" given="$2" dir="$WORK/kept"
    rm -rf "$dir"; mkdir -p "$dir"
    [[ "$3" == none ]] || printf '%s\n' "$3" > "$dir/clients.env"
    bash -c "
        $(conf_reader "$conf")
        CONF_DIR='$dir'
        SUBNET6_GIVEN=$given
        IPV6_MODE=auto
        $KEPT_OFF
        printf '%s\n' \"\$IPV6_MODE\""
}

# --------------------------------------------------------------------- cases
# mawk is what these servers have and gawk is what a developer has, the same
# reason tests/hooks.sh runs everything twice.
RAN=0
for AWK in mawk gawk original-awk busybox; do
    [[ "$AWK" == busybox ]] && { command -v busybox >/dev/null 2>&1 || continue; AWK="busybox awk"; }
    command -v "${AWK%% *}" >/dev/null 2>&1 || continue
    RAN=$((RAN+1))
    printf '\n== %s ==\n' "$AWK"

    wan() { $AWK "$ROUTE_AWK"; }

    # The form every cloud image writes, and the one that used to be read
    # correctly by taking the fifth field.
    ck "a route with a gateway" \
       "$(printf 'default via 10.0.0.1 dev eth0 proto dhcp src 10.0.0.5 metric 100\n' | wan)" "eth0"
    # The form that was not: no gateway, so the fifth field is the word "scope".
    ck "a route without one" \
       "$(printf 'default dev ppp0 scope link\n' | wan)" "ppp0"
    ck "a route that ends at the device" \
       "$(printf 'default dev wg-upstream\n' | wan)" "wg-upstream"
    # Multipath prints the interfaces on continuation lines, and the first line
    # is the word "default" on its own - which the fifth field read as nothing
    # at all, so the installer stopped on "cannot determine the default route
    # interface" on a machine with two working uplinks.
    ck "a multipath route takes the first nexthop" \
       "$(printf 'default \n\tnexthop via 10.0.0.1 dev eth0 weight 1\n\tnexthop via 10.0.1.1 dev eth1 weight 1\n' | wan)" "eth0"
    ck "two defaults take the first" \
       "$(printf 'default via 10.0.0.1 dev eth0 metric 100\ndefault via 10.9.0.1 dev eth1 metric 200\n' | wan)" "eth0"
    ck "no default route at all" "$(printf '' | wan)" ""

    # `ip route get`, which is a different question with a different answer
    # shape: one line describing how this box would reach one address, and the
    # panel wants the address it would send from.
    src() { $AWK "$SRC_AWK"; }

    # What a server with a gateway prints, and the form that used to be read
    # correctly by taking the seventh field.
    ck "a src behind a gateway" \
       "$(printf '1.1.1.1 via 10.0.0.1 dev eth0 src 10.0.0.5 uid 1000 \n    cache \n' | src)" \
       "10.0.0.5"
    # And the form that was not. With no `via <gw>` pair every field shifts two
    # to the left, so the seventh word is the number after uid - which is how
    # `awg-panel url` came to print http://1000:8088/ on a PPPoE box, and how
    # the certificate menu came to offer 1000 as the address to issue for.
    ck "a src on a point-to-point route" \
       "$(printf '1.1.1.1 dev ppp0 src 198.51.100.7 uid 0 \n    cache \n' | src)" \
       "198.51.100.7"
    ck "a src on a directly-attached route" \
       "$(printf '1.1.1.1 dev eth0 src 192.0.2.10 \n    cache \n' | src)" "192.0.2.10"
    # Running as root drops the uid field, which moves the address to the last
    # word on the line. `i < NF` must not be `i <= NF` here or this prints
    # nothing: there would be no field after the one holding "src".
    ck "a src as the last word on the line" \
       "$(printf '1.1.1.1 via 10.0.0.1 dev eth0 src 10.0.0.5\n' | src)" "10.0.0.5"
    ck "the first line wins over the cache line" \
       "$(printf '1.1.1.1 dev eth0 src 192.0.2.10 uid 0 \n    cache expires 599sec\n' | src)" \
       "192.0.2.10"
    # A machine with no route to 1.1.1.1 gets an error on stderr and nothing on
    # stdout. Empty is the answer, and every caller has a further fallback for
    # it - a bare `src` with no address after it must not invent one either.
    ck "no route out at all" "$(printf '' | src)" ""
    ck "a line with no src in it" \
       "$(printf 'broadcast 10.0.0.255 dev eth0 table local \n' | src)" ""
done

(( RAN )) || { echo "  FAIL  no awk to test with" >&2; exit 1; }

printf '\n== the two copies of route_src_addr ==\n'
ck "bin/awg-panel carries the same awk as lib/common.sh" \
   "$SRC_AWK_CLI" "$SRC_AWK"
# Neither caller may go back to counting fields. Both have a fallback chain
# around this call, so a wrong answer here is not an error anywhere - it is a
# wrong address printed with confidence, which is what makes it worth pinning.
for f in bin/awg-panel bin/awg-menu; do
    if grep -qF "print \$7" "$REPO/$f"; then
        bad "${f} reads a route field by position"
    else
        ok "${f} reads the route by field name"
    fi
done

printf '\n== the IPv6 a config already carries ==\n'
has6 v4      && bad "a v4-only config carries no IPv6"     || ok "a v4-only config carries no IPv6"
has6 v4peer  && bad "a peer's colons are not the interface's" \
             || ok "a peer's colons are not the interface's"
for c in blackhole nat native; do
    has6 "$c" && ok "the ${c} config carries IPv6" || bad "the ${c} config carries IPv6"
    ck   "and its rules say ${c}" "$(mode6 "$c")" "$c"
done
mode6 v4 >/dev/null && bad "a v4-only config has no mode to read" \
                    || ok "a v4-only config has no mode to read"

printf '\n== --subnet6 / --ipv6 on an upgrade ==\n'
# Nothing given: nothing to clear, and nothing said.
ck "neither flag given, IPv6 already there" \
   "$(preflight nat 0 auto auto)" "0 auto auto"
# Given against a tunnel that has none. This is the migration the flags exist
# for, and they have to survive it.
ck "flags honoured on a config with no IPv6" \
   "$(preflight v4 1 fd00:dead::/64 nat)" "1 fd00:dead::/64 nat"
# Given against one that already has some. Cleared, not merely warned about:
# every section below reads these, and half an answer is what they used to get.
ck "flags cleared on a config that already has IPv6" \
   "$(preflight nat 1 fd00:dead::/64 native)" "0 auto auto"
ck "and --ipv6 off cannot strip it either" \
   "$(preflight blackhole 1 auto off)" "0 auto auto"

printf '\n== what an upgrade keeps ==\n'
ck "clients.env, when it agrees with the config" \
   "$(existing_read nat 'SUBNET6_CIDR="fd7a:1e5f:22::/64"
SUBNET6_MODE="nat"')" "fd7a:1e5f:22::/64 nat"
# The config is what the interface is numbered out of; clients.env is a mirror,
# and a mirror is the half that gets hand-edited or restored from a backup older
# than the tunnel. Believing it is how the panel comes to allocate addresses out
# of a network nothing answers on.
ck "the config wins when the mirror disagrees" \
   "$(existing_read nat 'SUBNET6_CIDR="fd00:beef::/64"
SUBNET6_MODE="nat"')" "fd7a:1e5f:22::/64 nat"
ck "the config answers when the mirror is empty" \
   "$(existing_read blackhole 'SUBNET6_CIDR=""
SUBNET6_MODE=""')" "fd7a:1e5f:22::/64 blackhole"
ck "the rules answer when the mode is junk" \
   "$(existing_read native 'SUBNET6_CIDR="2a01:4f8:c17:1::/64"
SUBNET6_MODE="sometimes"')" "2a01:4f8:c17:1::/64 native"
# A v4-only config has nothing to keep, so the run goes on to work one out -
# which is the migration that stops those servers leaking their clients' IPv6.
ck "a v4-only config leaves both to be derived" \
   "$(existing_read v4 'SUBNET6_CIDR=""
SUBNET6_MODE=""')" "auto auto"
ck "and leaves an off that 1b took up as off" \
   "$(existing_read v4 'SUBNET6_CIDR=""
SUBNET6_MODE="off"' off)" "auto off"

printf '\n== an "off" an upgrade keeps ==\n'
ck "a tunnel installed with --ipv6 off stays off" \
   "$(kept_off v4 0 'SUBNET6_CIDR=""   # blank = this tunnel carries no IPv6
SUBNET6_MODE="off"   # native | nat | blackhole | off')" "off"
# Blank is every server from before the record, and those are the servers the
# migration is for.
ck "a blank mode is still migrated" \
   "$(kept_off v4 0 'SUBNET6_CIDR=""
SUBNET6_MODE=""')" "auto"
ck "and so is a server with no clients.env at all" "$(kept_off v4 0 none)" "auto"
ck "a flag typed on this run wins over the record" \
   "$(kept_off v4 1 'SUBNET6_MODE="off"')" "auto"
ck "and a tunnel that has IPv6 is not off whatever the file says" \
   "$(kept_off nat 0 'SUBNET6_MODE="off"')" "auto"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
exit $(( FAIL > 0 ))
