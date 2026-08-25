# shellcheck shell=bash
# shellcheck disable=SC2034
#
# lib/firewall.sh - keep an active host firewall in sync.
#
# Self-contained. ufw and firewalld are handled; anything else, and cloud
# security groups, cannot be reached from here and stay the operator's job -
# which is why every caller reports FW_HANDLED back to them.
#
# All three functions succeed quietly when no supported firewall is active:
# a missing firewall is not an error, it is a host that does its filtering
# elsewhere. FW_HANDLED names the backend that took the change, or is empty.

FW_HANDLED=""

# fw_move NEW OLD [PROTO] - allow NEW, and drop OLD once NEW is in place.
# An empty OLD just opens NEW.
fw_move() {
    local new="$1" old="${2:-}" proto="${3:-udp}"
    FW_HANDLED=""
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        ufw allow "${new}/${proto}" >/dev/null 2>&1 || return 0
        if [[ -n "$old" && "$old" != "$new" ]]; then
            ufw delete allow "${old}/${proto}" >/dev/null 2>&1
        fi
        FW_HANDLED="ufw"
    elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
        firewall-cmd -q --permanent --add-port="${new}/${proto}" 2>/dev/null || return 0
        if [[ -n "$old" && "$old" != "$new" ]]; then
            firewall-cmd -q --permanent --remove-port="${old}/${proto}" 2>/dev/null
        fi
        firewall-cmd -q --reload 2>/dev/null
        FW_HANDLED="firewalld"
    fi
    return 0
}

# fw_allow PORT [PROTO] - open a port.
fw_allow() { fw_move "$1" "" "${2:-udp}"; }

# fw_delete PORT [PROTO] - remove a rule this tooling added earlier.
fw_delete() {
    local port="$1" proto="${2:-udp}"
    FW_HANDLED=""
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        if ufw delete allow "${port}/${proto}" >/dev/null 2>&1; then
            FW_HANDLED="ufw"
        fi
    elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
        firewall-cmd -q --permanent --remove-port="${port}/${proto}" 2>/dev/null
        firewall-cmd -q --reload 2>/dev/null
        FW_HANDLED="firewalld"
    fi
    return 0
}
