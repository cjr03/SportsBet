"""Services side of the fair-pricing audit: quote persistence, margin-model fits, why persistence."""
import json
from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from gridiron_edge.strategy.distributions import ConditionalMarginModel, NormalModel
from gridiron_edge.types import Quote

NOW = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)


def test_quotes_round_trip_with_fetch_time():
    from gridiron_edge.services import _dump_items, _load_items
    q = Quote("therundown", "draftkings", "g", "h2h", "home", None, 1.8, NOW - timedelta(hours=3), NOW)
    old = Quote("odds_api", "pinnacle", "g", "h2h", "home", None, 1.8, NOW)
    back = _load_items(Quote, _dump_items([q, old]))
    assert back == [q, old] and back[1].fetched_at is None


def _sched(n=1200, first=2015):
    rng = np.random.default_rng(3)
    spread = rng.choice(np.arange(-14, 14.5, 0.5), n)
    return pl.DataFrame({"season": rng.integers(first - 3, 2026, n), "spread_line": spread,
                         "result": np.rint(spread + rng.normal(0, 13, n))})


def test_nfl_margin_model_uses_2015_on():
    from gridiron_edge.services import MARGIN_FIRST_SEASON, nfl_margin_model
    sched = _sched()
    m = nfl_margin_model(sched)
    assert isinstance(m, ConditionalMarginModel)
    assert m.n == sched.filter(pl.col("season") >= MARGIN_FIRST_SEASON).height
    assert isinstance(nfl_margin_model(_sched(n=50)), NormalModel)


def test_cfb_margin_model_from_cached_lines_only(services_mocked):
    from gridiron_edge.services import CFB_MARGIN_KEY
    svc = services_mocked()
    out = svc._fit_cfb_margin(NOW)
    assert CFB_MARGIN_KEY not in svc.margin_models
    assert any("rough estimate" in line for _, line in out)
    rng = np.random.default_rng(4)
    rows = []
    for i in range(700):
        s = float(rng.choice(np.arange(-28, 28.5, 0.5)))
        margin = int(np.rint(-s + rng.normal(0, 16)))
        rows.append({"homeTeam": "A", "awayTeam": "B", "homeScore": max(margin, 0) + 10,
                     "awayScore": max(-margin, 0) + 10, "startDate": "2024-10-01",
                     "lines": [{"spread": s}, {"spread": "bad"}]})
    svc.conn.execute("INSERT INTO api_cache (key, fetched_at, immutable, body) VALUES (?,?,1,?)",
                     ("cfbd:/lines?year=2024", NOW.isoformat(), json.dumps(rows)))
    calls = len(services_mocked.router.calls)
    out = svc._fit_cfb_margin(NOW)
    assert len(services_mocked.router.calls) == calls  # cache only: no API call
    m = svc.margin_models[CFB_MARGIN_KEY]
    assert isinstance(m, ConditionalMarginModel) and m.n == 700
    assert any("700" in line for _, line in out)


def test_why_survives_the_database(services_mocked, mk):
    from gridiron_edge.types import Leg, Recommendation
    svc = services_mocked()
    leg = Leg("kalshi", "H1 ML", "yes", 0.48, 0.55, "g1", ticker="T1")
    why = {"fair_source": "pinnacle", "books": ["pinnacle"], "p_market": 0.55, "p_model": None,
           "model_weight": 0.0}
    rec = Recommendation(None, "kalshi_game", [leg], 100, 2, 0.05, 0.55, None, NOW, NOW + timedelta(hours=3),
                         why=why)
    saved, new = svc.lifecycle.upsert_recommendation(rec, NOW)
    assert new and saved.why == why
    plain = Recommendation(None, "kalshi_game", [Leg("kalshi", "x", "yes", 0.3, 0.4, "g1", ticker="T2")],
                           100, 2, 0.05, 0.4, None, NOW, NOW + timedelta(hours=3))
    assert svc.lifecycle.upsert_recommendation(plain, NOW)[0].why is None
