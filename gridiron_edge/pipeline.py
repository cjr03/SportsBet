"""Recommendation pipeline: turn a market snapshot into Kalshi, PrizePicks and arb recommendations."""
import inspect
import math
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal, NamedTuple, Protocol
from zoneinfo import ZoneInfo

import numpy as np

from gridiron_edge.clients.kalshi import series_type
from gridiron_edge.config import Settings
from gridiron_edge.matching import normalize_name
from gridiron_edge.models import Prediction
from gridiron_edge.models.blend import blend, default_weight
from gridiron_edge.strategy.arb import (
    Arb,
    contract_text,
    filter_arbs,
    kalshi_cross_spreads,
    kalshi_ladders,
    kalshi_middles,
    kalshi_ml_no_pairs,
    kalshi_ml_pairs,
    ml_tie_risk,
    payoff_range,
)
from gridiron_edge.strategy.devig import devig_worst
from gridiron_edge.strategy.distributions import (
    EmpiricalMarginModel,
    MarginModel,
    implied_mean,
    implied_mean_from_line,
    prop_p_over,
)
from gridiron_edge.strategy.fair import (
    TIER_RANK,
    FairPrice,
    fair_price,
    fair_spread_mean,
    fair_total_mean,
    implied_market_mean,
)
from gridiron_edge.strategy.kalshi_pricing import effective_price, kalshi_ev, passes_filters, taker_fee
from gridiron_edge.strategy.kelly import Bet, optimize_slate, to_cents
from gridiron_edge.strategy.prizepicks import (
    Pick,
    allowed_structures,
    breakeven,
    build_best_entry,
    payout_table,
    robust_kelly,
    worst_distribution,
)
from gridiron_edge.types import Game, KalshiMarket, Leg, PropQuote, Quote, Recommendation

_GAME_TYPES = ("ml", "spread", "total")
_PT = ZoneInfo("America/Los_Angeles")
_HEDGED = ("arb", "middle")  # capital tied up, but not directional risk on the game
# league -> market type -> margin_models key
_MODEL_KEY = {
    "nfl": {"ml": "nfl_ml", "spread": "nfl_spread", "total": "nfl_total"},
    "cfb": {"ml": "cfb", "spread": "cfb_spread", "total": "cfb"},
}
_MODEL_FALLBACK = {"cfb_spread": "cfb"}  # no CFB lines history cached: the normal model
_TIE_NO = re.compile(r"\btie\b[^.]*\b(resolves?|settles?)\b[^.]*\bno\b")


@dataclass
class Snapshot:
    now: datetime
    games: dict[str, Game]
    quotes: list[Quote]
    props: list[PropQuote]
    kalshi: list[KalshiMarket]
    predictions: dict[str, Prediction]
    gates: dict[tuple[str, str], bool]  # (league, ml|spread|total|prop) -> observe-only
    bankroll_cents: int
    open_exposure_cents: int
    pp_staked_today_cents: int
    margin_models: dict[str, MarginModel]
    player_teams: dict[str, str] = field(default_factory=dict)
    # open positions: games with a PLACED Kalshi game bet, players (lower-cased) with a PLACED
    # Kalshi prop bet, and leg keys of PLACED recs - none of these is recommended again
    open_game_ids: set[str] = field(default_factory=set)
    open_players: set[str] = field(default_factory=set)
    open_leg_keys: set[str] = field(default_factory=set)
    # walk-forward blend fits: (league, ml|spread|total) -> (b_model, se); weight = clip(b - se, 0, default)
    blend_fits: dict[tuple[str, str], tuple[float, float]] = field(default_factory=dict)
    # league -> 95th percentile of |model - closing line| in the backtest (pts)
    disagree_caps: dict[str, float] = field(default_factory=dict)

    # projector-only props may stake only once the projector's backtest shows an edge
    projector_live: bool = False
    # cumulative open risk in cents (PLACED bets, arbs and middles excluded): per game, per slate
    # window (PT kickoff date), per same-direction key; stakes placed today (PT); players and
    # games in open PrizePicks entries; Kelly multiplier per league from rolling CLV
    open_game_exposure: dict[str, int] = field(default_factory=dict)
    open_window_exposure: dict[str, int] = field(default_factory=dict)
    open_dir_exposure: dict[str, int] = field(default_factory=dict)
    placed_today_cents: int = 0
    pp_open_players: set[str] = field(default_factory=set)
    pp_open_games: set[str] = field(default_factory=set)
    clv_mult: dict[str, float] = field(default_factory=dict)
    # (league, market) paused by the CLV throttle or drift (also merged into gates); no setting lifts it
    paused: dict[tuple[str, str], bool] = field(default_factory=dict)


def slate_window(g: Game) -> str:
    """A game's slate: its kickoff date in Pacific time."""
    return f"{g.kickoff.astimezone(_PT):%Y-%m-%d}"


def _leg_type(leg: Leg) -> str:
    """The leg's market type; a known spread/total series wins over guessing from team/line, so a
    spread leg with no team is never read as a total."""
    kind = series_type(leg.ticker)
    if kind in ("spread", "total"):
        return kind
    return "prop" if leg.player else "ml" if leg.line is None else "total" if leg.team is None else "spread"


def direction_key(leg: Leg, g: Game | None) -> str | None:
    """Which way a Kalshi leg leans on its game: "<game>|home"/"away" for moneylines, spreads and
    props on a known team's player (over = that team), "<game>|over"/"under" for totals."""
    if g is None or leg.game_id != g.game_id:
        return None
    up = leg.side in ("yes", "over")
    if _leg_type(leg) == "total":
        return f"{g.game_id}|{'over' if up else 'under'}"
    if leg.team not in (g.home, g.away):
        return None
    return f"{g.game_id}|{'home' if (leg.team == g.home) == up else 'away'}"


@dataclass
class Exposure:
    game: dict[str, int] = field(default_factory=dict)
    window: dict[str, int] = field(default_factory=dict)
    direction: dict[str, int] = field(default_factory=dict)


def open_exposure(positions: Iterable[tuple[str, int, list[Leg]]], games: Mapping[str, Game]) -> Exposure:
    """Open directional risk from (kind, stake_cents, legs) of PLACED bets. A PrizePicks entry
    counts its full stake on each of its games; arbs and middles are hedged and skipped."""
    out = Exposure(defaultdict(int), defaultdict(int), defaultdict(int))
    for kind, stake, legs in positions:
        if kind in _HEDGED or stake <= 0:
            continue
        gids = {leg.game_id for leg in legs if leg.game_id}
        for gid in gids:
            out.game[gid] += stake
        for w in {slate_window(games[gid]) for gid in gids if gid in games}:
            out.window[w] += stake
        if kind in ("kalshi_game", "kalshi_prop"):
            for key in {direction_key(leg, games.get(leg.game_id or "")) for leg in legs} - {None}:
                out.direction[key] += stake
    return Exposure(dict(out.game), dict(out.window), dict(out.direction))


