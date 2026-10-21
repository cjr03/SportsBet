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
