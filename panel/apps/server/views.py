"""The server endpoints: read the config, change it, explain what applying costs.

Nothing here caches. Every request re-reads awg0.conf and clients.env through
``awg.store``, because this process is one of several that write them and an
admin with an editor is another, so it is never entitled to assume it made the
last change.

Saving applies. Contract 11a decides how: peer changes go on the live interface
with ``awg syncconf`` and nobody notices, but every obfuscation parameter, and
the port, address and MTU, need a full down/up, because a half-applied junk
configuration fails the handshake with nothing in any log. The panel takes the
interface down and back up inside the save, because a saved setting that quietly
does not apply until somebody finds the right button is the worse failure. The
response still says which of the two happened: ``needsRestart`` is what the
change cost, ``applied`` is whether the running interface now carries it.

A moved listen port takes the host firewall rule with it, through
``awg.firewall`` - the same ufw and firewalld handling ``lib/firewall.sh`` gives
the installer. Everything the panel cannot reach - a cloud security group above
all - comes back as a warning naming what is left to do. The port it moved to is
asked about first, through ``awg.ports``: a port another service already holds
is a tunnel that will not come up, and nothing downstream of the save ever names
the process that was in the way.

Every view needs an authenticated session: DRF's defaults supply that, and
SessionAuthentication enforces CSRF on the mutations by itself.
"""

import logging
from typing import Any

from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.events import kinds, recorder
from apps.panel.serializers import camelize_keys, to_camel_case
from awg import firewall, paths, ports, store, validate
from awg.controller import get_controller
from awg.errors import AwgError
from awg.errors import ValidationError as CoreValidationError

from .serializers import (
    ParamPreviewSerializer,
    ParamSpecSerializer,
    ReconfigureSerializer,
    ServerSaveResultSerializer,
    ServerSerializer,
    ServerStatusSerializer,
    ServerUpdateSerializer,
)

log = logging.getLogger(__name__)

HOOK_FIELDS = ("post_up", "post_down", "pre_down")

HOOKS_NOT_LIVE = (
    "PostUp, PostDown and PreDown only run when the interface starts, so the new commands "
    "take effect at the next restart."
)


class ServerView(APIView):
    """GET api/v1/server, PUT api/v1/server."""

    def get(self, request: Request) -> Response:
        return Response(ServerSerializer(store.read_server()).data)

    def put(self, request: Request) -> Response:
        payload = ServerUpdateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        changes: dict[str, Any] = dict(payload.validated_data)
        # The UI nests the obfuscation parameters because that is how it renders
        # them; save_server takes them at the top level under their config keys.
        changes.update(changes.pop("params", None) or {})

        hooks_changed = _hooks_changed(changes)
        port_before = _listen_port() if "listen_port" in changes else 0
        try:
            _check_listen_port(changes, port_before)
            saved = store.save_server(changes)
        except CoreValidationError as exc:
            raise _camelised(exc) from exc

        warnings = list(saved["warnings"])
        # Before the restart, so the interface comes back up on a port that is
        # already reachable, instead of during a window where it is not.
        _sync_firewall(port_before, warnings)
        # save_server regenerates every client config when must_reimport is set,
        # so the files on disk already carry the new settings by the time this
        # answers; the UI only has to tell people to re-import.
        log.info(
            "server settings saved (restart=%s, reimport=%s, %d client config(s) rewritten)",
            saved["needs_restart"],
            saved["must_reimport"],
            saved["resynced"],
        )

        if saved["needs_restart"]:
            applied = _restart_now(request.user.get_username(), warnings)
        elif hooks_changed:
            applied = False
            warnings.append(HOOKS_NOT_LIVE)
        else:
            applied = _apply_live(warnings)

        result = {
            "needs_restart": saved["needs_restart"],
            "must_reimport": saved["must_reimport"],
            "applied": applied,
            "warnings": warnings,
        }
        # One event for the save, carrying what the save cost rather than what
        # was in it. The values are in the config file and in the backup beside
        # it; what is nowhere else afterwards is that this was the change that
        # dropped every session, or the one that made every client re-import.
        recorder.record(
            kinds.SERVER_SAVED,
            request,
            restart=bool(saved["needs_restart"]),
            reimport=bool(saved["must_reimport"]),
            applied=applied,
        )
        return Response(ServerSaveResultSerializer(result).data)


