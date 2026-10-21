"""fit_gates fails closed: one league's backtest failure never ungates another."""
from datetime import UTC, datetime

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
    assert any(k == "error" and "gate cfb" in m for k, m in out)
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
    await svc.fit_gates(NOW)
    assert svc.gates_complete and svc._get("gates") is not None
    assert svc.gates[("nfl", "ml")] is False and svc.gates[("cfb", "ml")] is False
    await svc.fit_gates(NOW)
    assert calls == ["nfl", "cfb"]  # second call served from cache


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
    assert any("gate nfl" in m for _, m in out)


async def test_totals_stay_gated_on_a_passing_spread_backtest(services_mocked):
    svc = services_mocked()
    live = BlendFit(a=0.0, b_model=0.5, b_market=0.5, b_model_se=0.05, n=600)

    async def backtest(league, start, end, now):
        return live, {"n": live.n}

    svc.backtest = backtest
    await svc.fit_gates(NOW)
    for lg in ("nfl", "cfb"):
        assert svc.gates[(lg, "spread")] is False and svc.gates[(lg, "ml")] is False
        assert svc.gates[(lg, "total")] is True  # no totals backtest yet
        assert svc.gates[(lg, "prop")] is False  # book-anchored props are market-derived
        assert svc.gates[(lg, "prop_model")] is True  # projector-only props have no backtest
