from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from gridiron_edge import describe as d
from gridiron_edge.types import Leg, Recommendation

PT = ZoneInfo("America/Los_Angeles")
GID = "nfl:20261025:DAL@PHI"
NOW = datetime(2026, 10, 25, 12, 30, tzinfo=PT)


def kleg(**kw):
    base = dict(venue="kalshi", description="PHI wins by >3.5", side="yes", price=0.47, p_fair=0.52,
                game_id=GID, ticker="KXNFLSPREAD-26OCT25DALPHI-PHI3", team="PHI", line=3.5)
    base.update(kw)
    return Leg(**base)


def rec(kind="kalshi_game", legs=None, **kw):
    base = dict(id=41, kind=kind, legs=legs or [kleg()], stake_cents=1880, qty=40, ev=0.06,
                p_win=0.52, structure=None, snapshot_ts=NOW, expires_at=NOW + timedelta(minutes=25))
    base.update(kw)
    return Recommendation(**base)


def test_team_names_nfl_and_fallback():
    assert d.team_name("nfl", "PHI") == "Eagles"
    assert d.team_name("nfl", "PHI", full=True) == "Philadelphia Eagles"
    assert d.team_name("cfb", "OSU") == "OSU"
    names = d.Names({("cfb", "OSU"): "Ohio State"})
    assert names.team("cfb", "OSU") == "Ohio State" and names.team("nfl", "DAL") == "Cowboys"


def test_kalshi_outcomes_in_words():
    assert d.leg_text(kleg()) == "Eagles win by 4 or more points vs Cowboys"
    assert d.leg_text(kleg(side="no")) == "Eagles do NOT win by 4 or more points vs Cowboys"
    ml = kleg(ticker="KXNFLGAME-26OCT25DALPHI-PHI", line=None)
    assert d.leg_text(ml) == "Eagles beat Cowboys"
    assert d.leg_text(kleg(ticker="KXNFLTOTAL-X", team=None, line=44.5)) == \
        "Cowboys vs Eagles total over 44.5 points"
    assert d.leg_text(kleg(ticker="KXNFLTOTAL-X", team=None, line=44.5, side="no")) == \
        "Cowboys vs Eagles total under 44.5 points"
    prop = kleg(ticker="KXNFLPASSYDS-X", team=None, player="Jalen Hurts", stat="pass_yds", line=249.5)
    assert d.leg_text(prop) == "Jalen Hurts 250 or more passing yards"
    tds = kleg(ticker=None, team=None, player="Jalen Hurts", stat="pass_tds", line=1.5)
    assert d.leg_text(tds) == "Jalen Hurts 2 or more passing touchdowns"


def test_prizepicks_leg_and_entry():
    leg = Leg("prizepicks", "x", "over", 0.0, 0.61, GID, player="Jalen Hurts", stat="pass_tds", line=1.5)
    assert d.leg_text(leg) == "Jalen Hurts MORE than 1.5 passing touchdowns"
    r = rec("pp_entry", [leg, Leg("prizepicks", "y", "under", 0.0, 0.6, GID, player="A B",
                                  stat="rush_yds", line=50.5)], structure="2-power", qty=None,
            stake_cents=1000)
    assert d.bet_text(r).startswith("2-pick Power Play: Jalen Hurts MORE")
    assert "A B LESS than 50.5 rushing yards" in d.bet_text(r)


def test_times_and_money():
    assert d.clock(datetime(2026, 10, 25, 19, 55, tzinfo=timezone.utc)) == "12:55 PM"
    assert d.clock(datetime(2026, 10, 25, 20, 5, tzinfo=timezone.utc)) == "1:05 PM"
    assert d.when(NOW + timedelta(days=1), NOW) == "Mon 12:30 PM"
    assert d.when(NOW + timedelta(hours=1), NOW) == "1:30 PM"
    assert d.money(128450) == "$1,284.50" and d.money(-500) == "-$5.00"
    assert d.countdown(NOW + timedelta(minutes=12, seconds=30), NOW) == "12 min left"
    assert d.countdown(NOW + timedelta(minutes=75), NOW) == "1 hr 15 min left"
    assert d.countdown(NOW + timedelta(seconds=20), NOW) == "under 1 min left"
    assert d.countdown(NOW - timedelta(seconds=1), NOW) == "expired"


