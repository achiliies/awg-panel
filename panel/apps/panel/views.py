"""Panel-wide endpoints: settings, the TLS certificate, backup, restore, update.

Every one of these needs an authenticated session; DRF's defaults
(SessionAuthentication + IsAuthenticated) supply that, and SessionAuthentication
enforces CSRF on the mutations by itself. Nothing here is exempt.
"""

import ipaddress
import logging
from collections.abc import Mapping

from django.conf import settings as django_settings
from django.http import FileResponse, HttpRequest
from django.http.request import split_domain_port
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import CREDENTIAL_PERMISSIONS
from apps.events import kinds, recorder
from awg import clientsenv, ports, release
from awg.errors import AwgError, ValidationError
from awg.paths import panel_env_file

from . import apidocs, backup, certs, defaults, endpoint, settings_store, update
from .serializers import CamelCaseMixin

log = logging.getLogger(__name__)


# --------------------------------------------------------------- serializers


class SettingsSaveSerializer(CamelCaseMixin, serializers.Serializer):
    """The answer to a settings save: the new state plus what it costs to apply."""

    settings = serializers.DictField(child=serializers.CharField(allow_blank=True))
    needs_restart = serializers.BooleanField()
    url = serializers.CharField(allow_blank=True)
    warnings = serializers.ListField(child=serializers.CharField())


class CertificateSerializer(CamelCaseMixin, serializers.Serializer):
    """What the configured TLS certificate covers, and whether it can be used at all.

    Served separately from the settings themselves because it is not a setting:
    it is read off a file that a renewal can change without anything in the
    panel being saved.
    """

    path = serializers.CharField(allow_blank=True)
    names = serializers.ListField(child=serializers.CharField())
    domain = serializers.CharField(allow_blank=True)
    # Whole days left, negative once it has expired, null when the file cannot
    # be read. Worth showing because a certificate is now often a six-day one
    # kept alive by a renewal timer, and the first visible sign that the timer
    # has stopped would otherwise be a browser refusing to open the panel.
    expires_in_days = serializers.IntegerField(allow_null=True)
    # Why a save of these two paths would be refused, in the words the save
    # itself would use, or "" when it would not be. Both paths come back so a
    # caller can tell which files the verdict is about.
    problem = serializers.CharField(allow_blank=True)
    key_path = serializers.CharField(allow_blank=True)
    key_problem = serializers.CharField(allow_blank=True)


class RestoreResultSerializer(CamelCaseMixin, serializers.Serializer):
    restored = serializers.ListField(child=serializers.CharField())
    clients = serializers.IntegerField()
    iface = serializers.CharField()
    # Whether the web service is about to go down and come back. The page has to
    # know: every request it makes in the next few seconds fails, and a restore
    # that ends in a dead-looking panel with no explanation is worse than one
    # that says where the page is going, the way a settings save does.
    restarting = serializers.BooleanField()
    detail = serializers.CharField()


class UpdateCheckSerializer(CamelCaseMixin, serializers.Serializer):
    """Always the same shape, checked or not, so the UI has no optional branches."""

    checked = serializers.BooleanField()
    reason = serializers.CharField(allow_blank=True)
    current = serializers.CharField()
    latest = serializers.CharField(allow_null=True)
    update_available = serializers.BooleanField()
    url = serializers.CharField(allow_blank=True)
    notes = serializers.CharField(allow_blank=True)
    published_at = serializers.CharField(allow_null=True)
    # Deliberately not serialized: the two asset URLs awg.release resolves. The
    # page has no use for them - it never downloads anything - and the tool that
    # does resolves them again for itself a moment before it fetches them, so
    # sending them here would only be a second copy to go stale.


class UpdateStatusSerializer(CamelCaseMixin, serializers.Serializer):
    """Where an update got to, in the words bin/awg-update wrote into its state file.

    `status` is one of idle, running, succeeded, failed and is the only field a
    caller has to branch on; `phase` names the step for a progress line and will
    grow new values as the updater does, so nothing may switch on it exhaustively.
    `detail` is a whole sentence meant to be shown as it stands.
    """

    status = serializers.CharField()
    phase = serializers.CharField(allow_blank=True)
    detail = serializers.CharField(allow_blank=True)
    from_version = serializers.CharField(allow_blank=True)
    to_version = serializers.CharField(allow_blank=True)
    started = serializers.CharField(allow_blank=True)
    finished = serializers.CharField(allow_blank=True)
    # Where the pre-update archive went. Shown, because an operator whose update
    # went wrong needs the path more than they need anything else on the page.
    backup = serializers.CharField(allow_blank=True)
    # The tail of the log, not the whole of it: a module rebuild is several
    # hundred kilobytes of compiler output and this is polled every two seconds.
    log = serializers.CharField(allow_blank=True)
    # Why this server cannot update itself from here, or "" when it can. Sent on
    # every poll rather than discovered on the button press, so a panel that was
    # installed without the updater says so before anybody clicks.
    unavailable = serializers.CharField(allow_blank=True)


