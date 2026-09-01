#!/bin/bash
#
# tests/mtu.sh - put a real tunnel on a real link and count the fragments.
#
# tests/wire.py works out how large a profile's datagrams are from the packet
# layout, and panel/tests/test_wire_sizes.py and tests/obfs_check.py assert
# against it. That is arithmetic, and arithmetic can be wrong in the same
# direction as the code it audits. This drives the module install.sh builds
# across a link of a known width and asks the kernel what actually happened.
#
# The measurement is FragCreates out of /proc/net/snmp in the sending namespace,
# and Ip6FragCreates out of /proc/net/snmp6 when the endpoint is reached over
# IPv6. That counter is the whole point of this file, because it is the only
# thing on the box that distinguishes the two states the tunnel can be in:
#
#   - the datagram fits, and is delivered
#   - the datagram does not fit, is cut into fragments, and is delivered
#
# Both of those look like a working tunnel from every direction a test usually
# looks. AmneziaWG sets skb->ignore_df on the outer datagram (socket.c:85 and
# socket.c:151) and passes df=0 to udp_tunnel_xmit_skb, so an oversized packet
# is never refused with an ICMP that something could act on and never appears in
# any log - it is quietly fragmented, on the sending host or at the first hop
# too small for it. On a lab veth the fragments then reassemble and the ping
# comes back, which is why every check this repository had before this one
# passed on a config that fragments: install.sh's self-test and awg-menu's both
# run the tunnel over loopback, where the link is 65536 bytes wide and there is
# nothing to be too large for.
#
# Out on a real path the fragments are what a client loses. A middlebox that
# reads UDP ports cannot see them on anything but the first fragment, reassembly
# buffers are small and are the first thing dropped under load, and plenty of
# networks discard IPv4 fragments outright. What the operator is told is that
# downloads are slow.
#
# So a fragment on a 1500-byte link is a failure, not a warning. Nothing about a
# full-size packet needs to be fragmented on a link that wide; if one is, the
# profile is spending more bytes than it has. Under 1500 it is printed instead -
# see ENFORCED, which draws the line tests/wire.py draws between its two Links.
#
# Both address families, because the budget is drawn against the larger outer
# header and the endpoint's family is not the generator's to know: install.sh
# takes a hostname for --endpoint, and a client that resolves it to an AAAA pays
# twenty bytes more with nothing in its config recording which it will be. One
# family per pass, and the tunnel inside stays IPv4 either way, so the only
# thing that moves between the two passes is the header the datagram is wrapped
# in - which is the whole of what the budget had wrong.
#
# Root is needed to make the namespaces, and the amneziawg module has to be
# loadable. Both are why this is a shell test on the side rather than part of
# `make test` - the panel's suite runs on a CI box with neither.
#
# Usage: sudo tests/mtu.sh [link-mtu ...]      (default: 1500 1492)
#
# Exit 0 - full-size traffic crossed every 1500-byte link whole, over every
#          address family this box could measure
#      1 - it fragmented on one of those, or a counter could not be read
#      2 - could not run here: no iproute2, no awg, not root, or no module
#
# A fragment on a link narrower than 1500 is printed and leaves the exit code
# alone. Which families actually ran is printed at the end, because a box with
# IPv6 compiled out measures half of this and must not read like one that
# measured all of it.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

# Sourced here rather than in the branch below that draws a profile, because
# the constants come with it and both branches read them - the fallback wants
# OBFS_DEFAULT_MTU, and the diagnosis in measure() wants OBFS_HEADER_NONCE on a
# box that had a config to read and never took the branch. `set -u` is on, so a
# constant reached before its library is an exit 1, which is the status that
# means the profile fragments: the wrong answer, from a run that measured
# nothing. Nothing in it runs at source time, and tests/obfs.sh already asserts
# that sourcing it under `set -euo pipefail` is quiet.
# shellcheck source=lib/obfs.sh
. "$REPO/lib/obfs.sh"

