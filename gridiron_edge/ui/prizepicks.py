"""PrizePicks tab and Home status line: every loaded line in plain English, and why no entry. Pure."""
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from gridiron_edge import describe as d
from gridiron_edge.config import Settings
from gridiron_edge.matching import normalize_name
from gridiron_edge.pipeline import PPLine, pp_stake
from gridiron_edge.strategy.prizepicks import (
    Pick, Structure, allowed_structures, breakeven, payout_table, robust_kelly, worst_distribution,
)
from gridiron_edge.types import Game, Leg, PropQuote

SEE_TAB = "See tab 5."


@dataclass
class PPView:
    """What the PrizePicks tab shows; built by Services.pp_view."""
    lines: list[PPLine]
    games: dict[str, Game]
    blocker: tuple[str, dict] | None  # pipeline.pp_blocker
    active: int  # PrizePicks ideas in PLACE NOW
    settings: Settings
    model_gated: dict[str, bool] = field(default_factory=dict)  # league -> model-only picks not texted
    next_load: datetime | None = None  # the next scheduled props pull
    props: list[PropQuote] = field(default_factory=list)  # sportsbook player lines (for "Why this pick?")
    budget: Any = None  # pipeline.Budget: cap headroom for sizing an entry you build yourself
    daily_left: int | None = None  # cents left under today's PrizePicks limit
    watch_only: dict[str, bool] = field(default_factory=dict)  # league -> PrizePicks ideas held back


def best_structure(s: Settings) -> tuple[Structure, float] | None:
    """The allowed entry type needing the lowest per-pick chance, and that chance."""
    table, best = payout_table(s), None
    for st in allowed_structures(s):
        if st not in table or table[st].get(st[0], 0) <= 1:
            continue
        try:
            be = breakeven(st, table)
        except ValueError:  # no break-even in range: this table never pays enough
            continue
        if best is None or be < best[1]:
            best = (st, be)
    return best


def needed_text(s: Settings) -> str:
    best = best_structure(s)
    if best is None:
        return "None of the allowed entry types can pay off with these payouts."
    (n, kind), be = best
    return (f"A {d.structure_text(f'{n}-{kind}')} needs about {be:.1%} per pick to break even "
            f"(we only pick it at {be + s.pp_margin_market:.1%} or more using sportsbook prices, "
            f"{be + s.pp_margin_model:.1%} using our model alone).")


def reason_text(code: str, info: dict) -> str:
    """Why no PrizePicks entry is texted, in one plain sentence."""
    match code:
        case "no_lines":
            return "No PrizePicks lines are loaded yet."
        case "stale":
            mins = round(info.get("seconds", 3 * 3600) / 60)
            age = (f"{mins // 60} hour{'s' * (mins != 60)}" if mins % 60 == 0 else f"{mins} minutes")
            return f"The loaded lines are over {age} old, too old to use."
        case "no_structures":
            return "None of the allowed entry types can pay off with these payouts."
        case "few_lines":
            n, unknown = info["priced"], info.get("no_team", 0)
            why = (f" ({unknown} skipped: players whose team we don't know yet)" if unknown else "")
            usable = "we can't use any picks yet" if n == 0 else f"we can use only {n} pick{'s' * (n != 1)}"
            return f"Not enough lines - {usable}{why} and the smallest entry needs {info['need']}."
        case "few_good":
            good = info["good"]
            return (f"Not enough good picks - {'none' if good == 0 else f'only {good}'} clear the needed "
                    f"chance and an entry needs at least {info['need']}.")
        case "one_team":
            return "All the good picks are on one team; PrizePicks needs picks from at least 2 teams."
        case "few_games":
            return (f"The good picks are bunched in too few games (at most {info['cap']} picks from one "
                    "game).")
        case "model_only":
            return "Only our own model likes these picks, and it is watch-only until it proves itself."
        case "no_value":
            return ("No mix of picks shows an expected profit once picks from the same game are treated "
                    "as linked.")
        case "paused":
            return ("PrizePicks ideas are paused because our recent picks fell behind the market "
                    "(type: drift reset all to turn them back on).")
        case "watch_only":
            return "PrizePicks entries are watch-only until our numbers prove themselves, so none is texted."
        case "daily_cap":
            return "Today's PrizePicks limit is used up (or the bankroll is empty)."
    return "No entry right now."


