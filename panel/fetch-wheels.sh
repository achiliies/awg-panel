#!/bin/bash
#
# fetch-wheels.sh - download every wheel requirements.lock names, for every
# interpreter and architecture a server might have, into panel/wheels/.
#
# build.sh packs that directory into the with-panel bundle and install-panel.sh
# installs from it with --no-index, so a server needs no contact with PyPI at
# all: the bytes it runs are the bytes in the release it downloaded. Installs
# then work on an air-gapped box, survive a PyPI outage, and cannot be affected
# by anything that happens to an index between the release and the install.
#
#     ./fetch-wheels.sh          # or: make wheels
#
# Deliberately not committed - it is ~29 MB of binaries that release.yml
# rebuilds from the lock on every tag. Re-run it after ./lock-deps.sh.
#
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

DEST=${1:-wheels}
# Both, because install-panel.sh installs the pinned pip before it installs
# anything else. Leaving the bootstrap lock out would mean an install that
# still reached for PyPI once, which is the same as reaching for it always.
LOCKS=(requirements-bootstrap.lock requirements.lock)

# Everything install-panel.sh accepts (it refuses anything older than 3.10),
# which spans Ubuntu 22.04 through the current Debian, plus a version of
# headroom. A pure-Python wheel is downloaded once and covers all of them; only
# the compiled packages actually multiply.
PYTHON_VERSIONS=(3.10 3.11 3.12 3.13 3.14)

# The two architectures a VPS is ever sold as. The manylinux variants are all
# offered together and pip picks the best each package publishes - cryptography
# ships _2_34 down to _2_17, argon2-cffi-bindings only _2_26 and _2_28, so no
# single tag covers the set.
ARCHES=(x86_64 aarch64)
MANYLINUX=(manylinux2014 manylinux_2_17 manylinux_2_28 manylinux_2_34)

command -v python3 >/dev/null || { echo "python3 is not installed" >&2; exit 1; }
for lock in "${LOCKS[@]}"; do
    [[ -f "$lock" ]] || { echo "no ${lock}; run ./lock-deps.sh first" >&2; exit 1; }
done

# Package pins without their markers. Asking pip for each package by name is
# the whole trick: given a requirements file it evaluates markers against the
# interpreter it is *running on*, not the one --python-version names, so
# `typing-extensions ; python_full_version < '3.11'` was silently skipped and
# the 3.10 set would have been missing a package it cannot start without.
# --no-deps means nothing is resolved and nothing can be inferred wrongly; the
# lock already lists the complete closure, so the union of these is exact.
mapfile -t PINS < <(grep -hoE '^[A-Za-z0-9][A-Za-z0-9._-]*==[^ ;\]+' "${LOCKS[@]}" | sort -u)
(( ${#PINS[@]} )) || { echo "no pins found in ${LOCKS[*]}" >&2; exit 1; }

rm -rf "$DEST"
mkdir -p "$DEST"
echo "==> ${#PINS[@]} packages x ${#PYTHON_VERSIONS[@]} interpreters x ${#ARCHES[@]} architectures"

for pyver in "${PYTHON_VERSIONS[@]}"; do
    for arch in "${ARCHES[@]}"; do
        platform_args=()
        for tag in "${MANYLINUX[@]}"; do
            platform_args+=(--platform "${tag}_${arch}")
        done
        python3 -m pip download --quiet --no-deps --only-binary=:all: \
            --python-version "$pyver" "${platform_args[@]}" \
            --dest "$DEST" "${PINS[@]}" \
            || { echo "  FAILED: python ${pyver} on ${arch}" >&2; exit 1; }
        echo "  python ${pyver} / ${arch}"
    done
done

# Every file came from the lock, and the lock accounts for every file. pip
# checks hashes again at install time, but a corrupt or stray wheel should not
# get as far as a release artifact.
echo "==> verifying against ${LOCKS[*]}"
grep -hoE 'sha256:[0-9a-f]{64}' "${LOCKS[@]}" | cut -d: -f2 | sort -u > "$DEST/.known"
unknown=0
while read -r sum file; do
    grep -qxF "$sum" "$DEST/.known" || { echo "  NOT IN THE LOCK: ${file##*/}" >&2; unknown=1; }
done < <(find "$DEST" -name '*.whl' -exec sha256sum {} +)
rm -f "$DEST/.known"
(( unknown )) && { echo "refusing to ship wheels the lock does not name" >&2; exit 1; }

missing=0
for pin in "${PINS[@]}"; do
    # Wheel filenames normalise - and . in the package name to _.
    name=$(printf '%s' "${pin%%==*}" | tr '.-' '__')
    version=${pin##*==}
    compgen -G "$DEST/${name}-${version}-*.whl" >/dev/null \
        || { echo "  NO WHEEL: ${pin}" >&2; missing=1; }
done
(( missing )) && { echo "the wheel set does not cover the lock" >&2; exit 1; }

count=$(find "$DEST" -name '*.whl' | wc -l)
echo "  ${count} wheels, $(du -sh "$DEST" | cut -f1), every one named by the lock"