NS_S="awg-mtu-server"
NS_C="awg-mtu-client"
DEV_S="awgmtus"
DEV_C="awgmtuc"
VETH_S="vmtus"
VETH_C="vmtuc"
NET4_S="198.51.100.1"
NET4_C="198.51.100.2"
# A ULA, so nothing here can route anywhere. The /64 is what gives the two ends
# a connected route to each other; the tunnel inside stays IPv4 either way.
NET6_S="fd00:5100::1"
NET6_C="fd00:5100::2"
TUN4_S="10.13.99.1"
TUN4_C="10.13.99.2"
PORT=51899

# The two outer headers, and tests/wire.py's IPV4 and IPV6 by another name. The
# budget reserves the larger one whichever the endpoint turns out to be, because
# a hostname handed to --endpoint can resolve to an AAAA and nothing in the
# config records that it did - the module reserves the same way and in the same
# place, `dev->mtu = ETH_DATA_LEN - overhead` with `overhead` counting
# max(sizeof(struct ipv6hdr), sizeof(struct iphdr)) (device.c:324). So the
# diagnosis below names the header of the family that was measured, and the
# advice names HDR6 whatever was measured: advice against the smaller one is
# advice to build a pair the panel refuses and a datagram that fragments the
# moment a client resolves the endpoint to an AAAA.
HDR4=20
HDR6=40

# The width at which a fragment stops being a report and becomes a failure. The
# same line tests/wire.py draws between ETHERNET and PPPOE, and drawn here for
# the same reason: a plain 1500-byte path is the widest untunnelled link there
# is and the one the module reserves for, so a profile that will not cross it
# whole has nowhere left to go. How much margin to leave under 1500 - for the
# 1492 of the PPPoE link most home clients sit behind - is a decision about who
# this software is for and not an arithmetic error, so it is printed at the
# width it happened and left to whoever picks the default MTU.
ENFORCED=1500

R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; N=$'\e[0m'
fail=0
reported=0
measured=0

note() { printf '%s\n' "$*"; }
bad()  { printf '%s✗%s %s\n' "$R" "$N" "$*"; fail=1; }
good() { printf '%s✓%s %s\n' "$G" "$N" "$*"; }
# The same shape as bad() and deliberately not the same effect - see ENFORCED.
# It carries no count of its own: it is used both for an overrun under 1500 and
# for a pass that could not be taken at all, and a summary that added those
# together would report a skipped family as a profile that overran.
warn() { printf '%s!%s %s\n' "$Y" "$N" "$*"; }

