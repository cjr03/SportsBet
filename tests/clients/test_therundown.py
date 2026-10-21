from datetime import date

import httpx
import pytest

from gridiron_edge.clients.http import AuthError
from gridiron_edge.clients.therundown import TheRundownClient


@pytest.fixture
def rd(conn):
    return TheRundownClient("k", conn, resolve=lambda lg, h, a, t: "nfl:20261101:CHI@SEA")


def _ok(load, **headers):
    return httpx.Response(200, json=load("therundown_events.json"), headers=headers)


async def test_scores_status_mapping(respx_mock, rd, load):
    route = respx_mock.get(url__regex=r".*/sports/2/events/2026-11-01.*").mock(return_value=_ok(load))
    games = await rd.scores("nfl", date(2026, 11, 1))
    assert [g.status for g in games] == ["final", "live", "void_candidate"]
    assert (games[0].home_pts, games[0].away_pts) == (27, 20)
    assert isinstance(games[0].home_pts, int) and games[1].period == "3" and games[1].clock == "4:32"
    assert (games[0].home, games[0].away, games[0].season) == ("SEA", "CHI", 2026)
    req = route.calls[0].request
    assert req.url.params["affiliate_ids"] == "0" and req.headers["X-TheRundown-Key"] == "k"


async def test_lines_skip_sentinel(respx_mock, rd, load):
    route = respx_mock.get(url__regex=r".*/sports/2/events/.*").mock(return_value=_ok(load))
    qs = await rd.lines("nfl", date(2026, 11, 1))
    assert qs and all(q.decimal > 1 for q in qs) and not any(q.book == "kalshi" for q in qs)
    assert {q.book for q in qs} == {"draftkings", "fanduel", "betmgm"}
    sea = [q for q in qs if q.book == "draftkings" and q.market == "h2h" and q.side == "home"]
    assert sea[0].decimal == pytest.approx(1.625) and sea[0].point is None
    assert {(q.side, q.point) for q in qs if q.market == "totals"} == {("over", 44.5), ("under", 44.5)}
    assert {(q.side, q.point) for q in qs if q.market == "spreads"} == {("home", -3.5), ("away", 3.5)}
    p = route.calls[0].request.url.params
    assert p["market_ids"] == "1,2,3" and p["affiliate_ids"] == "19,23,22,25" and p["main_line"] == "true"


async def test_rundown_records_budget_and_delay(respx_mock, rd, load):
    respx_mock.get(url__regex=r".*/events/.*").mock(return_value=_ok(
        load, **{"X-Data-Delay-Seconds": "300", "X-Datapoints-Monthly-Remaining": "199000",
                 "X-Tier": "free", "X-Bookmakers": "19,23,22"}))
    await rd.scores("nfl", date(2026, 11, 1))
    assert rd.delay_seconds == 300 and rd.tier == "free" and rd.bookmakers_header == "19,23,22"
    assert rd.budget.remaining() == 199000


async def test_401_disables(respx_mock, rd):
    respx_mock.get(url__regex=r".*/events/.*").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthError):
        await rd.scores("nfl", date(2026, 11, 1))
    assert rd.disabled and await rd.scores("nfl", date(2026, 11, 1)) == []


async def test_unmatched_game_skipped(respx_mock, rd, load):
    rd.resolve = lambda *a: None
    respx_mock.get(url__regex=r".*/events/.*").mock(return_value=_ok(load))
    assert await rd.scores("nfl", date(2026, 11, 1)) == [] and rd.unmatched == 3


