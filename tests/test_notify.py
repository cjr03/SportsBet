import subprocess
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gridiron_edge.bets.lifecycle import Lifecycle
from gridiron_edge.db import Ledger
from gridiron_edge.notify import NotifyResult
from gridiron_edge.notify.chain import MAX_PER_DAY, MAX_PER_HOUR, RETEXT_GAP, NotifyChain
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
    assert "We put your chance of winning at 58%; the price implies 53%." in t


def test_format_pp_plain_english(rec_pp):
    t = format_message(rec_pp)
    _plain(t)
    assert t.startswith("Bet idea #42 (PrizePicks 3-pick Flex Play, $10.00): "
                        "1) Jalen Hurts MORE than 1.5 passing touchdowns (61%) "
                        "2) Player 1 MORE than 51.5 receiving yards (60%) ")
    assert "Expected profit about $0.94." in t and "Check the app says it pays 3 times your entry for 3 right, your entry back for 2." in t
    assert "Lines as of 10:05 AM PT." in t and t.endswith("When placed, type: confirm 42")


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


def test_arb_stops_at_first_ok_channel(conn, rec_arb, noon):
    a, b = [], []
    ch = NotifyChain(conn, [ConsoleNotifier(a), ConsoleNotifier(b)], (23, 8))
    assert ch.notify(rec_arb, noon).ok and len(a) == 1 and b == []
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1


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
    assert ch.notify(rec_at(0.53), noon + timedelta(minutes=31)) is None
    assert ch.notify(rec_at(0.52), noon + RETEXT_GAP).ok and len(sink) == 2


def test_arb_price_drift_not_retexted(conn, rec_arb, noon):
    lc = _lc(conn)
    sink = []
    ch = NotifyChain(conn, [ConsoleNotifier(sink)], (23, 8))
    a, _ = lc.upsert_recommendation(replace(rec_arb, id=None), noon)
    legs = [replace(leg, price=leg.price - 0.01) for leg in rec_arb.legs]
    b, new = lc.upsert_recommendation(_rec("arb", legs, id=None, qty=20, ev=0.014), noon)
    assert new and a.dedupe_key != b.dedupe_key
    assert ch.notify(a, noon).ok and ch.notify(b, noon + timedelta(minutes=5)) is None and len(sink) == 1


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


def test_format_pp_uses_the_payout_table_in_use(rec_pp):
    t = format_message(rec_pp, payouts={(3, "flex"): {3: 2.25, 2: 1.25}})
    assert "pays 2.25 times your entry for 3 right, 1.25 times for 2" in t


# -- one idea = one text ------------------------------------------------------------


class Phone:
    """A phone channel (counts toward the text limits, unlike the console)."""
    name = "imessage"

    def __init__(self, sink, ok=True):
        self.sink, self.ok = sink, ok

    def send(self, text):
        self.sink.append(text)
        return NotifyResult(self.ok, self.name, None if self.ok else "down")


def _lc(conn):
    return Lifecycle(conn, Ledger(conn))


def _game_rec(ticker, ev=0.05, side="yes", price=0.50, game_id="g", line=None):
    leg = Leg("kalshi", ticker, side, price, 0.60, game_id, ticker=ticker, line=line)
    return _rec("kalshi_game", [leg], id=None, qty=20, ev=ev, expires_at=datetime(2026, 10, 25, 18, 0, tzinfo=PT))


def _ladder_arb(game, a, b, ev=0.03, pa=0.28, pb=0.62):
    """A ladder pair on one game (like the AKR/KENT spread pairs that were each texted on their own)."""
    legs = [Leg("kalshi", a, "yes", pa, pa, game, ticker=f"KXNCAAFSPREAD-{game}-{a}", line=5.5),
            Leg("kalshi", b, "no", pb, pb, game, ticker=f"KXNCAAFSPREAD-{game}-{b}", line=6.5)]
    return _rec("arb", legs, id=None, qty=20, ev=ev, structure="kalshi_ladder")


def test_yes_and_no_on_one_ticker_are_different_ideas():
    yes, no = _game_rec("KXNFLGAME-X"), _game_rec("KXNFLGAME-X", side="no")
    assert yes.leg_key == no.leg_key  # unchanged: the DB and CLV report rely on it
    assert yes.idea_key != no.idea_key
    assert _game_rec("KXNFLGAME-X", price=0.51).idea_key == yes.idea_key  # price is not part of the idea


