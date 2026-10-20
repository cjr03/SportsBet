from datetime import UTC, datetime

from gridiron_edge.types import Leg, Recommendation, make_game_id

T = datetime(2026, 9, 13, 17, tzinfo=UTC)


def _rec(price=0.55, ticker="KX-A"):
    legs = [Leg("kalshi", "d", "yes", price, 0.6, "nfl:20260913:A@B", ticker=ticker),
            Leg("prizepicks", "p", "over", 1.0, 0.5, None, player="X", stat="rec_yds", line=40.5)]
    return Recommendation(None, "arb", legs, 100, None, 0.05, 0.6, None, T, T)


def test_game_id():
    assert make_game_id("nfl", T, "KC", "BUF") == "nfl:20260913:KC@BUF"


def test_keys():
    a, b = _rec(0.551), _rec(0.554)
    assert a.dedupe_key == b.dedupe_key and a.leg_key == b.leg_key
    c = _rec(0.60)
    assert c.dedupe_key != a.dedupe_key and c.leg_key == a.leg_key
    assert _rec(ticker="KX-B").leg_key != a.leg_key
    a.legs.reverse()
    assert a.dedupe_key == b.dedupe_key
