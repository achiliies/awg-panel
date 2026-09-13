# shellcheck shell=bash
# shellcheck disable=SC2034
#
# lib/subnet6.sh - the tunnel's IPv6 network, and what the server does with it.
#
# The IPv4 side allocates: parse_cidr reads a pool and next_ip walks it for the
# lowest free address. This side allocates nothing. A client's IPv6 address is
# its IPv4 address's offset within the v4 pool, written into the v6 prefix, so
# 10.13.0.5 out of 10.13.0.0/20 is <prefix>::5. One allocator, two renderings.
#
# That is the whole design decision here, and it is worth saying why. A second
# pool would need its own free-list scan, its own collision rules and its own
# agreement between awg-client, awg-menu and the panel - a fourth place for the
# two families to drift apart, on top of the three that already have to agree
# about the v4 one. Deriving the address instead means a client cannot hold an
# IPv4 address and an IPv6 address that belong to different clients, because
# there is only ever one number.
#
# The tunnel prefix is always a /64. Nothing smaller: every client stack's
# assumptions about a link are written around 64 bits of host part. Nothing
# larger: a /64 is the smallest unit a provider routes, and carving one out of
# a routed /48 or /56 costs nothing.
#
# ---------------------------------------------------------------- the modes
#
# What the server does with the prefix, and the only three answers there are:
#
#   native     the prefix is global and routed to this host, so packets are
#              forwarded and nothing is translated. Clients get real addresses.
#   nat        the host has global IPv6 but no prefix it can carve from - the
#              common VPS that gets a single on-link /64. Clients get a ULA
#              and the server masquerades it. Working IPv6, one shared address.
#   blackhole  the host has no usable IPv6 at all. Clients still get a ULA and
#              still route ::/0 into the tunnel, and the server rejects it.
#
# The last one is the point of this file existing. A tunnel that carries only
# IPv4 while the client has working IPv6 does not fail - it leaks: every
# dual-stack destination is reached over the client's own connection, with the
# client's own address, outside the tunnel, silently. Claiming ::/0 and
# rejecting it is worse connectivity and infinitely better privacy, and it is
# the only honest behaviour for something calling itself a full tunnel.
#
# Rejecting rather than dropping matters as much as claiming. A silent drop
# makes a dual-stack client wait out a connection timeout before it tries IPv4,
# on every connection; an ICMPv6 "administratively prohibited" makes it fail
# over at once. Same privacy, and the difference between a VPN that works and
# one everybody says is slow.

SUBNET6_PREFIX=""  # the /64 as its leading groups, trailing zero groups dropped
SUBNET6_MODE=""    # native | nat | blackhole

# Read one IPv6 /64 into SUBNET6_PREFIX. Accepts what the config actually
# holds in each place it is written: a bare prefix (2001:db8:1:2::/64), the
# server's own Address with host bits set (2001:db8:1:2::1/64), and an Address
# list of which the first IPv6 entry wins - "Address = 10.13.0.1/20,
# fd00::1/64" is a legal line and lib/subnet.sh reads the other half of it.
#
# Returns 1 and leaves SUBNET6_PREFIX empty on anything it cannot read, so a
# caller can treat "no IPv6 configured" and "IPv6 configured wrongly" the same
# way: both mean do not write a v6 address into a client config.
parse_cidr6() {
    local part parts IFS=,
    SUBNET6_PREFIX=""
    read -ra parts <<<"${1:-}"
    for part in "${parts[@]:-}"; do
        if parse_cidr6_one "$part"; then return 0; fi
    done
    return 1
}

