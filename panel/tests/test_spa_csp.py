"""The shell has to survive its own Content-Security-Policy.

Two scripts in the served page must be inline: the bootstrap object is built
per request, and the theme switch has to run before the first paint or every
dark-mode reload flashes white. Under a bare `script-src 'self'` a browser
drops both without a console error the user would think to look for, and the
SPA then comes up not knowing its base path and 404s on every API call. A curl
check cannot see this, so it is pinned here instead.
"""

import re

import pytest
from django.conf import settings

pytestmark = pytest.mark.django_db

_SCRIPT_OPEN = re.compile(r"<script([^>]*)>", re.IGNORECASE)


def _index(client):
    response = client.get(settings.BASE_PATH)
    assert response.status_code == 200, "the SPA shell must render"
    return response, response.content.decode()


def _nonce_from(response) -> str:
    csp = response.headers["Content-Security-Policy"]
    match = re.search(r"script-src [^;]*'nonce-([^']+)'", csp)
    assert match, f"no script nonce in the policy: {csp}"
    return match.group(1)


@pytest.mark.skipif(
    not (settings.FRONTEND_DIST / "index.html").is_file(),
    reason="frontend/dist is not built in this checkout",
)
class TestServedShell:
    def test_every_inline_script_carries_the_header_nonce(self, client):
        response, html = _index(client)
        nonce = _nonce_from(response)
        inline = [
            attrs
            for attrs in _SCRIPT_OPEN.findall(html)
            if not re.search(r"\ssrc\s*=", attrs, re.IGNORECASE)
        ]
        assert inline, "the bootstrap script should be in the page"
        for attrs in inline:
            assert f'nonce="{nonce}"' in attrs, (
                f"inline <script{attrs}> would be blocked by the policy"
            )

    def test_scripts_loaded_by_src_are_left_alone(self, client):
        """They are covered by 'self'; rewriting them would only risk mangling
        the build's own attributes."""
        _, html = _index(client)
        for attrs in _SCRIPT_OPEN.findall(html):
            if re.search(r"\ssrc\s*=", attrs, re.IGNORECASE):
                assert "nonce=" not in attrs

    def test_the_bootstrap_object_is_present(self, client):
        _, html = _index(client)
        assert "window.__AWG__={" in html.replace(" ", "")
        assert settings.BASE_PATH in html

    def test_the_nonce_is_fresh_on_every_response(self, client):
        first, _ = _index(client)
        second, _ = _index(client)
        assert _nonce_from(first) != _nonce_from(second)

    def test_the_shell_is_never_cached(self, client):
        """A cached shell would carry a stale nonce and a stale base path."""
        response, _ = _index(client)
        assert "no-store" in response.headers.get("Cache-Control", "")


def test_api_responses_get_no_script_nonce(client):
    """Only the HTML shell writes inline scripts; handing a nonce to anything
    else would widen the policy for no reason."""
    response = client.get(f"{settings.BASE_PATH}api/v1/health")
    assert response.status_code == 200
    assert "nonce-" not in response.headers.get("Content-Security-Policy", "")


def test_policy_still_forbids_arbitrary_inline_script(client):
    """The nonce must widen the policy, not disable it: 'unsafe-inline' would
    let any injected <script> run."""
    response = client.get(f"{settings.BASE_PATH}api/v1/health")
    csp = response.headers["Content-Security-Policy"]
    script_src = next(part for part in csp.split(";") if "script-src" in part)
    assert "'unsafe-inline'" not in script_src
    assert "frame-ancestors 'none'" in csp
