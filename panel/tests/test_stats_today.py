"""Where the dashboard's day starts, and what falls inside it.

The panel keeps one clock and it is UTC: traffic is stored in it, the daily
total is cut on its midnight, and every timestamp the UI shows is converted to
the reader's own zone in their browser. So "today" on the dashboard card is the
UTC day, and two admins in two countries reading the same panel see the same
number rather than each seeing a figure the other cannot reproduce.

Which day a moment belongs to is decided in one place, ``models.utc_day``,
because the collector writes the row and this endpoint reads it. The first half
below is about that function; the second is about the read finding the right
row, and nothing else - the figure itself is accumulated by the collector, and
tests/test_collector.py is where the adding up is checked.
"""

from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.stats.models import DailyTotal, utc_day
from apps.stats.views import today_bytes

pytestmark = pytest.mark.django_db


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=dt_timezone.utc)  # noqa: UP017


def stored(day: date, rx: int = 0, tx: int = 0) -> None:
    DailyTotal.objects.create(day=day, rx=rx, tx=tx)


# ------------------------------------------------------------ the boundary


def test_the_day_starts_at_utc_midnight() -> None:
    assert utc_day(utc(2026, 8, 6, 12, 0)) == date(2026, 8, 6)


def test_midnight_itself_is_already_inside_its_day() -> None:
    assert utc_day(utc(2026, 8, 6, 0, 0)) == date(2026, 8, 6)


def test_the_last_second_of_a_day_is_still_in_it() -> None:
    assert utc_day(utc(2026, 8, 6, 23, 59, 59)) == date(2026, 8, 6)


@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        # 15:30 on the 6th in Tehran, and 01:30 on the 7th in Auckland. Both are
        # the same instant, and the instant decides the day - not the wall clock
        # of whoever's datetime object it arrived as.
        ("Asia/Tehran", date(2026, 8, 6)),
        ("Pacific/Auckland", date(2026, 8, 6)),
    ],
)
def test_a_moment_given_in_another_zone_is_read_as_the_instant_it_is(
    zone: str, expected: date
) -> None:
    moment = utc(2026, 8, 6, 12, 0).astimezone(ZoneInfo(zone))
    assert utc_day(moment) == expected


def test_every_day_of_the_year_starts_on_its_own_day() -> None:
    """The property, rather than a date somebody looked up."""
    minute = timedelta(minutes=1)

    for offset in range(365):
        noon = utc(2026, 1, 1, 12, 0) + timedelta(days=offset)
        midnight = noon.replace(hour=0, minute=0)

        assert utc_day(noon) == noon.date(), f"{noon.date()}: read as the wrong day"
        assert utc_day(midnight) == noon.date(), f"{noon.date()}: midnight fell outside its day"
        assert utc_day(midnight - minute) < noon.date(), (
            f"{noon.date()}: the minute before the day started was still in it"
        )


# ----------------------------------------------------------- reading it back


def test_today_reads_todays_row() -> None:
    now = utc(2026, 8, 6, 12, 0)
    stored(date(2026, 8, 6), rx=77, tx=6)

    assert today_bytes(now) == (77, 6)


def test_yesterdays_row_is_not_todays_figure() -> None:
    now = utc(2026, 8, 6, 0, 30)
    stored(date(2026, 8, 5), rx=1_000_000, tx=2_000_000)
    stored(date(2026, 8, 6), rx=7, tx=2)

    assert today_bytes(now) == (7, 2)


def test_a_day_with_no_row_reads_as_zero_rather_than_none() -> None:
    """The row is opened by the first flush after the first byte, so a quiet
    night and a panel installed an hour ago look the same - and both of them
    genuinely have moved nothing."""
    stored(date(2026, 8, 5), rx=500, tx=50)

    assert today_bytes(utc(2026, 8, 6, 12, 0)) == (0, 0)


def test_nothing_recorded_at_all_reads_as_zero() -> None:
    assert today_bytes(utc(2026, 8, 6, 12, 0)) == (0, 0)


def test_the_default_moment_is_now() -> None:
    stored(utc_day(timezone.now()), rx=11, tx=22)

    assert today_bytes() == (11, 22)
