"""The PrizePicks boxes on Home: every loaded line in plain English, and why no entry. Pure."""
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from gridiron_edge import describe as d
from gridiron_edge.config import Settings
from gridiron_edge.matching import normalize_name
from gridiron_edge.pipeline import PPLine, pp_stake
from gridiron_edge.strategy.prizepicks import (
    Pick, Structure, allowed_structures, breakeven, payout_table, robust_kelly, top_entries, worst_distribution,
)
from gridiron_edge.types import Game, Leg, PropQuote

@dataclass
class PPView:
    """What the PrizePicks boxes on Home show; built by Services.pp_view."""
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
    refresh_games: int | None = None  # games `pp refresh` would load now; None: no working Odds API key
    refresh_cost: int = 3  # Odds API credits per game for a props pull
    refresh_why: str = ""  # why nothing can load now although games are close (credits kept for game odds)


def _hours(seconds: float) -> str:
    """'3 hours', '1 hour', '90 minutes'."""
    mins = round(seconds / 60)
    return f"{mins // 60} hour{'s' * (mins != 60)}" if mins % 60 == 0 else f"{mins} minutes"


def age_words(t: datetime, now: datetime) -> str:
    """How old something is, rounded down: '7 hours', '1 hour', '45 minutes'."""
    mins = max(0, int((now - t).total_seconds() // 60))
    if mins < 60:
        return f"{mins} minute{'s' * (mins != 1)}"
    h = mins // 60
    return f"{h} hour{'s' * (h != 1)}"


def _age_short(t: datetime, now: datetime) -> str:
    """'7 hr ago', '45 min ago'."""
    mins = max(0, int((now - t).total_seconds() // 60))
    return f"{mins} min ago" if mins < 60 else f"{mins // 60} hr ago"


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
            return (f"The loaded lines are over {_hours(info.get('seconds', 3 * 3600))} old, too old for an "
                    "entry we text.")
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


def _source(ln: PPLine, gated: bool, now: datetime | None = None) -> str:
    if not ln.fresh and now is not None:  # older than props_stale_seconds: say how old the prices are
        ago = _age_short(ln.fetched_at, now)
        return f"(sportsbook prices from {ago})" if ln.anchored else f"(our model only, line from {ago})"
    if ln.anchored:
        return "(sportsbooks)"
    return "(our model only - not texted)" if gated else "(our model only)"


NO_PRICE = "no price yet (no sportsbook prices and no estimate from our model)"


def line_text(ln: PPLine, gated: bool = True, now: datetime | None = None) -> str:
    """e.g. 'Caleb Williams MORE than 249.5 passing yards - our chance 56.0% (sportsbooks)'."""
    line = f"{ln.line:g}"
    if ln.p is None or ln.side is None:
        why = NO_PRICE if ln.team else "team unknown"
        return f"{ln.player} {line} {d.stat_words(ln.stat)} - {why}"
    more = "MORE" if ln.side == "over" else "LESS"
    return (f"{ln.player} {more} than {line} {d.stat_words(ln.stat)} - our chance {ln.p:.1%} "
            f"{_source(ln, gated, now)}")


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


def refresh_text(view: PPView) -> str:
    """How to load new lines now, with the cost."""
    if view.refresh_games is None:
        return ("New lines need a working Odds API key (ODDS_API_KEY in .env); you can type lines in with "
                "pp add.")
    if view.refresh_games == 0 and view.refresh_why:
        return view.refresh_why
    if view.refresh_games == 0:
        return (f"Type pp refresh (or press r) to load new ones once a game is within 24 hours of kickoff "
                f"(about {view.refresh_cost} Odds API credits per game).")
    k = view.refresh_games
    return (f"To load new ones, press r or type pp refresh (about {k * view.refresh_cost} Odds API credits, "
            f"{k} game{'s' * (k != 1)}).")


def old_lines_text(view: PPView, now: datetime) -> str | None:
    """When the lines are older than the fresh limit: how old, what that means, and how to refresh."""
    old = [ln.fetched_at for ln in view.lines if ln.game_id in view.games and not ln.fresh]
    if not old:
        return None
    limit = _hours(view.settings.props_stale_seconds)
    return (f"These lines are {age_words(max(old), now)} old, past the {limit} we trust for texted entries: "
            f"you can still pick them, but prices may have moved. {refresh_text(view)}")


def nothing_to_pick_text(view: PPView | None) -> str:
    """Why Enter or Down on Home finds no line to pick, and what to do."""
    if view is not None and "prizepicks" not in view.settings.venues:
        return "PrizePicks is turned off. To turn it on, type: set venues kalshi,prizepicks"
    cost = view.refresh_cost if view is not None else 3
    if view is not None and view.refresh_games is None:
        what = 'type lines in with "pp add" (no working Odds API key to load them)'
    else:
        what = f"type pp refresh to load them now (about {cost} credits per game)"
    if view is None or not _by_game(view):
        return f"No line can be picked yet: lines load about 3 hours before kickoff, or {what}."
    return ("No line can be picked yet: none of the loaded lines has a sportsbook price or an estimate from our "
            f"model, or we don't know the player's team. Wait for new lines, or {what}.")


PLAYS_EMPTY = "No play shows an expected profit with the fresh lines yet."


def plays_empty_text(view: PPView) -> str:
    """SUGGESTED PLAYS when it is empty: plays are built from fresh lines only."""
    if any(not ln.fresh and ln.p is not None for ln in view.lines if ln.game_id in view.games):
        return ("No play from fresh lines shows an expected profit. Older lines are in ALL LINES below; press r "
                "to load new ones.")
    return PLAYS_EMPTY


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
    return ln.p is not None and need is not None and ln.p >= need


def clear_text(n_clear: int, n: int) -> str:
    """'4 of 18 lines clear the needed chance'."""
    return f"{n_clear} of {n} line{'s' * (n != 1)} clear{'s' * (n == 1)} the needed chance"


def best_picks(view: PPView, n: int = 3) -> list[PPLine]:
    """The n picks with the highest chance above what they need (fresh lines that reach it only)."""
    best = best_structure(view.settings)
    usable = [ln for ln in view.lines if ln.game_id in view.games and ln.team is not None and ln.fresh
              and _clears(ln, view.settings, best)]

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
    """The PrizePicks boxes: (summary, best picks now, every line by game)."""
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
               needed_text(view.settings)]
    old = old_lines_text(view, now)
    if not (old and view.blocker is not None and view.blocker[0] == "stale"):  # the old-lines line says why
        summary.append(_entry_text(view))
    one = one_game_text(view)
    if one:  # before the last two lines: with picks chosen, YOUR ENTRY repeats it and the summary keeps 2
        summary.append(one)
    summary.append(clear_text(n_clear, n).capitalize() + ".")
    if old:
        summary.append(old)
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
    """The PrizePicks boxes, line by line (all three in order)."""
    summary, top, lines = board_sections(view, now, names)
    out = list(summary)
    if top:
        out += ["", "Best picks now:", *(f"  {t}" for t in top)]
    if lines:
        out += ["", *lines]
    return out


# -- your own entry: pick lines on Home ----------------------------------------------------------
PickKey = tuple[str, str, float, str]  # (player, stat, line, side): survives view refreshes
MIN_PICKS, MAX_PICKS = 2, 6
_LEAGUE_WORDS = {"nfl": "NFL", "cfb": "college football"}


def min_picks(s: Settings) -> int:
    """The fewest picks any entry type we use takes (PrizePicks itself allows 2)."""
    sizes = [st[0] for st in allowed_structures(s)]
    return max(MIN_PICKS, min(sizes)) if sizes else MIN_PICKS


def one_game_text(view: PPView) -> str | None:
    """When the loaded games can't fill even the smallest entry under the per-game cap: say so, and what to do."""
    games = {ln.game_id for ln in pickable(view).values()}
    need, cap = min_picks(view.settings), view.settings.pp_max_per_game
    if not games or len(games) * cap >= need:
        return None
    k = len(games)
    has = f"Only {k} game{' has' if k == 1 else 's have'} lines"
    per_game = -(-need // k)
    fix = f", or type: set pp_max_per_game {per_game}" if per_game <= MAX_PICKS else "."
    return (f"{has}, and an entry needs {need} picks with at most {cap} from one game. Wait for more games to "
            f"load{fix}")


def pick_key(ln: PPLine) -> PickKey:
    return (ln.player, ln.stat, ln.line, ln.side or "")


def leg_pick_key(leg: Leg) -> PickKey:
    return (leg.player or "", leg.stat or "", float(leg.line or 0.0), leg.side)


def pick_words(ln: PPLine) -> str:
    """'Jalen Hurts MORE than 1.5 passing touchdowns'."""
    more = "MORE" if ln.side == "over" else "LESS"
    return f"{ln.player} {more} than {ln.line:g} {d.stat_words(ln.stat)}"


def pickable(view: PPView) -> dict[PickKey, PPLine]:
    """Lines you can put in an entry: on a listed game, with our chance worked out and a known team."""
    return {pick_key(ln): ln for ln in view.lines
            if ln.game_id in view.games and ln.p is not None and ln.side is not None and ln.team is not None}


def reconcile(keys: list[PickKey], view: PPView) -> tuple[list[PickKey], list[PickKey]]:
    """(kept, dropped): chosen picks whose line is still loaded and priced, and those that are not.
    While no line at all is priced (startup, or a refresh before player lines load) every pick is
    kept, so a short gap never empties your entry."""
    live = pickable(view)
    if not live:
        return list(keys), []
    return [k for k in keys if k in live], [k for k in keys if k not in live]


def dropped_text(key: PickKey) -> str:
    player, stat, line, side = key
    more = "MORE" if side == "over" else "LESS"
    return (f"Removed {player} {more} than {line:g} {d.stat_words(stat)} from your PrizePicks entry: that line "
            "is gone or we now favor the other side.")


def add_problem(chosen: list[PPLine], ln: PPLine, view: PPView, names: d.Names = d.DEFAULT_NAMES) -> str | None:
    """Why this line can't be added to the entry (a plain sentence), or None when it can."""
    if ln.p is None or ln.side is None:
        return f"{ln.player} {ln.line:g} {d.stat_words(ln.stat)} has no chance worked out yet, so it can't go in an entry."
    if ln.team is None:
        return (f"We don't know {ln.player}'s team yet, so the pick can't go in an entry (PrizePicks needs "
                "picks from at least 2 different teams).")
    if len(chosen) >= MAX_PICKS:
        return f"An entry holds at most {MAX_PICKS} picks; remove one first (Enter on a picked line)."
    if any(pick_key(c) == pick_key(ln) for c in chosen):
        return f"{pick_words(ln)} is already in your entry (Enter on it removes it)."
    cap = view.settings.pp_max_per_game
    same = sum(c.game_id == ln.game_id for c in chosen)
    if same >= cap:
        g = view.games.get(ln.game_id)
        where = _matchup(g, names) if g is not None else "that game"
        why = (f"Your entry already has {same} pick{'s' * (same != 1)} from {where}; we allow at most {cap} "
               "from one game.")
        if one_game_text(view):
            k = len({x.game_id for x in pickable(view).values()})
            why += (f" Only {k} game{' has' if k == 1 else 's have'} lines right now, so wait for more games "
                    "to load.")
        return why
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
    old_since: datetime | None = None  # oldest pull among picks older than the fresh limit

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
    old = [ln.fetched_at for ln in picks if not ln.fresh]
    out.old_since = min(old) if old else None
    least = min_picks(s)
    if n < least:
        out.problems.append(f"Pick at least {least} lines; the entry types we use need {least} to "
                            f"{MAX_PICKS} picks.")
    if n > MAX_PICKS:
        out.problems.append(f"A PrizePicks entry holds at most {MAX_PICKS} picks; remove {n - MAX_PICKS}.")
    keys = Counter(pick_key(ln) for ln in picks)
    for ln in picks:
        if keys[pick_key(ln)] > 1:
            out.problems.append(f"{pick_words(ln)} is in your entry twice; remove one.")
            keys[pick_key(ln)] = 0
    players = Counter(normalize_name(ln.player) for ln in picks)
    for ln in picks:  # several stats on one player: your call, but PrizePicks itself may not take it
        if players[normalize_name(ln.player)] > 1:
            out.warnings.append(f"{ln.player} is in your entry twice (two different stats); PrizePicks usually "
                                "allows one pick per player, so check the site takes it.")
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
    if n >= least and not cands:
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
    ref = (out.structure, breakeven(out.structure, table)) if out.structure else best_structure(s)
    for ln in picks:
        if ref is not None:
            need = ref[1] + (s.pp_margin_market if ln.anchored else s.pp_margin_model)
            what = _structure_words(*ref[0])
            if ln.p < need:
                out.warnings.append(f"{ln.player} is just under the {need:.1%} a {what} needs."
                                    if f"{ln.p:.1%}" == f"{need:.1%}" else
                                    f"{ln.player} is below the {need:.1%} a {what} needs (our chance {ln.p:.1%}).")
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


def old_prices_text(ev: EntryEval, now: datetime) -> str | None:
    """'Prices are 7 hours old and may have moved. Type pp refresh ...' when a pick uses old prices."""
    if ev.old_since is None:
        return None
    return (f"Prices are {age_words(ev.old_since, now)} old and may have moved. Type pp refresh to load new ones "
            "before you place this.")


def entry_lines(ev: EntryEval, view: PPView, names: d.Names = d.DEFAULT_NAMES, waiting: int = 0,
                now: datetime | None = None) -> list[str]:
    """The YOUR ENTRY box, line by line, in plain English; the last line names the keys. `waiting` is
    how many chosen picks are kept while their lines aren't loaded."""
    if not ev.picks and waiting:
        kept = (f"Your {waiting} picks are kept, but their lines aren't loaded right now; the entry is worked "
                "out again when they come back." if waiting != 1 else
                "Your 1 pick is kept, but its line isn't loaded right now; the entry is worked out again when "
                "it comes back.")
        return [kept, "x clears it. We never place bets for you."]
    one = one_game_text(view)
    if not ev.picks:
        return [f"No picks yet. Highlight a line below and press Enter to add it ({min_picks(view.settings)} to "
                f"{MAX_PICKS} picks, at least 2 teams).", *([one] if one else [])]
    s = view.settings
    out = []
    for i, ln in enumerate(ev.picks, 1):
        g = view.games.get(ln.game_id)
        chance = f" - our chance {ln.p:.1%}" if ln.p is not None else ""
        out.append(f"{i}. {pick_words(ln)}{chance}" + (f" ({_matchup(g, names)})" if g else ""))
    old = old_prices_text(ev, now) if now is not None else None
    if old:
        out.append(f"Warning: {old}")
    for msg in ev.problems:
        out.append(f"Can't record yet: {msg}")
    if ev.structure is not None and not ev.problems:
        n, kind = ev.structure
        rule = "every pick must hit" if kind == "power" else "it still pays with a miss"
        out.append(f"Best entry type for these {n} picks: {_structure_words(n, kind)} ({rule}).")
        out += outcome_texts(ev)
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
    note = _list_type_note(ev, s)
    if note:
        out.append(note)
    if one and not ev.ready:
        out.append(one)
    tail = "Press c after you place it on PrizePicks to record it" if ev.ready else "Fix the above to record it"
    out.append(f"{tail}; x clears it. We never place bets for you.")
    return out


def _list_type_note(ev: EntryEval, s: Settings) -> str | None:
    """When the lists' 'needs' numbers are for a different entry type than this entry, say so."""
    best = best_structure(s)
    if ev.structure is None or ev.problems or best is None or best[0] == ev.structure:
        return None
    try:
        be = breakeven(ev.structure, payout_table(s))
    except ValueError:
        return None
    return (f"The needed chances in the lists are for a {_structure_words(*best[0])}; this "
            f"{_structure_words(*ev.structure)} needs {be + s.pp_margin_market:.1%} per pick "
            f"({be + s.pp_margin_model:.1%} for model-only picks).")


# -- suggested plays: the best few entries of each size, ranked ---------------------------------------
PLAYS_PER_SIZE, PLAYS_LIMIT = 3, 10


def suggested_plays(view: PPView) -> list[EntryEval]:
    """Up to PLAYS_LIMIT 2- to 6-pick plays from the fresh lines (up to PLAYS_PER_SIZE of each size),
    ranked by long-run growth of the bankroll; each worked out like YOUR ENTRY, so its buy-in is the
    amount YOUR ENTRY would suggest."""
    s = view.settings
    lines = {(ln.player, ln.stat, ln.line, ln.side): ln for ln in pickable(view).values() if ln.fresh}
    picks = [Pick(ln.player, ln.team or "", ln.game_id, ln.stat, ln.line, ln.side, ln.p, ln.anchored)
             for ln in lines.values()]  # type: ignore[arg-type]
    try:
        table = payout_table(s)
        found = top_entries(picks, s, allowed_structures(s), payouts=table, brute=s.pp_brute_force,
                            per_size=PLAYS_PER_SIZE, limit=PLAYS_LIMIT)
    except ValueError:  # a bad payout override: no plays rather than a broken tab
        return []
    out = []
    for legs, *_ in found:
        ev = evaluate_entry([lines[(pk.player, pk.stat, pk.line, pk.side)] for pk in legs], view)
        if ev.ready:
            out.append(ev)
    return out


_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def _last_name(player: str) -> str:
    parts = [w for w in player.split() if w.rstrip(".").lower() not in _SUFFIXES]
    return parts[-1] if parts else player


def play_row_text(ev: EntryEval, rank: int) -> str:
    """'1. 3-pick Power Play - buy in $5.00, expect +$1.10 (+22.0%), profit chance 24.1% - Mahomes, Hurts, Kelce'."""
    n, kind = ev.structure  # type: ignore[misc]
    buy = f"buy in {d.money(ev.stake_cents)}" if ev.stake_cents > 0 else "no buy-in"
    gain = (f"expect {'+' if ev.expected_profit_cents >= 0 else ''}{d.money(ev.expected_profit_cents)} "
            f"({ev.ev:+.1%})" if ev.stake_cents > 0 else f"expect {ev.ev:+.1%} per $1")
    who = ", ".join(_last_name(ln.player) for ln in ev.picks)
    return f"{rank}. {_structure_words(n, kind)} - {buy}, {gain}, profit chance {ev.p_profit:.1%} - {who}"


ONE_AT_A_TIME = ("Each buy-in assumes it is the only play you make; if you play more than one, use smaller "
                 "amounts, since they all come out of the same bankroll and today's limit.")


def plays_note(plays: list[EntryEval]) -> str | None:
    """Why no play has a buy-in (once), or, when more than one has one, that each is worked out alone.
    A play without a buy-in among others says 'no buy-in' on its own row."""
    staked = sum(p.stake_cents > 0 for p in plays)
    notes = [p.stake_note for p in plays if p.stake_cents <= 0 and p.stake_note]
    if plays and not staked and notes:
        return f"No buy-in: {_lower_first(notes[0])}"
    out = [ONE_AT_A_TIME] if staked > 1 else []
    if notes:
        out.append(f"Plays marked no buy-in: {_lower_first(notes[0])}")
    return " ".join(out) or None


OLD_NOT_IN_PLAYS = "old prices, so not in a play"


def pick_amount_text(ln: PPLine, plays: list[EntryEval]) -> str:
    """Where a pick is used: 'play #2, $15.32 buy-in' for the best suggested play holding it."""
    key = pick_key(ln)
    for rank, ev in enumerate(plays, 1):
        if key in {pick_key(x) for x in ev.picks}:
            buy = f"{d.money(ev.stake_cents)} buy-in" if ev.stake_cents > 0 else "no buy-in"
            return f"play #{rank}, {buy}"
    return OLD_NOT_IN_PLAYS if not ln.fresh else "in no play"


def opened_play_text(rank: int, ev: EntryEval) -> str:
    n, kind = ev.structure  # type: ignore[misc]
    buy = f" Suggested buy-in {d.money(ev.stake_cents)}." if ev.stake_cents > 0 else ""
    return f"Opened suggested play #{rank} ({_structure_words(n, kind)}) in YOUR ENTRY.{buy}"


# -- what picking writes to the log ---------------------------------------------------------------
CANT_PICK = "That line can't be picked right now."
WAIT_FOR_LINES = "Your picks' lines aren't loaded right now; wait for them to come back before recording."
CLEARED = "Cleared your PrizePicks entry."
ALREADY_EMPTY = "Your PrizePicks entry is already empty."
NOT_RECORDED = "PrizePicks entry not recorded."
ONLY_PP_OPENS = "Only PrizePicks entries can be opened in YOUR ENTRY."


def empty_entry_text(s: Settings) -> str:
    return f"Your PrizePicks entry is empty: pick {min_picks(s)} to {MAX_PICKS} lines with Enter first."


def added_text(ln: PPLine, n: int) -> str:
    return f"Added {pick_words(ln)} to your PrizePicks entry ({n} pick{'s' * (n != 1)})."


def removed_text(words: str) -> str:
    return f"Took {words} out of your PrizePicks entry."


def same_picks_text(rec_id: int) -> str:
    return f"Same picks as PrizePicks idea #{rec_id} in PLACE NOW."


def opened_text(rec_id: int) -> str:
    return f"Opened PrizePicks idea #{rec_id} in YOUR ENTRY."


@dataclass(frozen=True)
class PickRow:
    """One row of a pick list: a game heading (key None), a pickable line, or a line we can't price."""
    text: str
    key: PickKey | None = None
    chosen: bool = False
    heading: bool = False


def _mark(chosen: bool) -> str:
    return "[x] " if chosen else "[ ] "


def pick_row_text(ln: PPLine, view: PPView, best=None, gated: bool = True, matchup: str = "",
                  now: datetime | None = None) -> str:
    """'Caleb Williams MORE than 249.5 passing yards - our chance 56.0%, needs 56.2% (sportsbooks)'.
    With `now`, an old line says how old its prices are: '(sportsbook prices from 7 hr ago)'."""
    need = needed_chance(ln, view.settings, best if best is not None else best_structure(view.settings))
    needs = "" if need is None else f", needs {need:.1%}"
    where = f" - {matchup}" if matchup else ""
    return f"{pick_words(ln)} - our chance {ln.p:.1%}{needs} {_source(ln, gated, now)}{where}"


NO_TEAM = "team unknown, can't be picked yet"


NOT_RANKED = "Not ranked (no price or no team yet)"


def ranked_lines(view: PPView) -> list[PPLine]:
    """Every pickable line, best first: the most room between our chance and the chance it needs,
    lines with fresh prices before older ones (suggested plays use fresh lines only)."""
    best = best_structure(view.settings)

    def room(ln: PPLine) -> float:
        need = needed_chance(ln, view.settings, best)
        return (ln.p or 0.0) - (need or 0.0)

    return sorted(pickable(view).values(), key=lambda ln: (not ln.fresh, -room(ln), ln.player, ln.stat, ln.line))


def all_rows(view: PPView, chosen: set[PickKey], now: datetime, names: d.Names = d.DEFAULT_NAMES,
             plays: list[EntryEval] | None = None) -> list[PickRow]:
    """ALL LINES as rows: every pickable line ranked best first, each with its game and the suggested
    play it is in; then, under a heading, the lines we can't price or whose team we don't know."""
    best = best_structure(view.settings)
    rows: list[PickRow] = []
    for rank, ln in enumerate(ranked_lines(view), 1):
        g = view.games[ln.game_id]
        gated = view.model_gated.get(g.league, True)
        key = pick_key(ln)
        text = pick_row_text(ln, view, best, gated, _matchup(g, names), now)
        rows.append(PickRow(f"{_mark(key in chosen)}#{rank} {text} - {pick_amount_text(ln, plays or [])}", key,
                            key in chosen))
    rest = []
    for g, lines in _by_game(view):
        gated = view.model_gated.get(g.league, True)
        for ln in lines:
            if ln.p is None or ln.side is None:
                rest.append(PickRow(f"    {line_text(ln, gated, now)} - {_matchup(g, names)}"))
            elif ln.team is None:
                rest.append(PickRow(f"    {pick_row_text(ln, view, best, gated, _matchup(g, names), now)} - "
                                    f"{NO_TEAM}"))
    if rest:
        rows += [PickRow(NOT_RANKED, heading=True), *rest]
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
        why = ("no sportsbook prices this line and our model has no estimate for the player" if ln.team
               else "we don't know the player's team yet")
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
    if not ln.fresh and ln.p is not None:
        out.append(f"These prices are {age_words(ln.fetched_at, now)} old, past the "
                   f"{_hours(s.props_stale_seconds)} we trust for texted entries, so they may have moved. Type pp "
                   "refresh to load new ones.")
    out.append(f"[details] {ln.game_id} {ln.player}|{ln.stat}|{ln.line:g}|{ln.side} "
               f"p={'-' if ln.p is None else f'{ln.p:.4f}'} anchored={ln.anchored} fresh={ln.fresh}")
    return out