# ---------------------------------------------------------------- settings


def _check_shaping(cleaned: dict[str, str]) -> None:
    """Refuse to switch bandwidth limits on where they cannot work.

    Which is one case: a tunnel whose subnet is too wide for a class id per
    client. Every limit set afterwards would be refused one at a time with that
    same reason, while the switch that promised to allow them sat there saying it
    had worked.

    Not in apps.panel.defaults with the rest of the rules, deliberately. Every
    check there is about the value itself or about another setting, and answering
    this one means reading the server config - a dependency the settings table has
    never had and is better off not acquiring for one field.
    """
    if cleaned.get("shaperOn") != "1":
        return
    from apps.clients import shaping

    problem = shaping.subnet_problem()
    if problem:
        raise ValidationError(
            {
                "shaperOn": (
                    f"Speed limits cannot be used on this server: {problem} The tunnel's subnet "
                    "is set on the server config page."
                )
            }
        )


SHAPER_KEYS = frozenset({"shaperOn", "shaperUpload", "shaperWanIface"})


def _check_web_port(cleaned: dict[str, str], current: Mapping[str, str]) -> None:
    """Refuse to move the panel onto a port another service is already on.

    A port change restarts the web service, so the page takes the browser to
    the new address on the strength of the save; a warning would go into a
    banner that is gone before it is read, about a panel that never comes up,
    on a machine now reachable only over ssh. That is the failure this whole
    page exists to avoid, and it is the one thing here that cannot be undone
    from a browser.

    Only a port that is actually moving is asked about, and only a socket bound
    at this moment answers - something installed and stopped is not in the way.
    A value that is not a valid 1..65535 port returns quietly so that the
    settings validator produces its own error message.
    """
    raw = cleaned.get("webPort")
    if raw is None:
        return
    if isinstance(raw, int):
        port_num = raw
    elif isinstance(raw, str):
        raw_str = raw.strip()
        if not raw_str.isdigit():
            return
        port_num = int(raw_str)
    else:
        return
    if not (1 <= port_num <= 65535):
        return
    current_port = current.get("webPort", "")
    if str(port_num) == str(current_port).strip():
        return
    if not ports.busy(port_num, "tcp"):
        return
    culprit = ports.holder(port_num, "tcp") or "Another service"
    raise ValidationError(
        {
            "webPort": (
                f"{culprit} is already listening on port {port_num}, so the panel would not come "
                "back up there. Stop it first, or choose a port nothing else is using."
            )
        }
    )


def _reshape(changed: dict[str, str], before: Mapping[str, str]) -> None:
    """Rebuild the shaping structure when a save moved one of the three settings behind it.

    Turning shaping on has to put every existing ceiling in the kernel at once,
    turning it off has to take the whole structure down, and turning upload
    shaping on or off adds or removes a whole interface's worth of it. All three
    are the reconcile pass's ordinary work, so this is not a second
    implementation of it - it is the same pass, run at the moment the admin
    changed its input rather than up to a minute later, which is the difference
    between a switch that works and a switch that appears not to.

    The one thing the pass cannot do is the removal below, and `before` is why it
    is here rather than there. Upload shaping lives on an interface named in the
    settings, and a pass reads the settings as they are - so the moment upload
    shaping is switched off, or pointed at a different interface, the old one is
    not in them any more and nothing afterwards can find out where the structure
    is. The kernel cannot be asked either: an HTB root carries no record of who
    put it there, and this project builds on one it finds rather than replacing
    it, so a root on some interface is as likely to be the operator's own. This
    save is the last moment anything knows, so it is where the knowing is used.

    Never fatal. The settings are already stored, the collector will converge on
    them regardless, and a save reported as failed would have the admin pressing
    it again against a server that had in fact accepted it.
    """
    if not changed.keys() & SHAPER_KEYS:
        return
    from apps.clients import shaping

    try:
        _drop_old_upload(before)
        plan, wanted = shaping.wanted()
        shaping.reconcile(plan, wanted, rebuild=True)
    except (AwgError, OSError) as exc:
        log.warning("the bandwidth settings were saved but could not be applied yet: %s", exc)