@dataclass
class Budget:
    """Cap headroom for one scan, in cents. Caps are fractions of the Kelly bankroll (cash + open
    stakes); open bets and recs sized earlier in the scan use it up. Stakes are sized on
    `available` (cash), so headroom is passed to the optimizer as a fraction of that."""
    bankroll: int
    available: int
    s: Settings
    snap: "Snapshot"
    used: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @classmethod
    def from_snap(cls, snap: "Snapshot", s: Settings) -> "Budget":
        return cls(snap.bankroll_cents, max(0, snap.bankroll_cents - snap.open_exposure_cents), s, snap)

    def _left(self, cap: float, open_cents: int, key: str) -> int:
        return max(0, math.floor(cap * self.bankroll) - open_cents - self.used[key])

    def game_left(self, gid: str) -> int:
        return self._left(self.s.cap_game, self.snap.open_game_exposure.get(gid, 0), "g:" + gid)

    def window_left(self, w: str) -> int:
        return self._left(self.s.cap_slate, self.snap.open_window_exposure.get(w, 0), "w:" + w)

    def dir_left(self, key: str) -> int:
        return self._left(self.s.cap_same_dir, self.snap.open_dir_exposure.get(key, 0), "d:" + key)

    def day_left(self) -> int:
        return self._left(self.s.cap_day, self.snap.placed_today_cents, "day")

    def charge(self, stake: int, gids: Iterable[str], dirs: Iterable[str | None] = ()) -> None:
        gids = set(gids)
        self.used["day"] += stake
        for gid in gids:
            self.used["g:" + gid] += stake
        for w in {slate_window(self.snap.games[g]) for g in gids if g in self.snap.games}:
            self.used["w:" + w] += stake
        for key in set(dirs) - {None}:
            self.used["d:" + key] += stake

    def cap_for(self, gids: Iterable[str]) -> int:
        """Most a new multi-game position (a PrizePicks entry) can stake under every cap."""
        gids = set(gids)
        caps = [self.day_left(), *(self.game_left(g) for g in gids),
                *(self.window_left(slate_window(self.snap.games[g])) for g in gids if g in self.snap.games)]
        return min(caps)


class Projector(Protocol):
    def project(self, player: str, stat: str, season: int, asof_week: int) -> tuple[float, float] | None: ...
    def team_of(self, player: str) -> str | None: ...


class _Side(NamedTuple):
    side: str
    price: float
    p: float
    qty: int
    ev: float


def _upcoming(snap: Snapshot, game_id: str | None) -> Game | None:
    """The game if it is known, scheduled and not yet kicked off."""
    g = snap.games.get(game_id) if game_id else None
    if g is None or g.status != "scheduled" or g.kickoff <= snap.now:
        return None
    return g


def _p_yes(mtype: str, model: MarginModel, mean: float, line: float | None, home_side: bool) -> float:
    if mtype == "total":
        return model.p_greater(mean, line)
    x = 0.0 if mtype == "ml" else line
    return model.p_greater(mean if home_side else -mean, x)


def _margin_model(snap: Snapshot, league: str, mtype: str) -> MarginModel:
    key = _MODEL_KEY[league][mtype]
    return snap.margin_models.get(key) or snap.margin_models[_MODEL_FALLBACK.get(key, key)]


def model_weight(league: str, mtype: str, hours_to_kickoff: float, snap: Snapshot) -> float:
    """The model's share of the logit blend: the fitted b_model less one se, capped at the prior default.

    With no fit, the prior default; NFL totals (a naive model) get 0 until their own backtest fits.
    """
    default = default_weight(league, hours_to_kickoff)
    fit = snap.blend_fits.get((league, mtype))
    if fit is None:
        return 0.0 if (league, mtype) == ("nfl", "total") else default
    b, se = fit
    return float(min(max(b - se, 0.0), default))


@dataclass(frozen=True)
class GameFair:
    """P(YES)/P(NO) for a Kalshi game market and where it came from."""
    p_yes: float
    p_no: float
    p_market: float
    p_model: float | None
    model_weight: float
    fair: FairPrice
    method: str  # h2h | exact line | adjacent line | implied mean
    tie_prob: float = 0.0
    mkt_yes: float | None = None  # market-only P(YES)/P(NO), tie-adjusted like p_yes/p_no
    mkt_no: float | None = None
    # points between the strike and the sportsbook line it was priced from (0: a quote at the strike)
    shift: float = 0.0

    def market_side(self, side: str) -> float:
        """Market-only P(this side wins), for calibration baselines and CLV."""
        if side == "yes":
            return self.p_market if self.mkt_yes is None else self.mkt_yes
        return 1.0 - self.p_market if self.mkt_no is None else self.mkt_no

    def why(self) -> dict:
        return {"fair_source": self.fair.source, "books": list(self.fair.books),
                "p_market": round(self.p_market, 4),
                "p_model": None if self.p_model is None else round(self.p_model, 4),
                "model_weight": round(self.model_weight, 4), "method": self.method,
                "tie_prob": self.tie_prob, "book_shift": self.shift}


class _MarketFair(NamedTuple):
    p_mkt: float
    mkt_mean: float | None
    fair: FairPrice
    method: str
    shift: float = 0.0  # |strike - the quoted line it was priced from|, in points


def _sharp_at(quotes: list[Quote], market: str, threshold: float, model: MarginModel,
              kw: dict) -> tuple[float, FairPrice, str, float] | None:
    """(P(home margin / total > threshold), fair, method, quoted line) straight from a sharp quote at
    the line or a half point away.

    A quote exactly at a half-point threshold is the answer. A quote at an adjacent integer line is
    P(cover | no push) there, so it goes through the push-aware inversion before pricing.
    """
    def at(x: float) -> FairPrice | None:
        fp = fair_price(quotes, market, -x if market == "spreads" else x, **kw)
        return fp if fp is not None and fp.tier < TIER_RANK["median"] else None

    if threshold != math.floor(threshold) and (fp := at(threshold)) is not None:
        return fp.p, fp, "exact line", threshold
    best: tuple[int, float, float, FairPrice] | None = None
    for x in (threshold - 0.5, threshold + 0.5):
        if (fp := at(x)) is not None and (best is None or (fp.tier, abs(fp.p - 0.5)) < best[:2]):
            best = (fp.tier, abs(fp.p - 0.5), x, fp)
    if best is None:
        return None
    _, _, x, fp = best
    mean = implied_mean(model, x, fp.p)
    return None if mean is None else (model.p_greater(mean, threshold), fp, "adjacent line", x)


def kalshi_tie_rule(m: KalshiMarket) -> str | None:
    """"no" when the market's rules say a tie settles every team's market NO; None when unknown."""
    text = (getattr(m, "rules_secondary", "") or "").lower()
    if "tie" in text and _TIE_NO.search(text):
        return "no"
    return None


def _market(m: KalshiMarket, g: Game, snap: Snapshot, s: Settings,
            cache: dict | None = None) -> _MarketFair | None:
    """Sportsbook-only P(YES), market mean and fair source, or None when inputs are missing.

    cache: shared across one scan so each game's implied mean is solved once, not per Kalshi strike.
    """
    mtype = m.market_type
    if mtype != "total" and m.team not in (g.home, g.away):
        return None
    if mtype != "ml" and m.line is None:
        return None
    home_side = m.team == g.home
    quotes = [q for q in snap.quotes if q.game_id == g.game_id]
    model = _margin_model(snap, g.league, mtype)
    kw = {"now": snap.now, "stale_seconds": s.stale_seconds}
    cache = {} if cache is None else cache

    def market_mean(market: str, mm: MarginModel):
        key = (g.game_id, market)
        if key not in cache:
            cache[key] = implied_market_mean(quotes, market, mm, **kw)
        return cache[key]

    if mtype == "ml":
        fp = fair_price(quotes, "h2h", None, **kw)
        if fp is None:
            return None
        p_mkt, method = (fp.p if home_side else 1.0 - fp.p), "h2h"
        im = market_mean("spreads", _margin_model(snap, g.league, "spread"))
        mkt_mean = None if im is None else im.mean
    else:
        market = "totals" if mtype == "total" else "spreads"
        im = market_mean(market, model)
        mkt_mean = None if im is None else im.mean
        threshold = m.line if (mtype == "total" or home_side) else -m.line
        direct = _sharp_at(quotes, market, threshold, model, kw)
        if direct is not None:
            p_a, fp, method, book_line = direct
            p_mkt = p_a if (mtype == "total" or home_side) else 1.0 - p_a
        elif im is not None:
            fp, method = im.fair, "implied mean"
            book_line = -im.point if market == "spreads" else im.point  # home-margin / total units
            p_mkt = _p_yes(mtype, model, im.mean, m.line, home_side)
        else:
            return None
        return _MarketFair(p_mkt, mkt_mean, fp, method, abs(threshold - book_line))
    return _MarketFair(p_mkt, mkt_mean, fp, method)


