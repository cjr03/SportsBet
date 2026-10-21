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


# -- YOUR ENTRY: picking lines yourself --------------------------------------------------------------
def sized(lines, **kw):
    """A view with bankroll headroom, like Services.pp_view builds: $1,000 bankroll, nothing open."""
    from gridiron_edge.pipeline import Budget, Snapshot
    games = {g.game_id: g for g in (NE_CHI, DAL_PHI)}
    snap = Snapshot(now=NOW, games=games, quotes=[], props=[], kalshi=[], predictions={}, gates={},
                    bankroll_cents=100_000, open_exposure_cents=0, pp_staked_today_cents=0, margin_models={})
    return view(lines, budget=Budget.from_snap(snap, Settings()), daily_left=10_000, **kw)


def entry(*specs):
    return [line(p, chance, game=g, team=t, stat=st) for p, chance, g, t, st in specs]


THREE = (("Caleb Williams", 0.62, NE_CHI, "CHI", "pass_yds"), ("Drake Maye", 0.63, NE_CHI, "NE", "pass_tds"),
         ("Jalen Hurts", 0.64, DAL_PHI, "PHI", "rush_yds"))


def test_evaluate_entry_three_picks_power_play_with_stake_and_outcomes():
    v = sized([])
    ev = ppv.evaluate_entry(entry(*THREE), v)
    assert ev.ready and ev.structure == (3, "power") and not ev.problems and not ev.warnings
    assert ev.linked  # two picks share Patriots at Bears
    assert ev.ev > 0 and ev.stake_cents >= 100 and ev.expected_profit_cents == round(ev.ev * ev.stake_cents)
    assert ev.stake_cents <= 0.02 * 100_000  # the one-bet limit
    assert sum(ev.dist) == pytest.approx(1.0) and ev.p_paid == pytest.approx(ev.dist[3])
    outs = ppv.outcome_texts(ev)
    assert outs[0].startswith(f"All 3 hit: pays back ${ev.stake_cents * 6 / 100:,.2f} - chance ")
    assert outs[-1].startswith("Fewer hit: you lose what you put in - chance ")
    text = ppv.entry_lines(ev, v)
    assert text[0] == "1. Caleb Williams MORE than 249.5 passing yards - our chance 62.0% (Patriots at Bears)"
    assert "Best entry type for these 3 picks: 3-pick Power Play (every pick must hit)." in text
    assert any(t.startswith("Suggested amount: $") and "Expected profit: about $" in t for t in text)
    assert text[-1].startswith("Press c after you place it on PrizePicks")


def test_evaluate_entry_matches_the_builder_on_the_same_picks():
    """Same correlation scoring as build_best_entry: our EV equals the builder's for its own legs."""
    from gridiron_edge.strategy.prizepicks import build_best_entry, payout_table
    picks = entry(*THREE)
    ev = ppv.evaluate_entry(picks, sized([]))
    s = Settings()
    built = build_best_entry(ev.legs, replace(s, pp_structures=("3-power",)), [(3, "power")],
                             payouts=payout_table(s))
    assert built is not None and built[1] == ev.structure and built[2] == pytest.approx(ev.ev)


def test_entry_rules_give_plain_messages_not_errors():
    v = sized([])
    one = ppv.evaluate_entry(entry(THREE[0]), v)
    assert not one.ready and one.problems == ["Pick at least 2 lines; a PrizePicks entry needs 2 to 6 picks."]
    same_team = ppv.evaluate_entry(entry(("A", 0.62, NE_CHI, "CHI", "pass_yds"),
                                         ("B", 0.62, DAL_PHI, "CHI", "rush_yds")), v)
    assert "All your picks are on one team (Bears); PrizePicks needs picks from at least 2 different teams." \
        in same_team.problems
    two = ppv.evaluate_entry(entry(THREE[0], THREE[2]), v)  # only 3- to 6-pick entries are allowed
    assert two.problems == ["None of the entry types we use has 2 picks (we use entries of 3, 4, 5, 6 picks)."]
    crowded = ppv.evaluate_entry(entry(("A", 0.62, NE_CHI, "CHI", "pass_yds"), ("B", 0.62, NE_CHI, "NE", "pass_tds"),
                                       ("C", 0.62, NE_CHI, "CHI", "rush_yds")), v)
    assert "3 picks from Patriots at Bears; we allow at most 2 from one game." in crowded.problems
    twice = ppv.evaluate_entry(entry(("A", 0.62, NE_CHI, "CHI", "pass_yds"), ("A", 0.62, NE_CHI, "CHI", "rush_yds"),
                                     ("C", 0.62, DAL_PHI, "PHI", "rush_yds")), v)
    assert "A is in your entry twice; PrizePicks allows one pick per player." in twice.problems
    for ev in (one, same_team, two, crowded, twice):
        assert any(t.startswith("Can't record yet: ") for t in ppv.entry_lines(ev, v))
        assert ppv.entry_lines(ev, v)[-1].startswith("Fix the above to record it")


