"""Client CRUD, config downloads and the export archive.

Every route needs an authenticated session: DRF's defaults supply
SessionAuthentication plus IsAuthenticated, and SessionAuthentication enforces
CSRF on the mutations by itself. Nothing here is exempt, downloads included -
clients/<name>/config and clients/<name>/qr hand out a private key.

A client is named in the URL because the name is the identifier the config file
carries and the client's own file is named for. The web server percent-decodes
the path before Django sees it, so the name arrives decoded and is checked
against store.NAME_RE before it is allowed anywhere near a file path.

State changes go through awg.store, which takes the config flock and writes the
formats every reader of these files expects. This module only adds the half of a client that
lives in the database, and re-reads everything through apps.clients.merge
afterwards rather than assuming its own write was the last one.
"""

import io
import logging
import time
import zipfile
from datetime import datetime
from typing import Any

import segno
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from segno.encoder import DataOverflowError

from apps.events import kinds, recorder
from apps.panel import endpoint, settings_store
from apps.panel.serializers import to_camel_case
from apps.stats import history
from apps.stats.serializers import TrafficHistorySerializer
from awg import store, traffic
from awg.errors import NAME_IN_USE, AwgError, Conflict, NotFound, ValidationError
from awg.lock import config_lock

from . import merge, shaping
from .models import ClientMeta
from .serializers import (
    ClientBulkLimitSerializer,
    ClientBulkRemoveSerializer,
    ClientCreateSerializer,
    ClientListSerializer,
    ClientQuerySerializer,
    ClientSerializer,
    ClientUpdateSerializer,
)

log = logging.getLogger(__name__)

# Big enough to scan from a phone held at arm's length, small enough that a
# config with five imitation packets still fits on screen. segno calls this
# scale; it is the same pixels-per-module the old box_size was, and the images
# come out the same size to the pixel.
QR_BOX = 6
QR_BORDER = 2

# The lowest error correction level, which buys the most capacity. A QR code on
# a screen is not a label on a crate: if a scan fails the user tries again.
QR_ERROR_CORRECTION = "l"

ARCHIVE_STAMP = "%Y%m%d%H%M%S"

# What `_enforce` did, as the event kind that says so. Keyed by the verdict it
# returns, which is also the string the client's `disabled_reason` is left
# holding - so the log and the reason a client is dark cannot drift apart.
_ENFORCED_KINDS = {
    "": kinds.CLIENT_RESTORED,
    "quota": kinds.CLIENT_QUOTA_REACHED,
    "expired": kinds.CLIENT_EXPIRED,
}


def _require_name(name: str) -> str:
    """Reject a name that cannot be a file name, before it reaches a file path.

    Deliberately not percent-decoded again: the path Django hands over has
    already been decoded once, and decoding it twice would let "%2541" name the
    client "A".
    """
    store.validate_name(name)
    return name


def _client_response(name: str, code: int = status.HTTP_200_OK) -> Response:
    """Answer with the client as it is on disk now, not as we believe we left it."""
    return Response(ClientSerializer(merge.merge_client(name)).data, status=code)


def _delivery_host() -> str:
    """The host every config leaving this panel should name, or "" to leave it as written.

    Asked once per request rather than once per config: on the export archive
    that is the difference between parsing the certificate once and parsing it
    for every client in the list.
    """
    return endpoint.wanted_host(settings_store.all_settings())


def _expiry_override(public_key: str, changes: dict[str, Any]) -> dict[str, object]:
    """What this edit leaves recorded about the client's expiry having been overruled.

    Switching a lapsed client on is the one way an admin can say "I know, and I
    want it working anyway", and it has to be written down or the collector
    switches it straight back off - which is what used to happen, and what made a
    passed date and a dark client the same fact. What is stored is the date being
    overruled, so the decision expires with it: an admin who then moves the date
    is making a new one.

    Two things clear it. Switching a client off ends the reprieve, because the
    admin is now saying the opposite. Moving the date ends it too, whether the new
    date is in the future - where there is nothing to forgive - or is another date
    in the past, which is a date nobody has been asked about yet.

    Everything else leaves the column alone rather than writing a null to it: an
    edit to a note or a quota is not a decision about the expiry, and neither is
    a request that repeats the date the client already had. An admin correcting
    somebody's email must not switch their client off as a side effect.

    The stored date is read back here rather than passed in because an edit may
    change it in the same request, and it is the value the client will have
    afterwards that is being forgiven, not the one it had a moment ago.
    """
    if "enabled" not in changes and "expires_at" not in changes:
        return {}
    stored = (
        ClientMeta.objects.filter(public_key=public_key)
        .values_list("expires_at", flat=True)
        .first()
    )
    expires_at = changes.get("expires_at", stored)

    if changes.get("enabled") and expires_at is not None and expires_at <= timezone.now():
        return {"expiry_override_at": expires_at}
    if "enabled" in changes or expires_at != stored:
        return {"expiry_override_at": None}
    return {}