def market_p_yes(m: KalshiMarket, g: Game, snap: Snapshot, s: Settings) -> float | None:
    """Devigged sportsbook P(YES) with no model (closing-line value, pure market edges)."""
    mkt = _market(m, g, snap, s)
    return None if mkt is None else mkt.p_mkt


def _game_fair(m: KalshiMarket, g: Game, snap: Snapshot, s: Settings,
               cache: dict | None = None) -> GameFair | None:
    """Blended P(YES)/P(NO) for a Kalshi game market, or None when inputs are missing.

    The model gets no weight when its gap to the market exceeds the league cap (likely missing
    information), when it is vetoed (QB change), for NFL totals until validated, and for CFB
    unless the CFB gate passes and the model disagrees by at least cfb_min_disagree.
    """
    mkt = _market(m, g, snap, s, cache)
    if mkt is None:
        return None
    p_mkt, mkt_mean, fp, method, shift = mkt
    mtype = m.market_type
    pred = snap.predictions.get(g.game_id)
    model_mean = None if pred is None else (pred.mean_total if mtype == "total" else pred.mean_margin)
    p_model, w, p = None, 0.0, p_mkt
    if model_mean is not None:
        hours = (g.kickoff - snap.now).total_seconds() / 3600
        w = model_weight(g.league, mtype, hours, snap)
        cap = snap.disagree_caps.get(g.league, s.nfl_max_disagree if g.league == "nfl" else s.cfb_max_disagree)
        gap = None if mkt_mean is None else abs(model_mean - mkt_mean)
        if pred.veto or (gap is not None and gap > cap):
            w = 0.0
        if g.league == "cfb" and (snap.gates.get((g.league, mtype), False) or gap is None
                                  or gap < s.cfb_min_disagree):
            w = 0.0  # CFB: model only as a bonus once gated in and clearly disagreeing
        if w > 0.0:
            p_model = _p_yes(mtype, _margin_model(snap, g.league, mtype), model_mean, m.line,
                             m.team == g.home)
            p = blend(p_model, p_mkt, w)
        else:
            w = 0.0
    tie = s.nfl_tie_prob if (g.league == "nfl" and mtype == "ml") else 0.0
    tie_no = kalshi_tie_rule(m) == "no"

    def sides(q: float) -> tuple[float, float]:
        # sportsbook moneylines and the model are P(win | no tie); a tie loses YES for both teams.
        # Unless the rules confirm a tie settles NO, a tie loses NO too (worst case per side).
        y = q * (1.0 - tie)
        return y, (1.0 - y if tie_no else (1.0 - q) * (1.0 - tie))

    p_yes, p_no = sides(p)
    mkt_yes, mkt_no = sides(p_mkt)
    return GameFair(p_yes, p_no, p_mkt, p_model, w, fp, method, tie, mkt_yes, mkt_no, shift)


def game_p_yes_w(m: KalshiMarket, g: Game, snap: Snapshot, s: Settings) -> tuple[float, float] | None:
    """(Blended P(YES), model weight) for a Kalshi game market, or None when inputs are missing."""
    gf = _game_fair(m, g, snap, s)
    return None if gf is None else (gf.p_yes, gf.model_weight)


def _game_p_yes(m: KalshiMarket, g: Game, snap: Snapshot, s: Settings) -> float | None:
    """Blended P(YES) for a Kalshi game market, or None when inputs are missing."""
    gf = _game_fair(m, g, snap, s)
    return None if gf is None else gf.p_yes


def _best_side(m: KalshiMarket, p_yes: float, snap: Snapshot, s: Settings, extra_ev: float = 0.0,
               p_no: float | None = None) -> _Side | None:
    """The YES/NO side with the higher EV that passes every filter.

    p_no defaults to 1 - p_yes; pass it when each side has its own worst-case probability.
    """
    bankroll = snap.bankroll_cents / 100
    best: _Side | None = None
    p_no = 1.0 - p_yes if p_no is None else p_no
    for side, price, p, depth in (("yes", m.yes_ask, p_yes, m.yes_ask_size),
                                  ("no", m.no_ask, p_no, m.no_ask_size)):
        if price <= 0:
            continue
        qty = min(math.floor(s.cap_bet * bankroll / price), int(depth))
        if qty < 1:
            continue
        p_eff = effective_price(price, qty, m.fee_multiplier)
        ok, _ = passes_filters(p, price, p_eff, depth, qty, s, m.status)
        ev = kalshi_ev(p, p_eff)
        if ok and ev >= s.kalshi_min_ev + extra_ev and (best is None or ev > best.ev):
            best = _Side(side, price, p, qty, ev)
    return best


def _kalshi_rec(kind: Literal["kalshi_game", "kalshi_prop"], m: KalshiMarket, g: Game, sd: _Side,
                description: str, snap: Snapshot, gate: str, tie_risk: bool, why: dict | None = None,
                observe: bool | None = None, team: str | None = None,
                p_mkt: float | None = None) -> Recommendation:
    """observe: None takes the (league, gate) gate; True/False overrides it. p_mkt: market-only P(side)."""
    leg = Leg("kalshi", description, sd.side, sd.price, sd.p, g.game_id, ticker=m.ticker,
              player=m.player, stat=m.stat, line=m.line, team=team or m.team, fee_multiplier=m.fee_multiplier,
              p_mkt=p_mkt)
    observe_only = snap.gates.get((g.league, gate), False) if observe is None else observe
    return Recommendation(None, kind, [leg], 0, sd.qty, sd.ev, sd.p, None, snap.now, g.kickoff,
                          observe_only=observe_only, tie_risk=tie_risk, why=why, p_mkt=p_mkt)


def _model_gated(snap: Snapshot, league: str) -> bool:
    """Projector-only (non-anchored) PrizePicks legs stake only once the projector passed its calibration
    check (snap.projector_live). Projector-only Kalshi props are always observe-only."""
    return not snap.projector_live


# Kalshi game strikes away from the book line: the Normal/empirical curve prices them by translating
# the book's line, and its tail is the weakest part of it. Kalshi's centre strike matches Pinnacle, so
# the Kalshi mid is a strong prior once we leave the quoted line.
GAME_MAX_GAP = 0.08  # our number this far from a liquid Kalshi mid is our error, not an edge


def game_max_shift(league: str, s: Settings) -> float:
    """Furthest (pts) a Kalshi game strike is priced from the sportsbook line it was translated from."""
    return s.game_max_shift_nfl if league == "nfl" else s.game_max_shift_cfb


GAME_QUOTED_SHIFT = 0.5  # at most this far from a sharp quote, the strike is priced by that quote


def _game_weight(shift: float, max_shift: float) -> float:
    """Share of our number kept against the Kalshi mid: all of it at (or a half point from) a quoted
    sharp line, exp(-shift / max_shift) once translated (37% at the limit)."""
    return 1.0 if shift <= GAME_QUOTED_SHIFT else math.exp(-shift / max_shift)


def _game_guard(m: KalshiMarket, league: str, gf: GameFair, s: Settings) -> tuple[float, float, dict] | None:
    """(P(YES), P(NO), why extras) after the far-strike guards, or None when the idea is dropped.

    Strikes further than game_max_shift from the book line are not priced. A translated strike needs
    a liquid Kalshi mid within GAME_MAX_GAP of our number (each side is a worst case, so the centre is
    compared), and is then shrunk toward that mid by _game_weight. A strike at (or a half point from)
    a quoted sharp line keeps its number.
    """
    max_shift = game_max_shift(league, s)
    if gf.shift > max_shift:
        return None
    if gf.shift <= GAME_QUOTED_SHIFT:
        return gf.p_yes, gf.p_no, {}
    mid = _kalshi_mid(m)
    if mid is None or abs((gf.p_yes + 1 - gf.p_no) / 2 - mid) > GAME_MAX_GAP:
        return None
    w = _game_weight(gf.shift, max_shift)
    p_yes, p_no = _shrink_to_market(gf.p_yes, gf.p_no, m, w)
    return p_yes, p_no, {"kalshi_mid": mid, "weight": round(w, 4)}