def test_clear_ev_gain_retexted_inside_gap_small_gain_not(conn, noon):
    lc, sink = _lc(conn), []
    ch = NotifyChain(conn, [ConsoleNotifier(sink)], (23, 8))
    first, _ = lc.upsert_recommendation(_game_rec("KXNFLGAME-X", ev=0.05, price=0.50), noon)
    small, _ = lc.upsert_recommendation(_game_rec("KXNFLGAME-X", ev=0.065, price=0.49), noon)
    big, _ = lc.upsert_recommendation(_game_rec("KXNFLGAME-X", ev=0.08, price=0.48), noon)
    assert ch.notify(first, noon).ok
    assert ch.notify(small, noon + timedelta(minutes=10)) is None  # +0.015: under the 0.02 bar
    assert ch.notify(big, noon + timedelta(minutes=20)).ok  # +0.03 and 1.6x
    nearly, _ = lc.upsert_recommendation(_game_rec("KXNFLGAME-X", ev=0.099, price=0.47), noon)
    assert ch.notify(nearly, noon + timedelta(minutes=30)) is None  # +0.019 over the last texted 0.08
    assert len(sink) == 2


def test_ev_gain_must_also_be_a_quarter_better(conn, noon):
    lc, sink = _lc(conn), []
    ch = NotifyChain(conn, [ConsoleNotifier(sink)], (23, 8))
    first, _ = lc.upsert_recommendation(_game_rec("KXNFLGAME-X", ev=0.20, price=0.50), noon)
    more, _ = lc.upsert_recommendation(_game_rec("KXNFLGAME-X", ev=0.23, price=0.49), noon)
    assert ch.notify(first, noon).ok and ch.notify(more, noon + timedelta(minutes=5)) is None  # 1.15x


def test_idea_retexted_after_gap(conn, noon):
    lc, sink = _lc(conn), []
    ch = NotifyChain(conn, [ConsoleNotifier(sink)], (23, 8))
    a, _ = lc.upsert_recommendation(_game_rec("KXNFLGAME-X", price=0.50), noon)
    b, _ = lc.upsert_recommendation(_game_rec("KXNFLGAME-X", price=0.51), noon)
    assert ch.notify(a, noon).ok
    assert ch.notify(b, noon + RETEXT_GAP - timedelta(minutes=1)) is None
    assert ch.notify(b, noon + RETEXT_GAP).ok and len(sink) == 2


def test_ladder_pairs_on_one_game_are_one_text(conn, noon):
    lc, sink = _lc(conn), []
    ch = NotifyChain(conn, [ConsoleNotifier(sink)], (23, 8))
    a, _ = lc.upsert_recommendation(_ladder_arb("26OCT24AKRKENT", "AKR6", "KENT7"), noon)
    b, _ = lc.upsert_recommendation(_ladder_arb("26OCT24AKRKENT", "AKR3", "KENT10"), noon)
    assert a.leg_key != b.leg_key and a.family_key == b.family_key
    assert ch.notify(a, noon).ok and ch.notify(b, noon + timedelta(minutes=5)) is None and len(sink) == 1
    other, _ = lc.upsert_recommendation(_ladder_arb("26OCT24MISSTEX", "MISS15", "TEX17"), noon)
    assert other.family_key != a.family_key and ch.notify(other, noon).ok


def test_family_key_falls_back_to_event_ticker():
    a = _ladder_arb(None, "AKR6", "KENT7")
    a.legs = [replace(g, ticker=f"KXNCAAFSPREAD-26OCT24AKRKENT-{g.description}") for g in a.legs]
    b = _ladder_arb(None, "AKR3", "KENT10")
    b.legs = [replace(g, ticker=f"KXNCAAFSPREAD-26OCT24AKRKENT-{g.description}") for g in b.legs]
    assert a.family_key == b.family_key and a.idea_key != b.idea_key


