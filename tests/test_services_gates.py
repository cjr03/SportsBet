"""fit_gates fails closed: one league's backtest failure never ungates another."""
from datetime import UTC, date, datetime, timedelta

import httpx

from gridiron_edge.clients import http as http_mod
from gridiron_edge.models.blend import BlendFit
from gridiron_edge.scheduler import Job

NOW = datetime(2026, 10, 31, 18, 5, tzinfo=UTC)
GATED_NFL = BlendFit(a=0.0, b_model=0.01, b_market=1.0, b_model_se=0.5, n=600)


def stub_nfl_backtest(svc, fit=GATED_NFL):
    real = svc.backtest

    async def backtest(league, start, end, now):
        if league == "nfl":
            return fit, {"n": fit.n}
        return await real(league, start, end, now)

    svc.backtest = backtest


async def _no_sleep(_):
    return None


async def test_cfbd_500_keeps_nfl_gate_and_gates_cfb(services_mocked, monkeypatch):
    monkeypatch.setattr(http_mod, "_sleep", _no_sleep)
    services_mocked.router.get(url__regex=r"collegefootballdata\.com/lines").mock(
        return_value=httpx.Response(500))
    svc = services_mocked()
    stub_nfl_backtest(svc)
    out = await svc.fit_gates(NOW)
    assert GATED_NFL.gated
    assert svc.gates[("nfl", "ml")] is True and svc.gates[("nfl", "spread")] is True
    assert all(svc.gates[("cfb", m)] for m in ("ml", "spread", "total"))  # failed league fails closed
    assert any(k == "error" and "College football" in m for k, m in out)
    assert svc._get("gates") is None  # a result missing a league is not cached
    assert not svc.gates_complete


async def test_no_history_with_fitted_model_is_gated(services_mocked):
    svc = services_mocked(cfbd_api_key="")
    svc.nfl_model = object()  # fitted model, but FakeNflverse has no history to backtest
    await svc.fit_gates(NOW)
    assert all(svc.gates[("nfl", m)] for m in ("ml", "spread", "total"))
    assert ("cfb", "ml") not in svc.gates  # no model, no history: nothing to gate
    assert svc._get("gates") is None


async def test_complete_result_is_cached(services_mocked):
    svc = services_mocked()
    live = BlendFit(a=0.0, b_model=0.5, b_market=0.5, b_model_se=0.05, n=600)
    calls = []

    async def backtest(league, start, end, now):
        calls.append(league)
        return live, {"n": live.n}

    svc.backtest = backtest
    await svc.fit_gates(NOW - timedelta(days=7))
    assert svc.gates[("nfl", "ml")] is True  # one passing refit is not enough to go live
    await svc.fit_gates(NOW)
    assert svc.gates_complete and svc._get("gates") is not None
    assert svc.gates[("nfl", "ml")] is False and svc.gates[("cfb", "ml")] is False
    await svc.fit_gates(NOW)
    assert calls == ["nfl", "cfb"] * 2  # third call served from cache


async def test_gate_hysteresis_and_per_season_sign(services_mocked):
    svc = services_mocked()
    live = BlendFit(a=0.0, b_model=0.5, b_market=0.5, b_model_se=0.05, n=600)
    state = {"fit": live, "seasons": {"2025": {"n": 300, "b_model": 0.4}}}

    async def backtest(league, start, end, now):
        return state["fit"], {"n": 600, "by_season": state["seasons"]}

    svc.backtest = backtest
    await svc.fit_gates(NOW - timedelta(days=14))
    await svc.fit_gates(NOW - timedelta(days=13, hours=12), force=True)  # forced, hours later: not a 2nd pass
    assert svc.gates[("nfl", "spread")] is True
    await svc.fit_gates(NOW - timedelta(days=7), force=True)
    assert svc.gates[("nfl", "spread")] is False
    state["seasons"] = {"2024": {"n": 300, "b_model": -0.1}, "2025": {"n": 300, "b_model": 0.9}}
    await svc.fit_gates(NOW)  # a season with b <= 0 gates at once
    assert svc.gates[("nfl", "spread")] is True


