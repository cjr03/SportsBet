import pytest

from gridiron_edge.strategy.arb import Arb, cross_venue, filter_arbs, kalshi_ladders, kalshi_ml_pairs


def test_cross_venue_example():
    roi, st = cross_venue({"a": (2.10, 0), "b": (0, 2.05)})
    assert roi == pytest.approx(0.0373, abs=1e-4) and st["a"] == pytest.approx(0.4940, abs=1e-3)


def test_cross_venue_none_when_no_arb():
    assert cross_venue({"a": (1.90, 0), "b": (0, 1.90)}) is None
    assert cross_venue({"a": (2.10, 0)}) is None


def test_ml_pair_found_with_fees(mk):
    arbs = kalshi_ml_pairs([mk("E1-SEA", "E1", "ml", "SEA", yes_ask=0.45, yes_ask_size=100),
                            mk("E1-CHI", "E1", "ml", "CHI", yes_ask=0.45, yes_ask_size=50)], lambda m: "cfb")
    assert len(arbs) == 1 and arbs[0].qty == 50 and not arbs[0].tie_risk and 0 < arbs[0].roi < 0.1
    # NFL with no tie clause: a tie pays nothing, so it is not an arb at all
    assert kalshi_ml_pairs([mk("E1-SEA", "E1", "ml", "SEA", yes_ask=0.45, yes_ask_size=100),
                            mk("E1-CHI", "E1", "ml", "CHI", yes_ask=0.45, yes_ask_size=50)], lambda m: "nfl") == []


def test_ml_pair_none_when_fees_eat_it(mk):
    assert kalshi_ml_pairs([mk("E1-A", "E1", "ml", "A", yes_ask=0.50),
                            mk("E1-B", "E1", "ml", "B", yes_ask=0.49)], lambda m: "nfl") == []


def test_ml_pair_requires_exactly_two_active(mk):
    ms = [mk("E1-A", "E1", "ml", "A", yes_ask=0.40), mk("E1-B", "E1", "ml", "B", yes_ask=0.40),
          mk("E1-C", "E1", "ml", "C", yes_ask=0.40)]
    assert kalshi_ml_pairs(ms, lambda m: "cfb") == []
    ms = [mk("E1-A", "E1", "ml", "A", yes_ask=0.40), mk("E1-B", "E1", "ml", "B", yes_ask=0.40, status="closed")]
    assert kalshi_ml_pairs(ms, lambda m: "cfb") == []


def test_ml_pair_cfb_no_tie_risk(mk):
    arbs = kalshi_ml_pairs([mk("E1-A", "E1", "ml", "A", yes_ask=0.40),
                            mk("E1-B", "E1", "ml", "B", yes_ask=0.40)], lambda m: "cfb")
    assert len(arbs) == 1 and not arbs[0].tie_risk


def test_ladder_violation(mk):
    lo = mk("E2-PHI7", "E2", "spread", "PHI", line=6.5, yes_bid=0.30, yes_ask=0.31)
    hi = mk("E2-PHI8", "E2", "spread", "PHI", line=7.5, yes_bid=0.36, yes_ask=0.37, no_ask=0.64)
    a = kalshi_ladders([lo, hi])
    assert len(a) == 1 and a[0].roi > 0 and a[0].kind == "kalshi_ladder"


def test_ladder_consistent_none(mk):
    lo = mk("E3-PHI7", "E3", "spread", "PHI", line=6.5, yes_bid=0.16, yes_ask=0.18)
    hi = mk("E3-PHI8", "E3", "spread", "PHI", line=7.5, yes_bid=0.13, yes_ask=0.14)
    assert kalshi_ladders([lo, hi]) == []


def test_filter_drops_absurd_roi():
    assert filter_arbs([Arb("kalshi_ml_pair", "g", [], 10, 5, 10, 1.0, False)], 0.08) == []
    assert filter_arbs([Arb("kalshi_ml_pair", "g", [], 0, 5, 10, 0.05, False)], 0.08) == []
    keep = Arb("kalshi_ml_pair", "g", [], 10, 5, 10, 0.05, False)
    assert filter_arbs([keep], 0.08) == [keep]