def _matchup(g: Game, names: d.Names) -> str:
    return f"{names.team(g.league, g.away)} at {names.team(g.league, g.home)}"


def _source(ln: PPLine, gated: bool) -> str:
    if ln.anchored:
        return "(sportsbooks)"
    return "(our model only - not texted)" if gated else "(our model only)"


def line_text(ln: PPLine, gated: bool = True) -> str:
    """e.g. 'Caleb Williams MORE than 249.5 passing yards - our chance 56.0% (sportsbooks)'."""
    line = f"{ln.line:g}"
    if ln.p is None or ln.side is None:
        why = "too old to use" if not ln.fresh else "no estimate yet" if ln.team else "team unknown"
        return f"{ln.player} {line} {d.stat_words(ln.stat)} - {why}"
    more = "MORE" if ln.side == "over" else "LESS"
    return f"{ln.player} {more} than {line} {d.stat_words(ln.stat)} - our chance {ln.p:.1%} {_source(ln, gated)}"


def _by_game(view: PPView) -> list[tuple[Game, list[PPLine]]]:
    groups: dict[str, list[PPLine]] = {}
    for ln in view.lines:
        if ln.game_id in view.games:
            groups.setdefault(ln.game_id, []).append(ln)
    out = []
    for gid, lines in groups.items():
        lines.sort(key=lambda ln: (ln.p is None, -(ln.p or 0.0), ln.player))
        out.append((view.games[gid], lines))
    return sorted(out, key=lambda x: (x[0].kickoff, x[0].game_id))


def _next_text(view: PPView, now: datetime) -> str:
    if view.next_load is not None and view.next_load > now:
        return f"More lines load about {d.when(view.next_load, now)} PT, 3 hours before kickoff."
    return "More lines load about 3 hours before each kickoff."


def _entry_text(view: PPView) -> str:
    if view.active:
        return f"{view.active} entr{'y' if view.active == 1 else 'ies'} ready in PLACE NOW."
    if view.blocker is None:
        return "An entry can be built; it shows up in PLACE NOW at the next check."
    return f"No entry yet: {_lower_first(reason_text(*view.blocker))}"


def _lower_first(text: str) -> str:
    return text if text.startswith("PrizePicks") else text[:1].lower() + text[1:]


def needed_chance(ln: PPLine, s: Settings, best: tuple[Structure, float] | None = None) -> float | None:
    """The chance a pick needs before we use it: break-even plus the margin for its source."""
    best = best if best is not None else best_structure(s)
    if best is None:
        return None
    return best[1] + (s.pp_margin_market if ln.anchored else s.pp_margin_model)


def _clears(ln: PPLine, s: Settings, best) -> bool:
    need = needed_chance(ln, s, best)
    return ln.p is not None and ln.fresh and need is not None and ln.p >= need


def clear_text(n_clear: int, n: int) -> str:
    """'4 of 18 lines clear the needed chance'."""
    return f"{n_clear} of {n} line{'s' * (n != 1)} clear{'s' * (n == 1)} the needed chance"


def best_picks(view: PPView, n: int = 3) -> list[PPLine]:
    """The n picks with the highest chance above what they need (lines that reach it only)."""
    best = best_structure(view.settings)
    usable = [ln for ln in view.lines if ln.game_id in view.games and _clears(ln, view.settings, best)]

    def room(ln: PPLine) -> float:
        need = needed_chance(ln, view.settings, best)
        return (ln.p or 0.0) - (need or 0.0)

    return sorted(usable, key=lambda ln: (-room(ln), ln.player))[:n]