def _drop_old_upload(before: Mapping[str, str]) -> None:
    """Take the upload structure off the interface it was on, when it has just moved off it.

    Three saves land here: upload shaping switched off, bandwidth limits switched
    off altogether, and the WAN interface changed to another one. In every one of
    them the structure the previous settings built is still on the old interface,
    and nothing else will ever remove it - the reconcile pass detaches through
    the plan, and the plan has already forgotten the interface.

    What that left behind was not merely untidy. The WAN keeps a class and an
    `fw` filter per client and the tunnel keeps marking their packets, so every
    client went on being held to an upload ceiling the panel no longer showed and
    offered no way to remove; and the HTB root stayed on the interface this
    machine sends everything through, which is exactly the cost the setting is
    worded to make the admin choose deliberately.

    Nothing happens unless the settings as they *were* had upload shaping on, so
    a server that has never used it is never asked to delete a qdisc it did not
    put there.
    """
    if not (before.get("shaperOn") == "1" and before.get("shaperUpload") == "1"):
        return
    from apps.clients import shaping

    was = shaping.wan_iface(before)
    if not was:
        return
    values = settings_store.all_settings()
    now = ""
    if values.get("shaperOn") == "1" and values.get("shaperUpload") == "1":
        now = shaping.wan_iface(values)
    if was != now:
        shaping.detach_upload(was)


class SettingsView(APIView):
    """GET the panel's settings, PUT a partial update."""

    def get(self, request: Request) -> Response:
        return Response(settings_store.all_settings())

    def put(self, request: Request) -> Response:
        payload = request.data
        if not isinstance(payload, dict):
            raise ValidationError("Send the settings to change as a JSON object.")

        current = settings_store.all_settings()
        cleaned = defaults.validate_settings(payload, current=current)
        _check_shaping(cleaned)
        _check_web_port(cleaned, current)
        changed = {key: value for key, value in cleaned.items() if current.get(key) != value}
        settings_store.set_many(changed)

        # `current` is the settings as they were before this save, which is the
        # only record of where an upload structure this save orphans actually is.
        _reshape(changed, current)

        values = settings_store.all_settings()
        needs_restart = bool(changed.keys() & defaults.RESTART_KEYS)
        # A save that changes no row still has work to do when the env file
        # disagrees with the settings - after a hand edit, or a write that
        # failed. Without this the only way to repair it from the UI is to
        # change a value to something else and then change it back, and the
        # form is already showing the answer the admin wants.
        if not needs_restart and cleaned.keys() & defaults.RESTART_KEYS:
            needs_restart = not settings_store.panel_env_is_current(values)

        applied, notes = True, []
        if needs_restart:
            applied, notes = _apply_web_settings(values)

        # Resolved before the warnings, because they quote it. One address, one
        # string: the overlay navigates to it and every line of advice names the
        # same thing, base path included. A message that says "reconnect over
        # https" or "at the new address" leaves the admin to reconstruct a
        # random secret prefix from memory, and getting it wrong looks exactly
        # like a panel that never came back.
        url = _panel_url(request, values, current) if applied else _current_url(request)
        warnings = defaults.warnings_for(changed, values, url)
        # Only once the move is really happening. These lines say where the page
        # is about to go and what the browser will make of it; on a save that
        # could not be applied nothing goes anywhere, and the notes below say so.
        if applied and changed.keys() & {"tlsCertPath", "tlsKeyPath"}:
            warnings.extend(_tls_warnings(request, values, current, url))
        warnings.extend(notes)

        # Only what actually moved: which settings an operator touched is the
        # part that explains a panel that stopped answering.
        #
        # With the new value beside the name where the value is fit to keep, and
        # apps.panel.defaults decides which those are - anything that says how to
        # reach this panel is recorded by name alone. Without the value the log
        # can only say that something about speed limits changed, on a page whose
        # switch is the one thing an admin is trying to remember the state of; a
        # line that names the setting and stops is a question rather than a
        # record.
        if changed:
            recorder.record(
                kinds.PANEL_SETTINGS_SAVED,
                request,
                count=len(changed),
                keys=sorted(changed),
                values=defaults.logged_changes(changed),
            )

        result = {
            "settings": values,
            # Only what is actually going to happen. The page hands the browser
            # to `url` on the strength of this flag and gets no second chance,
            # so announcing a restart that was never scheduled sends an admin to
            # an address nothing will ever answer on - which is indistinguishable
            # from the panel having died, at the moment they can least afford it.
            "needs_restart": needs_restart and applied,
            "url": url,
            "warnings": warnings,
        }
        return Response(SettingsSaveSerializer(result).data)


