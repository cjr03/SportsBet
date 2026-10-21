"""Services model wiring: neutral sites, priors, CLV, weights."""
from datetime import UTC, date, datetime, timedelta

import httpx
import polars as pl

from gridiron_edge.types import Game, make_game_id

NOW = datetime(2026, 10, 31, 18, 5, tzinfo=UTC)


def _game(league, home, away, kickoff, neutral=False):
    return Game(make_game_id(league, kickoff, away, home), league, 2026, None, kickoff, home, away, neutral)


def test_nflverse_location_marks_registered_game_neutral(services_mocked):
    svc = services_mocked(cfbd_api_key="")
    ko = NOW + timedelta(days=1)
    g = _game("nfl", "KC", "BUF", ko)
    svc.registry.upsert(g)
    sched = pl.DataFrame([{"season": 2026, "week": 9, "gameday": ko.date(), "home_team": "KC",
                           "away_team": "BUF", "location": "Neutral"}])
    svc._assign_nfl_weeks(sched, NOW)
    got = svc.registry.get(g.game_id)
    assert got.week == 9 and got.neutral


def test_nflverse_neutral_matches_swapped_home_away(services_mocked):
    svc = services_mocked(cfbd_api_key="")
    ko = NOW + timedelta(days=1)
    g = _game("nfl", "KC", "BUF", ko)
    svc.registry.upsert(g)
    sched = pl.DataFrame([{"season": 2026, "week": 9, "gameday": ko.date(), "home_team": "BUF",
                           "away_team": "KC", "location": "Neutral"}])
    svc._assign_nfl_weeks(sched, NOW)
    assert svc.registry.get(g.game_id).neutral


async def test_cfbd_neutral_site_copied_to_registry(services_mocked):
    ko = NOW + timedelta(days=1)
    services_mocked.router.get(url__regex=r"collegefootballdata\.com/games\?.*year=2026").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "season": 2026, "week": 10, "startDate": ko.isoformat().replace("+00:00", "Z"),
             "homeTeam": "Georgia", "awayTeam": "Florida", "homePoints": None, "awayPoints": None,
             "neutralSite": True}]))
    svc = services_mocked()
    svc.teams.add("cfb", "Georgia", "UGA")
    svc.teams.add("cfb", "Florida", "FLA")
    g = _game("cfb", "UGA", "FLA", ko)
    svc.registry.upsert(g)
    await svc._fit_cfb(NOW)
    assert svc.registry.get(g.game_id).neutral


async def test_cfbd_postseason_neutral_in_december(services_mocked):
    now = datetime(2026, 12, 20, 18, 0, tzinfo=UTC)
    ko = now + timedelta(days=2)
    services_mocked.router.get(url__regex=r"collegefootballdata\.com/games\?.*seasonType=postseason").mock(
        return_value=httpx.Response(200, json=[
            {"id": 2, "season": 2026, "week": 1, "startDate": ko.isoformat().replace("+00:00", "Z"),
             "homeTeam": "Georgia", "awayTeam": "Florida", "homePoints": None, "awayPoints": None,
             "neutralSite": True}]))
    svc = services_mocked()
    svc.teams.add("cfb", "Georgia", "UGA")
    svc.teams.add("cfb", "Florida", "FLA")
    g = _game("cfb", "UGA", "FLA", ko)
    svc.registry.upsert(g)
    await svc._fit_cfb(now)
    assert svc.registry.get(g.game_id).neutral


def test_cfb_clv_recorded_from_market(services_mocked, mk):
    from gridiron_edge.models.cfb import CfbRatings
    from gridiron_edge.types import Leg, Quote, Recommendation

    svc = services_mocked()
    ko = NOW + timedelta(minutes=20)
    g = _game("cfb", "UGA", "FLA", ko)
    svc.registry.upsert(g)
    svc.cfb_model = CfbRatings({"UGA": 10.0, "FLA": 7.0}, 0.0)  # agrees with the line
    m = mk("T-UGA", "E", "ml", "UGA", yes_ask=0.55, game_id=g.game_id)
    svc.latest["kalshi"] = [m]
    svc.latest["quotes"] = [Quote("odds_api", "pinnacle", g.game_id, "h2h", "home", None, 1.60, NOW),
                            Quote("odds_api", "pinnacle", g.game_id, "h2h", "away", None, 2.50, NOW),
                            Quote("odds_api", "pinnacle", g.game_id, "spreads", "home", -3.0, 1.95, NOW),
                            Quote("odds_api", "pinnacle", g.game_id, "spreads", "away", 3.0, 1.95, NOW)]
    leg = Leg("kalshi", "UGA ML", "yes", 0.55, 0.6, g.game_id, ticker="T-UGA", team="UGA")
    rec, _ = svc.lifecycle.upsert_recommendation(
        Recommendation(None, "kalshi_game", [leg], 0, 10, 0.05, 0.6, None, NOW, ko), NOW)
    svc.ledger.deposit(100_00)
    bet = svc.lifecycle.confirm(rec.id, NOW)
    out = svc._record_clv(NOW)
    assert any(f"clv bet {bet}" in msg for _, msg in out), out


def _line_rows(year, strong="Georgia", weak="Akron", n=8):
    rows = []
    for k in range(n):
        d = datetime(year, 9, 1, 23, tzinfo=UTC) + timedelta(days=7 * k)
        home, away = (strong, weak) if k % 2 == 0 else (weak, strong)
        hs, as_ = (42, 10) if home == strong else (10, 42)
        rows.append({"homeTeam": home, "awayTeam": away, "homeScore": hs, "awayScore": as_, "week": k + 1,
                     "startDate": d.isoformat().replace("+00:00", "Z"), "neutralSite": False,
                     "lines": [{"spread": -24.0 if home == strong else 24.0}]})
    return rows


def test_cfb_gate_span_is_two_seasons():
    from gridiron_edge.services import Services

    assert Services._gate_spans(NOW)["cfb"] == (2024, 2025)


async def test_cfb_backtest_uses_last_season_prior(services_mocked):
    import re

    def lines(request):
        year = int(re.search(r"year=(\d+)", str(request.url)).group(1))
        return httpx.Response(200, json=_line_rows(year) if year in (2023, 2024) else [])

    services_mocked.router.get(url__regex=r"collegefootballdata\.com/lines").mock(side_effect=lines)
    svc = services_mocked()
    games, predict_asof = await svc._cfb_inputs([2024, 2025], NOW.date())
    assert {g["season"] for g in games} == {2024}
    early = predict_asof(date(2024, 8, 26))  # before any 2024 game: the 2023-based prior alone
    assert early({"home": "Georgia", "away": "Akron", "neutral": True}) > 5.0
