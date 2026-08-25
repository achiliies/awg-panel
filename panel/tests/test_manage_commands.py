"""The `awg-panel manage` commands - the shell's only way into the panel.

Three of them are the tunnel's own hooks. `enforce` and `shape` are the
config's two PostUps and `trafficsync` is its PreDown, which means all of them
run from `awg-quick` at boot and at shutdown: no web service up, no admin
watching, no output anybody reads. The other two are what install.sh calls to
create the first client and to re-issue every config after an upgrade that
changed something clients can see.

Nothing else covers them. They were added when the client CLI was retired and
its four jobs moved here, and a command with no test that only ever runs
unattended is one that can stop working for a release without anybody noticing:
the counters go missing on restart, or the revoked clients come back at boot,
and the first report is a bill or a breach.

`accountname` is the odd one out: nothing calls it for its effect, because it has
none. It answers "what is this panel's account called" for the shell wrapper, so
that `awg-panel passwd` finds an account that has been renamed from Settings
instead of assuming it is still `admin` - which makes its output the whole of its
contract, and a wrong answer the difference between recovering a locked-out panel
and not.

What is asserted throughout is the exit status as much as the effect. Both hooks
are written into the config with `|| true` after them, so a failure here cannot
take the tunnel down - which is right, and is also exactly why a broken one is
silent.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.clients.models import ClientMeta
from apps.panel import settings_store
from apps.stats.models import utc_day
from awg import names, shaper, store, traffic
from awg.controller import get_controller, reset_controller
from awg.paths import client_dir, traffic_db

pytestmark = pytest.mark.django_db

CLIENT1_KEY = "1e6gtjXOkZIBabwDiZ9M10rGc6Z4D0whZvSvvx2TfWQ="
DISABLED_KEY = "edfiWfM06eSgGaJASvpbKkw2mQ3k87ZCrKf+2jWf1h4="


@pytest.fixture(autouse=True)
def fresh_controller():
    """The controller is a process-wide singleton; each test gets its own mock."""
    reset_controller()
    yield
    reset_controller()


def run(command: str, *args: str) -> str:
    """Call a management command and hand back what it printed."""
    from io import StringIO

    out = StringIO()
    call_command(command, *args, stdout=out)
    return out.getvalue()


# ------------------------------------------------------------------ addclient


def test_addclient_writes_a_peer_and_a_config(server_conf: Path) -> None:
    """What install.sh calls to create the first client on a fresh server."""
    out = run("addclient", "phone")

    view = store.get_client("phone")
    assert view.name == "phone"
    assert view.ip in out

    config = client_dir() / "phone.conf"
    assert config.is_file()
    assert config.stat().st_mode & 0o777 == 0o600
    text = config.read_text(encoding="utf-8")
    assert "PrivateKey" in text and "[Peer]" in text
    assert str(config) in out


def test_addclient_quiet_prints_the_path_and_nothing_else(server_conf: Path) -> None:
    """For a script that wants to read the file it just made."""
    out = run("addclient", "phone", "--quiet")
    assert out.strip() == str(client_dir() / "phone.conf")


def test_addclient_never_puts_the_private_key_on_stdout(server_conf: Path) -> None:
    """The installer's output is scrolled through, logged and pasted into issues."""
    out = run("addclient", "phone")
    key = next(
        line.split("=", 1)[1].strip()
        for line in store.client_conf_text("phone").splitlines()
        if line.startswith("PrivateKey")
    )
    assert key not in out


def test_addclient_with_no_name_draws_one(server_conf: Path) -> None:
    """The same name the panel's form would have suggested, for a shell that has no form."""
    out = run("addclient")

    name = out.split()[1]
    assert len(name) == names.LENGTH
    assert store.get_client(name).name == name
    assert (client_dir() / f"{name}.conf").is_file()


def test_addclient_refuses_a_duplicate_name(server_conf: Path) -> None:
    run("addclient", "phone")
    with pytest.raises(CommandError, match="already exists"):
        run("addclient", "phone")


