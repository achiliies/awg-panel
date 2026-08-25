#!/bin/bash
#
# tests/basepath.sh - the secret base path, wherever it is chosen.
#
# Two places choose it without somebody watching: panel/install-panel.sh on a
# fresh install, and panel_base_path_new in lib/panel.sh, which is what the
# menu's "generate a new random path" rotates onto after a URL has leaked. It
# is the whole of what keeps the login page out of an internet-wide scan. A
# generator that quietly returned a short string, or the same string twice,
# would still produce a panel that comes up and works, so nothing downstream
# would ever complain.
#
# The menu used to call rand_hex, which lives in lib/obfs.sh and which the menu
# does not source, so the substitution expanded to nothing and rotating a
# leaked path wrote /awg// - a working panel with no secret in it, reported as
# a success. That is why the boundary check below is here as well as the
# arithmetic: the generator being right is no use if the caller cannot reach
# it.
#
# Both copies are extracted from the files they live in rather than copied
# here, with one expression, so a copy cannot keep passing forever while the
# other drifts away from it.
#
# Usage: tests/basepath.sh          (exit 0 = all checks passed)
#
set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
INSTALLER="$REPO/panel/install-panel.sh"
LIB="$REPO/lib/panel.sh"
MENU="$REPO/bin/awg-menu"

PASS=0 FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }
is()  { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "got '$2', wanted '$3'"; fi; }

# ---------------------------------------------------------------- extraction
# Any assignment that computes something, which is the one in the fresh-install
# branch; the upgrade branch reads the existing value back with no quotes of
# its own. Deliberately not matched on the shape it produces - a generator that
# changed shape has to reach the checks below and fail there, saying what it
# now produces, rather than disappear from the extraction and be reported as a
# test that could not find its subject.
extract() { sed -n 's/^ *BASE_PATH="\(.*\$(.*\)"$/\1/p' "$1"; }

GENERATOR=$(extract "$INSTALLER")
LIB_GENERATOR=$(extract "$LIB")
for pair in "install-panel.sh:$GENERATOR" "lib/panel.sh:$LIB_GENERATOR"; do
    if [[ -z "${pair#*:}" ]]; then
        echo "  FAIL  could not find the base-path generator in ${pair%%:*}" >&2
        exit 1
    fi
done
printf '== generator ==\n  %s\n\n' "$GENERATOR"

# install-panel.sh runs on a box with no checkout on it and cannot source
# lib/panel.sh, so there are two copies and there is no arrangement in which
# there are not. What can be had is that they are the same text, checked here
# rather than hoped for - the shape assertions below are written once and only
# mean anything for both while this holds.
printf '== the two copies ==\n'
is "install-panel.sh and lib/panel.sh generate the same way" \
   "$LIB_GENERATOR" "$GENERATOR"

# Under the installer's own options, because a pipeline that dies of SIGPIPE
# passes on its own and aborts the install.
generate() { bash -euo pipefail -c "printf '%s' \"$GENERATOR\""; }

# ------------------------------------------------------------------- shape
printf '== shape ==\n'
FIRST=$(generate)
is "the generator runs clean under set -euo pipefail" "$?" "0"
if [[ "$FIRST" =~ ^/awg/[a-z0-9]{20}/$ ]]; then
    ok "/awg/ and then 20 characters from [a-z0-9], between slashes"
else
    bad "/awg/ and then 20 characters from [a-z0-9], between slashes" "got '$FIRST'"