def test_entry_warnings_below_needed_and_model_only():
    v = sized([])
    picks = entry(*THREE)
    picks[0] = replace(picks[0], p=0.55)
    picks[1] = replace(picks[1], anchored=False)
    ev = ppv.evaluate_entry(picks, v)
    assert ev.ready  # warnings never block
    assert "Caleb Williams is below the 57.0% a 3-pick Power Play needs (our chance 55.0%)." in ev.warnings
    assert "Drake Maye's chance is from our model only, not backed by sportsbooks." in ev.warnings
    held = ppv.evaluate_entry(entry(*THREE), sized([], watch_only={"nfl": True}))
    assert any("watch-only or paused" in w for w in held.warnings)


def test_entry_with_no_profit_suggests_nothing():
    low = [("A", 0.52, NE_CHI, "CHI", "pass_yds"), ("B", 0.52, NE_CHI, "NE", "pass_tds"),
           ("C", 0.52, DAL_PHI, "PHI", "rush_yds")]
    v = sized([])
    ev = ppv.evaluate_entry(entry(*low), v)
    assert ev.ready and ev.ev < 0 and ev.stake_cents == 0
    text = " ".join(ppv.entry_lines(ev, v))
    assert "Suggested amount: none. We suggest not playing it" in text and "loses about" in text
    capped = ppv.evaluate_entry(entry(*THREE), replace(sized([]), daily_left=0))
    assert capped.stake_cents == 0 and capped.stake_note.startswith("Today's PrizePicks limit is used up")


def test_add_problem_and_reconcile():
    v = view([])
    picked = entry(*THREE[:2])
    assert ppv.add_problem(picked, line("Caleb Williams", 0.6, stat="rush_yds"), v) == \
        "Caleb Williams is already in your entry; PrizePicks allows one pick per player."
    assert ppv.add_problem(picked, line("New", 0.6), v) == \
        "Your entry already has 2 picks from Patriots at Bears; we allow at most 2 from one game."
    assert "no chance worked out" in ppv.add_problem([], line("X", None), v)
    assert "don't know X's team" in ppv.add_problem([], line("X", 0.6, team=None), v)
    full = [line(f"P{i}", 0.6, game=DAL_PHI if i % 2 else NE_CHI) for i in range(6)]
    assert ppv.add_problem(full, line("Q", 0.6), view([])).startswith("An entry holds at most 6 picks")
    assert ppv.add_problem([], line("Y", 0.6), v) is None
    lines = [line("A", 0.6), line("B", None)]
    keys = [("A", "pass_yds", 249.5, "over"), ("B", "pass_yds", 249.5, "over"), ("A", "pass_yds", 249.5, "under")]
    kept, dropped = ppv.reconcile(keys, view(lines))
    assert kept == keys[:1] and dropped == keys[1:]
    assert ppv.dropped_text(keys[2]).startswith("Removed A LESS than 249.5 passing yards from your PrizePicks entry")


def test_pick_rows_mark_chosen_and_headings_are_not_pickable():
    lines = [line("A", 0.52), line("B", 0.61), line("C", None), line("D", 0.59, game=DAL_PHI, team="PHI")]
    v = view(lines)
    chosen = {ppv.pick_key(lines[1])}
    rows = ppv.all_rows(v, chosen, NOW)
    assert rows[0].heading and rows[0].key is None and rows[0].text.startswith("Cowboys at Eagles - kickoff")
    assert rows[1].text == "[ ] D MORE than 249.5 passing yards - our chance 59.0%, needs 56.2% (sportsbooks)"
    b = next(r for r in rows if r.key == ppv.pick_key(lines[1]))
    assert b.chosen and b.text.startswith("[x] B MORE")
    c = next(r for r in rows if "C 249.5" in r.text)
    assert c.key is None and not c.heading and c.text.endswith("no estimate yet")
    best = ppv.best_rows(v, chosen)
    assert [r.text[:7] for r in best] == ["[x] 1. ", "[ ] 2. "]
    assert best[0].text.endswith("(sportsbooks) - Patriots at Bears")