def test_expected_profit_and_fees():
    r = rec()
    assert d.fee_cents(r) == 70  # ceil(0.07 * 40 * .47 * .53 * 100)
    assert d.expected_profit_cents(r) == round(40 * 52 - 1880 - 70)
    arb = rec("arb", [kleg(ticker="A", side="yes", price=0.48, p_fair=0.48, line=None),
                      kleg(ticker="B", team="DAL", side="yes", price=0.49, p_fair=0.49, line=None)],
              qty=20, ev=0.01, stake_cents=1990)
    assert d.fee_cents(arb) == 70  # 35 + 35
    assert d.expected_profit_cents(arb) == 2000 - 960 - 980 - 70
    pp = rec("pp_entry", [kleg(venue="prizepicks")], qty=None, ev=0.094, stake_cents=1000)
    assert d.expected_profit_cents(pp) == 94


def test_why_lines_plain_then_jargon_last():
    lines = d.why_lines(rec(), NOW, kelly_fraction=0.25, cap_bet=0.02)
    body = " ".join(lines[:-1])
    assert "Our chance: 52%" in body and "price implies 47%" in body
    assert "$0.70" in body and "12:55 PM" in body
    for word in ("EV", "CLV", "fair", "Brier", "KXNFL", "x40"):
        assert word not in body
    assert "KXNFLSPREAD" in lines[-1] and "fair" in lines[-1]


def test_why_uses_rec_why_dict():
    r = rec()
    r.why = {"source": "Pinnacle and Circa lines, 30% our model", "sizing": "capped at 2% of bankroll"}
    text = " ".join(d.why_lines(r, NOW))
    assert "Pinnacle and Circa lines, 30% our model" in text and "capped at 2% of bankroll" in text


def test_no_jargon_in_bet_text():
    for r in (rec(), rec("arb", [kleg(ticker="A", line=None), kleg(ticker="B", team="DAL", line=None)])):
        t = d.bet_text(r)
        assert "KX" not in t and "ML" not in t and ">" not in t
    assert d.bet_text(rec("arb", [kleg(ticker="A", line=None), kleg(ticker="B", team="DAL", line=None)])) \
        == "Eagles beat Cowboys AND Cowboys beat Eagles"


# -- cross-stream wiring: the fair-pricing why dict, middles ---------------------------------------
JARGON = ("fair_source", "anchored-", "p_market", "p_model", "model_weight", "EV", "CLV", "Brier",
          "implied mean", "h2h", "hfa")


def test_why_panel_renders_fair_pricing_reasons_in_plain_english():
    why = {"fair_source": "anchored-pinnacle", "books": ["pinnacle"], "p_market": 0.54, "p_model": 0.58,
           "model_weight": 0.3, "method": "adjacent line", "tie_prob": 0.004}
    lines = d.why_lines(rec(why=why), NOW, kelly_fraction=0.25, cap_bet=0.02)
    text = "\n".join(lines[:-1])
    assert "the last price from Pinnacle, the sharpest sportsbook, moved by how much" in text
    assert "(Pinnacle)" in text and "adjusted from a line half a point away" in text
    assert "Sportsbooks say 54%; our team ratings say 58% and count for 30% of our number." in text
    assert "0.4% chance of a tie" in text
    assert not [j for j in JARGON if j in text], text
    assert text.isascii()
    assert lines[-1].startswith("[details] id 41") and "fair_source=anchored-pinnacle" in lines[-1]


def test_why_panel_market_only_and_median_source():
    why = {"fair_source": "median", "books": ["draftkings", "fanduel"], "p_market": 0.5, "p_model": None,
           "model_weight": 0.0, "method": "h2h", "tie_prob": 0.0}
    text = "\n".join(d.why_lines(rec(why=why), NOW)[:-1])
    assert "middle of the regular sportsbooks' prices" in text and "DraftKings, FanDuel" in text
    assert "our team ratings are not used for this one" in text and "tie" not in text


def test_why_panel_sizing_names_the_caps_and_prizepicks_cut():
    from gridiron_edge.config import Settings
    s = Settings()
    pp = rec("pp_entry", legs=[Leg("prizepicks", "x", "over", 0.0, 0.6, GID, player="Jalen Hurts",
                                    stat="pass_tds", line=1.5)], structure="5-flex", qty=None, stake_cents=500)
    text = "\n".join(d.why_lines(pp, NOW, settings=s))
    assert f"at most {s.cap_game:.0%} of bankroll on one game" in text
    assert "until you confirm the PrizePicks payouts" in text


def test_middle_expected_profit_and_why():
    legs = [kleg(line=2.5), kleg(side="no", line=6.5, ticker="KXNFLSPREAD-B", price=0.50)]
    mid = rec("middle", legs=legs, qty=20, p_win=0.18, stake_cents=0)
    cost, fee = d.cost_cents(mid), d.fee_cents(mid)
    assert d.expected_profit_cents(mid) == round(20 * 1.18 * 100) - cost - fee
    text = "\n".join(d.why_lines(mid, NOW))
    assert "Not risk-free" in text and "18% chance" in text