class ServerParamsView(APIView):
    """GET api/v1/server/params - the catalog the Server Config page is built from."""

    def get(self, request: Request) -> Response:
        features = get_controller().features()
        rows = [_annotate(row, features) for row in validate.param_list()]
        return Response(ParamSpecSerializer(rows, many=True).data)


class ReconfigureObfuscationView(APIView):
    """POST api/v1/server/reconfigure - a preview, not a save.

    Draws one server's worth of settings and hands them back. Nothing is
    written: the UI puts the values in the form and the admin saves them like
    any other edit, because applying this costs every client a re-import and
    that is not a thing to do on one click.

    Two scopes, because the page has two halves that are set independently. The
    default one is the obfuscation every client speaks. The other is the
    AmneziaWG 3.0 group, and it is the one that has to be filtered here: a
    parameter the installed module cannot do is an error at save time rather
    than a silent drop, so generating one would hand the admin a set that
    cannot be saved. What was left out is said out loud instead.

    Whatever is drawn has to be savable, which decides where the numbers behind
    it come from: the config, always, and never the form. S4 is measured against
    the MTU at save time, and the MTU that is in force then is the saved one, so
    a preview drawn against an unsaved one is a preview the save refuses. An
    unreadable config is not a reason to refuse - the generator falls back to its
    own default, and the save will fail on its own if the config is really
    broken.
    """

    def post(self, request: Request) -> Response:
        options = ReconfigureSerializer(data=request.data)
        options.is_valid(raise_exception=True)
        wanted = options.validated_data

        try:
            server = store.read_server()
        except AwgError:
            server = None
        mtu = server.mtu if server else 0
        profile = wanted["profile"]

        if wanted["scope"] == "advanced":
            params, warnings = _advanced_preview(profile, server)
        else:
            params = validate.randomize(mtu=mtu or None, profile=profile)
            warnings = validate.warnings_for({**params, "MTU": str(mtu)} if mtu else params)
            # Coming back as 0 when the profile asked for more is the generator
            # giving up on a value rather than choosing one, and it happens
            # quietly - the field simply reads 0. It takes an MTU the panel will
            # not save to get there, so the sentence points at the MTU.
            if params["S4"] == "0" and validate.PROFILES[profile].s4[0]:
                warnings.append(
                    f"S4 was left off. It is added to every data packet and comes out of what "
                    f"the MTU leaves, and an MTU of {mtu or validate.DEFAULT_MTU} leaves no room "
                    f"for it: the largest that does is {validate.MTU_BUDGET - validate.HEADER_NONCE}. "
                    "Lower the MTU on the Server page and save it, then draw this again."
                )

        return Response(ParamPreviewSerializer({"params": params, "warnings": warnings}).data)


class ServerRestartView(APIView):
    """POST api/v1/server/restart - awg-quick down and up."""

    def post(self, request: Request) -> Response:
        # Worth a log line at warning level: this drops every session, and the
        # journal is where an operator looks to find out why.
        log.warning("%s restarted the interface from the panel", request.user.get_username())
        store.restart_iface()
        # After the call, so the event describes a restart that happened. One
        # that failed raises, and the operator gets the error rather than a log
        # entry contradicting it.
        recorder.record(kinds.SERVER_RESTARTED, request, target=paths.iface())
        return Response(status=status.HTTP_204_NO_CONTENT)


class ServerStopView(APIView):
    """POST api/v1/server/stop - take the tunnel down and leave it down.

    Not a restart with the second half missing. A restart is an operation on a
    tunnel that is meant to be running; this ends with a server that serves
    nobody until somebody says otherwise, which is why it gets its own event
    rather than sharing one.

    Idempotent, because systemd's stop is: pressing it on a tunnel that is
    already down succeeds and says so, instead of producing an error about a
    state the admin was asking for anyway.
    """

    def post(self, request: Request) -> Response:
        log.warning("%s stopped the interface from the panel", request.user.get_username())
        store.stop_iface()
        recorder.record(kinds.SERVER_STOPPED, request, target=paths.iface())
        return Response(status=status.HTTP_204_NO_CONTENT)


