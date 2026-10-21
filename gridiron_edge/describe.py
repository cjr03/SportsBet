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


_ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}


def period_text(period: str | None, game_clock: str | None, short: bool = False) -> str:
    """A live game's period in words: '3rd quarter, 12:34 left' ('3rd qtr 12:34' when short),
    'overtime' for period 5 and up, 'halftime'. period is TheRundown's number (or e.g. 'Q2')."""
    raw = (period or "").strip()
    if "half" in raw.lower():
        return "halftime"
    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return raw
    n, left = int(digits), (game_clock or "").strip()
    name = _ORDINALS.get(n, "overtime") if n >= 1 else raw
    if short:
        name = name if n >= 5 else f"{name} qtr"
        return f"{name} {left}".strip()
    if n < 5:
        name += " quarter"
    return f"{name}, {left} left" if left else name


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


_STAT_PARTS = {"yds": "yards", "tds": "touchdowns", "td": "touchdown", "rec": "receiving", "reception": "receiving",
               "pass": "passing", "rush": "rushing", "int": "interceptions", "ints": "interceptions", "pts": "points"}


_KINDS = {"passing", "rushing", "receiving"}


def stat_words(stat: str | None) -> str:
    """'pass_yds' -> 'passing yards'; unknown keys (e.g. 'player_kicking_pts') spelled out word by word."""
    key = (stat or "").removeprefix("player_")
    if key in STAT_WORDS:
        return STAT_WORDS[key]
    words: list[str] = []
    for w in (w for w in key.split("_") if w):
        part = _STAT_PARTS.get(w, w)
        if part in _KINDS and words and words[-1].rsplit(" + ", 1)[-1] in _KINDS:
            words[-1] += f" + {part}"  # a combined stat: 'passing + rushing + receiving yards'
        else:
            words.append(part)
    return " ".join(words)


def margin_text(team: str, line: float, yes: bool = True) -> str:
    """'<team> wins by more than <line>' in words, for any line (zero and negative lines included).

    YES pays when the margin is at least k = floor(line) + 1; NO when it is k - 1 or less.
    """
    k = math.floor(line) + 1
    if yes:
        if k >= 1:
            return f"{team} win by {k} or more points"
        return f"{team} win or tie" if k == 0 else f"{team} win, tie, or lose by {-k} or fewer points"
    if k >= 1:
        return f"{team} do NOT win by {k} or more points"
    return f"{team} lose" if k == 0 else f"{team} lose by {1 - k} or more points"


# -- legs -------------------------------------------------------------------------
def market_type(leg: Leg) -> str:
    """ml | spread | total | prop, from the leg itself."""
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
    kind = market_type(leg)
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
        return margin_text(team, leg.line, yes) + vs
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
    return sum(round(taker_fee(rec.qty, g.price, g.fee_multiplier) * 100) for g in rec.legs)


def cost_cents(rec: Recommendation) -> int:
    """Contract cost before fees."""
    if rec.kind == "pp_entry" or not rec.qty:
        return rec.stake_cents
    return sum(round(rec.qty * g.price * 100) for g in rec.legs)


def expected_profit_cents(rec: Recommendation) -> int:
    if rec.kind == "arb" and rec.qty:
        return rec.qty * 100 - cost_cents(rec) - fee_cents(rec)
    if rec.kind == "middle" and rec.qty:  # $1 back per pair, $2 when the game lands in the middle
        return round(rec.qty * (1.0 + rec.p_win) * 100) - cost_cents(rec) - fee_cents(rec)
    if rec.kind in ("kalshi_game", "kalshi_prop") and rec.qty:
        return round(rec.qty * rec.legs[0].p_fair * 100) - cost_cents(rec) - fee_cents(rec)
    return round(rec.ev * rec.stake_cents)


def price_text(rec: Recommendation) -> str:
    if rec.kind == "pp_entry":
        return "PrizePicks entry"
    return " + ".join(f"{round(g.price * 100)} cents" for g in rec.legs)


# -- the "Why this bet?" panel ----------------------------------------------------
BOOK_NAMES = {
    "pinnacle": "Pinnacle", "betfair_ex_eu": "Betfair", "betfair_ex_uk": "Betfair", "matchbook": "Matchbook",
    "novig": "Novig", "prophetx": "ProphetX", "lowvig": "LowVig", "betonlineag": "BetOnline",
    "bookmaker": "BookMaker", "draftkings": "DraftKings", "fanduel": "FanDuel", "betmgm": "BetMGM",
    "williamhill_us": "Caesars", "caesars": "Caesars", "betrivers": "BetRivers", "espnbet": "ESPN BET",
    "hardrockbet": "Hard Rock Bet", "fanatics": "Fanatics", "bovada": "Bovada", "mybookieag": "MyBookie",
    "kalshi": "Kalshi", "polymarket": "Polymarket", "prizepicks": "PrizePicks",
}
_SOURCE_WORDS = {
    "pinnacle": "Pinnacle, the sharpest sportsbook",
    "exchanges": "betting exchanges, where bettors set the price",
    "mid": "low-margin sportsbooks",
    "median": "the middle of the regular sportsbooks' prices (no sharp sportsbook had this game)",
}
_METHOD_WORDS = {
    "exact line": "taken straight from the same line",
    "adjacent line": "adjusted from a line half a point away",
    "implied mean": "worked out from the sportsbooks' point spread or total",
    "h2h": "from the sportsbooks' win/lose prices",
}


def book_name(key: str) -> str:
    return BOOK_NAMES.get(key, key.replace("_", " ").title())


