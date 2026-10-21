from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from gridiron_edge.config import Settings
from gridiron_edge.pipeline import PPLine
from gridiron_edge.types import Game
from gridiron_edge.ui import prizepicks as ppv

NOW = datetime(2026, 10, 25, 15, 2, tzinfo=timezone.utc)  # 8:02 AM PT
NE_CHI = Game("nfl:20261025:NE@CHI", "nfl", 2026, 8, NOW + timedelta(hours=5), "CHI", "NE")
DAL_PHI = Game("nfl:20261025:DAL@PHI", "nfl", 2026, 8, NOW + timedelta(hours=2), "PHI", "DAL")
JARGON = ("EV", "CLV", "Brier", "fair", "rho", "copula", "Kelly")


def line(player, p, game=NE_CHI, side="over", anchored=True, stat="pass_yds", point=249.5, team="CHI",
         fresh=True):
    return PPLine(game.game_id, player, team, stat, point, side if p is not None else None, p, anchored, NOW,
                  fresh)


def view(lines, blocker=None, active=0, **kw):
    games = {g.game_id: g for g in (NE_CHI, DAL_PHI)}
    return ppv.PPView(lines, games, blocker, active, Settings(), {"nfl": True, "cfb": True}, **kw)


def test_line_text_plain_english():
    assert ppv.line_text(line("Caleb Williams", 0.56)) == \
        "Caleb Williams MORE than 249.5 passing yards - our chance 56.0% (sportsbooks)"
    assert ppv.line_text(line("D'Andre Swift", 0.58, side="under", anchored=False, stat="rush_yds", point=60.5)) == \
        "D'Andre Swift LESS than 60.5 rushing yards - our chance 58.0% (our model only - not texted)"
    assert ppv.line_text(line("X", 0.58, anchored=False), gated=False).endswith("(our model only)")
    assert ppv.line_text(line("Y", None)).endswith("- no estimate yet")
    assert ppv.line_text(line("Y", None, team=None)).endswith("- team unknown")


def test_board_groups_by_game_sorted_by_kickoff_and_chance():
    lines = [line("A", 0.52), line("B", 0.61), line("C", None), line("D", 0.57, game=DAL_PHI, team="PHI")]
    out = ppv.board_lines(view(lines, ("one_team", {})), NOW)
    assert out[0].startswith("Lines as of 8:02 AM PT - 4 lines for 2 games.")
    assert out[1].startswith("A 6-pick Flex Play needs about 54.2% per pick")
    assert out[2] == ("No entry yet: all the good picks are on one team; PrizePicks needs picks from at least "
                      "2 teams.")
    assert out[3] == "2 of 4 lines clear the needed chance."
    i = out.index("Cowboys at Eagles - kickoff 10:02 AM PT - 1 of 1 line clears the needed chance")
    j = out.index("Patriots at Bears - kickoff 1:02 PM PT - 1 of 3 lines clear the needed chance")
    assert i < j
    assert [ln.split()[0] for ln in out[j + 1:j + 4]] == ["B", "A", "C"]
    assert not any(w in " ".join(out) for w in JARGON)


def test_home_status_line():
    lines = [line(f"P{i}", 0.55) for i in range(43)]
    v = view(lines, ("one_team", {}), next_load=NOW + timedelta(hours=1, minutes=23))
    assert ppv.status_line(v, NOW) == (
        "PrizePicks: 43 lines loaded for 1 game (Patriots at Bears). No entry yet: all the good picks are on "
        "one team; PrizePicks needs picks from at least 2 teams. More lines load about 9:25 AM PT, 3 hours before "
        "kickoff. See tab 5.")
    ready = ppv.status_line(view(lines, None, active=1), NOW)
    assert "1 entry ready in PLACE NOW." in ready and "No entry" not in ready
    assert ppv.status_line(view([]), NOW) == \
        "PrizePicks: no lines loaded yet. More lines load about 3 hours before each kickoff. See tab 5."
    off = replace(view([]), settings=replace(Settings(), venues=("kalshi",)))
    assert "turned off" in ppv.status_line(off, NOW)


