#!/bin/bash
#
# lock-deps.sh - regenerate the hash-pinned Python dependency locks.
#
# The ranges in requirements.txt and pyproject.toml say what the panel is
# willing to run against. The .lock files beside them say what it actually
# runs, down to the exact version and the sha256 of every file pip downloads,
# transitive dependencies included. Servers install from the locks, never from
# the ranges, so two boxes installed six months apart get the same code.
#
# That means a dependency only ever moves when someone runs this and commits
# the result. Do it deliberately, then run the test suite before pushing:
#
#     ./lock-deps.sh          # or: make lock
#     make test
#
# Nothing here runs on a server or in the install path. It needs network
# access and it rewrites tracked files.
#
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

# The resolver is part of the output: a different uv can pick a different
# valid solution from the same ranges, and the diff would then be noise
# nobody can account for. Bump this on purpose, like any other dependency.
UV_VERSION=0.12.2

# The oldest interpreter install-panel.sh accepts. Resolving for it keeps the
# lock installable there; --universal keeps it installable on a maintainer's
# laptop too, by carrying the markers for the other platforms rather than
# silently resolving for this one.
PYTHON_FLOOR=3.10

# uv from PATH when it is the pinned version, otherwise a private venv that
# holds it. Not the panel's own .venv: the tool that resolves the panel's
# dependencies has no business living among them.
LOCKGEN=.lockgen
if [[ "$(uv --version 2>/dev/null)" == "uv $UV_VERSION"* ]]; then
    UV=uv
else
    if [[ ! -x "$LOCKGEN/bin/uv" ]] \
       || [[ "$("$LOCKGEN/bin/uv" --version 2>/dev/null)" != "uv $UV_VERSION"* ]]; then
        echo "==> installing uv ${UV_VERSION} into ${LOCKGEN}"
        rm -rf "$LOCKGEN"
        python3 -m venv "$LOCKGEN"
        "$LOCKGEN/bin/python" -m pip install --quiet --upgrade pip
        "$LOCKGEN/bin/python" -m pip install --quiet "uv==${UV_VERSION}"
    fi
    UV="$LOCKGEN/bin/uv"
fi

compile() {
    local source="$1" out="$2"; shift 2
    "$UV" pip compile "$source" \
        --generate-hashes --universal --python-version "$PYTHON_FLOOR" \
        --no-header --quiet --output-file "$out" "$@"
    echo "  ${out}  ($(grep -cE '^[a-zA-Z0-9]' "$out") packages)"
}

echo "==> runtime"
compile requirements.txt requirements.lock

# Constrained by the runtime lock so the two can never name different versions
# of the same package: CI installs both, and a test suite running against a
# Django the server will not get is a test suite proving nothing. This is a
# superset - one --require-hashes install of it gives CI everything.
echo "==> development (runtime + test and lint tools)"
compile pyproject.toml requirements-dev.lock --extra dev --constraint requirements.lock

# pip installs the locks, so pip is itself a dependency that would otherwise
# float. The venv's bundled pip is whatever the distro froze - 22.0.2 on
# Ubuntu 22.04 - which predates the wheel metadata some of these packages now
# ship, so it is upgraded rather than merely tolerated.
echo "==> bootstrap (pip and wheel, installed before the locks)"
printf 'pip\nwheel\n' > "$LOCKGEN.in"
compile "$LOCKGEN.in" requirements-bootstrap.lock --no-annotate
rm -f "$LOCKGEN.in"

echo
echo "Locks rewritten. Run 'make test' before committing them."