def best_pick_text(ln: PPLine, view: PPView, names: d.Names = d.DEFAULT_NAMES, best=None) -> str:
    """'Patrick Mahomes MORE than 1.5 passing touchdowns: our chance 59.1%, needs 56.2% (Dolphins at Chiefs)'."""
    g = view.games[ln.game_id]
    need = needed_chance(ln, view.settings, best if best is not None else best_structure(view.settings))
    more = "MORE" if ln.side == "over" else "LESS"
    vs = "" if need is None else f", needs {need:.1%}"
    src = "" if ln.anchored else (" - our model only, not texted" if view.model_gated.get(g.league, True)
                                  else " - our model only")
    return (f"{ln.player} {more} than {ln.line:g} {d.stat_words(ln.stat)}: our chance {ln.p:.1%}{vs} "
            f"({_matchup(g, names)}){src}")


def board_sections(view: PPView, now: datetime, names: d.Names = d.DEFAULT_NAMES
                   ) -> tuple[list[str], list[str], list[str]]:
    """The PrizePicks tab as its three boxes: (summary, best picks now, every line by game)."""
    if "prizepicks" not in view.settings.venues:
        return ["PrizePicks is turned off. To turn it on, type: set venues kalshi,prizepicks"], [], []
    groups = _by_game(view)
    if not groups:
        return (["No PrizePicks lines are loaded yet. " + _next_text(view, now),
                 needed_text(view.settings),
                 'If they never show up, your odds plan may not include PrizePicks; type lines in with "pp add".'],
                [], [])
    stamps = [ln.fetched_at for ln in view.lines if ln.game_id in view.games]
    n = sum(len(lines) for _, lines in groups)
    best = best_structure(view.settings)
    n_clear = sum(_clears(ln, view.settings, best) for _, lines in groups for ln in lines)
    summary = [f"Lines as of {d.when(max(stamps), now)} PT - {n} line{'s' * (n != 1)} for "
               f"{len(groups)} game{'s' * (len(groups) != 1)}. " + _next_text(view, now),
               needed_text(view.settings),
               _entry_text(view),
               clear_text(n_clear, n).capitalize() + "."]
    top = [f"{i}. {best_pick_text(ln, view, names, best)}" for i, ln in enumerate(best_picks(view), 1)]
    lines_out: list[str] = []
    for g, lines in groups:
        k = sum(_clears(ln, view.settings, best) for ln in lines)
        lines_out.append(f"{_matchup(g, names)} - kickoff {d.when(g.kickoff, now)} PT - {clear_text(k, len(lines))}")
        gated = view.model_gated.get(g.league, True)
        lines_out += [f"  {line_text(ln, gated)}" for ln in lines]
        lines_out.append("")
    return summary, top, lines_out[:-1]


def board_lines(view: PPView, now: datetime, names: d.Names = d.DEFAULT_NAMES) -> list[str]:
    """The PrizePicks tab, line by line (all three boxes in order)."""
    summary, top, lines = board_sections(view, now, names)
    out = list(summary)
    if top:
        out += ["", "Best picks now:", *(f"  {t}" for t in top)]
    if lines:
        out += ["", *lines]
    return out


def status_line(view: PPView, now: datetime, names: d.Names = d.DEFAULT_NAMES) -> str:
    """One Home line, e.g. 'PrizePicks: 43 lines loaded for 1 game (Patriots at Bears). No entry yet:
    need good picks from at least 2 teams. More lines load ... See tab 5.'"""
    if "prizepicks" not in view.settings.venues:
        return "PrizePicks: turned off (type: set venues kalshi,prizepicks)."
    groups = _by_game(view)
    if not groups:
        return f"PrizePicks: no lines loaded yet. {_next_text(view, now)} {SEE_TAB}"
    n = sum(len(lines) for _, lines in groups)
    which = f" ({', '.join(_matchup(g, names) for g, _ in groups)})" if len(groups) <= 2 else ""
    head = f"PrizePicks: {n} line{'s' * (n != 1)} loaded for {len(groups)} game{'s' * (len(groups) != 1)}{which}."
    tail = "" if view.active else f" {_next_text(view, now)}"
    return f"{head} {_entry_text(view)}{tail} {SEE_TAB}"