@pytest.mark.parametrize("code,info", [
    ("no_lines", {}), ("stale", {}), ("no_structures", {}), ("few_lines", {"priced": 1, "need": 3}),
    ("few_good", {"good": 2, "need": 3}), ("few_good", {"good": 0, "need": 3}), ("one_team", {}),
    ("few_games", {"cap": 2}), ("model_only", {}), ("no_value", {}), ("paused", {}), ("watch_only", {}),
    ("daily_cap", {}),
])
def test_every_reason_is_plain_english(code, info):
    text = ppv.reason_text(code, info)
    assert text.endswith(".") and text != "No entry right now." and not any(w in text for w in JARGON)


def test_reason_examples():
    assert ppv.reason_text("few_good", {"good": 2, "need": 3}) == \
        "Not enough good picks - only 2 clear the needed chance and an entry needs at least 3."
    assert "watch-only" in ppv.reason_text("model_only", {})
    assert ppv.reason_text("few_lines", {"priced": 0, "need": 3, "no_team": 22}) == (
        "Not enough lines - we can't use any picks yet (22 skipped: players whose team we don't know yet) "
        "and the smallest entry needs 3.")
    assert "we can use only 1 pick " in ppv.reason_text("few_lines", {"priced": 1, "need": 3})
    assert ppv.reason_text("stale", {"seconds": 5400}) == "The loaded lines are over 90 minutes old, too old to use."
    assert ppv.reason_text("stale", {"seconds": 7200}) == "The loaded lines are over 2 hours old, too old to use."
    assert ppv.reason_text("stale", {"seconds": 3600}) == "The loaded lines are over 1 hour old, too old to use."
    assert "drift reset" in ppv.reason_text("paused", {})


def test_needed_text_follows_payouts():
    assert ppv.needed_text(Settings()).startswith("A 6-pick Flex Play needs about 54.2% per pick")
    only5 = replace(Settings(), pp_structures=("5-flex",))
    assert ppv.needed_text(only5).startswith("A 5-pick Flex Play needs about 54.3% per pick")


def test_best_picks_and_needed_chance():
    lines = [line("A", 0.52), line("B", 0.61), line("E", 0.59, anchored=False), line("C", None),
             line("D", 0.57, game=DAL_PHI, team="PHI"), line("F", 0.70, fresh=False)]
    v = view(lines)
    s = v.settings
    be = ppv.best_structure(s)[1]
    assert ppv.needed_chance(line("x", 0.5), s) == pytest.approx(be + s.pp_margin_market)
    assert ppv.needed_chance(line("x", 0.5, anchored=False), s) == pytest.approx(be + s.pp_margin_model)
    assert [ln.player for ln in ppv.best_picks(v)] == ["B", "E", "D"]  # most room above the needed chance
    short = view([line("B", 0.61), line("G", be + s.pp_margin_market - 0.002)])  # just under what it needs
    assert [ln.player for ln in ppv.best_picks(short)] == ["B"]
    out = ppv.board_lines(v, NOW)
    k = out.index("Best picks now:")
    assert out[k + 1] == "  1. B MORE than 249.5 passing yards: our chance 61.0%, needs 56.2% (Patriots at Bears)"
    assert out[k + 2].endswith("needs 57.7% (Patriots at Bears) - our model only, not texted")
    assert len([x for x in out[k + 1:k + 4] if x.startswith("  ")]) == 3
    assert "3 of 6 lines clear the needed chance." in out  # stale F and unpriced C never count
    assert ppv.clear_text(1, 1) == "1 of 1 line clears the needed chance"
    assert ppv.best_picks(view([line("C", None)])) == []
    assert "Best picks now:" not in ppv.board_lines(view([line("C", None)]), NOW)
