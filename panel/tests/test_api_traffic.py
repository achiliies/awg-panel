"""The two traffic-history endpoints, and the one contract they share.

`stats/traffic` and `clients/<name>/traffic` answer the same shape because the
panel draws both with one chart component. That is the whole of what is asserted
here beyond authentication: the same two series, the same period spelling, every
bucket present. A difference between them would not fail anywhere - it would
render, slightly wrong, in one of the two places.

The arithmetic behind the series is tested in test_stats_history.py and the
figures that go into it in test_collector.py. What is left for this file is the
seam: that a name reaches the right client's rows, that a client nobody has is
a 404 rather than an empty chart, and that neither route answers a stranger.
"""

from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.clients.models import ClientMeta
from apps.stats import history
from apps.stats.models import ClientDaily, DailyTotal, utc_day
from awg import store

pytestmark = pytest.mark.django_db

SERIES = {"daily", "monthly", "earliest"}
POINT = {"period", "rx", "tx"}


def api_url(path: str) -> str:
    return f"/api/v1/{path}"


@pytest.fixture
def api(server_conf) -> APIClient:
    user = get_user_model().objects.create_user("admin")
    client = APIClient()
    client.force_login(user)
    return client


@pytest.fixture
def named(server_conf) -> tuple[str, ClientMeta]:
    """A client in the config, with the metadata row its history hangs off."""
    name = store.list_clients()[0].name
    key = store.get_client(name).public_key
    meta, _ = ClientMeta.objects.get_or_create(public_key=key)
    return name, meta


def today() -> date:
    return utc_day(timezone.now())


# --------------------------------------------------------------- the shape


def assert_series(body: dict) -> None:
    """Both series, every bucket, and nothing keyed by anything else.

    The lengths are asserted as bounds rather than as the two constants, because
    the two routes no longer answer with the same number of buckets and should
    not: a client's rows are swept after thirteen months and the server's never
    are, so the server's monthly window is three years and a client's is what is
    left of one. Which window each gets is pinned in test_stats_history.py; what
    is asserted here is that both of them are a chart's worth.
    """
    assert set(body) == SERIES
    assert 0 < len(body["daily"]) <= history.MAX_DAILY_DAYS
    assert 0 < len(body["monthly"]) <= history.MAX_MONTHLY_MONTHS
    for series in (body["daily"], body["monthly"]):
        for point in series:
            assert set(point) == POINT
            assert isinstance(point["rx"], int)
            assert isinstance(point["tx"], int)
    # A label, not a moment. A month is not an instant, and a browser handed one
    # would convert it to the reader's zone and relabel August as July for
    # anybody far enough west.
    assert all(len(point["period"]) == 10 for point in body["daily"])
    assert all(len(point["period"]) == 7 for point in body["monthly"])


def test_the_server_series_has_both_windows_whole(api) -> None:
    response = api.get(api_url("stats/traffic"))

    assert response.status_code == 200
    assert_series(response.json())


def test_the_default_window_is_the_one_the_charts_open_on(api) -> None:
    """No parameters is the case every page load takes, and the two spans are
    what the charts are sized to scroll through rather than what fits across
    them. Nothing is stored here, so nothing cuts them short."""
    body = api.get(api_url("stats/traffic")).json()

    assert body["earliest"] is None
    assert len(body["daily"]) == history.DAILY_DAYS
    assert len(body["monthly"]) == history.MONTHLY_MONTHS


def test_the_window_starts_where_the_records_do(api) -> None:
    """A panel installed last week has no three years to draw, and an axis of
    empty columns in front of its two bars reads as a chart that failed rather
    than as a young server."""
    DailyTotal.objects.create(day=today() - timedelta(days=2), rx=1, tx=2)

    body = api.get(api_url("stats/traffic")).json()

    assert body["earliest"] == (today() - timedelta(days=2)).isoformat()
    assert body["daily"][0]["period"] == body["earliest"]
    assert len(body["daily"]) == 3
    assert len(body["monthly"]) <= 2


def test_a_clients_series_is_the_same_shape_as_the_servers(api, named) -> None:
    """The panel draws both with one component; the two have to be
    interchangeable or one of the charts is quietly wrong.

    Interchangeable in shape and in where the buckets fall, not in how many of
    them there are: a client's history is swept and the server's is not, so the
    client's months are the recent end of the server's rather than all of them.
    """
    name, _ = named

    server = api.get(api_url("stats/traffic")).json()
    client = api.get(api_url(f"clients/{name}/traffic")).json()

    assert_series(client)
    assert [point["period"] for point in client["daily"]] == [
        point["period"] for point in server["daily"]
    ]
    months = [point["period"] for point in client["monthly"]]
    assert months == [point["period"] for point in server["monthly"]][-len(months) :]


