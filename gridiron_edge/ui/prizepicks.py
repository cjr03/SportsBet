"""PrizePicks tab and Home status line: every loaded line in plain English, and why no entry. Pure."""
from dataclasses import dataclass, field
from datetime import datetime

from gridiron_edge import describe as d
from gridiron_edge.config import Settings
from gridiron_edge.pipeline import PPLine
from gridiron_edge.strategy.prizepicks import Structure, allowed_structures, breakeven, payout_table
from gridiron_edge.types import Game

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