def kalshi_game_candidates(snap: Snapshot, s: Settings) -> list[Recommendation]:
    """Best Kalshi ml/spread/total recommendation per game (unsized: stake 0, provisional qty).

    Strikes away from the book line go through _game_guard (translation limit, Kalshi-mid gap check
    and shrink)."""
    best: dict[str, tuple[KalshiMarket, Game, _Side, GameFair, dict]] = {}
    cache: dict = {}
    for m in snap.kalshi:
        if m.market_type not in _GAME_TYPES:
            continue
        g = _upcoming(snap, m.game_id)
        if g is None or g.game_id in snap.open_game_ids:  # one market per game, already held
            continue
        gf = _game_fair(m, g, snap, s, cache)
        if gf is None:
            continue
        if gf.fair.source == "median" and s.median_fair_policy == "block":
            continue
        guarded = _game_guard(m, g.league, gf, s)
        if guarded is None:
            continue
        p_yes, p_no, extra = guarded
        sd = _best_side(m, p_yes, snap, s, p_no=p_no)
        if sd is not None and (g.game_id not in best or sd.ev > best[g.game_id][2].ev):
            if extra:
                extra = {**extra, "p_ours": gf.p_yes if sd.side == "yes" else gf.p_no}
            best[g.game_id] = (m, g, sd, gf, extra)

    out = []
    for m, g, sd, gf, extra in best.values():
        desc = contract_text(m.market_type, m.team, m.line, g.away, g.home, g.league)
        tie = m.market_type == "ml" and ml_tie_risk(m, g.league)
        # the gate judges the model: a market-only edge (w=0) may go live when market_only_live is on
        gated = snap.gates.get((g.league, m.market_type), False) and (gf.model_weight > 0
                                                                       or not s.market_only_live)
        # soft-book median only: no sharp price behind it, so log it rather than stake it
        observe = (snap.paused.get((g.league, m.market_type), False) or gated
                   or (gf.fair.source == "median" and s.median_fair_policy == "observe"))
        p_mkt = gf.market_side(sd.side)
        out.append(_kalshi_rec("kalshi_game", m, g, sd, desc, snap, m.market_type, tie, {**gf.why(), **extra},
                               observe, p_mkt=p_mkt))
    return out


def _stat_kind(stat: str) -> Literal["yards", "count"]:
    return "yards" if stat.endswith("_yds") else "count"


def _max_shift(stat: str, s: Settings, point: float) -> float:
    """Furthest a prop is repriced from the book's point: relative for rushing / receiving yards (a
    9.5-yard line says little about 25+), fixed for pass yards and counts."""
    if stat == "pass_yds":
        return s.props_max_shift_pass_yds
    if _stat_kind(stat) != "yards":
        return s.props_max_shift_count
    return min(s.props_max_shift_yds, max(s.props_min_shift_yds, s.props_max_shift_frac * point))


def _prop_fresh(q: PropQuote, snap: Snapshot, s: Settings) -> bool:
    """Prop lines are pulled once near kickoff: age them from fetch time against the props TTL."""
    return (snap.now - q.seen_at).total_seconds() <= s.props_stale_seconds


def _book_quotes(snap: Snapshot, s: Settings, game_id: str, player: str, stat: str) -> list[PropQuote]:
    """Fresh sportsbook (non-PrizePicks) prop quotes for one player and stat."""
    key = normalize_name(player)
    return [q for q in snap.props
            if q.book != "prizepicks" and q.decimal is not None and q.game_id == game_id and q.stat == stat
            and normalize_name(q.player) == key and _prop_fresh(q, snap, s)]


def _book_pairs(snap: Snapshot, s: Settings, game_id: str, player: str,
                stat: str) -> dict[tuple[str, float], dict[str, PropQuote]]:
    """{(book, point): {"over": q, "under": q}} for every book quoting both sides (newest quote each)."""
    pairs: dict[tuple[str, float], dict[str, PropQuote]] = {}
    for q in _book_quotes(snap, s, game_id, player, stat):
        sides = pairs.setdefault((q.book, q.point), {})
        if q.side not in sides or q.last_update > sides[q.side].last_update:
            sides[q.side] = q
    return {k: v for k, v in pairs.items() if "over" in v and "under" in v}


def _book_point(snap: Snapshot, s: Settings, game_id: str, player: str, stat: str, line: float) -> float | None:
    """The two-sided book point nearest the line (the anchor _book_p_sides prices from)."""
    points = {pt for _, pt in _book_pairs(snap, s, game_id, player, stat)}
    return min(points, key=lambda pt: (abs(pt - line), pt)) if points else None


def _book_p_sides(snap: Snapshot, s: Settings, game_id: str, player: str, stat: str,
                  line: float) -> tuple[float, float] | None:
    """(P(over), P(under)) at the line from the consensus of books at the point nearest it.

    Each book's sides are devigged with their own most conservative method; the anchor is the median
    across books of each side (so P(over) + P(under) <= 1). Each side is then shifted to the line
    through its own implied mean, never further than the stat's max shift.
    """
    both = _book_pairs(snap, s, game_id, player, stat)
    if not both:
        return None
    point = min({pt for _, pt in both}, key=lambda pt: (abs(pt - line), pt))
    if abs(line - point) > _max_shift(stat, s, point):
        return None
    overs, unders = [], []
    for (_, pt), sides in both.items():
        if pt == point:
            decimals = [sides["over"].decimal, sides["under"].decimal]
            overs.append(devig_worst(decimals, 0))
            unders.append(devig_worst(decimals, 1))
    p_over_at_point, p_under_at_point = float(np.median(overs)), float(np.median(unders))
    if line == point:
        return p_over_at_point, p_under_at_point
    kind = _stat_kind(stat)
    try:  # a separate implied mean for each side's worst-case probability
        mean_over = implied_mean_from_line(point, p_over_at_point, kind, stat=stat)
        mean_under = implied_mean_from_line(point, 1.0 - p_under_at_point, kind, stat=stat)
    except ValueError:  # no mean reproduces this probability
        return None
    # the spread belongs to the book's line (the player's median), not to the strike
    return (prop_p_over(line, mean_over, kind, stat=stat, anchor=point),
            1.0 - prop_p_over(line, mean_under, kind, stat=stat, anchor=point))


def _game_context(snap: Snapshot, projector: Projector, g: Game, player: str) -> dict:
    """Opponent and market game script (team implied total, team spread) for the projector."""
    team = _player_team(snap, projector, player)
    if team not in (g.home, g.away):
        return {}
    home = team == g.home
    quotes = [q for q in snap.quotes if q.game_id == g.game_id]
    margin = fair_spread_mean(quotes, now=snap.now,
                              model=_margin_model(snap, g.league, "spread"))  # expected home margin
    total = fair_total_mean(quotes, now=snap.now)
    ctx: dict = {"opponent": g.away if home else g.home, "team": team}
    if margin is not None:
        ctx["team_spread"] = -margin if home else margin
        if total is not None:
            ctx["team_total"] = (total + (margin if home else -margin)) / 2
    return ctx


def _model_p_over(projector: Projector, g: Game, player: str, stat: str, line: float,
                  ctx: dict | None = None) -> float | None:
    if g.week is None:
        return None
    params = inspect.signature(projector.project).parameters
    if ctx and "opponent" in params:  # pass what this projector accepts (team picks among shared names)
        proj = projector.project(player, stat, g.season, g.week, **{k: v for k, v in ctx.items() if k in params})
    else:
        proj = projector.project(player, stat, g.season, g.week)
    if proj is None:
        return None
    mean, sd = proj
    kind = _stat_kind(stat)
    return prop_p_over(line, mean, kind, sd if sd > 0 else None, stat=stat)