class CertificateView(APIView):
    """GET api/v1/settings/certificate[?path=...&key=...] - what the panel makes of a TLS pair.

    The Settings page offers the certificate's names as the address to write
    into client configs, so it has to read them from the file rather than from
    anything stored: the admin has just typed the path, and a renewal can change
    the names later without a save happening at all. A certificate that cannot
    be parsed answers with empty lists rather than an error - gunicorn is the
    authority on whether a file works, and there is simply nothing to offer.

    Whether the two files are *there* is a different question, and one the page
    has to be able to ask before it commits to anything. Saving a TLS path
    restarts the web service, so the page puts a confirmation in front of it;
    asking that question after the admin has agreed to the restart, and then
    refusing the save over a path that was never checked, is how a form ends up
    demanding permission for something it was never going to do. `problem` and
    `keyProblem` are the answer the save would give, in the same words, early
    enough to be shown under the box that has to change.

    `path` and `key` ask about files that have been typed and not saved. That is
    also the only way the page can say where the panel will reconnect before the
    save that moves it: turning HTTPS on hands the browser to the name inside
    the certificate, and until this exists the only certificate anyone can read
    is the one being replaced. It reads no more than saving the paths and asking
    again would, and answers about the stored ones when they are absent.

    Both paths are echoed back so a caller can tell which files the answer is
    about. A reply to a stale request is worth nothing here - it would name the
    previous certificate's domain, with every appearance of naming this one's.
    """

    def get(self, request: Request) -> Response:
        stored = settings_store.all_settings()
        path = _asked_path(request, "path", stored.get("tlsCertPath", ""))
        key_path = _asked_path(request, "key", stored.get("tlsKeyPath", ""))
        problems = defaults.tls_problems(path, key_path)

        # A path the save would refuse is not one to open. A relative one is
        # read against the service's working directory, which is a file nobody
        # asked about, and a missing one has nothing to say for itself either.
        readable = path if not problems.get("tlsCertPath") else ""
        result = {
            "path": path,
            "names": certs.certificate_names(readable) if readable else [],
            "domain": endpoint.certificate_domain({**stored, "tlsCertPath": readable}),
            "expires_in_days": certs.expires_in_days(readable) if readable else None,
            "problem": problems.get("tlsCertPath", ""),
            "key_path": key_path,
            "key_problem": problems.get("tlsKeyPath", ""),
        }
        return Response(CertificateSerializer(result).data)


def _asked_path(request: Request, param: str, stored: str) -> str:
    """The file a certificate query is about: the one named, or the stored one."""
    asked = (request.query_params.get(param) or "").strip()
    return asked or stored


def _apply_web_settings(values: dict[str, str]) -> tuple[bool, list[str]]:
    """Write the env file and schedule the restart.

    Returns whether the new settings are really about to be live, and anything
    the admin has to do themselves. False means the service keeps serving where
    it is, and the caller has to say so rather than move the browser.
    """
    if not settings_store.write_panel_env(values):
        return False, [
            f"The change is saved, but {panel_env_file()} could not be written, so the service "
            "is still serving the old address. It will keep doing that until that file is "
            "updated by hand and the service restarted."
        ]
    if not settings_store.request_web_restart():
        return False, [
            "The change is saved and written to the environment file, but the panel could not "
            "restart itself (awg-panel is not installed here), so nothing has moved yet. Run "
            "'sudo systemctl restart awg-panel-web' to pick it up."
        ]
    return True, []


def _current_url(request: HttpRequest | Request) -> str:
    """Where this browser already is - the honest answer when a change did not take."""
    return f"{request.scheme}://{request.get_host()}{django_settings.BASE_PATH}"


