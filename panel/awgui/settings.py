"""Django settings for the AWG Panel.

Two processes share this module: the gunicorn web service and the collector.
They also share one SQLite file and one config directory, so anything decided
here has to be true for both.

Importing this module must never fail on a machine that has not been installed.
CI, `make dev` and `pytest` all import it as an unprivileged user with no
/var/lib/awg-panel and no /etc/awg-panel.env, and a settings module that raises
in that situation turns every failure into "cannot import awgui.settings",
which tells the operator nothing.
"""

import contextlib
import os
import secrets
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from django.db.backends.signals import connection_created

BASE_DIR = Path(__file__).resolve().parent.parent

# Running under gunicorn (`awgui.wsgi:application`) or pytest, `awgui` is
# importable, so its parent is already on the path and this is a no-op. It
# matters for `python -c "import awgui.settings"` from an odd cwd.
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from awg import paths  # noqa: E402  (needs BASE_DIR on sys.path first)
from awgui.envflags import env_flag  # noqa: E402


def _env_flag(name: str, default: bool = False, *, strict: bool = False) -> bool:
    """One boolean out of the env file, parsed the way gunicorn parses it too.

    Thin on purpose: the parser itself lives in awgui.envflags because
    deploy/gunicorn.conf.py reads the same keys before Django exists, and two
    copies of the accepted-value list is exactly how the two of them came to
    disagree about AWG_PANEL_TLS.
    """
    return env_flag(name, default, strict=strict)


