"""Recommendation pipeline: turn a market snapshot into Kalshi, PrizePicks and arb recommendations."""
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal, NamedTuple, Protocol

from gridiron_edge.config import Settings
from gridiron_edge.models import Prediction
from gridiron_edge.models.blend import blend, default_weight
from gridiron_edge.strategy.arb import filter_arbs, kalshi_ladders, kalshi_ml_pairs
from gridiron_edge.strategy.devig import devig_worst
from gridiron_edge.strategy.distributions import MarginModel, implied_mean, implied_mean_from_line, prop_p_over
from gridiron_edge.strategy.fair import TIER_RANK, FairPrice, fair_price, implied_market_mean
from gridiron_edge.strategy.kalshi_pricing import effective_price, kalshi_ev, passes_filters
from gridiron_edge.strategy.kelly import Bet, optimize_slate, to_cents
from gridiron_edge.strategy.prizepicks import PAYOUTS, Pick, build_best_entry, hit_distribution, kelly_entry
from gridiron_edge.types import Game, KalshiMarket, Leg, PropQuote, Quote, Recommendation

_GAME_TYPES = ("ml", "spread", "total")
# league -> market type -> margin_models key
_MODEL_KEY = {
    "nfl": {"ml": "nfl_ml", "spread": "nfl_spread", "total": "nfl_total"},
    "cfb": {"ml": "cfb", "spread": "cfb_spread", "total": "cfb"},
}
_MODEL_FALLBACK = {"cfb_spread": "cfb"}  # no CFB lines history cached: the normal model
_TIE_NO = re.compile(r"tie[^.]*\b(resolves?|settles?)\b[^.]*\bno\b")


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

    def why(self) -> dict:
        return {"fair_source": self.fair.source, "books": list(self.fair.books),
                "p_market": round(self.p_market, 4),
                "p_model": None if self.p_model is None else round(self.p_model, 4),
                "model_weight": round(self.model_weight, 4), "method": self.method,
                "tie_prob": self.tie_prob}


def _sharp_at(quotes: list[Quote], market: str, threshold: float, model: MarginModel,
              kw: dict) -> tuple[float, FairPrice, str] | None:
    """P(home margin / total > threshold) straight from a sharp quote at the line or a half point away.

    A quote exactly at a half-point threshold is the answer. A quote at an adjacent integer line is
    P(cover | no push) there, so it goes through the push-aware inversion before pricing.
    """
    def at(x: float) -> FairPrice | None:
        fp = fair_price(quotes, market, -x if market == "spreads" else x, **kw)
        return fp if fp is not None and fp.tier < TIER_RANK["median"] else None

    if threshold != math.floor(threshold) and (fp := at(threshold)) is not None:
        return fp.p, fp, "exact line"
    best: tuple[int, float, float, FairPrice] | None = None
    for x in (threshold - 0.5, threshold + 0.5):
        if (fp := at(x)) is not None and (best is None or (fp.tier, abs(fp.p - 0.5)) < best[:2]):
            best = (fp.tier, abs(fp.p - 0.5), x, fp)
    if best is None:
        return None
    _, _, x, fp = best
    mean = implied_mean(model, x, fp.p)
    return None if mean is None else (model.p_greater(mean, threshold), fp, "adjacent line")


def kalshi_tie_rule(m: KalshiMarket) -> str | None:
    """"no" when the market's rules say a tie settles every team's market NO; None when unknown."""
    text = (getattr(m, "rules_secondary", "") or "").lower()
    if "tie" in text and _TIE_NO.search(text):
        return "no"
    return None