class ServerStartView(APIView):
    """POST api/v1/server/start - bring the tunnel back up."""

    def post(self, request: Request) -> Response:
        log.warning("%s started the interface from the panel", request.user.get_username())
        store.start_iface()
        # After the call for the same reason the restart records afterwards: a
        # start that failed raises, and the operator gets the error rather than
        # a log entry claiming the tunnel is up.
        recorder.record(kinds.SERVER_STARTED, request, target=paths.iface())
        return Response(status=status.HTTP_204_NO_CONTENT)


class ServerStatusView(APIView):
    """GET api/v1/server/status - what the module, the tools and the interface are doing.

    This is the page an admin opens when something is wrong, so nothing in it is
    allowed to fail: a missing config, absent tools and a down interface are all
    answers rather than errors.
    """

    def get(self, request: Request) -> Response:
        controller = get_controller()
        features = controller.features()
        iface = paths.iface()
        iface_up = controller.iface_up()
        service = controller.service_state()

        # `awg show` is the only honest test of whether the tunnel holds its UDP
        # socket: the kernel module's socket never appears in /proc/net/udp, so
        # scanning for a listener would report an empty port on a working server.
        dump = controller.show_dump() if iface_up else None
        live_port = dump.listen_port if dump else 0

        server = None
        conf_error = ""
        try:
            server = store.read_server()
        except AwgError as exc:
            conf_error = str(exc)

        module_loaded = features.get("module_loaded")
        if module_loaded is None:
            module_loaded = controller.module_loaded()

        payload = {
            "iface_up": iface_up,
            "module_loaded": bool(module_loaded),
            "tools_version": features.get("tools_version"),
            "module_version": features.get("module_version"),
            "features": _features_payload(features),
            "service_active": service.get("active") or "unknown",
            "service_enabled": service.get("enabled") or "unknown",
            "listening": bool(live_port),
            "warnings": _status_warnings(
                controller=controller,
                features=features,
                iface=iface,
                iface_up=iface_up,
                service=service,
                live_port=live_port,
                server=server,
                conf_error=conf_error,
            ),
        }
        return Response(ServerStatusSerializer(payload).data)


# ---------------------------------------------------------------- internals


def _advanced_preview(
    profile: str,
    server: store.ServerView | None,
) -> tuple[dict[str, str], list[str]]:
    """Draw the AmneziaWG 3.0 group, minus whatever this build cannot do.

    The key this draws puts a floor under S1-S4, and those are on the other card
    rather than this one, so a set drawn here can be refused by a save on
    account of padding this never touched. It is checked against what is in the
    config and said out loud, because the alternative is an operator pressing
    Save on a form the generator filled in and being told the problem is in
    fields it left alone.
    """
    features = get_controller().features()

    params = validate.randomize_advanced(profile=profile)
    unsupported = [
        key for key in params if not bool(features.get(validate.FEATURE_OF.get(key, ""), True))
    ]
    for key in unsupported:
        params[key] = ""

    warnings = validate.warnings_for(params)
    if unsupported:
        warnings.append(
            f"{', '.join(unsupported)} was left empty: the installed AmneziaWG does not support "
            "it, and setting it would stop the config saving at all. Upgrade the kernel module "
            "and tools to use it."
        )
    if params["HeaderProtectionKey"] and server:
        short = validate.header_protection_short({**server.params, **params})
        if short:
            warnings.append(
                f"{', '.join(short)} will have to be raised to at least {validate.HEADER_NONCE} "
                "before this can be saved: the header protection key is used with a nonce read "
                "from the front of that padding, and the kernel refuses a configuration where it "
                "does not fit. Draw the obfuscation above again, which never goes below it."
            )
    return params, warnings


def _annotate(row: dict, features: dict) -> dict:
    """Add `supported` to one catalog row, and name its feature the way status does.

    The feature name is camelCased here because the UI looks it up in the
    `features` object from GET server/status, whose keys went through the same
    conversion. A parameter with no feature works on every module version.
    """
    feature = row.get("feature")
    return {
        **row,
        "feature": to_camel_case(feature) if feature else None,
        "supported": bool(features.get(feature, True)) if feature else True,
    }