def _env_list(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def normalise_base_path(raw: str | None) -> str:
    """Return a base path in "/x/y/" form: always one leading and one trailing slash.

    The installer generates a random path like ``/ab12cd34/`` and writes it to
    /etc/awg-panel.env; an operator editing that file by hand will sooner or
    later write ``ab12cd34`` or ``/ab12cd34``. All three have to mean the
    same thing, because the cookie paths, the URL map and the SPA's router
    basename are all derived from this one string and a mismatch between them
    logs everybody out.
    """
    segments = [seg for seg in (raw or "").strip().split("/") if seg]
    if not segments:
        return "/"
    return "/" + "/".join(segments) + "/"


def _resolve_dir(target: Path, env_var: str, suffix: str, warning: str) -> Path:
    """`target` if the panel can write there, else a substitute under /tmp.

    A read-only or missing /var/lib/awg-panel is normal off a server, and so is
    a /run that only root may create under. Falling back keeps `pytest` and
    `make dev` working; the fallback is exported back into the environment so
    `awg.paths` agrees with us and the collector does not write live.json
    somewhere the web process never looks.
    """
    try:
        paths.ensure_dir(target)
        if os.access(target, os.W_OK | os.X_OK):
            return target
    except OSError:
        pass

    fallback = Path(tempfile.gettempdir()) / f"awg-panel-{os.getuid()}{suffix}"
    fallback.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.environ[env_var] = str(fallback)
    print(f"warning: {target} is not writable; using {fallback}. {warning}", file=sys.stderr)
    return fallback


def _resolve_data_dir() -> Path:
    """The panel's private state directory, or a usable substitute."""
    return _resolve_dir(
        paths.data_dir(),
        paths.ENV_DATA_DIR,
        "",
        "Sessions and traffic history will not survive a reboot.",
    )


def _resolve_run_dir() -> Path:
    """Where the collector drops the live blob, or a usable substitute.

    Resolved here, in the web process, for the same reason the data dir is: both
    units load these settings, and the live blob is only worth writing to a path
    the reader agrees on.
    """
    return _resolve_dir(
        paths.run_dir(),
        paths.ENV_RUN_DIR,
        "-run",
        "Live stats will not survive a reboot, which is all they were worth anyway.",
    )


def _load_secret_key(data_dir: Path) -> str:
    """Signing key: from the environment, else generated once into DATA_DIR/secret.key.

    Generated rather than shipped, and stored rather than random per process:
    two processes with different keys reject each other's session cookies, and
    a key that changes on restart logs the admin out every time the service is
    restarted from the settings page.
    """
    from_env = os.environ.get("AWG_PANEL_SECRET_KEY") or os.environ.get("DJANGO_SECRET_KEY")
    if from_env:
        return from_env

    key_file = data_dir / "secret.key"

    def stored() -> str:
        try:
            return key_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    existing = stored()
    if existing:
        return existing

    # Write the whole key into a temp file, then publish it under the real name
    # with os.link. O_EXCL on the real path is not enough on its own: it creates
    # the file empty and fills it a moment later, so the web and collector units
    # starting together on first boot can have one of them read the other's
    # zero-byte file, fall back to a key of its own, and reject every session
    # the other issues. Linking publishes a name that already has its content.
    generated = secrets.token_urlsafe(64)
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".secret.key.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(generated + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, key_file)
        except FileExistsError:
            found = stored()
            if found:
                # Somebody else got there first. Their key is the one on disk,
                # and by construction it is already complete, so read it back
                # rather than keep a key nothing else will honour.
                return found
            # Present but empty: what an older version left behind when it lost
            # this race. There are no sessions in an empty key worth keeping,
            # and replacing it is the only way out of a state that would
            # otherwise repeat on every single start.
            os.replace(tmp, key_file)
            tmp = ""
        return stored() or generated
    except OSError:
        return generated
    finally:
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _read_version() -> str:
    try:
        return (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


# --------------------------------------------------------------------------
# Deployment shape
# --------------------------------------------------------------------------

DEBUG = _env_flag("AWG_PANEL_DEBUG") or _env_flag("DJANGO_DEBUG")

DATA_DIR = _resolve_data_dir()
RUN_DIR = _resolve_run_dir()
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"

BASE_PATH = normalise_base_path(os.environ.get("AWG_PANEL_BASE_PATH"))
# Strict, alone among the flags here. Everything else fails soft into "off",
# which costs a feature; getting this one wrong costs the admin's password,
# because gunicorn refuses to start on a value it cannot read and a panel that
# did start on a different reading of it would be serving cleartext under
# Secure cookies.
PANEL_TLS = _env_flag("AWG_PANEL_TLS", strict=True)
PANEL_VERSION = _read_version()
# The product's name, not a setting. It is what the panel calls itself in the
# sidebar, the browser title and the label an authenticator files its TOTP
# entry under, and one server's operator renaming their copy only ever made
# those three disagree with the documentation.
PANEL_NAME = "AWG Panel"
# Exposed so views can say so out loud instead of quietly showing invented
# traffic as if it came off a real interface.
PANEL_MOCK = _env_flag("AWG_MOCK")

SECRET_KEY = _load_secret_key(DATA_DIR)

# The panel is reached by IP on a port of the operator's choosing, often through
# a reverse proxy with a name we cannot know. Host validation would only lock
# people out; the secret base path plus the login form are the real gate.
ALLOWED_HOSTS = _env_list("AWG_PANEL_ALLOWED_HOSTS") or ["*"]

# Trailing-slash redirects would turn a POST to /api/v1/clients into a GET of
# /api/v1/clients/. The API paths are exact on purpose.
APPEND_SLASH = False

ROOT_URLCONF = "awgui.urls"
WSGI_APPLICATION = "awgui.wsgi.application"
ASGI_APPLICATION = "awgui.asgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --------------------------------------------------------------------------
# Applications and middleware
# --------------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "rest_framework",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "axes",
    "apps.accounts",
    "apps.panel",
    "apps.server",
    "apps.clients",
    "apps.stats",
    "apps.events",
]
# django.contrib.admin is deliberately absent: it is a second, differently
# authenticated way into the same data, and nothing here needs it.

MIDDLEWARE = [
    # First of all, because everything below it - and Django's own
    # USE_X_FORWARDED_HOST and SECURE_PROXY_SSL_HEADER - reads request.META
    # without any idea of which peer put those headers there. It removes the
    # forwarded headers unless the connection came from a proxy
    # AWG_PANEL_FORWARDED_ALLOW_IPS names.
    "awgui.middleware.TrustedProxyMiddleware",
    # Then the headers, so a 403 from CSRF or a 404 from the base-path check
    # carries the same ones as a rendered page.
    "awgui.middleware.SecurityHeadersMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "awgui.middleware.BasePathMiddleware",
    # After the base-path check, so a cleartext request for anything else still
    # gets the same empty 404 as a server with nothing on it. Before everything
    # below, so a request that arrived in the clear reaches no session, no login
    # and no static file.
    "awgui.middleware.HttpsOnlyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "awgui.middleware.SessionAgeMiddleware",
    # After the two above: it reads request.user, and the expiry the first one
    # applies is part of what it reports.
    "apps.accounts.middleware.LoginSessionMiddleware",
    # Last: it needs request.user, and it only acts on the login view.
    "axes.middleware.AxesMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # The built SPA is rendered as a template so the bootstrap script can be
        # injected into it; see awgui.views.SpaView.
        "DIRS": [FRONTEND_DIST] if FRONTEND_DIST.is_dir() else [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
            ],
        },
    },
]


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(DATA_DIR / "db.sqlite3"),
        # The collector writes a batch of samples every 10 s while the web
        # process is serving a poll; without a busy timeout one of them gets
        # "database is locked" instead of waiting a few milliseconds.
        "OPTIONS": {"timeout": 20},
    }
}