def _prop_p_sides(snap: Snapshot, s: Settings, projector: Projector, g: Game, player: str, stat: str,
                  line: float) -> tuple[float, float, bool] | None:
    """(P(over), P(under), anchored): sportsbook-anchored when any fresh book quote exists, else the
    projector. A known market price is never replaced by the weaker model."""
    if _book_quotes(snap, s, g.game_id, player, stat):
        sides = _book_p_sides(snap, s, g.game_id, player, stat, line)
        return None if sides is None else (sides[0], sides[1], True)
    p = _model_p_over(projector, g, player, stat, line, _game_context(snap, projector, g, player))
    return None if p is None else (p, 1.0 - p, False)


# Kalshi props: how much of our number to keep against the Kalshi mid (the rest is the mid). Book and
# Kalshi agree within ~1.3pp at the same strike, so the mid is a strong prior; a translated strike leans
# on the yardage curve and a projector-only number on a model never checked against a market.
PROP_W_EXACT = 0.8      # book line == strike
PROP_W_SHIFT = 0.5      # translated: PROP_W_SHIFT * exp(-|K - L| / L)
PROP_W_MODEL = 0.25     # projector only
PROP_MID_MAX_SPREAD = 0.06  # the mid is only trusted on a tight, two-sided Kalshi book
PROP_MAX_GAP = 0.10     # our number this far from a liquid mid is our error, not an edge


def _kalshi_mid(m: KalshiMarket) -> float | None:
    """Mid of the YES book, or None when it is wide or one-sided."""
    if m.yes_bid <= 0 or m.yes_ask <= 0 or m.yes_ask_size <= 0 or m.no_ask_size <= 0:
        return None
    if m.yes_ask - m.yes_bid > PROP_MID_MAX_SPREAD + 1e-9:
        return None
    return (m.yes_bid + m.yes_ask) / 2


def _shrink_to_market(p_over: float, p_under: float, m: KalshiMarket, w: float) -> tuple[float, float] | None:
    """Each side pulled toward the Kalshi mid: w * ours + (1 - w) * mid. None when there is no usable mid."""
    mid = _kalshi_mid(m)
    if mid is None:
        return None
    return w * p_over + (1 - w) * mid, w * p_under + (1 - w) * (1 - mid)


def _prop_weight(anchored: bool, strike: float, point: float | None) -> float:
    if not anchored or point is None:
        return PROP_W_MODEL
    if strike == point:
        return PROP_W_EXACT
    return PROP_W_SHIFT * math.exp(-abs(strike - point) / max(point, 1.0))


def kalshi_prop_candidates(snap: Snapshot, s: Settings, projector: Projector) -> list[Recommendation]:
    """Best Kalshi prop recommendation per player (unsized). Our number is shrunk toward the Kalshi mid
    and dropped when it sits implausibly far from a liquid one; projector-only picks are observe-only."""
    best: dict[str, tuple[KalshiMarket, Game, _Side, bool, dict, float | None]] = {}
    for m in snap.kalshi:
        if m.market_type != "prop" or m.player is None or m.stat is None or m.line is None:
            continue
        key = normalize_name(m.player)
        if key in snap.open_players:  # already holding a prop on this player
            continue
        g = _upcoming(snap, m.game_id)
        if g is None:
            continue
        got = _prop_p_sides(snap, s, projector, g, m.player, m.stat, m.line)
        if got is None:
            continue
        p_over, p_under, anchored = got
        mid = _kalshi_mid(m)
        # our two-sided centre (each side is a worst case, so they sum below 1) against the mid
        if mid is None or abs((p_over + 1 - p_under) / 2 - mid) > PROP_MAX_GAP:
            continue
        point = _book_point(snap, s, g.game_id, m.player, m.stat, m.line) if anchored else None
        w = _prop_weight(anchored, m.line, point)
        p_yes, p_no = _shrink_to_market(p_over, p_under, m, w)
        extra = 0.0 if anchored else s.pp_margin_model - s.pp_margin_market
        sd = _best_side(m, p_yes, snap, s, extra, p_no=p_no)
        if sd is not None and (key not in best or sd.ev > best[key][2].ev):
            why = {"kalshi_mid": mid, "weight": w, "p_ours": p_over if sd.side == "yes" else p_under,
                   "book_line": point}
            # books priced this exact strike: their devigged side is a market probability; a translated
            # strike or a projection is not
            p_mkt = (p_over if sd.side == "yes" else p_under) if anchored and point == m.line else None
            best[key] = (m, g, sd, anchored, why, p_mkt)
    out = []
    for m, g, sd, anchored, why, p_mkt in best.values():
        out.append(_kalshi_rec("kalshi_prop", m, g, sd, f"{m.player} {m.stat} over {m.line}", snap, "prop",
                               False, why=why, observe=True if not anchored else None,
                               team=_player_team(snap, projector, m.player), p_mkt=p_mkt))
    return out


def _player_team(snap: Snapshot, projector: Projector, player: str) -> str | None:
    team = snap.player_teams.get(player)
    if team is None:
        key = normalize_name(player)
        team = next((t for name, t in snap.player_teams.items() if normalize_name(name) == key), None)
    return team or projector.team_of(player)


def _pp_standard(q: PropQuote) -> bool:
    """Spec 6.3: only standard lines; Demon/Goblin-type lines carry an adjusted payout multiplier."""
    return q.multiplier is None or q.multiplier == 1.0


def _pp_picks(snap: Snapshot, s: Settings, projector: Projector) -> list[Pick]:
    """Best side of every PrizePicks line, skipping players and games already in open entries."""
    picks: list[Pick] = []
    seen: set[tuple] = set()
    for q in snap.props:
        if q.book != "prizepicks" or not _pp_standard(q):
            continue
        if q.source != "manual" and not _prop_fresh(q, snap, s):
            continue
        key = (q.game_id, normalize_name(q.player), q.stat, q.point)
        if key[1] in snap.pp_open_players or q.game_id in snap.pp_open_games:
            continue
        if key in seen:
            continue
        seen.add(key)
        g = _upcoming(snap, q.game_id)
        if g is None:
            continue
        team = _player_team(snap, projector, q.player)
        if team is None:  # unknown team: skip rather than guess (the 2-team rule depends on it)
            continue
        got = _prop_p_sides(snap, s, projector, g, q.player, q.stat, q.point)
        if got is None:
            continue
        p_over, p_under, anchored = got
        side, p = ("over", p_over) if p_over >= p_under else ("under", p_under)
        picks.append(Pick(q.player, team, g.game_id, q.stat, q.point, side, p, anchored))
    return picks


@dataclass(frozen=True)
class PPLine:
    """One loaded PrizePicks line with our better side and chance (None when we can't price it)."""
    game_id: str
    player: str
    team: str | None
    stat: str
    line: float
    side: str | None
    p: float | None
    anchored: bool
    fetched_at: datetime
    fresh: bool


def pp_lines(snap: Snapshot, s: Settings, projector: Projector) -> list[PPLine]:
    """Every standard PrizePicks line on an upcoming game, for display and entries you build yourself.

    A line older than props_stale_seconds (fresh=False) is still priced the same way, from the
    sportsbook prices cached with it, so you can check an entry; the builder and texts skip it."""
    out: list[PPLine] = []
    seen: set[tuple] = set()
    loose = replace(s, props_stale_seconds=10**9)  # stale lines: every cached sportsbook price counts
    for q in snap.props:
        if q.book != "prizepicks" or not _pp_standard(q):
            continue
        key = (q.game_id, normalize_name(q.player), q.stat, q.point)
        g = _upcoming(snap, q.game_id)
        if g is None or key in seen:
            continue
        seen.add(key)
        fresh = q.source == "manual" or _prop_fresh(q, snap, s)
        team = _player_team(snap, projector, q.player)
        got = _prop_p_sides(snap, s if fresh else loose, projector, g, q.player, q.stat, q.point)
        side = p = None
        anchored = False
        if got is not None:
            p_over, p_under, anchored = got
            side, p = ("over", p_over) if p_over >= p_under else ("under", p_under)
        out.append(PPLine(g.game_id, q.player, team, q.stat, q.point, side, p, anchored, q.seen_at, fresh))
    return out