def _enforce(current: store.ClientView, name: str, changes: dict[str, Any]) -> str | None:
    """Apply this edit's own verdict to the client, in whichever direction it falls.

    Changing a limit used to leave the client as it was until the collector's
    next pass came round, which on the default interval is up to a minute. The
    collector would reach exactly this conclusion, so reaching it here is not a
    second opinion - it is the same verdict, applied at the moment the admin
    changes the input to it.

    Both directions, because a limit has two of them and only one was ever
    handled here. Raising a quota freed the client while the admin was still
    looking at the screen; lowering one below what the client had already spent
    left a peer the panel drew as "quota" and the kernel went on carrying, for as
    long as the reconcile interval happened to be. That gap was the whole of the
    complaint, and it was worse than a slow control: the panel's own status
    column reads the usage rather than the interface, so it said "switched off"
    about a client that was still transferring.

    What comes back is what the client's `disabled_reason` should now say - the
    verdict for a peer this switched off, "" for one it switched back on, and
    None when it did nothing at all. The caller writes it as it stands, so the
    reason a client is dark is the same string the collector would have written
    for it and `_enforce` can find its own work again on a later edit.

    Deliberately narrow, in both directions and for the same reason: only when
    the request says nothing about `enabled`. An explicit switch is the admin
    answering the question directly, and it is handled where it is read. So
    "switch on and lower the quota past what it has spent" is still the
    collector's to settle on its next pass - the only combination left waiting,
    and one nobody performs by accident.

    The relief direction keeps a second guard the disable direction has no use
    for: only a client the panel itself switched off is eligible. That is the
    rule the collector has always applied - a peer somebody disabled deliberately
    is a decision and stays off however much quota it is given - and there is no
    counterpart going the other way, because a client that is on is on for no
    recorded reason at all.

    `_expiry_override` is consulted rather than reimplemented, because whether a
    lapsed client is forgiven decides the verdict and that rule has one home. It
    costs a second read of the row, against a block that is already rewriting the
    server config.

    The counters come from traffic.db, which the collector flushes every ten
    seconds, so this reads a figure up to that far behind the collector's own.
    Nothing here can do better - the mirror it would want lives in the other
    process - and nothing needs to: a client close enough to its limit for those
    seconds to decide the verdict is one the next pass switches off anyway. This
    removes the wait for the clients that are unambiguously over, and leaves the
    boundary exactly where it already was.
    """
    if "enabled" in changes or not any(key in changes for key in ("quota_bytes", "expires_at")):
        return None

    row = ClientMeta.objects.filter(public_key=current.public_key).first()
    if row is None:
        return None

    # The row as this edit will leave it, without saving it: the verdict is about
    # the client the admin is about to have, not the one they had.
    for key in ("quota_bytes", "expires_at"):
        if key in changes:
            setattr(row, key, changes[key])
    override = _expiry_override(current.public_key, changes)
    if "expiry_override_at" in override:
        row.expiry_override_at = override["expiry_override_at"]

    used = merge.used_bytes(row, traffic.read_db().get(current.public_key))
    verdict = merge.verdict(row, used, timezone.now())

    if current.enabled:
        if not verdict:
            return None
        store.set_client_enabled(name, False)
        log.warning("client %s disabled: the edit put it outside its limits", name)
        return verdict

    if verdict or row.disabled_reason not in merge.ENFORCED_REASONS:
        return None
    store.set_client_enabled(name, True)
    log.info("client %s re-enabled: the edit put it back inside its limits", name)
    return ""


def _shape(address: str, meta: ClientMeta, enabled: bool) -> None:
    """Put this client's bandwidth ceiling in the kernel, now that its row is written.

    Here for the same reason `_enforce` is: the collector would do exactly this
    on its next pass, and doing it in the request that changed the number means
    an operator sees the effect rather than watching a control that looks broken
    for up to a minute. It is the same verdict applied at the moment its input
    changed, not a second opinion - the reconcile still runs and still corrects
    whatever this could not.

    Which is why it does not raise. The row is already stored and the ceiling is
    what the server will converge on; a tc that failed - no iproute2 on the box,
    a kernel without the module - is a line in the log, not a 500 for an edit
    that in fact landed.

    Outside the config lock, deliberately. tc touches no file in the config
    directory, and making every other writer queue behind a netlink round trip
    would be paying for a mutex that protects nothing.
    """
    if not address:
        return
    reason = shaping.apply_client(address, meta.down_bps, meta.up_bps, enabled=enabled)
    if reason:
        log.info("the bandwidth ceiling for %s is not in force yet: %s", address, reason)


