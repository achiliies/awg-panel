"""How a boolean in /etc/awg-panel.env is read, for everything that reads one.

Two processes decide things from that file and they are not the same program.
Django reads it through `awgui.settings`; gunicorn reads it through
`deploy/gunicorn.conf.py`, which runs before Django exists and cannot import
settings without dragging the whole application in. So each of them used to
parse the same keys on its own, and they disagreed: gunicorn tested
`AWG_PANEL_TLS == "1"` while settings accepted `1`, `true`, `yes` and `on`.

`AWG_PANEL_TLS=true` - which nothing in this repository writes, but which
`docs/PANEL.md` invites an operator to `sed` into place by hand - therefore had
gunicorn listening in the clear while Django believed the panel was encrypted.
HttpsOnlyMiddleware then bounces every readable request to the https:// URL of
a port that speaks no TLS, and refuses every write with a 400, so the panel is
unreachable in a way that reads as a browser or proxy fault. The cookies are
marked `Secure` on top of that, and a browser drops one that arrives over
`http://`, so even reaching the login form would not have signed anybody in.

One parser fixes that by construction rather than by two lists being kept in
step. It deliberately has no imports beyond the standard library and no
knowledge of Django, because the whole point is that gunicorn can use it at
config-load time.
"""

import os

# Both spellings of both answers, plus the ones an operator reaches for. The
# sets are exhaustive on purpose: a value in neither is a typo, and `strict`
# below exists so the one flag where guessing is dangerous can say so.
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def env_flag(name: str, default: bool = False, *, strict: bool = False) -> bool:
    """Read `name` as a boolean.

    Unset, or set to nothing but whitespace, reads as `default`: a key present
    and blank is a half-written env file, not an instruction.

    A value that is neither true nor false reads as `default` too, which is
    what every caller here wants for a flag whose worst case is a feature that
    stays off. `strict=True` raises `ValueError` instead, for the caller whose
    worst case is the panel and the server disagreeing about whether the
    connection is encrypted.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    if strict:
        accepted = ", ".join(sorted(_TRUE | _FALSE))
        raise ValueError(
            f"{name}={raw!r} in /etc/awg-panel.env is neither true nor false. "
            f"Use one of: {accepted}."
        )
    return default
