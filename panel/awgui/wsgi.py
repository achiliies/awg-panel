"""WSGI entry point. gunicorn is started as `awgui.wsgi:application`.

systemd passes /etc/awg-panel.env through EnvironmentFile, so under the unit
the environment is already correct. It is loaded here as well for the cases
that are not the unit - a container started by hand, `gunicorn` run from a
shell to debug something - because a panel that silently falls back to the
compiled-in defaults would edit a different config directory than the CLI does.
"""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _load_panel_env() -> None:
    # manage.py owns the parser; duplicating it here is how the two entry
    # points end up disagreeing about quoting six months from now.
    try:
        from awg.paths import panel_env_file
        from manage import load_env_file
    except ImportError:
        return
    load_env_file(panel_env_file())


_load_panel_env()
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "awgui.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