parse_cidr6_one() {
    local raw="$1" addr prefix groups joined
    local -a g keep
    raw="${raw//[[:space:]]/}"
    if [[ -z "$raw" ]]; then return 1; fi

    if [[ "$raw" == */* ]]; then addr="${raw%%/*}"; prefix="${raw#*/}"
    else                         addr="$raw";       prefix=64
    fi
    # Only a /64. A shorter prefix would be a block to carve from rather than a
    # link, and a longer one breaks every client stack's idea of a subnet.
    if [[ ! "$prefix" =~ ^[0-9]{1,3}$ ]]; then return 1; fi
    if (( 10#$prefix != 64 )); then return 1; fi

    if ! groups=$(expand6 "$addr"); then return 1; fi

    # Take the first four groups - the network half of a /64 - and drop the
    # trailing zero ones, so that appending "::<host>" spells the address the
    # way a person would write it. Only *trailing* zeros go: an interior zero
    # group has to stay, or the "::" would swallow it too and the address would
    # land in a different network.
    IFS=: read -ra g <<<"$groups"

    # Only a prefix a tunnel can be numbered out of: global unicast (2000::/3),
    # which is what a provider routes to a host, or unique-local (fc00::/7) for
    # a tunnel that carries no IPv6 upstream. Everything else - link-local,
    # multicast, the loopback, the documentation range, the IETF protocol
    # assignments - either cannot be routed or must not be squatted on, and a
    # tunnel numbered out of one comes up and then carries nothing.
    # awg/subnet6.py's usable() rejects exactly this set.
    local g0 g1
    g0=$(( 16#${g[0]} ))
    g1=$(( 16#${g[1]} ))
    if (( (g0 < 0x2000 || g0 > 0x3fff) && (g0 < 0xfc00 || g0 > 0xfdff) )); then return 1; fi
    # 2001::/23 is reserved for IETF protocol assignments, and 2001:db8::/32 is
    # the documentation prefix that half the examples on the internet use.
    if (( g0 == 0x2001 && g1 <= 0x01ff )); then return 1; fi
    if (( g0 == 0x2001 && g1 == 0x0db8 )); then return 1; fi

    keep=("${g[0]}" "${g[1]}" "${g[2]}" "${g[3]}")
    while (( ${#keep[@]} > 1 )) && [[ "${keep[${#keep[@]}-1]}" == 0 ]]; do
        unset "keep[${#keep[@]}-1]"
    done
    # An all-zero prefix is ::/64 - the unspecified address, not a network
    # anybody can be given.
    if (( ${#keep[@]} == 1 )) && [[ "${keep[0]}" == 0 ]]; then return 1; fi

    printf -v joined '%s:' "${keep[@]}"
    SUBNET6_PREFIX="${joined%:}"
    return 0
}

# Expand an IPv6 address to eight colon-separated groups, each lowercase hex
# with leading zeros stripped. Written out by hand because this has to run on a
# server that has nothing installed yet, and because the alternative - shelling
# to python3 - would make the bash tools depend on the panel's interpreter.
expand6() {
    local addr="$1" head tail group rendered
    local -a hg=() tg=() out=()

    if [[ "$addr" != *:* ]]; then return 1; fi
    if [[ ! "$addr" =~ ^[0-9A-Fa-f:]+$ ]]; then return 1; fi

    if [[ "$addr" == *::* ]]; then
        head="${addr%%::*}"
        tail="${addr#*::}"
        # A second "::" is ambiguous, and ":::" arrives here as a tail that
        # still starts with a colon. Both are malformed.
        if [[ "$tail" == *::* ]]; then return 1; fi
        if [[ "$head" == *:  || "$tail" == :* ]]; then return 1; fi
    else
        head="$addr"
        tail=""
        # Without a "::" every group must be spelled out.
        local colons="${addr//[^:]/}"
        if (( ${#colons} != 7 )); then return 1; fi
    fi

    if [[ -n "$head" ]]; then
        local IFS=:
        read -ra hg <<<"$head"
    fi
    if [[ -n "$tail" ]]; then
        local IFS=:
        read -ra tg <<<"$tail"
    fi

    local fill=$(( 8 - ${#hg[@]} - ${#tg[@]} ))
    if (( fill < 0 )); then return 1; fi
    # A "::" standing for no groups at all is not legal; a fully spelled
    # address took the branch above and needs no fill.
    if [[ "$addr" == *::* ]] && (( fill < 1 )); then return 1; fi
    if [[ "$addr" != *::* ]] && (( fill != 0 )); then return 1; fi

    # Length-guarded rather than "${hg[@]:-}": an empty array with a ":-"
    # default expands to one empty string, not to nothing, so a prefix written
    # the ordinary way - anything ending in "::" - would arrive here as a
    # single unparseable group and be rejected.
    if (( ${#hg[@]} )); then
        for group in "${hg[@]}"; do
            if [[ ! "$group" =~ ^[0-9A-Fa-f]{1,4}$ ]]; then return 1; fi
            out+=( "$(( 16#$group ))" )
        done
    fi
    local i
    for (( i = 0; i < fill; i++ )); do out+=( 0 ); done
    if (( ${#tg[@]} )); then
        for group in "${tg[@]}"; do
            if [[ ! "$group" =~ ^[0-9A-Fa-f]{1,4}$ ]]; then return 1; fi
            out+=( "$(( 16#$group ))" )
        done
    fi

    if (( ${#out[@]} != 8 )); then return 1; fi
    printf -v rendered '%x:' "${out[@]}"
    printf '%s' "${rendered%:}"
}

# The tunnel network in CIDR form, for the MASQUERADE source and the summary.
subnet6_cidr() {
    if [[ -z "$SUBNET6_PREFIX" ]]; then return 1; fi
    printf '%s::/64' "$SUBNET6_PREFIX"
}

# The server's own address: ::1 in the tunnel prefix, mirroring the way the
# IPv4 side takes the network's first host.
subnet6_server_addr() {
    if [[ -z "$SUBNET6_PREFIX" ]]; then return 1; fi
    printf '%s::1/64' "$SUBNET6_PREFIX"
}

# A client's address, from its offset within the IPv4 pool. The offset is the
# number the v4 allocator already chose, so nothing is allocated here.
#
# Offsets above 0xffff take two groups. The widest tunnel accepted is a /16,
# whose 65533 clients stop just short of that, so nothing reaches the second
# group today; it stays because what makes the address right is the arithmetic,
# not the ceiling of the moment, and the ceiling has moved before.
subnet6_host_addr() {
    local offset="${1:-}"
    if [[ -z "$SUBNET6_PREFIX" ]]; then return 1; fi
    if [[ ! "$offset" =~ ^[0-9]+$ ]]; then return 1; fi
    if (( offset <= 0 )); then return 1; fi
    if (( offset > 0xffff )); then
        printf '%s::%x:%x' "$SUBNET6_PREFIX" $(( offset >> 16 )) $(( offset & 0xffff ))
    else
        printf '%s::%x' "$SUBNET6_PREFIX" "$offset"
    fi
}

# The same address with the /128 the server config's AllowedIPs needs.
subnet6_host_cidr() {
    local addr
    if ! addr=$(subnet6_host_addr "${1:-}"); then return 1; fi
    printf '%s/128' "$addr"
}

# Would adding ::/0 to this route list close a leak?
#
# True when the list routes the whole of IPv4 and none of IPv6 - a client that
# asked for everything and is getting half of it.
#
# The question is about coverage, not about the text. "0.0.0.0/0" is one way to
# spell a full tunnel; "0.0.0.0/1, 128.0.0.0/1" is the split-default-route form
# that clients and generators write to override a system default route without
# replacing it, and it covers exactly the same addresses. Matching the string
# instead of the coverage read the second as a split tunnel somebody had chosen
# and left every client using it leaking.
#
# Any IPv6 entry at all means no: whoever put a v6 route in this list has
# already decided what the client does with IPv6, and appending ::/0 beside a
# deliberate v6 split tunnel would override a real choice rather than fill in a
# missing one.
#
# Needs ip2int from lib/subnet.sh; every tool that sources this sources that.
# The Python side is awg/subnet6.py's needs_ipv6(), and tests/subnet6.sh holds
# the two to the same answer.
needs_ipv6() {
    local value="${1:-}" part ip prefix start size reach s e
    local -a parts=() ranges=()
    [[ -n "$value" ]] || return 1

    local IFS=,
    read -ra parts <<<"$value"
    unset IFS

    for part in "${parts[@]}"; do
        part="${part//[[:space:]]/}"
        [[ -n "$part" ]] || continue
        if [[ "$part" == *:* ]]; then return 1; fi
        if [[ "$part" == */* ]]; then ip="${part%%/*}"; prefix="${part#*/}"
        else                          ip="$part";       prefix=32
        fi
        if [[ ! "$prefix" =~ ^[0-9]{1,2}$ ]]; then return 1; fi
        if (( 10#$prefix > 32 )); then return 1; fi
        if ! start=$(ip2int "$ip"); then return 1; fi
        size=$(( 1 << (32 - 10#$prefix) ))
        # Host bits cleared, the way a network is written even when the entry
        # carries an address inside it.
        start=$(( start & ~(size - 1) & 0xFFFFFFFF ))
        ranges+=( "$start $(( start + size - 1 ))" )
    done
    (( ${#ranges[@]} )) || return 1

    # Sweep the ranges in order: a gap anywhere means something is not routed,
    # and reaching the last address means everything is.
    reach=-1
    while read -r s e; do
        if (( s > reach + 1 )); then return 1; fi
        if (( e > reach )); then reach=$e; fi
    done < <(printf '%s\n' "${ranges[@]}" | sort -n -k1,1 -k2,2)
    (( reach == 4294967295 ))
}

# The same route list, routing IPv6 too when that would close a leak.
with_ipv6() {
    local value="${1:-}"
    if needs_ipv6 "$value"; then
        # Trimmed at both ends, because Python's .strip() is, and a config
        # written by the CLI has to match one written by the panel character
        # for character - "0.0.0.0/0  , ::/0" is the same routes and a
        # different string.
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"
        printf '%s, ::/0' "$value"
    else
        printf '%s' "$value"
    fi
}

# Is this one of the three modes? Anything else in clients.env is a hand edit,
# and callers fall back to blackhole rather than guessing: of the ways to be
# wrong here, the one that leaks is the one worth not choosing by accident.
#
# "off" is not a mode either. It is what install.sh records for a tunnel that
# carries no IPv6 by choice, and 1b there is the one place that reads it.
subnet6_mode_valid() {
    case "${1:-}" in
        native|nat|blackhole) return 0 ;;
        *) return 1 ;;
    esac
}

# A stable ULA /64 for this server, for the two modes that do not use a routed
# prefix. Derived from the machine ID rather than drawn at random so that
# re-running the installer lands on the same network instead of silently
# renumbering every client; hashed rather than used raw, because the machine ID
# identifies the host and this value ends up in config files that get mailed
# around.
#
# RFC 4193 asks for a random 40-bit global ID, which is what this is: the
# machine ID is unique per host and the hash spreads it, so a collision between
# two servers needs the same 40 bits out of 2^40.
subnet6_ula() {
    local seed hash
    seed=$(cat /etc/machine-id 2>/dev/null || hostname 2>/dev/null || echo awg)
    hash=$(printf 'amneziawg-ula-%s' "$seed" | sha256sum | cut -c1-10)
    printf 'fd%s:%s:%s::/64' "${hash:0:2}" "${hash:2:4}" "${hash:6:4}"
}

# ------------------------------------------------- what this host already has
#
# Reading the machine's own IPv6 rather than the tunnel's. install.sh needs it
# to choose a mode, and awg-menu needs it to say why the mode is what it is -
# an admin looking at "blackhole" deserves to be told it is because the host
# has no IPv6, not left to guess.
#
# Every function here takes the egress interface, because "the network this
# server reaches the internet through" is the installer's answer to a question
# these do not ask.

# The four groups of an address's network half, for comparing prefixes.
ipv6_top4() {
    local groups
    groups=$(expand6 "${1%%/*}") || return 1
    cut -d: -f1-4 <<<"$groups"
}

# Is this global unicast (2000::/3)? Only a global prefix can be routed to a
# host and carved from; a unique-local one on the egress interface belongs to
# somebody's private network and is not a block this host was given.
ipv6_is_global() {
    local groups g0
    groups=$(expand6 "${1%%/*}") || return 1
    g0=$(( 16#${groups%%:*} ))
    (( g0 >= 0x2000 && g0 <= 0x3fff ))
}

# Every global IPv6 address configured on the interface, with its prefix.
ipv6_addrs() {
    ip -6 -o addr show dev "${1:?interface}" scope global 2>/dev/null | awk '{print $4}'
}

# The shortest global prefix this host holds on the interface, or nothing.
#
# A provider that routes a block here leaves it visible as an address or an
# on-link route carrying its real length: a /48 or /56 is a block to carve a
# tunnel /64 out of, a /64 is a single link and cannot be subdivided.
#
# Prefixes of /64 and longer are skipped, which is also what excludes the /126
# or /127 point-to-point transfer network that usually sits beside a routed
# block - by length it would otherwise look like the most specific thing here,
# and numbering a tunnel out of the link to the upstream router would break
# both the tunnel and the route to it.
ipv6_routed_block() {
    local iface="${1:?interface}" cand plen best="" bestlen=64
    while read -r cand; do
        [[ "$cand" == */* ]] || continue
        plen="${cand#*/}"
        [[ "$plen" =~ ^[0-9]+$ ]] || continue
        (( plen < bestlen )) || continue
        ipv6_is_global "$cand" || continue
        bestlen=$plen
        best="$cand"
    done < <(
        ip -6 route show dev "$iface" 2>/dev/null | awk '$1 ~ /:.*\// {print $1}'
        ipv6_addrs "$iface"
    )
    [[ -n "$best" ]] || return 1
    printf '%s' "$best"
}

# Choose a /64 inside BLOCK that this host is not already numbered out of.
#
# The /64s within a block differ in the fourth group, so that is the only part
# that moves. A block shorter than a /48 is treated as its first /48: any /64
# in there is still inside the block, and walking 2^32 candidates to find one
# that is somehow freer would buy nothing.
ipv6_carve() {
    local block="${1:?block}" iface="${2:?interface}"
    local plen top4 g3 base span used=() addr other host3 k cand
    plen="${block#*/}"
    (( plen < 48 )) && plen=48
    top4=$(ipv6_top4 "$block") || return 1
    g3=$(cut -d: -f4 <<<"$top4")
    base=$(( (16#$g3) & ~((1 << (64 - plen)) - 1) & 0xffff ))
    span=$(( 1 << (64 - plen) ))
    (( span > 1 )) || return 1

    host3=$(cut -d: -f1-3 <<<"$top4")
    while read -r addr; do
        other=$(ipv6_top4 "$addr") || continue
        [[ "$(cut -d: -f1-3 <<<"$other")" == "$host3" ]] || continue
        used+=( "$(( 16#$(cut -d: -f4 <<<"$other") ))" )
    done < <(ipv6_addrs "$iface")

    for (( k = 0; k < span; k++ )); do
        cand=$(( base + k ))
        if [[ " ${used[*]-} " != *" $cand "* ]]; then
            printf '%s:%x::/64' "$host3" "$cand"
            return 0
        fi
    done
    return 1
}

# Does this host have IPv6 that works at all - a global address and somewhere
# to send packets? It is the whole difference between masquerading a
# unique-local tunnel out to the internet and having nowhere to send it.
ipv6_host_online() {
    [[ -n "$(ipv6_addrs "${1:?interface}")" ]] || return 1
    [[ -n "$(ip -6 route show default 2>/dev/null)" ]] || return 1
}

# Is the host's IPv6 default route learned from Router Advertisements?
#
# This decides whether enabling forwarding is safe. The kernel stops honouring
# RAs on an interface once net.ipv6.conf.all.forwarding is set - a host that
# forwards is a router, and routers are not supposed to take routing from
# their neighbours - so on a machine whose default route came from an RA,
# turning forwarding on removes that route when the advertisement's lifetime
# runs out. Not at once: minutes later, long after the installer said it had
# finished, and the symptom is a server that has quietly lost IPv6.
#
# accept_ra=2 is the exemption: keep taking RAs even while forwarding. It is
# only set where it is needed, because setting it on a host that is not
# listening for RAs would start it listening, and adding routes nobody asked
# for is its own way to break a working machine.
ipv6_default_from_ra() {
    ip -6 route show default 2>/dev/null | grep -q 'proto ra'
}

# --------------------------------------------- whether the kernel allows it
#
# Every mode puts an IPv6 address on the tunnel interface - blackhole too, on a
# host with no IPv6 anywhere - and a kernel with IPv6 switched off refuses it
# with "IPv6 is disabled on this device", after which awg-quick deletes the
# interface. That is a different state from a host with no IPv6 upstream, and
# nothing above tells the two apart: both have no global address and no default
# route. These do.
#
# AWG_ROOT_DIR is the seam tests/subnet6.sh points at a directory of its own.

# Does this kernel have IPv6 at all? One booted with ipv6.disable=1, or built
# without it, has no net.ipv6 sysctls, and no sysctl can turn it back on.
ipv6_in_kernel() {
    [[ -e "${AWG_ROOT_DIR:-}/proc/sys/net/ipv6/conf/default/disable_ipv6" ]]
}

# Is a number the kernel holds anything but zero? disable_ipv6 is an int and
# every value except 0 switches IPv6 off - -1 included, which a test for a
# positive number would read as on.
sysctl_int_set() {
    [[ "${1:-}" =~ ^-?[0-9]+$ ]] && (( 10#${1#-} != 0 ))
}

# Is IPv6 switched off for interfaces created from now on? A new interface takes
# its disable_ipv6 from "default", so that is what awg-quick's interface starts
# with. Writing "all" writes "default" as well, which is why a host switched off
# under either name reads as off here.
ipv6_disabled_now() {
    local v
    v=$(cat "${AWG_ROOT_DIR:-}/proc/sys/net/ipv6/conf/default/disable_ipv6" 2>/dev/null) || return 1
    sysctl_int_set "$v"
}

# Was the kernel started with one of the ipv6 module's switches on? "disable" is
# ipv6.disable=1, which leaves no IPv6 at all. "disable_ipv6" is
# ipv6.disable_ipv6=1, which is "default" above as every boot begins - so a host
# switched on with `sysctl -w` passes ipv6_disabled_now and is off again after
# the next reboot. No sysctl write moves either of these, which is what makes
# them the answer about the next boot rather than about this one.
ipv6_param_set() {
    local v
    v=$(cat "${AWG_ROOT_DIR:-}/sys/module/ipv6/parameters/${1:?parameter}" 2>/dev/null) || return 1
    sysctl_int_set "$v"
}

# AWG_ROOT_DIR as readlink spells it, so it can be taken back off the front of a
# path that has been through readlink.
sysctl_root() {
    [[ -z "${AWG_ROOT_DIR:-}" ]] || readlink -f -- "$AWG_ROOT_DIR"
}

# The files a sysctl run reads, in the order it reads them. Two runs decide what
# the tunnel's interface is given, and they do not read the same files:
#
#   install  procps's `sysctl --system`, which install.sh runs just before it
#            brings the tunnel up: every name ending in .conf in the five
#            directories, hidden names included, and /etc/sysctl.conf after all
#            of them.
#   boot     systemd-sysctl, at every boot and again for each network interface
#            as udev sees it appear: the same directories without the hidden
#            names, and never /etc/sysctl.conf itself. Debian and Ubuntu link
#            that file in as /etc/sysctl.d/99-sysctl.conf; trixie stopped.
#
# Both sort the names across the directories, and in both the first directory
# holding a name hides that name in the ones after it - also when what it holds
# is a link to /dev/null, which is the documented way to switch off a file a
# package ships. The name is taken and nothing is read from it.
#
# Printed as the files they resolve to, so the /etc/sysctl.conf that procps
# reaches both ways is one file in a message, and is still read twice.
sysctl_system_files() {
    local view="${1:?install or boot}" root d f name
    local -A seen=()
    local -a found=()
    root=$(sysctl_root)
    for d in /etc/sysctl.d /run/sysctl.d /usr/local/lib/sysctl.d /usr/lib/sysctl.d /lib/sysctl.d; do
        for f in "$root$d"/*.conf "$root$d"/.*.conf; do
            [[ -e "$f" || -L "$f" ]] || continue
            name="${f##*/}"
            [[ "$view" == install || "$name" != .* ]] || continue
            [[ -z "${seen[$name]:-}" ]] || continue
            seen[$name]=1
            [[ -f "$f" ]] || continue
            found+=("${name}"$'\t'"$(readlink -f -- "$f")")
        done
    done
    if (( ${#found[@]} )); then
        printf '%s\n' "${found[@]}" | LC_ALL=C sort -t $'\t' -k1,1 | cut -f2-
    fi
    if [[ "$view" == install && -f "$root/etc/sysctl.conf" ]]; then
        readlink -f -- "$root/etc/sysctl.conf"
    fi
}

# What one of those runs leaves disable_ipv6 at, in the two places the tunnel's
# interface takes it from: "default", which the interface copies as it is
# created, and the interface's own key, which systemd-sysctl writes as udev sees
# the interface appear - a race awg-quick's `ip -6 address add` loses either
# way, before the write or after it. Printed as
#
#   default 1|0|-          what "default" is left at, - when nothing writes it
#   iface 1|0|-            what IFACE's own key is written to, the same way
#   at default|iface F:N   each line that writes a 1 to either, in order
#
# Read the way procps and systemd both read them, because whether a line counts
# is the whole of the question:
#
#   - Later lines win, in the file order above. Writing "all" writes "default"
#     too - the kernel does that, not sysctl - so "all = 0" switches the next
#     interface back on as surely as "default = 0" does.
#   - A key with * ? or [ in it is a glob. It writes every key it matches
#     except one that some line names outright, wherever that line is, and
#     glob(3) matches it, so * stops at a separator.
#   - The key and the value are trimmed at both ends. A "-" in front of the key
#     only asks not to hear about failing to set it, and "- net..." with a
#     space after it names no key at all.
#   - The kernel reads an int off the front of the value - decimal or 0x hex,
#     with a sign - and refuses a value that does not start with one. A refused
#     write changes nothing, so "yes" after "1" leaves it switched off.
#   - A key is spelled with dots or with slashes, whichever comes first, and the
#     other one is part of a name: net.ipv6.conf.eth0/100 is device eth0.100.
#     Compared here in the slash spelling.
ipv6_sysctl_state() {
    local view="${1:?install or boot}" iface="${2:?interface}" root
    local -a files
    root=$(sysctl_root)
    mapfile -t files < <(sysctl_system_files "$view")
    if (( ! ${#files[@]} )); then
        printf 'default -\niface -\n'
        return 0
    fi
    awk -v root="$root" -v iface="$iface" '
        function trim(s) { sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s); return s }
        function slashed(k,    i, c, out) {
            if (index(k, "/") && (!index(k, ".") || index(k, "/") < index(k, "."))) return k
            out = ""
            for (i = 1; i <= length(k); i++) {
                c = substr(k, i, 1)
                if (c == ".") c = "/"
                else if (c == "/") c = "."
                out = out c
            }
            return out
        }
        function glob_re(g,    i, c, j, out) {
            out = "^"
            for (i = 1; i <= length(g); i++) {
                c = substr(g, i, 1)
                if (c == "*") out = out "[^/]*"
                else if (c == "?") out = out "[^/]"
                else if (c == "[" && (j = index(substr(g, i + 1), "]")) > 1) {
                    c = substr(g, i + 1, j - 1)
                    if (substr(c, 1, 1) == "!") c = "^" substr(c, 2)
                    out = out "[" c "]"
                    i += j
                }
                else if (index("\\^$.|+(){}[]", c)) out = out "\\" c
                else out = out c
            }
            return out "$"
        }
        BEGIN {
            ALL = "net/ipv6/conf/all/disable_ipv6"
            DEF = "net/ipv6/conf/default/disable_ipv6"
            DEV = "net/ipv6/conf/" iface "/disable_ipv6"
        }
        {
            line = $0
            sub(/^[[:space:]]+/, "", line)
            c = substr(line, 1, 1)
            if (c == "#" || c == ";") next
            if (c == "-") line = substr(line, 2)
            eq = index(line, "=")
            if (eq < 2) next
            key = substr(line, 1, eq - 1)
            if (key ~ /^[[:space:]]/) next
            key = slashed(trim(key))
            val = trim(substr(line, eq + 1))
            sub(/[[:space:]].*/, "", val)
            if (val !~ /^-?([0-9]+|0[xX][0-9a-fA-F]+)$/) next
            sub(/^-/, "", val)
            sub(/^0[xX]/, "", val)
            n++
            K[n] = key
            V[n] = (val ~ /[1-9a-fA-F]/)
            at = FILENAME
            if (root != "" && index(at, root "/") == 1) at = substr(at, length(root) + 1)
            AT[n] = at ":" FNR
            if (index(key, "*") || index(key, "?") || index(key, "[")) G[n] = 1
            else named[key] = 1
        }
        END {
            d = "-"; f = "-"; m = 0
            for (i = 1; i <= n; i++) {
                if (G[i]) {
                    re = glob_re(K[i])
                    td = (!(ALL in named) && ALL ~ re) || (!(DEF in named) && DEF ~ re)
                    tf = !(DEV in named) && DEV ~ re
                } else {
                    td = (K[i] == ALL || K[i] == DEF)
                    tf = (K[i] == DEV)
                }
                if (td) { d = V[i]; if (V[i]) out[++m] = "at default " AT[i] }
                if (tf) { f = V[i]; if (V[i]) out[++m] = "at iface " AT[i] }
            }
            print "default " d
            print "iface " f
            for (i = 1; i <= m; i++) print out[i]
        }
    ' "${files[@]}"
}

# Everything that would keep IFACE from taking an IPv6 address, one reason to a
# line - or nothing, and 1, when there is none:
#
#   kernel disabled   the kernel was started with ipv6.disable=1
#   kernel absent     it has no IPv6 at all: not built in, or not loaded
#   at FILE:LINE      a stored line that switches it off, in whichever of the
#                     two runs above reads it
#   now               off right now, and the `sysctl --system` install.sh runs
#                     before the tunnel comes up does not switch it back on
#   boot              ipv6.disable_ipv6=1 at boot, and nothing systemd-sysctl
#                     reads switches it back on
#
# All of them rather than the first one found, so that one round of fixing is
# enough. A host with IPv6 switched off on its kernel command line very often
# has it switched off in sysctl.conf as well, and hearing about the second from
# a refusal after the reboot that fixed the first is a poor way to be told.
#
# A live value the stored files set back to on is not a reason. install.sh's own
# `sysctl --system` puts it back before the tunnel exists, and a host fixed on
# disk but not yet re-read is not one to turn away.
ipv6_off_reasons() {
    local iface="${1:?interface}" view kind a b
    local install_def="-" boot_def="-" boot_dev="-"
    local -a install_at=() boot_at=() dev_at=() why=()
    local -A said=()

    for view in install boot; do
        while read -r kind a b; do
            case "$view $kind $a" in
                "install default "*) install_def=$a ;;
                "boot default "*)    boot_def=$a ;;
                "boot iface "*)      boot_dev=$a ;;
                "install at default") install_at+=("at $b") ;;
                "boot at default")   boot_at+=("at $b") ;;
                "boot at iface")     dev_at+=("at $b") ;;
            esac
        done < <(ipv6_sysctl_state "$view" "$iface")
    done

    if ! ipv6_in_kernel; then
        if ipv6_param_set disable; then why+=("kernel disabled"); else why+=("kernel absent"); fi
    fi
    [[ "$install_def" != 1 ]] || why+=("${install_at[@]}")
    [[ "$boot_def" != 1 ]]    || why+=("${boot_at[@]}")
    [[ "$boot_dev" != 1 ]]    || why+=("${dev_at[@]}")
    if [[ "$install_def" != 0 ]] && ipv6_disabled_now; then why+=("now"); fi
    if [[ "$boot_def" != 0 ]] && ipv6_param_set disable_ipv6; then why+=("boot"); fi

    (( ${#why[@]} )) || return 1
    for a in "${why[@]}"; do
        [[ -z "${said[$a]:-}" ]] || continue
        said[$a]=1
        printf '%s\n' "$a"
    done
    return 0
}
