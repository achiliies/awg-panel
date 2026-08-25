"""gunicorn and Django read the env file's booleans the same way.

They did not. `deploy/gunicorn.conf.py` tested `AWG_PANEL_TLS == "1"` and
`awgui.settings` accepted `1`, `true`, `yes` and `on`, so `AWG_PANEL_TLS=true`
in `/etc/awg-panel.env` - a value `docs/PANEL.md` shows an operator writing by
hand - meant plain HTTP to the server and HTTPS to the application. The panel
then redirected every read to an https:// port with no TLS behind it and
answered every write with a 400.

Nothing about that is visible to a test that only imports settings, because the
disagreement needs both readers. So this drives both: `env_flag` directly, and
`deploy/gunicorn.conf.py` by executing it the way gunicorn does.
"""

import pytest

from awgui.envflags import env_flag
from awgui.settings import _env_flag

# Every spelling both sides now accept, and what it means. Kept as one list so
# that adding a spelling to the parser without adding it here is a test that
# fails rather than a divergence that ships.
TRUE_VALUES = ["1", "true", "yes", "on", "TRUE", "Yes", " on "]
FALSE_VALUES = ["0", "false", "no", "off", "FALSE", "No", " off "]
# Absent, or present and blank: a half-written env file, which reads as the
# default rather than as an instruction.
DEFAULTED_VALUES = ["", "   "]


@pytest.mark.parametrize("value", TRUE_VALUES)
def test_true_spellings_are_true(monkeypatch, value):
    monkeypatch.setenv("AWG_TEST_FLAG", value)
    assert env_flag("AWG_TEST_FLAG") is True
    assert _env_flag("AWG_TEST_FLAG") is True


@pytest.mark.parametrize("value", FALSE_VALUES)
def test_false_spellings_are_false(monkeypatch, value):
    monkeypatch.setenv("AWG_TEST_FLAG", value)
    assert env_flag("AWG_TEST_FLAG", default=True) is False
    assert _env_flag("AWG_TEST_FLAG", default=True) is False


@pytest.mark.parametrize("value", DEFAULTED_VALUES)
def test_blank_reads_as_the_default(monkeypatch, value):
    monkeypatch.setenv("AWG_TEST_FLAG", value)
    assert env_flag("AWG_TEST_FLAG", default=True) is True
    assert env_flag("AWG_TEST_FLAG", default=False) is False


def test_unset_reads_as_the_default(monkeypatch):
    monkeypatch.delenv("AWG_TEST_FLAG", raising=False)
    assert env_flag("AWG_TEST_FLAG", default=True) is True
    assert env_flag("AWG_TEST_FLAG", default=False) is False


def test_a_value_that_is_neither_reads_as_the_default(monkeypatch):
    """For a flag whose worst case is a feature that stays off."""
    monkeypatch.setenv("AWG_TEST_FLAG", "maybe")
    assert env_flag("AWG_TEST_FLAG") is False


def test_a_value_that_is_neither_is_refused_when_strict(monkeypatch):
    """For AWG_PANEL_TLS, where guessing is what put the password on the wire.

    The message has to name the key: this is read out of a journal by somebody
    whose panel will not start, and "invalid value" would send them looking
    through every line of the env file.
    """
    monkeypatch.setenv("AWG_TEST_FLAG", "maybe")
    with pytest.raises(ValueError, match="AWG_TEST_FLAG"):
        env_flag("AWG_TEST_FLAG", strict=True)


@pytest.mark.parametrize("value", [*TRUE_VALUES, *FALSE_VALUES, *DEFAULTED_VALUES])
def test_gunicorn_and_django_agree_about_tls(monkeypatch, value):
    """The same AWG_PANEL_TLS decides the same thing in both processes.

    gunicorn's answer is read off the `certfile` its config sets, because that
    - not any variable - is what makes the socket speak TLS. Django's is
    PANEL_TLS, which decides the Secure flag on both cookies, HSTS, and whether
    HttpsOnlyMiddleware refuses cleartext. The two disagreeing is the bug.
    """
    from pathlib import Path

    from django.conf import settings

    conf = Path(settings.BASE_DIR) / "deploy" / "gunicorn.conf.py"
    if not conf.is_file():
        pytest.skip("deploy/gunicorn.conf.py is not present")

    monkeypatch.setenv("AWG_PANEL_TLS", value)
    monkeypatch.setenv("AWG_PANEL_TLS_CERT", "/etc/ssl/awg-panel/panel.pem")
    monkeypatch.setenv("AWG_PANEL_TLS_KEY", "/etc/ssl/awg-panel/panel.key")

    namespace: dict[str, object] = {}
    exec(compile(conf.read_text(encoding="utf-8"), str(conf), "exec"), namespace)  # noqa: S102

    gunicorn_serves_tls = bool(namespace.get("certfile"))
    django_believes_tls = _env_flag("AWG_PANEL_TLS", strict=True)
    assert gunicorn_serves_tls == django_believes_tls