# --------------------------------------------------------------- the figures


def test_the_server_series_reports_the_stored_days(api) -> None:
    DailyTotal.objects.create(day=today(), rx=11, tx=22)
    DailyTotal.objects.create(day=today() - timedelta(days=1), rx=3, tx=4)

    daily = api.get(api_url("stats/traffic")).json()["daily"]

    assert (daily[-1]["rx"], daily[-1]["tx"]) == (11, 22)
    assert (daily[-2]["rx"], daily[-2]["tx"]) == (3, 4)


def test_todays_bar_is_the_figure_the_dashboard_card_shows(api) -> None:
    """Both read the same row, so the card and the last bar of the chart beside
    it cannot disagree however long the page is left open."""
    DailyTotal.objects.create(day=today(), rx=500, tx=900)

    summary = api.get(api_url("stats/summary")).json()
    daily = api.get(api_url("stats/traffic")).json()["daily"]

    assert (daily[-1]["rx"], daily[-1]["tx"]) == (summary["todayRx"], summary["todayTx"])


def test_a_clients_series_holds_that_clients_rows_only(api, named) -> None:
    name, meta = named
    other = ClientMeta.objects.create(public_key="Z" * 43 + "=")
    ClientDaily.objects.create(meta=meta, day=today(), rx=7, tx=8)
    ClientDaily.objects.create(meta=other, day=today(), rx=700, tx=800)

    daily = api.get(api_url(f"clients/{name}/traffic")).json()["daily"]

    assert (daily[-1]["rx"], daily[-1]["tx"]) == (7, 8)


def test_a_client_that_has_never_transferred_answers_zeroes(api, named) -> None:
    """Not a 404 and not an empty list: the client exists and has moved nothing,
    which is a chart of zeroes rather than an error."""
    name, _ = named

    body = api.get(api_url(f"clients/{name}/traffic")).json()

    assert_series(body)
    assert all(point["rx"] == 0 and point["tx"] == 0 for point in body["daily"])


# ----------------------------------------------------------------- the window
#
# What the window means is worked out in test_stats_history.py. What is left for
# these is the query string: that both routes read the same two parameters, that
# they reach the parser at all, and that nonsense in them is a 400 with
# something to read rather than a traceback.


@pytest.mark.parametrize("route", ["stats/traffic", "client"])
def test_either_route_takes_the_same_window(api, named, route: str) -> None:
    name, _ = named
    path = f"clients/{name}/traffic" if route == "client" else route
    first = (today() - timedelta(days=6)).isoformat()
    last = (today() - timedelta(days=2)).isoformat()

    body = api.get(api_url(f"{path}?from={first}&to={last}")).json()

    assert [point["period"] for point in body["daily"]][0] == first
    assert [point["period"] for point in body["daily"]][-1] == last


@pytest.mark.parametrize("route", ["stats/traffic", "client"])
@pytest.mark.parametrize("query", ["from=never", "to=2026-13-02", "from=2026-03-02&to=2026-03-01"])
def test_a_window_that_is_not_one_is_a_400_on_either_route(
    api, named, route: str, query: str
) -> None:
    name, _ = named
    path = f"clients/{name}/traffic" if route == "client" else route

    response = api.get(api_url(f"{path}?{query}"))

    assert response.status_code == 400
    assert response.json()


# ----------------------------------------------------------------- the seam


def test_a_client_nobody_has_is_a_404(api) -> None:
    assert api.get(api_url("clients/nosuchclient/traffic")).status_code == 404


def test_a_name_that_could_be_a_path_is_refused(api) -> None:
    """The name reaches store.get_client, which reads files named for it."""
    assert api.get(api_url("clients/..%2F..%2Fetc/traffic")).status_code in (400, 404)


def test_neither_route_answers_without_a_session(server_conf, named) -> None:
    """A client's history is who they talk to and when. 401, like every other
    route in the panel - see test_api_auth.py, which sweeps the whole map."""
    anonymous = APIClient()
    name, _ = named

    assert anonymous.get(api_url("stats/traffic")).status_code == 401
    assert anonymous.get(api_url(f"clients/{name}/traffic")).status_code == 401