fi
# The prefix is fixed on purpose, so the URL reads as this panel's. It buys
# nothing against a scanner, which is exactly why none of the entropy may be
# spent on it: the segment behind it has to be the full 20 either way.
case "$FIRST" in
    /awg/*) ok  "the fixed part is /awg/, ahead of the random segment" ;;
    *)      bad "the fixed part is /awg/, ahead of the random segment" "got '$FIRST'" ;;
esac

# ------------------------------------------------------------- every draw
#
# 200 draws, because the failure this guards against is not a generator that
# never works - that would be caught by hand on the first install - but one
# that comes up short occasionally, on the run nobody sees.
printf '\n== 200 draws ==\n'
SHORT=0 SEEN=""
for _ in $(seq 1 200); do
    path=$(generate)
    if [[ ! "$path" =~ ^/awg/[a-z0-9]{20}/$ ]]; then
        SHORT=$((SHORT+1))
        # A broken generator is broken on every draw, and 200 identical
        # complaints bury the counts underneath them.
        (( SHORT <= 3 )) && printf '     bad draw: %s\n' "$path"
    fi
    SEEN+="$path"$'\n'
done
is "every draw has the full 20 characters" "$SHORT" "0"
is "no two draws are the same" "$(printf '%s' "$SEEN" | sort -u | wc -l)" "200"

# Every character of the alphabet should turn up somewhere in 4000 draws of it,
# and none outside it. A generator that lost the digits, or that leaked a byte
# tr was meant to drop, still looks random until it is counted. The /awg/ is
# cut off first, or its own three letters would stand in for a generator that
# had stopped producing them.
ALPHABET=$(printf '%s' "${SEEN//\/awg\//}" | tr -d '/\n' | fold -w1 | sort -u | tr -d '\n')
is "the alphabet is exactly [a-z0-9]" "$ALPHABET" "0123456789abcdefghijklmnopqrstuvwxyz"

# ------------------------------------------------------- the caller's reach
#
# The menu is sourced the way the menu sources itself - its own `. "$AWG_LIB"`
# lines, in order - and then asked what it can call. Nothing here reads the
# generator's output; the question is only whether the name resolves at all,
# which is the half that was broken and the half no amount of testing the
# generator in isolation would have found.
printf '\n== what bin/awg-menu can call ==\n'
LIBS=$(sed -n 's/^\. "\$AWG_LIB\/\(.*\)"$/\1/p' "$MENU")
[[ -n "$LIBS" ]] || bad "the menu's library list could be read" "no . \"\$AWG_LIB/...\" lines matched"
have() {
    AWG_LIB_DIR="$REPO/lib" bash -c '
        for f in $2; do . "'"$REPO"'/lib/$f" 2>/dev/null || exit 3; done
        declare -F "$1" >/dev/null' _ "$1" "$LIBS"
}
if have panel_base_path_new; then
    ok "panel_base_path_new is defined by a library the menu sources"
else
    bad "panel_base_path_new is defined by a library the menu sources" \
        "the rotation would expand to nothing again"
fi
# The other half of the same boundary, stated so that moving the generator
# into lib/obfs.sh to tidy it up fails here instead of at 3am on a server.
if have rand_hex; then
    bad "rand_hex is still out of the menu's reach" \
        "lib/obfs.sh is being sourced now; the comment at bin/awg-menu says it is not"
else
    ok "rand_hex is still out of the menu's reach"
fi
# And that the rotation calls the one it can reach.
if grep -q 'panel_env_set AWG_PANEL_BASE_PATH "\$newpath"' "$MENU"; then
    ok "the rotation writes what it generated"
else
    bad "the rotation writes what it generated" "the call site in bin/awg-menu has moved"
fi


# ------------------------------------------------------------- what is given
#
# The generated path is only half of it: --base-path takes one from an operator
# instead. install.sh normalises what it asks for before handing it over, so
# the installed panel never saw anything else - but install-panel.sh on its own
# is a documented way to install, and on that path the value went unread into
# /etc/awg-panel.env, which is sourced, and into Django's URL configuration.
#
# Run as the flag parser sees it, in a subshell that stops before the script
# does anything: what is checked is which values it will take at all.
printf '\n== --base-path is normalised where it arrives ==\n'
INST_NORM=$(sed -n '/^norm_base_path()/,/^}/p' "$INSTALLER")
[[ -n "$INST_NORM" ]] || bad "install-panel.sh normalises --base-path" "no norm_base_path in it"
inst_norm() ( eval "$INST_NORM"; norm_base_path "$1" )

for pair in \
    'awg:/awg/' \
    '/awg:/awg/' \
    'awg/:/awg/' \
    '/awg/k2p9x4mt7wq1bz8n5rv3/:/awg/k2p9x4mt7wq1bz8n5rv3/' \
    '//awg///x//:/awg/x/' \
    '/:/' \
    'a.b_c~d-e:/a.b_c~d-e/' \
    '/awg/a b/:/awg/ab/' \
    '/awg/../etc/:/awg/../etc/'
do
    is "takes ${pair%%:*}" "$(inst_norm "${pair%%:*}")" "${pair#*:}"
done

# Two of the accepted values above are worth saying out loud, because neither
# is obvious and both are what install.sh has always done. Whitespace is
# deleted rather than refused: the answer comes off a terminal and a stray
# space in it is a slip, not a different path. And ".." is a legal segment,
# here and in the panel's own _BASE_PATH_SEGMENT - it is a prefix on a URL, not
# a filesystem path, and the two have to agree about what they will take.
for val in \
    '/awg/$(id)/' \
    '/awg/a;id/' \
    '/awg/a|b/' \
    '/awg/%2e%2e/' \
    '/awg/a#b/'
do
    if inst_norm "$val" >/dev/null 2>&1; then
        bad "refuses ${val}" "it was accepted"
    else
        ok "refuses ${val}"
    fi
done

# install.sh has the same function, and the two have to answer the same way:
# one operator typing the same path at the two entry points must land on the
# same panel.
MAIN_NORM=$(sed -n '/^norm_base_path()/,/^}/p' "$REPO/install.sh")
is "install.sh and install-panel.sh normalise identically" "$INST_NORM" "$MAIN_NORM"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
