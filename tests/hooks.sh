#!/bin/bash
#
# tests/hooks.sh - the config migrations in install.sh, against real configs.
#
# awg0.conf carries two hooks that make the kernel's own behaviour stop undoing
# the panel's: a PreDown that snapshots the per-peer counters before an orderly
# down discards them, and a PostUp that re-revokes the disabled peers a bring-up
# would otherwise re-admit. They used to call bin/awg-client. That tool no longer
# does either job, so every server upgrading past this point has two lines in its
# config naming commands that are gone, and the upgrade has to repair them.
#
# The same awk also puts "|| true" back on a firewall hook that predates the
# guard. awg-quick runs under `set -e`, so an -A that fails deletes the
# interface it was bringing up and a -D that fails aborts the teardown; the
# IPv6 lines have carried the guard since they were written and the IPv4 lines
# beside them never did. Repairing those is the whole reason the gate in
# install.sh grew a condition that does not mention the panel's hooks at all,
# and that gate is extracted and exercised at the bottom of this file - a
# migration that is correct and never runs fixes nothing.
#
# The same upgrade adds the interface's IPv6 address to a server installed
# before it carried one, and that half is checked here too. It was written as a
# call to an iface_set helper in lib/conf.sh, which was then deleted on the
# grounds that nothing edits these files from bash any more - leaving the
# installer calling a function that does not exist, so every upgrade of an
# IPv4-only server died on "could not add the IPv6 address". Nothing
# noticed: shellcheck cannot tell a missing function from a command it
# has never heard of, and the hook migration below was the only part of
# this function under test.
#
# This is the one piece of the change that runs on machines nobody is watching,
# on a file that holds every client's key, so it is tested rather than reasoned
# about. Both awk programs are extracted from install.sh rather than copied
# here: a copy would pass this suite forever while the installer drifted away
# from it.
#
# Usage: tests/hooks.sh          (exit 0 = all checks passed)
#
set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
INSTALLER="$REPO/install.sh"

PREDOWN_HOOK='PreDown = /usr/local/bin/awg-panel manage trafficsync || true'
POSTUP_HOOK='PostUp = /usr/local/bin/awg-panel manage enforce || true'
SHAPE_HOOK='PostUp = /usr/local/bin/awg-panel manage shape || true'
UNSHAPE_HOOK='PostDown = /usr/local/bin/awg-panel manage shape --detach || true'

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok    %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"
        [[ -n "${2:-}" ]] && printf '        got: %s\n' "$2"; }
ck()  { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "$2 (wanted $3)"; fi; }

# ---------------------------------------------------------------- extraction
# Everything between the awk invocation and the line that closes it. Both ends
# are anchored on text that cannot plausibly appear twice in the installer.
PROGRAM=$(sed -n "/awk -v predown=/,/}' \"\$CONF_DIR/p" "$INSTALLER" \
    | sed -e "1,2d" -e "\$s/}' \".*$/}/")

[[ -n "$PROGRAM" && "$PROGRAM" == *"haspost"* && "$PROGRAM" == *"hasshape"* ]] || {
    echo "  FAIL  could not find the hook-migration awk in install.sh" >&2
    echo "        (the anchors in this test need updating)" >&2
    exit 1; }

migrate() {
    $AWK -v predown="$PREDOWN_HOOK" -v postup="$POSTUP_HOOK" \
         -v shape="$SHAPE_HOOK" -v unshape="$UNSHAPE_HOOK" "$PROGRAM"
}

# The IPv6 address half of the same function. Anchored on the awk that rewrites
# the Address line; the first and last lines of the range are the shell around
# it rather than the program.
ADDR_PROGRAM=$(sed -n "/if awk -v addr=/,/' \"\$conf\" > \"\$tmp\"/p" "$INSTALLER" \
    | sed -e '1d' -e '$d')

[[ -n "$ADDR_PROGRAM" && "$ADDR_PROGRAM" == *"seen"* ]] || {
    echo "  FAIL  could not find the Address-migration awk in install.sh" >&2
    echo "        (the anchors in this test need updating)" >&2
    exit 1; }

DUAL_ADDR='10.13.0.1/20, fd7a:1e5f:22::1/64'

migrate_addr() { $AWK -v addr="$DUAL_ADDR" "$ADDR_PROGRAM"; }

# ------------------------------------------------------------------ fixtures
# What bin/awg-client left behind on every server installed before this change.
LEGACY='[Interface]
Address = 10.13.0.1/20
ListenPort = 51820
PreDown = /usr/local/bin/awg-client traffic sync
PostUp = /usr/local/bin/awg-client enforce || true
PostUp = iptables -t nat -A POSTROUTING -s 10.13.0.0/20 -o eth0 -j MASQUERADE
PostDown = iptables -t nat -D POSTROUTING -s 10.13.0.0/20 -o eth0 -j MASQUERADE