def _panel_url(
    request: HttpRequest | Request, values: dict[str, str], previous: dict[str, str]
) -> str:
    """Where the panel will answer once the new settings are live.

    The listen address wins when it names one interface, because that is where
    the service will actually be; when it is a wildcard the host in this request
    is the only address we know reaches the box.
    """
    scheme = "https" if _tls_on(values) else "http"
    base = settings_store.panel_env_values(values)["AWG_PANEL_BASE_PATH"]
    host = _panel_host(request, values, previous)
    return f"{scheme}://{host}:{values.get('webPort', '')}{base}"


def _panel_host(
    request: HttpRequest | Request, values: dict[str, str], previous: dict[str, str]
) -> str:
    """The host the panel will answer on, from the point of view of this browser.

    A pinned listen address wins over everything: the service will be there and
    nowhere else. On a wildcard the address in this request is the only one
    known to reach the box - until TLS is in play in either direction, because
    then it is also an address the browser is about to refuse.
    """
    listen = values.get("webListen", "")
    if listen not in ("", "0.0.0.0", "::"):
        return f"[{listen}]" if ":" in listen else listen  # IPv6 literal needs brackets

    host = split_domain_port(request.get_host())[0] or "localhost"
    if _tls_on(values):
        # A panel is nearly always administered at http://<ip>/, and a certificate
        # is issued for a domain. Keeping the address the admin arrived on would
        # send them to one the certificate cannot match, and a browser refusing a
        # name is indistinguishable from a panel that failed to restart - at the
        # one moment the page has already handed control over and cannot take it
        # back. So the name on the certificate wins over the host in the request.
        names = certs.certificate_names(values["tlsCertPath"])
        if not names or certs.covers(names, host):
            return host
        usable = [name for name in names if not name.startswith("*")]
        return usable[0] if usable else host

    # The way back is not the way out. Every response served over HTTPS carried
    # an HSTS header, so this browser has been told to use HTTPS for that name
    # for a year - and it obeys that before it makes a request, upgrading
    # http://<name>/ to https:// and failing against a panel that no longer
    # speaks it. The pin is on the name only, so the server's own address is the
    # one thing that still answers, and it is where the handover has to go.
    if _tls_on(previous) and not _is_address(host):
        return _server_address() or host
    return host


def _tls_on(values: dict[str, str]) -> bool:
    """Is the panel itself terminating TLS?

    Not "is this page on HTTPS": a reverse proxy in front answers yes to that
    while both of these stay empty, and nothing the panel does to them can turn
    its certificate off.
    """
    return bool(values.get("tlsCertPath") and values.get("tlsKeyPath"))


def _is_address(host: str) -> bool:
    """An IP literal rather than a name, brackets and all. Only names get pinned."""
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _server_address() -> str:
    """The server's own address, as every client config already dials it.

    Only an address is offered, never a name: this is read at the moment a name
    has just become unusable, and a second name is only another thing HSTS may
    have pinned. Blank when the endpoint is left to auto-detect, which is the
    caller's cue that there is nothing better than the host it already has -
    probing for one here would spend the metadata service's timeout inside a
    save that is about to restart the service.
    """
    try:
        host = (clientsenv.read_env().get("ENDPOINT_HOST") or "").strip()
    except OSError:
        return ""
    if not _is_address(host):
        return ""
    return f"[{host}]" if ":" in host else host


def _tls_warnings(
    request: HttpRequest | Request, values: dict[str, str], previous: dict[str, str], url: str
) -> list[str]:
    """Why the address is about to change, or why the browser is about to complain.

    `url` is the whole address, not just the name that changed: this line is the
    one an admin copies when the handover does not land, and a bare hostname
    leaves them to remember the port and the secret path themselves.
    """
    asked = split_domain_port(request.get_host())[0] or "localhost"
    if not _tls_on(values):
        return _tls_off_warnings(request, values, previous, url, asked)
    names = certs.certificate_names(values["tlsCertPath"])
    if not names:
        return []
    serving = _panel_host(request, values, previous)
    if serving != asked:
        return [
            f"The certificate is issued for {serving}, not {asked}, so this page will reconnect "
            f"at {url}. That name has to resolve to this server; if it does not yet, add the DNS "
            f"record before the restart finishes."
        ]
    if not certs.covers(names, asked):
        covered = ", ".join(names[:3])
        return [
            f"The certificate does not cover {asked}. HTTPS will work, but every browser will "
            f"warn about the name until the panel is reached as {covered}."
        ]
    return []


