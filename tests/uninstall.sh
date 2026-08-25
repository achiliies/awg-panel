#!/bin/bash
#
# tests/uninstall.sh - prove the uninstaller takes the TLS side with it.
#
# Everything else awg-uninstall removes is visible the moment it fails: a
# tunnel still up, a panel still answering, a menu entry that is still there.
# The certificate is not. A renewal timer and a deploy hook left behind on a
# machine with no panel on it do nothing an admin would ever see - they wake up
# four times a day, find a lineage nobody serves, and either renew it against
# Let's Encrypt or fail quietly into the journal, for as long as the box lives.
# That is the part of the teardown that needs a test rather than a look.
#
# acme_remove is checked directly, against a tree of fakes, because the script
# around it cannot be: awg-uninstall deletes /usr/bin/awg and unregisters a DKMS
# module by absolute path, so running it to see what it does costs whoever runs
# it their AmneziaWG installation.
#
# Usage: tests/uninstall.sh      (exit 0 = all checks passed)

set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

# shellcheck source=lib/common.sh
. "$REPO/lib/common.sh"
# shellcheck source=lib/firewall.sh
. "$REPO/lib/firewall.sh"
# shellcheck source=lib/panel.sh
. "$REPO/lib/panel.sh"
# shellcheck source=lib/acme.sh
. "$REPO/lib/acme.sh"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

FAIL=0
ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; FAIL=1; }
gone()  { [[ -e "$1" ]] && bad "$2 is still there" || ok "$2"; }
there() { [[ -e "$1" ]] && ok "$2" || bad "$2 was removed"; }

# A systemctl that answers every call and records none of it. The real one is
# not on every machine this suite runs on, and what it would be asked here -
# disable a timer, reload - has no result acme_remove reads.
# A systemctl that answers every call and records none of it, except that the
# two listing calls awg_ifaces makes will read back whatever FAKE_UNITS names.
# Unset - which is every case in the TLS section below - it prints nothing and
# exits 0, exactly as before.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/systemctl" <<'STUB'
#!/bin/sh
case "$1" in
    list-units|list-unit-files)
        [ -n "${FAKE_UNITS:-}" ] && [ -f "$FAKE_UNITS" ] && cat "$FAKE_UNITS"
        exit 0 ;;
esac
exit 0
STUB
chmod 755 "$WORK/bin/systemctl"

# And a certbot on PATH that refuses everything, standing in for the machine
# whose certbot has been uninstalled out from under /etc/letsencrypt. It is
# here for safety as much as for the case it covers: certbot takes its config
# directory from its own default, not from the ACME_LE this test repoints, so a
# real one reached from PATH would delete the real awg-panel certificate off
# whatever machine the suite is running on.
cat > "$WORK/bin/certbot" <<'STUB'
#!/bin/sh
echo "certbot: command not usable" >&2
exit 1
STUB
chmod 755 "$WORK/bin/certbot"
PATH="$WORK/bin:$PATH"

# Point every path in lib/acme.sh at the fake tree. They are set at source time
# from ACME_LE, so the derived ones have to be repointed by hand.
setup() {
    rm -rf "$WORK/tree"
    ACME_LE="$WORK/tree/etc/letsencrypt"
    ACME_LIVE="${ACME_LE}/live/${ACME_LINEAGE}"
    ACME_HOOK_DIR="${ACME_LE}/renewal-hooks/deploy"
    ACME_HOOK="${ACME_HOOK_DIR}/10-awg-panel"
    ACME_UNIT_DIR="$WORK/tree/etc/systemd/system"
    ACME_VENV="$WORK/tree/opt/certbot"
    ACME_CERTBOT=certbot

    mkdir -p "$ACME_UNIT_DIR" "$ACME_HOOK_DIR" "$ACME_VENV/bin"
    : > "${ACME_UNIT_DIR}/${ACME_TIMER}.timer"
    : > "${ACME_UNIT_DIR}/${ACME_TIMER}.service"
    : > "$ACME_HOOK"
    lineage "$ACME_LINEAGE"

    # A certbot in the virtualenv, which is the one acme_pick_certbot prefers,
    # deleting a lineage the way the real one does.
    cat > "$ACME_VENV/bin/certbot" <<STUB
#!/bin/bash
[[ "\$1" == delete ]] || exit 0
name="\${4:-}"
rm -rf "${ACME_LE}/live/\${name}" "${ACME_LE}/archive/\${name}"
rm -f  "${ACME_LE}/renewal/\${name}.conf"
STUB
    chmod 755 "$ACME_VENV/bin/certbot"
}