def _configure_sqlite(sender, connection, **kwargs) -> None:
    """Put every new SQLite connection into WAL with a generous busy timeout.

    WAL is what makes the collector's writes and the web process's reads
    concurrent at all: in the default rollback journal a writer blocks readers
    for the whole transaction, which on a 2 s dashboard poll is visible.
    The pragmas are per-connection except journal_mode, which is persistent;
    setting it every time is harmless and covers a database file restored from
    a backup taken in the old mode.
    """
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=20000;")
        # NORMAL trades an fsync per commit for the loss of at most the last
        # few traffic samples if the box loses power. Nothing here is money.
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")


connection_created.connect(_configure_sqlite, dispatch_uid="awgui.sqlite_pragmas")


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

# Argon2 first: the panel has exactly one password and it guards a root-level
# service, so the slowest hash we can afford is the right one.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

# Empty on purpose: the panel has one account and its owner chooses its
# password. A length floor, a dictionary and a "not all digits" rule between
# them refuse a great many passwords somebody meant to use, and on a panel that
# is not handing out accounts to strangers there is nobody for them to protect
# from that choice - the person typing the password owns the server underneath
# it. What a weak one is actually exposed to is guessing over the network, and
# that is answered where guessing happens: django-axes locks an address out for
# 15 minutes after five failures, and the optional second factor stops a guessed
# password being enough on its own.
#
# This list is the one place the policy lives. Everything that sets a password -
# the settings page, `manage.py seedadmin`, `manage.py changepassword` - runs
# validate_password against it, so putting rules back for a particular
# deployment means adding them here and nowhere else.
AUTH_PASSWORD_VALIDATORS = []

# axes first: its backend refuses a lockout before ModelBackend ever sees the
# password, so a locked-out address cannot keep burning Argon2 time.
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = timedelta(minutes=15)
# Per source address, not per username: the panel has one account, so locking
# on the username alone would let anyone lock the admin out of their own server.
AXES_LOCKOUT_PARAMETERS = ["ip_address"]
AXES_RESET_ON_SUCCESS = True
AXES_HTTP_RESPONSE_CODE = 403
AXES_LOCKOUT_TEMPLATE = None
# Behind a proxy the peer address is the proxy's, so every failed login in the
# world would share one lockout bucket. Read one hop of X-Forwarded-For only
# when the operator says there is a proxy: trusting the header unconditionally
# makes the lockout trivially bypassable. Resolved by our own callable rather
# than by axes' AXES_IPWARE_* knobs, which do nothing at all unless
# django-ipware is installed - and it is deliberately not a dependency here.
if _env_flag("AWG_PANEL_TRUST_PROXY"):
    # None of these three ask who sent the header - they cannot; they read
    # META, which is written before any of them runs. Which peers are believed
    # is decided in one place, by TrustedProxyMiddleware at the top of the
    # stack, from AWG_PANEL_FORWARDED_ALLOW_IPS. Turning this flag on where the
    # panel is still reachable on 0.0.0.0 used to hand a forged
    # X-Forwarded-For - and so a fresh django-axes lockout bucket per request -
    # to anyone who could open a socket to the port.
    AXES_CLIENT_IP_CALLABLE = "awgui.middleware.client_ip_from_forwarded_for"
    USE_X_FORWARDED_HOST = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # A proxy in front is the documented way to run this with TLS, and in that
    # arrangement gunicorn itself speaks plain HTTP - so AWG_PANEL_TLS is 0 and
    # the cookies below would ship without Secure, and HSTS would never be
    # sent. Treat a declared proxy as TLS: SECURE_PROXY_SSL_HEADER already
    # tells Django to believe X-Forwarded-Proto, so is_secure() is right either
    # way, and a proxy that terminates plain HTTP is not a deployment this
    # panel should be making comfortable.
    PANEL_TLS = True