async def test_malformed_items_skipped_not_fatal(respx_mock, rd, load):
    import copy
    body = copy.deepcopy(load("therundown_events.json"))
    evs = body["events"] if isinstance(body, dict) else body
    good = copy.deepcopy(evs[0])
    bad_date = copy.deepcopy(evs[0]); bad_date.pop("event_date")
    bad_team = copy.deepcopy(evs[0])
    for t in bad_team["teams"]:
        t.pop("team_id", None)
    bad_price = copy.deepcopy(evs[0])
    for mk in bad_price.get("markets", []):
        for part in mk.get("participants", []):
            for line in part.get("lines", []):
                if line.get("value") is not None:
                    line["value"] = "n/a"
                for pr in line.get("prices", {}).values():
                    pr.pop("updated_at", None)
    body2 = {"events": [bad_date, bad_team, bad_price, good]}
    respx_mock.get(url__regex=r".*/events/.*").mock(return_value=httpx.Response(200, json=body2))
    qs = await rd.lines("nfl", date(2026, 11, 1))
    assert qs and rd.malformed >= 2
    games = await rd.scores("nfl", date(2026, 11, 1))
    assert len(games) >= 1


async def test_scores_bad_game_id_skipped(respx_mock, rd, load):
    rd.resolve = lambda *a: "weird-id"
    respx_mock.get(url__regex=r".*/events/.*").mock(return_value=_ok(load))
    assert await rd.scores("nfl", date(2026, 11, 1)) == []
    assert rd.malformed >= 1


async def test_low_daily_header_blocks_next_call(respx_mock, rd, load):
    route = respx_mock.get(url__regex=r".*/events/.*").mock(return_value=_ok(
        load, **{"X-Datapoints-Remaining": "1500", "X-Datapoints-Monthly-Remaining": "150000",
                 "X-Datapoints": "6"}))
    await rd.lines("nfl", date(2026, 11, 1))
    assert rd.daily_remaining() == 1500 and route.call_count == 1
    assert await rd.lines("nfl", date(2026, 11, 1)) == [] and rd.last_status == "skipped_budget"
    assert await rd.scores("nfl", date(2026, 11, 1)) == []
    assert route.call_count == 1
    # an open bet's scores may still dip into the daily reserve
    await rd.scores("nfl", date(2026, 11, 1), essential=True)
    assert route.call_count == 2


async def test_monthly_reserve_blocks_non_essential(respx_mock, rd, load):
    route = respx_mock.get(url__regex=r".*/events/.*").mock(return_value=_ok(
        load, **{"X-Datapoints-Remaining": "19000", "X-Datapoints-Monthly-Remaining": "19000"}))
    await rd.scores("nfl", date(2026, 11, 1))
    assert await rd.lines("nfl", date(2026, 11, 1)) == [] and route.call_count == 1


async def test_daily_reading_from_another_day_is_ignored(respx_mock, rd, load):
    route = respx_mock.get(url__regex=r".*/events/.*").mock(return_value=_ok(load))
    rd.daily = (date(2000, 1, 1), 0)
    await rd.scores("nfl", date(2026, 11, 1))
    assert route.call_count == 1


async def test_lines_stamp_fetch_time(respx_mock, rd, load):
    respx_mock.get(url__regex=r".*/sports/2/events/.*").mock(return_value=_ok(load))
    qs = await rd.lines("nfl", date(2026, 11, 1))
    assert qs and all(q.fetched_at is not None for q in qs)


async def test_lines_add_sharp_affiliates_only_when_allowed(respx_mock, rd, load):
    route = respx_mock.get(url__regex=r".*/sports/2/events/.*").mock(
        return_value=_ok(load, **{"X-Bookmakers": "19,23,22,25,3,11"}))
    await rd.lines("nfl", date(2026, 11, 1))  # first call: allowed list unknown yet
    assert route.calls[0].request.url.params["affiliate_ids"] == "19,23,22,25"
    await rd.lines("nfl", date(2026, 11, 1))
    assert route.calls[1].request.url.params["affiliate_ids"] == "19,23,22,25,3,11"
    rd.extra_affiliates = False
    await rd.lines("nfl", date(2026, 11, 1))
    assert route.calls[2].request.url.params["affiliate_ids"] == "19,23,22,25"


def test_extra_affiliates_map_to_odds_api_book_keys():
    from gridiron_edge.clients.therundown import AFFILIATE
    assert AFFILIATE[3] == "pinnacle" and AFFILIATE[11] == "lowvig" and AFFILIATE[6] == "betonlineag"
