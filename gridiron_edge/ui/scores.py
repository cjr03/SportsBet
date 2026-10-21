"""Scores tab: one row per game with the score or kickoff, sportsbook lines, Kalshi win prices, our win
chance and how many of your bets ride on it. Grouped by league and day. Pure (uses only loaded data)."""
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from gridiron_edge import describe as d
from gridiron_edge.types import Game, KalshiMarket, Quote

LEAGUE_ORDER = (("nfl", "NFL"), ("cfb", "College football"))
OPEN = ("active", "open", "")
CFB_EXTRA_LIMIT = 15  # college games with no Kalshi prices and no bets: show this many, then a count


@dataclass
class ScoreRow:
    """A heading (game None) or one game: the head line and the detail segments under it."""
    head: str
    game: Game | None = None
    state: str = ""  # 'LIVE ...', 'Final: ...', 'Sun 1:25 PM PT'
    details: list[str] = field(default_factory=list)
    bet_status: str = "none"  # winning / losing / none
    note: bool = False  # a plain line (e.g. how many games are not shown), not a heading


def _latest_per_book(quotes: Iterable[Quote]) -> list[Quote]:
    best: dict[tuple, Quote] = {}
    for q in quotes:
        key = (q.book, q.market, q.side)
        if key not in best or q.seen_at > best[key].seen_at:
            best[key] = q
    return list(best.values())


def book_lines(quotes: Iterable[Quote], g: Game, names: d.Names = d.DEFAULT_NAMES) -> str | None:
    """'Eagles by 3.5 · total 44.5' from the middle sportsbook line; None when no line is loaded."""
    qs = _latest_per_book(q for q in quotes if q.game_id == g.game_id and q.point is not None)
    home = [q.point for q in qs if q.market == "spreads" and q.side == "home"]
    home += [-q.point for q in qs if q.market == "spreads" and q.side == "away"]
    totals = [q.point for q in qs if q.market == "totals"]
    parts = []
    if home:
        pt = median(home)  # home handicap: negative means the home team is favored
        if abs(pt) < 1e-9:
            parts.append("even game (no favorite)")
        else:
            fav = g.home if pt < 0 else g.away
            parts.append(f"{names.team(g.league, fav)} by {abs(pt):g}")
    if totals:
        parts.append(f"total {median(totals):g} points")
    return " · ".join(parts) if parts else None


def kalshi_prices(markets: Iterable[KalshiMarket], g: Game) -> dict[str, int]:
    """Team code -> Kalshi's price to buy that team to win, in cents (open markets with a seller)."""
    out = {}
    for m in markets:
        if (m.game_id == g.game_id and m.market_type == "ml" and m.team in (g.home, g.away)
                and m.status in OPEN and 0 < m.yes_ask < 1):
            out[m.team] = round(m.yes_ask * 100)
    return out


def kalshi_text(prices: dict[str, int], g: Game, names: d.Names = d.DEFAULT_NAMES) -> str | None:
    """'Kalshi win price: Cowboys 40 cents, Eagles 62 cents' (away first, as in the matchup)."""
    parts = [f"{names.team(g.league, t)} {_cents(prices[t])}" for t in (g.away, g.home) if t in prices]
    return f"Kalshi win price: {', '.join(parts)}" if parts else None


def _cents(c: int) -> str:
    return f"{c} cent{'s' * (c != 1)}"


def chance_text(chance: tuple[str, float] | None, g: Game, names: d.Names = d.DEFAULT_NAMES) -> str | None:
    """'our win chance: Eagles 61%' for the team we think is likelier to win."""
    if chance is None:
        return None
    team, p = chance
    if p < 0.5:
        other = g.away if team == g.home else g.home
        team, p = other, 1 - p
    return f"our win chance: {names.team(g.league, team)} {d.pct(p)}"


def state_text(g: Game, now: datetime, names: d.Names = d.DEFAULT_NAMES) -> str:
    """Live score with quarter and time left, final score, or the kickoff day and time."""
    away, home = names.team(g.league, g.away), names.team(g.league, g.home)
    has_score = g.home_pts is not None and g.away_pts is not None
    score = f"{away} {g.away_pts}, {home} {g.home_pts}" if has_score else ""
    if g.status == "live":
        when = d.period_text(g.period, g.clock) if g.period else "in progress"
        return f"LIVE {score} · {when}" if score else f"LIVE · {when}"
    if g.status == "final":
        return f"Final: {score}" if score else "Final"
    if g.status != "scheduled":
        return g.status.replace("_", " ").capitalize()
    kick = g.kickoff.astimezone(d.PT)
    return f"kickoff {kick:%a} {d.clock(g.kickoff)} PT"