def _tls_off_warnings(
    request: HttpRequest | Request,
    values: dict[str, str],
    previous: dict[str, str],
    url: str,
    asked: str,
) -> list[str]:
    """What a browser does with the name it was just told to stop using.

    Only for the panel's own certificate going away. Turning off a pair that was
    already off changes nothing, and TLS terminated by a proxy is not ours to
    have pinned.
    """
    if not _tls_on(previous) or _is_address(asked):
        return []
    if _panel_host(request, values, previous) == asked:
        return [
            f"This browser was told to use HTTPS for {asked} for a year, so it will turn {url} "
            f"back into an https:// address and find nothing listening. Reach the panel by the "
            f"server's IP address instead, or clear the HSTS entry for {asked} in the browser's "
            f"settings."
        ]
    # It moved. Worth explaining only when the pin is what moved it: a listen
    # address that names one interface moves it too, and says so on its own.
    if values.get("webListen", "") not in ("", "0.0.0.0", "::"):
        return []
    return [
        f"That address is no longer {asked} because this browser was told to use HTTPS for that "
        f"name for a year, and it would refuse to load it over plain HTTP until that expires."
    ]


# ------------------------------------------------------- backup and restore


class BackupView(APIView):
    """GET api/v1/backup - the whole configuration as a .tar.gz.

    Open to an API token, and that decision is the limit of the rule
    apps.accounts.permissions draws rather than an exception to it. The archive
    carries db.sqlite3, which holds the session table - and a session key is the
    awgsessionid cookie itself, not a hash of it - along with the TOTP secret and
    the password hash, and it carries the panel's signing key beside them. A
    token that fetches one can put on a signed-in browser's cookie, so it has the
    account whatever the routes under settings/ answer it.

    Left open all the same, because the alternative buys less than it costs: the
    same archive holds the server's private key and every client's, so anything
    trusted to copy a backup off the box nightly is already trusted with the
    tunnel, and closing this would only push that job back onto a stored
    password. What makes it bearable is that it is not quiet - the row recorded
    below names the token that asked, which is the one thing an operator has to
    go on afterwards.
    """

    def get(self, request: Request) -> FileResponse:
        path = backup.create_archive()
        handle = path.open("rb")
        # Unlinked while open: the download still reads from the file
        # descriptor, and there is nothing left to clean up if the client
        # disconnects halfway through.
        path.unlink(missing_ok=True)
        response = FileResponse(
            handle,
            as_attachment=True,
            filename=backup.archive_name(),
            content_type="application/gzip",
        )
        response.headers["Cache-Control"] = "no-store"
        # Recorded when the archive is handed over rather than when it finishes
        # downloading, which is the only moment the server knows about. What it
        # is worth recording for is that this file holds the server's private key
        # and every client's, so it is one of the few reads here worth a row.
        recorder.record(kinds.PANEL_BACKUP_CREATED, request)
        return response


class RestoreView(APIView):
    """POST api/v1/restore - replace the configuration from an uploaded archive.

    Closed to an API token, which is the one route outside apps.accounts that
    has to be. A restore replaces `db.sqlite3`, and that file holds the password
    hash and the tokens themselves - so a token allowed to upload an archive
    could hand the account to whoever chose the archive, which is precisely the
    thing every rule in apps.accounts.permissions exists to prevent.

    Downloading one is left open, and the view above says why and what it costs.
    The short of it is that this rule bounds what a token can do quietly rather
    than what it can do at all.
    """

    permission_classes = CREDENTIAL_PERMISSIONS

    def post(self, request: Request) -> Response:
        upload = request.FILES.get("file") or request.FILES.get("archive")
        if upload is None:
            raise ValidationError({"file": "Attach the .tar.gz backup file you want to restore."})
        result = backup.restore_archive(upload)
        log.warning("configuration restored from an uploaded backup: %s", result["restored"])
        # Into the database that has just replaced the one this request started
        # with, which is exactly where it belongs: the archive brought its own
        # history with it, and this is the first thing that happened afterwards.
        # The restore has already closed and reopened the connections, so this
        # write goes to the new file rather than to the one it displaced.
        recorder.record(
            kinds.PANEL_RESTORED, request, count=result["clients"], parts=result["restored"]
        )
        return Response(RestoreResultSerializer(result).data)