[Peer]
# Client = phone
PublicKey = 1e6gtjXOkZIBabwDiZ9M10rGc6Z4D0whZvSvvx2TfWQ='

# Older still: a config from before either hook existed.
NOHOOKS='[Interface]
Address = 10.13.0.1/20
ListenPort = 51820
PostUp = iptables -t nat -A POSTROUTING -s 10.13.0.0/20 -o eth0 -j MASQUERADE

[Peer]
# Client = phone
PublicKey = 1e6gtjXOkZIBabwDiZ9M10rGc6Z4D0whZvSvvx2TfWQ='

# A server the panel-hook gate would wave straight through: all four hooks are
# already present and correct, so the only thing wrong with it is the six
# unguarded IPv4 lines. This is what every install between the panel hooks
# landing and the guard being fixed looks like. The IPv6 lines are guarded
# already, which is what makes it worth having both halves here.
GUARDLESS='[Interface]
Address = 10.13.0.1/20, fd7a:1e5f:22::1/64
ListenPort = 51820

PreDown = /usr/local/bin/awg-panel manage trafficsync || true

PostUp = /usr/local/bin/awg-panel manage enforce || true
PostUp = /usr/local/bin/awg-panel manage shape || true
PostUp = iptables -t nat -A POSTROUTING -s 10.13.0.0/20 -o eth0 -j MASQUERADE
PostUp = iptables -A FORWARD -i %i -j ACCEPT
PostUp = iptables -A FORWARD -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -s 10.13.0.0/20 -o eth0 -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -j ACCEPT
PostDown = iptables -D FORWARD -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
PostDown = /usr/local/bin/awg-panel manage shape --detach || true

PostUp = ip6tables -A FORWARD -i %i -j ACCEPT || true
PostDown = ip6tables -D FORWARD -i %i -j ACCEPT || true

[Peer]
# Client = phone
PublicKey = 1e6gtjXOkZIBabwDiZ9M10rGc6Z4D0whZvSvvx2TfWQ='

# A server with no firewall rules and no clients yet.
BARE='[Interface]
Address = 10.13.0.1/20
ListenPort = 51820'

# Peers but no PostUp at all - somebody who set their firewall up elsewhere. The
# case that has nowhere obvious to put a hook: past the first [Peer] there is no
# [Interface] left, so appending at the end of the file would bury the hooks
# inside a peer block, where awg-quick would refuse the whole config.
NOPOSTUP='[Interface]
Address = 10.13.0.1/20
ListenPort = 51820

[Peer]
# Client = phone
PublicKey = 1e6gtjXOkZIBabwDiZ9M10rGc6Z4D0whZvSvvx2TfWQ='

# For the address migration: a second Address line past the first [Peer]. Not
# something the panel or the installer writes, which is the point - it is what a
# hand-edited file can hold, and the peers are not this migration's to touch.
PEERADDR='[Interface]
Address = 10.13.0.1/20
ListenPort = 51820

[Peer]
# Client = phone
PublicKey = 1e6gtjXOkZIBabwDiZ9M10rGc6Z4D0whZvSvvx2TfWQ=
AllowedIPs = 10.13.0.2/32
Address = 10.99.0.1/24'

# And a config with no Address at all, which the migration must refuse rather
# than quietly pass through.
NOADDR='[Interface]
PrivateKey = QOfCyw+PsGvKmwLTLtCUZzRLTBGjLBaCSlqZOtjLYX4=
ListenPort = 51820'

first_of() {  # which of two patterns appears first: prints the winner
    printf '%s\n' "$1" | grep -oE "$2" | head -1
}