# -- quant upgrade: tie rule, NO+NO pairs, full ladder scan, cross-team pairs, middles ----------
from dataclasses import replace  # noqa: E402

from gridiron_edge.strategy.arb import (  # noqa: E402
    has_tie_split,
    kalshi_cross_spreads,
    kalshi_middles,
    kalshi_ml_no_pairs,
)

TIE_RULES = ("If Seattle wins the game, the market resolves to Yes. If the game ends in a tie, "
             "the market resolves to a 50/50 split.")


def test_tie_split_rule_detected():
    assert has_tie_split(TIE_RULES)
    assert has_tie_split("In the event of a tie, each contract settles at $0.50.")
    assert not has_tie_split("If Seattle wins the game, the market resolves to Yes.")
    assert not has_tie_split("")


def test_ml_pair_no_tie_risk_when_rules_split_ties(mk):
    ms = [replace(mk("E1-SEA", "E1", "ml", "SEA", yes_ask=0.45), rules_primary=TIE_RULES),
          replace(mk("E1-CHI", "E1", "ml", "CHI", yes_ask=0.45), rules_primary=TIE_RULES)]
    (a,) = kalshi_ml_pairs(ms, lambda m: "nfl")
    assert not a.tie_risk and a.legs[0].fee_multiplier == 1.0


def test_ml_no_pair(mk):
    ms = [mk("E1-SEA", "E1", "ml", "SEA", yes_ask=0.60, yes_bid=0.55, no_ask=0.44),
          mk("E1-CHI", "E1", "ml", "CHI", yes_ask=0.60, yes_bid=0.55, no_ask=0.44)]
    assert kalshi_ml_pairs(ms, lambda m: "nfl") == []
    (a,) = kalshi_ml_no_pairs(ms)
    assert a.kind == "kalshi_ml_no_pair" and not a.tie_risk and {leg.side for leg in a.legs} == {"no"}
    assert 0 < a.roi < 0.1


def test_ladder_non_adjacent_strikes(mk):
    ladder = [mk("E2-PHI3", "E2", "spread", "PHI", line=2.5, yes_ask=0.40, no_ask=0.65),
              mk("E2-PHI4", "E2", "spread", "PHI", line=3.5, yes_ask=0.60, no_ask=0.65),
              mk("E2-PHI5", "E2", "spread", "PHI", line=4.5, yes_ask=0.70, no_ask=0.45)]
    arbs = kalshi_ladders(ladder)
    assert [(a.legs[0].ticker, a.legs[1].ticker) for a in arbs] == [("E2-PHI3", "E2-PHI5")]


def test_cross_team_no_no_spreads(mk):
    ms = [mk("E2-PHI3", "E2", "spread", "PHI", line=3.5, yes_ask=0.62, no_ask=0.40, game_id="g1"),
          mk("E2-DAL2", "E2", "spread", "DAL", line=2.5, yes_ask=0.58, no_ask=0.45, game_id="g1"),
          mk("E2-DAL9", "E2", "spread", "DAL", line=9.5, yes_ask=0.10, no_ask=0.91, game_id="g1")]
    (a,) = kalshi_cross_spreads(ms)
    assert a.kind == "kalshi_cross_spread" and {leg.ticker for leg in a.legs} == {"E2-PHI3", "E2-DAL2"}
    assert all(leg.side == "no" for leg in a.legs) and a.roi > 0


def test_middles_cost_just_over_a_dollar(mk):
    ladder = [mk("E2-PHI3", "E2", "spread", "PHI", line=2.5, yes_ask=0.52, no_ask=0.49),
              mk("E2-PHI4", "E2", "spread", "PHI", line=3.5, yes_ask=0.51, no_ask=0.50)]
    assert kalshi_ladders(ladder) == []
    mids = kalshi_middles(ladder, 0.10)
    assert len(mids) == 1 and mids[0].kind == "ladder_middle" and mids[0].cost > mids[0].qty
    assert kalshi_middles(ladder, 0.0) == []


