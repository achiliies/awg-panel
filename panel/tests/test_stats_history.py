"""The stored days, as the two series the charts draw.

Everything here is about the shape of the answer rather than about the figures
in it: the collector does the adding up, and tests/test_collector.py checks that.
What can go wrong at this end is that a day lands in the wrong bucket, that a
quiet day disappears instead of reading as zero, or that a month is cut a day
early - and every one of those produces a chart that looks entirely plausible.

The month arithmetic gets the most attention because it is where the mistakes
are. Months are not a fixed number of days, the twelfth month back crosses a
year boundary for most of the year, and the obvious implementation of "same
month last year" produces month zero.
"""

from datetime import date, timedelta

import pytest

from apps.clients.models import ClientMeta
from apps.stats import history
from apps.stats.models import ClientDaily, DailyTotal
from awg.errors import ValidationError

pytestmark = pytest.mark.django_db


def stored(day: date, rx: int = 0, tx: int = 0) -> None:
    DailyTotal.objects.create(day=day, rx=rx, tx=tx)


def stored_for(meta: ClientMeta, day: date, rx: int = 0, tx: int = 0) -> None:
    ClientDaily.objects.create(meta=meta, day=day, rx=rx, tx=tx)


@pytest.fixture
def client_meta() -> ClientMeta:
    return ClientMeta.objects.create(public_key="A" * 43 + "=", name="phone")


def periods(points: list[history.Point]) -> list[str]:
    return [point.period for point in points]


def totals(points: list[history.Point]) -> list[tuple[int, int]]:
    return [(point.rx, point.tx) for point in points]


# ------------------------------------------------------------------ the window


def test_the_daily_series_is_ninety_days_ending_today() -> None:
    series = history.server_series(date(2026, 8, 13))

    assert len(series["daily"]) == history.DAILY_DAYS
    assert series["daily"][-1].period == "2026-08-13"
    assert series["daily"][0].period == "2026-05-16"


def test_the_monthly_series_is_three_years_ending_this_month() -> None:
    series = history.server_series(date(2026, 8, 13))

    assert len(series["monthly"]) == history.MONTHLY_MONTHS
    assert series["monthly"][-1].period == "2026-08"
    assert series["monthly"][0].period == "2023-09"


def test_both_series_run_oldest_first() -> None:
    """The tables are ordered newest-first for every other reader; a chart is the
    one caller that can only be drawn the other way round."""
    series = history.server_series(date(2026, 8, 13))

    assert periods(series["daily"]) == sorted(periods(series["daily"]))
    assert periods(series["monthly"]) == sorted(periods(series["monthly"]))


def test_a_clients_months_all_fit_inside_what_the_sweep_keeps(
    client_meta: ClientMeta,
) -> None:
    """The server's own days are never swept, so its window is the calendar's.
    A client's rows are, and the window has to stop where they do: a first
    column that is short because the sweep took half its days is a bar that
    reads as a quiet fortnight."""
    from apps.stats.models import CLIENT_HISTORY_DAYS

    for day in (date(2026, 1, 31), date(2026, 3, 31), date(2026, 12, 31)):
        oldest = history.client_series(client_meta.public_key, day)["monthly"][0].period
        first_day = date(int(oldest[:4]), int(oldest[5:]), 1)
        assert (day - first_day).days <= CLIENT_HISTORY_DAYS, (
            f"{day}: the oldest column reaches past what retention keeps"
        )


def test_a_clients_window_is_the_servers_where_retention_allows(
    client_meta: ClientMeta,
) -> None:
    """The two answers have to be interchangeable for the days both of them
    hold: the panel draws them with one component, and a client's ninetieth day
    ago has to be the server's."""
    server = history.server_series(date(2026, 8, 13))
    client = history.client_series(client_meta.public_key, date(2026, 8, 13))

    assert periods(client["daily"]) == periods(server["daily"])
    assert periods(client["monthly"]) == periods(server["monthly"])[-len(client["monthly"]) :]