def _record_edit(
    request: Request, before: str, after: str, changes: dict[str, Any], enforced: str | None
) -> None:
    """Write down what this edit actually did, in as many lines as it did things.

    One request can be several decisions - a client renamed, switched off and
    given a new quota in one dialog - and folding them into a single "edited"
    line would lose the two that an operator would ever search for. So a rename
    is its own event and so is a switch, and everything else is one line naming
    the fields that moved.

    The rename comes first and names the client by its new name, which is the
    name every later row is about; the old one is the detail. Read in the order
    the log draws them - newest at the top - that is a client whose history goes
    on above the moment it was renamed rather than stopping there.

    Field names travel in the spelling the API uses for them, not Python's, so
    the word in the event is the word in the request that caused it.
    """
    if after != before:
        recorder.record(kinds.CLIENT_RENAMED, request, target=after, name=before)
    if "enabled" in changes:
        kind = kinds.CLIENT_ENABLED if changes["enabled"] else kinds.CLIENT_DISABLED
        recorder.record(kind, request, target=after)
    elif enforced is not None:
        # Not an "enabled" the admin asked for: the panel moving a client because
        # this edit changed what its limits say about it. It is the answer to
        # "why is this working now" and to "why did this stop", and without it
        # the only trace is a quota that changed.
        #
        # The same three kinds the collector writes for the same three verdicts,
        # because they are the same decision and an operator filtering the log
        # for clients stopped by their data limit must not have to know which of
        # the two reached it. What differs is the actor: the collector acts on
        # nobody's behalf and leaves it empty, and here there is an admin whose
        # edit is the whole reason, sitting one line below their own "updated".
        recorder.record(_ENFORCED_KINDS[enforced], request, target=after)

    fields = sorted(to_camel_case(key) for key in changes if key not in ("name", "enabled"))
    if fields:
        recorder.record(kinds.CLIENT_UPDATED, request, target=after, fields=fields)


def _name_taken(name: str) -> bool:
    """Whether a peer already answers to this name.

    Advisory only: the store re-checks under the config lock, which is the check
    that actually decides. This one exists so a doomed rename fails before the
    rest of the same request has been written.
    """
    try:
        store.get_client(name)
    except NotFound:
        return False
    return True


class ClientListView(APIView):
    """GET api/v1/clients (one page plus subnet summary), POST api/v1/clients (create)."""

    def get(self, request: Request) -> Response:
        """One page of clients, narrowed and ordered by the query string.

        A query string that will not validate is answered with the default page
        rather than with a 400. Every part of it comes from a bookmark, a shared
        URL or a link the panel wrote itself, and the useful reply to a stale
        one is the client list - not an error about a filter word that was
        removed two versions ago.
        """
        params = ClientQuerySerializer(data=request.query_params)
        query = params.to_query() if params.is_valid() else merge.ClientQuery()
        return Response(ClientListSerializer(merge.merge_clients(query)).data)

    def post(self, request: Request) -> Response:
        payload = ClientCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        # Blank or absent asks the store to name the client itself. Checked here
        # as well when there is one, so a malformed name is refused before the
        # config lock is taken rather than after.
        asked = data.get("name", "").strip()
        if asked:
            _require_name(asked)
        # A limit the request did not ask about is the server's default for new
        # clients, not zero. Absent rather than 0 is what distinguishes them: a
        # caller that means "no limit" says so with a 0 and gets one.
        default_down, default_up = shaping.default_limits()
        # Blank means "use the server default", which is what passing None to
        # the store means; an empty string would be written out literally.
        view = store.add_client(
            asked or None,
            allowed_ips=data.get("allowed_ips") or None,
            dns=data.get("dns") or None,
        )
        # From the view rather than from the request: the two are the same name
        # whenever one was asked for, and when none was it is the only place the
        # name exists at all.
        name = view.name

        meta, _ = ClientMeta.objects.update_or_create(
            public_key=view.public_key,
            defaults={
                "name": name,
                "email": data.get("email", ""),
                "note": data.get("note", ""),
                "quota_bytes": data.get("quota_bytes", 0),
                "down_bps": data.get("down_bps", default_down),
                "up_bps": data.get("up_bps", default_up),
                "expires_at": data.get("expires_at"),
                "disabled_reason": "",
                # Named so that a key reused by a client of the same name starts
                # with nobody having overruled anything, the same way it starts
                # with no usage behind it.
                "expiry_override_at": None,
                "offset_rx": 0,
                "offset_tx": 0,
                # The same instant the store just wrote into the peer's
                # "# Created" comment, taken from the comment itself rather than
                # read from the clock again, so the file and the database cannot
                # disagree about when this client was added.
                "created_at": merge.parse_created(view.created) or timezone.now(),
            },
        )
        _shape(view.ip, meta, enabled=True)
        log.info("client %s created", name)
        recorder.record(kinds.CLIENT_CREATED, request, target=name)
        return _client_response(name, status.HTTP_201_CREATED)


