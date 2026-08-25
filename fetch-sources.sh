#!/bin/bash
#
# fetch-sources.sh - download the AmneziaWG kernel module and userspace tools
# at the commits install.sh pins, into vendor/.
#
# build.sh packs that directory into every bundle and install.sh builds from it
# instead of cloning, so an install needs nothing from github.com. The panel
# bundle already carries every Python wheel it needs; without this it would
# still stop three steps earlier fetching the module the whole thing exists to
# install, on exactly the networks least able to reach GitHub.
#
#     ./fetch-sources.sh
#
# Deliberately not committed - it is upstream's source, reproduced from the
# pins on every tag. Re-run it after changing KMOD_SHA / TOOLS_SHA.
#
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

DEST=${1:-vendor}

# install.sh is the one place the pins live. Reading them back rather than
# repeating them here is what stops a vendor/ that quietly disagrees with the
# installer it ships beside - the case install.sh refuses at run time.
pin() { sed -n "s/^$1=//p" install.sh | head -1; }
KMOD_REF=$(pin KMOD_REF); KMOD_SHA=$(pin KMOD_SHA)
TOOLS_REF=$(pin TOOLS_REF); TOOLS_SHA=$(pin TOOLS_SHA)

for v in KMOD_REF KMOD_SHA TOOLS_REF TOOLS_SHA; do
    [[ -n "${!v}" ]] || { echo "could not read $v from install.sh" >&2; exit 1; }
done

command -v git >/dev/null || { echo "git is not installed" >&2; exit 1; }

rm -rf "$DEST"
mkdir -p "$DEST"

# --depth 1 on the tag, then check what it resolved to. A shallow clone of a
# moved tag looks exactly like a shallow clone of the right one, so the commit
# is the only thing worth trusting; install.sh makes the same check when it
# clones for itself.
fetch() {
    local repo="$1" ref="$2" want="$3" got
    git -c advice.detachedHead=false clone --depth 1 -q --branch "$ref" \
        "https://github.com/amnezia-vpn/${repo}.git" "$DEST/$repo"
    got=$(git -C "$DEST/$repo" rev-parse HEAD)
    if [[ "$got" != "$want" ]]; then
        echo "  ${repo} ${ref} is ${got}, not the pinned ${want}" >&2
        echo "  the tag moved upstream: check the release, then re-pin install.sh" >&2
        exit 1
    fi
    # The history is not what gets built and would be the largest thing here.
    rm -rf "$DEST/$repo/.git"
    echo "  ${repo} ${ref} at ${got:0:12}"
}

echo "==> fetching the sources install.sh pins"
fetch amneziawg-linux-kernel-module "$KMOD_REF"  "$KMOD_SHA"
fetch amneziawg-tools               "$TOOLS_REF" "$TOOLS_SHA"

# What install.sh checks this directory against before it builds anything.
cat > "$DEST/PINNED" <<EOF
KMOD_REF=${KMOD_REF}
KMOD_SHA=${KMOD_SHA}
TOOLS_REF=${TOOLS_REF}
TOOLS_SHA=${TOOLS_SHA}
EOF

# The two files the build actually reaches for. A clone that succeeded but
# landed a layout this does not expect should fail here, not on a VPS.
for f in amneziawg-linux-kernel-module/src/version.h \
         amneziawg-linux-kernel-module/src/dkms.conf \
         amneziawg-tools/src/Makefile; do
    [[ -f "$DEST/$f" ]] || { echo "  MISSING: ${f}" >&2; exit 1; }
done

echo "  $(du -sh "$DEST" | cut -f1) in ${DEST}/, both commits verified"
