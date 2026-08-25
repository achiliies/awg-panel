#!/bin/bash
#
# install.sh
#
# One-shot installer: builds AmneziaWG from source (kernel module, not the
# userspace Go implementation), configures a server with a DPI-evasion
# profile that the Amnezia client app can actually import, and installs the
# web panel that manages it.
#
# Usage:  sudo ./install.sh [options]
#   --port N        listen port          (default: random 20000-59999)
#   --endpoint IP   public address       (default: auto-detected)
#   --iface NAME    tunnel name          (default: awg0)
#   --subnet CIDR   tunnel network       (default: 10.13.0.0/20)
#   --client NAME   first client name    (default: client1)
#   --mtu N         tunnel MTU           (default: 1400)
#   --lang CODE     en | ru              (default: asked, then en)
#   --panel         accepted and ignored; the panel is always installed
#   --panel-port N  panel HTTP port      (default: 2097)
#   --panel-listen  panel bind address   (default: 0.0.0.0)
#   --no-ask        take every default instead of asking for it
#   --no-selftest   skip the end-to-end check
#   --fresh         regenerate the config even if one exists
#   --kmod-ref REF  kernel-module tag/branch (default: pinned release)
#   --tools-ref REF tools tag/branch         (default: pinned release)
#
# The panel is not optional. It is the only thing that adds, revokes or
# accounts for a client, so a server without it is a tunnel nobody can be let
# onto. awg-menu remains for the box itself - ports, updates, diagnostics -
# and awg-panel for the service.
#
# Re-running against an existing installation is a safe upgrade: the module
# and tools are rebuilt, the config, keys and clients are left untouched.
#
set -euo pipefail

# Resolved before anything cd's away; the management tools live in bin/
# and the shared helpers in lib/, both next to this script - in the
# checkout, in the self-extracting bundle, and in the copy this installer
# keeps under /usr/local/share/awg-script for menu-driven upgrades.
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

# The one message here that cannot be translated, and the only one with an
# excuse: t() arrives with the library this line is reporting the absence of.
[[ -f "$SCRIPT_DIR/lib/common.sh" ]] || {
    echo "error: lib/ not found next to this script (incomplete checkout?)" >&2; exit 1; }
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=lib/conf.sh
. "$SCRIPT_DIR/lib/conf.sh"
# shellcheck source=lib/subnet.sh
. "$SCRIPT_DIR/lib/subnet.sh"
# shellcheck source=lib/subnet6.sh
. "$SCRIPT_DIR/lib/subnet6.sh"
# shellcheck source=lib/obfs.sh
. "$SCRIPT_DIR/lib/obfs.sh"
# shellcheck source=lib/firewall.sh
. "$SCRIPT_DIR/lib/firewall.sh"
# shellcheck source=lib/panel.sh
. "$SCRIPT_DIR/lib/panel.sh"
# shellcheck source=lib/acme.sh
. "$SCRIPT_DIR/lib/acme.sh"

IFACE=awg0
SUBNET=10.13.0.0/20
MTU=1400
PORT=""
ENDPOINT=""
CLIENT=client1
# Which of the above the admin actually typed. An upgrade keeps the running
# configuration, so it has to say which requested values it is ignoring - and
# "differs from the default" is not the same question as "was asked for".
SUBNET_GIVEN=0
MTU_GIVEN=0
ENDPOINT_GIVEN=0
PORT_GIVEN=0
# The tunnel's IPv6 network and what the server does with it. "auto" detects
# both; a prefix or a mode given here overrides that half of the answer. See
# lib/subnet6.sh for what the three modes mean.
SUBNET6=auto
IPV6_MODE=auto
SUBNET6_GIVEN=0
# Which of the two was typed, for the message an upgrade prints when it cannot
# act on them. They set one flag between them because they answer one question,
# and naming "--subnet6" at somebody who passed --ipv6 would be a worse answer
# than naming neither.
SUBNET6_FLAGS=()
SELFTEST=1
FRESH=0
# Whether to put the settings below to the operator before building anything.
# See "the questions" further down for what is asked and when.
ASK=1
PANEL_PORT=2097
PANEL_LISTEN=0.0.0.0
# Same question as SUBNET_GIVEN above, for the panel's half: the port and bind
# address live in /etc/awg-panel.env once it exists, and the operator can move
# both from awg-menu or the panel's own settings page. Passing this file's
# defaults down on every run would hand those back to 2097 on every interface -
# see where install-panel.sh is called.
PANEL_PORT_GIVEN=0
PANEL_LISTEN_GIVEN=0
# The three the interview can answer that have nowhere else to live. Empty
# means "not chosen here", which install-panel.sh reads as "generate one" - so
# the random path, username and password are drawn in exactly one place
# whether or not anybody was asked. The password travels to install-panel.sh
# through the environment rather than argv, because /proc/<pid>/cmdline is
# world-readable and /proc/<pid>/environ is not.
PANEL_BASE_PATH=""
PANEL_ADMIN_USER=""
PANEL_ADMIN_PASS=""
SRC_DIR=/usr/local/src/amneziawg
SHARE_DIR=/usr/local/share/awg-script

# Pinned upstream releases (the v3.0 line this repo's parameters target),
# so installs are reproducible and not exposed to whatever lands on
# master. Override with --kmod-ref / --tools-ref.
KMOD_REF=v3.0.20260805
TOOLS_REF=v3.0.20260805

# And the commit each of those tags stood at when this release was tested.
# A tag is a name, not a fact: upstream can move one at any time and a clone
# would follow it without a word, which is the whole failure a pin exists to
# prevent. Checked after the clone, or against vendor/PINNED when the sources
# came with the bundle; a mismatch stops the install either way. Cleared when
# --kmod-ref / --tools-ref name something else, because a commit recorded for
# one ref proves nothing about another.
KMOD_SHA=ce163101dbcddfb64631f5fea52252ea836372b5
TOOLS_SHA=9f70177d204d5be66c5b043518a57b7d62b3f9d1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)       PORT="${2:?}";     PORT_GIVEN=1;     shift 2 ;;
        --endpoint)   ENDPOINT="${2:?}"; ENDPOINT_GIVEN=1; shift 2 ;;
        --iface)      IFACE="${2:?}";    shift 2 ;;
        --subnet)     SUBNET="${2:?}";   SUBNET_GIVEN=1;   shift 2 ;;
        --subnet6)    SUBNET6="${2:?}";  SUBNET6_GIVEN=1
                      SUBNET6_FLAGS+=(--subnet6); shift 2 ;;
        --ipv6)       IPV6_MODE="${2:?}"; SUBNET6_GIVEN=1
                      SUBNET6_FLAGS+=(--ipv6);    shift 2 ;;
        --client)     CLIENT="${2:?}";   shift 2 ;;
        --mtu)        MTU="${2:?}";      MTU_GIVEN=1;      shift 2 ;;
        # Which language the rest of this prints in. Checked here rather than
        # with the other values further down, which are checked after the root
        # test: a language is the one flag somebody can get wrong without
        # being root, and "run with sudo" is a poor answer to a typo in it.
        # In English whatever was asked for, because the two answers it names
        # are the whole of the message and one of them is what the reader
        # failed to type. LANG_GIVEN so the question in step 0 is not put to
        # somebody who has answered it here.
        --lang)
            LANG_CHOICE="${2:?}"; LANG_GIVEN=1
            [[ "$LANG_CHOICE" == en || "$LANG_CHOICE" == ru ]] || die \
                "--lang '${LANG_CHOICE}' is not a language this installs in; it takes 'en' or 'ru'"
            shift 2 ;;
        # Accepted and ignored. The panel is not optional any more - it is the
        # only thing that manages a client, so an install without it would come
        # up as a tunnel nobody can add anybody to. The flag survives to keep
        # old command lines and the panel bundle's own `set -- --panel` working.
        --panel)       shift ;;
        --no-panel)
            die "--no-panel is no longer supported: the web panel is what manages clients,
     and a server without it has no way to add, remove or revoke one. Adding
     and removing clients over SSH now goes through 'awg-panel manage'." ;;
        --panel-port)  PANEL_PORT="${2:?}";   PANEL_PORT_GIVEN=1;   shift 2 ;;
        --panel-listen) PANEL_LISTEN="${2:?}"; PANEL_LISTEN_GIVEN=1; shift 2 ;;
        --no-ask)      ASK=0;            shift ;;
        --no-selftest) SELFTEST=0;       shift ;;
        --fresh)       FRESH=1;          shift ;;
        --kmod-ref)    KMOD_REF="${2:?}";  KMOD_SHA="";  shift 2 ;;
        --tools-ref)   TOOLS_REF="${2:?}"; TOOLS_SHA=""; shift 2 ;;
        -h|--help)
            cat <<'USAGE'
install.sh - build and configure an AmneziaWG server from source

  Builds the kernel module (fast path, not the userspace Go version),
  registers it with DKMS, builds awg/awg-quick, writes a server config with
  a DPI-evasion profile the Amnezia client app can actually import, installs
  the web panel that manages the clients, and creates a first one.

Options:
  --port N        listen port          (default: random 20000-59999)
  --endpoint IP   public address       (default: auto-detected)
  --iface NAME    tunnel name          (default: awg0)
  --subnet CIDR   tunnel network       (default: 10.13.0.0/20)
                  The prefix sets how many clients fit: /24 holds 253,
                  /20 holds 4093, /16 holds 65533. The server takes the
                  first address, clients the rest.
                  /16 is the widest accepted and /30 the narrowest.
                  A bare "10.13.13" still means 10.13.13.0/24.
  --subnet6 CIDR  tunnel IPv6 network  (default: auto)
                  A /64. Auto carves one out of a routed prefix if this
                  host has one, and otherwise derives a stable unique-local
                  /64 from the machine ID.
  --ipv6 MODE     native | nat | blackhole | auto | off  (default: auto)
                  native forwards a routed /64 with no translation; nat
                  masquerades a unique-local /64 behind the host's own
                  address, for a VPS that got a single on-link /64;
                  blackhole hands out a unique-local /64 and rejects it at
                  the server, for a host with no IPv6 upstream.
                  Every mode routes ::/0 into the tunnel, because a client
                  with working IPv6 and a v4-only tunnel does not fail - it
                  sends that traffic outside the tunnel, in the clear, with
                  its own address. "off" restores that behaviour; it is
                  there for hosts that genuinely need it and it says so
                  loudly every time it runs.
                  Both of these settle a tunnel's IPv6 once. A re-run adds
                  IPv6 to a server that has none, and reports them as
                  ignored on one that already has it - the prefix is in
                  every config already issued, and only --fresh renumbers.
  --client NAME   first client name    (default: client1)
  --mtu N         tunnel MTU           (default: 1400)
  --lang CODE     en | ru              (default: asked once, then en)
                  What this installer and the panel installer print. It is
                  not a setting on the server: awg-menu and the panel's own
                  interface pick their language for themselves.
  --panel         accepted and ignored: the web panel is always installed,
                  because it is the only thing that manages clients
  --panel-port N  panel HTTP port      (default: 2097)
  --panel-listen A panel bind address  (default: 0.0.0.0)
  --no-ask        take every default instead of asking for it. A first
                  install asks for the ports, the network size and the
                  panel's path and credentials once the module is built;
                  this, and any run with no terminal to ask into, skips
                  that and uses the defaults above.
  --no-selftest   skip the end-to-end check
  --fresh         regenerate the config even if one exists; without it,
                  re-running is an upgrade that keeps config and clients
  --kmod-ref REF  kernel-module tag/branch (default: pinned release)
  --tools-ref REF tools tag/branch         (default: pinned release)
USAGE
            exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