class ClientDetailView(APIView):
    """GET, PUT and DELETE for one client."""

    def get(self, request: Request, name: str) -> Response:
        _require_name(name)
        return _client_response(name)

    def put(self, request: Request, name: str) -> Response:
        """Apply any subset of the editable fields.

        Ordered so that everything addressed by the old name happens before the
        rename, and the database row - keyed by public key, which none of this
        changes - last.

        All of it under one lock. Each store call takes the lock for itself, so
        without this an edit that changes several things is several windows with
        gaps between them, and anything landing in a gap - another worker, the
        collector re-asserting a quota - leaves the first half applied and
        answers the admin about a client that no longer has that name.
        config_lock is re-entrant, so the calls below collapse into this one
        window and the flock is released once.

        The metadata row is written after the lock is released. It is keyed by
        public key, which nothing in the block changes, and SQLite has a writer
        of its own in the collector process - so a wait there would be paid by
        everything queued on the config lock, for a file none of them reads.
        """
        _require_name(name)
        payload = ClientUpdateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        changes = payload.validated_data
        # Kept because `name` is rebound by a rename below, and the event log
        # needs both halves to say that one happened.
        before = name

        with config_lock():
            # Resolves the name to a peer and 404s if there is none, before anything
            # is written. Also the public key the metadata row hangs off.
            current = store.get_client(name)
            meta_changes: dict[str, object] = {}
            # Whether the client this edit produces is switched on, which the
            # shaping call below needs and which three separate branches can
            # move. Seeded from the peer as it stands and corrected as each of
            # them decides, rather than re-read afterwards: the answer has to be
            # about the client the admin is about to have.
            enabled_after = current.enabled

            new_name = changes.get("name")
            if new_name and new_name != name:
                # Checked here rather than where the rename happens. A name that is
                # malformed or already taken is the likely failure in this handler,
                # and finding out about it after the peer had already been disabled
                # would leave half an edit applied with a 4xx to explain it.
                store.validate_name(new_name)
                if _name_taken(new_name):
                    raise Conflict(
                        f"a client called '{new_name}' already exists. Pick another name.",
                        NAME_IN_USE,
                    )

            # Forwarded by presence, not by truthiness. Both fields accept the
            # empty string, and for both it means "drop my own setting and take
            # the server's" - a request the store can only be given if the empty
            # string reaches it. Collapsing it to None here made an absent field
            # and a cleared one arrive as the same argument, so clearing either
            # one answered 200 and changed nothing.
            scope = {key: changes[key] for key in ("allowed_ips", "dns") if key in changes}
            if scope:
                store.update_client(name, **scope)

            if "enabled" in changes:
                enabled = bool(changes["enabled"])
                enabled_after = enabled
                store.set_client_enabled(name, enabled)
                # An admin switching a client back on overrules the collector: the
                # quota or expiry that stopped it is a decision they have just
                # reversed, and leaving the reason behind would make the next status
                # read contradict the config file.
                #
                # Only the expiry half of that reversal is written down, by
                # _expiry_override below. A date is a decision and can be
                # overruled; a quota is a measurement, and a client over its
                # limit is over it again the moment anything looks, so the way to
                # keep one of those on is to raise the limit or clear the counter.
                meta_changes["disabled_reason"] = "" if enabled else "manual"

            # Before the rename, like everything else the store is asked to do by
            # the name the request arrived with.
            #
            # The verdict is written as the reason whichever way it fell, so a
            # client this switched off carries "quota" or "expired" rather than
            # "manual" - which is not bookkeeping. `_enforce` will only ever
            # bring back a client whose reason is one of those two, so recording
            # a lowered limit as a manual decision would switch the client off
            # here and then refuse to restore it when the admin raised the limit
            # again, breaking the direction that already worked.
            enforced = _enforce(current, name, changes)
            if enforced is not None:
                meta_changes["disabled_reason"] = enforced
                enabled_after = not enforced

            if new_name and new_name != name:
                store.rename_client(name, new_name)
                log.info("client %s renamed to %s", name, new_name)
                name = new_name

            for key in ("email", "note", "quota_bytes", "expires_at", "down_bps", "up_bps"):
                if key in changes:
                    meta_changes[key] = changes[key]
            # Refreshed even when nothing else changed: it is a display cache, and a
            # rename made from the CLI is the usual reason it is out of date.
            meta_changes["name"] = name

        # Reads the row, so it waits until the flock is released with the write
        # it belongs to.
        meta_changes.update(_expiry_override(current.public_key, changes))
        meta, _ = ClientMeta.objects.update_or_create(
            public_key=current.public_key, defaults=meta_changes
        )
        # From the saved row rather than from `changes`, so an edit that left the
        # ceilings alone still applies the ones the client already had - which is
        # what a client switched back on needs, and is the same call either way.
        _shape(current.ip, meta, enabled=enabled_after)
        _record_edit(request, before, name, changes, enforced)
        # Read-only, and it re-reads every source anyway: no reason to make a
        # concurrent CLI command wait for it.
        return _client_response(name)

    def delete(self, request: Request, name: str) -> Response:
        """Remove the peer, its config file, its traffic row and its metadata."""
        _require_name(name)
        # One window for the read and the removal, so a CLI rename between them
        # cannot leave this deleting a peer the admin did not mean. The database
        # row goes after the lock is released: it is keyed by public key, and
        # waiting on SQLite - which the collector process also writes - is not
        # something everything else queued on the config lock should pay for.
        with config_lock():
            current = store.get_client(name)
            # Drops the traffic.db row and re-syncs the live interface, so the
            # key stops working now rather than at the next bring-up.
            store.remove_client(name)
        ClientMeta.objects.filter(public_key=current.public_key).delete()
        # After the row is gone, because clearing the ceiling asks whether anybody
        # still wants one before it takes the whole structure down.
        #
        # Not optional tidying. A class is derived from the client's address, and
        # the allocator hands a freed address to the next client - so a ceiling
        # left behind here is one the next client at that address inherits
        # without anybody setting it, and every command involved succeeded.
        shaping.clear_client(current.ip)
        log.info("client %s removed", name)
        recorder.record(kinds.CLIENT_DELETED, request, target=name)
        return Response(status=status.HTTP_204_NO_CONTENT)


