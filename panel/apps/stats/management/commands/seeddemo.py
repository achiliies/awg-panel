"""Fill a mock panel with clients and a year of traffic, for looking at.

`make demo` runs this. It exists because the two traffic history charts are the
one part of the panel that cannot be judged from a fresh install: they are about
what a server has done over months, and a server that was set up ten minutes ago
draws them as two rows of zeroes, correctly. Reviewing a change to them, or
showing somebody what the panel does, meant either waiting a year or writing the
rows by hand every time.

So the rows are written here, and the client list comes in two halves. The
fixed ones each exist to put a specific case on the screen - a weekday laptop, a
phone that trickles, a television that does not, one that stopped connecting six
months ago, one added last week, one that has never moved a byte and one whose
whole life is two megabytes. The generated ones fill the table out, with habits
drawn from a range rather than written down. Both halves come off one seeded
generator, so "random" means varied rather than different every run: two people
looking at two copies of this demo are looking at the same panel, and a figure
somebody quotes from it is still there tomorrow.

The mixture is the point. A history that only ever draws the same bar cannot
show that an empty month renders, that a client older than the retention window
is cut off where the window is, that a client added last Tuesday has zeroes
before it and not after, or that a figure in megabytes is legible on an axis
whose other rows are in terabytes.

Consistency across the three stores matters as much as the shapes, and is easier
to get wrong. The history tables are written here; the all-time totals live in
traffic.db, which nothing else in this command would touch. Left alone, the
dashboard showed a chart of a terabyte over thirty days above a card reading
eleven gigabytes for all time - not a state any real server can be in, and the
sort of thing that sends somebody looking for a bug in the panel. So the
counters are written too, from the same rows.

Everything on screen is kept monotonic: the all-time card covers the whole
seeded history, the twelve-month chart is a subset of it and the thirty-day
chart a subset of that. See PRE_WINDOW_SHARE for the one realistic effect that
is deliberately *not* modelled, and why.

Refuses to run outside AWG_MOCK. It rewrites the server config and deletes both
history tables, and while that is exactly right for a sandbox it would be
somewhere between rude and destructive on a box carrying real clients. The mock
flag is the panel's existing word for "this is not a real server", and it is
checked before anything is written rather than as an argument somebody has to
remember to pass.

Nothing here is imported by the panel itself. It is a development command that
ships with the code for the same reason the tests do: it is how the feature is
looked at, and a copy kept outside the repository would rot.
"""

import random
import secrets
import string
from dataclasses import dataclass
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.clients.models import ClientMeta
from apps.stats.models import CLIENT_HISTORY_DAYS, ClientDaily, DailyTotal, utc_day
from awg import store, traffic
from awg.controller import get_controller, mock_enabled
from awg.errors import AwgError

MB = 1_000_000
GB = 1_000_000_000

# Letters and digits only. The obvious generator here is secrets.token_urlsafe,
# and what it returns is base64url - so roughly one password in three carries a
# "-" or a "_". This one is not stored in a manager and pasted; it is read off
# one screen and typed on another, sometimes read aloud, and those two
# characters are the ones that get lost on the way: dictated as "dash" or
# "underscore", mistaken for each other in a proportional font, and swallowed by
# a keyboard layout that puts them somewhere else.
#
# The length pays for the smaller alphabet. Sixteen characters out of sixty-two
# is about 95 bits, against the 96 that token_urlsafe(12) gave, so nothing is
# traded away here but the punctuation.
PASSWORD_ALPHABET = string.ascii_letters + string.digits
PASSWORD_LENGTH = 16

# What a client that has been here the whole retention window moved before the
# window opened. traffic.db is a counter that has never been reset and the
# history is swept at CLIENT_HISTORY_DAYS, so a long-lived client's all-time
# figure is legitimately larger than everything its charts can add up.
PRE_WINDOW_SHARE = 0.35

# Nothing is attributed to clients that have since been deleted, and that is a
# decision rather than an omission.
#
# A real server does carry such traffic: the server's daily row keeps a deleted
# client's bytes and the per-client rows go with the client, so the sum of the
# days can exceed the sum of the clients' all-time counters. It is a real
# asymmetry and the models document it.
#
# Modelling it here produced a dashboard whose all-time card read *less* than
# the chart beneath it - correct, defensible, and indistinguishable from a bug
# to anybody who had not read the models. Demo data is looked at rather than
# reasoned about, so the figures it shows are kept monotonic: all-time covers
# the whole seeded history, the twelve-month chart is a subset of it, and the
# thirty-day chart is a subset of that. Nothing on the screen can be read as
# contradicting anything else on it.