async def test_nfl_totals_gated_separately(services_mocked):
    svc = services_mocked()
    live = BlendFit(a=0.0, b_model=0.5, b_market=0.5, b_model_se=0.05, n=600)

    async def backtest(league, start, end, now):
        return live, {"n": live.n}

    svc.backtest = backtest
    await svc.fit_gates(NOW - timedelta(days=7))
    await svc.fit_gates(NOW)
    assert svc.gates[("nfl", "spread")] is False
    assert svc.gates[("nfl", "total")] is True  # no totals history: totals model not validated
    assert ("nfl", "total") not in svc.snapshot(NOW).blend_fits

    async def totals(start, end, now):
        return live, {"n": live.n}

    svc.backtest_totals = totals
    await svc.fit_gates(NOW + timedelta(days=7))
    await svc.fit_gates(NOW + timedelta(days=14))
    assert svc.gates[("nfl", "total")] is False
    assert svc.snapshot(NOW).blend_fits[("nfl", "total")] == (0.5, 0.05)


async def test_nfl_backtest_uses_production_margin_model(services_mocked, monkeypatch):
    import polars as pl

    from gridiron_edge import services as services_mod

    svc = services_mocked(cfbd_api_key="")
    svc.history["nfl"] = pl.DataFrame([{"season": y, "week": 1, "gameday": date(y, 9, 10),
                                        "home_team": "KC", "away_team": "BUF", "home_score": 20,
                                        "away_score": 17, "result": 3.0, "spread_line": 2.5,
                                        "total_line": 47.5, "location": "Home"} for y in (2025, 2026)])
    seen = {}

    def fake(league, games, predict, margin_model=None):
        seen["mm"] = margin_model
        return GATED_NFL, {"n": 1}

    monkeypatch.setattr(services_mod, "walkforward_ats", fake)
    await svc.backtest("nfl", 2026, 2026, NOW)
    assert seen["mm"] is svc.margin_models["nfl_spread"]


async def test_nflverse_refresh_refits_gates_when_model_appears(services_mocked):
    svc = services_mocked(cfbd_api_key="")
    await svc.fit_gates(NOW)
    assert ("nfl", "ml") not in svc.gates
    stub_nfl_backtest(svc)

    async def fit_nfl(now):
        svc.nfl_model = object()
        return [("info", "nfl model: stub")]

    svc._fit_nfl = fit_nfl
    out = await svc.run_job(Job("nflverse_refresh"), NOW)
    assert not [ln for ln in out if ln[0] == "error"], out
    assert svc.gates[("nfl", "ml")] is True
    assert any("NFL spreads/moneylines" in m for _, m in out)


async def test_fits_and_caps_reach_the_snapshot(services_mocked):
    svc = services_mocked()
    live = BlendFit(a=0.0, b_model=0.5, b_market=0.5, b_model_se=0.05, n=600)

    async def backtest(league, start, end, now):
        return live, {"n": live.n, "disagree_p95": 4.5 if league == "nfl" else 8.0}

    svc.backtest = backtest
    await svc.fit_gates(NOW)
    snap = svc.snapshot(NOW)
    assert snap.blend_fits[("nfl", "spread")] == (0.5, 0.05)
    assert snap.disagree_caps == {"nfl": 4.5, "cfb": 8.0}


JARGON = ("gate", "b_model", "±", "->", "observe-only", "backtest")


async def test_gate_log_lines_are_plain_english(services_mocked):
    svc = services_mocked(cfbd_api_key="")
    svc.nfl_model = object()
    stub_nfl_backtest(svc)

    async def totals(start, end, now):
        raise ValueError("no nfl totals history for 2024-2026")

    svc.backtest_totals = totals
    out = await svc.fit_gates(NOW)
    msgs = [m for _, m in out]
    nfl = next(m for m in msgs if m.startswith("NFL spreads/moneylines"))
    assert "checked on 600 past games" in nfl
    assert "not yet trusted, bets are shown but not texted" in nfl
    tot = next(m for m in msgs if m.startswith("NFL over/unders"))
    assert "sportsbooks' number only" in tot
    assert "no nfl totals history" not in tot  # raw exception text stays out of the Log tab
    for m in msgs:
        assert not any(j in m for j in JARGON), m


async def test_live_gate_line_is_plain_english(services_mocked):
    svc = services_mocked(cfbd_api_key="")
    live = BlendFit(a=0.0, b_model=0.5, b_market=0.5, b_model_se=0.05, n=600)

    async def backtest(league, start, end, now):
        return live, {"n": live.n}

    svc.backtest = backtest
    await svc.fit_gates(NOW - timedelta(days=7))
    out = await svc.fit_gates(NOW)
    nfl = next(m for _, m in out if m.startswith("NFL spreads/moneylines"))
    assert "trusted, bets are texted" in nfl
    assert not any(j in nfl for j in JARGON), nfl