def _expired_keys(now: datetime) -> set[str]:
    """The clients whose date has passed and has not been forgiven, by public key.

    What counts as expired is the date and only the date. `disabled_reason` also
    says "expired" for a client the collector has switched off, but that string
    outlives the reason for it - an admin who has just moved the date forward
    has a client that still reads "expired" until the collector's next pass, and
    deleting it here because of a stale marker would be deleting a client whose
    expiry is in the future. A date that has passed is a fact about the client;
    the marker is a note about a decision.

    The one lapsed client this leaves out is the one an admin has switched back
    on since. That is the same decision a sweep would undo, made about the same
    date and made explicitly, and honouring it in the collector while a bulk
    tidy-up deleted the client outright would be the worse half of both
    behaviours. Such a client is still shown as expired and can still be removed
    one at a time, which is where deleting a client anybody is deliberately
    keeping ought to happen.

    The query half of this rule is merge.sweepable_q, which is what the panel
    counts the button's promise with. The two are held together by a test that
    sweeps and compares, because a count that disagrees with the removal behind
    it is a button that says three and deletes five.
    """
    return {
        row.public_key
        for row in ClientMeta.objects.filter(expires_at__isnull=False, expires_at__lte=now)
        if not row.expiry_overridden
    }


def _spent_keys() -> set[str]:
    """The clients that have used up a data limit, by public key.

    Half of what the panel calls disabled, and the half that is not written in
    the config file: a client goes over its limit the moment the counter says
    so, and stays switched on until the collector's next pass reaches it. The
    table has always shown that client as stopped rather than online, so a sweep
    of the disabled clients that skipped it would leave behind exactly the rows
    the operator was looking at when they asked for it.

    The other half needs no query at all - it is the "# Disabled" marker in the
    config, which the peer list carries.

    The query form of both halves together is merge.OFF.
    """
    counters = traffic.read_db()
    return {
        row.public_key
        for row in ClientMeta.objects.filter(quota_bytes__gt=0)
        if merge.used_bytes(row, counters.get(row.public_key)) >= row.quota_bytes
    }


def _sweep(expired: bool, disabled: bool) -> list[str]:
    """Delete whole categories of client at once, and report what actually went.

    The same removal the delete handler performs, for a set at a time: the peers
    leave the server config, their files and traffic rows go with them and the
    interface is re-applied. Doing that one request at a time is one config
    rewrite and one `awg syncconf` per client, so the sweep goes through
    store.remove_clients, which pays each of those once.

    Both selections are read inside a single lock window, so the config these
    names came out of is the config they are removed from. Read outside it, a
    rename landing in between would leave this asking for a name that no longer
    exists - and, worse, leave a renamed client's metadata row deleted under a
    name that was never selected at all.

    Whether a peer is switched off is taken from the config rather than from the
    index, because that file is the one thing here that cannot be out of date:
    the sweep is already holding it open.
    """
    now = timezone.now()
    with config_lock():
        lapsed = _expired_keys(now) if expired else set()
        spent = _spent_keys() if disabled else set()
        # The address is taken here alongside the key, in the one pass over the
        # config that was already being made: the peers are about to leave that
        # file, and the address is the only thing their place in the shaping
        # structure is keyed by, so afterwards there is nothing left to read it
        # from.
        doomed: dict[str, tuple[str, str]] = {
            view.name: (view.public_key, view.ip)
            for view in store.list_clients()
            if view.name
            and (
                view.public_key in lapsed
                or (disabled and (not view.enabled or view.public_key in spent))
            )
        }
        removed = store.remove_clients(doomed.keys())

    # Keyed by public key, and settled in one statement rather than one per
    # client - the same reason the peers left the config together. Outside the
    # lock, like the delete handler's: SQLite has a writer of its own in the
    # collector process, and a wait there would be paid by everything queued on
    # the config lock, for a file none of them reads.
    if removed:
        ClientMeta.objects.filter(public_key__in=[doomed[name][0] for name in removed]).delete()
        # One batch rather than one process per client, for the same reason the
        # peers left the config together: a sweep of five hundred clients is
        # three thousand tc commands, and as separate invocations that is a
        # minute of the request an operator is watching.
        shaping.clear_clients([doomed[name][1] for name in removed if doomed[name][1]])
        log.info("removed %d client(s) in bulk: %s", len(removed), ", ".join(removed))
    return removed