def pp_blocker(snap: Snapshot, s: Settings, projector: Projector) -> tuple[str, dict] | None:
    """Why no PrizePicks entry can be texted now, as (code, details); None when one can be built.

    Codes: no_lines, stale, no_structures, few_lines, few_good, one_team, few_games, model_only,
    no_value, paused (drift/CLV), watch_only (gate), daily_cap."""
    lines = pp_lines(snap, s, projector)
    if not lines:
        return ("no_lines", {})
    if not any(ln.fresh for ln in lines):
        return ("stale", {"seconds": s.props_stale_seconds})
    table, allowed = payout_table(s), allowed_structures(s)
    sts = [st for st in allowed if st in table and table[st].get(st[0], 0) > 1]
    if not sts:
        return ("no_structures", {})
    need = min(st[0] for st in sts)
    picks = _pp_picks(snap, s, projector)
    if len(picks) < need:
        no_team = sum(ln.fresh and ln.team is None for ln in lines)
        return ("few_lines", {"priced": len(picks), "need": need, "no_team": no_team})
    good, two_teams, enough, spread = 0, False, False, False
    for st in sts:
        be = breakeven(st, table)
        qual = [pk for pk in picks if pk.p >= be + (s.pp_margin_market if pk.anchored else s.pp_margin_model)]
        good = max(good, len(qual))
        if len(qual) < st[0]:
            continue
        enough = True
        if len({pk.team for pk in qual}) >= 2:
            two_teams = True
            per_game = defaultdict(int)
            for pk in qual:
                per_game[pk.game_id] += 1
            spread = spread or sum(min(c, s.pp_max_per_game) for c in per_game.values()) >= st[0]
    if not enough:
        return ("few_good", {"good": good, "need": need})
    if not two_teams:
        return ("one_team", {})
    if not spread:
        return ("few_games", {"cap": s.pp_max_per_game})
    pool = [pk for pk in picks if pk.anchored or not _model_gated(snap, snap.games[pk.game_id].league)]
    found = build_best_entry(pool, s, allowed, payouts=table, brute=s.pp_brute_force)
    if found is None:
        any_found = build_best_entry(picks, s, allowed, payouts=table, brute=s.pp_brute_force)
        return ("model_only", {}) if any_found is not None else ("no_value", {})
    leagues = {snap.games[pk.game_id].league for pk in found[0]}
    keys = [(lg, k) for lg in leagues for k in ("prop", "pp")]
    if any(snap.paused.get(k) for k in keys):
        return ("paused", {})
    if any(snap.gates.get(k) for k in keys):
        return ("watch_only", {})
    if math.floor(s.cap_pp_daily * snap.bankroll_cents) - snap.pp_staked_today_cents < max(s.min_stake_cents, 1):
        return ("daily_cap", {})
    return None


def _pp_rec(legs: list[Pick], st: tuple, ev: float, stake: int, observe: bool, table: dict,
            snap: Snapshot, rho: float = 0.0) -> Recommendation:
    return pp_entry_rec(legs, st, ev, stake, observe, table, snap.now, snap.games, rho)


def pp_stake(legs: list[Pick], st: tuple, table: dict, s: Settings, budget: "Budget", daily_left: int) -> int:
    """Cents to put on one PrizePicks entry: robust Kelly on the available bankroll, shrunk per pick
    source (and while the payout table is unverified), capped by the bet, daily PrizePicks and
    game/slate limits. Never negative."""
    f, _ = robust_kelly(legs, st, table, s.copula_rho)
    mult = s.kelly_fraction * min(s.shrink_prop_anchored if pk.anchored else s.shrink_prop_model for pk in legs)
    if not s.pp_payouts_verified:
        mult *= s.pp_unverified_mult
    avail = budget.available
    cap = min(s.cap_bet * avail, daily_left, budget.cap_for(pk.game_id for pk in legs))
    return max(0, math.floor(min(f * mult * avail, cap)))


def pp_entry_rec(legs: list[Pick], st: tuple, ev: float, stake: int, observe: bool, table: dict,
                 now: datetime, games: Mapping[str, Game], rho: float = 0.0) -> Recommendation:
    """The pp_entry recommendation for these picks (built entries and ones you put together yourself)."""
    dist = worst_distribution(legs, st, table, rho)  # same-game picks: the correlation we fear most
    p_win = sum(dist[k] for k, mult in table[st].items() if mult > 1)
    out_legs = [Leg("prizepicks", f"{pk.player} {pk.side} {pk.line} {pk.stat}", pk.side, 0.0, pk.p,
                    pk.game_id, player=pk.player, stat=pk.stat, line=pk.line, team=pk.team,
                    p_mkt=pk.p if pk.anchored else None)
                for pk in legs]
    p_mkt = p_win if all(pk.anchored for pk in legs) else None  # all-anchored legs: p_win is market-only
    return Recommendation(None, "pp_entry", out_legs, stake, None, ev, p_win, f"{st[0]}-{st[1]}",
                          now, min(games[pk.game_id].kickoff for pk in legs), observe_only=observe,
                          p_mkt=p_mkt)


def pp_entry_candidates(snap: Snapshot, s: Settings, projector: Projector,
                        budget: Budget | None = None) -> list[Recommendation]:
    """PrizePicks entries with no game or player shared between entries (an entry itself may hold
    up to pp_max_per_game picks from one game), best Kelly growth first, until nothing
    qualifies, pp_max_entries is reached or the caps run out. Stakes come from the available
    bankroll, shrunk per pick source and while the payout table is unverified.

    Until the projector passes its backtest, live entries use book-anchored legs only; if none can
    be built, one entry including projector legs is logged observe-only.
    """
    picks = _pp_picks(snap, s, projector)
    table, allowed = payout_table(s), allowed_structures(s)
    budget = budget or Budget.from_snap(snap, s)
    daily_left = math.floor(s.cap_pp_daily * snap.bankroll_cents) - snap.pp_staked_today_cents
    out: list[Recommendation] = []
    pool = [pk for pk in picks if pk.anchored or not _model_gated(snap, snap.games[pk.game_id].league)]
    built = False
    while pool and len(out) < s.pp_max_entries:
        found = build_best_entry(pool, s, allowed, payouts=table, brute=s.pp_brute_force)
        if found is None:
            break
        built = True
        legs, st, ev, _ = found
        games = [snap.games[pk.game_id] for pk in legs]
        observe = any(snap.gates.get((g.league, "prop"), False) or snap.gates.get((g.league, "pp"), False)
                      for g in games)
        stake = 0
        if not observe:
            stake = pp_stake(legs, st, table, s, budget, daily_left)
            if stake < max(s.min_stake_cents, 1):
                break
            daily_left -= stake
            budget.charge(stake, {pk.game_id for pk in legs})
        out.append(_pp_rec(legs, st, ev, stake, observe, table, snap, s.copula_rho))
        used_games = {pk.game_id for pk in legs}
        used_players = {normalize_name(pk.player) for pk in legs}
        pool = [pk for pk in pool if pk.game_id not in used_games and normalize_name(pk.player) not in used_players]
    if not built and len(pool) < len(picks):
        found = build_best_entry(picks, s, allowed, payouts=table, brute=s.pp_brute_force)
        if found is not None:  # projector legs, unproven: log it, stake nothing
            legs, st, ev, _ = found
            out.append(_pp_rec(legs, st, ev, 0, True, table, snap, s.copula_rho))
    return out


def _plain_legs(legs: list[Leg], snap: Snapshot, g: Game) -> list[Leg]:
    """Describe each Kalshi leg as the outcome its "Yes" pays on, not by ticker."""
    mtype = {m.ticker: m.market_type for m in snap.kalshi}
    return [replace(leg, description=contract_text(mtype.get(leg.ticker or "") or _leg_type(leg), leg.team,
                                                   leg.line, g.away, g.home, g.league)) for leg in legs]