# -- plain-English contract text (texts and dashboard) ---------------------------------------

def test_contract_text_describes_the_yes_outcome():
    from gridiron_edge.strategy.arb import contract_text
    assert contract_text("spread", "PHI", 3.5, "DAL", "PHI") == "Eagles win by 4 or more points"
    assert contract_text("spread", "PHI", 3.0, "DAL", "PHI") == "Eagles win by 4 or more points"
    assert contract_text("ml", "BUF", None, "MIA", "BUF") == "Bills win"
    assert contract_text("total", None, 44.5, "DAL", "PHI") == "Cowboys vs Eagles total over 44.5 points"
    assert contract_text("total", None, 44.0, "DAL", "PHI") == "Cowboys vs Eagles total 45 or more points"
    assert contract_text("spread", "Alabama", 6.5, "Auburn", "Alabama") == "Alabama win by 7 or more points"


def test_middle_window_text():
    from gridiron_edge.strategy.arb import middle_window
    from gridiron_edge.types import Leg
    lo = Leg("kalshi", "x", "yes", 0.5, 0.5, "g", ticker="A", line=2.5, team="PHI")
    hi = Leg("kalshi", "x", "no", 0.5, 0.5, "g", ticker="B", line=6.5, team="PHI")
    assert middle_window([lo, hi]) == "the Eagles win by 3 to 6 points"
    tl = Leg("kalshi", "x", "yes", 0.5, 0.5, "g", ticker="KXNFLTOTAL-26OCT26DALPHI-42", line=41.5)
    th = Leg("kalshi", "x", "no", 0.5, 0.5, "g", ticker="KXNFLTOTAL-26OCT26DALPHI-45", line=44.5)
    assert middle_window([th, tl]) == "the total lands between 42 and 44 points"
    a = Leg("kalshi", "x", "no", 0.5, 0.5, "g", ticker="E", line=3.5, team="PHI")
    b = Leg("kalshi", "x", "no", 0.5, 0.5, "g", ticker="F", line=6.5, team="DAL")
    assert middle_window([a, b]) == "the game ends anywhere from a Cowboys win by 6 to an Eagles win by 3, ties included"
    neg = lambda lo_line, hi_line: middle_window([replace(lo, line=lo_line), replace(hi, line=hi_line)])
    assert neg(-2.5, 2.5) == "the Eagles finish anywhere from losing by 2 to winning by 2 points"
    assert neg(-2.5, 0.5) == "the Eagles finish anywhere from losing by 2 to a tie"
    assert neg(-5.5, -2.5) == "the Eagles lose by 3 to 5 points"
    assert neg(-0.5, 3.5) == "the Eagles tie or win by up to 3 points"


def test_contract_text_handles_zero_and_negative_spreads():
    from gridiron_edge.strategy.arb import contract_text
    assert contract_text("spread", "PHI", -0.5, "DAL", "PHI") == "Eagles win or tie"
    assert contract_text("spread", "PHI", -3.5, "DAL", "PHI") == "Eagles win, tie, or lose by 3 or fewer points"


def test_middle_and_contract_text_use_cfb_school_names():
    from gridiron_edge.describe import Names
    from gridiron_edge.strategy.arb import contract_text, middle_window
    from gridiron_edge.types import Leg
    names = Names({("cfb", "ALA"): "Alabama", ("cfb", "AUB"): "Auburn"})
    assert contract_text("spread", "ALA", 6.5, "AUB", "ALA", "cfb", names) == "Alabama win by 7 or more points"
    gid = "cfb:20261128:AUB@ALA"
    lo = Leg("kalshi", "x", "yes", 0.5, 0.5, gid, ticker="A", line=2.5, team="ALA")
    hi = Leg("kalshi", "x", "no", 0.5, 0.5, gid, ticker="B", line=6.5, team="ALA")
    assert middle_window([lo, hi], names) == "Alabama win by 3 to 6 points"
    a = Leg("kalshi", "x", "no", 0.5, 0.5, gid, ticker="E", line=3.5, team="ALA")
    b = Leg("kalshi", "x", "no", 0.5, 0.5, gid, ticker="F", line=1.5, team="AUB")
    assert "ALA" not in middle_window([a, b], names) and "from an Auburn win by 1 to an Alabama win by 3" in middle_window([a, b], names)