[[ ${EUID} -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

# Checked here rather than where each one is used. Every value below ends up in
# the server config, and nothing reads that config back until step 10 brings the
# tunnel up - so a mistyped port spent twenty minutes building a kernel module
# and installing a panel before failing with "tunnel failed to start", which
# names neither the flag nor the typo. --subnet already refuses junk this early;
# these are the rest of them.
#
# English in either language, these, for the reason --lang's own error is: the
# message is a flag and the value typed after it, one of which the reader got
# wrong, and neither of those is in Russian on anybody's command line. What
# does get translated is every failure further down - a build that dies, a
# clone github.com would not answer - because those are not about something
# the operator typed, and the person who asked for Russian is exactly the
# person who cannot act on them in English.
valid_port() { [[ "$1" =~ ^[0-9]{1,5}$ ]] && (( 10#$1 >= 1 && 10#$1 <= 65535 )); }
[[ -z "$PORT" ]] || valid_port "$PORT" || die "--port '${PORT}' is not a port between 1 and 65535"
valid_port "$PANEL_PORT" || die "--panel-port '${PANEL_PORT}' is not a port between 1 and 65535"
# 1280 is IPv6's minimum link MTU, and every config this writes claims ::/0:
# below it the tunnel comes up and carries no IPv6 at all.
[[ "$MTU" =~ ^[0-9]{1,5}$ ]] && (( MTU >= 1280 && MTU <= 9000 )) \
    || die "--mtu '${MTU}' is not between 1280 and 9000"
# What the kernel will accept as a device name, which is also what keeps this
# out of the paths built from it below.
[[ "$IFACE" =~ ^[A-Za-z0-9_.-]{1,15}$ ]] \
    || die "--iface '${IFACE}' is not a usable interface name"
# Not a grammar for addresses and host names - it is the set of characters that
# cannot damage what this one is written into. The endpoint reaches every client
# config's Endpoint line, and it reaches clients.env through the replacement half
# of a sed expression, where an unescaped "&" stands for the whole matched line
# and a "|" ends the expression early. Every legal address and host name is
# inside this set; nothing that could rewrite a config as a side effect is.
valid_endpoint() { [[ "$1" =~ ^[A-Za-z0-9.:_-]+$ ]]; }
[[ -z "$ENDPOINT" ]] || valid_endpoint "$ENDPOINT" \
    || die "--endpoint '${ENDPOINT}' is not an address or a host name"

# --iface may have moved us off the default the library assumed.
SERVER_CONF="$CONF_DIR/${IFACE}.conf"

# ---------------------------------------------------------- the subnet
# The tunnel network. Everything downstream is derived from it: the server's
# own Address, the MASQUERADE source, the self-test route, and the pool
# awg-client allocates from. lib/subnet.sh is the same parser awg-client
# uses, with the same rules as awg/subnet.py in the panel - all three read
# the one Address line and have to read it identically.
#
# Sets SUBNET_CIDR (the network), SUBNET_ADDR (the server's own address with
# its prefix) and SUBNET_HOSTS (how many clients fit) from the last
# parse_cidr result.
SUBNET_CIDR="" SUBNET_ADDR="" SUBNET_HOSTS=0
subnet_derive() {
    SUBNET_CIDR=$(subnet_cidr)
    SUBNET_ADDR=$(subnet_server_addr)
    SUBNET_HOSTS=$(subnet_hosts)
}

# Whether the server config's [Interface] already carries an IPv6 address.
#
# Colons on the Address line, before the first [Peer]: once the migration below
# has run, every peer's AllowedIPs carries them too, and a hand-edited file is
# not something to assume about. Asked in one place because two callers ask it -
# what an upgrade decides to do and what the migration then writes have to be
# about the same config, and a second spelling of the question is how they come
# apart.
conf_has_ipv6() {
    [[ -f "$SERVER_CONF" ]] || return 1
    awk '/^\[Peer\]/ { exit }
         /^[[:space:]]*Address[[:space:]]*=/ && /:/ { found = 1 }
         END { exit !found }' "$SERVER_CONF"
}

# Which of the three modes the config's own rules implement. The mode is not a
# value in this file, but it is legible from it: only blackhole rejects, only
# nat masquerades, and a forward rule with neither is native. Read rather than
# guessed, for a server whose clients.env has lost the answer.
conf_ipv6_mode() {
    [[ -f "$SERVER_CONF" ]] || return 1
    if   grep -q 'icmp6-adm-prohibited'            "$SERVER_CONF"; then printf 'blackhole'
    elif grep -q 'ip6tables -t nat -A POSTROUTING' "$SERVER_CONF"; then printf 'nat'
    elif grep -q 'ip6tables -A FORWARD'            "$SERVER_CONF"; then printf 'native'
    else return 1
    fi
}

# Point clients.env at the network the server config says it is on. The
# pre-CIDR SUBNET_BASE is replaced rather than kept beside the new name: three
# octets can only ever mean a /24, and two names for one fact is how they end
# up disagreeing.
env_set_subnet() {
    local file="$1"
    if grep -q '^[[:space:]]*SUBNET_CIDR=' "$file"; then
        sed -i "s|^[[:space:]]*SUBNET_CIDR=.*|SUBNET_CIDR=\"${SUBNET_CIDR}\"|" "$file"
    elif grep -q '^[[:space:]]*SUBNET_BASE=' "$file"; then
        sed -i "s|^[[:space:]]*SUBNET_BASE=.*|SUBNET_CIDR=\"${SUBNET_CIDR}\"|" "$file"
    else
        printf 'SUBNET_CIDR="%s"\n' "$SUBNET_CIDR" >> "$file"
    fi
}

# --------------------------------------------------------- 0. language
# The terminal, not stdin, and not stdout. The documented way to run this is a
# download followed by `sudo bash install-...sh`, where stdin is the terminal
# anyway - but the piped form is what people actually type, and a prompt
# written into a pipe is a prompt nobody ever sees. /dev/tty is the controlling
# terminal in both. Its absence - cron, a CI runner, nohup - means there is
# nobody to answer and every default stands.
#
# Opened here, rather than beside the questions in step 5b where every other
# one is asked, because of the single question below it. The rest of the
# interview waits for the build on purpose - see 5b for why - but a language
# cannot: every line the build prints is already in one language or the other
# by the time the module starts compiling, and asking afterwards would be
# offering a translation of output that has finished scrolling past.
ASK_FD=0
if (( ASK )) && [[ -e /dev/tty ]] && (exec 3<>/dev/tty) 2>/dev/null; then
    exec 3<>/dev/tty
    ASK_FD=1
    # Two signals, one cause. Reading this descriptor raises SIGTTIN, and
    # changing the terminal's mode - ask_drain turns ICANON off for its -n, the
    # password question turns echo off for its -s - is tcsetattr, which raises
    # SIGTTOU. The kernel sends both to a process that is not the foreground
    # process group of that terminal, and the default disposition of both is to
    # stop it. Not kill, not fail: stop, with whatever it was about to print
    # still unprinted.
    #
    # Which is exactly what `curl | sudo bash` does on Ubuntu 26.04. sudo is
    # sudo-rs there, and with stdin a pipe it runs the command under a pty of
    # its own; this shell lands outside that pty's foreground group for as long
    # as it takes sudo-rs to hand it over, and ask_drain - the first thing the
    # installer touches, three lines before its first output - falls into the
    # window. The install stops dead with nothing on the terminal at all, no
    # error and no prompt, and the operator has nothing to report but a hang.
    # Ubuntu 24.04's sudo is the C one, which uses no pty for a piped stdin, so
    # the same command is fine there and the bug looks like it is about AWS.
    #
    # Ignoring them is the fix rather than a way around it. POSIX has both
    # calls stop signalling once the signal is ignored: tcsetattr then proceeds
    # normally, and read fails with EIO instead. So the drain and the prompts
    # do what they were written to do where the terminal is really ours, and
    # report a terminal they cannot use where it is not - which is what the
    # EIO branch at each of them is for. Neither default is wanted here anyway:
    # job control is off in a non-interactive shell, and an installer that
    # suspends itself over a terminal mode is never right.
    trap '' TTOU TTIN
fi
ASK_TTY=$ASK_FD

# Whatever was already waiting on the terminal, thrown away before anything is
# asked, for the reason acme_tty_drain in lib/acme.sh gives at length: a
# newline left over on the terminal reads as somebody accepting a default they
# never saw, and at the two password questions it reads as the password. This
# question is the one with least behind it - nothing has run yet - but a
# `curl | sudo bash` still spends a few seconds downloading, and whatever is
# typed into those seconds is queued and waiting right here.
ask_drain() {
    # Nothing looks at what comes back, hence the "_": read has to put a chunk
    # somewhere, and this loop is only here to make sure it is not left where
    # the next question would find it.
    local _
    (( ASK_FD )) || return 0
    # No test on the chunk. read stops at a newline, so a line that is only a
    # newline comes back empty - and breaking out on that drained exactly one
    # of them and left the rest of the queue to answer the question being
    # printed. Two taps on Enter during the build was all it took. The loop
    # still ends the moment nothing is waiting, because that is what the
    # timeout is for.
    while read -r -t 0.05 -n 4096 -u 3 _; do
        continue
    done
    return 0
}

prompt_installer_language

# ---------------------------------------------------------- 1. preflight
step "$(t "Checking the system" "Проверка системы")"
. /etc/os-release 2>/dev/null || die "$(t "cannot read /etc/os-release" \
                                          "не удалось прочитать /etc/os-release")"
[[ "${ID:-}" =~ ^(ubuntu|debian)$ ]] || warn "$(t "untested on ${ID:-unknown}; continuing" \
                                                  "система ${ID:-unknown} не тестировалась; продолжаем")"
echo "  ${PRETTY_NAME:-unknown}, kernel $(uname -r), $(dpkg --print-architecture)"

KVER=$(uname -r)
DISK=$(df --output=avail -m / | tail -1)
(( DISK > 1500 )) || die "$(t "need ~1.5 GB free on / (have ${DISK} MB)" \
                              "требуется не менее 1.5 ГБ свободного места на / (доступно ${DISK} МБ)")"

# Memory, which until this was the one resource nothing here looked at - and
# the only one whose exhaustion this script cannot report. Disk fills and a
# command fails; memory runs out and the kernel picks a process to kill, or
# systemd-oomd picks a cgroup, and the cgroup is the login session: SSH drops,
# or the tmux window the install was started in disappears, with no error
# printed anywhere and nothing in this script's output to say what happened.
# The only place that can be said is before it happens, which is here.
#
# MemAvailable rather than MemTotal, because what matters is what this run can
# have, and on an upgrade a few hundred MB of the box belongs to the panel
# being upgraded, which stays up throughout. Swap counts toward surviving the
# build; it does not count toward running it in parallel, below.
meminfo() { awk -v k="$1:" '$1 == k { print int($2 / 1024); exit }' /proc/meminfo; }
MEM_AVAIL=$(meminfo MemAvailable)
SWAP_FREE=$(meminfo SwapFree)
[[ -n "$MEM_AVAIL" ]] || MEM_AVAIL=$(meminfo MemFree)
MEM_AVAIL=${MEM_AVAIL:-0}
SWAP_FREE=${SWAP_FREE:-0}

# Compiling this module needs about 300 MB for one gcc, and apt wants around
# 150 MB of that back while it unpacks the headers. Below this the box does
# not survive the build, and there is no version of that failure the operator
# can read afterwards - so it is worth refusing, and worth saying exactly why.
if (( MEM_AVAIL + SWAP_FREE < 450 )); then
    die "$(t "only $(( MEM_AVAIL + SWAP_FREE )) MB of memory is free (${MEM_AVAIL} MB RAM, ${SWAP_FREE} MB swap),
     and building the kernel module needs about 450 MB beyond whatever is
     already running. A machine that runs out here does not report it: the
     kernel's OOM killer, or systemd-oomd, takes the whole login session, so
     the SSH connection or the tmux session this was started in simply
     vanishes. Give it swap and run this again:

       fallocate -l 1G /swapfile && chmod 600 /swapfile
       mkswap /swapfile && swapon /swapfile" \
             "свободно только $(( MEM_AVAIL + SWAP_FREE )) МБ памяти (${MEM_AVAIL} МБ ОЗУ, ${SWAP_FREE} МБ swap),
     а сборке модуля ядра нужно около 450 МБ сверх всего, что уже запущено.
     Машина, у которой память кончится здесь, об этом не сообщит: OOM-killer
     ядра или systemd-oomd снимет весь сеанс входа, и SSH-подключение или
     сессия tmux, из которой шла установка, просто исчезнет. Добавьте swap
     и запустите ещё раз:

       fallocate -l 1G /swapfile && chmod 600 /swapfile
       mkswap /swapfile && swapon /swapfile")"
fi
(( MEM_AVAIL + SWAP_FREE >= 800 )) || \
    warn "$(t "only $(( MEM_AVAIL + SWAP_FREE )) MB of memory is free; this will build, but slowly" \
              "свободно всего $(( MEM_AVAIL + SWAP_FREE )) МБ памяти; сборка возможна, но займет больше времени")"

# How many compilers to run at once. `nproc` alone is a memory decision
# disguised as a speed one: the 4-core 1 GB VPS this software lives on has the
# cores for -j4 and the memory for one of them, and four gcc processes on it
# end the same way as the paragraph above. RAM only, no swap - a compiler that
# has to swap is slower than not starting it at all.
JOBS=$(nproc)
(( MEM_AVAIL / 300 < JOBS )) && JOBS=$(( MEM_AVAIL / 300 ))
(( JOBS > 0 )) || JOBS=1
# The swap clause lifted out of the sentence rather than spliced into the
# middle of it, because the two languages want it in different places and a
# $( ) inside the message would have to be written twice to get it there.
SWAPNOTE=""
(( SWAP_FREE )) && SWAPNOTE=$(t " + ${SWAP_FREE} MB swap" " + ${SWAP_FREE} МБ swap")
echo "$(t "  ${MEM_AVAIL} MB memory free${SWAPNOTE}, ${DISK} MB disk, building with ${JOBS} of $(nproc) core(s)" \
          "  свободно ${MEM_AVAIL} МБ памяти${SWAPNOTE}, ${DISK} МБ на диске, сборка на ${JOBS} из $(nproc) ядер")"

# ------------------------------------------------ 1b. existing install?
# An existing config means an upgrade: rebuild the module and tools but
# leave the config, keys and clients exactly as they are. Regenerating
# the config would break every issued client (they pin the server key).
EXISTING=0
ENDPOINT_MOVED=0
if [[ -f "$CONF_DIR/${IFACE}.conf" && $FRESH -eq 0 ]]; then
    EXISTING=1
    IGNORED=()
    (( PORT_GIVEN ))    && IGNORED+=(--port)
    (( MTU_GIVEN ))     && IGNORED+=(--mtu)
    (( SUBNET_GIVEN ))  && IGNORED+=(--subnet)
    (( ${#IGNORED[@]} )) && \
        warn "$(t "${IGNORED[*]} ignored: a config exists (change them via awg-menu, or pass --fresh)" \
                  "параметры ${IGNORED[*]} проигнорированы: конфигурация уже существует (измените через awg-menu или укажите --fresh)")"
    # The IPv6 pair is ignored too, but only once there is IPv6 for it to
    # disagree with. A server that carries none is the migration case further
    # down and takes both flags; one that already has a prefix cannot, because
    # only half of the answer would ever land. The prefix is in the interface's
    # Address, in every peer's AllowedIPs and in every config already issued,
    # and the mode is a set of ip6tables rules in the same file - an upgrade
    # rewrites none of that. What it did rewrite was clients.env, so the panel
    # was left allocating addresses out of a network the server was not on and
    # the summary printed a mode the kernel was not running, both without a
    # word. Cleared rather than merely reported, so the sections below read the
    # configuration that is actually in force.
    if (( SUBNET6_GIVEN )) && conf_has_ipv6; then
        warn "$(t "${SUBNET6_FLAGS[*]} ignored: this tunnel already carries IPv6, and moving it
     would strand every client config already issued in the old prefix. Pass
     --fresh to renumber the tunnel from nothing." \
                  "${SUBNET6_FLAGS[*]} проигнорировано: этот туннель уже несёт IPv6, и перенос
     оставил бы без связи каждую уже выданную конфигурацию клиента в старом
     префиксе. Передайте --fresh, чтобы перенумеровать туннель с нуля.")"
        SUBNET6_GIVEN=0
        SUBNET6=auto
        IPV6_MODE=auto
    fi
    PORT=$(iface_get ListenPort)
    MTU=$(iface_get MTU)
    parse_cidr "$(iface_get Address)" \
        || die "$(t "cannot read Address from $CONF_DIR/${IFACE}.conf; fix it, or pass --fresh" \
                    "не удалось прочитать Address из $CONF_DIR/${IFACE}.conf; исправьте файл или передайте --fresh")"
    subnet_derive
    # --endpoint is not in that list: it is the one setting an upgrade can move
    # without touching keys or addresses, and an admin passing it has usually
    # just moved the box to a new IP. Honoured, and every client config is
    # rebuilt below so the change actually reaches them.
    OLD_ENDPOINT=""
    [[ -f "$CONF_DIR/clients.env" ]] && \
        OLD_ENDPOINT=$(sed -n 's/^ENDPOINT_HOST="\([^"]*\)".*/\1/p' "$CONF_DIR/clients.env")
    if (( ENDPOINT_GIVEN )) && [[ "$ENDPOINT" != "$OLD_ENDPOINT" ]]; then
        ENDPOINT_MOVED=1
    else
        ENDPOINT="$OLD_ENDPOINT"
    fi
    # Counted into a variable rather than spliced into the message, which
    # would otherwise run the grep once per language every time. "|| true"
    # because grep -c exits 1 on a count of zero, and a tunnel that has not
    # been given a client yet is an ordinary thing to be upgrading.
    PEERS=$(grep -c '^\[Peer\]' "$CONF_DIR/${IFACE}.conf" || true)
    echo "$(t "  upgrade: keeping ${IFACE}.conf - port ${PORT}/udp, ${SUBNET_CIDR}, ${PEERS} client(s)" \
              "  обновление: сохраняем ${IFACE}.conf — порт ${PORT}/udp, ${SUBNET_CIDR}, клиентов: ${PEERS}")"
    (( ENDPOINT_MOVED )) && echo "$(t "  endpoint moving to ${ENDPOINT} - client configs will be rebuilt" \
                                      "  endpoint переезжает на ${ENDPOINT} — конфигурации клиентов будут пересобраны")"
else
    parse_cidr "$SUBNET" || die \
        "--subnet '${SUBNET}' is not an IPv4 network between /16 and /30, e.g. 10.13.13.0/24"
    subnet_derive
fi

# ------------------------------------------------- which kernel, and when
# Two questions this script has to keep apart: which kernel it is compiling
# for now, and which kernel this machine will be running the next time anyone
# looks at it. They are the same answer on a box that has just booted and
# different on every box that has taken a kernel upgrade since - which is
# every box unattended-upgrades has touched and nobody has restarted yet.

# The headers meta package for a kernel's flavour, which is what will pull
# headers for the next kernel image apt installs, so that DKMS has something
# to build against when it does. "6.8.0-137-generic" is generic,
# "6.8.0-1021-aws" is aws, Debian's "6.1.0-18-cloud-amd64" is cloud-amd64:
# the flavour is whatever follows the version and the ABI number. A name
# shaped like none of those - a kernel somebody built by hand, mostly - falls
# back to the distribution's usual default rather than to a package that does
# not exist.
headers_flavour() {
    local kver="$1" flavour
    flavour="${kver#*-}"        # drop the version
    flavour="${flavour#*-}"     # drop the ABI number
    if [[ "$kver" == *-*-* && "$flavour" =~ ^[a-z][a-z0-9-]*$ ]]; then
        printf '%s\n' "$flavour"
        return
    fi
    case "${ID:-}" in
        debian) dpkg --print-architecture ;;
        *)      printf 'generic\n' ;;
    esac
}

# The newest kernel image /boot can hand to the CPU, which is the one the next
# reboot uses unless somebody has pinned the bootloader to another. Read from
# the images rather than from the directories under /lib/modules: a purged
# kernel leaves its module directory behind, and a directory with no image is
# not something this machine can boot into.
next_kernel() {
    local img
    # linux-version understands these version strings better than sort -V does
    # - it is what the kernel packages compare with themselves - but it only
    # ever reads the real /boot, so the seam the tests use comes first.
    if [[ -z "${AWG_BOOT_DIR:-}" ]] && command -v linux-version >/dev/null 2>&1; then
        img=$(linux-version list 2>/dev/null | linux-version sort 2>/dev/null | tail -1)
        if [[ -n "$img" ]]; then printf '%s\n' "$img"; return; fi
    fi
    # Sorted upwards and read from the end rather than sorted downwards and
    # read from the front, because this script runs under `set -o pipefail`: a
    # `head -1` that leaves while sort is still writing makes the pipeline 141,
    # the assignment around it takes that status, and errexit ends an install
    # over a race in a version comparison. tail reads to the end of the input,
    # so there is nothing to race.
    for img in "${AWG_BOOT_DIR:-/boot}"/vmlinuz-*; do
        [[ -e "$img" ]] || continue
        printf '%s\n' "${img##*/vmlinuz-}"
    done | sort -V | tail -1
}

# ------------------------------------------------------ 2. dependencies
step "$(t "Installing build dependencies" "Установка зависимостей для сборки")"
export DEBIAN_FRONTEND=noninteractive
# unattended-upgrades runs on a timer on a stock Ubuntu box, so a plain
# apt-get here fails with "Could not get lock" whenever it happens to be
# mid-run. Wait for the lock instead of dying; apt has supported this since
# 2.0, and the alternative is an installer that fails for reasons that have
# nothing to do with it.
APT="apt-get -o DPkg::Lock::Timeout=300"
# The same argument one line further on. `apt-get update` fails as a whole when
# any single source does, and the source that fails on a box that has been
# around a while is a third-party repository somebody added and stopped
# maintaining - which has nothing to say about whether build-essential can be
# installed. Under errexit that ended the run here, before a word had been
# printed about what was being installed or why it stopped. The install below is
# what actually needs the packages, and it fails with apt's own message naming
# the one it cannot find, which is a far better answer than this line stopping
# on somebody's dead PPA.
$APT update -qq \
    || warn "$(t "apt-get update did not finish cleanly; carrying on with the package
     lists this box already has" \
                 "apt-get update завершился не полностью; продолжаем со списками
     пакетов, которые на этой машине уже есть")"
$APT install -y -qq \
    build-essential dkms git pkg-config libmnl-dev \
    iproute2 iptables iputils-ping qrencode whiptail curl \
    ca-certificates >/dev/null
# Two headers packages, answering the two questions above. The exact one is
# what this build needs. The meta one is what the kernel upgrade after this
# install will need: DKMS rebuilds amneziawg for a new kernel image out of
# that image's own postinst hook, and it can only do so if headers for it
# arrive alongside it. A machine carrying exact headers and nothing else takes
# an image from unattended-upgrades, has nothing to build against, and loses
# the module at the reboot that follows - so the meta package goes on whether
# the exact one was found or not, rather than only as a fallback for the cloud
# kernels that have no exact package at all.
HDR_META="linux-headers-$(headers_flavour "$KVER")"
if ! $APT install -y -qq "linux-headers-${KVER}" >/dev/null 2>&1; then
    warn "$(t "no linux-headers-${KVER} package; trying ${HDR_META}" \
              "пакет linux-headers-${KVER} не найден; пробуем ${HDR_META}")"
fi
$APT install -y -qq "$HDR_META" >/dev/null 2>&1 || true
echo "$(t "  done" "  готово")"

[[ -d "/usr/src/linux-headers-${KVER}" ]] || \
    die "$(t "no headers for the running kernel ${KVER}; if a newer kernel is installed, reboot into it and re-run" \
             "заголовки для текущего ядра ${KVER} не найдены; если установлено более новое ядро, перезагрузитесь в него и повторите установку")"

# ------------------------------------------------- 3. build the module
step "$(t "Building the AmneziaWG kernel module" "Сборка модуля ядра AmneziaWG")"
mkdir -p "$SRC_DIR"
cd "$SRC_DIR"
rm -rf amneziawg-linux-kernel-module amneziawg-tools

# What the tag resolved to, against what it resolved to when this release was
# built. Refusing here costs a re-pin; not refusing means compiling whatever
# the name points at today into a kernel module and loading it as root.
check_pinned_commit() {
    local dir="$1" ref="$2" want="$3" kind="$4" got
    [[ -n "$want" ]] || return 0
    got=$(git -C "$dir" rev-parse HEAD)
    [[ "$got" == "$want" ]] || die \
"${dir} ${ref} is not the commit this release was built against.

  expected  ${want}
  got       ${got}

The tag was moved upstream, or the clone is not the repository it claims to
be. Nothing has been installed. Check the upstream release, then either pin
the new commit in install.sh or pass --${kind}-ref to build a ref you have
chosen yourself."
}

# A release bundle carries both source trees at the pinned commits, so nothing
# here has to reach github.com. That matters more than a clone being tidy: the
# people installing a DPI-evasion VPN are disproportionately behind networks
# that block GitHub, and until this the panel could ship every Python wheel it
# needed and still fail three steps earlier fetching the module it exists for.
#
# Only when both pins are intact. --kmod-ref or --tools-ref means the admin
# asked for something other than what was vendored, so that has to be cloned.
VENDOR_DIR="$SCRIPT_DIR/vendor"
VENDORED=0
if [[ -n "$KMOD_SHA" && -n "$TOOLS_SHA" \
      && -d "$VENDOR_DIR/amneziawg-linux-kernel-module" \
      && -d "$VENDOR_DIR/amneziawg-tools" ]]; then
    # The bundle records what fetch-sources.sh put there, and a copy that does
    # not match this file's pins is a release assembled from a stale vendor/:
    # it would build a different module than the one printed below, quietly.
    # || true on the reads because set -e would otherwise treat a vendor/ with
    # no PINNED as the end of the install, which is the case being explained.
    VEND_KMOD=$(sed -n 's/^KMOD_SHA=//p'  "$VENDOR_DIR/PINNED" 2>/dev/null || true)
    VEND_TOOLS=$(sed -n 's/^TOOLS_SHA=//p' "$VENDOR_DIR/PINNED" 2>/dev/null || true)
    [[ "$VEND_KMOD" == "$KMOD_SHA" && "$VEND_TOOLS" == "$TOOLS_SHA" ]] || die \
"the bundled sources are not the commits this installer pins.

  install.sh wants  ${KMOD_SHA} / ${TOOLS_SHA}
  vendor/ carries   ${VEND_KMOD:-none} / ${VEND_TOOLS:-none}

The release was assembled from a stale vendor/ directory. Rebuild it with
./fetch-sources.sh, or delete vendor/ to clone from upstream instead."
    cp -a "$VENDOR_DIR/amneziawg-linux-kernel-module" .
    cp -a "$VENDOR_DIR/amneziawg-tools" .
    VENDORED=1
else
    command -v git >/dev/null 2>&1 || die "$(t "git is needed to fetch the sources" \
                                               "для загрузки исходников нужен git")"
    git clone --depth 1 -q --branch "$KMOD_REF" \
        https://github.com/amnezia-vpn/amneziawg-linux-kernel-module.git \
        || die "$(t "could not clone the kernel module source from github.com" \
                    "не удалось склонировать исходники модуля ядра с github.com")"
    git clone --depth 1 -q --branch "$TOOLS_REF" \
        https://github.com/amnezia-vpn/amneziawg-tools.git \
        || die "$(t "could not clone the tools source from github.com" \
                    "не удалось склонировать исходники утилит с github.com")"
    check_pinned_commit amneziawg-linux-kernel-module "$KMOD_REF"  "$KMOD_SHA"  kmod
    check_pinned_commit amneziawg-tools               "$TOOLS_REF" "$TOOLS_SHA" tools
fi

# The "|| true" is what keeps this line from ending the install. What it
# produces is one word of the echo below, hence the ":-unknown" there - but
# under `set -o pipefail` a version.h that upstream has moved or renamed makes
# sed exit non-zero, the assignment takes that status, and errexit ends the run
# on the spot with nothing printed, two lines after announcing a module build.
MODVER=$(sed -n 's|.*WIREGUARD_VERSION "\(.*\)".*|\1|p' \
         amneziawg-linux-kernel-module/src/version.h 2>/dev/null | head -1 || true)
echo "$(t "  module ${KMOD_REF} (source version ${MODVER:-unknown}), tools ${TOOLS_REF}" \
          "  модуль ${KMOD_REF} (версия исходников ${MODVER:-unknown}), утилиты ${TOOLS_REF}")"
if (( VENDORED )); then
    echo "$(t "  built from the sources in this bundle, at the pinned commits" \
              "  собрано из исходного кода в составе пакета (зафиксированные коммиты)")"
elif [[ -n "$KMOD_SHA" && -n "$TOOLS_SHA" ]]; then
    echo "$(t "  cloned from github.com; both commits match the pins in install.sh" \
              "  склонировано с github.com; коммиты соответствуют зафиксированным в install.sh")"
else
    warn "$(t "a ref was given on the command line, so its commit was not verified" \
              "указан пользовательский ref в командной строке, проверка коммита пропущена")"
fi

cd amneziawg-linux-kernel-module/src
# -j from the preflight, not from nproc: on a small box the number of cores is
# not the number of compilers it can hold. Exported as well as passed, because
# dkms below runs a make of its own out of the module's dkms.conf, and MAKEFLAGS
# is the only say this script gets over that one.
export MAKEFLAGS="-j${JOBS}"
make -j"$JOBS" >/dev/null 2>&1 || { make; die "$(t "module build failed" \
                                                   "сборка модуля не удалась")"; }
echo "$(t "  compiled" "  скомпилировано")"

# DKMS so the module survives kernel upgrades instead of vanishing.
step "$(t "Registering the module with DKMS" "Регистрация модуля в DKMS")"
DKMSVER=$(sed -n 's|^PACKAGE_VERSION="\(.*\)"|\1|p' dkms.conf)

# Whatever is registered now, which is not necessarily what is about to be:
# upstream moves PACKAGE_VERSION between releases. Removing only the version
# being installed left the previous one registered for good, and two entries
# under one module name both autoinstall on every kernel upgrade with nothing
# deciding which .ko wins. dkms prints "amneziawg, 1.0.0, ..." or
# "amneziawg/1.0.0, ..." depending on its own major version; take both.
dkms_registered() {
    dkms status amneziawg 2>/dev/null \
        | sed -n 's|^amneziawg[,/][[:space:]]*\([^,:]*\).*|\1|p' \
        | sed 's|[[:space:]]*$||' | sort -u
}
while read -r OLDVER; do
    # The rm below runs as root against a path built from this string, so it
    # has to look like a version and nothing else.
    [[ "$OLDVER" =~ ^[A-Za-z0-9._+-]+$ ]] || continue
    echo "$(t "  removing the registered amneziawg ${OLDVER}" \
              "  удаление ранее зарегистрированного amneziawg ${OLDVER}")"
    dkms remove -m amneziawg -v "$OLDVER" --all >/dev/null 2>&1 || true
    rm -rf "/usr/src/amneziawg-${OLDVER}"
done < <(dkms_registered)

rm -rf "/usr/src/amneziawg-${DKMSVER}"
make dkms-install >/dev/null 2>&1
dkms add    -m amneziawg -v "$DKMSVER" >/dev/null 2>&1 || true
dkms build  -m amneziawg -v "$DKMSVER" >/dev/null 2>&1 || die "$(t "dkms build failed" \
                                                                   "сборка через dkms не удалась")"
dkms install -m amneziawg -v "$DKMSVER" --force >/dev/null 2>&1 || die "$(t "dkms install failed" \
                                                                            "установка через dkms не удалась")"
depmod -a

# The module already in memory is the one from before this upgrade, and
# modprobe does nothing at all when the name is present: without this, an
# upgrade wrote new code to disk, reported success, and went on running the
# old code until the machine happened to reboot. The interface is what holds
# the reference, so it comes down first - step 9 brings it back up.
# The line above is true of a run that reaches step 9. A run that dies before it
# - a build error, a dependency that will not install, a bug in this script -
# left the server with no VPN at all and said nothing about it, which is a far
# worse outcome than the upgrade simply not happening. An upgrade is allowed to
# fail; it is not allowed to hand back a machine that was working and is now
# offline.
#
# So whatever goes wrong from here on, the tunnel that was running when this
# started is put back before the script gives up. The original exit status is
# preserved, because the failure is still a failure and the caller has to see
# it.
IFACE_WAS_UP=0
if ip link show "$IFACE" &>/dev/null; then
    IFACE_WAS_UP=1
fi

restore_iface_on_failure() {
    local rc=$?
    trap - EXIT
    if (( rc == 0 )); then
        exit 0
    fi
    if (( IFACE_WAS_UP )) && ! ip link show "$IFACE" &>/dev/null; then
        warn "$(t "this run did not finish and ${IFACE} is down; putting it back as it was" \
                  "установка не завершилась и ${IFACE} остановлен; возвращаем в исходное состояние")"
        if awg-quick up "$IFACE" >/dev/null 2>&1; then
            warn "$(t "${IFACE} is up again on the configuration it had before" \
                      "интерфейс ${IFACE} снова запущен с прежней конфигурацией")"
        else
            warn "$(t "${IFACE} could not be brought back up. Run 'awg-quick up ${IFACE}' to see why" \
                      "не удалось перезапустить ${IFACE}. Выполните 'awg-quick up ${IFACE}' для проверки")"
        fi
    fi
    exit "$rc"
}
trap restore_iface_on_failure EXIT

KMOD_STALE=0
if [[ -d /sys/module/amneziawg ]]; then
    awg-quick down "$IFACE" >/dev/null 2>&1 || true
    if modprobe -r amneziawg >/dev/null 2>&1; then
        echo "$(t "  unloaded the running module so the new one takes over now" \
                  "  работающий модуль выгружен, новый модуль активирован")"
    else
        # Another amneziawg interface, or something else holding it open.
        # Not fatal: the installed module is correct and boot will load it.
        KMOD_STALE=1
        warn "$(t "the running module could not be unloaded; the version just installed takes over at the next reboot" \
                  "не удалось выгрузить работающий модуль; установленная версия активируется после перезагрузки")"
    fi
fi
modprobe amneziawg || die "$(t "module will not load" "модуль не загружается")"
echo "  $(dkms status amneziawg | head -1)"

# --------------------------------------- 3b. the kernel that boots next
# Everything above built for the kernel that is running. That is not always
# the kernel that will be running an hour from now, and the difference is what
# turns a finished install into a server with no VPN on it: an install on a box
# whose newer kernel image is already unpacked - any box unattended-upgrades
# has touched since its last reboot - ends with a tunnel that works, and the
# reboot everybody performs afterwards lands on a kernel that has no
# amneziawg.ko anywhere on the disk.
#
# DKMS does not cover this one, and its name on the summary line is why the
# hole took so long to see. It rebuilds a module when a kernel is installed,
# from that kernel package's postinst hook, so it covers every kernel that
# arrives after amneziawg was registered and no kernel that was already
# sitting there when it was.
#
# Neither half of the symptom points here. `modinfo amneziawg` says nothing at
# all, and awg-quick - whose `ip link add type amneziawg` had no module to
# autoload - reports "Cannot find device awg0", which reads like a broken
# config file and is not one.
#
# So the newest bootable kernel gets a module of its own. Newest, rather than
# whatever the bootloader has been told to prefer: reading GRUB's saved entry
# is a great deal of parsing for a case that is rare and self-correcting,
# where guessing wrong costs one extra module nobody loads.
KMOD_NEXT=""
NEXT_KVER=$(next_kernel)
if [[ -n "$NEXT_KVER" && "$NEXT_KVER" != "$KVER" ]]; then
    step "$(t "Building for ${NEXT_KVER}, the kernel this machine boots next" \
              "Сборка для ядра ${NEXT_KVER}, с которым машина загрузится в следующий раз")"
    [[ -d "/lib/modules/${NEXT_KVER}/build" ]] \
        || $APT install -y -qq "linux-headers-${NEXT_KVER}" >/dev/null 2>&1 || true
    if [[ -d "/lib/modules/${NEXT_KVER}/build" ]] \
       && dkms build   -m amneziawg -v "$DKMSVER" -k "$NEXT_KVER" >/dev/null 2>&1 \
       && dkms install -m amneziawg -v "$DKMSVER" -k "$NEXT_KVER" --force >/dev/null 2>&1; then
        depmod -a "$NEXT_KVER" >/dev/null 2>&1 || true
        echo "$(t "  built for ${NEXT_KVER} as well - the tunnel will survive the reboot" \
                  "  собрано также для ${NEXT_KVER} — туннель переживёт перезагрузку")"
    else
        # Not fatal. What was asked for is installed and running, and saying
        # so and stopping would leave a working tunnel uninstalled over a
        # problem the admin can fix with one reboot.
        KMOD_NEXT="$NEXT_KVER"
        warn "$(t "no module could be built for ${NEXT_KVER}, and that is the kernel
     the next reboot will use: the tunnel will not come up on it. Reboot
     into ${NEXT_KVER} and run this installer again." \
                  "не удалось собрать модуль для ${NEXT_KVER}, а именно с этим ядром
     произойдёт следующая загрузка: туннель на нём не запустится.
     Перезагрузитесь в ${NEXT_KVER} и запустите установщик заново.")"
    fi
fi

# ------------------------------------------------- 4. build the tools
step "$(t "Building the userspace tools (awg / awg-quick)" \
          "Сборка утилит пространства пользователя (awg / awg-quick)")"
cd "$SRC_DIR/amneziawg-tools/src"
make -j"$JOBS" >/dev/null 2>&1 || { make; die "$(t "tools build failed" \
                                                   "сборка утилит не удалась")"; }
make install >/dev/null 2>&1
echo "  $(awg --version)"

# ------------------------------------------------------ 5. networking
step "$(t "Detecting the network" "Определение сетевых параметров")"
# By the "dev" keyword rather than by position. A default route with a gateway
# - "default via 10.0.0.1 dev eth0 proto dhcp" - does put the interface in the
# fifth field, and every cloud image writes one, which is why reading it that
# way survived. A route without one does not: "default dev ppp0 scope link" is
# what a PPPoE link, a point-to-point WAN and several container setups have, and
# the fifth field there is the word "scope".
#
# Nothing reports that, which is the part worth knowing. iptables accepts an
# interface name that matches no device - rules are allowed to name one that has
# yet to appear - so the MASQUERADE and FORWARD rules went in without complaint
# and matched nothing. The tunnel came up, the handshake completed, the panel
# showed the peer connected, and no client could reach anything past the server.
# A multipath default is read the same way, taking the first nexthop, where the
# old expression saw a line that was only the word "default" and gave up.
WAN=$(ip route show default |
      awk '{ for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit } }')
[[ -n "$WAN" ]] || die "$(t "cannot determine the default route interface" \
                            "не удалось определить интерфейс маршрута по умолчанию")"

if [[ -z "$ENDPOINT" ]]; then
    TOK=$(curl -sX PUT 'http://169.254.169.254/latest/api/token' \
          -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' --max-time 3 2>/dev/null || true)
    ENDPOINT=$(curl -s -H "X-aws-ec2-metadata-token: ${TOK:-}" --max-time 3 \
               http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true)
    [[ "$ENDPOINT" =~ ^[0-9.]+$ ]] || ENDPOINT=$(curl -s --max-time 5 https://api.ipify.org || true)
    [[ -n "$ENDPOINT" ]] || die "$(t "cannot determine the public IP; pass --endpoint" \
                                     "не удалось определить публичный IP; передайте --endpoint")"
    # Only what was detected here. A value that came from the flag was checked
    # before anything was built, and one that came from clients.env is what this
    # server has been answering on for months - neither is this line's business.
    # What is, is a captive portal or a proxy answering the lookup with a page of
    # HTML: without this that page became the endpoint, and it went into every
    # client config and into the sed that writes clients.env.
    valid_endpoint "$ENDPOINT" || die \
        "the address detected for this server is not one:

  ${ENDPOINT}

A captive portal or a proxy answering the lookup with a page of its own is the
ordinary way to get here. Pass --endpoint with this server's public address."
fi

# The port nobody chose. Random because 51820 is a fingerprint on its own, and
# redrawn when the draw lands on something already bound: the installer's own
# default has no business being the thing that collides, and out of forty
# thousand ports it almost never is. Ten attempts and then it stops - a machine
# where that many draws are all busy has something stranger going on than a
# port conflict, so the installer refuses to proceed rather than picking a
# port nothing can bind.
if [[ -z "$PORT" ]]; then
    for _ in {1..10}; do
        PORT=$(shuf -i 20000-59999 -n 1)
        port_busy udp "$PORT" || break
        PORT=""
    done
    [[ -n "$PORT" ]] || die "$(t "could not find a free UDP port between 20000 and 59999 after 10 attempts; pass --port" \
                                 "не удалось найти свободный UDP-порт от 20000 до 59999 за 10 попыток; укажите --port")"
fi
# The interface only, because it is the one of the three this step used to
# print that is not repeated later. The endpoint and the port are both in the
# summary at the end, and an interactive run has already put the port in the
# list of answers it confirms - said here as well, they arrived three times in
# one install.
echo "$(t "  egress ${WAN}" "  исходящий интерфейс ${WAN}")"

WANMTU=$(cat "/sys/class/net/${WAN}/mtu" 2>/dev/null || echo 1500)
(( WANMTU > 1500 )) && warn "$(t "${WAN} has MTU ${WANMTU} (jumbo); tunnel MTU pinned to ${MTU} so internet traffic does not black-hole" \
                                 "интерфейс ${WAN} имеет MTU ${WANMTU} (jumbo frames); MTU туннеля зафиксирован на ${MTU} во избежание потери пакетов")"

# ------------------------------------------------------ 5b. the questions
# Everything above this point is the same on every machine: the module, the
# tools, and the network this box already has. Everything below it is a
# choice, and until now the only way to make one was a flag typed before any
# of it had been proven to work. So an operator who wanted the panel somewhere
# other than its default either knew to pass the flag blind, or found out
# twenty minutes later from an installer that had already finished and written
# the answer into /etc.
#
# The questions go here instead. The module is built and registered, the tools
# are installed and the endpoint is known, so the install is as close to
# certain as it is ever going to get - and nothing has touched /etc yet, so an
# answer given now still costs nothing to act on.
#
# Only what has not been decided already is asked. A flag on the command line
# is an answer; an existing tunnel config answers the tunnel's half; an
# installed panel answers the panel's, which is why an upgrade that never had
# a panel is still asked about one. --no-ask, and any run with no terminal to
# ask into, takes every default without a word.

# The terminal these are asked on was opened at the top of the script, for the
# language question, which has to be answered before there is any output to
# put a language on. ASK_FD and ASK_TTY have been set since then.

# Whether there is already an account to sign in with, and what it is called.
# Both questions below turn on it: with no account the answers create one and a
# blank answer draws a random value, and with one already there a blank answer
# leaves it exactly as it is and anything typed replaces it. The name comes from
# the panel rather than being assumed to be "admin", because it can be renamed
# from the panel's own settings and the operator who re-runs the installer to
# get back in is precisely the one who renamed it.
#
# A panel carrying more than one account answers nothing here: accountname
# refuses to guess which of them was meant, and neither should this. Those
# installs keep the accounts they have and are told to use awg-panel passwd.
PANEL_ADMIN_EXISTS=0
PANEL_ADMIN_CURRENT=""
if [[ -f /var/lib/awg-panel/db.sqlite3 ]]; then
    PANEL_ADMIN_EXISTS=1
    PANEL_ADMIN_CURRENT=$(/usr/local/bin/awg-panel manage accountname 2>/dev/null | tr -d '[:space:]' || true)
fi

ASK_Q=()
if (( ASK_TTY )); then
    if (( ! EXISTING )); then
        (( PORT_GIVEN ))   || ASK_Q+=(port)
        (( SUBNET_GIVEN )) || ASK_Q+=(subnet)
    fi
    if [[ ! -f /etc/awg-panel.env ]]; then
        (( PANEL_PORT_GIVEN )) || ASK_Q+=(panelport)
        ASK_Q+=(panelpath)
    fi
    # Asked on an upgrade too, which it never used to be. The account outlives
    # the panel's code - it is in /var/lib, not /opt - so re-running the
    # installer over an existing panel skipped both questions and then printed
    # no credentials either, and an operator who had lost the password had
    # nothing to do but read the summary telling them they already had one.
    if (( ! PANEL_ADMIN_EXISTS )) || [[ -n "$PANEL_ADMIN_CURRENT" ]]; then
        ASK_Q+=(paneluser panelpass)
    fi
fi

say() { printf '%s\n' "$*" >&3; }

# A dashed rule with the question's number and name on it, wide enough to
# separate one question from the last without drawing a box nobody's terminal
# renders the same way.
ask_rule() {
    local label="$1" pad dashes
    pad=$(( 62 - ${#label} )); (( pad < 3 )) && pad=3
    printf -v dashes '%*s' "$pad" ''
    printf '\n  %s----%s %s%s%s %s%s%s\n' \
        "$DIM" "$N" "$B" "$label" "$N" "$DIM" "${dashes// /-}" "$N" >&3
}

ask_body() { local l; for l in "$@"; do printf '       %s\n' "$l" >&3; done; }
ask_warn() { printf '       %s!! %s%s\n' "$Y" "$*" "$N" >&3; }

# What was typed, in REPLY_VAL; empty means "take the default". Leading and
# trailing whitespace goes with read's own field splitting, which is what
# anybody typing a port with a stray space in front of it meant.
#
# End of file - a closed terminal, or Ctrl-D - reads as Enter and puts ASK_TTY
# back to 0, so a session that goes away halfway through finishes on defaults
# rather than spinning against a descriptor that will never produce another
# line.
REPLY_VAL=""
ask_line() {
    local prompt="$1" secret="${2:-}" line=""
    ask_drain
    printf '\n       %s%s%s ' "$B" "$prompt" "$N" >&3
    if [[ -n "$secret" ]]; then
        read -r -s -u 3 line 2>/dev/null || ASK_TTY=0
        printf '\n' >&3
    else
        read -r -u 3 line 2>/dev/null || ASK_TTY=0
    fi
    REPLY_VAL="$line"
}

# "/awg/office/" out of "awg/office", "/awg/office" or "/awg//office". The same
# normalisation the panel's own settings do, and the same characters its
# settings page accepts, because this string becomes the cookie paths, the URL
# map and the SPA's router basename at once and a disagreement between those
# three logs everybody out. Fails on anything else, rather than quietly
# dropping the part it did not like.
norm_base_path() {
    local raw="${1//[[:space:]]/}" seg norm=""
    local -a parts=()
    IFS=/ read -ra parts <<<"$raw"
    for seg in "${parts[@]}"; do
        [[ -n "$seg" ]] || continue
        [[ "$seg" =~ ^[A-Za-z0-9._~-]+$ ]] || return 1
        norm+="/$seg"
    done
    printf '%s/' "$norm"
}

if (( ${#ASK_Q[@]} )); then
    step "$(t "Server settings" "Параметры сервера")"
    say ""
    # "N вопросов" is right for 5 and wrong for 2, 3 and 4, which is what an
    # upgrade asks; Russian declines the noun after a numeral by the numeral's
    # last digit. Every count in this file is printed as "noun: N" for that
    # reason - the colon takes the agreement out of the sentence, and the
    # number can then be anything.
    say "$(t "  ${#ASK_Q[@]} questions, and the rest of the install runs unattended." \
             "  Вопросов: ${#ASK_Q[@]}, дальнейшая установка пройдёт автоматически.")"
    say "$(t "  Each prompt says what ${B}Enter${N} takes. All of it can be changed later" \
             "  В каждой подсказке указано значение по умолчанию для ${B}Enter${N}. Все параметры можно")"
    say "$(t "  from ${B}awg-menu${N} or the panel, and ${B}--no-ask${N} skips the questions." \
             "  изменить позже через ${B}awg-menu${N} или веб-панель; флаг ${B}--no-ask${N} пропускает опрос.")"

    # The width of the label column in the summary at the end of the
    # interview. One number for the whole table rather than a width per line,
    # because what makes a table a table is that every row breaks in the same
    # place - and the Russian words for these six things run long enough that
    # the English column would put half of them in the value's seat.
    COL=$(t 14 18)

    CHOSEN=()
    for Q in "${ASK_Q[@]}"; do
        QN=$(( ${#CHOSEN[@]} + 1 ))
        case "$Q" in

        port)
            ask_rule "${QN}/${#ASK_Q[@]}  $(t "VPN port" "Порт VPN")"
            ask_body "$(t "The UDP port clients dial. 1-65535." \
                          "UDP-порт, к которому подключаются клиенты. 1-65535.")"
            while :; do
                ask_line "$(t "VPN port [Enter = ${PORT}]:" "порт VPN [Enter = ${PORT}]:")"
                CAND="$PORT"
                if [[ -n "$REPLY_VAL" ]]; then
                    if valid_port "$REPLY_VAL"; then
                        CAND=$(( 10#$REPLY_VAL ))
                    else
                        ask_warn "$(t "'${REPLY_VAL}' is not a port between 1 and 65535." \
                                      "'${REPLY_VAL}' — не порт в диапазоне от 1 до 65535.")"
                        continue
                    fi
                fi
                if port_busy udp "$CAND"; then
                    HOLDER=$(port_holder udp "$CAND")
                    ask_warn "$(t "${HOLDER:-something} already holds ${CAND}/udp; the tunnel will not come up there" \
                                  "${HOLDER:-что-то} уже занимает ${CAND}/udp; туннель там не поднимется")"
                    (( ASK_TTY )) || break
                    continue
                fi
                PORT="$CAND"
                break
            done
            CHOSEN+=("$(pad "$(t "VPN port" "порт VPN")" "$COL") ${PORT}/udp")
            ;;

        subnet)
            ask_rule "${QN}/${#ASK_Q[@]}  $(t "Tunnel network" "Сеть туннеля")"
            ask_body "$(t "Client capacity: /24 = 253, /22 = 1021, /20 = 4093, /16 = 65533." \
                          "Сколько клиентов поместится: /24 = 253, /22 = 1021, /20 = 4093, /16 = 65533.")" \
                     "$(t "Type a prefix (22) or a whole network (10.8.0.0/22). /16 is the widest allowed." \
                          "Введите префикс (22) или сеть целиком (10.8.0.0/22). /16 — самая широкая допустимая сеть.")"
            while :; do
                ask_line "$(t "network [Enter = ${SUBNET_CIDR}, ${SUBNET_HOSTS} clients]:" \
                              "сеть [Enter = ${SUBNET_CIDR}, клиентов: ${SUBNET_HOSTS}]:")"
                [[ -n "$REPLY_VAL" ]] || break
                CAND="$REPLY_VAL"
                [[ "$CAND" =~ ^/?([0-9]{1,2})$ ]] && CAND="${SUBNET%%/*}/${BASH_REMATCH[1]}"
                if parse_cidr "$CAND"; then subnet_derive; SUBNET="$SUBNET_CIDR"; break; fi
                ask_warn "$(t "'${REPLY_VAL}' is neither a prefix between 16 and 30 nor a network like 10.8.0.0/22." \
                              "'${REPLY_VAL}' — не префикс от 16 до 30 и не сеть вида 10.8.0.0/22.")"
            done
            CHOSEN+=("$(pad "$(t "network" "сеть")" "$COL") ${SUBNET_CIDR}   $(t "(${SUBNET_HOSTS} clients)" "(клиентов: ${SUBNET_HOSTS})")")
            ;;

        panelport)
            ask_rule "${QN}/${#ASK_Q[@]}  $(t "Panel port" "Порт панели")"
            ask_body "$(t "The TCP port the web panel answers on. Opened in this host's" \
                          "TCP-порт, на котором отвечает веб-панель. Открывается в фаерволе")" \
                     "$(t "firewall; a cloud security group still needs the rule by hand." \
                          "этой машины; в облачной security group правило всё равно вручную.")"
            while :; do
                ask_line "$(t "panel port [Enter = ${PANEL_PORT}]:" "порт панели [Enter = ${PANEL_PORT}]:")"
                CAND="$PANEL_PORT"
                if [[ -n "$REPLY_VAL" ]]; then
                    if valid_port "$REPLY_VAL"; then
                        CAND=$(( 10#$REPLY_VAL ))
                    else
                        ask_warn "$(t "'${REPLY_VAL}' is not a port between 1 and 65535." \
                                      "'${REPLY_VAL}' — не порт в диапазоне от 1 до 65535.")"
                        continue
                    fi
                fi
                if port_busy tcp "$CAND"; then
                    HOLDER=$(port_holder tcp "$CAND")
                    ask_warn "$(t "${HOLDER:-something} already holds ${CAND}/tcp; the panel will not come up there" \
                                  "${HOLDER:-что-то} уже занимает ${CAND}/tcp; панель там не поднимется")"
                    (( ASK_TTY )) || break
                    continue
                fi
                PANEL_PORT="$CAND"
                break
            done
            # Asked and answered, so it is passed on rather than left to
            # install-panel.sh's own default - the two agree today, and this is
            # what stops them having to agree forever.
            PANEL_PORT_GIVEN=1
            CHOSEN+=("$(pad "$(t "panel port" "порт панели")" "$COL") ${PANEL_PORT}/tcp")
            ;;

        panelpath)
            ask_rule "${QN}/${#ASK_Q[@]}  $(t "Panel path" "Путь панели")"
            ask_body "$(t "A secret prefix on the panel's URL, so a port scan never reaches" \
                          "Секретный префикс в URL панели, чтобы сканирование портов не")" \
                     "$(t "the login page. Cover, not authentication." \
                          "доходило до страницы входа. Маскировка, а не аутентификация.")"
            while :; do
                ask_line "$(t "panel path [Enter = /awg/ + 20 random characters]:" \
                              "путь панели [Enter = /awg/ + 20 случайных символов]:")"
                [[ -n "$REPLY_VAL" ]] || break
                if CAND=$(norm_base_path "$REPLY_VAL"); then
                    PANEL_BASE_PATH="$CAND"
                    if [[ "$CAND" == "/" ]]; then
                        ask_warn "$(t "the panel will answer at the site root, with nothing keeping scanners off the login page" \
                                      "панель будет отвечать в корне сайта, и страницу входа не будет прикрывать ничто")"
                    fi
                    break
                fi
                ask_warn "$(t "letters, digits, dot, dash, underscore and slashes only - /awg/office/, say." \
                              "только латинские буквы, цифры, точка, дефис, подчёркивание и слеши — например /awg/office/.")"
            done
            CHOSEN+=("$(pad "$(t "panel path" "путь панели")" "$COL") $(t "${PANEL_BASE_PATH:-random, printed at the end}" "${PANEL_BASE_PATH:-случайный, будет показан в конце}")")
            ;;

        paneluser)
            ask_rule "${QN}/${#ASK_Q[@]}  $(t "Panel username" "Имя пользователя панели")"
            if (( PANEL_ADMIN_EXISTS )); then
                ask_body "$(t "The account you sign in with. A name typed here renames it," \
                              "Учётная запись, под которой вы входите. Имя, введённое здесь,")" \
                         "$(t "and it keeps its password." \
                              "переименует её, и пароль у неё останется прежний.")"
                USERDEF="$(t "${PANEL_ADMIN_CURRENT}, unchanged" "${PANEL_ADMIN_CURRENT}, без изменений")"
            else
                ask_body "$(t "The account you sign in with, printed at the end." \
                              "Учётная запись, под которой вы входите; будет показана в конце.")"
                USERDEF="$(t "10 random characters" "10 случайных символов")"
            fi
            while :; do
                ask_line "$(t "username [Enter = ${USERDEF}]:" "имя пользователя [Enter = ${USERDEF}]:")"
                [[ -n "$REPLY_VAL" ]] || break
                if [[ "$REPLY_VAL" =~ ^[A-Za-z0-9._-]{1,32}$ ]]; then
                    PANEL_ADMIN_USER="$REPLY_VAL"; break
                fi
                ask_warn "$(t "1 to 32 characters: letters, digits, dot, dash, underscore." \
                              "от 1 до 32 символов: латинские буквы, цифры, точка, дефис, подчёркивание.")"
            done
            if [[ -n "$PANEL_ADMIN_USER" ]];  then USERNOTE="$PANEL_ADMIN_USER"
            elif (( PANEL_ADMIN_EXISTS )); then USERNOTE="$(t "${PANEL_ADMIN_CURRENT}, unchanged" \
                                                              "${PANEL_ADMIN_CURRENT}, без изменений")"
            else                                USERNOTE="$(t "random, printed at the end" \
                                                              "случайное, будет показано в конце")"; fi
            CHOSEN+=("$(pad "$(t "username" "имя пользователя")" "$COL") ${USERNOTE}")
            ;;

        panelpass)
            ask_rule "${QN}/${#ASK_Q[@]}  $(t "Panel password" "Пароль панели")"
            if (( PANEL_ADMIN_EXISTS )); then
                ask_body "$(t "Your own: at least 8 characters, not echoed, asked twice." \
                              "Свой: не короче 8 символов, не отображается, спрашивается дважды.")"
                PASSDEF="$(t "unchanged" "без изменений")"
            else
                ask_body "$(t "Shown once at the end, then kept only as a hash." \
                              "Будет показан один раз в конце, дальше хранится только хеш.")" \
                         "$(t "Your own: at least 8 characters, not echoed, asked twice." \
                              "Свой: не короче 8 символов, не отображается, спрашивается дважды.")"
                PASSDEF="$(t "16 random characters" "16 случайных символов")"
            fi
            while :; do
                ask_line "$(t "password [Enter = ${PASSDEF}]:" "пароль [Enter = ${PASSDEF}]:")" secret
                [[ -n "$REPLY_VAL" ]] || break
                if (( ${#REPLY_VAL} < 8 )); then ask_warn "$(t "at least 8 characters." \
                                                               "не короче 8 символов.")"; continue; fi
                CAND="$REPLY_VAL"
                ask_line "$(t "repeat:" "повторите:")" secret
                if [[ "$REPLY_VAL" == "$CAND" ]]; then PANEL_ADMIN_PASS="$CAND"; break; fi
                ask_warn "$(t "those two did not match." "эти два не совпали.")"
            done
            CAND="" REPLY_VAL=""
            if [[ -n "$PANEL_ADMIN_PASS" ]]; then PASSNOTE="$(t "the one you typed" "тот, что вы ввели")"
            # Unchanged covers both ways of getting here: nothing typed at all,
            # and a rename typed at the question before. Renaming an account is
            # not resetting it, and the password it had goes with it.
            elif (( PANEL_ADMIN_EXISTS )); then PASSNOTE="$(t "unchanged" "без изменений")"
            else                                PASSNOTE="$(t "random, printed at the end" \
                                                              "случайный, будет показан в конце")"; fi
            CHOSEN+=("$(pad "$(t "password" "пароль")" "$COL") ${PASSNOTE}")
            ;;

        esac
    done

    # On stdout, unlike the questions: this is the record of what the install
    # is about to do, and it belongs in a log or a scrollback the same as every
    # other step's output does.
    printf '\n' >&3
    for CHOICE in "${CHOSEN[@]}"; do printf '  %s\n' "$CHOICE"; done
fi
if (( ASK_FD )); then exec 3>&-; fi

# ------------------------------------------------ 5b'. ports already held
# Every answer is final by this line, whichever way it arrived: a flag typed
# before any of this ran, a prompt just above, or a default nobody was asked
# about. That is why the check is here and not only beside the questions:
# --panel-port, --port, and every run with no terminal to ask into must also
# be checked, and an occupied port is refused flatly so the install does not
# proceed onto a socket it cannot bind.
#
# The tunnel's port is not checked on an upgrade. The port then comes from the
# interface's own config, the interface is up and holding it, and the only
# thing this could report is that the server is running - which it is.
if (( ! EXISTING )) && port_busy udp "$PORT"; then
    PORT_HOLDER=$(port_holder udp "$PORT")
    die "$(t "${PORT_HOLDER:-something} already holds ${PORT}/udp; choose another port or stop that service" \
             "${PORT_HOLDER:-что-то} уже занимает ${PORT}/udp; выберите другой порт или остановите эту службу")"
fi

# The panel's port, against the panel that is already installed rather than
# against nothing. An upgrade that leaves the port alone would otherwise be
# told its own gunicorn is in the way, and an operator who reads that and
# stops the panel to clear the port has been given a worse machine than the
# one they started with. A port that is genuinely moving is checked normally.
PANEL_PORT_LIVE=$(panel_env_get AWG_PANEL_PORT 2>/dev/null || true)
if [[ "$PANEL_PORT" != "$PANEL_PORT_LIVE" ]] && port_busy tcp "$PANEL_PORT"; then
    PANEL_PORT_HOLDER=$(port_holder tcp "$PANEL_PORT")
    die "$(t "${PANEL_PORT_HOLDER:-something} already holds ${PANEL_PORT}/tcp; choose another port or stop that service" \
             "${PANEL_PORT_HOLDER:-что-то} уже занимает ${PANEL_PORT}/tcp; выберите другой порт или остановите эту службу")"
fi

# ------------------------------------------------------ 5c. the IPv6 side
# A client config that routes 0.0.0.0/0 and nothing else does not turn IPv6
# off on the client - it leaves it alone. So a phone on a dual-stack network
# reaches every dual-stack destination over its own connection, with its own
# address, outside the tunnel, while the VPN reports itself as connected and
# the traffic counters show the IPv4 remainder as though it were everything.
# There is no error anywhere. That is the failure this section exists to stop.
#
# The rule is that the tunnel claims ::/0 in every configuration it issues,
# whether or not this server can carry it. What changes with the mode is only
# what happens to the traffic once it arrives.

step "$(t "Planning the IPv6 side" "Планирование конфигурации IPv6")"

SUBNET6_CIDR="" SUBNET6_ADDR="" IPV6_BLOCK="" IPV6_ACCEPT_RA_FIX="" IPV6_MIGRATED=0

# An upgrade keeps the network it already handed out: renumbering would
# invalidate every client config in one silent step. Only an explicit
# --subnet6 or --ipv6 moves it, and only on a tunnel that has no IPv6 yet - the
# preflight above clears them for one that has.
if (( EXISTING && ! SUBNET6_GIVEN )); then
    EXIST6="" EXIST6_MODE=""
    if [[ -f "$CONF_DIR/clients.env" ]]; then
        EXIST6=$(sed -n 's/^[[:space:]]*SUBNET6_CIDR="\{0,1\}\([^"]*\)"\{0,1\}.*/\1/p' \
                 "$CONF_DIR/clients.env" | tail -1)
        EXIST6_MODE=$(sed -n 's/^[[:space:]]*SUBNET6_MODE="\{0,1\}\([^"]*\)"\{0,1\}.*/\1/p' \
                      "$CONF_DIR/clients.env" | tail -1)
    fi
    # The interface's own Address wins over clients.env's copy of it. That file
    # is a mirror kept for the client side, and a mirror is the half that gets
    # hand-edited, deleted, or restored from a backup older than the tunnel;
    # the address the kernel is actually loaded with is in the config. Taking
    # the mirror's word for the prefix is how the panel comes to allocate out of
    # a network nothing is numbered on. awg-menu's status screen already reads
    # it this way round, for the same reason.
    if parse_cidr6 "$(iface_get Address)"; then
        EXIST6=$(subnet6_cidr)
    fi
    # The mode is not a value in that file, but the rules in it say which one is
    # running, so a clients.env that has lost the answer is asked the config
    # rather than guessed at. Only when it has lost it: a mode written there is
    # the operator's, and rules somebody edited by hand should not silently
    # redefine what the panel is told this server does.
    subnet6_mode_valid "${EXIST6_MODE:-}" || EXIST6_MODE=$(conf_ipv6_mode || true)
    if parse_cidr6 "${EXIST6:-}"; then
        SUBNET6="$EXIST6"
        subnet6_mode_valid "${EXIST6_MODE:-}" && IPV6_MODE="$EXIST6_MODE"
    fi
fi

if [[ "$IPV6_MODE" == "off" ]]; then
    SUBNET6_MODE=""
    warn "$(t "IPv6 disabled by --ipv6 off: clients will route only IPv4, so any client" \
              "IPv6 отключён параметром --ipv6 off: клиенты будут маршрутизировать только IPv4.")"
    warn "$(t "  with working IPv6 sends that traffic outside the tunnel, unencrypted" \
              "  Клиенты с рабочим IPv6 будут отправлять такой трафик мимо туннеля,")"
    warn "$(t "  and with its own address. Re-run with --ipv6 auto to stop that." \
              "  в открытом виде со своего реального адреса. Перезапустите с --ipv6 auto для защиты.")"
else
    # The prefix. An explicit --subnet6 wins; otherwise carve one from a routed
    # block, and failing that derive a stable unique-local /64.
    if [[ "$SUBNET6" != "auto" ]]; then
        parse_cidr6 "$SUBNET6" || die \
            "--subnet6 '${SUBNET6}' is not a usable /64 (global unicast or unique-local), e.g. fd00::/64"
        SUBNET6_CIDR=$(subnet6_cidr)
    else
        if IPV6_BLOCK=$(ipv6_routed_block "$WAN") && SUBNET6_CIDR=$(ipv6_carve "$IPV6_BLOCK" "$WAN"); then
            parse_cidr6 "$SUBNET6_CIDR" || die "internal: carved an unusable /64 from ${IPV6_BLOCK}"
        else
            IPV6_BLOCK=""
            SUBNET6_CIDR=$(subnet6_ula)
            parse_cidr6 "$SUBNET6_CIDR" || die "internal: derived an unusable unique-local /64"
        fi
        SUBNET6_CIDR=$(subnet6_cidr)
    fi

    # The mode. Native needs a prefix that is actually routed here, so it is
    # only chosen for a global prefix; anything unique-local is masqueraded if
    # this host has IPv6 upstream and rejected if it has not.
    if [[ "$IPV6_MODE" == "auto" ]]; then
        if ipv6_is_global "$SUBNET6_CIDR"; then
            SUBNET6_MODE=native
        elif ipv6_host_online "$WAN"; then
            SUBNET6_MODE=nat
        else
            SUBNET6_MODE=blackhole
        fi
    else
        subnet6_mode_valid "$IPV6_MODE" || die \
            "--ipv6 '${IPV6_MODE}' is not one of: native, nat, blackhole, auto, off"
        SUBNET6_MODE="$IPV6_MODE"
    fi

    SUBNET6_ADDR=$(subnet6_server_addr)

    # Whether turning forwarding on would cost this host its own default route,
    # and the interface to exempt if so. Only when the route really did come
    # from a Router Advertisement and the interface is really still listening
    # for them: setting accept_ra on a host that has them switched off would
    # start it taking routes nobody asked for, which is its own way to break a
    # machine that was working.
    if ipv6_default_from_ra; then
        if [[ "$(cat "/proc/sys/net/ipv6/conf/${WAN}/accept_ra" 2>/dev/null || echo 0)" == "1" ]]; then
            IPV6_ACCEPT_RA_FIX="$WAN"
        fi
    fi

    case "$SUBNET6_MODE" in
        native)
            echo "$(t "  native: ${SUBNET6_CIDR}${IPV6_BLOCK:+ (carved from ${IPV6_BLOCK})}, forwarded, no translation" \
                      "  native: ${SUBNET6_CIDR}${IPV6_BLOCK:+ (выделено из ${IPV6_BLOCK})}, маршрутизируется, без трансляции")"
            ipv6_host_online "$WAN" || warn "$(t "this host has no IPv6 default route yet; native mode will carry nothing until it does" \
                                                 "у этой машины ещё нет маршрута IPv6 по умолчанию; до тех пор режим native не понесёт ничего")"
            ;;
        nat)
            echo "$(t "  nat: ${SUBNET6_CIDR} masqueraded behind this host's own address" \
                      "  nat: ${SUBNET6_CIDR} маскируется (masquerade) за адресом этого сервера")"
            ;;
        blackhole)
            echo "$(t "  blackhole: ${SUBNET6_CIDR} claimed and rejected - this host has no IPv6 upstream" \
                      "  blackhole: ${SUBNET6_CIDR} перехватывается и отклоняется (у сервера нет аплинка IPv6)")"
            echo "$(t "  clients still route ::/0 into the tunnel, so nothing escapes it" \
                      "  клиенты по-прежнему направляют ::/0 в туннель, предотвращая утечки")"
            ;;
    esac
fi

# The ip6tables half of the config's hooks. Empty when IPv6 is off, so an
# install that wants nothing to do with it gets a config with no v6 in it at
# all rather than rules that quietly match nothing.
#
# No source filter on what comes out of the tunnel, matching the IPv4 rules
# above: WireGuard will not accept a packet from a peer whose source address is
# outside that peer's AllowedIPs, so the spoofing an -s rule would stop cannot
# reach these chains in the first place.
#
# Every line ends in "|| true", and that is not tidiness. awg-quick runs with
# `set -e` and arms `trap 'del_if; exit'` before it creates the interface,
# clearing it only after the last PostUp has run - so a hook that exits non-zero
# does not merely fail to apply, it deletes the interface and takes the whole
# tunnel down with it. One ip6tables target missing from a kernel is then the
# difference between "IPv6 does not work" and "nobody can connect at all", and
# it strands the admin with a server that was fine until they upgraded it.
#
# The same reasoning already sits above this in the config, on the enforce hook:
# "a PostUp that fails would stop the tunnel from coming up at all". These
# should have been written that way to begin with.
#
# PostDown needs it just as much: `-D` fails when the rule is not there, which
# is exactly the state after a hook that failed to add it, and a failing
# PostDown aborts `awg-quick down` and leaves the interface half torn down.
ipv6_write_hooks() {
    [[ -n "$SUBNET6_MODE" ]] || return 0
    printf '\n'
    case "$SUBNET6_MODE" in
        native|nat)
            if [[ "$SUBNET6_MODE" == nat ]]; then
                printf 'PostUp = ip6tables -t nat -A POSTROUTING -s %s -o %s -j MASQUERADE || true\n' \
                    "$SUBNET6_CIDR" "$WAN"
            fi
            printf 'PostUp = ip6tables -A FORWARD -i %%i -j ACCEPT || true\n'
            # IPv6 routers do not fragment, so path MTU discovery is not an
            # optimisation there - it is the only way a large packet ever gets
            # through. The error that carries it is ICMPv6 Packet Too Big, and
            # a rule set that leaves ICMPv6 to conntrack drops it for any flow
            # conntrack has forgotten. The failure that produces is a tunnel
            # where small requests work and large transfers hang forever, which
            # is about the worst thing to be asked to debug.
            printf 'PostUp = ip6tables -A FORWARD -o %%i -p ipv6-icmp -j ACCEPT || true\n'
            printf 'PostUp = ip6tables -A FORWARD -o %%i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT || true\n'
            if [[ "$SUBNET6_MODE" == nat ]]; then
                printf 'PostDown = ip6tables -t nat -D POSTROUTING -s %s -o %s -j MASQUERADE || true\n' \
                    "$SUBNET6_CIDR" "$WAN"
            fi
            printf 'PostDown = ip6tables -D FORWARD -i %%i -j ACCEPT || true\n'
            printf 'PostDown = ip6tables -D FORWARD -o %%i -p ipv6-icmp -j ACCEPT || true\n'
            printf 'PostDown = ip6tables -D FORWARD -o %%i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT || true\n'
            ;;
        blackhole)
            # Peers still reach each other over IPv6 inside the tunnel - it is
            # a private network and that much works without an upstream - and
            # everything aimed past it is refused.
            #
            # REJECT rather than DROP, and this is the whole reason the mode is
            # worth having rather than just leaving ::/0 out of AllowedIPs. A
            # silent drop makes a dual-stack client sit through a connection
            # timeout before it tries IPv4, on every connection it opens; an
            # ICMPv6 "administratively prohibited" arrives at once and it moves
            # on. Same privacy either way, and the difference between a VPN
            # that works and one everybody says is slow.
            printf 'PostUp = ip6tables -A FORWARD -i %%i -o %%i -j ACCEPT || true\n'
            printf 'PostUp = ip6tables -A FORWARD -i %%i -j REJECT --reject-with icmp6-adm-prohibited || true\n'
            printf 'PostDown = ip6tables -D FORWARD -i %%i -o %%i -j ACCEPT || true\n'
            printf 'PostDown = ip6tables -D FORWARD -i %%i -j REJECT --reject-with icmp6-adm-prohibited || true\n'
            ;;
    esac
}

# Set a key in clients.env, adding it if the file predates it.
#
# The line is replaced with sed's c rather than an s expression, because one of
# the comments this is called with is "# native | nat | blackhole". A pipe in
# the replacement half of s|...|...| closes the expression early; sed then read
# " nat | blackhole|" as flags and every upgrade of a config without a
# SUBNET6_MODE died on "unknown option to `s'" before it reached the panel. c
# takes its text literally, so no value or comment passed here can be syntax.
env_set_kv() {
    local file="$1" key="$2" val="$3" comment="${4:-}"
    if grep -q "^[[:space:]]*${key}=" "$file"; then
        sed -i "/^[[:space:]]*${key}=/c\\${key}=\"${val}\"${comment:+  ${comment}}" "$file"
    else
        printf '%s="%s"%s\n' "$key" "$val" "${comment:+  ${comment}}" >> "$file"
    fi
}

# Add the IPv6 half to a server that was configured before it had one.
#
# Only what lives in <iface>.conf and clients.env. The peers' own AllowedIPs
# and the config files already issued to clients belong to awg-client, which
# rewrites them from its own side.
#
# Every step is conditional on the thing not already being there, so re-running
# the installer is not how a config ends up with two sets of ip6tables hooks.
ipv6_migrate_conf() {
    local conf="$CONF_DIR/${IFACE}.conf" env="$CONF_DIR/clients.env"
    local tmp hooks touched=0
    [[ -n "$SUBNET6_MODE" && -f "$conf" ]] || return 0

    # The interface Address, through the same check the preflight makes when it
    # decides whether --subnet6 and --ipv6 can be acted on. One spelling of the
    # question, so that what an upgrade decided and what this writes cannot end
    # up being about different configurations.
    if ! conf_has_ipv6; then
        # Rewritten here rather than through a lib/conf.sh helper. That library
        # deliberately has no writer in it - the panel is the only thing that
        # edits these files once they exist, and a shared "set a key in
        # [Interface]" would be a second implementation for the two to disagree
        # over. The installer is the exception because it is what writes the
        # file in the first place, so the edit lives beside the here-doc that
        # produced the line, in the same spelling.
        # In the tunnel's own directory rather than $TMPDIR, which is what the
        # self-test's stripped config and the panel report already do. Two
        # reasons, and the second is the one that bites: this file is the
        # server config, private key included, and on the images where /tmp is
        # a tmpfs it is also a different filesystem - so the mv below is a copy
        # and an unlink rather than a rename, and a copy that dies halfway
        # leaves a truncated awg0.conf. Nothing on this path took a backup
        # first, unlike the fresh-install branch, so there would be nothing to
        # put back. Beside the config it is one filesystem and one rename.
        tmp=$(mktemp "$CONF_DIR/.mig-XXXXXX") || return 1
        if awk -v addr="${SUBNET_ADDR}, ${SUBNET6_ADDR}" '
                # Only the [Interface] copy: a peer has an AllowedIPs, not an
                # Address, but a hand-edited file is not something to assume
                # about, and the first [Peer] is where this stops mattering.
                /^\[Peer\]/ { done = 1 }
                !done && /^[[:space:]]*Address[[:space:]]*=/ && !seen {
                    print "Address = " addr; seen = 1; next
                }
                { print }
                END { exit !seen }
            ' "$conf" > "$tmp" && [[ -s "$tmp" ]]; then
            chmod 600 "$tmp" && mv "$tmp" "$conf"
        else
            rm -f "$tmp"
            die "$(t "could not add the IPv6 address to ${conf}" \
                     "не удалось добавить адрес IPv6 в ${conf}")"
        fi
        touched=1
    fi

    # The hooks, before the first [Peer]: appending them to the end of the file
    # would put PostUp lines inside the last peer's section, where awg-quick
    # will not run them and awg will not accept them.
    if ! grep -q 'ip6tables' "$conf"; then
        # $hooks stays in $TMPDIR: it holds the ip6tables lines, which are in
        # the config already and are not a secret, and it is read by awk rather
        # than moved anywhere. $tmp becomes awg0.conf, so it goes beside it -
        # see the migration above for why.
        hooks=$(mktemp) || return 1
        tmp=$(mktemp "$CONF_DIR/.mig-XXXXXX") || { rm -f "$hooks"; return 1; }
        ipv6_write_hooks > "$hooks"
        if awk -v hooks="$hooks" '
                /^\[Peer\]/ && !done {
                    while ((getline line < hooks) > 0) print line
                    close(hooks); done = 1
                }
                { print }
                END {
                    if (!done) {
                        while ((getline line < hooks) > 0) print line
                        close(hooks)
                    }
                }
            ' "$conf" > "$tmp" && [[ -s "$tmp" ]]; then
            chmod 600 "$tmp" && mv "$tmp" "$conf"
            touched=1
        else
            rm -f "$tmp"
            rm -f "$hooks"
            die "$(t "could not add the IPv6 firewall hooks to ${conf}" \
                     "не удалось добавить хуки фаервола IPv6 в ${conf}")"
        fi
        rm -f "$hooks"
    fi

    if [[ -f "$env" ]]; then
        env_set_kv "$env" SUBNET6_CIDR "$SUBNET6_CIDR" '# blank = this tunnel carries no IPv6'
        env_set_kv "$env" SUBNET6_MODE "$SUBNET6_MODE" '# native | nat | blackhole'
        # A full tunnel becomes a full tunnel. Anything else in there is a
        # split-tunnel route list somebody chose, and choosing it again for
        # them would be a worse surprise than leaving it: a client that routes
        # a named subnet is not the client that is leaking everything.
        #
        # "Full" is a question about coverage rather than about the text -
        # "0.0.0.0/1, 128.0.0.0/1" is the same tunnel as "0.0.0.0/0", written
        # the way a client that wants to override a default route without
        # replacing it writes it.
        local current
        current=$(sed -n 's/^[[:space:]]*CLIENT_ALLOWED_IPS="\{0,1\}\([^"]*\)"\{0,1\}.*/\1/p' "$env" | tail -1)
        if needs_ipv6 "$current"; then
            env_set_kv "$env" CLIENT_ALLOWED_IPS "$(with_ipv6 "$current")" \
                "# \"${SUBNET_CIDR}\" for split tunnel"
            touched=1
        fi
    fi

    if (( touched )); then
        IPV6_MIGRATED=1
        echo "$(t "  added IPv6 (${SUBNET6_MODE}, ${SUBNET6_CIDR}) to the existing configuration" \
                  "  добавлен IPv6 (${SUBNET6_MODE}, ${SUBNET6_CIDR}) в существующую конфигурацию")"
    fi
    return 0
}

# What a client config should carry, given the mode. Both are defaults for new
# clients; awg-client and the panel can still be told otherwise per client.
#
# ::/0 is in the list whatever the mode, because it is what makes the client
# route IPv6 into the tunnel at all - and a client that does not route it does
# not stop using IPv6, it uses it outside the tunnel. That is the leak, and the
# only configuration that closes it is one that claims the whole address space.
CLIENT_ALLOWED_DEFAULT="0.0.0.0/0"
CLIENT_DNS_DEFAULT="8.8.8.8, 8.8.4.4"
if [[ -n "$SUBNET6_MODE" ]]; then
    CLIENT_ALLOWED_DEFAULT="0.0.0.0/0, ::/0"
    # A resolver reached over IPv6 is only useful where IPv6 leaves the server.
    # Handing one out in blackhole mode would point every lookup at an address
    # this server rejects, and name resolution would depend on the client
    # failing over to the v4 entries quickly enough not to be noticed.
    case "$SUBNET6_MODE" in
        native|nat)
            CLIENT_DNS_DEFAULT="8.8.8.8, 8.8.4.4, 2001:4860:4860::8888, 2001:4860:4860::8844" ;;
    esac
fi

# --------------------------------------------- 6. obfuscation profile
# Only parameters the Amnezia client's .conf importer preserves:
#   required  Jc Jmin Jmax S1 S2 H1-H4      optional  S3 S4 I1-I5
# HeaderProtectionKey / ContentPaddingAddition / timer overrides are
# silently DROPPED on import, so setting them only breaks the handshake.
# Imitation packets use only <r N> and <b 0xHEX>; the kernel also accepts
# <t> <c> <rc> <rd> but the client rejects those with error code 1000.
#
# Every one of them is drawn here rather than written as a constant. A value
# this script ships the same to everybody is not obfuscation - it is a
# signature with an extra step, and one filter rule then matches every server
# that ever ran it.
umask 077
install -d -m 700 "$CONF_DIR" "$CONF_DIR/clients"

# The language, written down for everything that comes after this run: awg-menu
# opens in it, and the next run of this script offers it as the default rather
# than starting the question over in English. Here rather than beside the
# question itself, because the question is the first thing this script does and
# this is the first point at which there is a directory to put the answer in.
#
# Best effort. An install that got this far and cannot write one word into a
# directory it has just created has something much larger wrong with it, and
# stopping over the language of the menu would be reporting that in the least
# useful way available.
lang_save "$LANG_CHOICE" || warn "$(t "could not record the language in ${LANG_FILE}; awg-menu will open in English" \
                                      "не удалось записать язык в ${LANG_FILE}; awg-menu будет открываться на английском")"

if (( EXISTING )); then
    step "$(t "Keeping the existing configuration" "Сохранение существующей конфигурации")"
    echo "$(t "  obfuscation profile, ${IFACE}.conf and clients untouched" \
              "  параметры маскировки, ${IFACE}.conf и клиенты оставлены без изменений")"
    if [[ -f "$CONF_DIR/clients.env" ]]; then
        # The one thing an upgrade does touch: the mirror of the server's own
        # network, so a clients.env written before subnets could be anything
        # but a /24 stops claiming otherwise.
        env_set_subnet "$CONF_DIR/clients.env"
        if (( ENDPOINT_MOVED )); then
            sed -i "s|^ENDPOINT_HOST=.*|ENDPOINT_HOST=\"${ENDPOINT}\"|" "$CONF_DIR/clients.env"
        fi
    fi
    # The other thing an upgrade touches, and the reason this one is not
    # "config untouched": a server installed before IPv6 was carried is issuing
    # configs that route only IPv4, and every client of it with working IPv6 is
    # sending that traffic outside the tunnel. Leaving that alone to preserve
    # the letter of "an upgrade changes nothing" would be preserving a leak.
    ipv6_migrate_conf
else

step "$(t "Generating the obfuscation profile" "Генерация профиля маскировки (обфускации)")"

gen_obfuscation "$MTU"
echo "$(t "  junk ${JC}x${JMIN}-${JMAX}, padding ${S1}/${S2}/${S3}/${S4}, four header ranges" \
          "  мусорные пакеты ${JC}x${JMIN}-${JMAX}, дополнение ${S1}/${S2}/${S3}/${S4}, 4 диапазона заголовков")"
echo "$(t "  decoys: ${GEN_DESC}" "  имитация протокола: ${GEN_DESC}")"

# ------------------------------------------------- 7. server config
step "$(t "Writing the server configuration" "Запись конфигурации сервера")"
SPRIV=$(awg genkey)
[[ -f "$CONF_DIR/${IFACE}.conf" ]] && \
    cp -a "$CONF_DIR/${IFACE}.conf" "$CONF_DIR/${IFACE}.conf.bak-$(date -u +%Y%m%d%H%M%S)"

cat > "$CONF_DIR/${IFACE}.conf" <<EOF
[Interface]
Address = ${SUBNET_ADDR}${SUBNET6_ADDR:+, ${SUBNET6_ADDR}}
ListenPort = ${PORT}
PrivateKey = ${SPRIV}
MTU = ${MTU}

Jc = ${JC}
Jmin = ${JMIN}
Jmax = ${JMAX}
S1 = ${S1}
S2 = ${S2}
S3 = ${S3}
S4 = ${S4}
H1 = ${H1}
H2 = ${H2}
H3 = ${H3}
H4 = ${H4}
EOF

# The decoy session is three to five packets long, so the unused slots are
# left out entirely rather than written blank: awg-quick reads an empty value
# as a malformed imitation packet and refuses to bring the interface up.
for SLOT in I1 I2 I3 I4 I5; do
    if [[ -n "${!SLOT}" ]]; then
        printf '%s = %s\n' "$SLOT" "${!SLOT}" >> "$CONF_DIR/${IFACE}.conf"
    fi
done

cat >> "$CONF_DIR/${IFACE}.conf" <<EOF

PreDown = /usr/local/bin/awg-panel manage trafficsync || true

PostUp = /usr/local/bin/awg-panel manage enforce || true
PostUp = /usr/local/bin/awg-panel manage shape || true
PostUp = iptables -t nat -A POSTROUTING -s ${SUBNET_CIDR} -o ${WAN} -j MASQUERADE || true
PostUp = iptables -A FORWARD -i %i -j ACCEPT || true
PostUp = iptables -A FORWARD -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT || true
PostDown = iptables -t nat -D POSTROUTING -s ${SUBNET_CIDR} -o ${WAN} -j MASQUERADE || true
PostDown = iptables -D FORWARD -i %i -j ACCEPT || true
PostDown = iptables -D FORWARD -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT || true
PostDown = /usr/local/bin/awg-panel manage shape --detach || true
EOF
ipv6_write_hooks >> "$CONF_DIR/${IFACE}.conf"
chmod 600 "$CONF_DIR/${IFACE}.conf"

if [[ -f "$CONF_DIR/clients.env" ]]; then
    # Keep existing preferences (DNS, split-tunnel choice); refresh only
    # what this run actually changed.
    cp -a "$CONF_DIR/clients.env" "$CONF_DIR/clients.env.bak-$(date -u +%Y%m%d%H%M%S)"
    sed -i -e "s|^ENDPOINT_HOST=.*|ENDPOINT_HOST=\"${ENDPOINT}\"|" \
           -e "s|^CLIENT_MTU=.*|CLIENT_MTU=\"${MTU}\"|" "$CONF_DIR/clients.env"
    env_set_subnet "$CONF_DIR/clients.env"
    echo "$(t "  kept existing clients.env (backed up)" \
              "  сохранён существующий clients.env (создана резервная копия)")"
else
    cat > "$CONF_DIR/clients.env" <<EOF
# AmneziaWG client defaults. Shell syntax - every value must be QUOTED.
ENDPOINT_HOST="${ENDPOINT}"     # blank = auto-detect
ENDPOINT_PORT=""                # blank = ListenPort from the server config
CLIENT_DNS="${CLIENT_DNS_DEFAULT}"
CLIENT_MTU="${MTU}"
CLIENT_ALLOWED_IPS="${CLIENT_ALLOWED_DEFAULT}"  # "${SUBNET_CIDR}" for split tunnel
SUBNET_CIDR="${SUBNET_CIDR}"
SUBNET6_CIDR="${SUBNET6_CIDR}"   # blank = this tunnel carries no IPv6
SUBNET6_MODE="${SUBNET6_MODE}"   # native | nat | blackhole
KEEPALIVE="25"
EOF
fi
chmod 600 "$CONF_DIR/clients.env"
echo "$(t "  wrote ${CONF_DIR}/${IFACE}.conf" "  файл ${CONF_DIR}/${IFACE}.conf записан")"

fi # fresh-install configuration

{
    echo "net.ipv4.ip_forward = 1"
    if [[ -n "$SUBNET6_MODE" ]]; then
        # On in every mode, blackhole included. Netfilter's FORWARD chain is
        # only reached from inside the kernel's forwarding path, so with
        # forwarding off the REJECT rule never runs and the packet is discarded
        # without an ICMPv6 error - which is exactly the silent drop that mode
        # exists to avoid.
        echo "net.ipv6.conf.all.forwarding = 1"
        if [[ -n "$IPV6_ACCEPT_RA_FIX" ]]; then
            # See ipv6_default_from_ra() in lib/subnet6.sh: a forwarding host
            # stops honouring Router Advertisements, and this machine's default
            # route came from one. Without this it would keep working until the
            # advertisement's lifetime ran out and then quietly disappear, long
            # after the installer said it was done.
            echo "net.ipv6.conf.${IPV6_ACCEPT_RA_FIX}.accept_ra = 2"
        fi
    fi
} > /etc/sysctl.d/99-amneziawg.conf
sysctl -q --system
if [[ -n "$SUBNET6_MODE" ]]; then
    echo "$(t "  ip_forward and IPv6 forwarding enabled" \
              "  включены ip_forward и пересылка IPv6")"
    if [[ -n "$IPV6_ACCEPT_RA_FIX" ]]; then
        echo "$(t "  accept_ra=2 on ${IPV6_ACCEPT_RA_FIX}, so forwarding does not cost this host its own default route" \
                  "  accept_ra=2 на ${IPV6_ACCEPT_RA_FIX}, чтобы пересылка не стоила этой машине её собственного маршрута по умолчанию")"
    fi
    # Cheap, and the one failure worth catching immediately: if the route has
    # already gone, everything downstream would come up looking healthy and
    # carry nothing.
    if [[ "$SUBNET6_MODE" != "blackhole" ]] && ! ip -6 route show default | grep -q .; then
        warn "$(t "this host has no IPv6 default route after enabling forwarding; IPv6 will not leave the server" \
                  "после включения пересылки у машины нет маршрута IPv6 по умолчанию; IPv6 не выйдет за пределы сервера")"
    fi
else
    echo "$(t "  ip_forward enabled" "  включён ip_forward")"
fi

# ------------------------------------------------- 8. management tools
step "$(t "Installing the management tools" "Установка инструментов управления")"
# awg-update among them, and not as an optional extra: it is what awg-menu's
# Update entry and the panel's Update tab both drive, so a server without it can
# check for a release from neither and has to be upgraded by hand forever. The
# one server that would be in that state is one upgrading from a version that
# predates the tool, which is exactly the run installing it here.
for tool in awg-menu awg-uninstall awg-update; do
    [[ -f "$SCRIPT_DIR/bin/$tool" ]] || die "$(t "bin/$tool not found next to this script (incomplete checkout?)" \
                                                   "bin/$tool не найден рядом с этим скриптом (неполная копия репозитория?)")"
    install -m 755 "$SCRIPT_DIR/bin/$tool" "/usr/local/bin/$tool"
done
echo "$(t "  awg-menu, awg-uninstall and awg-update installed to /usr/local/bin" \
          "  awg-menu, awg-uninstall и awg-update установлены в /usr/local/bin")"

# awg-client is gone, and an upgrade has to take the installed copy with it.
# Leaving it would be worse than untidy: it still writes the server config and
# traffic.db directly, under a lock the panel no longer coordinates with the
# same way, so an admin reaching for the command they have used for a year
# would edit the tunnel behind the panel's back - and the panel's copy of the
# client list would disagree with the file until something happened to notice.
if [[ -e /usr/local/bin/awg-client ]]; then
    rm -f /usr/local/bin/awg-client
    echo "$(t "  removed the retired awg-client (clients are managed in the panel now)" \
              "  удалён устаревший awg-client (управление клиентами теперь осуществляется через веб-панель)")"
fi

# Keep the shared lib where the installed tools look for it, plus a full
# copy of this installer and bin/, so awg-menu's Update entry can re-run
# the whole thing later without a checkout on the box. Skipped when this
# IS that copy running - the files are already in place.
if [[ "$SCRIPT_DIR" != "$SHARE_DIR" ]]; then
    install -d -m 755 "$SHARE_DIR" "$SHARE_DIR/lib" "$SHARE_DIR/bin"
    install -m 644 "$SCRIPT_DIR"/lib/*.sh "$SHARE_DIR/lib/"
    install -m 755 "$SCRIPT_DIR/install.sh" "$SHARE_DIR/install.sh"
    rm -f "$SHARE_DIR/bin/awg-client"
    for tool in awg-menu awg-uninstall awg-panel awg-update; do
        if [[ -f "$SCRIPT_DIR/bin/$tool" ]]; then
            install -m 755 "$SCRIPT_DIR/bin/$tool" "$SHARE_DIR/bin/$tool"
        fi
    done
    # And the upstream sources, when this installer came with them. Two megabytes
    # to keep the stored copy able to build the module without github.com, which
    # is the whole reason a release carries them: the people running this are
    # disproportionately behind networks that cannot reach it, and an update that
    # needs it is an update they cannot run. Removed first, so a checkout with no
    # vendor/ does not leave the last bundle's sources sitting beside a newer
    # install.sh that pins different commits - which install.sh refuses to build
    # from, correctly, and would be a confusing way to be told.
    rm -rf "$SHARE_DIR/vendor"
    if [[ -d "$VENDOR_DIR" ]]; then
        cp -a "$VENDOR_DIR" "$SHARE_DIR/vendor"
        echo "$(t "  lib/, the upstream sources and a copy of the installer kept in ${SHARE_DIR} (awg-menu -> Update)" \
                  "  lib/, исходники и копия установщика лежат в ${SHARE_DIR} (awg-menu -> Обновление)")"
    else
        echo "$(t "  lib/ and a copy of the installer kept in ${SHARE_DIR} (awg-menu -> Update)" \
                  "  lib/ и копия установщика лежат в ${SHARE_DIR} (awg-menu -> Обновление)")"
    fi
fi

# The four hooks the tunnel carries so the kernel's own behaviour does not undo
# the panel's, added to a config that predates them and repaired in one that
# still calls the retired client CLI.
#
# The first PostUp re-revokes disabled peers: awg-quick up loads the config
# straight into the kernel, and a disabled peer is still in that file - that is
# what reserves its address - so without this every bring-up, boot included,
# re-admits everyone a quota, an expiry or an admin had switched off. The second
# re-applies every bandwidth ceiling, because a tc structure belongs to the
# network device and goes when the device does: the panel's database still knows
# what each client is entitled to and, after a boot, nothing in the kernel does.
# PreDown snapshots the kernel's per-peer counters before an orderly down
# discards them, which is all that keeps a client's usage history across a
# restart. PostDown takes the shaping off the WAN, which is the one part of the
# structure that does not leave with the tunnel - and while the tunnel is down it
# is a queue on the path of everything this machine sends, this panel and the
# operator's own SSH session included.
#
# All of them end in "|| true" because awg-quick runs under `set -e`: a hook that
# exits non-zero takes the whole operation with it, and a panel mid-upgrade must
# not be a tunnel that will not come down.
#
# A config naming awg-client is rewritten where the line stands rather than
# having the replacement appended beside it. Those commands no longer exist, so
# the old line would fail on every bring-up - swallowed by "|| true", but
# logging a failure forever on a healthy server, which is how a real one stops
# being read.
# True when a firewall hook in this config predates the guard below.
#
# It has to be asked, because the condition underneath it only asks after the
# panel's own four hooks: a server installed after those landed but before this
# has all four and would be skipped, and it is exactly the server carrying six
# unguarded iptables lines.
hooks_unguarded() {
    grep -E '^(PostUp|PostDown)[[:space:]]*=.*ip6?tables' "$1" 2>/dev/null \
        | grep -qv '||[[:space:]]*true[[:space:]]*$'
}

PREDOWN_HOOK='PreDown = /usr/local/bin/awg-panel manage trafficsync || true'
POSTUP_HOOK='PostUp = /usr/local/bin/awg-panel manage enforce || true'
SHAPE_HOOK='PostUp = /usr/local/bin/awg-panel manage shape || true'
UNSHAPE_HOOK='PostDown = /usr/local/bin/awg-panel manage shape --detach || true'

if ! grep -qxF "$POSTUP_HOOK" "$CONF_DIR/${IFACE}.conf" 2>/dev/null \
   || ! grep -qxF "$PREDOWN_HOOK" "$CONF_DIR/${IFACE}.conf" 2>/dev/null \
   || ! grep -qxF "$SHAPE_HOOK" "$CONF_DIR/${IFACE}.conf" 2>/dev/null \
   || ! grep -qxF "$UNSHAPE_HOOK" "$CONF_DIR/${IFACE}.conf" 2>/dev/null \
   || grep -q 'awg-client \(enforce\|traffic sync\)' "$CONF_DIR/${IFACE}.conf" 2>/dev/null \
   || hooks_unguarded "$CONF_DIR/${IFACE}.conf"; then
    awk -v predown="$PREDOWN_HOOK" -v postup="$POSTUP_HOOK" \
        -v shape="$SHAPE_HOOK" -v unshape="$UNSHAPE_HOOK" '
        # A firewall hook from before these carried a guard. awg-quick runs
        # under `set -e` and arms `trap del_if` before it creates the
        # interface, so an -A that fails does not merely fail to apply - it
        # deletes the interface and takes the tunnel down with it. A -D that
        # fails is the mirror of that: it aborts `awg-quick down` and leaves
        # the interface half torn down, which is the state a config gets into
        # the moment anything else flushes the chains.
        #
        # The IPv6 lines have ended in "|| true" since they were written and
        # the IPv4 lines beside them never did, so this repairs whichever half
        # a given config is missing. It rewrites the line rather than replacing
        # it and does not consume it: every rule below still sees the line and
        # still decides where the hooks the panel owns belong relative to it.
        /^(PostUp|PostDown)[[:space:]]*=.*ip6?tables/ && $0 !~ /\|\|[[:space:]]*true[[:space:]]*$/ {
            $0 = $0 " || true"
        }
        # An existing hook line pointing at the old CLI becomes the new one,
        # in place, so ordering relative to the firewall rules is preserved.
        /^PreDown[[:space:]]*=.*awg-client traffic sync/ { print predown; haspre=1; next }
        /^PostUp[[:space:]]*=.*awg-client enforce/       { print postup;  haspost=1; next }
        # A line that is already right is kept, and kept once: a config that
        # somehow carries two copies of one hook leaves with one.
        $0 == predown { if (!haspre)   { haspre=1;   print } next }
        $0 == postup  { if (!haspost)  { haspost=1;  print } next }
        $0 == shape   { if (!hasshape) { hasshape=1; print } next }
        $0 == unshape { if (!hasdown)  { hasdown=1;  print } next }
        # Otherwise the two hooks the panel owns go ahead of the firewall rules:
        # neither depends on them, and nothing is served by making a revocation
        # queue behind routing.
        /^PostUp[[:space:]]*=/ {
            if (!haspost)  { print postup; haspost=1 }
            if (!hasshape) { print shape;  hasshape=1 }
            print; next
        }
        # Whatever is still missing belongs in [Interface], so it has to land
        # before the first peer block rather than at the end of the file. The
        # teardown lands here rather than beside the PostUps so that it comes
        # after the firewall rules it must not run ahead of.
        /^\[Peer\]/ && !placed {
            placed = 1
            added = 0
            if (!haspost)  { print postup;  haspost=1;  added=1 }
            if (!hasshape) { print shape;   hasshape=1; added=1 }
            if (!hasdown)  { print unshape; hasdown=1;  added=1 }
            if (!haspre)   { print predown; haspre=1;   added=1 }
            # Only when something was actually put here. The installer re-runs on
            # every upgrade, and a blank line printed unconditionally would make
            # each run differ from the last by one - which is a config that grows
            # a line every upgrade and a test that can never assert idempotence.
            if (added) print ""
            print; next
        }
        { print }
        # Only reached by a config with no peers at all, where the end of the
        # file is still inside [Interface].
        END {
            if (!haspost)  print postup
            if (!hasshape) print shape
            if (!hasdown)  print unshape
            if (!haspre)   print predown
        }' "$CONF_DIR/${IFACE}.conf" > "$CONF_DIR/.hooks.tmp"
    chmod 600 "$CONF_DIR/.hooks.tmp"
    mv "$CONF_DIR/.hooks.tmp" "$CONF_DIR/${IFACE}.conf"
    echo "$(t "  tunnel hooks in ${IFACE}.conf point at the panel" \
              "  хуки в ${IFACE}.conf настроены на вызов веб-панели")"
fi

# --------------------------------------------------- 9. the web panel
# Before the tunnel comes up, which is a change from when this was optional
# and ran last. Two reasons, and both are about the hooks written above.
#
# The PostUp hook re-revokes disabled peers, and it fires on the very first
# bring-up below. On an upgrade that is not a formality: `awg-quick up` loads
# every peer in the file, disabled ones included, so a server whose panel is
# not yet installed spends the rest of the install with every revoked client
# back on the interface. Installing first closes that window instead of
# leaving it open and apologising for it afterwards.
#
# The other reason is simply that the panel is the only thing that can create
# a client now, and this installer creates one.
#
# The old ordering existed so that a panel that failed to build still left a
# working VPN. That trade is gone with the choice it was protecting: a tunnel
# nobody can add a client to is not a working VPN, so a panel that will not
# install is an install that failed.
step "$(t "Installing the web panel" "Установка веб-панели")"
# What the summary says about the account is no longer guessed from the state
# of the disk before this step: install-panel.sh reports what it actually did,
# and the summary reads that. Guessing was how every upgrade came to end with
# "credentials printed above - save them now" above a run that had printed no
# password at all.

# --port and --listen only when this run was actually given them. They are
# written to /etc/awg-panel.env, which is the panel's own source of truth for
# both and is edited from awg-menu and from the panel's settings page - so
# passing this file's defaults unconditionally would take a panel the operator
# had bound to 127.0.0.1 and put it back on every interface, and move one they
# had moved to another port back to 2097 while the firewall rule stayed on the
# old one. install-panel.sh distinguishes "asked for" from "defaulted" for
# exactly this reason; it can only do that if the caller does too.
#
# --conf-dir because AWG_CONF_DIR can move this whole installation, and a panel
# pointed at /etc/amnezia/amneziawg while the tunnel is somewhere else manages
# a config that does not exist.
#
# The last three are the interview's, and each one is passed only when somebody
# actually chose it: install-panel.sh draws a random path, username and
# password for whatever is left, and it is the only thing that draws any of
# them. The password goes through the environment because /proc/<pid>/cmdline
# is world-readable and an argv is visible to every local user for as long as
# the command runs.
#
# --lang unconditionally, unlike the two above it: install-panel.sh is a
# separate process, and the language is this run's own property rather than
# something the server records, so there is nothing on the far side for it to
# disagree with. The two halves of one install printing in two languages is
# what leaving it out would mean.
PANEL_ARGS=(--iface "$IFACE" --conf-dir "$CONF_DIR" --lang "$LANG_CHOICE")
(( PANEL_PORT_GIVEN ))   && PANEL_ARGS+=(--port "$PANEL_PORT")
(( PANEL_LISTEN_GIVEN )) && PANEL_ARGS+=(--listen "$PANEL_LISTEN")
[[ -n "$PANEL_BASE_PATH"  ]] && PANEL_ARGS+=(--base-path  "$PANEL_BASE_PATH")
[[ -n "$PANEL_ADMIN_USER" ]] && PANEL_ARGS+=(--admin-user "$PANEL_ADMIN_USER")
[[ -n "$PANEL_ADMIN_PASS" ]] && export AWG_PANEL_ADMIN_PASSWORD="$PANEL_ADMIN_PASS"

# An answer about an account that already exists is a request to change it, and
# seedadmin leaves an existing account alone unless it is told this. Only when
# something was actually typed: an upgrade where both questions were answered
# with Enter must reach the panel looking exactly like every upgrade before it,
# or the operator who wanted nothing changed gets a renamed account.
#
# The name goes with it either way. A password typed on its own still has to
# say which account it belongs to, and on this path that is the name the panel
# is already using.
if (( PANEL_ADMIN_EXISTS )) && [[ -n "$PANEL_ADMIN_USER$PANEL_ADMIN_PASS" ]]; then
    PANEL_ARGS+=(--admin-reset)
    [[ -n "$PANEL_ADMIN_USER" ]] || PANEL_ARGS+=(--admin-user "$PANEL_ADMIN_CURRENT")
fi

# Where install-panel.sh leaves the credentials it settled on, so the summary at
# the end of this script can print them. In the tunnel's own directory, which is
# 700 and root-owned, for the same reason the self-test's stripped config is:
# this file holds a password in the clear for the few minutes between the panel
# going in and the summary being printed, and /tmp is not a place to leave one.
PANEL_REPORT=$(mktemp "$CONF_DIR/.panel-report-XXXXXX")
export AWG_PANEL_REPORT="$PANEL_REPORT"

# The copy kept in /usr/local/share/awg-script has lib/, bin/ and this script,
# and no panel/ - which is ~40 MB of prebuilt UI and Python wheels, stored on
# every server for the sake of reinstalling the version already running. So the
# copy awg-menu's Update entry runs cannot do this step, and until this it died
# here, one step after unloading the kernel module and taking the tunnel down.
# The trap put the tunnel back, so the damage was a failed update rather than
# an offline server, but the entry had never once worked.
#
# Skipped rather than fixed by storing panel/, because updating the panel is
# not what that entry offers: it rebuilds the module and the tools against a
# kernel that has moved, and the panel it leaves alone is the same version it
# would have reinstalled. Anything with a panel/ beside it - a bundle, a
# checkout, a release tarball - still installs and upgrades it as before.
#
# Through bash rather than executed: the bundle unpacks into a temp directory,
# and /tmp is mounted noexec on plenty of hardened servers. It is how the
# bundle starts this script, for the same reason.
if [[ -f "$SCRIPT_DIR/panel/install-panel.sh" ]]; then
    bash "$SCRIPT_DIR/panel/install-panel.sh" "${PANEL_ARGS[@]}" \
        || { rm -f "$PANEL_REPORT"
             die "$(t "panel installation failed; the tunnel has not been started" \
                      "установка панели не удалась; туннель не запущен")"; }
elif /usr/local/bin/awg-panel version >/dev/null 2>&1; then
    echo "$(t "  no panel/ beside this installer - keeping the panel already installed" \
              "  директория panel/ не найдена рядом с установщиком — сохраняем установленную версию")"
    echo "$(t "  (module and tools only; re-run a bundle or checkout to upgrade the panel)" \
              "  (обновлены только модуль и утилиты; для обновления панели запустите установку из репозитория/бандла)")"
else
    die "$(t "panel/install-panel.sh is not next to this script, and there is no
     installed panel to keep. The panel is the only thing that can add, revoke
     or account for a client, so going on would leave a tunnel nobody can be
     let onto. Run this from a release bundle or a checkout, both of which
     carry panel/." \
             "panel/install-panel.sh не найден рядом с этим скриптом, и панели,
     которую можно было бы сохранить, тоже нет. Панель — единственное, что
     умеет добавить, отозвать и учесть клиента, так что дальше остался бы
     туннель, на который некого пустить. Запускайте из релизного бандла или из
     копии репозитория: и там, и там panel/ есть.")"
fi
unset AWG_PANEL_ADMIN_PASSWORD
PANEL_ADMIN_PASS=""

# Read back, then removed: what it holds only has to survive as far as the
# summary, and every second past that is a password sitting on a disk that has
# no reason to be holding one. Parsed line by line rather than sourced, because
# a file written by one script and read by another is a file that can be
# replaced by a third.
#
# The line was matched for a known key and then eval'd, which is sourcing one
# line at a time: the key was checked and everything after the "=" was still
# run. `PANEL_REPORT_PASS=$(...)` in that file executed. The directory is 0700
# and root-owned, so nothing short of root could put it there and this was
# never a way in - but a guard that only holds because of where the file sits
# is not the guard the comment above claims, and the comment is the one that
# has to be true.
#
# So the value is base64 now, written that way by install-panel.sh. The shape
# is checked before anything is decoded, the decode cannot fail open, and no
# part of the line reaches an interpreter. The two counts are read as text and
# then required to be digits, because `(( ))` is an interpreter too - an
# arithmetic context expands `$(...)` inside an array subscript.
PANEL_REPORT_USER="" PANEL_REPORT_PASS=""
PANEL_REPORT_CREATED=0 PANEL_REPORT_RESET=0
if [[ -s "$PANEL_REPORT" ]]; then
    while IFS= read -r LINE; do
        REPORT_KEY="${LINE%%=*}" REPORT_VAL="${LINE#*=}"
        case "$REPORT_KEY" in
            PANEL_REPORT_USER|PANEL_REPORT_PASS|PANEL_REPORT_CREATED|PANEL_REPORT_RESET) ;;
            *) continue ;;
        esac
        [[ "$LINE" == *=* && "$REPORT_VAL" =~ ^[A-Za-z0-9+/=]*$ ]] || continue
        REPORT_VAL=$(printf '%s' "$REPORT_VAL" | base64 -d 2>/dev/null) || continue
        case "$REPORT_KEY" in
            PANEL_REPORT_USER) PANEL_REPORT_USER="$REPORT_VAL" ;;
            PANEL_REPORT_PASS) PANEL_REPORT_PASS="$REPORT_VAL" ;;
            PANEL_REPORT_CREATED) [[ "$REPORT_VAL" =~ ^[0-9]+$ ]] && PANEL_REPORT_CREATED="$REPORT_VAL" ;;
            PANEL_REPORT_RESET)   [[ "$REPORT_VAL" =~ ^[0-9]+$ ]] && PANEL_REPORT_RESET="$REPORT_VAL" ;;
        esac
    done < "$PANEL_REPORT"
    unset REPORT_KEY REPORT_VAL
fi
rm -f "$PANEL_REPORT"
unset AWG_PANEL_REPORT

PANEL_URL=$(/usr/local/bin/awg-panel url 2>/dev/null || true)

# ------------------------------------------------------- 10. bring up
step "$(t "Starting the tunnel" "Запуск туннеля")"
fw_allow "$PORT" udp
[[ -n "$FW_HANDLED" ]] && echo "$(t "  ${FW_HANDLED}: allowed ${PORT}/udp" \
                                    "  ${FW_HANDLED}: разрешён порт ${PORT}/udp")"
systemctl enable "awg-quick@${IFACE}" >/dev/null 2>&1
# Enabled, and ordered after DKMS's own boot-time rebuild. There is one boot
# where the module is not on the disk when userspace starts - the first boot
# into a kernel DKMS could not get ahead of, where dkms.service compiles it
# while the machine comes up - and it takes minutes. Without this, awg-quick@,
# wanted by the same target and ordered against nothing, starts while that
# build is still running, fails on a device that cannot exist yet, and stays
# failed for the rest of the uptime: the module appears a minute later and
# nothing goes back to look. `After=` on a unit that is not queued for this
# boot costs nothing at all, so every other boot is unaffected.
DROPIN="/etc/systemd/system/awg-quick@${IFACE}.service.d"
mkdir -p "$DROPIN"
cat > "$DROPIN/10-dkms.conf" <<'EOF'
# Written by AWG-panel's install.sh. Removed by awg-uninstall.
[Unit]
After=dkms.service
EOF
systemctl daemon-reload >/dev/null 2>&1
awg-quick down "$IFACE" >/dev/null 2>&1 || true
awg-quick up "$IFACE" >/dev/null 2>&1 || { awg-quick up "$IFACE"
                                           die "$(t "tunnel failed to start" \
                                                    "туннель не запустился")"; }
echo "$(t "  ${IFACE} up on ${PORT}/udp, enabled at boot" \
          "  ${IFACE} запущен на ${PORT}/udp, включён автозапуск при загрузке")"

# ------------------------------------------- 10b. re-issue and first client
# Everything here writes client config files, which only the panel does now.
#
# The IPv6 half of this used to run several steps earlier, as soon as
# awg-client and lib/ were in place. It waits for the panel instead, which is
# the same reason it never ran inside ipv6_migrate_conf: it has to happen once
# something exists that can rewrite a client config.
if (( IPV6_MIGRATED )); then
    step "$(t "Re-issuing client configurations with IPv6" \
              "Перевыпуск конфигураций клиентов с поддержкой IPv6")"
    # Nothing a client holds changes until it re-imports, so this is where the
    # upgrade stops being invisible - and it is worth being loud about, because
    # until each device does, it is still sending its IPv6 around the tunnel.
    if /usr/local/bin/awg-panel manage resync >/dev/null; then
        warn "$(t "every client must re-import its config before its IPv6 goes through the tunnel" \
                  "каждый клиент должен повторно импортировать конфигурацию для работы IPv6 через туннель")"
    else
        warn "$(t "could not re-issue the client configs; run 'awg-panel manage resync' by hand" \
                  "не удалось обновить конфигурации клиентов; выполните 'awg-panel manage resync' вручную")"
    fi
fi

if (( ENDPOINT_MOVED )); then
    step "$(t "Re-issuing client configs for the new endpoint" \
              "Перевыпуск конфигураций клиентов под новый endpoint")"
    # Nothing else reaches the devices: the endpoint lives in each client's own
    # [Peer] block, so a moved server with unrebuilt configs is a fleet dialling
    # an address that no longer answers.
    /usr/local/bin/awg-panel manage resync >/dev/null \
        || warn "$(t "resync failed; run 'awg-panel manage resync' by hand before re-importing" \
                     "resync не удался; выполните 'awg-panel manage resync' вручную перед повторным импортом")"
    echo "$(t "  clients now point at ${ENDPOINT} - re-import them on every device" \
              "  клиенты теперь указывают на ${ENDPOINT} — импортируйте их заново на каждом устройстве")"
fi

step "$(t "Creating the first client" "Создание первого клиента")"
if (( EXISTING )) && grep -q '^\[Peer\]' "$CONF_DIR/${IFACE}.conf"; then
    # Counted once, not once per language: both halves of a t() call are
    # expanded before either is chosen, so a $( ) written into the message
    # runs whichever language wins. No "|| true" behind it, unlike PEERS
    # further up - grep -c exits 1 on a count of zero, and the branch this is
    # in was entered by a grep that already found a [Peer].
    KEPT=$(grep -c '^\[Peer\]' "$CONF_DIR/${IFACE}.conf")
    echo "$(t "  keeping the existing clients (${KEPT})" \
              "  оставляем существующих клиентов (${KEPT})")"
elif grep -q "Client = ${CLIENT}\$" "$CONF_DIR/${IFACE}.conf" 2>/dev/null; then
    echo "$(t "  ${CLIENT} already exists - left alone" "  ${CLIENT} уже есть — оставлен как есть")"
else
    /usr/local/bin/awg-panel manage addclient "$CLIENT" >/dev/null \
        || die "$(t "could not create client '$CLIENT'" "не удалось создать клиента '$CLIENT'")"
    echo "  ${CLIENT} -> ${CONF_DIR}/clients/${CLIENT}.conf"
fi

# ------------------------------------------------------ 11. self-test
# A real handshake against the running server, from a network namespace holding
# a second amneziawg interface configured as the client just created.
#
# In a function, and called from a conditional, because bash suspends errexit
# for the whole body of a function invoked that way - which is the only reason
# this is safe to run at all. Everything above has already finished by now: the
# module is installed, the tunnel is up, the panel is serving and the client
# exists. A check that cannot be set up here - an interface a killed run left
# behind, a kernel without namespaces, a box with no ping - is a check that did
# not run, and letting one abort the script threw away an installation that had
# succeeded, along with the summary below, which is where the endpoint, the
# panel's URL and the firewall warning are printed. Three of the commands were
# unguarded, so this was one leftover `sttest` interface away from happening.
#
# 0 the tunnel works, 1 it does not, anything else the test could not be run.
run_selftest() {
    local conf="$1" ns=awginstalltest stconf stip rc=0

    stip=$(sed -n 's|^Address = \([^/]*\).*|\1|p' "$conf" | head -1)
    [[ -n "$stip" ]] || return 2

    # A host with no ping cannot answer the question this asks, which is not the
    # same as answering it no. Step 2 installs one, so this is the box where
    # that package would not install, or where somebody has since removed it -
    # and without the check `ip netns exec` exited 127, the ping below read that
    # as a failure, and a perfectly healthy install ended on "self-test FAILED",
    # which is the one outcome that sends an operator hunting a fault that is
    # not there.
    command -v ping >/dev/null 2>&1 || return 2

    # In the tunnel's own directory, which is 700 and root-owned, rather than
    # at a fixed name in /tmp. It is a stripped copy of a client config, so it
    # carries that client's private key - and the fixed name meant a run
    # interrupted anywhere in the middle of this left the key sitting in a
    # world-readable directory for good.
    stconf=$(mktemp "$CONF_DIR/.selftest-XXXXXX") || return 2

    # Whatever an interrupted run left behind, before anything new is made. The
    # namespace was already cleared here; the interface was not, and it is
    # created in this namespace before being moved, so one abandoned `sttest`
    # made every later self-test on that machine fail to start.
    ip netns del "$ns" 2>/dev/null
    ip link del sttest 2>/dev/null

    if ! awg-quick strip "$conf" \
           | sed -e "s#^Endpoint = .*#Endpoint = 127.0.0.1:${PORT}#" \
                 -e "s#^AllowedIPs = .*#AllowedIPs = ${SUBNET_CIDR}#" > "$stconf" \
       || ! ip netns add "$ns" \
       || ! ip link add sttest type amneziawg \
       || ! ip link set sttest netns "$ns"; then
        rc=2
    else
        ip netns exec "$ns" awg setconf sttest "$stconf" || rc=1
        ip netns exec "$ns" ip addr add "${stip}/32" dev sttest 2>/dev/null
        ip netns exec "$ns" ip link set mtu "$MTU" up dev sttest || rc=1
        ip netns exec "$ns" ip route add "$SUBNET_CIDR" dev sttest 2>/dev/null
        ip netns exec "$ns" ping -c 2 -W 5 -q "${SUBNET_ADDR%%/*}" >/dev/null 2>&1 || rc=1
        ip netns exec "$ns" ping -c 1 -W 5 -q -M 'do' -s $((MTU-28)) "${SUBNET_ADDR%%/*}" \
            >/dev/null 2>&1 || rc=1
    fi

    ip netns del "$ns" 2>/dev/null
    ip link del sttest 2>/dev/null
    rm -f "$stconf"
    return "$rc"
}

if (( SELFTEST )); then
    step "$(t "Running the end-to-end self-test" "Запуск комплексной самодиагностики")"
    STCONF="$CONF_DIR/clients/${CLIENT}.conf"
    # find rather than a glob through ls: with pipefail an unmatched glob makes
    # the whole assignment fail, and under errexit that ended the installer on
    # the one case the next line exists to report.
    [[ -f "$STCONF" ]] || \
        STCONF=$(find "$CONF_DIR/clients" -maxdepth 1 -name '*.conf' 2>/dev/null | sort | head -1) || true
    if [[ -z "$STCONF" ]]; then
        warn "$(t "skipped - no client configs to test with" \
                  "пропущено — нет конфигураций клиентов для тестирования")"
    else
        ST_RC=0; run_selftest "$STCONF" || ST_RC=$?
        case "$ST_RC" in
            0) echo "$(t "  ${G}PASS${N} - handshake and full-MTU traffic both work" \
                         "  ${G}ПРОЙДЕН${N} — работают и рукопожатие, и трафик на полный MTU")" ;;
            # The menu entries are named in the language the menu will open
            # in, which is this one: install.sh writes its choice down and
            # awg-menu reads it back. They were English in both halves for as
            # long as awg-menu was English in both.
            1) warn "$(t "self-test FAILED - run 'sudo awg-menu' -> Diagnostics -> self-test" \
                         "самопроверка НЕ ПРОЙДЕНА — запустите 'sudo awg-menu' -> Диагностика -> Полная сквозная проверка туннеля")" ;;
            *) warn "$(t "self-test could not be set up on this host, so it did not run" \
                         "самопроверку не удалось развернуть на этой машине, так что она не выполнялась")" ;;
        esac
    fi
fi

# ---------------------------------------------------------- 11b. HTTPS
# Asked here rather than with the other questions, which run before anything
# has been built. This one needs the panel installed to point at a certificate,
# port 80 free to validate one, and the tunnel up so that a firewall rule added
# for http-01 is added to the firewall the rest of the install already
# configured. It is also the last thing that can still be answered before the
# summary prints the address the admin is about to sign in at, which is the
# moment the answer actually matters.
#
# In a conditional so errexit is suspended for the whole of it, the same reason
# the self-test above is. Everything worth having is already done; a certificate
# that cannot be issued is a panel on HTTP, which is where every install before
# this one finished, and it must not cost the summary.
PANEL_TLS_ON=0
[[ "$(panel_env_get AWG_PANEL_TLS)" == "1" ]] && PANEL_TLS_ON=1
# An upgrade of a panel that is already on HTTPS is asked nothing: the question
# has been answered, and asking it again would only invite somebody to replace a
# working certificate with a failed attempt at another one.
if [[ -n "$PANEL_URL" ]] && (( ASK )) && (( ! PANEL_TLS_ON )); then
    step "$(t "HTTPS for the panel" "Настройка HTTPS для панели")"
    if acme_offer "$ENDPOINT"; then
        PANEL_TLS_ON=1
        PANEL_URL=$(/usr/local/bin/awg-panel url 2>/dev/null || printf '%s' "$PANEL_URL")
        echo "$(t "  panel now served over HTTPS for ${ACME_CERT_NAME}" \
                  "  панель теперь доступна по HTTPS для ${ACME_CERT_NAME}")"
    fi
fi

# --------------------------------------------------------- 12. summary
MODE="$(t "fresh install" "чистая установка")"
(( EXISTING )) && MODE="$(t "upgrade - existing config and clients kept" \
                            "обновление — существующая конфигурация и клиенты сохранены")"
case "$SUBNET6_MODE" in
    native)    IPV6_NOTE="${SUBNET6_CIDR}   $(t "routed, no translation" \
                                                "маршрутизируется, без трансляции")" ;;
    nat)       IPV6_NOTE="${SUBNET6_CIDR}   $(t "masqueraded behind this host" \
                                                "маскируется за адресом этой машины")" ;;
    blackhole) IPV6_NOTE="${SUBNET6_CIDR}   $(t "claimed and rejected (no IPv6 upstream here)" \
                                                "занят и отклоняется (аплинка IPv6 здесь нет)")" ;;
    *)         IPV6_NOTE="${Y}$(t "off - clients with IPv6 will send it outside the tunnel" \
                                  "выключен — клиенты с IPv6 отправят его мимо туннеля")${N}" ;;
esac
# Said here as well as where it happened, because that scrolled past several
# steps ago and this is the part anyone actually reads.
MODNOTE="$(t "kernel-space (DKMS: survives kernel upgrades)" \
             "в пространстве ядра (DKMS: переживает обновления ядра)")"
(( KMOD_STALE )) && MODNOTE="$(t "kernel-space - ${Y}still running the previous module; reboot to use the new one${N}" \
                                 "в пространстве ядра — ${Y}работает ещё прежний модуль; перезагрузитесь, чтобы включить новый${N}")"
# Last, because it outranks the line above: a module that is merely a version
# behind still carries traffic, and a kernel with no module at all does not.
[[ -n "$KMOD_NEXT" ]] && MODNOTE="$(t "kernel-space - ${Y}nothing built for ${KMOD_NEXT}, the kernel this machine boots next; reboot into it and run install.sh again${N}" \
                                     "в пространстве ядра — ${Y}для ${KMOD_NEXT} ничего не собрано, а именно с этим ядром машина загрузится в следующий раз; перезагрузитесь в него и запустите install.sh заново${N}")"
# Both ports, because the panel's own firewall line went with the summary it
# used to print halfway through the install. Read from the panel's env file
# rather than from this script's answers: an upgrade that kept a panel already
# on another port has to name that one, and the panel is also what put the TCP
# rule in. Left out when it is bound to loopback, where nothing outside the
# box is meant to reach it in the first place.
PANEL_FW_PORT=$(panel_env_get AWG_PANEL_PORT)
[[ -n "$PANEL_FW_PORT" ]] || PANEL_FW_PORT="$PANEL_PORT"
PORTS="${B}UDP ${PORT}${N} $(t "(tunnel)" "(туннель)")"
case "$(panel_env_get AWG_PANEL_LISTEN)" in
    127.0.0.1|::1|localhost) ;;
    *) PORTS="${PORTS} $(t "and" "и") ${B}TCP ${PANEL_FW_PORT}${N} $(t "(panel)" "(панель)")" ;;
esac
if [[ -n "$FW_HANDLED" ]]; then
    FWNOTE="$(t "${FW_HANDLED} now allows ${PORTS}
on this host. A cloud security group (AWS etc.) still has to be opened
by hand." \
                "${FW_HANDLED} теперь пропускает ${PORTS}
на этой машине. Облачную security group (AWS и прочие) всё равно
придётся открыть руками.")"
else
    FWNOTE="$(t "allow inbound ${PORTS}.
On AWS that is the instance's security group - nothing reaches this host
until it is open." \
                "разрешите входящие ${PORTS}.
В AWS это security group инстанса — пока она закрыта, до этой машины
не доходит ничего.")"
fi
# Whether the thing this whole install exists to leave behind is actually
# answering. systemctl, not the install's own idea of how it went: every step
# above can have succeeded and the service still be dead, and an operator who
# is told the panel is running has stopped looking for the reason it is not.
PANEL_RUNNING=0
systemctl is-active --quiet awg-panel-web && PANEL_RUNNING=1
if (( PANEL_RUNNING )); then
    PANEL_STATE="$(t "${G}AWG panel successfully installed and running.${N}" \
                     "${G}Панель AWG установлена и работает.${N}")"
else
    PANEL_STATE="$(t "${Y}AWG panel installed, but awg-panel-web is not running.${N}
  See: sudo awg-panel logs" \
                     "${Y}Панель AWG установлена, но awg-panel-web не запущен.${N}
  Смотрите: sudo awg-panel logs")"
fi

# The width of the label column for everything below, the credentials block
# and the table under it alike, since the two are read as one thing. Written
# out rather than padded with spaces in the string, because the Russian words
# are longer than the English ones and hand-alignment only ever holds for the
# language it was typed in.
SCOL=$(t 12 18)

# The block underneath, which is the part that gets copied into a password
# manager. Everything in it is what the operator needs to sign in and nothing
# else, and it is printed here rather than where it was decided, several
# screens up, because this is the end of the install and the end is where
# people look.
#
# The username is only the one install-panel.sh reported when it actually put
# it on the account. When an existing account was kept, that name is a random
# one this run generated and threw away, and printing it would be handing over
# a login that does not exist.
if (( PANEL_REPORT_CREATED || PANEL_REPORT_RESET )); then
    PANEL_USER_SHOWN="$PANEL_REPORT_USER"
else
    PANEL_USER_SHOWN="$PANEL_ADMIN_CURRENT"
fi
if [[ -n "$PANEL_REPORT_PASS" ]]; then
    PANEL_PASS_SHOWN="$PANEL_REPORT_PASS"
elif (( PANEL_REPORT_RESET )); then
    PANEL_PASS_SHOWN="$(t "unchanged - the one this account already had" \
                          "без изменений — тот, что у этой учётной записи и был")"
else
    PANEL_PASS_SHOWN="$(t "unchanged - sudo awg-panel passwd resets it" \
                          "без изменений — сбрасывается через sudo awg-panel passwd")"
fi
[[ -n "$PANEL_USER_SHOWN" ]] || PANEL_USER_SHOWN="$(t "the account this panel already had" \
                                                      "та учётная запись, что у панели уже была")"

L_PANEL=$(pad "$(t "panel" "панель")" "$SCOL")
L_USER=$(pad "$(t "username" "имя пользователя")" "$SCOL")
L_PASS=$(pad "$(t "password" "пароль")" "$SCOL")
if [[ -n "$PANEL_URL" ]]; then
    PANEL_LINE="  ${L_PANEL}${B}${PANEL_URL}${N}
  ${L_USER}${B}${PANEL_USER_SHOWN}${N}
  ${L_PASS}${B}${PANEL_PASS_SHOWN}${N}"
else
    # The install would have died before here if the panel had not gone in, so
    # this is only reachable when awg-panel is installed but cannot say what
    # its URL is - a broken env file, most likely.
    PANEL_LINE="  ${L_PANEL}$(t "installed, but its URL could not be read" \
                                "установлена, но её URL не удалось прочитать")
  $(pad "" "$SCOL")$(t "check: sudo awg-panel status" "проверьте: sudo awg-panel status")
  ${L_USER}${B}${PANEL_USER_SHOWN}${N}
  ${L_PASS}${B}${PANEL_PASS_SHOWN}${N}"
fi
if (( PANEL_REPORT_CREATED || PANEL_REPORT_RESET )) && [[ -n "$PANEL_REPORT_PASS" ]]; then
    PANEL_LINE="${PANEL_LINE}

$(t "  ${Y}Copy those three now. The password is kept only as a hash, so this
  is the only time it is shown.${N}" \
    "  ${Y}Скопируйте эти три строки сейчас. Пароль хранится только в виде хеша,
  и это единственный раз, когда он показан.${N}")"
fi

# The last thing in the summary, because it is the one warning here that
# describes something already happening rather than something to configure.
# An admin signing in over HTTP has not been told anything until they are told
# what that costs, and "use HTTPS" on its own reads as advice about hygiene
# rather than as a description of who can read their password.
if (( PANEL_TLS_ON )) && [[ "$ACME_RESULT" == domain || "$ACME_RESULT" == ip ]]; then
    TLSNOTE="$(t "${G}HTTPS is on${N}, for ${B}${ACME_CERT_NAME}${N}. certbot renews it and the panel
picks the new one up on its own." \
                 "${G}HTTPS включён${N}, для ${B}${ACME_CERT_NAME}${N}. certbot продлевает сертификат,
а панель подхватывает новый сама.")"
elif (( PANEL_TLS_ON )) && [[ "$ACME_RESULT" == existing && "$ACME_ADOPT_HOW" == copy ]]; then
    # A certificate the service could not reach, so it was copied into
    # /etc/ssl/awg-panel/. That copy is a file and nothing more: the renewal
    # that refreshes the original leaves it alone, and the panel goes on
    # serving it until somebody makes it again. Said here because the failure
    # is a month or two away and silent when it comes.
    TLSNOTE="$(t "${G}HTTPS is on.${N} The certificate was copied to ${B}/etc/ssl/awg-panel/${N} so the
panel could read it. Whatever renews the original will not reach that copy -
copy it again after a renewal, or the panel serves an expired certificate.
awg-menu's HTTPS screen does it for you." \
                 "${G}HTTPS включён.${N} Сертификат скопирован в ${B}/etc/ssl/awg-panel/${N}, чтобы панель
могла его прочитать. То, что продлевает оригинал, до этой копии не дойдёт —
копируйте заново после продления, иначе панель будет отдавать просроченный
сертификат. Экран HTTPS в awg-menu делает это за вас.")"
elif (( PANEL_TLS_ON )); then
    # Somebody else's certificate: one that was already configured before this
    # run, or one found on the machine and reused. Whatever issued it owns
    # renewing it, and saying otherwise here would be a promise this install
    # did not make.
    TLSNOTE="$(t "${G}HTTPS is on.${N} The certificate came from somewhere other than this
install, so whatever issued it is still what has to renew it." \
                 "${G}HTTPS включён.${N} Сертификат пришёл не от этой установки, так что
продлевать его по-прежнему тому, что его выпустило.")"
else
    # The menu path is named in whichever language this install is being done
    # in, because that is the language awg-menu will open in: the choice is
    # written down here and read back there.
    TLSNOTE="$(t "${Y}The panel is on plain HTTP; upgrade to HTTPS as soon as possible. The
password you sign in with, the session after it and every client key you
download cross the network readable by anything on the path, and stay
compromised until they are rotated.${N}
  ${B}sudo awg-menu${N} -> Web panel -> Certificates     issue a certificate now" \
                 "${Y}Панель работает по обычному HTTP; переведите её на HTTPS как можно скорее.
Пароль, которым вы входите, сессия после него и каждый ключ клиента, который
вы скачиваете, идут по сети открыто для всего, что стоит на пути, и остаются
скомпрометированными, пока их не сменят.${N}
  ${B}sudo awg-menu${N} -> Веб-панель -> Сертификаты     выпустить сертификат")"
fi
# The banner is drawn to a fixed width rather than typed as a run of "=",
# because the Russian title is nine characters longer than the English one and
# a hand-typed rule would end somewhere else on the line for each.
BANNER=$(t "AWG Panel is ready" "Панель AWG готова")
BANPAD=$(( 54 - ${#BANNER} - 2 ))
BANL=$(printf '%*s' "$(( BANPAD / 2 ))" '');            BANL=${BANL// /=}
BANR=$(printf '%*s' "$(( BANPAD - BANPAD / 2 ))" '');   BANR=${BANR// /=}

cat <<EOF

${B}${BANL} ${BANNER} ${BANR}${N}

  ${PANEL_STATE}

${PANEL_LINE}

  $(pad "endpoint" "$SCOL")${B}${ENDPOINT}:${PORT}/udp${N}
  $(pad "$(t "tunnel" "туннель")" "$SCOL")${IFACE}   ${SUBNET_CIDR}   MTU ${MTU}   $(t "room for ${SUBNET_HOSTS} clients" "клиентов: ${SUBNET_HOSTS}")
  $(pad "ipv6" "$SCOL")${IPV6_NOTE}
  $(pad "$(t "mode" "режим")" "$SCOL")${MODE}
  $(pad "$(t "module" "модуль")" "$SCOL")${MODNOTE}

${Y}$(t "FIREWALL:" "ФАЕРВОЛ:")${N} ${FWNOTE}

${TLSNOTE}

EOF