# -- your own entry: pick lines on tab 5 ----------------------------------------------------------
PickKey = tuple[str, str, float, str]  # (player, stat, line, side): survives view refreshes
MIN_PICKS, MAX_PICKS = 2, 6
_LEAGUE_WORDS = {"nfl": "NFL", "cfb": "college football"}


def pick_key(ln: PPLine) -> PickKey:
    return (ln.player, ln.stat, ln.line, ln.side or "")


def leg_pick_key(leg: Leg) -> PickKey:
    return (leg.player or "", leg.stat or "", float(leg.line or 0.0), leg.side)


def pick_words(ln: PPLine) -> str:
    """'Jalen Hurts MORE than 1.5 passing touchdowns'."""
    more = "MORE" if ln.side == "over" else "LESS"
    return f"{ln.player} {more} than {ln.line:g} {d.stat_words(ln.stat)}"


def pickable(view: PPView) -> dict[PickKey, PPLine]:
    """Lines you can put in an entry: on a listed game, with our chance worked out."""
    return {pick_key(ln): ln for ln in view.lines
            if ln.game_id in view.games and ln.p is not None and ln.side is not None}


def reconcile(keys: list[PickKey], view: PPView) -> tuple[list[PickKey], list[PickKey]]:
    """(kept, dropped): chosen picks whose line is still loaded and priced, and those that are not."""
    live = pickable(view)
    return [k for k in keys if k in live], [k for k in keys if k not in live]


def dropped_text(key: PickKey) -> str:
    player, stat, line, side = key
    more = "MORE" if side == "over" else "LESS"
    return (f"Removed {player} {more} than {line:g} {d.stat_words(stat)} from your PrizePicks entry: that line "
            "is gone, too old to use, or we now favor the other side.")


def add_problem(chosen: list[PPLine], ln: PPLine, view: PPView, names: d.Names = d.DEFAULT_NAMES) -> str | None:
    """Why this line can't be added to the entry (a plain sentence), or None when it can."""
    if ln.p is None or ln.side is None:
        return f"{ln.player} {ln.line:g} {d.stat_words(ln.stat)} has no chance worked out yet, so it can't go in an entry."
    if ln.team is None:
        return (f"We don't know {ln.player}'s team yet, so the pick can't go in an entry (PrizePicks needs "
                "picks from at least 2 different teams).")
    if len(chosen) >= MAX_PICKS:
        return f"An entry holds at most {MAX_PICKS} picks; remove one first (Enter on a picked line)."
    if any(normalize_name(c.player) == normalize_name(ln.player) for c in chosen):
        return f"{ln.player} is already in your entry; PrizePicks allows one pick per player."
    cap = view.settings.pp_max_per_game
    same = sum(c.game_id == ln.game_id for c in chosen)
    if same >= cap:
        g = view.games.get(ln.game_id)
        where = _matchup(g, names) if g is not None else "that game"
        return (f"Your entry already has {same} pick{'s' * (same != 1)} from {where}; we allow at most {cap} "
                f"from one game (type: set pp_max_per_game 3 to allow more).")
    return None