LOGIN_URL = f"{BASE_PATH}login"


# --------------------------------------------------------------------------
# Sessions and CSRF
# --------------------------------------------------------------------------

SESSION_COOKIE_NAME = "awgsessionid"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
SESSION_COOKIE_SECURE = PANEL_TLS
# Scoped to the base path so a panel at /ab12cd34/ never sends its cookie to
# anything else served from the same host and port.
SESSION_COOKIE_PATH = BASE_PATH
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
# Starting point only; SessionAgeMiddleware applies the sessionMaxAge setting
# to each live session, so changing it in the UI takes effect without a restart.
SESSION_COOKIE_AGE = 86400

CSRF_COOKIE_NAME = "awgcsrftoken"
CSRF_COOKIE_PATH = BASE_PATH
CSRF_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_SECURE = PANEL_TLS
# Readable by JavaScript on purpose: the SPA copies it into X-CSRFToken. The
# double-submit check is what protects the request, not the cookie's secrecy.
CSRF_COOKIE_HTTPONLY = False
CSRF_TRUSTED_ORIGINS = _env_list("AWG_PANEL_TRUSTED_ORIGINS") or _env_list(
    "AWG_PANEL_CSRF_TRUSTED_ORIGINS"
)
CSRF_FAILURE_VIEW = "awgui.views.csrf_failure"


# --------------------------------------------------------------------------
# Static files
# --------------------------------------------------------------------------

# Vite emits every hashed bundle into dist/assets and references it relatively,
# so the URL prefix has to be the base path plus "assets/".
STATIC_URL = f"{BASE_PATH}assets/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# WhiteNoise warns on every start when STATIC_ROOT is missing, which it is in a
# checkout that has not run collectstatic. Creating it is cheaper than teaching
# people to ignore a warning.
try:
    STATIC_ROOT.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
STATICFILES_DIRS = [FRONTEND_DIST / "assets"] if (FRONTEND_DIST / "assets").is_dir() else []

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Compressed, not manifested: the file names Vite generates are already
    # content-hashed, and the manifest storage would rewrite URLs inside the
    # bundles it does not understand.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

# Serve straight from STATICFILES_DIRS as well, so a checkout that has run
# `npm run build` but not `collectstatic` still works.
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = DEBUG
WHITENOISE_MAX_AGE = 31536000  # every name under assets/ is content-hashed
WHITENOISE_INDEX_FILE = False


# --------------------------------------------------------------------------
# REST framework
# --------------------------------------------------------------------------

REST_FRAMEWORK = {
    # The browser's session cookie, and a bearer token for everything that is
    # not a browser. The token is tried first so an explicit Authorization
    # header decides the request rather than a cookie that happens to be lying
    # around; with no such header it returns None and the cookie is read as
    # before.
    #
    # Tokens are named, expiring and individually revocable by construction -
    # see apps.accounts.models.ApiToken - which is what makes them something
    # other than a password with no way to notice it had been copied. What they
    # deliberately cannot do is change the credentials that authorise them; that
    # rule lives in apps.accounts.permissions and is applied in the views.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.authentication.ApiTokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        # No BrowsableAPIRenderer: it would serve an HTML console for every
        # endpoint and pull in templates and static files nobody needs.
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        # POST api/v1/restore takes an uploaded archive.
        "rest_framework.parsers.MultiPartParser",
        "rest_framework.parsers.FormParser",
    ],
    "EXCEPTION_HANDLER": "awgui.exceptions.api_exception_handler",
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
}


# --------------------------------------------------------------------------
# Localisation and logging
# --------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

LOG_LEVEL = (os.environ.get("AWG_PANEL_LOG_LEVEL") or ("DEBUG" if DEBUG else "INFO")).upper()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    # journald and gunicorn both add their own timestamp; a second one is noise.
    "formatters": {"plain": {"format": "%(levelname)s %(name)s: %(message)s"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django.db.backends": {"level": "WARNING", "propagate": False},
        "apps": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "awg": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "awgui": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        # One failed login per line is useful; axes also logs every success.
        "axes": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
