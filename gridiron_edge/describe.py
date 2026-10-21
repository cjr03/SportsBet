"""Plain-English wording for legs, recommendations and bets; shared by the dashboard and texts. Pure."""
import json
import math
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gridiron_edge.strategy.kalshi_pricing import taker_fee
from gridiron_edge.types import Leg, Recommendation

PT = ZoneInfo("America/Los_Angeles")
_NFL = {code: info["names"] for code, info in
        json.loads((Path(__file__).parent / "data" / "nfl_teams.json").read_text()).items()}
STAT_WORDS = {
    "pass_yds": "passing yards", "rush_yds": "rushing yards", "reception_yds": "receiving yards",
    "rec_yds": "receiving yards", "pass_tds": "passing touchdowns", "rush_tds": "rushing touchdowns",
    "reception_tds": "receiving touchdowns", "rec_tds": "receiving touchdowns", "receptions": "catches",
    "pass_completions": "completions", "pass_attempts": "pass attempts", "rush_attempts": "carries",
    "pass_interceptions": "interceptions thrown", "rush_reception_yds": "rushing + receiving yards",
    "pass_rush_yds": "passing + rushing yards", "anytime_td": "touchdowns",
}
PP_STRUCTURES = {"power": "Power Play", "flex": "Flex Play"}


class Names:
    """Team display names: NFL from nfl_teams.json, others (CFB schools) from `extra`."""

    def __init__(self, extra: dict[tuple[str, str], str] | Callable[[], dict] | None = None):
        # a callable (e.g. TeamResolver.display_names) is read on each lookup, so names seeded
        # after startup show up without rebuilding
        self._extra = extra if callable(extra) else dict(extra or {})

    @property
    def extra(self) -> dict[tuple[str, str], str]:
        return self._extra() if callable(self._extra) else self._extra

    def team(self, league: str | None, code: str | None, full: bool = False) -> str:
        if not code:
            return "?"
        if (league or "nfl") == "nfl" and code in _NFL:
            names = _NFL[code]
            return names[0] if full or len(names) < 2 else names[1]
        return self.extra.get((league or "", code), code)


DEFAULT_NAMES = Names()


def team_name(league: str | None, code: str | None, full: bool = False) -> str:
    return DEFAULT_NAMES.team(league, code, full)


def parse_game_id(game_id: str | None) -> tuple[str | None, str | None, str | None]:
    """'nfl:20261025:DAL@PHI' -> (league, away, home); Nones if it doesn't parse."""
    parts = (game_id or "").split(":")
    if len(parts) != 3 or "@" not in parts[2]:
        return None, None, None
    away, _, home = parts[2].partition("@")
    return parts[0], away, home


# -- numbers and times ----------------------------------------------------------
def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def pct(p: float) -> str:
    return f"{p:.0%}"


def clock(t: datetime, tz: ZoneInfo = PT) -> str:
    """12-hour local time without a leading zero, e.g. '1:05 PM'."""
    return f"{t.astimezone(tz):%I:%M %p}".lstrip("0")


def when(t: datetime, now: datetime, tz: ZoneInfo = PT) -> str:
    """Clock time, prefixed with the weekday unless it is today."""
    same_day = t.astimezone(tz).date() == now.astimezone(tz).date()
    return clock(t, tz) if same_day else f"{t.astimezone(tz):%a} {clock(t, tz)}"


