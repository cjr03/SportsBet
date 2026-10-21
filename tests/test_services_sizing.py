"""Snapshot wiring for cumulative caps, PrizePicks exclusions and the CLV throttle."""
from datetime import UTC, datetime, timedelta

from gridiron_edge.types import Game, Leg, Recommendation

NOW = datetime(2026, 10, 31, 18, 5, tzinfo=UTC)
KICK = NOW + timedelta(hours=20)


def _game(svc, gid, home="PHI", away="DAL", kick=KICK):
    g = Game(gid, "nfl", 2026, 8, kick, home, away)
    svc.registry.upsert(g)
    return g


def _place(svc, kind, legs, qty=None, stake=0, structure=None, at=NOW):
    rec = Recommendation(None, kind, legs, stake, qty, 0.05, 0.5, structure, at, KICK)
    saved, _ = svc.lifecycle.upsert_recommendation(rec, at)
    return svc.lifecycle.confirm(saved.id, at)


async def test_snapshot_carries_cumulative_exposure(services_mocked):
    svc = services_mocked()
    svc.ledger.deposit(100_000)
    _game(svc, "nfl:a")
    _game(svc, "nfl:b", "KC", "BUF")
    _place(svc, "kalshi_game", [Leg("kalshi", "PHI ML", "yes", 0.50, 0.55, "nfl:a", ticker="T1", team="PHI")], qty=20)
    pp = [Leg("prizepicks", "x", "over", 0.0, 0.6, gid, player=p, stat="rush_yds", line=50.5, team=t)
          for gid, p, t in (("nfl:a", "Jalen Hurts", "PHI"), ("nfl:b", "Josh Allen", "BUF"))]
    _place(svc, "pp_entry", pp, stake=500, structure="2-power")
    snap = svc.snapshot(NOW)
    assert snap.open_game_exposure == {"nfl:a": 1000 + 500, "nfl:b": 500}
    assert sum(snap.open_window_exposure.values()) == 1500
    assert snap.open_dir_exposure == {"nfl:a|home": 1000}
    assert snap.placed_today_cents == 1500
    assert snap.pp_open_players == {"jalen hurts", "josh allen"} and snap.pp_open_games == {"nfl:a", "nfl:b"}
    assert {"jalen hurts", "josh allen"} <= snap.open_players  # no Kalshi prop on a PrizePicks player


async def test_clv_throttle_in_snapshot(services_mocked):
    svc = services_mocked()
    svc.ledger.deposit(100_000)
    _game(svc, "nfl:a")
    for i, c in enumerate([-0.02, -0.03, -0.01, 0.005] * 5):
        bet = _place(svc, "kalshi_game", [Leg("kalshi", "x", "yes", 0.5, 0.5, "nfl:a", ticker=f"T{i}", team="PHI")],
                     qty=2)
        svc.conn.execute("UPDATE bets SET clv=? WHERE id=?", (c, bet))
        svc.settler.manual_settle(bet, "lost", now=NOW)
    snap = svc.snapshot(NOW)
    assert snap.clv_mult["nfl"] == 0.0
    assert all(snap.gates[("nfl", m)] for m in ("ml", "spread", "total"))


async def test_first_run_asks_to_confirm_prizepicks_payouts(services_mocked):
    lines = [m for k, m in await services_mocked().startup(NOW) if k == "info"]
    assert any("PrizePicks payouts not confirmed" in m and "set pp_payouts_verified true" in m for m in lines)
    lines = [m for _, m in await services_mocked(pp_payouts_verified=True).startup(NOW)]
    assert not any("PrizePicks payouts not confirmed" in m for m in lines)


async def test_set_pp_mode_updates_settlement_table(services_mocked):
    svc = services_mocked()
    svc.set_setting("pp_mode", "arena")
    assert svc.settler.payouts[(5, "flex")] == {5: 10}


async def test_bad_prizepicks_setting_is_not_saved(services_mocked):
    import pytest
    svc = services_mocked()
    with pytest.raises(ValueError):
        svc.set_setting("pp_payouts_json", "{bad")
    assert svc._get("set.pp_payouts_json") is None
    assert svc.settings.pp_payouts_json == ""


async def test_first_run_prizepicks_probe_plain_english(services_mocked):
    lines = [m for k, m in await services_mocked().startup(NOW) if k == "info"]
    probe = [m for m in lines if m.startswith("PrizePicks lines:")]
    assert len(probe) == 1 and "working" in probe[0] and "Bears at Seahawks" in probe[0]
    assert not any("unavailable on this key" in m for m in lines)