@dataclass
class EntryEval:
    """Your entry, worked out: the best allowed entry type, the chance of each outcome and the stake."""
    picks: list[PPLine]
    problems: list[str] = field(default_factory=list)  # PrizePicks rules it breaks: can't be recorded
    warnings: list[str] = field(default_factory=list)  # allowed, but worth knowing
    structure: Structure | None = None
    table: dict[int, float] = field(default_factory=dict)  # hits -> times the stake paid back
    dist: list[float] = field(default_factory=list)  # chance of exactly k hits (the linked worst case)
    ev: float | None = None  # profit per $1 put in, on average
    stake_cents: int = 0
    stake_note: str = ""
    legs: list[Pick] = field(default_factory=list)
    linked: bool = False  # some picks share a game

    @property
    def ready(self) -> bool:
        return self.structure is not None and not self.problems

    @property
    def expected_profit_cents(self) -> int:
        return round((self.ev or 0.0) * self.stake_cents)

    @property
    def p_paid(self) -> float:
        return sum(self.dist[k] for k, m in self.table.items() if m > 0 and k < len(self.dist))

    @property
    def p_profit(self) -> float:
        return sum(self.dist[k] for k, m in self.table.items() if m > 1 and k < len(self.dist))


def _structure_words(n: int, kind: str) -> str:
    return d.structure_text(f"{n}-{kind}")


def evaluate_entry(picks: list[PPLine], view: PPView, names: d.Names = d.DEFAULT_NAMES) -> EntryEval:
    """Check your picks against PrizePicks' rules and work out the entry the same way the builder does:
    the allowed entry type with the best long-run growth, same-game picks scored at the worst of
    linked-together, independent and linked-apart, and the stake from the same sizing and limits."""
    s, out = view.settings, EntryEval(list(picks))
    n = len(picks)
    if n < MIN_PICKS:
        out.problems.append(f"Pick at least {MIN_PICKS} lines; a PrizePicks entry needs {MIN_PICKS} to "
                            f"{MAX_PICKS} picks.")
    if n > MAX_PICKS:
        out.problems.append(f"A PrizePicks entry holds at most {MAX_PICKS} picks; remove {n - MAX_PICKS}.")
    players = Counter(normalize_name(ln.player) for ln in picks)
    for ln in picks:
        if players[normalize_name(ln.player)] > 1:
            out.problems.append(f"{ln.player} is in your entry twice; PrizePicks allows one pick per player.")
            players[normalize_name(ln.player)] = 0
    for gid, k in Counter(ln.game_id for ln in picks).items():
        if k > s.pp_max_per_game:
            g = view.games.get(gid)
            out.problems.append(f"{k} picks from {_matchup(g, names) if g else 'one game'}; we allow at most "
                                f"{s.pp_max_per_game} from one game.")
    if any(ln.p is None or ln.side is None or ln.team is None for ln in picks):
        out.problems.append("Some picks have no chance worked out or no known team; remove them.")
        return out
    teams = {ln.team for ln in picks}
    if n >= MIN_PICKS and len(teams) < 2:
        team = names.team(view.games[picks[0].game_id].league, picks[0].team) if picks[0].game_id in view.games \
            else picks[0].team
        out.problems.append(f"All your picks are on one team ({team}); PrizePicks needs picks from at least 2 "
                            "different teams.")
    try:
        table = payout_table(s)
    except ValueError:
        out.problems.append("The PrizePicks payout setting can't be read, so the entry can't be worked out.")
        return out
    out.legs = [Pick(ln.player, ln.team or "", ln.game_id, ln.stat, ln.line, ln.side, ln.p, ln.anchored)
                for ln in picks]  # type: ignore[arg-type]
    out.linked = len({ln.game_id for ln in picks}) < n
    cands = [st for st in allowed_structures(s) if st[0] == n and st in table and table[st].get(n, 0) > 1]
    if n >= MIN_PICKS and not cands:
        sizes = sorted({st[0] for st in allowed_structures(s)})
        out.problems.append(f"None of the entry types we use has {n} picks (we use entries of "
                            f"{', '.join(map(str, sizes))} picks).")
    best: tuple[float, float, Structure] | None = None
    for st in cands:
        dist = worst_distribution(out.legs, st, table, s.copula_rho)
        ev = sum(dist[k] * m for k, m in table[st].items() if k < len(dist)) - 1.0
        _, growth = robust_kelly(out.legs, st, table, s.copula_rho)
        if best is None or (growth, ev) > (best[0], best[1]):
            best = (growth, ev, st)
    if best is not None:
        _, out.ev, out.structure = best
        out.table = dict(table[out.structure])
        out.dist = worst_distribution(out.legs, out.structure, table, s.copula_rho)
    need_be = breakeven(out.structure, table) if out.structure else (best_structure(s) or (None, None))[1]
    for ln in picks:
        if need_be is not None:
            need = need_be + (s.pp_margin_market if ln.anchored else s.pp_margin_model)
            if ln.p < need:
                out.warnings.append(f"{ln.player} is just under the {need:.1%} chance it needs."
                                    if f"{ln.p:.1%}" == f"{need:.1%}" else
                                    f"{ln.player} is below the chance it needs ({ln.p:.1%}, needs {need:.1%}).")
        if not ln.anchored:
            out.warnings.append(f"{ln.player}'s chance is from our model only, not backed by sportsbooks.")
    held = sorted({view.games[ln.game_id].league for ln in picks if ln.game_id in view.games
                   and view.watch_only.get(view.games[ln.game_id].league)})
    for lg in held:
        out.warnings.append(f"PrizePicks ideas for {_LEAGUE_WORDS.get(lg, lg)} are watch-only or paused right "
                            "now, so we would not text this entry.")
    if out.structure is None or out.problems:
        return out
    minimum = max(s.min_stake_cents, 1)
    if out.ev is None or out.ev <= 0:
        out.stake_note = "We suggest not playing it: together these picks don't show an expected profit."
    elif view.budget is None:
        out.stake_note = "The bankroll isn't loaded yet, so there is no suggested amount."
    else:
        daily_left = view.daily_left if view.daily_left is not None else 0
        stake = pp_stake(out.legs, out.structure, table, s, view.budget, daily_left)
        if stake >= minimum:
            out.stake_cents = stake
            out.stake_note = "based on our chances and your bankroll limits"
        elif daily_left < minimum:
            out.stake_note = "Today's PrizePicks limit is used up (or the bankroll is empty)."
        else:
            out.stake_note = (f"Your bankroll limits leave less than the {d.money(minimum)} smallest bet, so "
                              "there is no suggested amount.")
    return out


