#!/usr/bin/env python3
"""Django's entry point, with /etc/awg-panel.env loaded first.

systemd hands both units that file through EnvironmentFile, so the services
always know which interface, config directory and data directory they manage.
An admin running `manage.py` over SSH gets none of it, and a panel that falls
back to the compiled-in defaults would quietly migrate a different database or
edit a different server config than the running services do. Loading the same
file here makes the two paths identical.

Variables already present in the environment always win, so
`AWG_CONF_DIR=/tmp/x manage.py ...` still overrides the file, which is what the
test suite and `make dev` rely on.
"""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def load_env_file(path: Path | str) -> int:
    """Merge simple KEY=VALUE lines from `path` into os.environ.

    Returns the number of variables set. A missing file is not an error: the
    panel is developed and tested on machines that have never been installed.
    Only the subset of shell syntax install-panel.sh writes is understood -
    comments, blank lines, an optional `export` prefix and optional surrounding
    quotes - matching what systemd's EnvironmentFile parser accepts.
    """
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return 0
    except OSError as exc:
        # Almost always a non-root shell reading a 0600 file. Say so instead of
        # silently running against the wrong paths.
        print(f"warning: cannot read {target}: {exc}", file=sys.stderr)
        return 0

    count = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in os.environ:
            continue
        os.environ[key] = value
        count += 1
    return count


def main() -> None:
    # Running `python /opt/awg-panel/manage.py` from another directory still has
    # to import awg and awgui from beside this file.
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))

    from awg.paths import panel_env_file

    load_env_file(panel_env_file())
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "awgui.settings")

    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Django is not importable. Run this through the panel's virtualenv "
            "(/opt/awg-panel/.venv/bin/python manage.py ...), or create one with "
            "'make venv' in a checkout."
        ) from exc

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
