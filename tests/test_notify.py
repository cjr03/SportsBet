import subprocess
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from gridiron_edge.notify.chain import NotifyChain
from gridiron_edge.notify.console import ConsoleNotifier
from gridiron_edge.notify.format import format_message
from gridiron_edge.notify.imessage import IMessageNotifier
from gridiron_edge.notify.ntfy import NtfyNotifier
from gridiron_edge.types import Leg, Recommendation

PT = ZoneInfo("America/Los_Angeles")


def _rec(kind, legs, **kw):
    base = dict(id=42, kind=kind, legs=legs, stake_cents=1000, qty=None, ev=0.05, p_win=0.6,
                structure=None, snapshot_ts=datetime(2026, 10, 25, 10, 5, tzinfo=PT),
                expires_at=datetime(2026, 10, 25, 13, 0, tzinfo=PT))
    base.update(kw)
    return Recommendation(**base)


GID = "nfl:20261025:DAL@PHI"


@pytest.fixture
def rec_kalshi():
    leg = Leg("kalshi", "PHI wins by >3.5", "yes", 0.47, 0.52, GID, ticker="KXNFLSPREAD-26OCT25DALPHI-PHI3",
              team="PHI", line=3.5)
    return _rec("kalshi_game", [leg], stake_cents=1880, qty=40, id=41)


@pytest.fixture
def rec_pp():
    legs = [Leg("prizepicks", f"P{i}", "over" if i % 2 else "under", 0.0, 0.6, GID,
                player=f"Player {i}", stat="rec_yds", line=50.5 + i) for i in range(3)]
    legs[0] = Leg("prizepicks", "JH", "over", 0.0, 0.61, GID, player="Jalen Hurts", stat="pass_tds", line=1.5)
    return _rec("pp_entry", legs, structure="3-flex", stake_cents=1000, ev=0.094)


@pytest.fixture
def rec_arb():
    legs = [Leg("kalshi", "A", "yes", 0.47, 0.47, GID, ticker="KXNFLGAME-X-PHI", team="PHI"),
            Leg("kalshi", "B", "yes", 0.48, 0.48, GID, ticker="KXNFLGAME-X-DAL", team="DAL")]
    return _rec("arb", legs, qty=20, ev=0.004, tie_risk=True, id=43, structure="kalshi_ml_pair",
                stake_cents=2010)


@pytest.fixture
def noon():
    return datetime(2026, 10, 25, 12, 0, tzinfo=PT)


@pytest.fixture
def midnight():
    return datetime(2026, 10, 26, 0, 30, tzinfo=PT)


JARGON = ("EV", "CLV", "fair", "Brier", "KXNFL", "YES @", " x40", " x20", "|")


def _plain(t):
    assert t.isascii() and len(t) <= 480
    assert not [w for w in JARGON if w in t], t


def test_format_kalshi_plain_english(rec_kalshi):
    t = format_message(rec_kalshi)
    _plain(t)
    assert t == ('Bet idea #41 (Kalshi): Buy 40 "Yes" contracts on "Eagles win by 4 or more points vs Cowboys" '
                 'at 47 cents. Cost $18.80 + $0.70 fee. We put this at 52%; the price implies 47%. '
                 'Expected profit about $1.30. Place before 1:00 PM PT. When placed, type: confirm 41')


def test_format_kalshi_no_side_explains(rec_kalshi):
    rec_kalshi.legs = [replace(rec_kalshi.legs[0], side="no", price=0.53, p_fair=0.58)]
    t = format_message(rec_kalshi)
    _plain(t)
    assert '"No" contracts on "Eagles win by 4 or more points vs Cowboys"' in t
    assert "you win if it does NOT happen" in t and t.endswith("When placed, type: confirm 41")


def test_format_pp_plain_english(rec_pp):
    t = format_message(rec_pp)
    _plain(t)
    assert t.startswith("Bet idea #42 (PrizePicks 3-pick Flex Play, $10.00): "
                        "1) Jalen Hurts MORE than 1.5 passing touchdowns (61%) "
                        "2) Player 1 MORE than 51.5 receiving yards (60%) ")
    assert "Expected profit about $0.94." in t and "Check the app shows 3x / 1x payouts" in t
    assert "Lines as of 10:05 AM." in t and t.endswith("When placed, type: confirm 42")


def test_format_pp_long_entry_fits(rec_pp):
    legs = [Leg("prizepicks", "x", "over", 0.0, 0.6, GID, player=f"Christian McCaffrey-Longname {i}",
                stat="rush_reception_yds", line=100.5) for i in range(6)]
    t = format_message(_rec("pp_entry", legs, structure="6-flex", stake_cents=2000))
    _plain(t)
    assert t.endswith("When placed, type: confirm 42")