# -------------------------------------------------------------- description


class OpenApiView(APIView):
    """GET api/v1/openapi.json - the whole API, as a document a tool can read.

    Authenticated like everything else, and open to an API token on purpose: the
    caller most likely to want this is a script being written against the panel,
    and making the description the one thing a token cannot fetch would be a
    strange place to draw a line. It describes routes rather than revealing
    anything about this server, so a token reading it learns nothing it could
    not learn by trying the routes.

    The `servers` entry is built from the request rather than from the settings,
    so the document names the address the caller actually reached - base path,
    port and scheme included - and an import into Postman or a client generator
    needs no editing. Behind a reverse proxy that is the proxy's address, which
    is the right answer and the one a browser would have used.
    """

    def get(self, request: Request) -> Response:
        root = request.build_absolute_uri(f"{django_settings.BASE_PATH}api/v1")
        return Response(apidocs.document(root, django_settings.PANEL_VERSION))


# ------------------------------------------------------------ update


class UpdateCheckView(APIView):
    """POST api/v1/update/check - compare VERSION against the newest release.

    Never fails on the network. A server that cannot reach GitHub is the normal
    case behind a strict egress policy, and an error here would look like the
    panel itself was broken - so the answer carries `checked: false` and a
    sentence, and the page says which of the two happened.

    A POST rather than a GET for a read, which is the one thing about this route
    that needs defending. It reaches out to a third party and takes seconds over
    a slow link, and a GET is what a browser prefetches, what a proxy caches and
    what the page's own query client would re-run on every window focus. Making
    it a mutation is what keeps the check something an operator asked for.
    """

    def post(self, request: Request) -> Response:
        return Response(UpdateCheckSerializer(release.check(_version())).data)


class UpdateStatusView(APIView):
    """GET api/v1/update/status - where a running or finished update got to.

    Read straight off the file bin/awg-update writes, which is what makes this
    survive the thing it is reporting on: the update restarts this service
    halfway through, and the page polling here simply fails for a few seconds
    and then reads the rest of the story out of the same file.

    Open to an API token, unlike starting one. Watching an update is reading, and
    a script that kicked off a deployment has a fair claim to know how it went.
    """

    def get(self, request: Request) -> Response:
        return Response(UpdateStatusSerializer(update.status()).data)


class UpdateApplyView(APIView):
    """POST api/v1/update/apply - install the newest stable release.

    Answers as soon as the updater is running, not when it has finished. It
    cannot answer later: this request is served by the web service the update
    restarts, so a handler that waited would be killed by its own work and the
    browser would see a dropped connection where the result should have been.
    The page polls `update/status` from there.

    Closed to API tokens. Everything else on that list is about the credentials
    themselves; this is on it for a larger reason. It replaces every line of code
    on the server, this file included, from a release fetched over the internet -
    so a leaked token that could press it would not merely read the panel's data,
    it would decide which software the box runs next. That is a decision for a
    person at the login form.
    """

    permission_classes = CREDENTIAL_PERMISSIONS

    def post(self, request: Request) -> Response:
        force = bool(request.data.get("force")) if isinstance(request.data, Mapping) else False
        state = update.start(force=force)
        # Recorded before the update replaces the database this row is in - and
        # it survives it, because an upgrade migrates that database rather than
        # replacing it. Worth a row of its own: an operator reading the log after
        # a bad release needs to see who started it and when, and the panel's own
        # version having changed underneath is exactly the context that makes the
        # surrounding rows hard to read otherwise.
        #
        # The version being installed is deliberately not on the row, because at
        # this moment nobody on this server knows it. `awg-update start` hands the
        # work to systemd without asking GitHub anything, so `to_version` is still
        # empty here and the worker fills it in a second later - a row naming it
        # would either be blank or be whatever the browser said it should be. The
        # release that got installed is in the state file, in the update's log and
        # in the version the panel reports afterwards.
        recorder.record(
            kinds.PANEL_UPDATE_STARTED,
            request,
            **{"from": state["from_version"] or _version()},
        )
        log.warning("update started by %s", getattr(request.user, "username", "?"))
        return Response(UpdateStatusSerializer(state).data)


def _version() -> str:
    """What this installation calls itself, which is the release's own tag."""
    return django_settings.PANEL_VERSION