def _features_payload(features: dict) -> dict:
    """The controller's feature map in the JSON spelling, minus its bookkeeping.

    `unknown` lists the capabilities the controller assumed rather than observed.
    It is a list, not a flag, so it is dropped here rather than mixed into a map
    of booleans the UI indexes by name.
    """
    payload = camelize_keys(features)
    payload.pop("unknown", None)
    return payload


def _status_warnings(
    *,
    controller: Any,
    features: dict,
    iface: str,
    iface_up: bool,
    service: dict,
    live_port: int,
    server: store.ServerView | None,
    conf_error: str,
) -> list[str]:
    """Everything an admin should act on, worst first, as finished sentences."""
    out: list[str] = []
    if conf_error:
        out.append(conf_error)

    if not controller.available():
        out.append(
            "The AmneziaWG tools are not installed on this server, so the panel cannot read or "
            "change the running tunnel. Re-run install.sh to build them."
        )
    if not features.get("module_loaded", True):
        out.append(
            "The amneziawg kernel module is not loaded. Run 'sudo modprobe amneziawg'; if that "
            "fails the module has to be rebuilt for the running kernel, which is what re-running "
            "install.sh does."
        )
    else:
        running = features.get("module_version")
        installed = features.get("module_version_on_disk")
        if running and installed and running != installed:
            # An upgrade that could not unload the module in use. Everything on
            # this page describes the version in the kernel, not the newer one
            # sitting in /lib/modules, and the difference is the whole reason a
            # fix someone installed has not changed anything they can see.
            out.append(
                f"The kernel is running amneziawg {running}, but {installed} is installed and "
                "waiting for a reboot. The running module could not be unloaded during the "
                "upgrade, so any fix in the newer one is not in effect yet."
            )

    if not iface_up:
        out.append(
            f"The {iface} interface is down, so no client can connect. Start it with "
            f"'sudo systemctl start awg-quick@{iface}'."
        )
    elif not live_port:
        out.append(
            f"{iface} is up but is not bound to a UDP port, so nothing reaches it. Restart the "
            "interface, then check the system journal for awg-quick."
        )
    elif server is not None and server.listen_port and server.listen_port != live_port:
        out.append(
            f"The config says port {server.listen_port} but {iface} is listening on {live_port}. "
            "Restart the interface to move it; until then clients have to use "
            f"{live_port}."
        )

    # "unknown" is systemd not being there at all, e.g. in a container: nothing
    # to act on, so nothing to say.
    if service.get("enabled") == "disabled":
        out.append(
            f"awg-quick@{iface} is not enabled, so the tunnel will not come back after a reboot. "
            f"Enable it with 'sudo systemctl enable awg-quick@{iface}'."
        )

    if server is not None:
        values = dict(server.params)
        if server.mtu:
            values["MTU"] = str(server.mtu)
        # Same advisories the save bar shows, from the same catalog: a parameter
        # that breaks the Amnezia app importer is a live problem, not only a
        # problem at the moment it is set.
        out.extend(validate.warnings_for(values))

    return out


def _hooks_changed(changes: dict) -> bool:
    """True when this payload rewrites PostUp, PostDown or PreDown.

    save_server writes them without asking for a restart, because they are not
    part of the protocol and no session has to drop. They still do not take
    effect until the interface next starts, which is a different sentence and
    the admin needs to read it.
    """
    if not any(field in changes for field in HOOK_FIELDS):
        return False
    current = store.read_server()
    return any(
        field in changes and _lines(changes[field]) != _lines(getattr(current, field))
        for field in HOOK_FIELDS
    )


def _lines(value: list[str]) -> list[str]:
    """Hook lines as store keeps them: stripped, blanks dropped."""
    return [str(line).strip() for line in value if str(line).strip()]


def _listen_port() -> int:
    """The port in the config right now, or 0 if it cannot be read.

    Only used to move a firewall rule off the old port, so an unreadable config
    means "open the new one and leave whatever is there alone" rather than an
    error: the save itself will fail on its own if the config is really broken.
    """
    try:
        return store.read_server().listen_port
    except AwgError:
        return 0