def _pays_at_least_one(a: Arb, snap: Snapshot) -> bool:
    """Re-check a pair's payoff from the snapshot's markets: >= $1 per pair in every final score."""
    by_ticker = {m.ticker: m for m in snap.kalshi}
    ms = [by_ticker.get(leg.ticker or "") for leg in a.legs]
    if any(m is None for m in ms):
        return False
    g = snap.games.get(a.game_id or "")
    ties = g.league == "nfl" if g is not None else not ms[0].event_ticker.startswith("KXNCAAF")
    rng = payoff_range([(m, leg.side) for m, leg in zip(ms, a.legs)], ties)
    return rng is not None and rng[0] >= 1 - 1e-9


def _middle_legging(legs: list[Leg], qty: int, p_mid: float, depth: dict[tuple[str, str], float]) -> list[Leg]:
    """Thinnest leg first; the other's worst price keeps the expected payout at or above the cost."""
    first, second = sorted(legs, key=lambda leg: depth.get((leg.ticker or "", leg.side), 0.0))
    paid = qty * first.price + taker_fee(qty, first.price, first.fee_multiplier)
    worst = second.price
    for cents in range(99, round(second.price * 100), -1):
        w = cents / 100
        if paid + qty * w + taker_fee(qty, w, second.fee_multiplier) <= qty * (1.0 + p_mid) + 1e-9:
            worst = w
            break
    return [replace(first, worst_price=first.price), replace(second, worst_price=worst)]


def _legging(legs: list[Leg], qty: int, depth: dict[tuple[str, str], float]) -> list[Leg]:
    """Order legs thinnest-first (fill the scarce side before the other moves) and give the
    second leg the worst price that still pays back at least the cost."""
    first, second = sorted(legs, key=lambda leg: depth.get((leg.ticker or "", leg.side), 0.0))
    paid = qty * first.price + taker_fee(qty, first.price, first.fee_multiplier)
    worst = second.price
    for cents in range(99, round(second.price * 100), -1):
        w = cents / 100
        if paid + qty * w + taker_fee(qty, w, second.fee_multiplier) <= qty + 1e-9:
            worst = w
            break
    return [replace(first, worst_price=first.price), replace(second, worst_price=worst)]


def arb_candidates(snap: Snapshot, s: Settings, league_of: Callable[[KalshiMarket], str]) -> list[Recommendation]:
    """Kalshi arbs on fresh quotes (ML YES and NO pairs, full strike ladders, cross-team spreads),
    sized to the arb bankroll fraction, with legging guidance."""
    fees = {m.ticker: m.fee_multiplier for m in snap.kalshi}
    fetched = {m.ticker: m.fetched_at for m in snap.kalshi}
    depth = {k: v for m in snap.kalshi for k, v in (((m.ticker, "yes"), m.yes_ask_size),
                                                    ((m.ticker, "no"), m.no_ask_size))}
    bankroll = snap.bankroll_cents / 100
    arbs = (kalshi_ml_pairs(snap.kalshi, league_of) + kalshi_ml_no_pairs(snap.kalshi)
            + kalshi_ladders(snap.kalshi) + kalshi_cross_spreads(snap.kalshi))
    out = []
    for a in filter_arbs(arbs, s.arb_max_roi, s.arb_min_roi):
        g = _upcoming(snap, a.game_id)
        if g is None or a.qty < 1 or a.cost <= 0 or not _pays_at_least_one(a, snap):
            continue
        if any((snap.now - fetched[leg.ticker]).total_seconds() > s.arb_stale_seconds for leg in a.legs):
            continue  # an old quote is probably gone
        qty = min(a.qty, math.floor(s.arb_fraction * bankroll / (a.cost / a.qty)))
        if qty < 1:
            continue
        # fees are rounded per order, so re-price at the smaller size
        cost = sum(effective_price(leg.price, qty, fees[leg.ticker]) for leg in a.legs) * qty
        if cost <= 0:
            continue
        roi = qty / cost - 1
        if roi <= 0 or roi < s.arb_min_roi:
            continue
        legs = _plain_legs(_legging(a.legs, qty, depth), snap, g)
        if not all(leg.description for leg in legs):  # a leg we can't describe (spread, no team)
            continue
        rec = Recommendation(None, "arb", legs, round(cost * 100),
                             qty, roi, 1.0,
                             a.kind, snap.now, g.kickoff, tie_risk=a.tie_risk)
        if rec.leg_key in snap.open_leg_keys:  # this arb is already placed
            continue
        out.append(rec)
    return out


def _empirical(model: MarginModel | None) -> bool:
    return isinstance(model, EmpiricalMarginModel) and not model._use_normal


def _middle_rec(a: Arb, snap: Snapshot, s: Settings) -> Recommendation | None:
    g = _upcoming(snap, a.game_id)
    if g is None or not _pays_at_least_one(a, snap):
        return None
    mtype = next((m.market_type for m in snap.kalshi if m.ticker == a.legs[0].ticker), None)
    if mtype not in ("spread", "total") or (mtype == "spread" and not all(leg.team for leg in a.legs)):
        return None
    model = snap.margin_models.get(_MODEL_KEY[g.league][mtype])
    if not _empirical(model):  # a normal fallback understates key-number mass
        return None
    quotes = [q for q in snap.quotes if q.game_id == g.game_id]
    mean = (fair_total_mean if mtype == "total" else fair_spread_mean)(quotes, now=snap.now)
    if mean is None:
        return None
    # every leg's strike within the translation limit of the book line (the expected margin / total)
    for leg in a.legs:
        x = leg.line if (mtype == "total" or leg.team == g.home) else -(leg.line or 0.0)
        if leg.line is None or abs(x - mean) > game_max_shift(g.league, s):
            return None
    ps = []
    for leg in a.legs:
        p_yes = _p_yes(mtype, model, mean, leg.line, leg.team == g.home)
        ps.append(p_yes if leg.side == "yes" else 1.0 - p_yes)
    p_mid = ps[0] + ps[1] - 1.0  # one leg always pays, so both pay with this probability
    if p_mid <= 0:
        return None
    ev = a.qty * (1.0 + p_mid) / a.cost - 1.0
    if ev < s.kalshi_min_ev:
        return None
    legs = _plain_legs([replace(leg, p_fair=p) for leg, p in zip(a.legs, ps)], snap, g)
    if not all(leg.description for leg in legs):
        return None
    observe = not s.middles_live or snap.gates.get((g.league, mtype), False)
    return Recommendation(None, "middle", legs, 0, a.qty, ev, p_mid, a.kind, snap.now, g.kickoff,
                          observe_only=observe)


def _size_middle(rec: Recommendation, snap: Snapshot, s: Settings, budget: Budget) -> Recommendation | None:
    """Kelly-size a middle as a bet that pays $2 or $1 per pair (a partial loss), within the caps."""
    avail, gid = budget.available, rec.legs[0].game_id or ""
    qty0 = rec.qty or 0
    if avail <= 0 or qty0 < 1:
        return None
    unit = sum(effective_price(leg.price, qty0, leg.fee_multiplier) for leg in rec.legs)
    bet = Bet(rec.p_win, 0.0, 2.0 / unit, gid, loss=1.0 / unit - 1.0)
    w = slate_window(snap.games[gid])
    (f,) = optimize_slate([bet], s.kelly_fraction, s.cap_bet, budget.game_left(gid) / avail,
                          budget.day_left() / avail, groups=[([0], budget.window_left(w) / avail)])
    (cents,) = to_cents([f], avail, s.min_stake_cents)
    qty = min(qty0, math.floor(cents / 100 / unit))
    if qty < 1:
        return None
    cost = sum(effective_price(leg.price, qty, leg.fee_multiplier) for leg in rec.legs) * qty
    ev = qty * (1.0 + rec.p_win) / cost - 1.0
    stake = round(cost * 100)
    if ev < s.kalshi_min_ev or stake < s.min_stake_cents:
        return None
    return replace(rec, qty=qty, stake_cents=stake, ev=ev)