def test_seventh_idea_in_an_hour_is_held_and_summarised(conn, noon):
    lc, sink = _lc(conn), []
    ch = NotifyChain(conn, [Phone(sink)], (23, 8))
    recs = [lc.upsert_recommendation(_game_rec(f"KXNFLGAME-{i}", game_id=f"g{i}"), noon)[0]
            for i in range(MAX_PER_HOUR + 1)]
    for i, r in enumerate(recs[:MAX_PER_HOUR]):
        assert ch.notify(r, noon + timedelta(minutes=i)).ok
    held = ch.notify(recs[-1], noon + timedelta(minutes=10))
    assert held == NotifyResult(False, "held", "rate limit") and ch.held == [recs[-1]]
    res = ch.flush_held(noon + timedelta(minutes=10))
    assert res.ok and len(sink) == MAX_PER_HOUR + 1 and f"#{recs[-1].id}" in sink[-1]
    assert len(sink[-1]) <= 480 and sink[-1].isascii() and ch.held == []
    row = conn.execute("SELECT rec_id, dedupe_key FROM notifications ORDER BY id DESC LIMIT 1").fetchone()
    assert row["rec_id"] is None and row["dedupe_key"] == "summary"
    assert ch.flush_held(noon + timedelta(minutes=11)) is None  # nothing held


def test_one_summary_per_hour(conn, noon):
    lc, sink = _lc(conn), []
    ch = NotifyChain(conn, [Phone(sink)], (23, 8))
    for i in range(MAX_PER_HOUR + 2):
        r, _ = lc.upsert_recommendation(_game_rec(f"KXNFLGAME-{i}", game_id=f"g{i}"), noon)
        ch.notify(r, noon)
        if i == MAX_PER_HOUR:
            assert ch.flush_held(noon).ok
    assert ch.flush_held(noon + timedelta(minutes=30)) is None and len(ch.held) == 1
    assert ch.flush_held(noon + timedelta(minutes=61)).ok and ch.held == []


def test_daily_cap_holds_and_summary_goes_to_console_only(conn, noon):
    lc = _lc(conn)
    for i in range(MAX_PER_DAY):  # 30 texts over the last day, none in the last hour
        conn.execute("INSERT INTO notifications (ts, rec_id, dedupe_key, channel, ok, text) "
                     "VALUES (?,?,?,?,1,'x')",
                     ((noon - timedelta(hours=2, minutes=i * 40)).isoformat(), 1000 + i, f"k{i}", "imessage"))
    phone, log = [], []
    ch = NotifyChain(conn, [Phone(phone), ConsoleNotifier(log)], (23, 8))
    r, _ = lc.upsert_recommendation(_game_rec("KXNFLGAME-NEW"), noon)
    assert ch.notify(r, noon).channel == "held"
    res = ch.flush_held(noon)
    assert res.ok and res.channel == "console" and phone == [] and f"#{r.id}" in log[0]


def test_burst_of_120_arbs_is_six_texts_and_one_summary(conn, noon):
    lc, sink = _lc(conn), []
    ch = NotifyChain(conn, [Phone(sink), ConsoleNotifier([])], (23, 8))
    for i in range(120):
        r, _ = lc.upsert_recommendation(_ladder_arb(f"G{i}", "A6", "B7", ev=0.01 + i / 1000), noon)
        ch.notify(r, noon)
    ch.flush_held(noon)
    assert len(sink) == MAX_PER_HOUR + 1
    assert conn.execute("SELECT COUNT(*) FROM notifications WHERE channel='console'").fetchone()[0] == 0


def test_upgrade_fills_keys_for_texts_already_sent(conn, noon):
    lc = _lc(conn)
    a, _ = lc.upsert_recommendation(_ladder_arb("26OCT24AKRKENT", "AKR6", "KENT7"), noon)
    conn.execute("DROP TABLE notifications")
    conn.execute("CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
                 "rec_id INTEGER, dedupe_key TEXT, channel TEXT NOT NULL, ok INTEGER NOT NULL, error TEXT, "
                 "text TEXT NOT NULL)")
    conn.execute("INSERT INTO notifications (ts, rec_id, dedupe_key, channel, ok, text) VALUES (?,?,?,?,1,'x')",
                 (noon.isoformat(), a.id, a.dedupe_key, "imessage"))
    ch = NotifyChain(conn, [ConsoleNotifier([])], (23, 8))
    b, _ = lc.upsert_recommendation(_ladder_arb("26OCT24AKRKENT", "AKR3", "KENT10"), noon)
    assert ch.notify(b, noon + timedelta(minutes=10)) is None  # same game as the text before the upgrade
