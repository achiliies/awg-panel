"""Session-wide test bootstrap: where the suite is allowed to read and write.

The panel's real home is /etc/amnezia/amneziawg and /var/lib/awg-panel. On the
machine most likely to run this suite - a server that already has the VPN on it -
those are a live config and a live database, so every AWG_* variable is
overwritten here rather than merely defaulted: a value exported in the operator's
shell must not be able to point a test at the interface their users are on.

tests/conftest.py narrows this further, giving each individual test its own
tmp_path. This file exists for the window that fixture cannot reach: pytest-django
imports awgui.settings from pytest_load_initial_conftests, which runs before any
conftest module is loaded, so a handful of settings are already frozen from the
pre-test environment by the time this file is read. pytest_configure below puts
those back in step with the environment established here.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent

# pytest-django finds manage.py and does this too, but only when the arguments
# point inside the project. `pytest panel/tests/test_conf.py` from the repo root
# is a normal thing to type and has to import awg/ and awgui/ just the same.
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from awg import paths, ports  # noqa: E402  (needs BASE_DIR on sys.path first)


@pytest.fixture(autouse=True)
def default_ports_free(monkeypatch, request):
    """Keep the test suite host-independent by treating all ports as free by default.

    Without this, port-conflict detection queries the live machine running the
    test suite, making test results depend on whatever services happen to be
    listening on the test runner. Tests simulating an occupied port override this
    fixture by patching awg.ports.busy themselves. test_ports.py is the port
    probe's own verification against live sockets and is left unmocked.
    """
    if getattr(request, "module", None) and "test_ports" in request.module.__name__:
        return
    monkeypatch.setattr(ports, "busy", lambda port, proto="udp": False)


# One directory for the whole session, not per test: the fixtures in
# tests/conftest.py own the per-test isolation, and what is wanted here is
# somewhere harmless for anything that escapes them.
_SESSION_DIR = Path(tempfile.mkdtemp(prefix="awg-panel-pytest-"))

_CONF_DIR = _SESSION_DIR / "amneziawg"
_DATA_DIR = _SESSION_DIR / "awg-panel"

for _directory in (_CONF_DIR, _CONF_DIR / "clients", _DATA_DIR, _SESSION_DIR / "run"):
    _directory.mkdir(parents=True, mode=0o700, exist_ok=True)

os.environ[paths.ENV_IFACE] = "awg0"
os.environ[paths.ENV_CONF_DIR] = str(_CONF_DIR)
os.environ[paths.ENV_DATA_DIR] = str(_DATA_DIR)
os.environ[paths.ENV_RUN_DIR] = str(_SESSION_DIR / "run")
# Saving panel settings rewrites this file. Left at its default it would be
# /etc/awg-panel.env, which is the running web service's configuration.
os.environ[paths.ENV_PANEL_ENV] = str(_SESSION_DIR / "awg-panel.env")
# No kernel module and no awg binary in CI or on a laptop, so the mock
# controller is the only thing the suite can be honest about.
os.environ["AWG_MOCK"] = "1"

# pyproject.toml already sets this through pytest-django's ini option; the
# fallback is for `pytest` run against a different ini, or none at all.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "awgui.settings")


def pytest_configure(config: pytest.Config) -> None:
    """Re-point the settings that awgui.settings resolved before this file ran.

    Both are read at import time by design, because in production they come from
    the systemd env file and never change while the process lives. Under pytest
    that import has already happened, so without this the session payload would
    report mock=false while every controller call went to the mock, and
    settings.DATA_DIR would name a directory awg.paths no longer agrees with.
    """
    from django.conf import settings

    settings.PANEL_MOCK = True
    settings.DATA_DIR = _DATA_DIR


def pytest_unconfigure(config: pytest.Config) -> None:
    shutil.rmtree(_SESSION_DIR, ignore_errors=True)