def _pays(mult: float, stake: int) -> str:
    if mult == 1:
        return "you get your money back"
    return f"pays back {d.money(round(mult * stake))}" if stake > 0 else f"pays back {mult:g} times what you put in"


def outcome_texts(ev: EntryEval) -> list[str]:
    """'All 3 hit: pays back $60.00 - chance 21.4%', one per paying outcome, then 'Fewer hit: ...'."""
    if ev.structure is None or not ev.dist:
        return []
    n, out, rest = ev.structure[0], [], 1.0
    for k in sorted((k for k, m in ev.table.items() if m > 0), reverse=True):
        p = ev.dist[k] if k < len(ev.dist) else 0.0
        rest -= p
        who = f"All {n} hit" if k == n else f"{k} of {n} hit"
        out.append(f"{who}: {_pays(ev.table[k], ev.stake_cents)} - chance {p:.1%}")
    out.append(f"Fewer hit: you lose what you put in - chance {max(rest, 0.0):.1%}")
    return out


def entry_lines(ev: EntryEval, view: PPView, names: d.Names = d.DEFAULT_NAMES) -> list[str]:
    """The YOUR ENTRY box, line by line, in plain English."""
    if not ev.picks:
        return [f"No picks yet. Highlight a line below and press Enter to add it ({MIN_PICKS} to {MAX_PICKS} "
                "picks, at least 2 teams)."]
    s = view.settings
    out = []
    for i, ln in enumerate(ev.picks, 1):
        g = view.games.get(ln.game_id)
        chance = f" - our chance {ln.p:.1%}" if ln.p is not None else ""
        out.append(f"{i}. {pick_words(ln)}{chance}" + (f" ({_matchup(g, names)})" if g else ""))
    for msg in ev.problems:
        out.append(f"Can't record yet: {msg}")
    if ev.structure is not None and not ev.problems:
        n, kind = ev.structure
        rule = "every pick must hit" if kind == "power" else "it still pays with a miss"
        out.append(f"Best entry type for these {n} picks: {_structure_words(n, kind)} ({rule}).")
        out.append(" | ".join(outcome_texts(ev)) + ".")
        out.append(f"Chance of getting any money back: {ev.p_paid:.1%}. Chance of a profit: {ev.p_profit:.1%}.")
        if ev.linked:
            out.append("Picks from the same game are treated as linked: we count whichever way hurts this "
                       "entry most.")
        if ev.stake_cents > 0:
            out.append(f"Suggested amount: {d.money(ev.stake_cents)} ({ev.stake_note}). Expected profit: about "
                       f"{d.money(ev.expected_profit_cents)} ({ev.ev:+.1%} of what you put in).")
        else:
            per_dollar = f" On average it {'makes' if (ev.ev or 0) > 0 else 'loses'} about " \
                         f"{abs(ev.ev or 0) * 100:.1f} cents per $1 put in." if ev.ev is not None else ""
            out.append(f"Suggested amount: none. {ev.stake_note}{per_dollar}")
    for msg in ev.warnings:
        out.append(f"Heads up: {msg}")
    tail = "Press c after you place it on PrizePicks to record it" if ev.ready else "Fix the above to record it"
    out.append(f"{tail}; x clears the entry. We never place bets for you.")
    return out