async def test_first_run_probe_without_prizepicks_lines(services_mocked, load):
    import httpx
    body = load("odds_api_event_props.json")
    body["bookmakers"] = [b for b in body["bookmakers"] if b["key"] != "prizepicks"]
    services_mocked.router.get(url__regex=r"the-odds-api\.com/v4/sports/americanfootball_nfl/events/[^/]+/odds"
                               ).mock(return_value=httpx.Response(200, json=body))
    lines = [m for k, m in await services_mocked().startup(NOW) if k == "info"]
    probe = next(m for m in lines if m.startswith("PrizePicks lines:"))
    assert "none posted yet for Bears at Seahawks" in probe and "3 hours before each kickoff" in probe
    assert '"pp add"' in probe


async def test_pp_games_are_active_prizepicks_ideas_and_open_entries(services_mocked):
    svc = services_mocked()
    svc.ledger.deposit(100_000)
    _game(svc, "nfl:a")
    _game(svc, "nfl:b", "KC", "BUF")
    _game(svc, "nfl:c", "NYJ", "MIA")
    leg = lambda gid: Leg("prizepicks", "x", "over", 0.0, 0.6, gid, player=f"P {gid}", stat="rush_yds", line=50.5)
    _place(svc, "pp_entry", [leg("nfl:a")], stake=500, structure="2-power")
    rec = Recommendation(None, "pp_entry", [leg("nfl:b")], 500, None, 0.05, 0.5, "2-power", NOW, KICK)
    svc.lifecycle.upsert_recommendation(rec, NOW)
    watch = Recommendation(None, "pp_entry", [leg("nfl:c")], 0, None, 0.05, 0.5, "2-power", NOW, KICK,
                           observe_only=True)
    svc.lifecycle.upsert_recommendation(watch, NOW)
    assert svc.pp_games() == {"nfl:a", "nfl:b"}


async def test_pp_refresh_plan_stays_within_budget(services_mocked):
    svc = services_mocked()
    await svc.startup(NOW)
    games, text = svc.pp_refresh_plan(NOW + timedelta(hours=10))
    assert len(games) == 1 and "3 Odds API credits (1 game) out of 480 left this month." in text and text.endswith("pp refresh yes")
    svc.odds.budget.record(52, 448, 3)  # just above the guard's 50-credit floor
    games, text = svc.pp_refresh_plan(NOW + timedelta(hours=10))
    assert games == [] and "Nothing can load now." in text
    assert (await svc.pp_refresh(NOW + timedelta(hours=10))) == [("info", text)]


async def test_pp_refresh_plan_keeps_the_scheduler_floor_on_a_full_cfb_slate(services_mocked):
    """A Saturday's worth of CFB games must not eat the credits kept for game odds and NFL props."""
    from gridiron_edge.scheduler import PROPS_COST, cfb_props_floor
    svc = services_mocked()
    await svc.startup(NOW)
    at = NOW + timedelta(hours=10)
    for i in range(40):
        gid = f"cfb:2026110{i % 2}:T{i}"
        svc.registry.upsert(Game(gid, "cfb", 2026, 10, at + timedelta(hours=2, minutes=i), f"H{i}", f"A{i}"))
        svc.registry.set_odds_event(gid, f"ev{i}")
    floor = cfb_props_floor(at)
    svc.odds.budget.record(floor + 4 * PROPS_COST, 500 - floor - 4 * PROPS_COST, 3)
    games, text = svc.pp_refresh_plan(at)
    assert len(games) == 4 and games[0].league == "nfl" and "Only 4 fit" in text
    assert "NFL player lines still to come" in text
    svc.odds.budget.record(floor, 500 - floor, 3)
    games, text = svc.pp_refresh_plan(at)
    assert games == [] and "Nothing can load now." in text


async def test_props_job_skipped_is_retried_not_marked_done(services_mocked):
    from gridiron_edge.scheduler import PROPS_RETRY, Job
    svc = services_mocked()
    await svc.startup(NOW)
    _game(svc, "nfl:unlisted", "KC", "BUF", kick=NOW + timedelta(hours=2))
    job = Job("odds_props", "nfl", {"game_id": "nfl:unlisted", "window": "slate"})
    lines = await svc.run_job(job, NOW)
    assert "does not list this game yet" in lines[0][1]
    svc.scheduler.mark_done(job, NOW)
    assert job.key not in svc.scheduler.state
    svc.scheduler.mark_done(job, NOW + PROPS_RETRY)  # once the retry wait is over it can be recorded
    assert svc.scheduler.state[job.key] == NOW + PROPS_RETRY