# One certbot lineage, complete enough for acme_lineages to count it.
lineage() {
    mkdir -p "${ACME_LE}/live/$1" "${ACME_LE}/archive/$1" "${ACME_LE}/renewal"
    : > "${ACME_LE}/live/$1/fullchain.pem"
    : > "${ACME_LE}/live/$1/privkey.pem"
    : > "${ACME_LE}/renewal/$1.conf"
}

echo "uninstall: the panel's certificate"

echo
echo "a full removal takes the timer, the hook, the certificate and certbot"
setup
acme_remove > "$WORK/out" 2>&1 || bad "acme_remove returned non-zero"
gone "${ACME_UNIT_DIR}/${ACME_TIMER}.timer"   "renewal timer"
gone "${ACME_UNIT_DIR}/${ACME_TIMER}.service" "renewal service"
gone "$ACME_HOOK"                             "deploy hook"
gone "${ACME_LE}/live/${ACME_LINEAGE}"        "certificate"
gone "${ACME_LE}/renewal/${ACME_LINEAGE}.conf" "renewal config"
gone "$ACME_VENV"                             "certbot virtualenv"

echo
echo "another certificate on the box keeps its certbot"
setup
lineage other.example.com
acme_remove > "$WORK/out" 2>&1 || bad "acme_remove returned non-zero"
gone  "${ACME_LE}/live/${ACME_LINEAGE}"     "the panel's certificate"
there "${ACME_LE}/live/other.example.com"   "the unrelated certificate"
there "$ACME_VENV"                          "certbot virtualenv"

echo
echo "--keep-cert leaves the certificate for the reinstall to find"
setup
acme_remove --keep-cert > "$WORK/out" 2>&1 || bad "acme_remove returned non-zero"
gone  "${ACME_UNIT_DIR}/${ACME_TIMER}.timer" "renewal timer"
gone  "$ACME_HOOK"                           "deploy hook"
there "${ACME_LE}/live/${ACME_LINEAGE}"      "certificate"
there "$ACME_VENV"                           "certbot virtualenv"

echo
echo "a certbot that has been uninstalled does not strand the lineage"
setup
rm -rf "$ACME_VENV"
acme_remove > "$WORK/out" 2>&1 || bad "acme_remove returned non-zero"
gone "${ACME_LE}/live/${ACME_LINEAGE}"         "certificate"
gone "${ACME_LE}/archive/${ACME_LINEAGE}"      "archived material"
gone "${ACME_LE}/renewal/${ACME_LINEAGE}.conf" "renewal config"

echo
echo "a panel that never had HTTPS on says so and succeeds"
setup
rm -rf "$WORK/tree"
if acme_remove > "$WORK/out" 2>&1; then
    grep -q "nothing installed" "$WORK/out" \
        && ok "nothing to remove" || bad "said something other than nothing"
else
    bad "acme_remove returned non-zero on a machine with no certificate"
fi

echo
echo "uninstall: which tunnel it is actually removing"
echo

# Extracted from the script rather than copied, for the reason tests/detect.sh
# extracts the installer's awk: a copy goes on passing while the original
# drifts away from it. Both pieces are lifted by anchor, and a missing anchor
# fails loudly here instead of silently testing nothing.
UNINSTALLER="$REPO/bin/awg-uninstall"
RESOLVE=$(sed -n '/^if \[\[ -z "${AWG_IFACE:-}" \]\]; then/,/^ENV_FILE=/p' "$UNINSTALLER")
IFACES_FN=$(awk '/^awg_ifaces\(\) \{/,/^\}/' "$UNINSTALLER")
[[ "$RESOLVE" == *panel_env_get* && "$IFACES_FN" == *list-unit-files* ]] || {
    echo "  FAIL  could not lift the interface resolution out of bin/awg-uninstall" >&2
    echo "        (the anchors in this test need updating)" >&2
    exit 1; }