# --------------------------------------------------- where the records start


def test_a_series_starts_no_earlier_than_the_first_stored_day() -> None:
    """The window is calendar arithmetic and the panel is not: thirty-six
    months back from today is a real date on a server installed last month, and
    thirty-four columns of nothing in front of two bars is a chart that reads
    as broken rather than as new."""
    stored(date(2026, 6, 20), rx=1, tx=2)

    series = history.server_series(date(2026, 8, 13))

    assert series["earliest"] == date(2026, 6, 20)
    assert periods(series["daily"])[0] == "2026-06-20"
    assert periods(series["monthly"]) == ["2026-06", "2026-07", "2026-08"]


def test_the_month_the_records_start_in_is_a_column_of_its_own() -> None:
    """Unlike the retention floor, which starts the series at the first *whole*
    month: a June that is short because the panel was installed on the 20th is
    a bar telling the truth about June, and dropping it would lose the traffic
    it holds."""
    stored(date(2026, 6, 20), rx=5, tx=6)

    monthly = history.server_series(date(2026, 8, 13))["monthly"]

    assert monthly[0].period == "2026-06"
    assert (monthly[0].rx, monthly[0].tx) == (5, 6)


def test_a_clients_series_starts_where_that_clients_records_do(
    client_meta: ClientMeta,
) -> None:
    """A client added in July has no thirteen months either, and the same
    empty columns would be worse here: they would read as a client that used to
    be busy and stopped."""
    stored(date(2024, 1, 1), rx=99, tx=99)
    stored_for(client_meta, date(2026, 7, 4), rx=1, tx=2)

    series = history.client_series(client_meta.public_key, date(2026, 8, 13))

    assert series["earliest"] == date(2026, 7, 4)
    assert periods(series["daily"])[0] == "2026-07-04"
    assert periods(series["monthly"]) == ["2026-07", "2026-08"]


def test_a_history_with_nothing_in_it_says_so_and_keeps_its_window() -> None:
    """Null rather than a date, and the full default window: a panel that has
    recorded nothing has no first day to report, and a chart of zeroes over the
    span it would normally draw is the honest picture of one."""
    series = history.server_series(date(2026, 8, 13))

    assert series["earliest"] is None
    assert len(series["daily"]) == history.DAILY_DAYS
    assert len(series["monthly"]) == history.MONTHLY_MONTHS


def test_a_named_window_inside_the_records_is_left_alone() -> None:
    """The floor only ever pulls the start forwards. A window that already
    begins after the first stored day is the caller's question, and answering a
    wider one would be answering something else."""
    stored(date(2026, 1, 1), rx=1, tx=1)
    window = history.parse_window({"from": "2026-06-01", "to": "2026-06-30"})

    series = history.server_series(date(2026, 8, 13), window)

    assert periods(series["daily"])[0] == "2026-06-01"
    assert periods(series["daily"])[-1] == "2026-06-30"


# ------------------------------------------------------- the window asked for


def test_no_parameters_at_all_is_the_default_window() -> None:
    assert history.parse_window({}) is None
    assert history.parse_window({"from": "", "to": ""}) is None


def test_a_named_window_cuts_both_series_to_itself() -> None:
    window = history.parse_window({"from": "2026-06-10", "to": "2026-07-02"})

    series = history.server_series(date(2026, 8, 13), window)

    assert periods(series["daily"])[0] == "2026-06-10"
    assert periods(series["daily"])[-1] == "2026-07-02"
    # A month the window touches at all is a whole column, because the column is
    # what the month came to - not what it came to inside the window.
    assert periods(series["monthly"]) == ["2026-06", "2026-07"]


def test_one_open_end_reaches_as_far_as_the_answer_does() -> None:
    to_today = history.parse_window({"from": "2026-08-01"})
    from_the_start = history.parse_window({"to": "2026-07-04"})

    assert periods(history.server_series(date(2026, 8, 13), to_today)["daily"])[-1] == "2026-08-13"
    assert periods(history.server_series(date(2026, 8, 13), from_the_start)["daily"])[-1] == (
        "2026-07-04"
    )


