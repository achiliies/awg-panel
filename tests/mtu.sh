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
# The measurement is IpFragCreates out of /proc/net/snmp in the sending
# namespace. That counter is the whole point of this file, because it is the
# only thing on the box that distinguishes the two states the tunnel can be in:
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
# So a fragment here is a failure, not a warning. Nothing about a full-size
# packet needs to be fragmented on a link this wide; if one is, the profile is
# spending more bytes than it has.
#
# Root is needed to make the namespaces, and the amneziawg module has to be
# loadable. Both are why this is a shell test on the side rather than part of
# `make test` - the panel's suite runs on a CI box with neither.
#
# Usage: sudo tests/mtu.sh [link-mtu ...]      (default: 1500 1492)
#
# Exit 0 - full-size traffic crossed every link whole
#      1 - it fragmented on at least one of them
#      2 - could not run here: no iproute2, no awg, not root, or no module

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

NS_S="awg-mtu-server"
NS_C="awg-mtu-client"
DEV_S="awgmtus"
DEV_C="awgmtuc"
VETH_S="vmtus"
VETH_C="vmtuc"
NET4_S="198.51.100.1"
NET4_C="198.51.100.2"
TUN4_S="10.13.99.1"
TUN4_C="10.13.99.2"
PORT=51899

R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; N=$'\e[0m'
fail=0

note() { printf '%s\n' "$*"; }
bad()  { printf '%s✗%s %s\n' "$R" "$N" "$*"; fail=1; }
good() { printf '%s✓%s %s\n' "$G" "$N" "$*"; }

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
    MTU=${AWG_MTU:-1400}
    # shellcheck source=lib/obfs.sh
    . "$REPO/lib/obfs.sh"
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
note "   MTU ${MTU}   S4 ${S4}   expected datagram $(( MTU + S4 + 32 + 8 + 20 )) bytes over IPv4"
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

# ------------------------------------------------------------- the harness

# Both interfaces are created inside the namespace that will use them, so each
# one's UDP socket lives there too and the veth between them is the only path
# the datagrams can take. That is what makes the link width below meaningful -
# over loopback there is nothing to be too large for, which is the flaw in the
# self-tests this file exists beside.
build() {
    local link=$1 conf_s conf_c priv_s priv_c pub_s pub_c

    ip netns add "$NS_S" || return 1
    ip netns add "$NS_C" || return 1
    ip link add "$VETH_S" type veth peer name "$VETH_C" || return 1
    ip link set "$VETH_S" netns "$NS_S" || return 1
    ip link set "$VETH_C" netns "$NS_C" || return 1

    ip -n "$NS_S" addr add "${NET4_S}/30" dev "$VETH_S"
    ip -n "$NS_C" addr add "${NET4_C}/30" dev "$VETH_C"
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
        printf '[Peer]\nPublicKey = %s\nAllowedIPs = %s/32\nEndpoint = %s:%s\n' \
               "$pub_s" "$TUN4_S" "$NET4_S" "$PORT"
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

# One link width, end to end. Returns non-zero if the tunnel would not come up
# at all; fragments are reported through `bad` rather than the return value,
# because a run that fragments is a failure of the profile and not of the test.
measure() {
    local link=$1 before_c before_s after_c after_s reasm ping_rc

    cleanup
    if ! build "$link"; then
        bad "link ${link}: the namespaces or the interfaces would not come up"
        return 1
    fi

    # Small first, so the handshake is done and the tunnel is known to work
    # before anything is counted. A failure here is not a fragment problem.
    if ! ip netns exec "$NS_C" ping -c 2 -W 5 -q "$TUN4_S" >/dev/null 2>&1; then
        bad "link ${link}: no handshake - the tunnel never came up"
        return 1
    fi

    before_c=$(snmp "$NS_C" FragCreates)
    before_s=$(snmp "$NS_S" FragCreates)

    # -M do sets DF on the *inner* packet, which is what makes it exactly MTU
    # bytes rather than something the client fragments before the tunnel sees
    # it. The DF that is missing is on the outer datagram, and no ping option
    # can set that one - it is skb->ignore_df in the module.
    #
    # Both directions: download is server to client, and it is the direction an
    # operator notices, so a test that only pushed one way could pass on the
    # half nobody complains about.
    ip netns exec "$NS_C" ping -c 10 -W 5 -q -M 'do' -s $(( MTU - 28 )) "$TUN4_S" >/dev/null 2>&1
    ping_rc=$?
    ip netns exec "$NS_S" ping -c 10 -W 5 -q -M 'do' -s $(( MTU - 28 )) "$TUN4_C" >/dev/null 2>&1

    after_c=$(snmp "$NS_C" FragCreates)
    after_s=$(snmp "$NS_S" FragCreates)
    reasm=$(( $(snmp "$NS_C" ReasmReqds) + $(snmp "$NS_S" ReasmReqds) ))

    local made=$(( (after_c - before_c) + (after_s - before_s) ))
    if (( made > 0 )); then
        # Said in this order on purpose. The ping passing is not incidental to
        # the failure, it is the failure: it is what every earlier check in this
        # repository was measuring, and it is what an operator sees before the
        # config reaches a path where the fragments do not survive.
        bad "link ${link}: full-size traffic passed, and cut ${made} fragment(s) to do it" \
            "($reasm reassembled)"
        note "        a datagram of $(( MTU + S4 + 32 + 8 + 20 )) bytes does not fit ${link}"
        note "        lower MTU to $(( link - S4 - 32 - 8 - 20 )), or S4 to $(( link - MTU - 60 ))"
    elif (( ping_rc != 0 )); then
        bad "link ${link}: no fragments, but full-size traffic did not get through"
    else
        good "link ${link}: full-size traffic passed whole, no fragments"
    fi
    return 0
}

note "== full-size traffic across a link of each width =="
for link in "${LINKS[@]}"; do
    [[ "$link" =~ ^[0-9]+$ ]] || { bad "'${link}' is not a link MTU"; continue; }
    measure "$link"
done

note
if (( fail )); then
    note "The handshake burst is not measured here. With RandomTrailers on, every"
    note "packet sent at handshake time is padded into a window derived from the"
    note "largest data packet the peer has sent (peer.h:98, send.c:266), so it is"
    note "bounded by what is measured above and fragments whenever that does."
    note "panel/tests/test_wire_sizes.py asserts that bound directly."
fi
exit "$fail"
