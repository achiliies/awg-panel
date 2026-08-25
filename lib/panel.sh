# shellcheck shell=bash
# shellcheck disable=SC2034,SC2154
#
# lib/panel.sh - what the shell side knows about the optional web panel.
#
# Requires lib/firewall.sh (panel_remove drops the panel's TCP rule).
#
# The panel's env file is the single source of truth for its listen address,
# port, base path and TLS: systemd, manage.py, bin/awg-panel and these
# helpers all read the same file. Values are written unquoted, exactly as
# panel/install-panel.sh writes them.

PANEL_CLI=/usr/local/bin/awg-panel
PANEL_ENV="${AWG_PANEL_ENV:-/etc/awg-panel.env}"
PANEL_PREFIX="${AWG_PANEL_PREFIX:-/opt/awg-panel}"
PANEL_DATA="${AWG_PANEL_DATA:-/var/lib/awg-panel}"

panel_installed() { [[ -x "$PANEL_CLI" || -d "$PANEL_PREFIX" ]]; }

# Nothing when the key is not set, and nothing - successfully - when the file
# is not there at all. The status is the point: every caller runs under `set -o
# pipefail`, so a missing env file made sed's exit 2 this function's, and a
# plain `port=$(panel_env_get AWG_PANEL_PORT)` under errexit then ended the
# script where it stood, with nothing printed, over a question whose answer was
# only ever going to be "not set". That reached the installer's own summary, the
# uninstaller's panel_remove and acme_state.
panel_env_get() {
    [[ -f "$PANEL_ENV" ]] || return 0
    sed -n "s/^${1}=//p" "$PANEL_ENV" 2>/dev/null | tr -d '"'
}

# Set one key in the env file, preserving everything else. Returns non-zero
# when the file is missing: writing settings for a panel that is not
# installed would only manufacture a half-installed one.
#
# The value never becomes syntax. This was `sed -i "s|^${key}=.*|${key}=${val}|"`,
# where the replacement half is not a literal: `&` stands for the whole match,
# `\1` for a group, a backslash is eaten, and a `|` closes the expression early
# and leaves sed reading the rest of the value as flags. install.sh has already
# been through this once - env_set_kv carries the note about a `|` in a comment
# killing every upgrade with "unknown option to `s'" - and this function was
# the copy that did not get the lesson.
#
# It went through awk rather than sed's `c`, which is what env_set_kv reached
# for, because `c` still eats backslashes in its text. The key and value cross
# into awk through the environment instead of `-v`, which would expand `\n` in
# a value into a newline and split the file. What comes out the far side is
# what went in, whatever it was.
#
# A key that somehow appears twice leaves once. The old expression rewrote both
# lines to the same value, which is not wrong so much as it is a file nobody
# can reason about; the panel and this function disagreeing about which one
# counts is worse than either answer.
panel_env_set() {
    local key="$1" val="$2" tmp
    [[ -f "$PANEL_ENV" ]] || return 1
    tmp=$(mktemp "${PANEL_ENV}.XXXXXX") || return 1
    # Before anything is written to it: the file carries the panel's TLS key
    # path and its base path, and a world-readable moment is still a moment.
    chmod 600 "$tmp"
    if AWG_ENV_KEY="$key" AWG_ENV_VAL="$val" awk '
        BEGIN { key = ENVIRON["AWG_ENV_KEY"]; val = ENVIRON["AWG_ENV_VAL"] }
        index($0, key "=") == 1 { if (!done) { print key "=" val; done = 1 } next }
        { print }
        END { if (!done) print key "=" val }
    ' "$PANEL_ENV" > "$tmp"; then
        mv -f "$tmp" "$PANEL_ENV" || { rm -f "$tmp"; return 1; }
    else
        rm -f "$tmp"
        return 1
    fi
    chmod 600 "$PANEL_ENV"
}

# A fresh secret base path, in the shape a first install generates.
#
# It lives here, beside the env-file helpers, rather than in lib/obfs.sh with
# the installer's other random helper, because bin/awg-menu deliberately does
# not source that file - and calling into it from there anyway is exactly how
# rotating a leaked URL came to destroy the secret rather than replace it.
# rand_hex expanded to nothing, panel_env_set wrote /awg// and reported
# success, and the panel came back up on a fixed prefix with no secret left in
# it and nothing anywhere saying so.
#
# 512 bytes in for 20 characters out. tr keeps only the 36 byte values that
# already are [a-z0-9], which is uniform over them and needs no modulo to
# skew, but it keeps about one byte in seven and how many survive varies from
# draw to draw - so the input has to be far longer than the output rather than
# merely longer, or an unlucky rotation lands on a short path.
#
# Written to the same shape as the copy in panel/install-panel.sh, which
# cannot source this file: tests/basepath.sh pulls both out with one
# expression and fails if they ever stop matching.
panel_base_path_new() {
    local BASE_PATH
    BASE_PATH="/awg/$(head -c 512 /dev/urandom | LC_ALL=C tr -dc 'a-z0-9' | cut -c1-20)/"
    printf '%s' "$BASE_PATH"
}

# Stop and delete the panel: services, unit files, code, env file, firewall
# rule and the awg-panel CLI. The database under /var/lib/awg-panel is left
# alone - accounts, quotas and history are the operator's data, and only
# awg-uninstall --purge deletes data. Reinstalling the panel picks it up.
panel_remove() {
    systemctl disable --now awg-panel-web awg-panel-collector >/dev/null 2>&1
    rm -f /etc/systemd/system/awg-panel-web.service \
          /etc/systemd/system/awg-panel-collector.service
    systemctl daemon-reload 2>/dev/null
    local pport; pport=$(panel_env_get AWG_PANEL_PORT)
    if [[ -n "$pport" ]]; then
        fw_delete "$pport" tcp
        [[ -n "$FW_HANDLED" ]] && echo "$(t "  ${FW_HANDLED}: removed ${pport}/tcp" \
                                             "  ${FW_HANDLED}: удалён порт ${pport}/tcp")"
    fi
    rm -rf "$PANEL_PREFIX"
    rm -f "$PANEL_ENV" "$PANEL_CLI"
    return 0
}