# -- payoff verifier: every pair must pay >= $1 in every final score --------------------------------
from gridiron_edge.strategy.arb import contract_text, payoff_range  # noqa: E402

CFB_EV = "KXNCAAFSPREAD-26OCT24AKRKENT"


def _no_bad_middles(mids):
    """No YES/NO pair across two teams; only NO/NO cross middles (always >= $1) may remain."""
    return all(a.kind == "cross_middle" and a.payout_min >= a.qty for a in mids)


def _rec146(mk, team_akr="AKR", team_kent="KENT"):
    """Placed rec 146: YES Akron wins by >5.5 @0.28 + NO Kent State wins by >6.5 @0.62."""
    akr = replace(mk(f"{CFB_EV}-AKR6", CFB_EV, "spread", team_akr, line=5.5, yes_ask=0.28, yes_bid=0.27),
                  series="KXNCAAFSPREAD")
    kent = replace(mk(f"{CFB_EV}-KENT7", CFB_EV, "spread", team_kent, line=6.5, yes_ask=0.39, yes_bid=0.38,
                      no_ask=0.62), series="KXNCAAFSPREAD")
    return akr, kent


def test_rec146_mixed_team_ladder_rejected(mk):
    akr, kent = _rec146(mk)
    assert payoff_range([(akr, "yes"), (kent, "no")], ties=False) == (0.0, 2.0)
    assert kalshi_ladders([akr, kent]) == []
    assert _no_bad_middles(kalshi_middles([akr, kent], 0.5))


def test_rec146_with_unknown_teams_rejected(mk):
    akr, kent = _rec146(mk, None, None)
    assert payoff_range([(akr, "yes"), (kent, "no")], ties=False) is None
    assert kalshi_ladders([akr, kent]) == []
    assert kalshi_middles([akr, kent], 0.5) == []


def test_rec174_equal_lines_other_team_rejected(mk):
    ev = "KXNFLSPREAD-26OCT25TENCLE"
    ten = mk(f"{ev}-TEN5", ev, "spread", "TEN", line=4.5, yes_ask=0.30)
    cle = mk(f"{ev}-CLE5", ev, "spread", "CLE", line=4.5, yes_ask=0.40, yes_bid=0.39, no_ask=0.61)
    assert payoff_range([(ten, "yes"), (cle, "no")], ties=True) == (0.0, 2.0)
    assert kalshi_ladders([ten, cle]) == [] and _no_bad_middles(kalshi_middles([ten, cle], 0.5))


def test_rec233_spreads_never_read_as_a_total(mk):
    ev = "KXNCAAFSPREAD-26OCT25LIBKENN"
    lo = mk(f"{ev}-LIB14", ev, "spread", "LIB", line=13.5, yes_ask=0.30)
    hi = mk(f"{ev}-KENN28", ev, "spread", "KENN", line=27.5, yes_ask=0.10, yes_bid=0.09, no_ask=0.62)
    assert payoff_range([(lo, "yes"), (hi, "no")], ties=False)[0] == 0.0
    assert kalshi_ladders([lo, hi]) == []
    assert "total" not in contract_text("spread", None, 13.5, "LIB", "KENN")


def test_mixed_team_spreads_never_ladder(mk):
    ms = [mk("E2-PHI7", "E2", "spread", "PHI", line=6.5, yes_ask=0.20),
          mk("E2-DAL8", "E2", "spread", "DAL", line=7.5, yes_ask=0.40, yes_bid=0.39, no_ask=0.50)]
    assert kalshi_ladders(ms) == [] and _no_bad_middles(kalshi_middles(ms, 0.5))