def _game_fair(m: KalshiMarket, g: Game, snap: Snapshot, s: Settings,
               cache: dict | None = None) -> GameFair | None:
    """Blended P(YES) for a Kalshi game market, or None when inputs are missing or CFB agrees.

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
            p_a, fp, method = direct
            p_mkt = p_a if (mtype == "total" or home_side) else 1.0 - p_a
        elif im is not None:
            fp, method = im.fair, "implied mean"
            p_mkt = _p_yes(mtype, model, im.mean, m.line, home_side)
        else:
            return None

    pred = snap.predictions.get(g.game_id)
    model_mean = None if pred is None else (pred.mean_total if mtype == "total" else pred.mean_margin)
    if g.league == "cfb":
        # CFB bets need the model to disagree with the line; without both means we can't tell.
        if model_mean is None or mkt_mean is None or abs(model_mean - mkt_mean) < s.cfb_min_disagree:
            return None
    p_model, w = None, 0.0
    p = p_mkt
    if model_mean is not None:
        p_model = _p_yes(mtype, model, model_mean, m.line, home_side)
        hours = (g.kickoff - snap.now).total_seconds() / 3600
        w = default_weight(g.league, hours)
        p = blend(p_model, p_mkt, w)
    p_no, tie = 1.0 - p, 0.0
    if g.league == "nfl" and mtype == "ml":
        # sportsbook moneylines and the model are P(win | no tie); a tie loses YES for both teams.
        # Unless the rules confirm a tie settles NO, a tie loses NO too (worst case per side).
        tie = s.nfl_tie_prob
        p_yes = p * (1.0 - tie)
        p_no = 1.0 - p_yes if kalshi_tie_rule(m) == "no" else (1.0 - p) * (1.0 - tie)
        p = p_yes
    return GameFair(p, p_no, p_mkt, p_model, w, fp, method, tie)


def _game_p_yes(m: KalshiMarket, g: Game, snap: Snapshot, s: Settings) -> float | None:
    """Blended P(YES) for a Kalshi game market, or None when inputs are missing or CFB agrees."""
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
                observe: bool = False) -> Recommendation:
    leg = Leg("kalshi", description, sd.side, sd.price, sd.p, g.game_id, ticker=m.ticker,
              player=m.player, stat=m.stat, line=m.line, team=m.team)
    return Recommendation(None, kind, [leg], 0, sd.qty, sd.ev, sd.p, None, snap.now, g.kickoff,
                          observe_only=observe or snap.gates.get((g.league, gate), False), tie_risk=tie_risk,
                          why=why)


def kalshi_game_candidates(snap: Snapshot, s: Settings) -> list[Recommendation]:
    """Best Kalshi ml/spread/total recommendation per game (unsized: stake 0, provisional qty)."""
    best: dict[str, tuple[KalshiMarket, Game, _Side, GameFair]] = {}
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
        sd = _best_side(m, gf.p_yes, snap, s, p_no=gf.p_no)
        if sd is not None and (g.game_id not in best or sd.ev > best[g.game_id][2].ev):
            best[g.game_id] = (m, g, sd, gf)

    out = []
    for m, g, sd, gf in best.values():
        desc = {"ml": f"{m.team} ML", "spread": f"{m.team} wins by >{m.line}",
                "total": f"Total over {m.line}"}[m.market_type]
        tie = g.league == "nfl" and m.market_type == "ml"
        # soft-book median only: no sharp price behind it, so log it rather than stake it
        observe = gf.fair.source == "median" and s.median_fair_policy == "observe"
        out.append(_kalshi_rec("kalshi_game", m, g, sd, desc, snap, m.market_type, tie, gf.why(), observe))
    return out


def _stat_kind(stat: str) -> Literal["yards", "count"]:
    return "yards" if stat.endswith("_yds") else "count"


def _book_p_sides(snap: Snapshot, s: Settings, game_id: str, player: str, stat: str,
                  line: float) -> tuple[float, float] | None:
    """(P(over), P(under)) at the line from the sportsbook nearest it, each side worst-case.

    Each side is devigged with its own most conservative method and shifted to the line through
    its own implied mean, so neither side is the optimistic complement of the other and
    P(over) + P(under) <= 1.
    """
    pairs: dict[tuple[str, float], dict[str, PropQuote]] = {}
    for q in snap.props:
        if (q.book == "prizepicks" or q.decimal is None or q.game_id != game_id or q.stat != stat
                or q.player.lower() != player.lower()
                or (snap.now - q.last_update).total_seconds() > s.stale_seconds):
            continue
        sides = pairs.setdefault((q.book, q.point), {})
        if q.side not in sides or q.last_update > sides[q.side].last_update:
            sides[q.side] = q
    both = [(k, v) for k, v in pairs.items() if "over" in v and "under" in v]
    if not both:
        return None
    (_, point), sides = min(both, key=lambda kv: (abs(kv[0][1] - line), kv[0][0]))
    decimals = [sides["over"].decimal, sides["under"].decimal]
    p_over_at_point = devig_worst(decimals, 0)
    p_under_at_point = devig_worst(decimals, 1)
    kind = _stat_kind(stat)
    try:  # a separate implied mean for each side's worst-case probability
        mean_over = implied_mean_from_line(point, p_over_at_point, kind)
        mean_under = implied_mean_from_line(point, 1.0 - p_under_at_point, kind)
    except ValueError:  # no mean reproduces this probability
        return None
    return prop_p_over(line, mean_over, kind), 1.0 - prop_p_over(line, mean_under, kind)


def _model_p_over(projector: Projector, g: Game, player: str, stat: str, line: float) -> float | None:
    if g.week is None:
        return None
    proj = projector.project(player, stat, g.season, g.week)
    if proj is None:
        return None
    mean, sd = proj
    kind = _stat_kind(stat)
    return prop_p_over(line, mean, kind, sd if sd > 0 else None)


def _prop_p_sides(snap: Snapshot, s: Settings, projector: Projector, g: Game, player: str, stat: str,
                  line: float) -> tuple[float, float, bool] | None:
    """(P(over), P(under), anchored): sportsbook-anchored when possible, else the projector."""
    sides = _book_p_sides(snap, s, g.game_id, player, stat, line)
    if sides is not None:
        return sides[0], sides[1], True
    p = _model_p_over(projector, g, player, stat, line)
    return None if p is None else (p, 1.0 - p, False)


def kalshi_prop_candidates(snap: Snapshot, s: Settings, projector: Projector) -> list[Recommendation]:
    """Best Kalshi prop recommendation per player (unsized)."""
    best: dict[str, tuple[KalshiMarket, Game, _Side]] = {}
    for m in snap.kalshi:
        if m.market_type != "prop" or m.player is None or m.stat is None or m.line is None:
            continue
        if m.player.lower() in snap.open_players:  # already holding a prop on this player
            continue
        g = _upcoming(snap, m.game_id)
        if g is None:
            continue
        got = _prop_p_sides(snap, s, projector, g, m.player, m.stat, m.line)
        if got is None:
            continue
        p_over, p_under, anchored = got
        extra = 0.0 if anchored else s.pp_margin_model - s.pp_margin_market
        sd = _best_side(m, p_over, snap, s, extra, p_no=p_under)
        key = m.player.lower()
        if sd is not None and (key not in best or sd.ev > best[key][2].ev):
            best[key] = (m, g, sd)
    return [_kalshi_rec("kalshi_prop", m, g, sd, f"{m.player} {m.stat} over {m.line}", snap, "prop", False)
            for m, g, sd in best.values()]


def _player_team(snap: Snapshot, projector: Projector, player: str) -> str | None:
    return snap.player_teams.get(player) or projector.team_of(player)


def pp_entry_candidates(snap: Snapshot, s: Settings, projector: Projector) -> list[Recommendation]:
    """At most one PrizePicks entry: the best structure and legs by Kelly growth."""
    picks: list[Pick] = []
    seen: set[tuple] = set()
    for q in snap.props:
        if q.book != "prizepicks":
            continue
        key = (q.game_id, q.player.lower(), q.stat, q.point)
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

    found = build_best_entry(picks, s)
    if found is None:
        return []
    legs, st, ev, _ = found
    ps = [pk.p for pk in legs]
    games = [snap.games[pk.game_id] for pk in legs]
    observe = any(snap.gates.get((g.league, "prop"), False) for g in games)
    stake = 0
    if not observe:
        f, _ = kelly_entry(ps, st)
        bank = snap.bankroll_cents
        cap = min(s.cap_bet * bank, s.cap_pp_daily * bank - snap.pp_staked_today_cents)
        stake = math.floor(min(f * s.kelly_fraction * bank, cap))
        if stake < max(s.min_stake_cents, 1):
            return []
    dist = hit_distribution(ps)
    p_win = sum(dist[k] for k, mult in PAYOUTS[st].items() if mult > 1)
    out_legs = [Leg("prizepicks", f"{pk.player} {pk.side} {pk.line} {pk.stat}", pk.side, 0.0, pk.p,
                    pk.game_id, player=pk.player, stat=pk.stat, line=pk.line, team=pk.team)
                for pk in legs]
    return [Recommendation(None, "pp_entry", out_legs, stake, None, ev, p_win, f"{st[0]}-{st[1]}",
                           snap.now, min(g.kickoff for g in games), observe_only=observe)]


def arb_candidates(snap: Snapshot, s: Settings, league_of: Callable[[KalshiMarket], str]) -> list[Recommendation]:
    """Kalshi ML-pair and ladder arbs, sized to the arb bankroll fraction."""
    fees = {m.ticker: m.fee_multiplier for m in snap.kalshi}
    bankroll = snap.bankroll_cents / 100
    out = []
    for a in filter_arbs(kalshi_ml_pairs(snap.kalshi, league_of) + kalshi_ladders(snap.kalshi), s.arb_max_roi):
        g = _upcoming(snap, a.game_id)
        if g is None or a.qty < 1 or a.cost <= 0:
            continue
        qty = min(a.qty, math.floor(s.arb_fraction * bankroll / (a.cost / a.qty)))
        if qty < 1:
            continue
        # fees are rounded per order, so re-price at the smaller size
        cost = sum(effective_price(leg.price, qty, fees[leg.ticker]) for leg in a.legs) * qty
        if cost <= 0:
            continue
        roi = qty / cost - 1
        if roi <= 0:
            continue
        rec = Recommendation(None, "arb", a.legs, round(cost * 100), qty, roi, 1.0, a.kind,
                             snap.now, g.kickoff, tie_risk=a.tie_risk)
        if rec.leg_key in snap.open_leg_keys:  # this arb is already placed
            continue
        out.append(rec)
    return out


def size_slate(recs: list[Recommendation], snap: Snapshot, s: Settings) -> list[Recommendation]:
    """Jointly Kelly-size the non-observe Kalshi recs; drops any that round to nothing."""
    markets = {m.ticker: m for m in snap.kalshi}
    pool = [r for r in recs if r.kind in ("kalshi_game", "kalshi_prop") and not r.observe_only]
    bets = []
    for r in pool:
        leg = r.legs[0]
        p_eff = leg.p_fair / (1 + r.ev)
        bets.append(Bet(leg.p_fair, 0.0, 1 / p_eff, leg.game_id))
    available = max(0, snap.bankroll_cents - snap.open_exposure_cents)
    fractions = optimize_slate(bets, s.kelly_fraction, s.cap_bet, s.cap_game, s.cap_slate)
    out = []
    for r, b, cents in zip(pool, bets, to_cents(fractions, available, s.min_stake_cents)):
        leg = r.legs[0]
        # stay within the depth-capped provisional size
        qty = min(math.floor(cents / 100 / (1 / b.d)), r.qty or 0)
        stake = round(qty * leg.price * 100)
        if qty < 1 or stake < s.min_stake_cents:
            continue
        m = markets.get(leg.ticker)
        if m is None:
            continue
        # fees round up per order, so a smaller qty can cost more per contract: re-check every filter
        p_eff = effective_price(leg.price, qty, m.fee_multiplier)
        depth = m.yes_ask_size if leg.side == "yes" else m.no_ask_size
        ok, _ = passes_filters(leg.p_fair, leg.price, p_eff, depth, qty, s, m.status)
        ev = kalshi_ev(leg.p_fair, p_eff)
        if not ok or ev < s.kalshi_min_ev + _extra_ev(r, snap, s):
            continue
        out.append(replace(r, qty=qty, stake_cents=stake, ev=ev))
    return out


def _extra_ev(r: Recommendation, snap: Snapshot, s: Settings) -> float:
    """Extra EV a rec had to clear as a candidate: projector-only props need the model margin."""
    leg = r.legs[0]
    if r.kind != "kalshi_prop" or leg.game_id is None or leg.player is None or leg.stat is None \
            or leg.line is None:
        return 0.0
    anchored = _book_p_sides(snap, s, leg.game_id, leg.player, leg.stat, leg.line) is not None
    return 0.0 if anchored else s.pp_margin_model - s.pp_margin_market


def scan(snap: Snapshot, s: Settings, projector: Projector,
         league_of: Callable[[KalshiMarket], str]) -> list[Recommendation]:
    """Arbs, then sized Kalshi recs, then observe-only recs (stake 0), then the PrizePicks entry."""
    kalshi = kalshi_game_candidates(snap, s) + kalshi_prop_candidates(snap, s, projector)
    observe = [replace(r, stake_cents=0) for r in kalshi if r.observe_only]
    recs = (arb_candidates(snap, s, league_of) + size_slate(kalshi, snap, s) + observe
            + pp_entry_candidates(snap, s, projector))
    return [r for r in recs if r.leg_key not in snap.open_leg_keys]  # never re-recommend a placed bet