# --------------------------------------------------------------------- cases
# mawk is what these servers actually have; gawk is what a developer has. A
# migration that only works under one of them is a migration that works on the
# wrong machines.
RAN=0
for AWK in mawk gawk original-awk busybox; do
    [[ "$AWK" == busybox ]] && { command -v busybox >/dev/null 2>&1 || continue; AWK="busybox awk"; }
    command -v "${AWK%% *}" >/dev/null 2>&1 || continue
    RAN=$((RAN+1))
    printf '\n== %s ==\n' "$AWK"

    out=$(printf '%s\n' "$LEGACY" | migrate)
    ck "no line still names awg-client"        "$(grep -c 'awg-client' <<<"$out")" "0"
    ck "exactly one enforce hook"              "$(grep -c 'manage enforce' <<<"$out")" "1"
    ck "exactly one trafficsync hook"          "$(grep -c 'manage trafficsync' <<<"$out")" "1"
    ck "exactly one shaping hook"              "$(grep -c 'manage shape || true' <<<"$out")" "1"
    ck "exactly one shaping teardown"          "$(grep -c 'manage shape --detach' <<<"$out")" "1"
    ck "enforce still ahead of the firewall"   "$(first_of "$out" 'manage enforce|MASQUERADE')" "manage enforce"
    ck "shaping ahead of the firewall too"     "$(first_of "$out" 'manage shape \|\||MASQUERADE')" "manage shape ||"
    ck "the teardown after the firewall"       "$(first_of "$out" 'D POSTROUTING|manage shape --detach')" "D POSTROUTING"
    ck "PreDown still inside [Interface]"      "$(first_of "$out" 'manage trafficsync|\[Peer\]')" "manage trafficsync"
    ck "the teardown inside [Interface] too"   "$(first_of "$out" 'manage shape --detach|\[Peer\]')" "manage shape --detach"
    ck "the peer survived untouched"           "$(grep -c '^PublicKey = 1e6gtj' <<<"$out")" "1"
    ck "the admin's own rules survived"        "$(grep -c 'MASQUERADE' <<<"$out")" "2"

    out=$(printf '%s\n' "$NOHOOKS" | migrate)
    ck "hookless config gains all four"        "$(grep -cE 'manage (enforce|trafficsync|shape)' <<<"$out")" "4"
    ck "hookless: enforce before the firewall" "$(first_of "$out" 'manage enforce|MASQUERADE')" "manage enforce"
    ck "hookless: shaping before it too"       "$(first_of "$out" 'manage shape \|\||MASQUERADE')" "manage shape ||"
    ck "hookless: PreDown before [Peer]"       "$(first_of "$out" 'manage trafficsync|\[Peer\]')" "manage trafficsync"
    ck "hookless: teardown before [Peer]"      "$(first_of "$out" 'manage shape --detach|\[Peer\]')" "manage shape --detach"

    out=$(printf '%s\n' "$BARE" | migrate)
    ck "bare config gains all four"            "$(grep -cE 'manage (enforce|trafficsync|shape)' <<<"$out")" "4"

    # A config with peers and no PostUp at all: everything has to land before the
    # first peer block, because past it there is no [Interface] left to land in.
    out=$(printf '%s\n' "$NOPOSTUP" | migrate)
    ck "no-PostUp config gains all four"       "$(grep -cE 'manage (enforce|trafficsync|shape)' <<<"$out")" "4"
    ck "and every one of them before [Peer]"   "$(first_of "$out" 'manage enforce|\[Peer\]')" "manage enforce"

    # The installer re-runs on every upgrade, so a second pass must be a no-op.
    once=$(printf '%s\n' "$LEGACY" | migrate)
    twice=$(printf '%s\n' "$once" | migrate)
    thrice=$(printf '%s\n' "$twice" | migrate)
    ck "a second pass changes nothing"         "$twice"  "$once"
    ck "and a third is still stable"           "$thrice" "$once"

    # An already-migrated config must not collect a second copy of any hook.
    ck "no duplicate hooks after re-running"   "$(grep -cE 'manage (enforce|trafficsync|shape)' <<<"$thrice")" "4"

    # A config that already had half of them - which is every server upgrading
    # from the version that carried the first two - gains the rest and keeps what
    # it had. Only the lines are checked, not where the blank ones fall: the
    # hooks are read by key rather than by position, and the panel rewrites the
    # section into one order on its next write anyway.
    half=$(printf '%s\n' "$NOHOOKS" | migrate | grep -v 'manage shape')
    out=$(printf '%s\n' "$half" | migrate)
    ck "a half-migrated config gains the rest" "$(grep -cE 'manage (enforce|trafficsync|shape)' <<<"$out")" "4"
    ck "half-migrated: all still in [Interface]" \
       "$(first_of "$out" 'manage shape --detach|\[Peer\]')" "manage shape --detach"

    # -------------------------------------------- the firewall hook guard
    out=$(printf '%s\n' "$GUARDLESS" | migrate)
    ck "every firewall hook ends in || true" \
       "$(grep -cE '^(PostUp|PostDown)[[:space:]]*=.*ip6?tables' <<<"$out")" \
       "$(grep -cE '^(PostUp|PostDown)[[:space:]]*=.*ip6?tables.*\|\| true$' <<<"$out")"
    ck "all eight firewall hooks are still there" \
       "$(grep -cE '^(PostUp|PostDown)[[:space:]]*=.*ip6?tables' <<<"$out")" "8"
    # The guard is appended, not substituted for anything: a rule that lost its
    # -o eth0 on the way through would still end in || true and still pass the
    # check above, while sending every client's traffic out of the wrong link.
    ck "the rules themselves are untouched" \
       "$(grep -c 'A POSTROUTING -s 10.13.0.0/20 -o eth0 -j MASQUERADE || true' <<<"$out")" "1"
    ck "and their teardown too" \
       "$(grep -c 'D POSTROUTING -s 10.13.0.0/20 -o eth0 -j MASQUERADE || true' <<<"$out")" "1"
    # An already-guarded line must not collect a second guard, or every upgrade
    # would lengthen it by four characters for ever.
    ck "no line gains a second guard" "$(grep -c 'true || true' <<<"$out")" "0"
    ck "the guard pass is stable"     "$(printf '%s\n' "$out" | migrate)" "$out"
    ck "the panel's own hooks are untouched" \
       "$(grep -cE 'manage (enforce|trafficsync|shape)' <<<"$out")" "4"

    # ------------------------------------------------ the IPv6 address half
    out=$(printf '%s\n' "$LEGACY" | migrate_addr)
    ck "the interface gains its IPv6 address"   "$(grep -c "^Address = $DUAL_ADDR\$" <<<"$out")" "1"
    ck "and carries exactly one Address line"   "$(grep -c '^Address' <<<"$out")" "1"
    ck "the peer's key survives the rewrite"    "$(grep -c '^PublicKey = 1e6gtj' <<<"$out")" "1"
    ck "nothing else in [Interface] moved"      "$(grep -c '^ListenPort = 51820$' <<<"$out")" "1"

    # A peer block ends the search. A hand-edited file with something that looks
    # like an Address down among the peers must not be the line that gets the
    # prefix - and must not stop the real one from getting it either.
    out=$(printf '%s\n' "$PEERADDR" | migrate_addr)
    ck "only the [Interface] copy is rewritten" "$(grep -c "^Address = $DUAL_ADDR\$" <<<"$out")" "1"
    ck "the stray line past [Peer] is kept"     "$(grep -c '^Address = 10.99.0.1/24$' <<<"$out")" "1"
    ck "the peer's own routes are left alone"   "$(grep -c '^AllowedIPs = 10.13.0.2/32$' <<<"$out")" "1"

    # No Address at all: the installer must die rather than write a config the
    # interface cannot come up on, so the program has to fail rather than pass
    # the file through unchanged.
    printf '%s\n' "$NOADDR" | migrate_addr >/dev/null 2>&1
    ck "a config with no Address line fails"    "$?" "1"

    # The guard in install.sh means this normally runs once, but a rewrite that
    # is not stable under a second pass is one that cannot be re-run by hand.
    once=$(printf '%s\n' "$LEGACY" | migrate_addr)
    ck "a second address pass changes nothing"  "$(printf '%s\n' "$once" | migrate_addr)" "$once"