def test_ladders_are_per_team(mk):
    ms = [mk("E2-PHI3", "E2", "spread", "PHI", line=2.5, yes_ask=0.40, no_ask=0.65),
          mk("E2-PHI5", "E2", "spread", "PHI", line=4.5, yes_ask=0.70, no_ask=0.45),
          mk("E2-DAL3", "E2", "spread", "DAL", line=2.5, yes_ask=0.40, no_ask=0.65),
          mk("E2-DAL5", "E2", "spread", "DAL", line=4.5, yes_ask=0.70, no_ask=0.45)]
    arbs = kalshi_ladders(ms)
    assert sorted((a.legs[0].ticker, a.legs[1].ticker) for a in arbs) == [("E2-DAL3", "E2-DAL5"),
                                                                          ("E2-PHI3", "E2-PHI5")]
    assert all(a.payout_min == a.qty and a.payout_max == 2 * a.qty for a in arbs)


def test_payoff_range_valid_structures(mk):
    lo = mk("E2-PHI3", "E2", "spread", "PHI", line=2.5)
    hi = mk("E2-PHI5", "E2", "spread", "PHI", line=4.5)
    assert payoff_range([(lo, "yes"), (hi, "no")], ties=True) == (1.0, 2.0)
    tlo = mk("KXNFLTOTAL-X-42", "E3", "total", None, line=41.5)
    thi = mk("KXNFLTOTAL-X-45", "E3", "total", None, line=44.5)
    assert payoff_range([(tlo, "yes"), (thi, "no")], ties=True) == (1.0, 2.0)
    a = mk("E2-PHI4", "E2", "spread", "PHI", line=3.5)
    b = mk("E2-DAL3", "E2", "spread", "DAL", line=2.5)
    assert payoff_range([(a, "no"), (b, "no")], ties=True) == (1.0, 2.0)


def test_payoff_range_moneylines(mk):
    sea, chi = mk("E1-SEA", "E1", "ml", "SEA"), mk("E1-CHI", "E1", "ml", "CHI")
    split = [replace(m, rules_secondary=TIE_RULES) for m in (sea, chi)]
    assert payoff_range([(split[0], "yes"), (split[1], "yes")], ties=True) == (1.0, 1.0)
    assert payoff_range([(sea, "yes"), (chi, "yes")], ties=True) == (0.0, 1.0)
    assert payoff_range([(sea, "no"), (chi, "no")], ties=True) == (1.0, 2.0)
    assert payoff_range([(sea, "yes"), (chi, "yes")], ties=False) == (1.0, 1.0)  # CFB: no ties
    assert payoff_range([(sea, "yes"), (mk("E1-X", "E1", "ml", None), "yes")], ties=True) is None


def test_nfl_ml_pair_without_tie_clause_rejected(mk):
    ms = [mk("E1-SEA", "E1", "ml", "SEA", yes_ask=0.45), mk("E1-CHI", "E1", "ml", "CHI", yes_ask=0.45)]
    assert kalshi_ml_pairs(ms, lambda m: "nfl") == []
    assert len(kalshi_ml_pairs(ms, lambda m: "cfb")) == 1


def test_tie_clause_in_rules_secondary(mk):
    from gridiron_edge.strategy.arb import ml_tie_risk
    sec = ("If the game ends in a tie, the market will resolve to $0.50 for each team.")
    m = replace(mk("E1-SEA", "E1", "ml", "SEA"), rules_secondary=sec)
    assert not ml_tie_risk(m, "nfl")
    ms = [m, replace(mk("E1-CHI", "E1", "ml", "CHI", yes_ask=0.45), rules_secondary=sec)]
    ms[0] = replace(ms[0], yes_ask=0.45)
    (a,) = kalshi_ml_pairs(ms, lambda m: "nfl")
    assert not a.tie_risk


def test_ml_groups_need_two_different_teams(mk):
    ms = [mk("E1-SEA", "E1", "ml", "SEA", yes_ask=0.40), mk("E1-SEA2", "E1", "ml", "SEA", yes_ask=0.40)]
    assert kalshi_ml_pairs(ms, lambda m: "cfb") == [] and kalshi_ml_no_pairs(ms) == []


def test_contract_text_spread_without_team_is_blank():
    assert contract_text("spread", None, 13.5, "LIB", "KENN") == ""