def test_a_window_running_past_today_stops_at_today() -> None:
    """Tomorrow has no row and never will until it arrives. A chart with a week
    of empty bars hanging off the end of it reads as a week of no traffic."""
    window = history.parse_window({"from": "2026-08-10", "to": "2026-12-25"})

    daily = periods(history.server_series(date(2026, 8, 13), window)["daily"])

    assert daily[-1] == "2026-08-13"
    assert daily == ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"]


def test_a_window_wider_than_the_chart_keeps_its_newest_end() -> None:
    """The ceilings are what stand between a hand-typed date and a chart with
    four thousand bars in it. What survives the cut is the recent end, which is
    the end somebody asking for "everything" is looking at."""
    window = history.parse_window({"from": "2001-01-01", "to": "2026-08-13"})

    series = history.server_series(date(2026, 8, 13), window)

    assert len(series["daily"]) == history.MAX_DAILY_DAYS
    assert periods(series["daily"])[-1] == "2026-08-13"
    assert len(series["monthly"]) == history.MAX_MONTHLY_MONTHS
    assert periods(series["monthly"])[-1] == "2026-08"


def test_a_date_older_than_any_panel_is_answered_rather_than_overflowing() -> None:
    """`date` has nothing under year one to subtract a year of days from, and
    every window here subtracts one somewhere."""
    window = history.parse_window({"from": "0001-01-01", "to": "0001-01-02"})

    series = history.server_series(date(2026, 8, 13), window)

    assert series["daily"]
    assert series["monthly"]


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-08-13", "to": "2026-08-12"},
        {"from": "yesterday"},
        {"to": "2026-13-01"},
        # Accepted by date.fromisoformat on new enough Pythons, as the first of
        # the month - which would start a range somewhere the caller did not.
        {"from": "2026-08"},
    ],
)
def test_a_window_that_is_not_one_is_refused(params: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        history.parse_window(params)


def test_a_clients_window_stops_where_the_sweep_does(client_meta: ClientMeta) -> None:
    """Asked for three years of a client that keeps thirteen months. The extra
    would come back as zeroes, and a zero here means "swept" rather than
    "quiet" - which is precisely the distinction a chart cannot draw."""
    from apps.stats.models import CLIENT_HISTORY_DAYS

    window = history.parse_window({"from": "2023-01-01", "to": "2026-08-13"})
    oldest = date(2026, 8, 13) - timedelta(days=CLIENT_HISTORY_DAYS)

    series = history.client_series(client_meta.public_key, date(2026, 8, 13), window)

    assert periods(series["daily"])[0] >= oldest.isoformat()
    # The months are where the difference shows: three years were asked for and
    # thirteen of them are all that is left to answer with.
    assert len(series["monthly"]) < history.MAX_MONTHLY_MONTHS
    assert periods(series["monthly"])[0] == "2025-08"


def test_a_client_window_entirely_before_retention_is_a_bucket_of_zeroes(
    client_meta: ClientMeta,
) -> None:
    """Not an error and not an empty series: a chart with no axis is worse than
    one honestly reporting that nothing is on record."""
    window = history.parse_window({"from": "2019-01-01", "to": "2019-02-01"})

    series = history.client_series(client_meta.public_key, date(2026, 8, 13), window)

    assert len(series["daily"]) == 1
    assert len(series["monthly"]) == 1
    assert totals(series["daily"]) == [(0, 0)]


# ----------------------------------------------------------- filling the gaps


def test_a_day_with_no_row_reads_as_zero_rather_than_going_missing() -> None:
    """A chart drawn from the rows alone would put Monday next to Thursday at
    the same spacing as Monday next to Tuesday, and the quiet stretch is often
    the thing somebody is looking for."""
    stored(date(2026, 8, 11), rx=5, tx=7)
    stored(date(2026, 8, 13), rx=1, tx=2)

    daily = {point.period: point for point in history.server_series(date(2026, 8, 13))["daily"]}

    assert (daily["2026-08-11"].rx, daily["2026-08-11"].tx) == (5, 7)
    assert (daily["2026-08-12"].rx, daily["2026-08-12"].tx) == (0, 0)
    assert (daily["2026-08-13"].rx, daily["2026-08-13"].tx) == (1, 2)


def test_a_month_with_nothing_in_it_is_still_a_column() -> None:
    series = history.server_series(date(2026, 8, 13))

    assert totals(series["monthly"]) == [(0, 0)] * history.MONTHLY_MONTHS


def test_nothing_stored_at_all_is_two_full_series_of_zeroes() -> None:
    """A panel installed this morning. Empty is a shape the chart can draw; a
    short series is one it would draw wrong."""
    series = history.server_series(date(2026, 8, 13))

    assert len(series["daily"]) == history.DAILY_DAYS
    assert len(series["monthly"]) == history.MONTHLY_MONTHS


# --------------------------------------------------------------- the buckets


def test_a_month_is_the_sum_of_its_days() -> None:
    stored(date(2026, 7, 1), rx=1, tx=10)
    stored(date(2026, 7, 15), rx=2, tx=20)
    stored(date(2026, 7, 31), rx=4, tx=40)

    monthly = {point.period: point for point in history.server_series(date(2026, 8, 13))["monthly"]}

    assert (monthly["2026-07"].rx, monthly["2026-07"].tx) == (7, 70)


def test_the_last_day_of_a_month_does_not_leak_into_the_next() -> None:
    """The bucket boundary, tested where months of different lengths meet."""
    stored(date(2026, 2, 28), rx=100, tx=0)
    stored(date(2026, 3, 1), rx=7, tx=0)

    monthly = {point.period: point for point in history.server_series(date(2026, 8, 13))["monthly"]}

    assert monthly["2026-02"].rx == 100
    assert monthly["2026-03"].rx == 7


def test_a_day_outside_the_window_is_in_neither_series() -> None:
    stored(date(2020, 1, 1), rx=999, tx=999)

    series = history.server_series(date(2026, 8, 13))

    assert sum(point.rx for point in series["daily"]) == 0
    assert sum(point.rx for point in series["monthly"]) == 0


def rows_read(monkeypatch) -> list[int]:
    """How many stored days each call actually hands to the fold.

    Asserted on rather than the series, because the two are not the same
    question: _fold looks a row up into the calendar the windows describe and
    ignores everything else, so reading a year of rows that are then thrown away
    is invisible in the answer and only shows up here.
    """
    counts: list[int] = []
    real = history._fold

    def counting(rows, *args, **kwargs):
        rows = list(rows)
        counts.append(len(rows))
        return real(rows, *args, **kwargs)

    monkeypatch.setattr(history, "_fold", counting)
    return counts


def test_a_closed_window_reads_no_further_than_it_asked_for(monkeypatch) -> None:
    """The rows fetched are the window's, not everything since it closed.

    Both series end where _windows cut them, so the query is bounded by the
    window's end. Bounded by today instead, it read every row from the end of
    the window to this morning and then threw all of them away - and on the
    server's own days, which are never swept, that is every day the panel has
    ever run.
    """
    for offset in range(400):
        stored(date(2025, 1, 1) + timedelta(days=offset), rx=1, tx=1)
    counts = rows_read(monkeypatch)

    window = history.Window(date(2025, 1, 1), date(2025, 3, 31))
    series = history.server_series(date(2026, 8, 13), window)

    assert counts == [90], "the 90 days of the window, not the 400 in the table"
    assert sum(point.rx for point in series["daily"]) == 90
    assert periods(series["daily"])[-1] == "2025-03-31"
    assert periods(series["monthly"])[-1] == "2025-03"


def test_a_clients_closed_window_reads_no_further_either(
    client_meta: ClientMeta, monkeypatch
) -> None:
    """The same bound on a client's series, which is the other call site."""
    for offset in range(40):
        stored_for(client_meta, date(2026, 8, 13) - timedelta(days=offset), rx=1, tx=1)
    counts = rows_read(monkeypatch)

    window = history.Window(date(2026, 8, 1), date(2026, 8, 5))
    series = history.client_series(client_meta.public_key, date(2026, 8, 13), window)

    assert counts == [5]
    assert sum(point.rx for point in series["daily"]) == 5
    assert periods(series["daily"])[-1] == "2026-08-05"


def test_an_open_window_still_reaches_today(monkeypatch) -> None:
    """The bound narrows and never cuts: left to itself the window ends today,
    so the default series reads exactly what it always did."""
    for offset in range(10):
        stored(date(2026, 8, 13) - timedelta(days=offset), rx=1, tx=1)
    counts = rows_read(monkeypatch)

    series = history.server_series(date(2026, 8, 13))

    assert counts == [10]
    assert sum(point.rx for point in series["daily"]) == 10


# --------------------------------------------------------- the month arithmetic


@pytest.mark.parametrize(
    ("first", "months", "expected"),
    [
        # Forwards and backwards over a year boundary, which is where the
        # obvious implementation produces month 0 or month 13.
        (date(2026, 1, 1), -1, date(2025, 12, 1)),
        (date(2025, 12, 1), 1, date(2026, 1, 1)),
        (date(2026, 8, 1), -11, date(2025, 9, 1)),
        (date(2026, 8, 1), 0, date(2026, 8, 1)),
        (date(2026, 1, 1), -12, date(2025, 1, 1)),
    ],
)
def test_shifting_a_month_crosses_the_year_correctly(
    first: date, months: int, expected: date
) -> None:
    assert history._shift_month(first, months) == expected


def test_every_month_of_a_decade_shifts_back_and_forth_to_itself() -> None:
    """The property, rather than the handful of dates above."""
    for index in range(120):
        month = history._shift_month(date(2020, 1, 1), index)
        assert month.day == 1
        assert history._shift_month(history._shift_month(month, -11), 11) == month


def test_the_monthly_window_is_whole_months_from_any_day_in_the_month() -> None:
    """The series must not slide with the date: the first of the month and the
    last of it look back to the same columns, or a chart open across midnight
    would relabel its own axis."""
    for day in (1, 15, 28):
        series = history.server_series(date(2026, 3, day))
        assert periods(series["monthly"])[0] == "2023-04"
        assert periods(series["monthly"])[-1] == "2026-03"


# ------------------------------------------------------------- one client


def test_a_client_series_holds_that_client_only(client_meta: ClientMeta) -> None:
    other = ClientMeta.objects.create(public_key="B" * 43 + "=", name="laptop")
    stored_for(client_meta, date(2026, 8, 13), rx=5, tx=6)
    stored_for(other, date(2026, 8, 13), rx=500, tx=600)

    series = history.client_series(client_meta.public_key, date(2026, 8, 13))

    assert series["daily"][-1].rx == 5
    assert series["monthly"][-1].tx == 6


def test_a_clients_month_is_the_sum_of_that_clients_days(client_meta: ClientMeta) -> None:
    """The same property as the server's month, asserted again on the other
    table. `_fold` is shared, so this cannot fail on its own - what it pins is
    that the client path really does go through it, and that the extra join
    narrowing to one client has not narrowed the month with it."""
    other = ClientMeta.objects.create(public_key="B" * 43 + "=", name="laptop")
    stored_for(client_meta, date(2026, 7, 2), rx=1, tx=10)
    stored_for(client_meta, date(2026, 7, 20), rx=2, tx=20)
    stored_for(client_meta, date(2026, 7, 31), rx=4, tx=40)
    # A day in the next month, and another client's July, neither of which
    # belongs in the figure below.
    stored_for(client_meta, date(2026, 8, 1), rx=1000, tx=1000)
    stored_for(other, date(2026, 7, 15), rx=500, tx=500)

    series = history.client_series(client_meta.public_key, date(2026, 8, 13))
    monthly = {point.period: point for point in series["monthly"]}

    assert (monthly["2026-07"].rx, monthly["2026-07"].tx) == (7, 70)
    assert (monthly["2026-08"].rx, monthly["2026-08"].tx) == (1000, 1000)


def test_a_client_with_no_rows_is_an_empty_series_rather_than_an_error(
    client_meta: ClientMeta,
) -> None:
    series = history.client_series(client_meta.public_key, date(2026, 8, 13))

    assert totals(series["daily"]) == [(0, 0)] * history.DAILY_DAYS


def test_a_public_key_nothing_knows_about_answers_empty() -> None:
    """A client removed between the page loading and the dialog opening. An
    empty history is the truth about a client that no longer exists."""
    series = history.client_series("Z" * 43 + "=", date(2026, 8, 13))

    assert series["monthly"]
    assert set(totals(series["monthly"])) == {(0, 0)}


def test_a_clients_history_goes_when_the_client_does(client_meta: ClientMeta) -> None:
    """The cascade, which is the whole reason these rows hang off the metadata
    row rather than being keyed by public key - see apps.stats.models."""
    stored_for(client_meta, date(2026, 8, 13), rx=5, tx=6)

    client_meta.delete()

    assert ClientDaily.objects.count() == 0


def test_a_clients_history_follows_a_key_rotation(client_meta: ClientMeta) -> None:
    """A rekey rewrites the public key in place on the same metadata row. To the
    admin this is one client with new credentials, and its history says so."""
    stored_for(client_meta, date(2026, 8, 13), rx=5, tx=6)

    client_meta.public_key = "C" * 43 + "="
    client_meta.save(update_fields=["public_key"])

    series = history.client_series("C" * 43 + "=", date(2026, 8, 13))
    assert series["daily"][-1].rx == 5


def test_the_server_series_keeps_a_deleted_clients_bytes(client_meta: ClientMeta) -> None:
    """The two tables are deliberately not derived from one another. Bytes that
    moved today moved today, whoever has since been removed - so the server's
    day is read from the server's row and never summed from the clients'."""
    stored(date(2026, 8, 13), rx=50, tx=60)
    stored_for(client_meta, date(2026, 8, 13), rx=50, tx=60)

    client_meta.delete()

    assert history.server_series(date(2026, 8, 13))["daily"][-1].rx == 50


# ----------------------------------------------------------------- retention


def test_the_sweep_takes_client_rows_past_the_window(client_meta: ClientMeta) -> None:
    from datetime import datetime
    from datetime import timezone as dt_timezone

    now = datetime(2026, 8, 13, 12, 0, tzinfo=dt_timezone.utc)  # noqa: UP017
    stored_for(client_meta, date(2026, 8, 13))
    stored_for(client_meta, date(2026, 8, 1))
    stored_for(client_meta, date(2024, 1, 1))

    assert history.prune(now) == 1
    assert ClientDaily.objects.filter(day=date(2024, 1, 1)).count() == 0
    assert ClientDaily.objects.count() == 2


def test_the_sweep_never_touches_the_servers_own_days(client_meta: ClientMeta) -> None:
    """One row a day whatever the server holds - three and a half thousand of
    them in a decade - and the only record of what this server has carried."""
    from datetime import datetime
    from datetime import timezone as dt_timezone

    stored(date(2015, 6, 1), rx=1, tx=1)

    history.prune(datetime(2026, 8, 13, 12, 0, tzinfo=dt_timezone.utc))  # noqa: UP017

    assert DailyTotal.objects.filter(day=date(2015, 6, 1)).exists()