def _record_sweep(request: Request, removed: list[str], *, expired: bool, disabled: bool) -> None:
    """One event for the whole sweep, naming the categories rather than the clients.

    One row and not one per client, because a sweep of five hundred is one thing
    an operator did and five hundred rows of it would bury every other event of
    that afternoon under it - and because the ledger has a ceiling, so a single
    large sweep could otherwise push a season of history out of the table.

    Which categories were asked for is recorded instead of which names went, and
    that is the honest half of the pair: the names are capped by what a detail
    may hold, so a list of them would be complete on a sweep of three and
    silently truncated on a sweep of thirty. The categories are exact at any
    size, and the count says how far they reached.

    A sweep that took nothing writes nothing. The operator pressed the button and
    the answer was "there was nothing to remove", which is not an event about
    this server - it is the reply they already read.
    """
    if not removed:
        return
    recorder.record(
        kinds.CLIENT_BULK_DELETED,
        request,
        count=len(removed),
        expired=expired,
        disabled=disabled,
    )


class ClientRemoveExpiredView(APIView):
    """POST api/v1/clients/remove-expired - delete every client whose date has passed.

    Kept as its own route, and now the narrowest way to ask what
    `clients/bulk-remove` does: this is that endpoint with `expired` set and
    nothing else. Callers written against it go on working unchanged, which is
    the point - it is documented, and a panel is not the only thing that calls
    an API.

    The answer names what was actually removed rather than repeating what was
    asked, because the two can differ: the browser counted rows from a list it
    fetched up to half a minute ago, and a client removed over SSH in the
    meantime is not in this count.
    """

    def post(self, request: Request) -> Response:
        removed = _sweep(expired=True, disabled=False)
        _record_sweep(request, removed, expired=True, disabled=False)
        return Response({"removed": removed, "count": len(removed)})


class ClientBulkRemoveView(APIView):
    """POST api/v1/clients/bulk-remove - delete the expired clients, the stopped ones, or both.

    One request for the tidy-up an operator otherwise does a row at a time.
    Which clients go is chosen by category rather than by name, because the
    categories are what the operator can see - the list filters by the same two
    words - and because a list of names gathered in the browser is a list that
    was already out of date when it was sent.

    The two categories:

    * `expired` - the date has passed, exactly as `clients/remove-expired`
      reads it, override and all.
    * `disabled` - the client is not working: switched off by an admin, switched
      off by the collector for its quota or its date, or still switched on and
      already over its data limit. That last one belongs here because the table
      has always shown it as stopped, and a sweep that left it behind would
      leave the operator looking at the rows they had just asked to be rid of.

    Asking for both is a union, not two passes: a lapsed client the collector
    has already switched off is one client and is removed once. So the count in
    the reply is not the sum of the two the list reports wherever those overlap,
    which is why the panel is given a third figure to promise rather than left
    to add them up - see merge._sweep_counts.

    Naming neither category is refused rather than answered with an empty
    removal, because "remove nothing" and "my flag never arrived" deserve
    different replies.
    """

    def post(self, request: Request) -> Response:
        payload = ClientBulkRemoveSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        expired = payload.validated_data["expired"]
        disabled = payload.validated_data["disabled"]
        if not expired and not disabled:
            raise ValidationError(
                "Say which clients to remove: set 'expired', 'disabled' or both to true."
            )

        removed = _sweep(expired=expired, disabled=disabled)
        _record_sweep(request, removed, expired=expired, disabled=disabled)
        return Response({"removed": removed, "count": len(removed)})


