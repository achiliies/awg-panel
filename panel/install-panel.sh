#!/bin/bash
#
# install-panel.sh - install, upgrade or remove AWG Panel, the web interface
#
# The panel owns /etc/amnezia/amneziawg: it is the only thing that writes the
# server config, the client configs and the traffic database, and it takes the
# same lock the shell tools took when they were writers too. Installing it
# changes nothing about the tunnel; removing it leaves the tunnel running, but
# with no way to add or revoke a client, which is why it is refused while one
# is configured.
#
# Called by ../install.sh --panel, and usable on its own against a server
# that already runs AmneziaWG:
#
#   sudo panel/install-panel.sh
#   sudo panel/install-panel.sh --uninstall
#
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

PREFIX=/opt/awg-panel
DATA_DIR=/var/lib/awg-panel
ENV_FILE=/etc/awg-panel.env
UNIT_DIR=/etc/systemd/system

IFACE=awg0
CONF_DIR=/etc/amnezia/amneziawg
PORT=2097
LISTEN=0.0.0.0
# Whether the operator asked for these on this run. An upgrade must not push a
# panel that was deliberately bound to 127.0.0.1 back onto 0.0.0.0 just because
# the flag defaults to it.
PORT_GIVEN=0
LISTEN_GIVEN=0
# The path and the account name, when a caller has already settled them - the
# installer asks for both on a first install. Empty means nothing was chosen,
# and this script draws one, which is the only place either is drawn. The
# password arrives the same way but through $AWG_PANEL_ADMIN_PASSWORD rather
# than a flag: /proc/<pid>/cmdline is world-readable, /proc/<pid>/environ is
# not, and a password on an argv is visible to every local user for as long as
# the command runs.
BASE_PATH_GIVEN=""
ADMIN_USER=""
# Whether the caller means those two to land on an account that already exists.
# Without it seedadmin leaves one alone, which is right for an upgrade nobody
# asked a question during and wrong for the one case it is asked in: an
# operator re-running the installer to get back into a panel they are locked
# out of.
ADMIN_RESET=0
STANDALONE=0
UNINSTALL=0
BUILD_FRONTEND=auto
# Which language to print in. install.sh passes what its own run settled on;
# a bundle run on its own takes English unless told otherwise, because the
# question that would ask is install.sh's and this script has no terminal
# interview of its own to hang it off.
LANG_CHOICE="${LANG_CHOICE:-en}"

usage() {
    cat <<'USAGE'
install-panel.sh - install or upgrade AWG Panel, the web interface

  --iface NAME      tunnel interface the panel manages (default: awg0)
  --conf-dir DIR    AmneziaWG config directory (default: /etc/amnezia/amneziawg)
  --port N          panel HTTP port          (default: 2097)
  --listen ADDR     panel bind address       (default: 0.0.0.0)
  --base-path PATH  secret URL prefix        (default: /awg/<20 random>/)
  --admin-user NAME first account's name     (default: 10 random characters)
  --admin-reset     apply --admin-user and the password to the account that is
                    already there, instead of keeping it
  --lang CODE       what this prints in: en | ru   (default: en)
  --standalone      do not require an existing AmneziaWG installation
  --no-build        never build the frontend; require a prebuilt dist
  --uninstall       stop and remove the panel (keeps /var/lib/awg-panel)

Re-running is an upgrade: code and dependencies are refreshed, the database,
accounts and settings are kept.
USAGE
}

# The secret prefix, in the one shape the rest of this understands: a leading
# slash, a trailing slash, no empty or repeated separators, and nothing in a
# segment that has a meaning to a URL, a shell or a regex. install.sh applies
# the same rule to what it asks the operator for and hands the result over
# already normalised - but this script is a documented way to install on its
# own, and on that path nothing had ever looked at the value. It went into
# /etc/awg-panel.env, which is sourced, and into Django's URL configuration.
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

while [[ $# -gt 0 ]]; do
    case "$1" in
        --iface)      IFACE="${2:?}";    shift 2 ;;
        --conf-dir)   CONF_DIR="${2:?}"; shift 2 ;;
        --port)       PORT="${2:?}";   PORT_GIVEN=1;   shift 2 ;;
        --listen)     LISTEN="${2:?}"; LISTEN_GIVEN=1; shift 2 ;;
        # Checked at the flag, like --lang: a path this cannot make sense of
        # is a typo, and answering it here says so instead of failing three
        # minutes later inside Django's URL configuration.
        --base-path)
            BASE_PATH_GIVEN=$(norm_base_path "${2:?}") \
                || { echo "--base-path '${2}': segments may use letters, digits and . _ ~ -" >&2
                     exit 1; }
            shift 2 ;;
        --admin-user) ADMIN_USER="${2:?}";      shift 2 ;;
        --admin-reset) ADMIN_RESET=1;           shift ;;
        # Checked at the flag rather than after the root test below, so that a
        # typo in it answers the typo instead of answering "run with sudo".
        --lang)
            LANG_CHOICE="${2:?}"
            [[ "$LANG_CHOICE" == en || "$LANG_CHOICE" == ru ]] \
                || { echo "--lang '${LANG_CHOICE}': expected 'en' or 'ru'" >&2; exit 1; }
            shift 2 ;;
        --standalone) STANDALONE=1;      shift ;;
        --no-build)   BUILD_FRONTEND=0;  shift ;;
        --uninstall)  UNINSTALL=1;       shift ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

