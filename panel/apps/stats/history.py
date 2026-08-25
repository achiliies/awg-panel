"""The stored days, as the two series the charts draw.

One module for both the server's history and a client's, because they are the
same question asked of two tables and the answer has to be shaped identically:
the panel draws them with one chart component, and a difference in how a month
is labelled or an empty day is represented would show up as one of the two
charts being subtly wrong.

Two series come back from one read, and that is the whole reason this is not two
endpoints. A month is a sum of days, so a request that has already fetched three
years of days to build the monthly series has, by definition, also fetched the
ninety days the daily series needs. Folding them here costs one pass over a few
hundred rows; asking for them separately would cost a second query and open the
possibility of the two answers being cut on different sides of midnight.

Empty periods are filled in rather than left out. A day nothing moved on has no
row - that is what keeps the tables small - but a chart drawn from the rows
alone would put Monday next to Thursday at the same spacing as Monday next to
Tuesday, and the quiet stretch that a reader is often looking *for* would be the
one thing the picture could not show. So the series is built from the calendar
and the rows are looked up into it, which makes a gap a zero and the axis even.

The window is the caller's, within limits. Left alone it is the last 90 days and
the last 36 months, which is what the two charts open on and rather more than
either draws at once: they scroll, so the default is sized to what somebody
scrolls back through rather than to what fits across a card. A caller that names
`from` and `to` gets that window instead, cut to the ceilings below - and the
ceilings are the honest part of this. A series is a fixed number of bars in a
box; letting a URL ask for four thousand of them would produce a chart no
browser can draw and no reader can read, so the answer is capped and the panel
shows the window it actually got rather than the one that was asked for.

What bounds a client's window further is retention: its rows are swept after
CLIENT_HISTORY_DAYS, so a window reaching past that would be answered with
zeroes that mean "swept" rather than "quiet". Those two are indistinguishable
once drawn, so a client's window is clamped to what the sweep still holds and
its months start at the first whole one - a month half of which has been pruned
would arrive as a short bar for a reason that is not traffic.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.db.models import Min, QuerySet
from django.utils import timezone

from awg.errors import ValidationError

from .models import CLIENT_HISTORY_DAYS, ClientDaily, DailyTotal, utc_day

# Days in the daily series when no window is asked for, counting today.
DAILY_DAYS = 90

# Months in the monthly series when no window is asked for, counting this one.
MONTHLY_MONTHS = 36

# The most days a daily series will hold, whatever window is asked for.
#
# A little over a year, which is both what a browser can draw as bars without
# the scroller turning to treacle and as far back as a client's rows go anyway.
# The server's own days are never swept, so this is the one thing standing
# between a hand-made `?from=2015-01-01` and eleven years of bars.
MAX_DAILY_DAYS = 400

# The most months a monthly series will hold. The default window is already the
# ceiling: three years is what the chart is sized to scroll through, and a
# request for ten gets the newest three rather than a chart nobody can read.
MAX_MONTHLY_MONTHS = MONTHLY_MONTHS

# The earliest day a window may name.
#
# Not a policy about how much history is worth keeping - the ceilings above are
# that - but a floor under the arithmetic. Every path below subtracts a year or
# thirty-six months from one end of the window, and `date` has no room under
# 0001-01-01 to subtract into: a hand-typed `?to=0001-01-02` would come back as
# an OverflowError rather than as an answer. No panel has records from before
# this, so a window that reaches past it is simply pulled up to it.
EARLIEST_DAY = date(2000, 1, 1)


@dataclass(frozen=True)
class Window:
    """A span of days, both ends included.

    Days rather than periods, for both series. A month is picked up by the
    monthly series if the window touches it at all, which is what makes "the
    first of March to the tenth" a legible request on either tab instead of two
    different questions the caller has to know which of to ask.
    """

    start: date
    end: date


@dataclass(frozen=True)
class Point:
    """One bucket of the series: what moved, and what to call it.

    `period` is the bucket's own name rather than a timestamp - "2026-08-13" for
    a day and "2026-08" for a month. It is what the chart keys and labels its
    bars from, and it is deliberately not a moment: a month is not an instant,
    and handing the browser one would invite it to convert to the reader's zone
    and relabel August as July for anybody far enough west.

    Which is the other half of why the string is built here. Every day in this
    file is a UTC day, cut where models.utc_day cuts it, because that is where
    the collector cut it when it wrote the row. A reader in Auckland sees the
    same figure against the same date as a reader in Tehran, and neither sees a
    day that the server never accounted.
    """

    period: str
    rx: int
    tx: int


def parse_window(params: Mapping[str, str]) -> Window | None:
    """The window `from` and `to` name, or None when neither is given.

    Either end may be left out and means "as far as the answer reaches on that
    side": `?from=2026-01-01` alone runs to today, `?to=2026-03-31` alone runs
    back as far as the ceiling allows. The open end is carried as the widest
    date there is rather than as None, because the clamp below has to pull both
    ends in regardless and one code path through it is easier to be sure of
    than two.

    Raises the panel's own ValidationError, which the DRF handler maps to a 400
    with the message in it - the same treatment a bad field on a form gets, and
    the reason this parses here rather than in each of the two views that need
    it. Nothing else here consults the clock or the database: what a caller
    *asked* for is a fact about the request, and narrowing it to what can be
    answered is the next step's job. The one exception is EARLIEST_DAY, which is
    not narrowing but the floor the arithmetic downstream needs to stay inside
    `date`.
    """
    raw_start = (params.get("from") or "").strip()
    raw_end = (params.get("to") or "").strip()
    if not raw_start and not raw_end:
        return None
    start = _parse_day(raw_start, "from") if raw_start else EARLIEST_DAY
    end = _parse_day(raw_end, "to") if raw_end else date.max
    if start > end:
        raise ValidationError({"from": "The start of the range is after its end."})
    return Window(max(start, EARLIEST_DAY), max(end, EARLIEST_DAY))


def _parse_day(value: str, field: str) -> date:
    """One end of the window, as the wire spells a day.

    ISO and only ISO. `date.fromisoformat` also accepts "2026-08" on new enough
    Pythons and would read it as the first of the month, which would make a
    monthly range silently start where the caller did not put it, so the length
    is checked first.
    """
    message = {field: "Give a date as YYYY-MM-DD."}
    if len(value) != 10:
        raise ValidationError(message)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(message) from exc


def server_series(
    today: date | None = None, window: Window | None = None
) -> dict[str, list[Point] | str | None]:
    """Every client's transfer, by day and by month.

    From DailyTotal rather than from a sum over ClientDaily, and the difference
    only shows on a server somebody has been removed from - see the module
    docstring in models.py. This is the figure the dashboard's "today" card is
    already showing, extended backwards.
    """
    day = today or utc_day(timezone.now())
    rows = DailyTotal.objects.all()
    recorded = _first_day(rows)
    daily, monthly = _windows(window, day, recorded=recorded)
    # Both windows end on the same day - _windows cuts them to one `end` - so
    # one bound covers both series, and it is the window's end rather than
    # today's: a request for a span that closed last March used to read every
    # row from it up to this morning and then throw away everything after the
    # tenth of the month, because _fold only ever looks a row up into the
    # calendar the windows describe.
    rows = rows.filter(day__gte=min(daily.start, monthly.start), day__lte=daily.end)
    return _fold(rows.values_list("day", "rx", "tx"), daily, monthly, recorded)


def client_series(
    public_key: str, today: date | None = None, window: Window | None = None
) -> dict[str, list[Point] | str | None]:
    """One client's transfer, by day and by month.

    Answered from the client's own rows, so it is what *this* client moved and
    not a share of anything. The sum of it will usually come out a little under
    the client's all-time total for the same span, because the all-time figure
    is a counter and this is a record of days: bytes the client moved before the
    retention window opened are in the counter and are no longer here.

    Which is also why the window is narrowed to that retention. The server's
    days go back to the day the panel was installed; a client's go back
    CLIENT_HISTORY_DAYS and no further, so asking this for three years would
    draw two years of zeroes that a reader would take for two quiet years.
    """
    day = today or utc_day(timezone.now())
    rows = ClientDaily.objects.filter(meta__public_key=public_key)
    recorded = _first_day(rows)
    daily, monthly = _windows(
        window, day, retained=day - timedelta(days=CLIENT_HISTORY_DAYS), recorded=recorded
    )
    # The window's end, not today's, for the reason given in server_series.
    rows = rows.filter(day__gte=min(daily.start, monthly.start), day__lte=daily.end)
    return _fold(rows.values_list("day", "rx", "tx"), daily, monthly, recorded)


def _first_day(rows: QuerySet) -> date | None:
    """The oldest day this history has a row for, or None if it has none.

    One aggregate over an indexed column, and the reason the charts stopped
    drawing a year of nothing on a panel installed last month. Without it the
    window is pure calendar arithmetic - thirty-six months back from today,
    whatever the panel was doing then - so a new server opened its monthly view
    on three years of axis with two bars at the end of it, and a client added in
    June had eleven empty columns in front of its first one. Both are honest and
    both read as a chart that failed.

    It is a second query on a request that already makes one, on a table where
    MIN(day) is the first row of an index. That is the whole cost, and it is
    paid on a request the panel makes every thirty seconds while somebody has
    the page open.
    """
    return rows.aggregate(first=Min("day"))["first"]


def _windows(
    requested: Window | None,
    today: date,
    retained: date | None = None,
    recorded: date | None = None,
) -> tuple[Window, Window]:
    """The window each series actually covers.

    Two of them, because the two series are two different amounts of history:
    left to themselves they are the last 90 days and the last 36 months, which
    are the spans the charts open on. A caller that names a window gets the same
    one on both tabs - one question, asked at two granularities - cut to the
    ceilings each series can draw.

    Every end is pulled inside what can be answered, and in a fixed order: no
    later than today, no earlier than the ceiling allows, no earlier than the
    sweep still keeps, and no earlier than the first day there is anything to
    show. The last two are different floors and they treat their month
    differently, which is the one subtlety here. `retained` cuts a month in
    half, so the series starts at the first whole month after it: a column that
    is short because the sweep took its first fortnight is a column that lies.
    `recorded` cuts nothing - a panel installed on the 14th of June really did
    carry what it carried in June - so its own month is included, short bar and
    all, because that bar is the truth about the month.

    Together they can leave the start past the end - a window entirely before
    the panel existed, or before what retention keeps - and it collapses to a
    single bucket rather than to nothing, because a series of no length is a
    chart with no axis and the truthful answer to "what did this client do in
    2019" is a zero, not an error.
    """
    end = min(requested.end, today) if requested is not None else today

    if requested is None:
        daily_start = end - timedelta(days=DAILY_DAYS - 1)
        monthly_start = _shift_month(_first_of(end), 1 - MONTHLY_MONTHS)
    else:
        daily_start = max(requested.start, end - timedelta(days=MAX_DAILY_DAYS - 1))
        monthly_start = max(
            _first_of(requested.start),
            _shift_month(_first_of(end), 1 - MAX_MONTHLY_MONTHS),
        )

    if retained is not None:
        daily_start = max(daily_start, retained)
        monthly_start = max(monthly_start, _first_whole_month(retained))

    if recorded is not None:
        daily_start = max(daily_start, recorded)
        monthly_start = max(monthly_start, _first_of(recorded))

    return (
        Window(min(daily_start, end), end),
        Window(min(monthly_start, _first_of(end)), end),
    )


def _first_of(day: date) -> date:
    return day.replace(day=1)


def _first_whole_month(day: date) -> date:
    """The first of `day`'s month if `day` is the first of it, else the next one."""
    return day if day.day == 1 else _shift_month(_first_of(day), 1)