def day_heading(t: datetime, now: datetime) -> str:
    """'Today', 'Tomorrow', 'Later', or 'Earlier' by Pacific calendar day."""
    day, today = t.astimezone(d.PT).date(), now.astimezone(d.PT).date()
    if day < today:
        return "Earlier"
    if day == today:
        return "Today"
    return "Tomorrow" if day == today + timedelta(days=1) else "Later"


_DAY_ORDER = {"Earlier": 0, "Today": 1, "Tomorrow": 2, "Later": 3}
_STATUS_ORDER = {"live": 0, "scheduled": 1}  # then finished, postponed and canceled games


def _bets_text(n: int) -> str | None:
    return f"your bets: {n}" if n else None


_STATUS_WORDS = {"winning": "your bet is winning right now", "losing": "your bet is losing right now"}


def score_rows(games: Iterable[Game], now: datetime, *, quotes: Iterable[Quote] = (),
               markets: Iterable[KalshiMarket] = (), chances: dict[str, tuple[str, float]] | None = None,
               bet_counts: dict[str, int] | None = None, bet_status: dict[str, str] | None = None,
               names: d.Names = d.DEFAULT_NAMES, cfb_extra_limit: int = CFB_EXTRA_LIMIT) -> list[ScoreRow]:
    """Headings and game rows: NFL then college; Earlier / Today / Tomorrow / Later under each league.
    Within a day, live games first, then upcoming games (those with Kalshi prices or your bets first), then
    finished ones; each by kickoff.
    College games with neither Kalshi prices nor bets are capped; a last row says how many were left out."""
    quotes, markets = list(quotes), list(markets)
    chances, bet_counts, bet_status = chances or {}, bet_counts or {}, bet_status or {}
    out: list[ScoreRow] = []
    for league, label in LEAGUE_ORDER:
        gs = [g for g in games if g.league == league]
        if not gs:
            continue
        info = {g.game_id: kalshi_prices(markets, g) for g in gs}

        def important(g: Game) -> bool:
            return bool(info[g.game_id]) or bet_counts.get(g.game_id, 0) > 0

        hidden = 0
        if league == "cfb":
            extra = sorted((g for g in gs if not important(g) and g.status != "live"), key=lambda g: g.kickoff)
            drop = {g.game_id for g in extra[cfb_extra_limit:]}
            hidden = len(drop)
            gs = [g for g in gs if g.game_id not in drop]
        gs.sort(key=lambda g: (_DAY_ORDER[day_heading(g.kickoff, now)], _STATUS_ORDER.get(g.status, 2),
                               not important(g), g.kickoff, g.game_id))
        day = None
        for g in gs:
            h = day_heading(g.kickoff, now)
            if h != day:
                day = h
                n = sum(day_heading(x.kickoff, now) == h for x in gs)
                out.append(ScoreRow(f"{label} · {h} ({n} game{'s' * (n != 1)})"))
            matchup = f"{names.team(g.league, g.away)} at {names.team(g.league, g.home)}"
            status = bet_status.get(g.game_id, "none")
            details = [x for x in (book_lines(quotes, g, names), kalshi_text(info[g.game_id], g, names),
                                   chance_text(chances.get(g.game_id), g, names),
                                   _bets_text(bet_counts.get(g.game_id, 0)), _STATUS_WORDS.get(status)) if x]
            out.append(ScoreRow(matchup, g, state_text(g, now, names), details, status))
        if hidden:
            out.append(ScoreRow(f"{hidden} more college game{'s' * (hidden != 1)} with no Kalshi prices and "
                                "none of your bets (not shown)", note=True))
    return out


def bet_counts(open_bets: Iterable, legs_of) -> dict[str, int]:
    """Game id -> how many open bets have a leg on it (a bet counts once per game)."""
    out: dict[str, int] = {}
    for b in open_bets:
        for gid in {leg.get("game_id") for leg in legs_of(b) if leg.get("game_id")}:
            out[gid] = out.get(gid, 0) + 1
    return out