class ClientBulkLimitView(APIView):
    """POST api/v1/clients/bulk-limit - give every client the same bandwidth limit.

    The one operation the per-client form cannot express: an operator who has
    decided everybody gets 20 Mbit should not have to open four thousand
    dialogs, and the "default for new clients" setting deliberately does not
    reach backwards into the clients already here.

    It overwrites. Whatever any client had - a limit set by hand last week, no
    limit at all - is replaced by what this request carries, and there is nothing
    that puts the old numbers back. The panel asks before calling it and says how
    many rows it is about to change; this endpoint does not ask again, because a
    confirmation the caller cannot see is not a safeguard.

    `changed` counts the rows that actually moved, so a second identical call
    answers 0 rather than the size of the server. `applied` is false when the
    numbers were stored but the kernel could not be brought into line - a host
    without iproute2, a tunnel that is down - which is the same split every other
    limit-setting path here reports, and for the same reason: the panel's record
    of what a client should have does not depend on anything being up to enforce
    it.
    """

    def post(self, request: Request) -> Response:
        payload = ClientBulkLimitSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        down = payload.validated_data["down_bps"]
        up = payload.validated_data["up_bps"]

        changed, reason = shaping.apply_to_all(down, up)
        log.info("every client's bandwidth limit was set to %d/%d bit/s", down, up)
        # Recorded even when no row moved. The operator made a decision about
        # every client on the server, and "it was already what you asked for" is
        # a fact about the clients rather than about whether it happened.
        recorder.record(kinds.CLIENT_BULK_LIMITED, request, count=changed, down=down, up=up)
        return Response({"changed": changed, "applied": not reason, "reason": reason})