@dataclass(frozen=True)
class PickRow:
    """One row of a pick list: a game heading (key None), a pickable line, or a line we can't price."""
    text: str
    key: PickKey | None = None
    chosen: bool = False
    heading: bool = False


def _mark(chosen: bool) -> str:
    return "[x] " if chosen else "[ ] "


def pick_row_text(ln: PPLine, view: PPView, best=None, gated: bool = True, matchup: str = "") -> str:
    """'Caleb Williams MORE than 249.5 passing yards - our chance 56.0%, needs 56.2% (sportsbooks)'."""
    need = needed_chance(ln, view.settings, best if best is not None else best_structure(view.settings))
    needs = "" if need is None else f", needs {need:.1%}"
    where = f" - {matchup}" if matchup else ""
    return f"{pick_words(ln)} - our chance {ln.p:.1%}{needs} {_source(ln, gated)}{where}"


def best_rows(view: PPView, chosen: set[PickKey], names: d.Names = d.DEFAULT_NAMES) -> list[PickRow]:
    """BEST PICKS NOW as selectable rows."""
    best = best_structure(view.settings)
    rows = []
    for i, ln in enumerate(best_picks(view), 1):
        g = view.games[ln.game_id]
        k = pick_key(ln)
        text = pick_row_text(ln, view, best, view.model_gated.get(g.league, True), _matchup(g, names))
        rows.append(PickRow(f"{_mark(k in chosen)}{i}. {text}", k, k in chosen))
    return rows


def all_rows(view: PPView, chosen: set[PickKey], now: datetime, names: d.Names = d.DEFAULT_NAMES
             ) -> list[PickRow]:
    """ALL LINES as rows: a heading per game, then each line (lines we can't price are not pickable)."""
    best = best_structure(view.settings)
    rows: list[PickRow] = []
    for g, lines in _by_game(view):
        k = sum(_clears(ln, view.settings, best) for ln in lines)
        rows.append(PickRow(f"{_matchup(g, names)} - kickoff {d.when(g.kickoff, now)} PT - "
                            f"{clear_text(k, len(lines))}", heading=True))
        gated = view.model_gated.get(g.league, True)
        for ln in lines:
            if ln.p is None or ln.side is None:
                rows.append(PickRow(f"    {line_text(ln, gated)}"))
                continue
            key = pick_key(ln)
            rows.append(PickRow(_mark(key in chosen) + pick_row_text(ln, view, best, gated), key, key in chosen))
    return rows