def _check_listen_port(changes: dict[str, Any], port_before: int) -> None:
    """Refuse to move the tunnel onto a UDP port another service is already on.

    A port change restarts the interface, so saving an occupied port leaves the
    tunnel down and unable to bind. Refusing the save before anything is written
    keeps the config on disk valid and leaves the running tunnel where it was.

    Only a port that is actually moving is checked: an unchanged one is held by
    this tunnel, and reporting that the server is running would be a regression.
    A value that is not a valid 1..65535 port returns quietly so that
    store.save_server can produce its own validation error.
    """
    raw = changes.get("listen_port")
    if raw is None:
        return
    if isinstance(raw, int):
        port = raw
    elif isinstance(raw, str):
        raw_str = raw.strip()
        if not raw_str.isdigit():
            return
        port = int(raw_str)
    else:
        return
    if not (1 <= port <= 65535):
        return
    if port == port_before:
        return
    if not ports.busy(port, "udp"):
        return
    culprit = ports.holder(port, "udp") or "Another service"
    log.warning("listen port %d is already held by %s", port, culprit)
    raise CoreValidationError(
        {
            "listen_port": (
                f"{culprit} is already listening on {port}/udp here. Two services cannot share "
                "one UDP port, so the tunnel will not come up there. Stop it first, or "
                "choose a port nothing else is using."
            )
        }
    )


def _sync_firewall(port_before: int, warnings: list[str]) -> None:
    """Move the host firewall rule to the port the config now names.

    Best effort by design - see awg.firewall. The sentence added here is the
    point as much as the rule is: whatever the panel could not reach, a cloud
    security group above all, is the admin's next job and has to be said out
    loud rather than left for them to discover when nobody can connect.
    """
    if not port_before:
        return
    port_after = _listen_port()
    if not port_after or port_after == port_before:
        return
    backend = firewall.move_port(port_after, port_before)
    log.info(
        "listen port moved %d -> %d, firewall handled by %s",
        port_before,
        port_after,
        backend or "nothing",
    )
    warnings.append(firewall.note(backend, port_after, port_before))


def _restart_now(username: str, warnings: list[str]) -> bool:
    """Take the interface down and back up, because this change cannot go on live.

    A restart drops every session for a second or two, which is why it is worth
    a warning-level log line: the journal is where an operator looks to find out
    what interrupted their tunnel.

    A failure is not a failed save. The files on disk are already correct and
    undoing them would be worse than an interface that is one restart behind, so
    it comes back as `applied: false` and a sentence saying what to do.
    """
    controller = get_controller()
    if not controller.iface_up():
        warnings.append(
            f"The settings are saved. The {paths.iface()} interface is down, so they take "
            "effect the next time it starts."
        )
        return False
    log.warning("%s saved a change that restarts the interface", username)
    try:
        store.restart_iface(controller)
    except AwgError as exc:
        warnings.append(
            f"The settings are saved, but the tunnel would not restart: {exc} Fix that and "
            "restart it, or the interface keeps running with the old settings."
        )
        return False
    return True


def _apply_live(warnings: list[str]) -> bool:
    """Push the saved config onto the running interface, and say so if we could not.

    A failure here is not a failed save: the files on disk are already correct,
    and undoing them would be worse than an interface that is one restart behind.
    """
    try:
        if store.apply_live():
            return True
    except AwgError as exc:
        warnings.append(
            f"The settings are saved, but the running interface would not take them: {exc} "
            "Restart the interface to apply them."
        )
        return False
    warnings.append(
        f"The settings are saved. The {paths.iface()} interface is down, so they take effect the "
        "next time it starts."
    )
    return False


def _camelised(exc: CoreValidationError) -> CoreValidationError:
    """Re-key field errors to the spelling the browser sent.

    store.save_server reports against its own field names (listen_port,
    endpoint_host); the form that has to show the message rendered listenPort.
    Parameter keys - "S1", "H2" - have no underscore and come back unchanged.
    """
    return CoreValidationError({to_camel_case(key): value for key, value in exc.errors.items()})
