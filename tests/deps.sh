#!/bin/bash
#
# tests/deps.sh - prove the dependency pins still say what they claim to.
#
# The panel declares its dependencies as ranges in two places and installs
# them from lock files in a third. That arrangement is only worth anything
# while the three agree, and every way they can stop agreeing is quiet: a
# range widened in pyproject.toml but not requirements.txt, a lock left
# unregenerated after either, an entry that lost its hashes in a merge, a
# development lock that pins a different Django than the servers get.
#
# So this checks the properties, not the resolution. It deliberately does NOT
# re-resolve against PyPI: a check that did would go red the morning upstream
# published anything, which is the same "green today, red tomorrow, no commits
# in between" the locks exist to end. Whether to take a newer version is a
# decision someone makes by running panel/lock-deps.sh, not one CI makes by
# noticing.
#
# Usage: tests/deps.sh        (exit 0 = the pins are intact)

set -uo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

PY="$REPO/panel/.venv/bin/python"
[[ -x "$PY" ]] || PY=$(command -v python3)
if ! "$PY" -c 'import packaging' 2>/dev/null; then
    echo "skip: packaging is not importable (make -C panel venv)" >&2
    exit 0
fi

"$PY" "$REPO/tests/deps_check.py" "$REPO/panel"