LINKS=("$@")
(( ${#LINKS[@]} )) || LINKS=(1500 1492)

# Three ways this cannot run, and all three exit 2: 0 is "no fragments", 1 is
# "the profile fragments", and a box that could not be asked has produced
# neither. The CI step and anyone scripting this read that difference, so a
# missing prerequisite must not borrow the code that means the tunnel is broken.
#
# Ordered by what can be answered without root, which is the opposite of the
# order they are needed in. A box with no AmneziaWG on it is the common case for
# somebody who has just cloned this - a laptop, a CI runner, a server that has
# not been installed yet - and demanding sudo before saying so costs them a
# round trip to find out the test was never going to run.
skip() {
    printf '%sskip%s: %s\n' "$Y" "$N" "$1"
    exit 2
}

# `ip` is iproute2 and `awg` is what install.sh builds into /usr/bin from
# vendor/amneziawg-tools. Named separately because they come from different
# places and the fix is different: one is a package, the other is this
# repository's own install step.
command -v ip  >/dev/null || skip "iproute2 is not installed, so there are no namespaces to make"
command -v awg >/dev/null || \
    skip "no awg in PATH - this is not an AmneziaWG server. install.sh builds it into /usr/bin"

if [[ $EUID -ne 0 ]]; then
    printf '%sThis needs root%s - it makes two network namespaces. Run:\n\n' "$Y" "$N"
    printf '    sudo %s\n\n' "${BASH_SOURCE[0]}"
    exit 2
fi

# Asked for by creating one, because modprobe succeeding says the module loaded
# and not that this kernel will hand out the device type - a mismatched DKMS
# build is exactly the case worth being clear about. Last of the three: it is
# the only one that needs root to ask.
modprobe amneziawg >/dev/null 2>&1
if ! ip link add awgmtuprobe type amneziawg >/dev/null 2>&1; then
    skip "no amneziawg device type - the module is not built or not loadable here"
fi
ip link del awgmtuprobe >/dev/null 2>&1

cleanup() {
    ip netns del "$NS_S" >/dev/null 2>&1
    ip netns del "$NS_C" >/dev/null 2>&1
}
trap cleanup EXIT
cleanup   # anything a killed run left behind would make every count below a lie

# ------------------------------------------------------------- the profile

# The obfuscation lines `awg setconf` reads. MTU is deliberately not among them:
# it is an awg-quick key, it is set on the device with `ip link` below, and it
# is the variable this whole file is sweeping.
OBFS_KEYS=(Jc Jmin Jmax S1 S2 S3 S4 H1 H2 H3 H4 I1 I2 I3 I4 I5
           HeaderProtectionKey ContentPaddingAddition RandomTrailers
           RekeyAfterTime RekeyTimeout RejectAfterTime KeepaliveTimeout
           MaxHandshakeAttempts)

CONF_DIR="${AWG_CONF_DIR:-/etc/amnezia/amneziawg}"
IFACE="${AWG_IFACE:-awg0}"
LIVE="$CONF_DIR/$IFACE.conf"

OBFS=""
if [[ -r "$LIVE" ]]; then
    # The deployed profile, so on a server this measures what clients are
    # actually carrying rather than what the generator would draw today.
    for key in "${OBFS_KEYS[@]}"; do
        line=$(sed -n "s/^${key}[[:space:]]*=[[:space:]]*\(.*\)$/${key} = \1/p" "$LIVE" | head -1)
        [[ -n "$line" ]] && OBFS+="${line}"$'\n'
    done
    MTU=$(sed -n 's/^MTU[[:space:]]*=[[:space:]]*\([0-9]*\).*/\1/p' "$LIVE" | head -1)
    SOURCE="$LIVE"
else
    # No install on this box, so draw one the way install.sh would. Same
    # generator, so a profile that fragments here is one it would have shipped.
    MTU=${AWG_MTU:-$OBFS_DEFAULT_MTU}
    gen_obfuscation "$MTU"
    OBFS=$(printf 'Jc = %s\nJmin = %s\nJmax = %s\nS1 = %s\nS2 = %s\nS3 = %s\nS4 = %s\n' \
                  "$JC" "$JMIN" "$JMAX" "$S1" "$S2" "$S3" "$S4"
           printf 'H1 = %s\nH2 = %s\nH3 = %s\nH4 = %s\n' "$H1" "$H2" "$H3" "$H4"
           for n in 1 2 3 4 5; do
               slot="I${n}"
               [[ -n "${!slot}" ]] && printf 'I%s = %s\n' "$n" "${!slot}"
           done
           [[ -n "$HPK" ]] && printf 'HeaderProtectionKey = %s\n' "$HPK"
           [[ -n "$CPA" ]] && printf 'ContentPaddingAddition = %s\n' "$CPA"
           [[ -n "$RANDOM_TRAILERS" ]] && printf 'RandomTrailers = %s\n' "$RANDOM_TRAILERS"
           # No effect on any packet's length, and written anyway: the point of
           # the fallback is a config shaped like the one install.sh writes, and
           # a setconf that takes every line but these is not evidence about the
           # file an operator ends up with. gen_advanced only fills them in when
           # the installed tools name them, so there is nothing here to refuse.
           [[ -n "$REKEY_AFTER" ]] && printf 'RekeyAfterTime = %s\n' "$REKEY_AFTER"
           [[ -n "$REKEY_TIMEOUT" ]] && printf 'RekeyTimeout = %s\n' "$REKEY_TIMEOUT"
           [[ -n "$REJECT_AFTER" ]] && printf 'RejectAfterTime = %s\n' "$REJECT_AFTER"
           [[ -n "$KEEPALIVE_TIMEOUT" ]] && printf 'KeepaliveTimeout = %s\n' "$KEEPALIVE_TIMEOUT"
           [[ -n "$MAX_ATTEMPTS" ]] && printf 'MaxHandshakeAttempts = %s\n' "$MAX_ATTEMPTS")
    SOURCE="a fresh draw from lib/obfs.sh"
fi

[[ "$MTU" =~ ^[0-9]+$ ]] || { bad "no usable MTU in ${SOURCE}"; exit 1; }
S4=$(printf '%s\n' "$OBFS" | sed -n 's/^S4[[:space:]]*=[[:space:]]*\([0-9]*\).*/\1/p' | head -1)
S4=${S4:-0}

note "== the profile under test =="
note "   from   ${SOURCE}"
note "   MTU ${MTU}   S4 ${S4}"
note "   datagram   $(( MTU + S4 + 32 + 8 + HDR4 )) bytes over IPv4"
note "              $(( MTU + S4 + 32 + 8 + HDR6 )) bytes over IPv6, what the budget reserves for"
note

# ------------------------------------------------------------- the counters

# /proc/net/snmp prints two "Ip:" lines, a header and its values. Read the
# header rather than counting columns: the list has grown between kernels, and
# a hard-coded index is how this would start reporting ReasmFails as fragments
# on somebody else's box.
snmp() {
    ip netns exec "$1" cat /proc/net/snmp 2>/dev/null | awk -v want="$2" '
        /^Ip:/ { if (!seen) { for (i = 2; i <= NF; i++) col[$i] = i; seen = 1; next }
                 print $(col[want]); exit }'
}

# /proc/net/snmp6 is a flat "name value" table, one counter to a line, so there
# is no header row to line up against and no column to count. Kept apart from
# snmp() rather than folded into it: the two files share a directory and nothing
# else, and a reader that tried to parse both would be harder to check than two.
snmp6() {
    ip netns exec "$1" cat /proc/net/snmp6 2>/dev/null |
        awk -v want="$2" '$1 == want { print $2; exit }'
}

# Which counter is which family's is the only thing that changes between the two
# passes, so it is the only thing these two decide. IPv6 has no fragmentation in
# routers at all, so on that pass every fragment counted was cut by one of the
# two hosts - which is where the oversized datagram is made, and both namespaces
# are read either way.
frags() {
    if (( $2 == 4 )); then snmp "$1" FragCreates; else snmp6 "$1" Ip6FragCreates; fi
}

reasms() {
    if (( $2 == 4 )); then snmp "$1" ReasmReqds; else snmp6 "$1" Ip6ReasmReqds; fi
}

# ------------------------------------------------------------- the harness

# Both interfaces are created inside the namespace that will use them, so each
# one's UDP socket lives there too and the veth between them is the only path
# the datagrams can take. That is what makes the link width below meaningful -
# over loopback there is nothing to be too large for, which is the flaw in the
# self-tests this file exists beside.
build() {
    local family=$1 link=$2 conf_s conf_c priv_s priv_c pub_s pub_c ep_s

    ip netns add "$NS_S" || return 1
    ip netns add "$NS_C" || return 1
    ip link add "$VETH_S" type veth peer name "$VETH_C" || return 1
    ip link set "$VETH_S" netns "$NS_S" || return 1
    ip link set "$VETH_C" netns "$NS_C" || return 1

    # One family on the veth and not both, so nothing the other one sends can
    # reach the counters read below. Return 2 rather than 1 when an address will
    # not go on: that is this box declining to carry the family, which is a pass
    # not measured, and it must not borrow the code that means the tunnel broke.
    #
    # `nodad` because a fresh IPv6 address is tentative until duplicate address
    # detection has finished with it and a socket bound to a tentative address
    # is refused - on a two-host link with made-up ULAs there is nothing for DAD
    # to find, and waiting for it is a second of nothing on every pass.
    if (( family == 4 )); then
        ip -n "$NS_S" addr add "${NET4_S}/30" dev "$VETH_S" || return 2
        ip -n "$NS_C" addr add "${NET4_C}/30" dev "$VETH_C" || return 2
        ep_s="${NET4_S}:${PORT}"
    else
        ip -n "$NS_S" addr add "${NET6_S}/64" dev "$VETH_S" nodad || return 2
        ip -n "$NS_C" addr add "${NET6_C}/64" dev "$VETH_C" nodad || return 2
        ep_s="[${NET6_S}]:${PORT}"
    fi
    ip -n "$NS_S" link set mtu "$link" up dev "$VETH_S" || return 1
    ip -n "$NS_C" link set mtu "$link" up dev "$VETH_C" || return 1
    ip -n "$NS_S" link set lo up
    ip -n "$NS_C" link set lo up

    priv_s=$(awg genkey); pub_s=$(printf '%s' "$priv_s" | awg pubkey)
    priv_c=$(awg genkey); pub_c=$(printf '%s' "$priv_c" | awg pubkey)

    # Fresh keys on both sides: the profile is what is under test and the live
    # server's private key has no business being copied into a test harness.
    conf_s=$(mktemp); conf_c=$(mktemp)
    {
        printf '[Interface]\nPrivateKey = %s\nListenPort = %s\n' "$priv_s" "$PORT"
        printf '%s\n' "$OBFS"
        printf '[Peer]\nPublicKey = %s\nAllowedIPs = %s/32\n' "$pub_c" "$TUN4_C"
    } > "$conf_s"
    {
        printf '[Interface]\nPrivateKey = %s\n' "$priv_c"
        printf '%s\n' "$OBFS"
        printf '[Peer]\nPublicKey = %s\nAllowedIPs = %s/32\nEndpoint = %s\n' \
               "$pub_s" "$TUN4_S" "$ep_s"
    } > "$conf_c"

    ip -n "$NS_S" link add "$DEV_S" type amneziawg || { rm -f "$conf_s" "$conf_c"; return 1; }
    ip -n "$NS_C" link add "$DEV_C" type amneziawg || { rm -f "$conf_s" "$conf_c"; return 1; }
    ip netns exec "$NS_S" awg setconf "$DEV_S" "$conf_s" || { rm -f "$conf_s" "$conf_c"; return 1; }
    ip netns exec "$NS_C" awg setconf "$DEV_C" "$conf_c" || { rm -f "$conf_s" "$conf_c"; return 1; }
    rm -f "$conf_s" "$conf_c"

    ip -n "$NS_S" addr add "${TUN4_S}/32" dev "$DEV_S"
    ip -n "$NS_C" addr add "${TUN4_C}/32" dev "$DEV_C"
    ip -n "$NS_S" link set mtu "$MTU" up dev "$DEV_S" || return 1
    ip -n "$NS_C" link set mtu "$MTU" up dev "$DEV_C" || return 1
    ip -n "$NS_S" route add "${TUN4_C}/32" dev "$DEV_S"
    ip -n "$NS_C" route add "${TUN4_S}/32" dev "$DEV_C"
    return 0
}

# One link width over one address family, end to end. Returns non-zero if the
# tunnel would not come up at all; fragments are reported through `bad` or
# `warn` rather than the return value, because a run that fragments is a failure
# of the profile and not of the test.
measure() {
    local family=$1 link=$2
    local before_c before_s after_c after_s reasm_c reasm_s reasm made rc
    local ping_c ping_s hdr fit_mtu fit_s4 where

    where="link ${link} over IPv${family}"

    # IPv6 will not put a link under 1280 up at all, so there is nothing here to
    # measure rather than something that failed to measure. Only reachable when
    # a width was passed on the command line: the defaults are both above it.
    if (( family == 6 && link < 1280 )); then
        warn "${where}: under the 1280-byte IPv6 minimum, so it was not measured"
        return 0
    fi

    hdr=$HDR4
    (( family == 4 )) || hdr=$HDR6

    cleanup
    build "$family" "$link"
    rc=$?
    if (( rc == 2 )); then
        warn "${where}: this box would not take the addresses, so it was not measured"
        return 0
    fi
    if (( rc != 0 )); then
        bad "${where}: the namespaces or the interfaces would not come up"
        return 1
    fi

    # Small first, so the handshake is done and the tunnel is known to work
    # before anything is counted. A failure here is not a fragment problem.
    if ! ip netns exec "$NS_C" ping -c 2 -W 5 -q "$TUN4_S" >/dev/null 2>&1; then
        bad "${where}: no handshake - the tunnel never came up"
        return 1
    fi

    before_c=$(frags "$NS_C" "$family")
    before_s=$(frags "$NS_S" "$family")

    # An unreadable counter is not zero fragments. Every subtraction below reads
    # an empty string as 0, so a pass that could not find the file at all would
    # print a clean result on a measurement it never took - which is the one
    # outcome this whole file exists to rule out.
    if [[ -z "$before_c" || -z "$before_s" ]]; then
        bad "${where}: the fragment counters could not be read, so nothing was measured"
        return 1
    fi

    # -M do sets DF on the *inner* packet, which is what makes it exactly MTU
    # bytes rather than something the client fragments before the tunnel sees
    # it. The DF that is missing is on the outer datagram, and no ping option
    # can set that one - it is skb->ignore_df in the module (socket.c:85 on the
    # IPv4 path and socket.c:151 on the IPv6 one).
    #
    # Both directions, and both return codes: download is server to client and
    # it is the direction an operator notices, so a test that only read the
    # client's result could pass on the half nobody complains about.
    ip netns exec "$NS_C" ping -c 10 -W 5 -q -M 'do' -s $(( MTU - 28 )) "$TUN4_S" >/dev/null 2>&1
    ping_c=$?
    ip netns exec "$NS_S" ping -c 10 -W 5 -q -M 'do' -s $(( MTU - 28 )) "$TUN4_C" >/dev/null 2>&1
    ping_s=$?

    after_c=$(frags "$NS_C" "$family")
    after_s=$(frags "$NS_S" "$family")
    # The same guard as the one above, and it has to be said twice: a counter
    # readable before the traffic and not after it would leave `made` negative,
    # which reads as no fragments and prints a pass. Half a subtraction is not
    # a measurement either.
    if [[ -z "$after_c" || -z "$after_s" ]]; then
        bad "${where}: the fragment counters stopped being readable mid-run"
        return 1
    fi
    reasm_c=$(reasms "$NS_C" "$family")
    reasm_s=$(reasms "$NS_S" "$family")
    # Through names rather than the command substitutions themselves: an empty
    # substitution inside $(( )) is a syntax error, an empty name is the 0 it
    # ought to be, and these can come back empty on a kernel without the file.
    reasm=$(( reasm_c + reasm_s ))

    made=$(( (after_c - before_c) + (after_s - before_s) ))
    # Counted here and not at the top of the function: everything above this
    # line can leave without having measured anything, and a family whose every
    # pass bailed must not be listed at the end as one that ran.
    measured=$(( measured + 1 ))
    if (( made > 0 )); then
        # Said in this order on purpose. The ping passing is not incidental to
        # the failure, it is the failure: it is what every earlier check in this
        # repository was measuring, and it is what an operator sees before the
        # config reaches a path where the fragments do not survive.
        if (( link >= ENFORCED )); then
            bad "${where}: full-size traffic passed, and cut ${made} fragment(s) to do it" \
                "($reasm reassembled)"
        else
            warn "${where}: full-size traffic passed, and cut ${made} fragment(s) to do it" \
                 "($reasm reassembled) - under ${ENFORCED}, so this is printed and not failed"
            reported=$(( reported + 1 ))
        fi
        fit_mtu=$(( link - S4 - 32 - 8 - HDR6 ))
        fit_s4=$(( link - MTU - 32 - 8 - HDR6 ))
        note "        a datagram of $(( MTU + S4 + 32 + 8 + hdr )) bytes over IPv${family} does"
        note "        not fit ${link}. The budget reserves ${HDR6} for the outer header whatever"
        # Named only where it is a number worth setting, which is the same rule
        # the panel's own advisory follows: under OBFS_HEADER_NONCE the padding
        # can no longer carry a header protection nonce, so an S4 there is a
        # config no key can be added to and not a fix. Below zero there is no S4
        # at all - the MTU is over the link on its own.
        if (( fit_s4 >= OBFS_HEADER_NONCE )); then
            note "        the endpoint resolves to, so lower MTU to ${fit_mtu}, or S4 to ${fit_s4}"
        else
            note "        the endpoint resolves to, so lower MTU to ${fit_mtu}. No S4 is a way"
            note "        out here: ${link} leaves ${fit_s4} for it, under the"
            note "        ${OBFS_HEADER_NONCE} a header protection nonce is read from"
        fi
    elif (( ping_c != 0 || ping_s != 0 )); then
        bad "${where}: no fragments, but full-size traffic did not get through"
    else
        good "${where}: full-size traffic passed whole, no fragments"
    fi
    return 0
}

# Validated once and up front rather than inside the loop below, which would
# otherwise say the same thing about the same bad argument on every family.
CHECKED=()
for link in "${LINKS[@]}"; do
    if [[ "$link" =~ ^[0-9]+$ ]]; then
        CHECKED+=("$link")
    else
        bad "'${link}' is not a link MTU"
    fi
done

# IPv6 has to be in the kernel before there is anything to measure over it. A
# fresh namespace comes up with its own net sysctls at their defaults, so a host
# that has set net.ipv6.conf.all.disable_ipv6 has not disabled it inside the
# namespaces made below - but a kernel booted with ipv6.disable=1 has no
# /proc/net/snmp6 and no inet6 at all, and that is what this asks about.
HAVE6=1
[[ -e /proc/net/snmp6 ]] || HAVE6=0

RAN=""
note "== full-size traffic across a link of each width, over each family =="
for family in 4 6; do
    if (( family == 6 && ! HAVE6 )); then
        warn "IPv6: no /proc/net/snmp6, so this kernel carries no IPv6 - and the family"
        note "    the budget is drawn against is the one this box cannot measure"
        continue
    fi
    done_before=$measured
    for link in "${CHECKED[@]}"; do
        measure "$family" "$link"
    done
    # Listed only if a pass on this family actually reached the counters. Every
    # width can be skipped - an IPv6 run given nothing but links under 1280 is
    # the plain case - and a family named here that measured none of them would
    # be the summary saying the opposite of what happened.
    (( measured > done_before )) && RAN="${RAN} IPv${family}"
done

note
# Printed on every run, passing or not. A box with IPv6 compiled out measures
# half of what this file is about - the outer header the budget was corrected
# for is the IPv6 one - and a run that quietly did half must not read like a run
# that did all of it.
note "families measured:${RAN:- none}"
if (( reported )); then
    note
    note "${reported} overrun(s) above are on links narrower than ${ENFORCED} and are printed"
    note "rather than failed. How much margin to leave under an ordinary 1500-byte path is"
    note "a decision about who this software is for and not an arithmetic error; tests/wire.py"
    note "and tests/obfs.sh print the same number from the arithmetic side."
fi
if (( fail )); then
    note "The handshake burst is not measured here. With RandomTrailers on, every"
    note "packet sent at handshake time is padded into a window derived from the"
    note "largest data packet the peer has sent (peer.h:98, send.c:266), so it is"
    note "bounded by what is measured above and fragments whenever that does."
    note "panel/tests/test_wire_sizes.py asserts that bound directly."
fi
exit "$fail"