def test_format_arb_locked_profit(rec_arb):
    t = format_message(rec_arb)
    _plain(t)
    assert t.startswith('Locked profit #43 (Kalshi): Buy 20 "Yes" on "Eagles beat Cowboys" AND '
                        '20 "Yes" on "Cowboys beat Eagles". Total $19.00 + $0.70 fees; you get $20.00 back '
                        'whichever team wins (profit about $0.30).')
    assert "a tie pays nothing" in t and "Place both right away; prices move fast." in t
    assert t.endswith("When placed, type: confirm 43")


def test_imessage_passes_argv_not_interpolated():
    calls = []
    n = IMessageNotifier("+15555550100", runner=lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0))
    evil = 'hi "; do shell script "rm -rf ~"'
    assert n.send(evil).ok and calls[0][0] == "osascript" and calls[0][-3:] == ["+15555550100", evil, "iMessage"]


def test_ntfy_posts_headers():
    seen = {}

    def post(url, **kw):
        seen.update(url=url, **kw)

        class R:
            def raise_for_status(self): pass
        return R()

    r = NtfyNotifier("topic", post=post).send("hello")
    assert r.ok and seen["url"] == "https://ntfy.sh/topic"
    assert seen["headers"] == {"Title": "Gridiron Edge", "Priority": "4"}


def test_chain_dedupe_and_fallback(conn, rec_kalshi, noon):
    def boom(*a, **k): raise subprocess.TimeoutExpired("osascript", 30)
    sink = []
    ch = NotifyChain(conn, [IMessageNotifier("+15555550100", runner=boom), ConsoleNotifier(sink)], (23, 8))
    assert ch.notify(rec_kalshi, noon).channel == "console" and ch.notify(rec_kalshi, noon) is None and len(sink) == 1
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 2


def test_quiet_hours_suppress_non_arb(conn, rec_kalshi, rec_arb, midnight):
    sink = []
    ch = NotifyChain(conn, [ConsoleNotifier(sink)], (23, 8))
    assert ch.notify(rec_kalshi, midnight) is None and ch.notify(rec_arb, midnight).ok
    assert ch.notify(rec_kalshi, midnight, force=True).ok


def test_arb_sends_to_every_channel(conn, rec_arb, noon):
    a, b = [], []
    ch = NotifyChain(conn, [ConsoleNotifier(a), ConsoleNotifier(b)], (23, 8))
    assert ch.notify(rec_arb, noon).ok and len(a) == 1 and len(b) == 1


def test_observe_only_never_sent(conn, rec_kalshi, noon):
    rec_kalshi.observe_only = True
    assert NotifyChain(conn, [ConsoleNotifier([])], (23, 8)).notify(rec_kalshi, noon) is None


def test_disabled_and_send_text(conn, rec_kalshi, noon, midnight):
    sink = []
    ch = NotifyChain(conn, [ConsoleNotifier(sink)], (23, 8), enabled=False)
    assert ch.notify(rec_kalshi, noon) is None
    assert ch.send_text("GE test", midnight).ok and sink == ["GE test"]


@pytest.mark.parametrize("exc", [
    subprocess.CalledProcessError(1, ["osascript", "-e", "s", "+15555550100", "hi", "iMessage"],
                                  stderr=b"execution error: Messages got an error (-1700)"),
    subprocess.TimeoutExpired(["osascript", "-e", "s", "+15555550100", "hi", "iMessage"], 30),
    OSError("osascript not found"),
])
def test_imessage_error_never_contains_phone(exc):
    def runner(args, **kw):
        raise exc
    result = IMessageNotifier("+15555550100", runner=runner).send("hi")
    assert not result.ok and result.error and "+15555550100" not in result.error
    if isinstance(exc, subprocess.CalledProcessError):
        assert result.error.startswith("osascript exit 1:") and "-1700" in result.error


def test_cooldown_one_text_per_contract_while_price_drifts(conn, noon):
    from datetime import timedelta

    from gridiron_edge.bets.lifecycle import Lifecycle
    from gridiron_edge.db import Ledger

    lc = Lifecycle(conn, Ledger(conn))
    sink = []
    ch = NotifyChain(conn, [ConsoleNotifier(sink)], (23, 8))

    def rec_at(price):
        leg = Leg("kalshi", "KC ML", "yes", price, 0.60, "g", ticker="KXNFLGAME-X")
        saved, new = lc.upsert_recommendation(_rec("kalshi_game", [leg], id=None, qty=20), noon)
        assert new
        return saved

    first, second = rec_at(0.55), rec_at(0.54)
    assert first.leg_key == second.leg_key and first.dedupe_key != second.dedupe_key
    assert ch.notify(first, noon).ok
    assert ch.notify(second, noon + timedelta(minutes=5)) is None
    assert len(sink) == 1
    assert ch.notify(rec_at(0.53), noon + timedelta(minutes=31)).ok and len(sink) == 2