def test_addclient_refuses_a_name_that_would_escape_the_directory(server_conf: Path) -> None:
    """The name becomes a file name, so this is a path traversal and not a typo."""
    with pytest.raises(CommandError):
        run("addclient", "../../etc/passwd")


def test_addclient_says_so_when_there_is_no_server_yet(conf_dir: Path) -> None:
    """A sentence, not a traceback: an admin runs this when things are wrong."""
    with pytest.raises(CommandError, match="no server configuration"):
        run("addclient", "phone")


# --------------------------------------------------------------------- resync


def test_resync_rewrites_a_config_that_no_longer_matches(server_conf: Path) -> None:
    """The upgrade path: a server-side value moved, every issued config must catch up.

    The endpoint is the one that matters most - it lives in each client's own
    [Peer] block, so a moved server with unrebuilt configs is a fleet dialling an
    address that no longer answers, and nothing else reaches the devices.
    """
    run("addclient", "phone")
    config = client_dir() / "phone.conf"
    config.write_text(
        config.read_text(encoding="utf-8").replace("203.0.113.10", "198.51.100.99"),
        encoding="utf-8",
    )

    out = run("resync")

    assert "1" in out
    assert "203.0.113.10" in config.read_text(encoding="utf-8")


def test_resync_leaves_a_config_that_is_already_right_alone(server_conf: Path) -> None:
    """Re-running the installer lands here with nothing to do, and must cost nothing.

    Reported as zero rather than as the client count, because "re-issued 1
    config" after a no-op is what makes somebody re-import a config that never
    changed.
    """
    run("addclient", "phone")
    config = client_dir() / "phone.conf"
    before = config.read_bytes()
    mtime = config.stat().st_mtime_ns

    out = run("resync")

    assert "0" in out
    assert config.read_bytes() == before
    assert config.stat().st_mtime_ns == mtime, "an unchanged config was rewritten"


def test_resync_dns_replaces_what_every_client_had(server_conf: Path) -> None:
    run("addclient", "phone")
    run("resync", "--dns", "9.9.9.9")
    assert "DNS = 9.9.9.9" in (client_dir() / "phone.conf").read_text(encoding="utf-8")


def test_resync_keeps_the_private_key_each_client_already_holds(server_conf: Path) -> None:
    """A re-issue the devices have to import is bad enough; a new key is worse."""
    run("addclient", "phone")
    config = client_dir() / "phone.conf"
    before = next(
        line for line in config.read_text(encoding="utf-8").splitlines() if "PrivateKey" in line
    )
    run("resync", "--dns", "9.9.9.9")
    after = next(
        line for line in config.read_text(encoding="utf-8").splitlines() if "PrivateKey" in line
    )
    assert before == after


def test_resync_says_so_when_there_is_no_server_yet(conf_dir: Path) -> None:
    with pytest.raises(CommandError, match="no server configuration"):
        run("resync")


# -------------------------------------------------------------------- enforce