def _shift_month(first: date, months: int) -> date:
    """The first of the month `months` away from `first`, which is itself a first.

    Counted in months since year zero rather than by adjusting the year and the
    month separately, because the separate version has to special-case the wrap
    at December in both directions and gets month 0 or month 13 wrong the first
    time somebody writes it.
    """
    index = first.year * 12 + first.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def _months_between(first: date, last: date) -> int:
    """How many monthly buckets there are from one month-first to another."""
    return (last.year * 12 + last.month) - (first.year * 12 + first.month) + 1


def _fold(
    rows: Iterable[tuple[date, int, int]],
    daily: Window,
    monthly: Window,
    recorded: date | None,
) -> dict[str, list[Point] | str | None]:
    """Both series, from one pass over the rows.

    The rows are read into two lookups and the series are then built from the
    calendar, which is what makes this a single pass: a month is a sum of days
    the daily series has already been handed, and asking the database for
    another thirty-six sums would be thirty-six trips for arithmetic that is
    already in memory.

    Oldest first in both, which is the opposite of how the tables are ordered and
    the only order a chart can be drawn in. The models order newest-first because
    every other reader wants today; here the series runs left to right the way
    time does.
    """
    by_day: dict[date, tuple[int, int]] = {}
    by_month: dict[str, list[int]] = {}
    for day, rx, tx in rows:
        by_day[day] = (rx, tx)
        # Bucketed as they arrive rather than by scanning the days once per
        # month: a row belongs to exactly one month and knows which from its own
        # date, so there is nothing for a search to find.
        month = by_month.setdefault(f"{day:%Y-%m}", [0, 0])
        month[0] += rx
        month[1] += tx

    days = (daily.end - daily.start).days + 1
    daily_points = [
        Point(day.isoformat(), *by_day.get(day, (0, 0)))
        for day in (daily.start + timedelta(days=offset) for offset in range(days))
    ]

    months = _months_between(monthly.start, monthly.end)
    monthly_points = [
        Point(name, *by_month.get(name, [0, 0]))
        for name in (f"{_shift_month(monthly.start, offset):%Y-%m}" for offset in range(months))
    ]

    # `earliest` travels with the series rather than being worked out from them,
    # because it is the one fact about this history that the window has cut out:
    # a reader looking at an empty March needs to be told that nothing was
    # recorded before June, and the series they are looking at cannot say so.
    return {"daily": daily_points, "monthly": monthly_points, "earliest": recorded}