class ClientConfigView(APIView):
    """GET api/v1/clients/<name>/config - the config file, private key included.

    The text is the file on disk with one substitution: apps.panel.endpoint may
    replace the host in the Endpoint line with the panel's certificate domain.
    Nothing is written back, so the file on disk and the backup archive both keep
    saying what the server was actually configured with.
    """

    def get(self, request: Request, name: str) -> HttpResponse:
        _require_name(name)
        # A file left behind by something else is not a client. Resolving the
        # peer first means a config can only be downloaded for a key the server
        # is currently willing to accept.
        store.get_client(name)
        text = endpoint.rewrite(store.client_conf_text(name), _delivery_host())
        response = HttpResponse(text, content_type="text/plain; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{name}.conf"'
        response["Cache-Control"] = "no-store"
        return response


class ClientQrView(APIView):
    """GET api/v1/clients/<name>/qr - the same config as a PNG for the mobile app."""

    def get(self, request: Request, name: str) -> HttpResponse:
        _require_name(name)
        store.get_client(name)
        # The same text the download hands over, so a phone that scans and a
        # laptop that downloads end up on the same address.
        text = endpoint.rewrite(store.client_conf_text(name), _delivery_host())
        response = HttpResponse(_qr_png(text), content_type="image/png")
        # The image is the private key in another form. It must not sit in a
        # disk cache or a proxy after the tab is closed.
        response["Cache-Control"] = "no-store, max-age=0"
        response["Content-Disposition"] = f'inline; filename="{name}.png"'
        return response


class ClientResetKeysView(APIView):
    """POST api/v1/clients/<name>/reset-keys - new keypair and preshared key."""

    def post(self, request: Request, name: str) -> Response:
        _require_name(name)
        before = store.get_client(name)

        # The metadata is keyed by public key, and for the moment between the
        # new key reaching the config and the row following it, the row looks
        # stale to the pruner in merge. Touching it first puts it inside the
        # grace period so a dashboard poll landing in that window cannot delete
        # somebody's quota.
        now = timezone.now()
        rows = ClientMeta.objects.filter(public_key=before.public_key)
        rows.update(updated_at=now)

        after = store.reset_client_keys(name)
        rows.update(public_key=after.public_key, updated_at=now)
        # Nothing else needs carrying across. Traffic history used to be keyed
        # by public key and had to be moved here by hand, for the same reason
        # store._rekey_traffic moves the all-time totals: to the person reading
        # the dashboard this is one client that got new keys, not a new client.
        # The server's daily totals are not per client at all, and a client's
        # own daily rows hang off the metadata row this line has just rewritten
        # rather than off the key in it - so both follow the rotation without
        # anything being said here. See apps.stats.models.ClientDaily.
        log.info("client %s issued new keys", name)
        recorder.record(kinds.CLIENT_KEYS_RESET, request, target=name)
        return _client_response(name)


class ClientResetUsageView(APIView):
    """POST api/v1/clients/<name>/reset-usage - clear one client's traffic figures.

    Both of them, because to the admin pressing the button there is only one:
    the all-time totals the list shows, and the days and months behind the
    history chart. This used to take the first and leave the second, on the
    grounds that a day's record is a fact about a date that has passed - which
    is true of the *server's* day and is why DailyTotal is still not touched
    here, but was never what an operator handing a client on to somebody else
    was asking for. What they want is a client that starts from nothing, and a
    chart still showing last month's evenings is the opposite of that.

    traffic.db is left alone even so, so the file keeps the true all-time figure
    and no reset can destroy the only record of what a peer has ever moved; what
    the panel reports is that figure less a stored offset. The history has no
    such second copy, so clearing it is a deletion, and the confirmation in the
    browser says so before anything happens.
    """

    def post(self, request: Request, name: str) -> Response:
        _require_name(name)
        current = store.get_client(name)
        counters = traffic.read_db().get(current.public_key)

        now = timezone.now()
        meta, _ = ClientMeta.objects.update_or_create(
            public_key=current.public_key,
            defaults={
                "name": name,
                "offset_rx": counters.cum_rx if counters else 0,
                "offset_tx": counters.cum_tx if counters else 0,
                # Stamped whether or not there were any rows to take: what the
                # collector reads it for is the moment its own copy of today
                # stopped being something it may write back.
                "history_reset_at": now,
            },
        )
        removed = history.clear_client(meta.pk)

        # A client the collector switched off for its quota has nothing left to
        # enforce once the counter is back at zero. Leaving it dark would look
        # like the reset had not worked.
        if not current.enabled and meta.disabled_reason == "quota":
            store.set_client_enabled(name, True)
            ClientMeta.objects.filter(pk=meta.pk).update(disabled_reason="")
            # Its ceiling comes back with it: a peer that is switched off holds
            # no class, so one that has just been switched on needs its put back.
            _shape(current.ip, meta, enabled=True)

        log.info("client %s usage counters reset, %d history row(s) removed", name, removed)
        recorder.record(kinds.CLIENT_USAGE_RESET, request, target=name)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ClientTrafficView(APIView):
    """GET api/v1/clients/<name>/traffic - what this client carried, by day and by month.

    The same two series and the same shape as `stats/traffic`, narrowed to one
    peer, because the panel draws both with one chart and the two answers have
    to be interchangeable.

    Under clients/ rather than stats/ because it is addressed the way every
    other per-client route is: by the name in the config, resolved through
    `store.get_client`, which is what turns a name an admin typed into the
    public key the history is hung off. A route under stats/ would have to take
    a public key, which is not what the panel or the operator has in hand, and
    would need percent-encoding for the base64 to survive a path segment.

    Read-only and unlocked. It touches the config only to resolve the name, and
    the rows behind it are the collector's - a page left open on this dialog
    costs one small query on the list's clock.

    `?from=` and `?to=` narrow the window, parsed by the same code as the
    server's route and answering the same shape, so the range control in the
    dialog is the one on the statistics page. What differs is how far back
    either can reach: these rows are swept after CLIENT_HISTORY_DAYS, and
    apps.stats.history clamps the window to what is left rather than answering
    with zeroes that would read as quiet months.
    """

    def get(self, request: Request, name: str) -> Response:
        _require_name(name)
        current = store.get_client(name)
        window = history.parse_window(request.query_params)
        series = history.client_series(current.public_key, window=window)
        return Response(TrafficHistorySerializer(series).data)


class ClientExportView(APIView):
    """GET api/v1/clients/export.zip - every client config in one archive.

    Not taken under the config lock: each file is replaced atomically, so a
    reader never sees half of one, and holding the lock while a couple of
    hundred files are read would stall every other writer for no real gain. A
    client added while the archive is being built is simply in the next one.

    Every entry goes through the same endpoint substitution as a single
    download, resolved once for the whole archive rather than per file.
    """

    def get(self, request: Request) -> HttpResponse:
        buffer = io.BytesIO()
        written = 0
        host = _delivery_host()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for view in store.list_clients():
                if not view.name or not view.has_conf_file:
                    continue
                try:
                    text = endpoint.rewrite(store.client_conf_text(view.name), host)
                except NotFound:
                    continue  # removed between listing and reading
                archive.writestr(f"{view.name}.conf", text)
                written += 1

        if not written:
            raise NotFound(
                "There are no client configs to export. Add a client first, or run "
                "'awg-panel manage resync' if the clients exist but their files are missing."
            )

        response = HttpResponse(buffer.getvalue(), content_type="application/zip")
        filename = f"awg-clients-{time.strftime(ARCHIVE_STAMP, time.gmtime())}.zip"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Cache-Control"] = "no-store"
        log.info("exported %d client config(s)", written)
        recorder.record(kinds.CLIENT_EXPORTED, request, count=written)
        return response


def _qr_png(text: str) -> bytes:
    """Render a client config as a QR code.

    An AmneziaWG config with all five imitation packets can run past what a QR
    code will hold. That is a real configuration, not a mistake, so it gets an
    answer that says what to do instead of a traceback.
    """
    try:
        code = segno.make(text, error=QR_ERROR_CORRECTION, micro=False)
    except DataOverflowError as exc:
        raise AwgError(
            "This configuration is too long to fit in a QR code. Download the .conf file and "
            "import it into the app from a file instead."
        ) from exc

    buffer = io.BytesIO()
    code.save(buffer, kind="png", scale=QR_BOX, border=QR_BORDER)
    return buffer.getvalue()
