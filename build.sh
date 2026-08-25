#!/bin/bash
#
# build.sh - pack the repo into a single self-contained installer at
# dist/awg-panel.sh, for one-file distribution (scp, curl | bash).
#
# The bundle is self-extracting: it unpacks install.sh, lib/ and bin/ into
# a temp dir and runs the installer from there, so the bundled and checked-
# out versions always behave identically.
#
# With --with-panel the web panel is bundled too and installed by default.
# The frontend is not built here: ship panel/frontend/dist prebuilt (make -C
# panel build, or the release artifact) so target servers never need Node.
#
# Nothing is fetched here either. Run ./fetch-sources.sh for vendor/ and
# 'make -C panel wheels' for panel/wheels first, and the bundle carries every
# byte it installs; skip them and it still builds, but the server it runs on
# has to reach github.com and PyPI for the parts that are missing.
#
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

OUT=dist/awg-panel.sh
WITH_PANEL=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-panel) WITH_PANEL=1; shift ;;
        -o|--output)  OUT="${2:?}";  shift 2 ;;
        -h|--help)
            cat <<'USAGE'
build.sh - pack the repo into a single self-contained installer

  --with-panel   bundle the web panel as well, and install it by default
  -o, --output F write the bundle somewhere other than dist/
USAGE
            exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

mkdir -p "$(dirname "$OUT")"

FILES=(install.sh lib bin/awg-menu bin/awg-uninstall bin/awg-panel bin/awg-update)
TAR_OPTS=()

# Both bundles, not just the panel one: the module is what the VPN-only
# installer exists to build, so it is the bundle that most needs to stop
# depending on github.com being reachable. Optional the same way the wheels
# are - without it install.sh clones, which still works wherever cloning does.
if [[ -d vendor ]]; then
    FILES+=(vendor)
    TAR_OPTS+=(--exclude=vendor/.git)
else
    cat >&2 <<'WARN'
warning: vendor/ is missing, so the bundle carries no upstream sources and the
         target server will clone them from github.com. Run
         './fetch-sources.sh' first for an installer that needs no network
         beyond itself.
WARN
fi

if (( WITH_PANEL )); then
    [[ -d panel ]] || { echo "no panel/ directory in this checkout" >&2; exit 1; }
    [[ -d panel/frontend/dist ]] || cat >&2 <<'WARN'
warning: panel/frontend/dist is missing, so the bundle carries no prebuilt UI.
         The target server will have to build it with Node, or the install
         will stop and say so. Run 'make -C panel build' first.
WARN
    # Included when present, which is what makes a bundle installable with no
    # index at all. Optional: without them the install still gets exactly the
    # locked versions, just from PyPI, so this only ever changes where the
    # bytes come from.
    [[ -d panel/wheels ]] || cat >&2 <<'WARN'
warning: panel/wheels is missing, so the bundle carries no Python wheels and
         the target server will download them from PyPI. Run
         'make -C panel wheels' first for an installer that needs no network
         beyond itself.
WARN
    FILES+=(panel)
    # Everything that is generated, huge, or machine-specific.
    TAR_OPTS+=(--exclude=panel/.venv --exclude=panel/node_modules
               --exclude=panel/frontend/node_modules --exclude='*/__pycache__'
               --exclude='*.pyc' --exclude=panel/.pytest_cache
               --exclude=panel/.ruff_cache --exclude=panel/staticfiles
               --exclude=panel/.lockgen --exclude=panel/.dev
               --exclude='*.sqlite3')
fi

# The archive is appended to the generated script rather than embedded in it.
# It used to be one base64 string assigned to a shell variable, and that costs
# memory in a way nothing about it suggests: bash reads the assignment into its
# parser as a single line, keeps the value, and copies it again for every
# expansion of it. A 46 MB payload measured 459 MB of resident memory that way,
# before the installer had printed a word. On the 1 GB VPS this is aimed at -
# more so on an upgrade, where the panel being upgraded is running and holding
# a few hundred MB of its own - that is most of the machine, and a machine that
# runs out does not answer with an error: the OOM killer, or systemd-oomd,
# takes the whole login session, and the terminal the install was started in
# disappears. Streamed off the end of the file instead, the payload never has
# to be in memory at all.
MARKER=__AWG_PAYLOAD__

# For the message a piped bundle prints, which is the one place the generated
# script has to name a URL. Read from release.py rather than repeated here,
# because that file is the single place this repository's name is written down
# and a fork that changed it there would otherwise ship a bundle pointing
# people at upstream.
REPO=$( { sed -n 's/^DEFAULT_REPO = "\(.*\)"$/\1/p' panel/awg/release.py 2>/dev/null || true; } | head -1)
REPO=${REPO:-achiliies/awg-panel}

{
    printf '#!/bin/bash\n'
    printf '# Self-contained AWG Panel installer. Generated by build.sh - do not edit.\n'
    printf '# Built %s. Unpacks install.sh + bin/%s and runs the installer.\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$( (( WITH_PANEL )) && printf ' + panel/')"
    printf 'set -euo pipefail\n'
    printf 'WITH_PANEL=%d\n' "$WITH_PANEL"
    printf 'MARKER=%s\n' "$MARKER"
    printf 'GET_URL=https://github.com/%s/releases/latest/download/get.sh\n' "$REPO"
    cat <<'EOF'
# Everything past the marker line at the end of this file is a tar.gz. It is
# read from the file and piped into tar, so what it costs is tar's memory and
# not the archive's size.

# Unset, not merely wrong, when bash is reading this from a pipe rather than
# opening it - so the default is what `curl ... | bash` lands on, and it has
# to reach the message below rather than trip over `set -u` on the way.
SELF="${BASH_SOURCE[0]:-}"
[[ -n "$SELF" && -f "$SELF" ]] || {
    echo "error: this installer carries its payload in its own file, so it has to be" >&2
    echo "       run from one rather than piped. Either download it and run the file:" >&2
    echo >&2
    echo "         sudo bash <the file>" >&2
    echo >&2
    echo "       or pipe get.sh, which downloads this, checks it against the" >&2
    echo "       published SHA256SUMS and runs it:" >&2
    echo >&2
    echo "         curl -fsSL $GET_URL | sudo bash" >&2
    echo >&2
    exit 1; }

# Which line the archive starts on. awk stops at the marker a few lines below
# this and never reads the archive itself.
START=$(awk -v m="$MARKER" '$0 == m { print NR + 1; exit }' "$SELF")
[[ -n "$START" ]] || {
    echo "error: no payload in this file - it was truncated in transit. Download it" >&2
    echo "       again, and check it against SHA256SUMS." >&2
    exit 1; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
tail -n +"$START" "$SELF" | tar -xz -C "$tmp" || {
    echo "error: the payload in this file will not unpack; check it against SHA256SUMS" >&2
    exit 1; }
# A panel bundle installs the panel unless told otherwise; that is the whole
# reason someone built it that way.
if (( WITH_PANEL )) && [[ " $* " != *" --panel "* && " $* " != *" --no-panel "* ]]; then
    set -- --panel "$@"
fi
bash "$tmp/install.sh" "$@"
# Nothing below is shell. bash parses one command at a time and stops here, so
# the archive after the marker is never read as source - but that holds only
# because this exit is unconditional. Nothing may be added after it.
exit $?
EOF
    printf '%s\n' "$MARKER"
} > "$OUT"
tar -cz "${TAR_OPTS[@]}" "${FILES[@]}" >> "$OUT"
chmod 755 "$OUT"
echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"