@dataclass(frozen=True)
class Habit:
    """One client's pattern, in the terms the charts are read in."""

    name: str
    # Typical bytes downloaded on a day it connects, and how much that varies.
    base_gb: float
    spread_gb: float
    # Weekday numbers it connects on; Monday is 0.
    days: tuple[int, ...]
    # Days ago this client first appeared, and last connected.
    since: int
    until: int = 0
    note: str = ""
    # Bytes downloaded over the client's whole life, instead of a daily pattern.
    # For the clients that exist to be small: a figure of a few megabytes cannot
    # be expressed as a daily average without rounding to nothing.
    total_bytes: int = 0
    # Whether the peer is on the interface. A disabled peer is the only kind
    # whose figures hold still - see FIXED below.
    enabled: bool = True


EVERY_DAY = (0, 1, 2, 3, 4, 5, 6)
WEEKDAYS = (0, 1, 2, 3, 4)

# The clients that exist to show something in particular. Spelled out rather
# than generated, because each one is here to put a specific case in front of
# somebody: an empty month, a client cut off by the retention window, a client
# younger than the chart, a figure in megabytes beside figures in terabytes, and
# a row of zeroes.
FIXED = (
    Habit("alice-laptop", 9, 6, WEEKDAYS, since=CLIENT_HISTORY_DAYS, note="Weekdays only"),
    Habit("bob-phone", 2, 2, EVERY_DAY, since=CLIENT_HISTORY_DAYS, note="A steady trickle"),
    Habit("carol-tv", 24, 18, EVERY_DAY, since=260, note="Heavy, and newer than the window"),
    Habit(
        "dave-old",
        4,
        3,
        EVERY_DAY,
        since=CLIENT_HISTORY_DAYS,
        until=190,
        note="Stopped in the spring",
    ),
    Habit("erin-new", 6, 5, EVERY_DAY, since=9, note="Added last week"),
    # The two that have to hold still, and the reason both are switched off.
    #
    # The mock interface fabricates traffic for every peer the kernel holds, and
    # the collector folds it in every couple of seconds - so an *enabled* client
    # seeded at two megabytes is a client reading forty megabytes by the time
    # anybody has finished logging in, and one seeded at zero never sees zero at
    # all. A disabled peer is left out of the configuration the interface is
    # given, exactly as a real one would be, so the mock never invents a byte
    # for it and the figure stays the figure.
    #
    # It costs nothing and demonstrates something: the client list needs a
    # disabled row to show what one looks like anyway.
    Habit(
        "frank-unused",
        0,
        0,
        (),
        since=30,
        note="Issued, never switched on",
        enabled=False,
    ),
    Habit(
        "grace-trial",
        0,
        0,
        (),
        since=45,
        note="A trial that barely ran",
        total_bytes=2 * MB,
        enabled=False,
    ),
)

# Names for the generated clients, in the order they are used. A demo with
# "client-7" in it looks like test data; these look like a small office, which
# is what makes the table worth reading at all.
GENERATED_NAMES = ("henry-desktop", "iris-ipad", "jack-router", "karen-phone")

# The ranges a generated client's habit is drawn from. Random within a range,
# and reproducible: the seed is fixed, so "random" here means varied rather than
# different every run. Two people looking at two copies of this demo are looking
# at the same panel.
GENERATED_BASE_GB = (1.0, 14.0)
GENERATED_SPREAD_SHARE = (0.3, 1.1)
GENERATED_SINCE_DAYS = (40, CLIENT_HISTORY_DAYS)

# How often a client that would otherwise have connected did not, so the daily
# chart has gaps in it and the zero-filling is visible rather than assumed.
QUIET_DAY_CHANCE = 0.12

# Today is only part way through when this runs, so its bars should be short.
# A demo whose last bar is a full day's traffic reads as a chart that has not
# noticed the time, which is the first thing anybody checks.
TODAY_SHARE = 3


def _generated_password() -> str:
    """A password for one demo run, out of letters and digits only.

    secrets.choice and not random.choice: this module seeds `random` from
    --seed so that the traffic it fabricates is the same on every run, and a
    password drawn from that generator would be the same on every run too -
    printed as though it were fresh, and identical on every machine the target
    is run on, which is the one thing the whole account is generated to avoid.
    """
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