# One run of that code against a fixture. The env file and the config
# directory are written per case; what comes back is what the teardown would
# have used, on the line the uninstaller itself would have used it.
resolve() {
    local panel_env="$1" conf_dir="$2" units="$3" want_iface="$4" want_list="$5"
    local label="$6" got
    got=$(
        export AWG_PANEL_ENV="$panel_env" FAKE_UNITS="$units"
        unset AWG_IFACE AWG_CONF_DIR
        [[ -n "${FORCE_IFACE:-}" ]] && export AWG_IFACE="$FORCE_IFACE"
        export AWG_CONF_DIR="$conf_dir"
        bash -c '
            set -uo pipefail
            . "$1/lib/common.sh"
            . "$1/lib/panel.sh"
            '"$RESOLVE"'
            '"$IFACES_FN"'
            printf "%s|%s|%s\n" "$IFACE" "$SERVER_CONF" "$(awg_ifaces | tr "\n" " ")"
        ' _ "$REPO"
    )
    local iface="${got%%|*}" rest="${got#*|}"
    local conf="${rest%%|*}" list="${rest#*|}"
    list="${list% }"
    if [[ "$iface" == "$want_iface" && "$list" == "$want_list" \
          && "$conf" == "$conf_dir/${want_iface}.conf" ]]; then
        ok "$label"
    else
        bad "$label"
        printf '        got:    iface=%s conf=%s list=[%s]\n' "$iface" "$conf" "$list"
        printf '        wanted: iface=%s conf=%s list=[%s]\n' \
               "$want_iface" "$conf_dir/${want_iface}.conf" "$want_list"
    fi
}

CASE="$WORK/case"
newcase() { rm -rf "$CASE"; mkdir -p "$CASE/conf"; : > "$CASE/units"; }

# The default install, and the only shape that ever worked: no --iface, so the
# env file records awg0 and the library's own default already agreed.
newcase
printf 'AWG_IFACE=awg0\nAWG_CONF_DIR=%s\n' "$CASE/conf" > "$CASE/env"
: > "$CASE/conf/awg0.conf"
resolve "$CASE/env" "$CASE/conf" "$CASE/units" awg0 "awg0" \
        "a default install is found where it always was"

# The regression. `install.sh --iface awg1` is a documented flag; the env file
# is where what it chose was written down. Reading awg0 here is what left a
# live tunnel behind on a box the tool had just called clean.
newcase
printf 'AWG_IFACE=awg1\nAWG_CONF_DIR=%s\n' "$CASE/conf" > "$CASE/env"
: > "$CASE/conf/awg1.conf"
resolve "$CASE/env" "$CASE/conf" "$CASE/units" awg1 "awg1" \
        "--iface awg1 is read back out of the panel env file"

# No panel, so no env file: nothing recorded the choice and awg0 is the only
# answer left. The config directory still gets a say, which is what stops a
# panel-less --iface install from being missed entirely.
newcase
: > "$CASE/conf/awg7.conf"
resolve "$CASE/nonexistent-env" "$CASE/conf" "$CASE/units" awg0 "awg0 awg7" \
        "a config on disk is swept even with no env file to name it"

# An earlier uninstall that died after deleting the config directory but before
# disabling the unit. The unit is the only evidence left, and it is enough.
newcase
printf 'AWG_IFACE=awg1\nAWG_CONF_DIR=%s\n' "$CASE/conf" > "$CASE/env"
printf 'awg-quick@awg1.service loaded active exited\n' > "$CASE/units"
resolve "$CASE/env" "$CASE/conf" "$CASE/units" awg1 "awg1" \
        "a unit with no config behind it is still taken down"

# Two tunnels. Both units fail at every boot once this run removes the module,
# the tools and the unit template, so both have to go.
newcase
printf 'AWG_IFACE=awg1\nAWG_CONF_DIR=%s\n' "$CASE/conf" > "$CASE/env"
: > "$CASE/conf/awg1.conf"; : > "$CASE/conf/awg2.conf"
printf 'awg-quick@awg3.service enabled enabled\n' > "$CASE/units"
resolve "$CASE/env" "$CASE/conf" "$CASE/units" awg1 "awg1 awg2 awg3" \
        "every tunnel on the box is taken down, not just the named one"

# clients.env sits in the same directory and is not a tunnel.
newcase
printf 'AWG_IFACE=awg0\nAWG_CONF_DIR=%s\n' "$CASE/conf" > "$CASE/env"
: > "$CASE/conf/awg0.conf"; : > "$CASE/conf/clients.env"; mkdir -p "$CASE/conf/clients"
resolve "$CASE/env" "$CASE/conf" "$CASE/units" awg0 "awg0" \
        "clients.env and clients/ are not mistaken for tunnels"

# The environment still wins. This is how the harnesses point the tool at a
# fixture, and it must not be overruled by a file on the machine.
newcase
printf 'AWG_IFACE=awg1\nAWG_CONF_DIR=%s\n' "$CASE/conf" > "$CASE/env"
: > "$CASE/conf/awg9.conf"
FORCE_IFACE=awg9 resolve "$CASE/env" "$CASE/conf" "$CASE/units" awg9 "awg9" \
        "an exported AWG_IFACE overrules the env file"

echo
if (( FAIL )); then printf 'RESULT: FAIL\n'; else printf 'RESULT: PASS\n'; fi
exit $FAIL
