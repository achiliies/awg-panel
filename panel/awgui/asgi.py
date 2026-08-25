"""ASGI entry point.

Nothing the panel ships uses it - the web unit runs gunicorn's sync worker over
WSGI, because the workload is one admin polling a 2 second endpoint and an
occasional `awg-quick` that blocks for seconds. It is here so an operator who
prefers uvicorn or hypercorn has a supported target to point at.
"""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _load_panel_env() -> None:
    try:
        from awg.paths import panel_env_file
        from manage import load_env_file
    except ImportError:
        return
    load_env_file(panel_env_file())


_load_panel_env()
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "awgui.settings")

from django.core.asgi import get_asgi_application  # noqa: E402

application = get_asgi_application()