def countdown(deadline: datetime, now: datetime) -> str:
    secs = (deadline - now).total_seconds()
    if secs <= 0:
        return "expired"
    mins = int(secs // 60)
    if mins < 1:
        return "under 1 min left"
    if mins < 60:
        return f"{mins} min left"
    h, m = divmod(mins, 60)
    if h >= 24:
        return f"{h // 24} day{'s' if h >= 48 else ''} left"
    return f"{h} hr {m} min left" if m else f"{h} hr left"


def _num(x: float) -> str:
    return f"{x:g}"


def _at_least(line: float) -> int:
    """Smallest whole number strictly over `line` ('over 3.5' and 'over 3' both mean 4+ / 4+)."""
    return math.floor(line) + 1


def stat_words(stat: str | None) -> str:
    return STAT_WORDS.get(stat or "", (stat or "").replace("_", " "))


# -- legs -------------------------------------------------------------------------
def _market_type(leg: Leg) -> str:
    t = (leg.ticker or "").upper()
    if leg.player:
        return "prop"
    if leg.line is None:
        return "ml"
    if "TOTAL" in t or (leg.team is None and leg.line is not None):
        return "total"
    if "SPREAD" in t or leg.line is not None:
        return "spread"
    return "ml"


def leg_text(leg: Leg, names: Names = DEFAULT_NAMES) -> str:
    """One leg as an outcome in words, e.g. 'Eagles win by 4 or more points vs Cowboys'."""
    if leg.venue == "prizepicks":
        more = "MORE" if leg.side == "over" else "LESS"
        line = "?" if leg.line is None else _num(leg.line)
        return f"{leg.player} {more} than {line} {stat_words(leg.stat)}"
    league, away, home = parse_game_id(leg.game_id)
    yes = leg.side != "no"
    kind = _market_type(leg)
    if kind == "prop":
        if leg.line is None:
            return f"{leg.player} {stat_words(leg.stat)}"
        n = _at_least(leg.line)
        return f"{leg.player} {n} or more {stat_words(leg.stat)}" if yes else \
            f"{leg.player} {n - 1} or fewer {stat_words(leg.stat)}"
    if kind == "total":
        game = f"{names.team(league, away)} vs {names.team(league, home)}" if home else "Game"
        return f"{game} total {'over' if yes else 'under'} {_num(leg.line)} points"
    team = names.team(league, leg.team)
    opp_code = away if leg.team == home else home if leg.team == away else None
    vs = f" vs {names.team(league, opp_code)}" if opp_code else ""
    if kind == "spread":
        cond = f"win by {_at_least(leg.line)} or more points"
        return f"{team} {cond if yes else 'do NOT ' + cond}{vs}"
    if opp_code:
        return f"{team} beat {names.team(league, opp_code)}" if yes else \
            f"{team} do NOT beat {names.team(league, opp_code)}"
    return f"{team} win" if yes else f"{team} do NOT win"


def structure_text(structure: str | None) -> str:
    n, _, kind = (structure or "").partition("-")
    return f"{n}-pick {PP_STRUCTURES.get(kind, kind.title())}".strip()


def bet_text(rec: Recommendation, names: Names = DEFAULT_NAMES) -> str:
    """The whole bet in words (legs joined; PrizePicks prefixed with its play type)."""
    if rec.kind == "pp_entry":
        return f"{structure_text(rec.structure)}: " + "; ".join(leg_text(g, names) for g in rec.legs)
    return " AND ".join(leg_text(g, names) for g in rec.legs)


def bet_row_text(row, names: Names = DEFAULT_NAMES) -> str:
    """A bets-table row (dict or sqlite Row) in words."""
    legs = [Leg(**g) for g in json.loads(row["legs_json"])]
    kind = row["kind"]
    if kind == "pp_entry":
        return f"{structure_text(row['structure'])}: " + "; ".join(leg_text(g, names) for g in legs)
    return " AND ".join(leg_text(g, names) for g in legs)


def venue(rec_kind: str) -> str:
    return "PrizePicks" if rec_kind == "pp_entry" else "Kalshi"


# -- money math -----------------------------------------------------------------------
def fee_cents(rec: Recommendation) -> int:
    """Kalshi taker fees the confirm will debit (0 for PrizePicks)."""
    if rec.kind == "pp_entry" or not rec.qty:
        return 0
    return sum(round(taker_fee(rec.qty, g.price) * 100) for g in rec.legs)


def cost_cents(rec: Recommendation) -> int:
    """Contract cost before fees."""
    if rec.kind == "pp_entry" or not rec.qty:
        return rec.stake_cents
    return sum(round(rec.qty * g.price * 100) for g in rec.legs)


def expected_profit_cents(rec: Recommendation) -> int:
    if rec.kind == "arb" and rec.qty:
        return rec.qty * 100 - cost_cents(rec) - fee_cents(rec)
    if rec.kind in ("kalshi_game", "kalshi_prop") and rec.qty:
        return round(rec.qty * rec.legs[0].p_fair * 100) - cost_cents(rec) - fee_cents(rec)
    return round(rec.ev * rec.stake_cents)


def price_text(rec: Recommendation) -> str:
    if rec.kind == "pp_entry":
        return "entry"
    return " + ".join(f"{round(g.price * 100)}c" for g in rec.legs)


# -- the "Why this bet?" panel ----------------------------------------------------
def why_lines(rec: Recommendation, now: datetime, names: Names = DEFAULT_NAMES,
              kelly_fraction: float | None = None, cap_bet: float | None = None) -> list[str]:
    """Plain-English reasons; only the last line carries internal identifiers."""
    why = getattr(rec, "why", None) or {}
    out = [f"The bet: {bet_text(rec, names)}."]
    if rec.kind == "arb":
        out.append(f"Buying every side costs {money(cost_cents(rec))} plus {money(fee_cents(rec))} "
                   f"in fees and pays {money((rec.qty or 0) * 100)} whichever way it goes, "
                   f"so the profit is locked: about {money(expected_profit_cents(rec))}.")
        if rec.tie_risk:
            out.append("Small catch: if the game ends in a tie, neither side pays.")
    elif rec.kind == "pp_entry":
        legs = ", ".join(f"{leg.player} {pct(leg.p_fair)}" for leg in rec.legs)
        out.append(f"Our chance each pick hits: {legs}.")
        out.append(f"Chance the entry pays more than you put in: {pct(rec.p_win)}.")
    else:
        leg = rec.legs[0]
        out.append(f"Our chance: {pct(leg.p_fair)}. The price implies {pct(leg.price)}.")
    out.append("Where our number came from: " + why.get("source", _default_source(rec)) + ".")
    if rec.kind != "pp_entry" and rec.kind != "arb":
        out.append(f"Kalshi fee: {money(fee_cents(rec))} (already counted in the profit).")
    out.append("How much to bet: " + why.get("sizing", _default_sizing(rec, kelly_fraction, cap_bet)) + ".")
    out.append(f"Expected profit: about {money(expected_profit_cents(rec))}.")
    out.append(f"Goes stale at {when(rec.expires_at, now)} PT ({countdown(rec.expires_at, now)}), "
               "or sooner if the price moves.")
    extra = [f"{k}={v}" for k, v in why.items() if k not in ("source", "sizing")]
    tickers = ",".join(g.ticker for g in rec.legs if g.ticker)
    fair = "/".join(f"{g.p_fair:.3f}" for g in rec.legs)
    out.append(f"[details] id {rec.id} {rec.kind} {tickers} EV {rec.ev:+.1%} fair {fair} "
               f"qty {rec.qty} {' '.join(extra)}".rstrip())
    return out


def _default_source(rec: Recommendation) -> str:
    if rec.kind == "arb":
        return "the prices themselves; no prediction is needed"
    if rec.kind == "kalshi_game":
        return "sportsbook lines with the bookmakers' cut removed, blended with our team ratings"
    return "sportsbook player lines with the bookmakers' cut removed, or our player projection"


def _default_sizing(rec: Recommendation, kelly_fraction: float | None, cap_bet: float | None) -> str:
    if rec.kind == "arb":
        return "as many contracts as are on offer, up to the share of bankroll set aside for locked profits"
    share = f"{kelly_fraction:.0%} of " if kelly_fraction else "a fraction of "
    cap = f", never more than {cap_bet:.0%} of bankroll on one bet" if cap_bet else ""
    return f"{share}the amount that grows the bankroll fastest at these odds{cap}"