def middle_candidates(snap: Snapshot, s: Settings, budget: Budget | None = None) -> list[Recommendation]:
    """Ladder middles: not risk-free, priced on the empirical margin model only; the best per
    game. Observe-only unless middles_live, then Kelly-sized as partial-loss bets."""
    best: dict[str, Recommendation] = {}
    for a in kalshi_middles(snap.kalshi, s.middle_max_over):
        rec = _middle_rec(a, snap, s)
        if rec is None or rec.leg_key in snap.open_leg_keys:
            continue
        gid = rec.legs[0].game_id or ""
        if gid not in best or rec.ev > best[gid].ev:
            best[gid] = rec
    budget = budget or Budget.from_snap(snap, s)
    depth = {k: v for m in snap.kalshi for k, v in (((m.ticker, "yes"), m.yes_ask_size),
                                                    ((m.ticker, "no"), m.no_ask_size))}
    out = []
    for rec in best.values():
        if not rec.observe_only:
            rec = _size_middle(rec, snap, s, budget)
        if rec is not None:
            out.append(replace(rec, legs=_middle_legging(rec.legs, rec.qty or 0, rec.p_win, depth)))
    return out


def _anchored(r: Recommendation, snap: Snapshot, s: Settings) -> bool:
    """Is a Kalshi prop priced from sportsbooks (else the projector alone)?"""
    leg = r.legs[0]
    if r.kind != "kalshi_prop" or leg.game_id is None or leg.player is None or leg.stat is None \
            or leg.line is None:
        return True
    return bool(_book_quotes(snap, s, leg.game_id, leg.player, leg.stat))


def _extra_ev(r: Recommendation, snap: Snapshot, s: Settings, anchored: bool | None = None) -> float:
    """Extra EV a rec had to clear as a candidate: projector-only props need the model margin."""
    if r.kind != "kalshi_prop":
        return 0.0
    anchored = _anchored(r, snap, s) if anchored is None else anchored
    return 0.0 if anchored else s.pp_margin_model - s.pp_margin_market


def _kelly_scale(r: Recommendation, snap: Snapshot, s: Settings, anchored: bool) -> float:
    """Kelly multiplier per source (uncertainty shrinkage); game bets also take the league's CLV throttle."""
    if r.kind == "kalshi_prop":
        return s.shrink_prop_anchored if anchored else s.shrink_prop_model
    g = snap.games.get(r.legs[0].game_id or "")
    return s.shrink_game * (snap.clv_mult.get(g.league, 1.0) if g else 1.0)


def _copula(dirs: list[str | None], rho: float) -> list[list[float]] | None:
    """Same-game legs: +rho when they lean the same way, -rho when opposite (home/away, over/under)."""
    n = len(dirs)
    if rho <= 0 or n < 2:
        return None
    c = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    opposite = ({"home", "away"}, {"over", "under"})
    for i in range(n):
        for j in range(i + 1, n):
            a, b = dirs[i], dirs[j]
            if a is None or b is None or a.split("|")[0] != b.split("|")[0]:
                continue
            if a == b:
                c[i][j] = c[j][i] = rho
            elif {a.split("|")[1], b.split("|")[1]} in opposite:
                c[i][j] = c[j][i] = -rho
    return c


def _slate_cents(pool: list[Recommendation], ds: list[float], scales: list[float], snap: Snapshot,
                 s: Settings, budget: Budget) -> list[int]:
    """One joint Kelly pass: stakes in cents within the per-game, slate, day and same-direction headroom."""
    avail = budget.available
    bets = [Bet(r.legs[0].p_fair, 0.0, d, r.legs[0].game_id or "") for r, d in zip(pool, ds)]
    dirs = [direction_key(r.legs[0], snap.games.get(r.legs[0].game_id or "")) for r in pool]
    windows: dict[str, list[int]] = defaultdict(list)
    same_dir: dict[str, list[int]] = defaultdict(list)
    for i, b in enumerate(bets):
        if b.game_id in snap.games:
            windows[slate_window(snap.games[b.game_id])].append(i)
        if dirs[i] is not None:
            same_dir[dirs[i]].append(i)
    groups = ([(idx, budget.window_left(w) / avail) for w, idx in windows.items()]
              + [(idx, budget.dir_left(k) / avail) for k, idx in same_dir.items()])
    fractions = optimize_slate(bets, s.kelly_fraction, s.cap_bet, 1.0, budget.day_left() / avail,
                               scales=scales, game_caps={b.game_id: budget.game_left(b.game_id) / avail
                                                         for b in bets},
                               groups=groups, corr=_copula(dirs, s.copula_rho))
    return to_cents(fractions, avail, s.min_stake_cents)


def size_slate(recs: list[Recommendation], snap: Snapshot, s: Settings,
               budget: Budget | None = None) -> list[Recommendation]:
    """Jointly Kelly-size the non-observe Kalshi recs within the remaining cap headroom; drops any
    that round to nothing or fail a filter at the final quantity.

    Two passes: the first uses fee odds at the provisional quantity; the second re-sizes the
    survivors at the fee of their first-pass quantity, so budget freed by dropped bets is reused.
    """
    budget = budget or Budget.from_snap(snap, s)
    markets = {m.ticker: m for m in snap.kalshi}
    pool = [r for r in recs if r.kind in ("kalshi_game", "kalshi_prop") and not r.observe_only
            and r.legs[0].ticker in markets]
    if not pool or budget.available <= 0:
        return []
    depth_qty = [r.qty or 0 for r in pool]
    anchored = [_anchored(r, snap, s) for r in pool]
    ds = [(1 + r.ev) / r.legs[0].p_fair for r in pool]
    sized: list[Recommendation] = []
    for _ in range(2):
        scales = [_kelly_scale(r, snap, s, a) for r, a in zip(pool, anchored)]
        cents = _slate_cents(pool, ds, scales, snap, s, budget)
        keep: list[int] = []
        sized, next_ds = [], []
        for i, (r, c) in enumerate(zip(pool, cents)):
            leg = r.legs[0]
            m = markets[leg.ticker]
            qty = min(math.floor(c / 100 * ds[i]), depth_qty[i])  # stake incl. fee within the cents
            stake = round(qty * leg.price * 100)
            if qty < 1 or stake < s.min_stake_cents:
                continue
            # fees round up per order, so a smaller qty can cost more per contract: re-check every filter
            p_eff = effective_price(leg.price, qty, m.fee_multiplier)
            depth = m.yes_ask_size if leg.side == "yes" else m.no_ask_size
            ok, _ = passes_filters(leg.p_fair, leg.price, p_eff, depth, qty, s, m.status)
            ev = kalshi_ev(leg.p_fair, p_eff)
            if not ok or ev < s.kalshi_min_ev + _extra_ev(r, snap, s, anchored[i]):
                continue
            keep.append(i)
            next_ds.append(1 / p_eff)
            sized.append(replace(r, qty=qty, stake_cents=stake, ev=ev))
        pool = [pool[i] for i in keep]
        depth_qty = [depth_qty[i] for i in keep]
        anchored = [anchored[i] for i in keep]
        ds = next_ds
        if not pool:
            return []
    for r in sized:
        leg = r.legs[0]
        budget.charge(r.stake_cents, [leg.game_id or ""], [direction_key(leg, snap.games.get(leg.game_id or ""))])
    return sized


def scan(snap: Snapshot, s: Settings, projector: Projector,
         league_of: Callable[[KalshiMarket], str]) -> list[Recommendation]:
    """Arbs, then sized Kalshi recs, observe-only recs (stake 0), middles, then PrizePicks entries.
    Kalshi recs, middles and entries share one cap budget, in that order."""
    budget = Budget.from_snap(snap, s)
    kalshi = kalshi_game_candidates(snap, s) + kalshi_prop_candidates(snap, s, projector)
    observe = [replace(r, stake_cents=0) for r in kalshi if r.observe_only]
    sized = size_slate(kalshi, snap, s, budget)
    recs = (arb_candidates(snap, s, league_of) + sized + observe + middle_candidates(snap, s, budget)
            + pp_entry_candidates(snap, s, projector, budget))
    return [r for r in recs if r.leg_key not in snap.open_leg_keys]  # never re-recommend a placed bet