def prune(now: datetime | None = None, keep_days: int = CLIENT_HISTORY_DAYS) -> int:
    """Drop the client rows that have fallen out of the retention window.

    Only ClientDaily. The server's own daily rows are one a day whatever the
    server holds - three and a half thousand of them in a decade - and they are
    the only record of what this server has ever carried, so nothing sweeps them
    up.

    Returns how many rows went, for the collector's log line.
    """
    cutoff = utc_day(now or timezone.now()) - timedelta(days=keep_days)
    removed, _ = ClientDaily.objects.filter(day__lt=cutoff).delete()
    return removed


def clear_everything(through: date | None = None) -> tuple[int, int]:
    """Drop every stored day, the server's and every client's. Both counts back.

    What "remove all traffic" takes, and the one place the server's own rows are
    ever deleted. Everything else in this module treats DailyTotal as a record
    that outlives whoever moved the bytes - a client's reset does not touch it,
    the sweep does not prune it, and deleting a client leaves their share of it
    standing - because none of those is a claim that the traffic never happened.
    This one is exactly that claim, made deliberately about the whole server by
    somebody who was shown what it means first.

    Bounded by a day for the same reason clear_client is: the collector finishes
    this after the fact, and by the time it does the clock may have passed
    midnight. The new day's rows are bytes that moved after the wipe and are
    nobody's past.

    Two counts rather than a sum, because they are two different sentences - one
    is how much of the server's history went and the other is how many
    client-days did, and a total of the two would be a number of rows rather
    than a fact about either.
    """
    server = DailyTotal.objects.all()
    clients = ClientDaily.objects.all()
    if through is not None:
        server = server.filter(day__lte=through)
        clients = clients.filter(day__lte=through)
    removed_server, _ = server.delete()
    removed_clients, _ = clients.delete()
    return removed_server, removed_clients


def clear_client(meta_id: int, through: date | None = None) -> int:
    """Drop one client's stored days, up to and including `through`.

    What "reset usage" takes along with the counters. One client's rows and no
    other's, and nothing at all from DailyTotal: the server's day is a record of
    what this server carried, and an admin clearing one client's figures is not
    a claim that those bytes never crossed the wire. So the two answers part
    company here, deliberately, and the dashboard reads the same total after
    this as before it.

    Bounded by a day rather than taking the table's whole side of the relation,
    because the collector calls this after the fact - it deletes the row it may
    have re-written from a total it was still holding in memory - and by then
    the clock may have passed midnight. The new day's row is bytes that moved
    after the reset and belong to nobody's past.

    Returns how many rows went, which the caller logs.
    """
    rows = ClientDaily.objects.filter(meta_id=meta_id)
    if through is not None:
        rows = rows.filter(day__lte=through)
    removed, _ = rows.delete()
    return removed