def _ago(t: datetime, now: datetime) -> str:
    mins = max(0, int((now - t).total_seconds() // 60))
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    h, m = divmod(mins, 60)
    return f"{h} hr {m} min ago" if m else f"{h} hr ago"


def pick_why_lines(ln: PPLine, view: PPView, now: datetime, names: d.Names = d.DEFAULT_NAMES) -> list[str]:
    """'Why this pick?': the sportsbook prices behind it, how our chance was worked out, the chance it
    needs and when the line was pulled. The last line holds the internal details."""
    s = view.settings
    g = view.games.get(ln.game_id)
    where = f" ({_matchup(g, names)}, kickoff {d.when(g.kickoff, now)} PT)" if g else ""
    out = [f"The pick: {pick_words(ln)}{where}." if ln.side else
           f"The line: {ln.player} {ln.line:g} {d.stat_words(ln.stat)}{where}."]
    key = normalize_name(ln.player)
    newest: dict[tuple[str, float], dict[str, PropQuote]] = {}
    for q in view.props:
        if (q.book == "prizepicks" or q.decimal is None or q.game_id != ln.game_id or q.stat != ln.stat
                or normalize_name(q.player) != key):
            continue
        sides = newest.setdefault((q.book, q.point), {})
        if q.side not in sides or q.last_update > sides[q.side].last_update:
            sides[q.side] = q
    if newest:
        out.append("Sportsbook prices (what a $1 bet pays back):")
        for (book, pt), sides in sorted(newest.items(), key=lambda kv: (abs(kv[0][1] - ln.line), kv[0])):
            parts = [f"{'MORE' if side == 'over' else 'LESS'} ${q.decimal:.2f}"
                     for side, q in sorted(sides.items())]
            out.append(f"  {d.book_name(book)}, line {pt:g}: {', '.join(parts)}")
        nearest = min((pt for _, pt in newest), key=lambda pt: (abs(pt - ln.line), pt))
    else:
        out.append("No sportsbook has a price for this player and stat.")
        nearest = None
    if ln.p is None or ln.side is None:
        why = "it is too old to use" if not ln.fresh else "we have no estimate yet" if ln.team else \
            "we don't know the player's team yet"
        out.append(f"Our chance: none, because {why}.")
    elif ln.anchored:
        moved = (f", then moved from their line of {nearest:g} to PrizePicks' {ln.line:g}"
                 if nearest is not None and nearest != ln.line else "")
        out.append("How we worked out our chance: we took each sportsbook's MORE and LESS prices, removed the "
                   f"bookmaker's cut the cautious way, and used the middle value across books{moved}.")
    else:
        out.append("How we worked out our chance: no sportsbook prices this line, so it comes from our own "
                   "model's projection of the player (not backed by sportsbooks).")
    best = best_structure(s)
    if ln.p is not None and ln.side is not None:
        need = needed_chance(ln, s, best)
        if best is not None and need is not None:
            (n, kind), be = best
            margin = s.pp_margin_market if ln.anchored else s.pp_margin_model
            verdict = "clears it" if ln.p >= need else "falls short"
            out.append(f"Our chance: {ln.p:.1%}. Needed chance: {need:.1%} (break-even {be:.1%} for a "
                       f"{_structure_words(n, kind)}, plus a {margin * 100:.1f} point safety margin for "
                       f"{'sportsbook-backed' if ln.anchored else 'model-only'} picks), so it {verdict}.")
        else:
            out.append(f"Our chance: {ln.p:.1%}. None of the allowed entry types can pay off with these payouts.")
    out.append(f"PrizePicks line pulled at {d.when(ln.fetched_at, now)} PT ({_ago(ln.fetched_at, now)}).")
    out.append(f"[details] {ln.game_id} {ln.player}|{ln.stat}|{ln.line:g}|{ln.side} "
               f"p={'-' if ln.p is None else f'{ln.p:.4f}'} anchored={ln.anchored} fresh={ln.fresh}")
    return out