[[ ${EUID} -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'
step() { printf '\n%s==> %s%s\n' "$G" "$1" "$N"; }
warn() { printf '%s!! %s%s\n' "$Y" "$1" "$N"; }
die()  { printf '%sERROR: %s%s\n' "$R" "$1" "$N" >&2; exit 1; }
# Spelled out here rather than taken from lib/i18n.sh, for the same reason the
# palette above it is: this script runs standalone from the bundle and sources
# nothing. The locale handling that i18n.sh does is left with it - nothing
# below pads a column or lowercases an answer, so there is nothing here for a
# C locale to get wrong.
t() {
    if [[ "$LANG_CHOICE" == ru ]]; then printf '%s' "$2"
    else                                printf '%s' "$1"; fi
}

# ------------------------------------------------------------- uninstall
if (( UNINSTALL )); then
    # Removing the panel from a server that still has a tunnel used to be a
    # supported thing to want: the panel was a second front end, and the CLI
    # went on managing clients without it. That is no longer true. The panel is
    # the only thing that adds or revokes a client, and awg0.conf carries two
    # hooks that call it - so taking it off a live tunnel leaves a server whose
    # clients cannot be changed, whose usage stops being counted across
    # restarts, and which re-admits every revoked client at the next boot.
    #
    # Refused rather than warned about, because all three failures are silent
    # and the last one is a security hole. Removing the whole thing is still
    # one command; that path takes the tunnel and its hooks with it.
    if [[ -f /etc/amnezia/amneziawg/${IFACE:-awg0}.conf ]]; then
        die "this server still has a tunnel, and the panel is the only thing that can
       manage its clients - removing it would leave clients that cannot be
       revoked and revocations that come undone at the next boot.

       To remove everything:      sudo awg-uninstall
       To stop the panel for now: sudo awg-panel stop"
    fi
    step "Removing the web panel"
    systemctl disable --now awg-panel-web awg-panel-collector >/dev/null 2>&1 || true
    rm -f "$UNIT_DIR/awg-panel-web.service" "$UNIT_DIR/awg-panel-collector.service"
    systemctl daemon-reload || true
    rm -rf "$PREFIX"
    rm -f "$ENV_FILE" /usr/local/bin/awg-panel
    # The updater goes with the thing it updates. It reads $ENV_FILE for the
    # repository and $PREFIX for the version and the release helper, all three
    # of which have just gone, so what is left of it would be a root-owned
    # command that can only report how broken it is.
    rm -f /usr/local/bin/awg-update
    echo "  services, ${PREFIX} and ${ENV_FILE} removed"
    # The two pieces of the TLS side that keep running on their own. The timer
    # would go on renewing a certificate nothing serves, four times a day for
    # as long as the machine lives, and the deploy hook would go on trying to
    # restart a service that is not there - neither visible anywhere an admin
    # would think to look. The certificate itself stays, like the database:
    # a panel put back here can use both again. Spelled out rather than taken
    # from lib/acme.sh, because this script runs standalone from the bundle
    # and sources nothing.
    if [[ -f "$UNIT_DIR/awg-certbot-renew.timer" ]]; then
        systemctl disable --now awg-certbot-renew.timer >/dev/null 2>&1 || true
        rm -f "$UNIT_DIR/awg-certbot-renew.timer" "$UNIT_DIR/awg-certbot-renew.service"
        systemctl daemon-reload || true
        echo "  awg-certbot-renew.timer removed"
    fi
    rm -f /etc/letsencrypt/renewal-hooks/deploy/10-awg-panel
    echo "  ${DATA_DIR} kept (accounts, quotas, traffic history)"
    echo "  the certificate is kept too, for a panel put back here later"
    echo "  delete them with: rm -rf ${DATA_DIR}; certbot delete --cert-name awg-panel"
    exit 0
fi

# -------------------------------------------------------------- preflight
step "$(t "Checking the system" "Проверка системы")"
. /etc/os-release 2>/dev/null || die "$(t "cannot read /etc/os-release" \
                                          "не удалось прочитать /etc/os-release")"
echo "  ${PRETTY_NAME:-unknown}"

if (( ! STANDALONE )); then
    [[ -f "$CONF_DIR/${IFACE}.conf" ]] \
        || die "$(t "no server config at $CONF_DIR/${IFACE}.conf - install AmneziaWG first (sudo ./install.sh), or pass --standalone" \
                    "нет конфигурации сервера в $CONF_DIR/${IFACE}.conf — сначала установите AmneziaWG (sudo ./install.sh) или передайте --standalone")"
fi

command -v python3 >/dev/null || die "$(t "python3 is not installed" "python3 не установлен")"
PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "$(t "python ${PYVER} is too old; the panel needs 3.10 or newer" \
                "python ${PYVER} слишком стар; панели нужен 3.10 или новее")"
echo "  python ${PYVER}"

[[ -d "$PREFIX" ]] && echo "$(t "  existing install at ${PREFIX} - upgrading" \
                                "  найдена установленная панель в ${PREFIX} — обновление")"

# ----------------------------------------------------------- dependencies
step "$(t "Installing dependencies" "Установка зависимостей")"
if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    # unattended-upgrades runs on a timer on a stock Ubuntu box, so a plain
    # apt-get here fails with "Could not get lock" whenever it happens to be
    # mid-run. Wait for the lock instead of dying; apt has supported this since
    # 2.0, and the alternative is an installer that fails for reasons that have
    # nothing to do with it.
    APT="apt-get -o DPkg::Lock::Timeout=300"
    NEED=()
    python3 -c 'import venv, ensurepip' 2>/dev/null || NEED+=(python3-venv)
    dpkg -s python3-dev >/dev/null 2>&1 || NEED+=(python3-dev)
    command -v cc >/dev/null 2>&1 || NEED+=(build-essential)
    if (( ${#NEED[@]} )); then
        $APT update -qq
        $APT install -y -qq "${NEED[@]}" >/dev/null
        echo "$(t "  installed: ${NEED[*]}" "  установлено: ${NEED[*]}")"
    else
        echo "$(t "  already present" "  уже установлены")"
    fi
else
    warn "$(t "no apt-get; make sure python3-venv and a C compiler are installed" \
              "здесь нет apt-get; убедитесь, что установлены python3-venv и компилятор C")"
fi

# ------------------------------------------------------------ frontend
# Built here rather than on the server when possible: a prebuilt dist keeps
# Node off production boxes entirely.
step "$(t "Preparing the web assets" "Подготовка веб-интерфейса")"
if [[ -f "$SCRIPT_DIR/frontend/dist/index.html" ]]; then
    echo "$(t "  using the prebuilt bundle in frontend/dist" \
              "  используется готовая сборка из frontend/dist")"
elif [[ "$BUILD_FRONTEND" == "0" ]]; then
    die "$(t "frontend/dist is missing and --no-build was given; build it with 'make -C panel build' or use a release tarball" \
             "frontend/dist нет, а передан --no-build; соберите через 'make -C panel build' или возьмите релизный архив")"
elif [[ -d "$SCRIPT_DIR/frontend" ]]; then
    # Bundling the UI is the only memory-hungry step in this installer, and the
    # cheapest VPS a VPN lands on is exactly where it runs out. Say so before
    # the OOM killer does.
    MEMKB=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
    SWAPKB=$(awk '/^SwapTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
    if (( MEMKB + SWAPKB < 1600000 )); then
        warn "$(t "only $(( (MEMKB + SWAPKB) / 1024 )) MB of RAM+swap; the UI build may be killed" \
                  "всего $(( (MEMKB + SWAPKB) / 1024 )) МБ ОЗУ+swap; сборку интерфейса может снять OOM-killer")"
        echo "$(t "  Better: build it where you have more memory and copy the result -" \
                  "  Лучше: соберите там, где памяти больше, и перенесите результат —")"
        echo "$(t "    make -C panel build     (then re-run this script)" \
                  "    make -C panel build     (затем запустите этот скрипт заново)")"
        echo "$(t "  or download awg-panel.sh from the releases page," \
                  "  или скачайте awg-panel.sh со страницы релизов —")"
        echo "$(t "  which ships the UI already built. Continuing anyway." \
                  "  там интерфейс уже собран. Пока продолжаем.")"
        # Node's default heap assumes a desktop; cap it so the build fails with
        # a readable error rather than taking the box down with it.
        export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=512}"
    fi
    if ! command -v npm >/dev/null 2>&1; then
        warn "$(t "no npm; installing nodejs to build the UI once" \
                  "npm не найден; ставим nodejs, чтобы собрать интерфейс один раз")"
        ${APT:-apt-get} install -y -qq nodejs npm >/dev/null 2>&1 \
            || die "$(t "could not install nodejs; build frontend/dist elsewhere and re-run" \
                        "не удалось установить nodejs; соберите frontend/dist в другом месте и запустите заново")"
    fi
    # Whatever the distro ships, which on Debian 12 is older than the engines
    # floor in package.json and older than anything CI has ever built with.
    # Not fatal - it usually works, and dying here would strand an install
    # that would have finished - but an untested toolchain should say so
    # rather than surface later as an error from inside esbuild. NodeSource
    # would fix it properly and is deliberately not added: a third-party apt
    # repository is a poor trade for one build on a VPN server.
    WANT_NODE=$(tr -d 'v \t\n' < "$SCRIPT_DIR/frontend/.nvmrc" 2>/dev/null || true)
    HAVE_NODE=$(node --version 2>/dev/null | tr -d 'v') || true
    if [[ -n "$WANT_NODE" && -n "$HAVE_NODE" && "${HAVE_NODE%%.*}" != "${WANT_NODE%%.*}" ]]; then
        warn "$(t "node ${HAVE_NODE}; this release is built and tested on ${WANT_NODE}" \
                  "node ${HAVE_NODE}; этот релиз собирается и тестируется на ${WANT_NODE}")"
        echo "$(t "  A prebuilt UI avoids the question entirely - see the releases page." \
                  "  С готовой сборкой интерфейса вопрос не встаёт вовсе — см. страницу релизов.")"
    fi
    echo "$(t "  building with npm (this takes a few minutes on a small VPS)" \
              "  собираем через npm (на маленьком VPS это занимает несколько минут)")"
    # npm ci alone. There used to be an "|| npm install" behind this, and it
    # undid the lockfile: npm install re-resolves every caret range against
    # the registry and rewrites package-lock.json, so the one build least able
    # to afford surprises - unattended, on someone else's server, with no test
    # suite after it - was the only one not pinned. If ci refuses, the lock and
    # package.json disagree, and that is a bug to fix in the repository rather
    # than route around here.
    (cd "$SCRIPT_DIR/frontend" && npm ci --no-audit --no-fund >/dev/null 2>&1) \
        || die "$(t "npm ci failed: package-lock.json disagrees with package.json, or the registry is unreachable. Build the UI where that can be fixed ('make -C panel build') and re-run, or use a release tarball, which ships it prebuilt" \
                    "npm ci не прошёл: package-lock.json расходится с package.json, либо реестр недоступен. Соберите интерфейс там, где это можно починить ('make -C panel build'), и запустите заново, либо возьмите релизный архив с готовой сборкой")"
    (cd "$SCRIPT_DIR/frontend" && npm run build >/dev/null 2>&1) \
        || die "$(t "the frontend build failed; run 'npm run build' in panel/frontend to see why" \
                    "сборка интерфейса не удалась; выполните 'npm run build' в panel/frontend, чтобы увидеть почему")"
    echo "$(t "  built frontend/dist" "  frontend/dist собран")"
else
    die "$(t "no frontend/ directory in this checkout" \
             "в этой копии репозитория нет директории frontend/")"
fi

# ------------------------------------------------------------- copy code
# Staged, not wiped-and-replaced. The old code used to be deleted before the
# new one was proven, so a pip install that ran out of disk - or a migration
# that failed - left a directory nothing could serve, on a box whose panel had
# been working five seconds earlier. Nothing under $PREFIX is destroyed now
# until the new release has installed its dependencies, migrated and collected
# its statics; if any of that fails, the previous tree and virtualenv go back.
#
# The virtualenv stays at $PREFIX/.venv throughout. pip bakes that absolute
# path into every console script and into pyvenv.cfg, so a venv built anywhere
# else and moved into place is a venv that does not work.
STAGE="" PREV="" SWAPPED=0 VENV_SAVED=0
VENV="$PREFIX/.venv"

restore_install() {
    local rc=$?
    trap - EXIT
    if (( rc == 0 )); then
        [[ -n "$STAGE" ]] && rm -rf "$STAGE"
        [[ -n "$PREV" ]] && rm -rf "$PREV"
        (( VENV_SAVED )) && rm -rf "${VENV}.prev"
        return 0
    fi
    [[ -n "$STAGE" ]] && rm -rf "$STAGE"
    if (( SWAPPED )) && [[ -n "$PREV" && -d "$PREV" ]]; then
        prefix_entries "$PREFIX" | while IFS= read -r -d '' entry; do
            rm -rf "$entry"
        done
        prefix_entries "$PREV" | while IFS= read -r -d '' entry; do
            mv -- "$entry" "$PREFIX/"
        done
        rmdir "$PREV" 2>/dev/null || true
    fi
    if (( VENV_SAVED )) && [[ -x "${VENV}.prev/bin/python" ]]; then
        rm -rf "$VENV"
        mv "${VENV}.prev" "$VENV"
    else
        # An unfinished snapshot is not something to restore from; it is only
        # something to clear away.
        rm -rf "${VENV}.prev"
    fi
    if (( SWAPPED || VENV_SAVED )); then
        warn "$(t "installation failed; the previous panel was put back" \
                  "установка не удалась; прежняя панель возвращена на место")"
        systemctl restart awg-panel-collector awg-panel-web >/dev/null 2>&1 || true
    fi
    return 0
}

# Everything directly inside a directory except the virtualenv and the two
# scratch directories this script owns, NUL-separated.
prefix_entries() {
    local dir="$1" entry name
    while IFS= read -r -d '' entry; do
        name=${entry##*/}
        [[ "$name" == ".venv" || "$name" == ".venv.prev" ]] && continue
        # fd 8 is held on this; moving it aside would release nothing but would
        # let the next run take a lock on a different inode.
        [[ "$name" == ".install.lock" ]] && continue
        [[ -n "$STAGE" && "$entry" == "$STAGE" ]] && continue
        [[ -n "$PREV" && "$entry" == "$PREV" ]] && continue
        printf '%s\0' "$entry"
    done < <(find "$dir" -mindepth 1 -maxdepth 1 -print0)
}

step "$(t "Staging the panel for ${PREFIX}" "Подготовка файлов панели в ${PREFIX}")"
install -d -m 755 "$PREFIX"
# One installer at a time. Two of them swap directories under each other, and
# the loser's rollback would put its own "previous" tree back over the winner's
# finished upgrade.
exec 8>"$PREFIX/.install.lock"
flock -n 8 || die "$(t "another install-panel.sh is already running against ${PREFIX}" \
                       "против ${PREFIX} уже работает другой install-panel.sh")"
trap restore_install EXIT
STAGE=$(mktemp -d "$PREFIX/.stage-XXXXXX")
tar -C "$SCRIPT_DIR" -cf - \
    --exclude=.venv --exclude=node_modules --exclude=__pycache__ \
    --exclude='*.pyc' --exclude=.pytest_cache --exclude=.ruff_cache \
    --exclude=staticfiles --exclude='*.sqlite3' --exclude=tests --exclude=.dev \
    . | tar -C "$STAGE" -xf - || die "$(t "could not stage the new panel code" \
                                          "не удалось подготовить новый код панели")"
echo "$(t "  staged" "  файлы скопированы")"

install -d -m 700 "$DATA_DIR"

# ---------------------------------------------------------- python deps
step "$(t "Installing Python dependencies" "Установка зависимостей Python")"
if [[ -x "$VENV/bin/python" ]]; then
    # A copy, so a half-finished upgrade cannot leave the old code running
    # against dependencies it was never tested with. The flag goes up BEFORE
    # the copy: a cp that dies half way still leaves a .venv.prev, and it is
    # the rollback's job to clear it.
    rm -rf "${VENV}.prev"
    VENV_SAVED=1
    cp -a "$VENV" "${VENV}.prev" || die "$(t "could not snapshot the existing virtualenv" \
                                             "не удалось сохранить копию существующего virtualenv")"
else
    python3 -m venv "$VENV" || die "$(t "could not create the virtualenv" \
                                        "не удалось создать virtualenv")"
fi
PY="$VENV/bin/python"
# A release bundle carries every wheel its locks name, for both architectures
# and every interpreter this script accepts, so the install needs no index at
# all: the bytes that end up running are the bytes in the file that was
# downloaded. An air-gapped box works, a PyPI outage is irrelevant, and
# nothing that happens to an index between the release and the install can
# reach this server.
PIP_SRC=()
if [[ -d "$STAGE/wheels" ]]; then
    PIP_SRC=(--no-index --find-links "$STAGE/wheels")
    WHEELS=$(find "$STAGE/wheels" -name '*.whl' | wc -l)
    # "noun: N" again, for the reason install.sh gives: the count decides
    # which ending the Russian noun takes, and nothing here knows the count.
    echo "$(t "  using the ${WHEELS} wheels bundled with this release" \
              "  wheel-пакетов из состава этого релиза: ${WHEELS}")"
fi

# --require-hashes either way. Every package is fixed to one version and one
# sha256, transitive dependencies included, so this server gets the code the
# release was tested with rather than whatever the ranges resolve to today.
install_locked() {
    local lock="$1" what="$2"
    if (( ${#PIP_SRC[@]} )); then
        "$PY" -m pip install --quiet "${PIP_SRC[@]}" --require-hashes -r "$lock" && return 0
        # A python or an architecture the release was not built for - musl, or
        # something newer than the versions fetch-wheels.sh covers. Falling back
        # keeps the install working, and loses nothing but the offline property:
        # the hashes still come from the lock, so what arrives is still exactly
        # what was tested.
        warn "$(t "the bundled wheels do not cover this python or architecture; using PyPI instead" \
                  "вложенные wheel-пакеты не подходят к этому python или архитектуре; берём с PyPI")"
        PIP_SRC=()
    fi
    "$PY" -m pip install --quiet --require-hashes -r "$lock" \
        || die "$(t "could not install ${what}; see the output above" \
                    "не удалось установить ${what}; смотрите вывод выше")"
}

# pip first, and pinned like all the rest. The pip a venv is born with is
# whatever the distro froze - 22.0.2 on Ubuntu 22.04 - which predates the wheel
# metadata several of these packages now ship. Upgrading it used to mean
# "whatever is newest today", the same unpinned fetch this file exists to stop.
install_locked "$STAGE/requirements-bootstrap.lock" "the pinned pip"
install_locked "$STAGE/requirements.lock" "the panel's dependencies"
echo "  $("$PY" -c 'import django; print("django", django.get_version())') (from requirements.lock)"

# The wheels have done their job. Left in place they would sit under
# /opt/awg-panel for the life of the install, costing 31 MB to serve no
# purpose - the next upgrade brings its own copy.
rm -rf "$STAGE/wheels"

# --------------------------------------------------------- swap it in
# The dependencies are in and the tree is complete, so this is the first
# moment the live code can be replaced without a window where neither the old
# nor the new one would run. Move-aside rather than delete, so the migration
# below still has something to go back to.
step "$(t "Swapping in the new code" "Обновление файлов веб-панели")"
PREV=$(mktemp -d "$PREFIX/.prev-XXXXXX")
prefix_entries "$PREFIX" | while IFS= read -r -d '' entry; do
    mv -- "$entry" "$PREV/"
done
SWAPPED=1
while IFS= read -r -d '' entry; do
    mv -- "$entry" "$PREFIX/"
done < <(find "$STAGE" -mindepth 1 -maxdepth 1 -print0)
rmdir "$STAGE"; STAGE=""
chmod 755 "$PREFIX"
echo "$(t "  in place" "  файлы обновлены")"

# ------------------------------------------------------------- env file
# Written once. An upgrade keeps the operator's port, path and TLS choices,
# and only fills in keys that a newer version added.
step "$(t "Configuring" "Настройка переменных окружения")"
env_get() { sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | head -1; }

# What may appear on the right of an "=" in this file, and it is not a taste in
# formatting. The file is sourced by bash a few lines below and again by the
# panel's own tooling, and it is an EnvironmentFile for two units, so a value is
# written bare: quoting it would satisfy one reader and be handed to the panel
# with the quotes still on by the other. Bare means a value carrying shell
# metacharacters is executed as root at the next start, so the values that can
# reach here are the ones that cannot mean anything to a shell.
#
# The expression is the one the panel enforces on every save from the UI -
# apps/panel/defaults.py, _ENV_SAFE - because a rule that only half the writers
# apply is not a rule the readers can lean on. Nothing passed today comes close
# to failing it: these are an interface name, two directories, a port, a bind
# address and a URL path.
ENV_SAFE='^[A-Za-z0-9._:/@,+=-]*$'
env_put() {
    [[ "$2" =~ $ENV_SAFE ]] || { die "$(t \
        "${1} cannot be set to '${2}': ${ENV_FILE} is read as shell, so the
     value has to keep to letters, digits and . _ - / : @ , + = with no
     spaces." \
        "${1} нельзя присвоить '${2}': ${ENV_FILE} читается как shell, поэтому
     значение может содержать только буквы, цифры и . _ - / : @ , + = без
     пробелов.")"
         return 1; }
    local tmp
    tmp=$(mktemp "${ENV_FILE}.XXXXXX") || die "$(t "could not write ${ENV_FILE}" "не удалось записать ${ENV_FILE}")"
    # 600 before a byte of it exists: the file carries the panel's TLS key path
    # and its secret URL, and a world-readable moment is still a moment.
    chmod 600 "$tmp"
    # awk, not `sed -i "s|^$1=.*|$1=$2|"`, which is what this was. The
    # replacement half of an s expression is not a literal: `&` stands for the
    # whole match, `\1` for a group, a backslash is eaten, and a `|` closes the
    # expression early and leaves sed reading the rest of the value as flags,
    # which fails the write outright. install.sh lost an upgrade to that once
    # and lib/panel.sh's panel_env_set was fixed for it; this was the third
    # copy. The key and the value cross into awk through the environment rather
    # than -v, which would turn a `\n` in either into a newline.
    #
    # `index($0, key "=") == 1` rather than a regex, so a key is compared as
    # text: AWG_PANEL_TLS is a prefix of AWG_PANEL_TLS_CERT, and a match on the
    # shorter one that rewrote the longer line would hand the panel a
    # certificate path of "1".
    if AWG_ENV_KEY="$1" AWG_ENV_VAL="$2" awk '
        BEGIN { key = ENVIRON["AWG_ENV_KEY"]; val = ENVIRON["AWG_ENV_VAL"] }
        index($0, key "=") == 1 { if (!done) { print key "=" val; done = 1 } next }
        { print }
        END { if (!done) print key "=" val }
    ' "$ENV_FILE" > "$tmp"; then
        mv -f "$tmp" "$ENV_FILE" || { rm -f "$tmp"; die "$(t "could not write ${ENV_FILE}" "не удалось записать ${ENV_FILE}")"; }
    else
        rm -f "$tmp"
        die "$(t "could not write ${ENV_FILE}" "не удалось записать ${ENV_FILE}")"
    fi
    chmod 600 "$ENV_FILE"
}
# Only fills a gap: an upgrade must never overwrite a TLS path or a port the
# operator changed from the UI.
env_default() { grep -q "^$1=" "$ENV_FILE" 2>/dev/null || env_put "$1" "$2"; }

if [[ ! -f "$ENV_FILE" ]]; then
    : > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    # A random path is not authentication, but it keeps the login page out of
    # every internet-wide scanner's hit list. Two segments: a fixed /awg/ so
    # the URL says at a glance what it belongs to, and a random one behind it
    # that is the part actually worth guessing. The prefix costs some of the
    # cover - a scanner walking /awg/ knows the shape it has found - so the
    # secret half carries all of the entropy and none of it is spent on the
    # part an operator has to read out loud.
    #
    # 512 bytes in for 20 characters out. tr keeps only the 36 byte values that
    # already are [a-z0-9], which is uniform over them and needs no modulo to
    # skew, but it keeps about one byte in seven and how many survive varies
    # from draw to draw - so the input has to be far longer than the output
    # rather than merely longer, or an unlucky install gets a short path and
    # nothing anywhere says so.
    BASE_PATH="/awg/$(head -c 512 /dev/urandom | LC_ALL=C tr -dc 'a-z0-9' | cut -c1-20)/"
else
    BASE_PATH=$(env_get AWG_PANEL_BASE_PATH)
    [[ -n "$BASE_PATH" ]] || BASE_PATH="/"
fi
# A caller that has already put the question to the operator wins over both
# branches: over the generator, because a chosen path is not a gap to fill,
# and over the existing value, because passing this at all is an instruction
# to move the panel. install.sh only passes it on a first install, so an
# upgrade still keeps the path it is on.
if [[ -n "$BASE_PATH_GIVEN" ]]; then
    BASE_PATH="$BASE_PATH_GIVEN"
fi

env_put AWG_IFACE            "$IFACE"
env_put AWG_CONF_DIR         "$CONF_DIR"
env_put AWG_PANEL_DATA       "$DATA_DIR"
# Only when asked for on this run, or on a first install. Otherwise an upgrade
# would undo a bind address or port the operator changed here or from the UI -
# and re-exposing a panel that was deliberately on 127.0.0.1 is a security
# regression, not a cosmetic one.
if (( LISTEN_GIVEN )); then env_put AWG_PANEL_LISTEN "$LISTEN"
else                        env_default AWG_PANEL_LISTEN "$LISTEN"; fi
if (( PORT_GIVEN )); then   env_put AWG_PANEL_PORT "$PORT"
else                        env_default AWG_PANEL_PORT "$PORT"; fi
LISTEN=$(env_get AWG_PANEL_LISTEN); PORT=$(env_get AWG_PANEL_PORT)
env_put AWG_PANEL_BASE_PATH  "$BASE_PATH"
env_default AWG_PANEL_TLS      0
env_default AWG_PANEL_TLS_CERT ""
env_default AWG_PANEL_TLS_KEY  ""
# Which repository this installation checks for releases. Written out even
# though awg/release.py already defaults to the same value, because the file is
# where an operator looks for it: a fork wanting its own releases edits this
# line, and one wanting no check at all sets it to something that is not a
# repository. Read from the source rather than repeated here, so the literal
# exists once in the whole tree.
#
# env_default, so an operator who has changed it keeps their answer through
# every upgrade - the same rule the port and the bind address follow.
UPDATE_REPO=$(sed -n 's/^DEFAULT_REPO = "\(.*\)"$/\1/p' "$SCRIPT_DIR/awg/release.py" | head -1)
env_default AWG_PANEL_UPDATE_REPO "$UPDATE_REPO"
env_put DJANGO_SETTINGS_MODULE awgui.settings
chmod 600 "$ENV_FILE"
echo "  ${ENV_FILE}"

# ------------------------------------------------------------- database
step "$(t "Preparing the database" "Подготовка базы данных")"
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a
(cd "$PREFIX" && "$PY" manage.py migrate --noinput >/dev/null) \
    || die "$(t "database migration failed" "миграция базы данных не удалась")"
(cd "$PREFIX" && "$PY" manage.py collectstatic --noinput >/dev/null 2>&1) || true
# migrate runs from this script's umask, not the units' UMask=0077, and an
# install upgrading from an older release already has the file. It holds the
# admin's password hash and the TOTP seeds, so it gets the same 0600 every other
# secret on the box has rather than relying on the directory alone.
chmod 600 "${DATA_DIR}"/db.sqlite3 "${DATA_DIR}"/db.sqlite3-wal "${DATA_DIR}"/db.sqlite3-shm \
    2>/dev/null || true
echo "  ${DATA_DIR}/db.sqlite3"

# Whether an account was actually created, which is not the same question as
# "was there an env file". The account lives in ${DATA_DIR}, so deleting only
# /etc/awg-panel.env used to make this print a fresh password that does not
# work: seedadmin is idempotent and leaves the existing account alone. Ask
# seedadmin instead - it answers "created" or "exists" in one word - and run it
# on every install, which also closes the case where the env file was written
# and seedadmin then died.
ADMIN_PASS=""
ADMIN_CREATED=0
# Told apart from "created" because they read differently at the end: one is a
# new panel handing over its first credentials, the other is an existing panel
# whose credentials have just been overwritten on purpose.
ADMIN_RESET_DONE=0
# Whether the password about to be used came from the operator. It changes
# nothing about the account and everything about the summary: printing back a
# password somebody typed is noise at best, and one more place it can be read
# off a screen at worst.
ADMIN_PASS_GIVEN=0
[[ -n "${AWG_PANEL_ADMIN_PASSWORD:-}" ]] && ADMIN_PASS_GIVEN=1

# "admin" was the username on every install this panel ever did, which made
# half of the credential a constant: anything that found the login page had
# only the password left to guess, and every list of default logins already
# carried the other half. A random name costs the operator nothing - it is
# printed with the password and typed once into a browser - and it means a
# guess has to be right about both. Lower case and digits only, so it survives
# being read off a terminal and typed back in.
if [[ -z "$ADMIN_USER" ]]; then
    ADMIN_USER=$("$PY" - <<'PYEOF'
import secrets, string
alphabet = string.ascii_lowercase + string.digits
print("".join(secrets.choice(alphabet) for _ in range(10)))
PYEOF
)
fi
# A rename with no new password beside it keeps the password the account
# already had, so nothing is drawn for it: an invented one here would be an
# operator asking for one change and getting two, the second of them being the
# credential they still knew.
ADMIN_KEEP_PASS=0
if (( ADMIN_RESET )) && (( ! ADMIN_PASS_GIVEN )); then ADMIN_KEEP_PASS=1; fi
GENERATED_PASS="${AWG_PANEL_ADMIN_PASSWORD:-}"
if [[ -z "$GENERATED_PASS" ]] && (( ! ADMIN_KEEP_PASS )); then
    GENERATED_PASS=$("$PY" - <<'PYEOF'
import secrets, string
alphabet = string.ascii_letters + string.digits
print("".join(secrets.choice(alphabet) for _ in range(16)))
PYEOF
)
fi
SEED_ARGS=(--username "$ADMIN_USER")
(( ADMIN_RESET )) && SEED_ARGS+=(--reset)
SEED_ERR=$(mktemp)
# Through the environment, not the command line: /proc/<pid>/cmdline is
# world-readable, so --password would show the generated password to every
# local user for as long as the command ran. /proc/<pid>/environ is not.
SEED_OK=0
if SEED_OUT=$(cd "$PREFIX" && AWG_PANEL_ADMIN_PASSWORD="$GENERATED_PASS" \
        "$PY" manage.py seedadmin "${SEED_ARGS[@]}" 2>"$SEED_ERR"); then
    SEED_OK=1
elif (( ADMIN_KEEP_PASS )); then
    # --admin-reset is only passed by a caller that has already seen an account
    # here, so the way this is reached is that account going away between the
    # two - and then there is no password to keep, because there is nothing
    # left holding one. Draw one and create the account rather than failing an
    # install over a race nobody will ever reproduce.
    GENERATED_PASS=$("$PY" - <<'PYEOF'
import secrets, string
alphabet = string.ascii_letters + string.digits
print("".join(secrets.choice(alphabet) for _ in range(16)))
PYEOF
)
    ADMIN_KEEP_PASS=0
    if SEED_OUT=$(cd "$PREFIX" && AWG_PANEL_ADMIN_PASSWORD="$GENERATED_PASS" \
            "$PY" manage.py seedadmin --username "$ADMIN_USER" 2>"$SEED_ERR"); then
        SEED_OK=1
    fi
fi
if (( SEED_OK )); then
    case "$SEED_OUT" in
        *created*)
            ADMIN_CREATED=1
            ADMIN_PASS="$GENERATED_PASS"
            echo "$(t "  admin account ${ADMIN_USER} created" \
                      "  учётная запись ${ADMIN_USER} создана")"
            ;;
        *reset*)
            ADMIN_RESET_DONE=1
            if (( ADMIN_KEEP_PASS )); then
                echo "$(t "  admin account renamed to ${ADMIN_USER}, password unchanged" \
                          "  учётная запись переименована в ${ADMIN_USER}, пароль сохранён")"
            else
                ADMIN_PASS="$GENERATED_PASS"
                echo "$(t "  admin account ${ADMIN_USER} reset" \
                          "  учётная запись ${ADMIN_USER} сброшена")"
            fi
            ;;
        *)
            echo "$(t "  existing admin account kept" "  существующая учётная запись сохранена")"
            ;;
    esac
else
    cat "$SEED_ERR" >&2
    rm -f "$SEED_ERR"
    die "$(t "could not create the admin account" \
             "не удалось создать учётную запись администратора")"
fi
rm -f "$SEED_ERR"

# What the caller has to be able to say at the end. install.sh prints one
# summary after everything - the tunnel, the self-test, the certificate - and
# the credentials belong in it, several screens below where they are decided
# here. A file in the panel's own data directory rather than /tmp or an argv:
# it is 0700 and root-owned, the file is created 0600, and it is removed by
# whoever asked for it as soon as it has been read.
#
# Every value is base64, and that is the whole of the format. It was %q, which
# is shell quoting - correct only if whatever reads it puts the value back
# through a shell, and the reader deliberately does not. base64 closes the
# alphabet instead: what comes out is [A-Za-z0-9+/=] whatever went in, so the
# reader can check the shape of a line before it decodes it and never has to
# hand any of it to an interpreter. A password with a quote, a space or a
# newline in it survives the trip, which under %q it only did if the reader
# unquoted it exactly the way bash would have.
report_put() {  # key value
    printf '%s=%s\n' "$1" "$(printf '%s' "$2" | base64 | tr -d '\n')"
}
if [[ -n "${AWG_PANEL_REPORT:-}" ]]; then
    # In a subshell, so the tighter umask does not follow the script into the
    # files it installs afterwards.
    ( umask 077
    {
        report_put PANEL_REPORT_USER      "$ADMIN_USER"
        report_put PANEL_REPORT_PASS      "$ADMIN_PASS"
        report_put PANEL_REPORT_CREATED   "$ADMIN_CREATED"
        report_put PANEL_REPORT_RESET     "$ADMIN_RESET_DONE"
        report_put PANEL_REPORT_BASE_PATH "$BASE_PATH"
        report_put PANEL_REPORT_PORT      "$PORT"
        report_put PANEL_REPORT_LISTEN    "$LISTEN"
    } > "$AWG_PANEL_REPORT" )
fi
unset GENERATED_PASS SEED_OUT AWG_PANEL_ADMIN_PASSWORD

# --------------------------------------------------------------- units
step "$(t "Installing the services" "Установка служб systemd")"
install -m 644 "$PREFIX/deploy/awg-panel-web.service"       "$UNIT_DIR/"
install -m 644 "$PREFIX/deploy/awg-panel-collector.service" "$UNIT_DIR/"
if [[ -f "$SCRIPT_DIR/../bin/awg-panel" ]]; then
    install -m 755 "$SCRIPT_DIR/../bin/awg-panel" /usr/local/bin/awg-panel
elif [[ -f "$PREFIX/bin/awg-panel" ]]; then
    install -m 755 "$PREFIX/bin/awg-panel" /usr/local/bin/awg-panel
else
    warn "$(t "bin/awg-panel not found; awg-menu will not see the panel" \
              "bin/awg-panel не найден; awg-menu не увидит панель")"
fi
# The updater too, so a panel installed by this script on its own can still
# update itself. ../install.sh installs the same file; doing it here as well is
# what makes the standalone path - a server that already ran AmneziaWG and had
# the panel added to it - not the one installation that has to be upgraded by
# hand forever. Absent without complaint, unlike the line above: the panel works
# perfectly well without it, and the Update tab says why the button is missing.
if [[ -f "$SCRIPT_DIR/../bin/awg-update" ]]; then
    install -m 755 "$SCRIPT_DIR/../bin/awg-update" /usr/local/bin/awg-update
fi
systemctl daemon-reload
systemctl enable awg-panel-web awg-panel-collector >/dev/null 2>&1
systemctl restart awg-panel-collector awg-panel-web
echo "$(t "  awg-panel-web and awg-panel-collector enabled and started" \
          "  службы awg-panel-web и awg-panel-collector включены и запущены")"

# ------------------------------------------------------------ firewall
# The panel speaks TCP; the tunnel's UDP rule is a separate thing.
FW=""
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
    ufw allow "${PORT}/tcp" >/dev/null 2>&1 && FW=ufw
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd -q --permanent --add-port="${PORT}/tcp" 2>/dev/null \
        && firewall-cmd -q --reload 2>/dev/null && FW=firewalld
fi

# -------------------------------------------------------------- summary
sleep 2
if ! systemctl is-active --quiet awg-panel-web; then
    warn "$(t "the web service did not stay up; see: journalctl -u awg-panel-web -n 40" \
              "веб-служба не удержалась; смотрите: journalctl -u awg-panel-web -n 40")"
fi
# Everything below is reporting; the install itself is done and must not be
# rolled back by a failure in it.
trap - EXIT
rm -rf "${VENV}.prev" "$PREV" 2>/dev/null || true

# Run from install.sh, which ends with one summary carrying every line of this
# one: the URL, the sign-in, the firewall rule and the state of TLS. It has the
# credentials already, from the report file written above. Printing the block
# here as well put all of it on the screen twice, several steps apart, and the
# copy that scrolls away is this one - so under install.sh this step reports
# what it did to the firewall, like every other step does, and leaves the rest
# to the end.
if [[ -n "${AWG_PANEL_REPORT:-}" ]]; then
    [[ -n "$FW" ]] && echo "$(t "  ${FW}: allowed ${PORT}/tcp" "  ${FW}: разрешён ${PORT}/tcp")"
    echo "$(t "  panel installed - its URL and sign-in are in the summary at the end" \
              "  панель установлена — её адрес и данные для входа в итоге в конце")"
    exit 0
fi

# Only reached when this script was run on its own, which is a documented way
# to install - a server that already has the tunnel and wants the panel put on
# it. --lang is answered here as much as anywhere else, and the block below is
# the whole reason somebody reads this screen: the address and the one showing
# of the password. It was the last thing left in English.
#
# The label column is written out per language rather than padded at runtime.
# install.sh needs pad() because its summary is assembled from rows built in
# six different places and they have to agree on one width; every row here is
# a single literal, so the alignment is a property of the text, and holds the
# way the English column already did.
URL=$(/usr/local/bin/awg-panel url 2>/dev/null || echo "http://<server>:${PORT}${BASE_PATH}")
cat <<EOF

${B}----------------------- AWG Panel -----------------------${N}

$(t "  URL         ${B}${URL}${N}" \
    "  URL               ${B}${URL}${N}")
EOF
if (( ADMIN_CREATED || ADMIN_RESET_DONE )) && (( ADMIN_PASS_GIVEN )); then
cat <<EOF
$(t "  username    ${B}${ADMIN_USER}${N}" \
    "  имя пользователя  ${B}${ADMIN_USER}${N}")
$(t "  password    the one you chose during setup" \
    "  пароль            тот, что вы задали при установке")

$(t "  ${Y}Write the username down - it is not \"admin\" unless you asked for it.${N}" \
    "  ${Y}Запишите имя пользователя: оно не \"admin\", если вы сами так не назвали.${N}")
$(t "  Reset the password later with: sudo awg-panel passwd" \
    "  Сбросить пароль потом: sudo awg-panel passwd")
EOF
elif (( ADMIN_CREATED || ADMIN_RESET_DONE )) && [[ -n "$ADMIN_PASS" ]]; then
cat <<EOF
$(t "  username    ${B}${ADMIN_USER}${N}" \
    "  имя пользователя  ${B}${ADMIN_USER}${N}")
$(t "  password    ${B}${ADMIN_PASS}${N}" \
    "  пароль            ${B}${ADMIN_PASS}${N}")

$(t "  ${Y}Write both down now - the username is not \"admin\" unless you asked
  for it, and the password is kept only as a hash, so this is the only
  time either of them is shown.${N}" \
     "  ${Y}Запишите оба сейчас: имя пользователя не \"admin\", если вы сами так
  не назвали, а пароль хранится только в виде хеша, так что это
  единственный раз, когда он показан.${N}")
$(t "  Change the password later with: sudo awg-panel passwd" \
    "  Сменить пароль потом: sudo awg-panel passwd")
EOF
elif (( ADMIN_RESET_DONE )); then
cat <<EOF
$(t "  username    ${B}${ADMIN_USER}${N}" \
    "  имя пользователя  ${B}${ADMIN_USER}${N}")
$(t "  password    unchanged (sudo awg-panel passwd resets it)" \
     "  пароль            без изменений (сбрасывается через sudo awg-panel passwd)")
EOF
else
cat <<EOF
$(t "  accounts    unchanged (sudo awg-panel passwd resets the password)" \
     "  учётные записи    без изменений (пароль сбрасывается через sudo awg-panel passwd)")
EOF
fi
cat <<EOF

$(t "  manage      sudo awg-panel status | logs | restart" \
    "  управление        sudo awg-panel status | logs | restart")
$(t "  data        ${DATA_DIR}   (included in the panel's backups)" \
     "  данные            ${DATA_DIR}   (входят в резервные копии панели)")

EOF
# Through %s, like the certbot messages in lib/acme.sh: a translated string is
# not a literal the author can eyeball, and a stray % in one would be read as
# a conversion against arguments that are not there.
if [[ -n "$FW" ]]; then
    printf '%s\n' "$(t "  ${FW} now allows TCP ${PORT}. A cloud security group still needs" \
                       "  ${FW} теперь пропускает TCP ${PORT}. Облачную security group всё")"
    printf '%s\n\n' "$(t "  that rule added by hand." \
                         "  равно придётся открыть руками.")"
else
    printf '%s\n\n' "$(t "  ${Y}FIREWALL:${N} allow inbound TCP ${PORT}, or the panel is unreachable." \
                         "  ${Y}ФАЕРВОЛ:${N} разрешите входящий TCP ${PORT}, иначе панель недоступна.")"
fi
printf '%s\n' "$(t "  ${Y}Serve it over TLS or behind a reverse proxy before exposing it" \
                   "  ${Y}Прежде чем открывать её в интернет, поставьте её за TLS или за")"
printf '%s\n\n' "$(t "  to the internet - see docs/PANEL.md.${N}" \
                     "  обратный прокси — см. docs/PANEL.md.${N}")"