def test_cooldown_does_not_hold_back_arbs(conn, rec_arb, noon):
    from datetime import timedelta

    from gridiron_edge.bets.lifecycle import Lifecycle
    from gridiron_edge.db import Ledger

    lc = Lifecycle(conn, Ledger(conn))
    sink = []
    ch = NotifyChain(conn, [ConsoleNotifier(sink)], (23, 8))
    a, _ = lc.upsert_recommendation(rec_arb, noon)
    legs = [replace(leg, price=leg.price - 0.01) for leg in rec_arb.legs]
    b, _ = lc.upsert_recommendation(_rec("arb", legs, qty=10, ev=0.05), noon)
    assert ch.notify(a, noon).ok and ch.notify(b, noon + timedelta(minutes=5)).ok and len(sink) == 2


def test_format_kalshi_fee_uses_market_multiplier(rec_kalshi):
    from dataclasses import replace
    rec = replace(rec_kalshi, legs=[replace(rec_kalshi.legs[0], fee_multiplier=0.5)])
    assert "Cost $18.80 + $0.35 fee." in format_message(rec)  # half of the $0.70 standard fee


def test_format_arb_legging_guidance(rec_arb):
    from dataclasses import replace
    legs = [replace(rec_arb.legs[0], worst_price=0.45), replace(rec_arb.legs[1], worst_price=0.52)]
    t = format_message(replace(rec_arb, legs=legs))
    assert 'Buy the "Eagles beat Cowboys" leg first at 45 cents or less, then the other at 52 cents or less.' in t
    assert "Place both right away" not in t and t.endswith("When placed, type: confirm 43")


def test_format_middle_plain_english(rec_arb):
    from dataclasses import replace
    legs = [Leg("kalshi", "Eagles win by 3 or more points", "yes", 0.52, 0.6, "g", ticker="KXNFLSPREAD-A",
                line=2.5, team="PHI", worst_price=0.52),
            Leg("kalshi", "Eagles win by 7 or more points", "no", 0.50, 0.6, "g", ticker="KXNFLSPREAD-B",
                line=6.5, team="PHI", worst_price=0.56)]
    t = format_message(replace(rec_arb, kind="middle", legs=legs, qty=20, p_win=0.18, stake_cents=0,
                               tie_risk=False, id=44))
    assert t.isascii() and len(t) <= 480 and t.startswith("Not risk-free #44 (Kalshi): ")
    assert ('Buy 20 "Yes" on "Eagles win by 3 or more points" AND 20 "No" on "Eagles win by 7 or more points".'
            in t)
    assert "Total $20.40 + $0.70 fees; you get $20.00 back, or $40.00 if the Eagles win by 3 to 6 points" in t
    assert "(about 18% chance)" in t
    assert 'Buy the "Eagles win by 3 or more points" leg first, then the other at 56 cents or less.' in t
    assert "KXNFL" not in t and " x20" not in t and t.endswith("When placed, type: confirm 44")


TEXT_JARGON = ("EV", "CLV", "fair", "YES @", "Brier", " x40", " x20", "KXNFL", "GE ", "ML", "_yds", "_tds")


def test_every_rec_kind_formats_in_plain_english(rec_kalshi, rec_pp, rec_arb):
    prop = _rec("kalshi_prop", [Leg("kalshi", "Jalen Hurts pass_tds over 1.5", "no", 0.40, 0.66, GID,
                                    ticker="KXNFLPASSTDS-X", player="Jalen Hurts", stat="pass_tds", line=1.5,
                                    team="PHI")], qty=25, stake_cents=1000, id=45)
    middle = _rec("middle", [Leg("kalshi", "m1", "yes", 0.52, 0.6, GID, ticker="KXNFLSPREAD-A", team="PHI",
                                 line=2.5),
                             Leg("kalshi", "m2", "no", 0.50, 0.6, GID, ticker="KXNFLSPREAD-B", team="PHI",
                                 line=6.5)], qty=20, p_win=0.18, stake_cents=0, id=44)
    for r in (rec_kalshi, prop, rec_pp, rec_arb, middle):
        t = format_message(r)
        assert t.isascii() and len(t) <= 480 and t.endswith(f"When placed, type: confirm {r.id}"), t
        assert not [j for j in TEXT_JARGON if j in t], t
    assert '"Jalen Hurts 2 or more passing touchdowns" (you win if it does NOT happen)' in format_message(prop)
    assert format_message(middle).startswith('Not risk-free #44 (Kalshi): Buy 20 "Yes" on "Eagles win by 3 or '
                                             'more points vs Cowboys" AND 20 "No" on')
