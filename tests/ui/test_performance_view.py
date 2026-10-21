import json
from datetime import datetime, timedelta, timezone

from gridiron_edge.ui import performance as pv

NOW = datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)  # Sun 10:00 AM PT
TABLE = [{"label": "Kalshi game bets", "bets": 12, "won": 7, "lost": 4, "refunded": 1, "cost_cents": 12_345,
          "profit_cents": 1_571, "roi": 0.1273},
         {"label": "PrizePicks", "bets": 3, "won": 0, "lost": 3, "refunded": 0, "cost_cents": 3_000,
          "profit_cents": -3_000, "roi": -1.0}]


def test_signed_money():
    assert pv.signed(420) == "+$4.20" and pv.signed(-310) == "-$3.10" and pv.signed(0) == "$0.00"


def test_type_table_aligned_with_total():
    out = pv.type_table_lines(TABLE)
    assert out[0].split() == ["Bet", "type", "Bets", "Won-lost-refunded", "Money", "bet", "Profit", "Return"]
    assert out[1].split()[-5:] == ["12", "7-4-1", "$123.45", "+$15.71", "+12.7%"]
    assert out[2].endswith("-$30.00 -100.0%")
    assert out[3].startswith("All bets") and "7-7-1" in out[3] and out[3].endswith("-9.3%")
    assert len({len(x) for x in out}) == 1 and max(len(x) for x in out) <= 96  # fits 100 columns
    assert len(pv.type_table_lines(TABLE[:1])) == 2  # no total row for one type
    assert pv.type_table_lines([]) == ["No settled bets yet, so there is nothing to break down by bet type."]


def test_money_lines():
    assert pv.money_lines({"today": 420, "week": -310, "all": 15_710}, 50_000, 0) == [
        "Profit today: +$4.20", "Profit this week: -$3.10", "Profit all time: +$157.10",
        "Money deposited: $500.00", "Money withdrawn: $0.00"]


def bet(i, result, payout, settled):
    legs = [{"venue": "kalshi", "description": "x", "side": "yes", "price": 0.5, "p_fair": 0.55,
             "game_id": "nfl:20261025:DAL@PHI", "ticker": "T", "team": "PHI", "line": None}]
    return {"id": i, "kind": "kalshi_game", "legs_json": json.dumps(legs), "structure": None, "result": result,
            "stake_cents": 500, "fee_cents": 10, "payout_cents": payout, "settled_at": settled.isoformat()}


def test_recent_lines_newest_first_and_cut_to_width():
    rows = [bet(12, "won", 1000, NOW - timedelta(hours=1)), bet(9, "lost", 0, NOW - timedelta(days=2)),
            bet(7, "refund", 510, NOW - timedelta(days=3))]
    out = pv.recent_lines(rows, NOW, 100)
    assert out[0].startswith("9:00 AM PT        won           +$4.90  #12 Eagles beat Cowboys")
    assert out[1].startswith("Fri 10:00 AM PT   lost          -$5.10  #9 ")
    assert " refunded " in out[2] and "$0.00" in out[2]
    narrow = pv.recent_lines(rows, NOW, 50)
    assert all(len(x) <= 50 for x in narrow) and narrow[0].endswith("...")
    assert pv.recent_lines([], NOW) == ["No settled bets yet."]


def test_dollar_ticks():
    vals, labels = pv.dollar_ticks(100_000, 157_100)
    assert labels == ["$1,000", "$1,200", "$1,400"] and vals == [1000, 1200, 1400]
    assert all(lab.startswith("$") for lab in labels)
    vals, labels = pv.dollar_ticks(1_000, 1_300)
    assert labels == ["$10", "$11", "$12", "$13"]
    assert pv.dollar_ticks(1_000, 1_100)[1][:2] == ["$10.00", "$10.25"]
    assert pv.dollar_ticks(5_000, 5_000) == ([50.0], ["$50"])