def test_pick_why_lines():
    from gridiron_edge.types import PropQuote
    ln = line("Caleb Williams", 0.6, point=249.5)
    quotes = [PropQuote("odds_api", "draftkings", NE_CHI.game_id, "Caleb Williams", "pass_yds", side, 244.5, dec,
                        NOW, fetched_at=NOW) for side, dec in (("over", 1.80), ("under", 2.02))]
    v = view([ln], props=quotes)
    out = ppv.pick_why_lines(ln, v, NOW + timedelta(minutes=12))
    assert out[0] == "The pick: Caleb Williams MORE than 249.5 passing yards (Patriots at Bears, kickoff 1:02 PM PT)."
    assert "  DraftKings, line 244.5: MORE $1.80, LESS $2.02" in out
    assert any("moved from their line of 244.5 to PrizePicks' 249.5" in t for t in out)
    assert any(t.startswith("Our chance: 60.0%. Needed chance: 56.2% (break-even 54.2%") for t in out)
    assert out[-2] == "PrizePicks line pulled at 8:02 AM PT (12 min ago)."
    assert out[-1].startswith("[details] ")
    model = ppv.pick_why_lines(line("X", 0.6, anchored=False), view([]), NOW)
    assert "No sportsbook has a price for this player and stat." in model
    assert any("our own model's projection" in t for t in model)


def test_below_needed_warning_never_shows_two_equal_numbers():
    picks = entry(*THREE)
    picks[0] = replace(picks[0], p=0.5699)  # 3-pick Power Play needs 57.0% for a sportsbook-backed pick
    ev = ppv.evaluate_entry(picks, sized([]))
    assert "Caleb Williams is just under the 57.0% a 3-pick Power Play needs." in ev.warnings


def test_lines_with_unknown_team_show_but_cannot_be_picked():
    lines = [line("A", 0.61), line("NoTeam", 0.66, team=None), line("D", 0.59, game=DAL_PHI, team="PHI")]
    v = view(lines)
    assert ppv.pick_key(lines[1]) not in ppv.pickable(v)
    row = next(r for r in ppv.all_rows(v, set(), NOW) if "NoTeam" in r.text)
    assert row.key is None and not row.heading and row.text.endswith("team unknown, can't be picked yet")
    assert all(r.key is not None for r in ppv.best_rows(v, set()))
    assert "NoTeam" not in " ".join(r.text for r in ppv.best_rows(v, set()))


def test_reconcile_keeps_picks_while_no_lines_are_priced():
    """A short gap (startup, or a refresh before props load) never empties YOUR ENTRY."""
    keys = [("A", "pass_yds", 249.5, "over")]
    assert ppv.reconcile(keys, view([])) == (keys, [])
    assert ppv.reconcile(keys, view([line("A", None)])) == (keys, [])
    assert ppv.reconcile(keys, view([line("B", 0.6)])) == ([], keys)  # lines are back: A is really gone
    text = ppv.entry_lines(ppv.evaluate_entry([], sized([])), sized([]), waiting=2)
    assert text[0] == ("Your 2 picks are kept, but their lines aren't loaded right now; the entry is worked "
                       "out again when they come back.")


def test_entry_outcomes_each_on_their_own_line_and_short_key_line():
    v = sized([])
    ev = ppv.evaluate_entry(entry(*THREE), v)
    text = ppv.entry_lines(ev, v)
    assert not any(" | " in t for t in text)
    outs = ppv.outcome_texts(ev)
    assert all(o in text for o in outs)
    assert text[-1] == "Press c after you place it on PrizePicks to record it; x clears it. We never place bets for you."
    assert len(text[-1]) <= 96  # one row at 100 columns


def test_entry_says_which_entry_type_the_list_numbers_are_for():
    v = sized([])
    ev = ppv.evaluate_entry(entry(*THREE), v)  # a 3-pick Power Play; the lists use a 6-pick Flex Play
    text = ppv.entry_lines(ev, v)
    assert ("The needed chances in the lists are for a 6-pick Flex Play; this 3-pick Power Play needs "
            "57.0% per pick (58.5% for model-only picks).") in text


def test_pick_log_messages_are_sentences():
    ln = line("Caleb Williams", 0.6)
    assert ppv.added_text(ln, 1) == "Added Caleb Williams MORE than 249.5 passing yards to your PrizePicks entry (1 pick)."
    assert ppv.removed_text("Caleb Williams MORE than 249.5 passing yards") == \
        "Took Caleb Williams MORE than 249.5 passing yards out of your PrizePicks entry."
    assert ppv.same_picks_text(7) == "Same picks as PrizePicks idea #7 in PLACE NOW."
    assert ppv.opened_text(7) == "Opened PrizePicks idea #7 in YOUR ENTRY on tab 5."