class Command(BaseCommand):
    help = "Fill a mock panel with demo clients and a year of traffic history."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--seed",
            type=int,
            default=11,
            help="Random seed, so two runs draw the same charts (default: 11).",
        )
        parser.add_argument(
            "--username",
            default="admin",
            help="Account to create or re-password (default: admin).",
        )
        parser.add_argument(
            "--password",
            default="",
            help="Password to set. A strong random one is generated and printed if omitted.",
        )

    def handle(self, *args: object, **options: object) -> None:
        if not mock_enabled():
            raise CommandError(
                "seeddemo only runs against a mock panel, and AWG_MOCK is not set. "
                "It deletes the traffic history and adds peers to the server config, "
                "which is not something to do to a server carrying real clients."
            )

        random.seed(options["seed"])
        today = utc_day(timezone.now())
        habits = FIXED + self._generated()

        try:
            self._configure(habits)
        except AwgError as exc:
            raise CommandError(f"cannot write the demo configuration: {exc}") from exc

        metas = self._metadata(habits)
        rows, server = self._history(habits, metas, today)

        # Replaced rather than added to, so re-running is idempotent and a
        # second run does not stack another year on top of the first.
        ClientDaily.objects.all().delete()
        DailyTotal.objects.all().delete()
        ClientDaily.objects.bulk_create(rows, batch_size=500)
        DailyTotal.objects.bulk_create(
            (DailyTotal(day=day, rx=rx, tx=tx) for day, (rx, tx) in server.items()),
            batch_size=500,
        )
        self._counters(habits, metas, rows)

        password = self._account(str(options["username"]), str(options["password"]))
        self._report(habits, len(rows), len(server), today, str(options["username"]), password)

    def _generated(self) -> tuple[Habit, ...]:
        """Clients with habits drawn from a range rather than written out.

        The fixed set covers the cases somebody is meant to go and look at. This
        is the rest of the office: enough rows that the table is worth sorting
        and the busiest-clients card has something to rank, without another five
        paragraphs deciding what each one is for.

        Drawn from the seeded generator, so "random" means varied rather than
        different every run - two people looking at two copies of this demo are
        looking at the same panel, and a figure somebody quotes from it is still
        there tomorrow.
        """
        habits: list[Habit] = []
        for name in GENERATED_NAMES:
            base = random.uniform(*GENERATED_BASE_GB)
            habits.append(
                Habit(
                    name=name,
                    base_gb=base,
                    spread_gb=base * random.uniform(*GENERATED_SPREAD_SHARE),
                    # Weekdays or every day, which is the one thing about a real
                    # client's shape that is visible in a daily chart.
                    days=random.choice((EVERY_DAY, WEEKDAYS)),
                    since=random.randint(*GENERATED_SINCE_DAYS),
                )
            )
        return tuple(habits)

    def _configure(self, habits: tuple[Habit, ...]) -> None:
        """Make the server config hold exactly these clients, and nothing else.

        The bootstrap writes a demo server with three clients of its own, and
        they are the reason this exists: they have no history, so they sat in the
        table as three rows that had never moved a byte and that nothing in the
        demo accounted for. Removing them makes the client list exactly what this
        command says it is.
        """
        store.bootstrap_if_missing()
        wanted = {habit.name for habit in habits}
        for client in store.list_clients():
            if client.name not in wanted:
                store.remove_client(client.name)
        existing = {client.name for client in store.list_clients()}
        for habit in habits:
            if habit.name not in existing:
                store.add_client(habit.name)
            if not habit.enabled:
                store.set_client_enabled(habit.name, False)

    def _metadata(self, habits: tuple[Habit, ...]) -> dict[str, ClientMeta]:
        """The metadata row each client's history hangs off, by name.

        Opened here rather than left to the collector because the history is
        written in the same breath and the rows are what it is keyed on. A peer
        added through the store has no metadata until something asks for it.
        """
        metas: dict[str, ClientMeta] = {}
        for habit in habits:
            key = store.get_client(habit.name).public_key
            meta, _ = ClientMeta.objects.get_or_create(public_key=key)
            if meta.name != habit.name or meta.note != habit.note:
                meta.name = habit.name
                meta.note = habit.note
                meta.save(update_fields=["name", "note"])
            metas[habit.name] = meta
        return metas

    def _history(
        self, habits: tuple[Habit, ...], metas: dict[str, ClientMeta], today: object
    ) -> tuple[list[ClientDaily], dict[object, list[int]]]:
        """Every client's days, and the server's day as their sum.

        The server's rows are accumulated from the clients' rather than drawn
        separately, because that is how the collector arrives at them: one pass
        over the deltas, counted twice. A demo that drew two independent random
        series would show the two charts disagreeing about the same day, which
        is a bug in the panel and must not be a feature of its demo data.

        A client carrying `total_bytes` is spent over a couple of days rather
        than spread across its life: a few megabytes divided by forty-five days
        is a row of zeroes, and the point of that client is to put a small
        number on the screen.
        """
        rows: list[ClientDaily] = []
        server: dict[object, list[int]] = {}

        def record(habit: Habit, day: object, up: int, down: int) -> None:
            rows.append(ClientDaily(meta=metas[habit.name], day=day, rx=up, tx=down))
            total = server.setdefault(day, [0, 0])
            total[0] += up
            total[1] += down

        for habit in habits:
            if habit.total_bytes:
                for offset, share in ((3, 0.7), (2, 0.3)):
                    day = today - timedelta(days=habit.since - offset)  # type: ignore[operator]
                    spent = int(habit.total_bytes * share)
                    record(habit, day, spent // 2, spent - spent // 2)
                continue
            for back in range(habit.until, habit.since):
                day = today - timedelta(days=back)  # type: ignore[operator]
                if day.weekday() not in habit.days:
                    continue
                if random.random() < QUIET_DAY_CHANCE:
                    continue
                down = max(1, int(random.gauss(habit.base_gb, habit.spread_gb))) * GB
                if back == 0:
                    down //= TODAY_SHARE
                up = down // random.randint(6, 14)
                record(habit, day, up, down)

        return rows, server

    def _counters(
        self,
        habits: tuple[Habit, ...],
        metas: dict[str, ClientMeta],
        rows: list[ClientDaily],
    ) -> None:
        """Write traffic.db so the all-time figures agree with the history.

        Without this the demo contradicted itself in the most visible way it
        could. The history tables are written here; the all-time totals are not
        in a table at all but in traffic.db, which only the collector writes -
        so the dashboard showed a chart of a terabyte over thirty days above a
        card reading eleven gigabytes for all time, which is not a state any
        real server can be in.

        Each client's counter is its whole seeded history plus a share for what
        moved before the retention window, which is the relationship a real
        server has: the file is a counter that has never been reset and the rows
        are swept at CLIENT_HISTORY_DAYS. So the card reads a little more than
        the chart under it, and the difference is a thing the panel can explain.

        `last_rx`/`last_tx` are taken from what the interface is reporting right
        now rather than left at zero. They are the raw values the counters were
        last differenced against, and a zero would tell the collector that
        everything the mock has ever counted arrived in the next two seconds -
        adding a few gigabytes per client to today's figure on the first poll,
        which is the same contradiction again in the other direction.
        """
        moved: dict[str, list[int]] = {}
        for row in rows:
            total = moved.setdefault(row.meta.public_key, [0, 0])
            total[0] += row.rx
            total[1] += row.tx

        raw = {}
        try:
            dump = get_controller().show_dump()
            if dump is not None:
                raw = {peer.public_key: (peer.rx, peer.tx) for peer in dump.peers}
        except AwgError:
            # No interface to read. The counters are still worth writing; the
            # first poll will simply fold the mock's opening figures in.
            pass

        counters: dict[str, traffic.Counters] = {}
        for habit in habits:
            key = metas[habit.name].public_key
            rx, tx = moved.get(key, [0, 0])
            # Only a client that reaches the far edge of the window has history
            # the window cannot account for.
            if habit.since >= CLIENT_HISTORY_DAYS:
                rx = int(rx * (1 + PRE_WINDOW_SHARE))
                tx = int(tx * (1 + PRE_WINDOW_SHARE))
            last_rx, last_tx = raw.get(key, (0, 0))
            counters[key] = traffic.Counters(cum_rx=rx, cum_tx=tx, last_rx=last_rx, last_tx=last_tx)
        traffic.write_db(counters)

    def _account(self, username: str, password: str) -> str:
        """Create or re-password the demo account, and answer with the password.

        Generated rather than fixed, and generated on every run. `make demo`
        binds to every interface, so this account is reachable by anything that
        can route to the box - and a demo target with a password baked into the
        repository is one that ships the same credentials to every machine it is
        ever run on.
        """
        chosen = password or _generated_password()
        model = get_user_model()
        user, created = model.objects.get_or_create(username=username)
        user.set_password(chosen)
        # A demo account is for looking at the panel, not for administering a
        # machine; nothing here needs Django's staff or superuser flags, and the
        # panel does not read them.
        user.save()
        if not created:
            self.stdout.write(f"  the {username} account already existed; its password was reset")
        return chosen

    def _report(
        self,
        habits: tuple[Habit, ...],
        client_rows: int,
        server_rows: int,
        today: object,
        username: str,
        password: str,
    ) -> None:
        self.stdout.write("")
        self.stdout.write(f"  demo data seeded, as of {today}")
        self.stdout.write(f"    {len(habits)} clients, {client_rows} client-days")
        self.stdout.write(f"    {server_rows} server-days, back to {CLIENT_HISTORY_DAYS} days ago")
        for habit in habits:
            # The generated ones carry no note; say what they are rather than
            # leaving a column of blanks somebody has to interpret.
            note = habit.note or f"~{habit.base_gb:.0f} GB a day, {len(habit.days)} days a week"
            off = "" if habit.enabled else "  (disabled)"
            self.stdout.write(f"      {habit.name:<14} {note}{off}")
        self.stdout.write("")
        self.stdout.write(f"    sign in as   {username} / {password}")
        self.stdout.write("")