done

# ------------------------------------------------------------------ the gate
#
# The awk above only runs when the condition in install.sh says the config
# needs it, and that condition asked about the panel's four hooks and nothing
# else. A server installed after those landed has all four, so it was waved
# through - and it is precisely the server carrying six unguarded IPv4 lines.
# The function that closes that is lifted out of the installer rather than
# restated here, for the same reason the awk programs are.
printf '\n== the gate that decides whether it runs ==\n'
GATE=$(sed -n '/^hooks_unguarded() {/,/^}/p' "$INSTALLER")
if [[ -z "$GATE" ]]; then
    echo "  FAIL  could not find hooks_unguarded in install.sh" >&2
    exit 1
fi
eval "$GATE"

gate_says() {  # fixture -> yes/no
    local f; f=$(mktemp)
    printf '%s\n' "$1" > "$f"
    if hooks_unguarded "$f"; then printf 'yes'; else printf 'no'; fi
    rm -f "$f"
}
ck "a config with unguarded IPv4 hooks is picked up" "$(gate_says "$GUARDLESS")" "yes"
ck "a config already repaired is left alone" \
   "$(gate_says "$(printf '%s\n' "$GUARDLESS" | AWK=${AWK:-awk} migrate)")" "no"
ck "the legacy config is picked up too" "$(gate_says "$LEGACY")" "yes"
ck "a config with no firewall hooks at all is left alone" "$(gate_says "$BARE")" "no"

(( RAN )) || { echo "  FAIL  no awk implementation found to test with"; FAIL=$((FAIL+1)); }

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