def source_text(why: dict) -> str | None:
    """Where our number came from, in words, from a rec's why dict (fair-pricing fields)."""
    src = why.get("fair_source")
    if not src:
        return why.get("source")
    anchored = src.startswith("anchored-")
    base = _SOURCE_WORDS.get(src.removeprefix("anchored-"), src)
    text = (f"the last price from {base}, moved by how much the other sportsbooks have moved since"
            if anchored else base)
    books = [b for b in dict.fromkeys(book_name(b) for b in why.get("books") or []) if b not in text]
    if books:
        text += f" ({', '.join(dict.fromkeys(books))})"
    method = _METHOD_WORDS.get(why.get("method") or "")
    if method:
        text += f", {method}"
    return text


def model_share_text(why: dict) -> str | None:
    """'Sportsbooks say 54%; our team ratings say 57% and count for 30% of our number.'"""
    p_mkt, p_model, w = why.get("p_market"), why.get("p_model"), why.get("model_weight")
    if p_mkt is None:
        return None
    if not w or p_model is None:
        return f"Sportsbooks say {pct(p_mkt)}; our number is theirs (our team ratings are not used for this one)"
    return (f"Sportsbooks say {pct(p_mkt)}; our team ratings say {pct(p_model)} and count for "
            f"{w * 100:.0f}% of our number")


def kalshi_mid_text(why: dict, side: str = "yes") -> str | None:
    """'Kalshi traders say 25%; our own estimate (30%) counts for 50% of our number, theirs for the rest.'"""
    mid, ours, w = why.get("kalshi_mid"), why.get("p_ours"), why.get("weight")
    if mid is None or ours is None or w is None:
        return None
    theirs = mid if side == "yes" else 1 - mid
    return (f"Kalshi traders say {pct(theirs)}; our own estimate ({pct(ours)}) counts for {w * 100:.0f}% "
            "of our number and theirs for the rest")


def _tie_text(why: dict) -> str | None:
    tie = why.get("tie_prob")
    if not tie:
        return None
    return f"Allows for a {tie * 100:.1f}% chance of a tie, which this bet would lose"


def why_lines(rec: Recommendation, now: datetime, names: Names = DEFAULT_NAMES,
              kelly_fraction: float | None = None, cap_bet: float | None = None,
              settings=None) -> list[str]:
    """Plain-English reasons; only the last line carries internal identifiers."""
    why = getattr(rec, "why", None) or {}
    out = [f"The bet: {bet_text(rec, names)}."]
    if rec.kind == "arb":
        out.append(f"Buying every side costs {money(cost_cents(rec))} plus {money(fee_cents(rec))} "
                   f"in fees and pays {money((rec.qty or 0) * 100)} whichever way it goes, "
                   f"so the profit is locked: about {money(expected_profit_cents(rec))}.")
        if rec.tie_risk:
            out.append("Small catch: if the game ends in a tie, neither side pays.")
    elif rec.kind == "middle":
        out.append(f"Not risk-free: both sides cost {money(cost_cents(rec))} plus {money(fee_cents(rec))} in fees. "
                   f"You get {money((rec.qty or 0) * 100)} back, or {money(2 * (rec.qty or 0) * 100)} "
                   f"if the game lands in the middle (about {pct(rec.p_win)} chance).")
    elif rec.kind == "pp_entry":
        legs = ", ".join(f"{leg.player} {pct(leg.p_fair)}" for leg in rec.legs)
        out.append(f"Our chance each pick hits: {legs}.")
        out.append(f"Chance the entry pays more than you put in: {pct(rec.p_win)}.")
    else:
        leg = rec.legs[0]
        out.append(f"Our chance: {pct(leg.p_fair)}. The price implies {pct(leg.price)}.")
    out.append("Where our number came from: " + (source_text(why) or _default_source(rec)) + ".")
    for extra_line in (model_share_text(why), kalshi_mid_text(why, rec.legs[0].side if rec.legs else "yes"),
                       _tie_text(why)):
        if extra_line:
            out.append(extra_line + ".")
    if rec.kind not in ("pp_entry", "arb", "middle"):
        out.append(f"Kalshi fee: {money(fee_cents(rec))} (already counted in the profit).")
    out.append("How much to bet: " + why.get("sizing", _default_sizing(rec, kelly_fraction, cap_bet, settings))
               + ".")
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
    if rec.kind in ("arb", "middle"):
        return "the prices themselves; no prediction is needed"
    if rec.kind == "kalshi_game":
        return "sportsbook lines with the bookmakers' cut removed, blended with our team ratings"
    return "sportsbook player lines with the bookmakers' cut removed, or our player projection"


def _default_sizing(rec: Recommendation, kelly_fraction: float | None, cap_bet: float | None,
                    settings=None) -> str:
    if rec.kind == "arb":
        return "as many contracts as are on offer, up to the share of bankroll set aside for locked profits"
    if rec.kind == "middle":
        return "sized like a bet that can lose part of its cost, within the per-bet limit"
    share = f"{kelly_fraction:.0%} of " if kelly_fraction else "a fraction of "
    cap = f", never more than {cap_bet:.0%} of bankroll on one bet" if cap_bet else ""
    text = f"{share}the amount that grows the bankroll fastest at these odds{cap}"
    if settings is not None:
        text += (f", at most {settings.cap_game:.0%} of bankroll on one game and "
                 f"{settings.cap_day:.0%} placed per day")
        if rec.kind == "kalshi_prop" or rec.kind == "pp_entry":
            text += ("; player bets are cut to "
                     f"{settings.shrink_prop_anchored:.0%} (sportsbook-priced) or "
                     f"{settings.shrink_prop_model:.0%} (our projection only) because their numbers are less certain")
        if rec.kind == "pp_entry" and not settings.pp_payouts_verified:
            text += (f", and cut again to {settings.pp_unverified_mult:.0%} until you confirm the PrizePicks "
                     "payouts (type: set pp_payouts_verified true)")
    return text