def spy_on_syncconf(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every configuration pushed to the interface, and still push it."""
    controller = get_controller()
    pushed: list[str] = []
    original = controller.syncconf

    def record(stripped_text: str) -> None:
        pushed.append(stripped_text)
        original(stripped_text)

    monkeypatch.setattr(controller, "syncconf", record)
    return pushed


def test_enforce_pushes_a_configuration_the_disabled_peer_is_not_in(
    server_conf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PostUp hook's whole job.

    `awg-quick up` loads the config into the kernel, disabled peers included -
    the entry is what reserves the address - so without this every bring-up, boot
    included, re-admits everyone a quota, an expiry or an admin switched off.
    What closes that window is this: the stripped configuration, which drops the
    marked peers, pushed over the top of what the bring-up just loaded.
    """
    pushed = spy_on_syncconf(monkeypatch)

    out = run("enforce")

    assert "1" in out
    assert len(pushed) == 1
    assert DISABLED_KEY not in pushed[0]
    assert CLIENT1_KEY in pushed[0], "an enabled client was revoked too"
    live = {peer.public_key for peer in get_controller().show_dump().peers}
    assert DISABLED_KEY not in live


def test_enforce_does_nothing_when_nothing_is_disabled(
    server_conf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overwhelmingly common case, and it runs on every single bring-up.

    A syncconf on a server with thousands of peers is not free, so the early
    return is the point: no disabled peer means the kernel is not touched at all.
    """
    server_conf.write_text(
        "\n".join(
            line
            for line in server_conf.read_text(encoding="utf-8").splitlines()
            if not line.startswith("# Disabled")
        )
        + "\n",
        encoding="utf-8",
    )
    pushed = spy_on_syncconf(monkeypatch)

    out = run("enforce")

    assert "nothing disabled" in out
    assert pushed == [], "the interface was re-synced with nothing to revoke"


def test_enforce_survives_a_downed_interface(server_conf: Path) -> None:
    """It fires from PostUp, so the tunnel is normally up - but not always.

    Run by hand, or by a hook on an interface that failed to come up, there is
    nothing to push to. That is not a failure: the marker is still in the file
    and whatever brings the interface up next loads it through this same path.
    """
    controller = get_controller()
    controller._up = False

    out = run("enforce")

    assert "interface down" in out


def test_enforce_is_quiet_and_successful_on_a_box_with_no_tunnel(conf_dir: Path) -> None:
    """Unlike the other three, this one must not raise when unconfigured.

    It is wired into a config that has to exist before it can fire, so reaching
    here by hand on a fresh install is not an error worth an exit status.
    """
    out = run("enforce")
    assert "no server configuration" in out


# ---------------------------------------------------------------- trafficsync


def test_trafficsync_folds_the_kernels_counters_into_traffic_db(server_conf: Path) -> None:
    """The PreDown hook's whole job: the counters die with the interface."""
    dump = get_controller().show_dump()
    assert dump is not None

    out = run("trafficsync")

    assert traffic_db().is_file()
    stored = traffic.read_db()
    assert CLIENT1_KEY in stored
    assert str(len(dump.transfers())) in out


def test_trafficsync_keeps_the_totals_across_a_counter_reset(server_conf: Path) -> None:
    """What the file exists for. A restart zeroes the kernel; the history must not.

    The hook folds as an interface is going down, so the raw counters it records
    are the last of that epoch. Nothing on a test machine publishes an epoch to
    compare against, which is exactly the case the hook's own signal exists for:
    it leaves the stored raw values at zero, and the next reading is counted in
    full rather than diffed against counters that no longer mean anything.
    """
    controller = get_controller()
    run("trafficsync")
    first = traffic.read_db()[CLIENT1_KEY]
    assert first.cum_rx > 0, "the mock handed out no traffic to account for"
    assert first.last_rx == 0, "the fold did not end the epoch it was told was ending"

    # The kernel's counters really do go back to zero here, and the mock's do too.
    controller.restart()
    run("trafficsync")
    second = traffic.read_db()[CLIENT1_KEY]

    assert second.cum_rx >= first.cum_rx, "an interface restart lost this client's history"
    assert second.cum_tx >= first.cum_tx


def test_trafficsync_counts_a_busy_peer_in_full_after_a_restart(server_conf: Path) -> None:
    """The undercount the epoch exists to stop.

    A peer that moves more after the restart than it had moved before it hands
    the next fold a raw counter above the stored one, so nothing about the
    reading itself says the interface restarted. Diffing against the stored
    value there does not lose a little: it loses the whole of the peer's
    previous epoch, silently, on the one peer moving the most traffic.
    """
    controller = get_controller()
    run("trafficsync")
    before = traffic.read_db()[CLIENT1_KEY]

    controller.restart()
    # Above whatever the peer had reached before the restart, whichever way the
    # mock happened to number it.
    busy = (before.cum_rx * 2 + 1000, before.cum_tx * 2 + 1000)
    traffic.sync({CLIENT1_KEY: busy})
    after = traffic.read_db()[CLIENT1_KEY]

    assert after.cum_rx == before.cum_rx + busy[0], "the peer's earlier epoch was diffed away"
    assert after.cum_tx == before.cum_tx + busy[1]


def test_trafficsync_is_quiet_and_successful_when_the_interface_is_gone(server_conf: Path) -> None:
    """PreDown can fire after the interface has already gone.

    Nothing has been lost that this could have saved - the counters went with it
    - and the last good reading is still in the file, so there is nothing to
    report and nothing to fail.
    """
    get_controller()._up = False
    out = run("trafficsync")
    assert "not up" in out
    assert not traffic_db().exists() or traffic.read_db() == {}


# ----------------------------------------------------------------------- shape
#
# The other PostUp, and the one whose absence is hardest to notice: a tunnel
# comes up perfectly, every client connects, and nobody is held to the rate they
# were sold. tc rules belong to the network device, so every one of them goes
# when `awg-quick` deletes the interface and this is what puts them back.


def shapeable(monkeypatch) -> list[list[str]]:
    """A host that can shape, with tc recorded rather than run."""
    calls: list[list[str]] = []

    class Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake(cmd, **kwargs):
        calls.append([*cmd, *(kwargs.get("input") or "").splitlines()])
        return Proc()

    monkeypatch.delenv("AWG_MOCK", raising=False)
    monkeypatch.setattr(shaper.shutil, "which", lambda name: f"/usr/sbin/{name}")
    monkeypatch.setattr(shaper.subprocess, "run", fake)
    return calls


def test_shape_puts_every_ceiling_back_on_the_interface(server_conf: Path, monkeypatch) -> None:
    calls = shapeable(monkeypatch)
    settings_store.set_many({"shaperOn": "1"})
    view = store.add_client("phone")
    ClientMeta.objects.update_or_create(
        public_key=view.public_key, defaults={"down_bps": 10_000_000}
    )

    out = run("shape")

    flat = " ".join(word for call in calls for word in call)
    assert "applied 1 bandwidth limit" in out
    assert "qdisc replace dev awg0 root" in flat
    assert "rate 10000000bit" in flat


def test_shape_says_so_and_touches_nothing_when_limits_are_off(
    server_conf: Path, monkeypatch
) -> None:
    """Which is every server that has not asked for this: nothing is attached at all."""
    calls = shapeable(monkeypatch)
    out = run("shape")
    assert "off" in out
    assert not [call for call in calls if "replace" in " ".join(call)]


def test_shape_says_so_when_no_client_has_a_limit(server_conf: Path, monkeypatch) -> None:
    shapeable(monkeypatch)
    settings_store.set_many({"shaperOn": "1"})
    store.add_client("phone")
    assert "no client" in run("shape")


def test_shape_detach_removes_the_structure_from_both_interfaces(
    server_conf: Path, monkeypatch
) -> None:
    """PostDown's job, and it must not consult the tunnel first.

    By the time PostDown fires the tunnel device has been deleted, so anything
    that asked whether the tunnel is shaped would answer no - while the WAN's own
    root, which nothing deleted, is still queueing everything this machine sends.
    """
    calls = shapeable(monkeypatch)
    settings_store.set_many({"shaperOn": "1", "shaperUpload": "1", "shaperWanIface": "ens3"})

    assert "removed" in run("shape", "--detach")

    flat = [" ".join(call) for call in calls]
    assert any("qdisc del dev awg0 root" in line for line in flat)
    assert any("qdisc del dev ens3 root" in line for line in flat)


def test_shape_is_quiet_on_a_host_that_cannot_shape(server_conf: Path) -> None:
    """AWG_MOCK is on for the suite, which is the developer's laptop case."""
    assert "AWG_MOCK" in run("shape")


def test_shape_says_so_when_there_is_no_server_yet(conf_dir: Path, monkeypatch) -> None:
    """Like enforce: the hook lives in a config that has to exist before it fires."""
    shapeable(monkeypatch)
    assert "no server configuration" in run("shape")


# ------------------------------------------------------------------ seeddemo


def test_seeddemo_fills_a_mock_panel_with_a_year_of_history(server_conf: Path) -> None:
    """The command `make demo` runs. Its job is charts worth looking at, so what
    is asserted is that the history it writes covers the window the charts draw
    rather than merely that some rows appeared."""
    from apps.stats.models import CLIENT_HISTORY_DAYS, ClientDaily, DailyTotal, utc_day

    printed = run("seeddemo")

    today = utc_day(timezone.now())
    oldest = DailyTotal.objects.order_by("day").first().day
    assert (today - oldest).days > 360, "the monthly chart would have empty columns"
    assert (
        ClientDaily.objects.filter(day__lt=today - timedelta(days=CLIENT_HISTORY_DAYS)).count() == 0
    )
    # Every client the charts are meant to demonstrate, and a password to sign
    # in with - the one line somebody actually reads off the terminal.
    for name in ("alice-laptop", "dave-old", "erin-new"):
        assert name in printed
    assert "sign in as" in printed


def test_seeddemo_is_idempotent(server_conf: Path) -> None:
    """Run twice, it re-seeds rather than stacking a second year on the first."""
    from apps.stats.models import ClientDaily

    run("seeddemo")
    first = ClientDaily.objects.count()
    run("seeddemo")

    assert ClientDaily.objects.count() == first


def test_seeddemo_generates_a_different_password_every_run(server_conf: Path) -> None:
    """`make demo` binds to every interface, so a password baked into the
    repository would ship the same credentials to every machine it runs on."""
    assert run("seeddemo").split("sign in as")[1] != run("seeddemo").split("sign in as")[1]


def test_the_generated_password_is_letters_and_digits_only() -> None:
    """It is read off one screen and typed on another, so "-" and "_" are not
    worth the two bits they carry.

    Many samples and the generator directly, rather than a few runs of the
    command. base64url only reaches for punctuation about two times in five, so
    a test that seeded a handful of demos would have passed on the old alphabet
    often enough to be no test at all.
    """
    from apps.stats.management.commands.seeddemo import _generated_password

    for _ in range(500):
        password = _generated_password()
        assert password.isalnum(), password
        assert password.isascii(), password
        assert len(password) == 16, password


def test_the_printed_password_is_the_generated_one(server_conf: Path) -> None:
    """The alphabet above is only worth pinning if it is what reaches the screen
    somebody types from."""
    printed = run("seeddemo").split("sign in as")[1].split("/")[1].strip()

    assert printed.isalnum() and len(printed) == 16, printed


def test_seeddemo_refuses_to_touch_a_real_server(server_conf: Path, monkeypatch) -> None:
    """It deletes the traffic history and adds peers to the server config. On a
    box carrying real clients that is somewhere between rude and destructive, so
    the mock flag is checked before anything at all is written."""
    from apps.stats.models import DailyTotal

    DailyTotal.objects.create(day=utc_day(timezone.now()), rx=99, tx=99)
    monkeypatch.delenv("AWG_MOCK", raising=False)

    with pytest.raises(CommandError, match="AWG_MOCK"):
        run("seeddemo")

    assert DailyTotal.objects.count() == 1, "it wrote before it checked"


def test_seeddemo_leaves_the_all_time_totals_above_the_history(server_conf: Path) -> None:
    """The figures on one screen must not contradict each other.

    The dashboard shows an all-time card above a chart of the last thirty days
    and twelve months. Seeding the history without the counters behind it put a
    terabyte in the chart over a card reading eleven gigabytes, which is not a
    state any real server can be in and reads as a bug in the panel rather than
    in its demo data.
    """
    from apps.stats.models import ClientDaily, DailyTotal

    run("seeddemo")

    counters = traffic.read_db()
    all_time = sum(row.cum_rx + row.cum_tx for row in counters.values())
    history = sum(row.rx + row.tx for row in ClientDaily.objects.all())

    assert all_time >= history, "the all-time card would read less than the charts under it"
    # The server's day is the sum of its clients' days, so no window of the
    # chart can ever exceed the card either.
    served = sum(row.rx + row.tx for row in DailyTotal.objects.all())
    assert all_time >= served


def test_seeddemo_gives_the_server_the_sum_of_its_clients_days(server_conf: Path) -> None:
    """One pass over the deltas, counted twice - which is how the collector
    arrives at the two tables, and the only way the two charts can agree about
    the same day."""
    from django.db.models import Sum

    from apps.stats.models import ClientDaily, DailyTotal, utc_day

    run("seeddemo")
    today = utc_day(timezone.now())

    clients = ClientDaily.objects.filter(day=today).aggregate(rx=Sum("rx"), tx=Sum("tx"))
    server = DailyTotal.objects.get(day=today)

    assert (server.rx, server.tx) == (clients["rx"], clients["tx"])


def test_seeddemo_covers_the_small_and_empty_cases(server_conf: Path) -> None:
    """A demo of nothing but terabytes cannot show that a two megabyte client is
    legible, or that a client which has never connected draws an empty chart
    rather than a broken one. Both are switched off, which is what holds their
    figures still while the mock interface invents traffic for every peer the
    kernel holds."""
    from apps.stats.models import ClientDaily

    run("seeddemo")

    unused = ClientMeta.objects.get(name="frank-unused")
    trial = ClientMeta.objects.get(name="grace-trial")

    assert ClientDaily.objects.filter(meta=unused).count() == 0
    assert sum(row.rx + row.tx for row in ClientDaily.objects.filter(meta=trial)) == 2_000_000
    assert not store.get_client("frank-unused").enabled
    assert not store.get_client("grace-trial").enabled


def test_seeddemo_owns_the_whole_client_list(server_conf: Path) -> None:
    """The mock bootstrap writes three demo clients of its own, and they have no
    history: left in place they sat in the table as rows that had never moved a
    byte and that nothing in the demo accounted for."""
    from apps.stats.management.commands.seeddemo import FIXED, GENERATED_NAMES

    run("seeddemo")

    wanted = {habit.name for habit in FIXED} | set(GENERATED_NAMES)
    assert {client.name for client in store.list_clients()} == wanted


def test_seeddemo_draws_the_same_panel_twice(server_conf: Path) -> None:
    """Random within a range, and reproducible: two people looking at two copies
    of this demo are looking at the same panel."""
    from apps.stats.models import ClientDaily

    run("seeddemo")
    first = sorted(ClientDaily.objects.values_list("meta__name", "day", "rx", "tx"))
    run("seeddemo")
    second = sorted(ClientDaily.objects.values_list("meta__name", "day", "rx", "tx"))

    assert first == second


# ---------------------------------------------------------------- accountname


def test_accountname_prints_the_one_account_and_nothing_else() -> None:
    """`awg-panel passwd` passes this straight to changepassword, so it is a bare name."""
    get_user_model().objects.create_user("operator", password="correct-horse-battery-staple")

    assert run("accountname").strip() == "operator"


def test_accountname_refuses_to_guess_between_two_accounts() -> None:
    """Picking one would set the password on whichever account sorted first."""
    user_model = get_user_model()
    user_model.objects.create_user("admin", password="correct-horse-battery-staple")
    user_model.objects.create_user("operator", password="a-quite-different-passphrase")

    with pytest.raises(CommandError) as refused:
        run("accountname")

    # Both names are in the message, because the answer is for a person to give.
    assert "admin" in str(refused.value)
    assert "operator" in str(refused.value)


def test_accountname_says_so_when_the_panel_has_no_account() -> None:
    with pytest.raises(CommandError):
        run("accountname")
