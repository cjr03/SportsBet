"""Recommendation pipeline: turn a market snapshot into Kalshi, PrizePicks and arb recommendations."""
import inspect
import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal, NamedTuple, Protocol

import numpy as np

from gridiron_edge.config import Settings
from gridiron_edge.matching import normalize_name
from gridiron_edge.models import Prediction
from gridiron_edge.models.blend import blend, default_weight
from gridiron_edge.strategy.arb import filter_arbs, kalshi_ladders, kalshi_ml_pairs
from gridiron_edge.strategy.devig import devig_worst
from gridiron_edge.strategy.distributions import MarginModel, implied_mean_from_line, prop_p_over
from gridiron_edge.strategy.fair import fair_spread_mean, fair_total_mean, fair_two_way
from gridiron_edge.strategy.kalshi_pricing import effective_price, kalshi_ev, passes_filters
from gridiron_edge.strategy.kelly import Bet, optimize_slate, to_cents
from gridiron_edge.strategy.prizepicks import PAYOUTS, Pick, build_best_entry, hit_distribution, kelly_entry
from gridiron_edge.types import Game, KalshiMarket, Leg, PropQuote, Quote, Recommendation

_GAME_TYPES = ("ml", "spread", "total")
# league -> market type -> margin_models key
_MODEL_KEY = {
    "nfl": {"ml": "nfl_ml", "spread": "nfl_spread", "total": "nfl_total"},
    "cfb": {"ml": "cfb", "spread": "cfb", "total": "cfb"},
}


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
    # projector-only props may stake only once the projector's backtest shows an edge
    projector_live: bool = False


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


def _game_p_yes(m: KalshiMarket, g: Game, snap: Snapshot, s: Settings) -> float | None:
    """Blended P(YES) for a Kalshi game market, or None when inputs are missing or CFB agrees."""
    mtype = m.market_type
    if mtype != "total" and m.team not in (g.home, g.away):
        return None
    if mtype != "ml" and m.line is None:
        return None
    home_side = m.team == g.home
    quotes = [q for q in snap.quotes if q.game_id == g.game_id]
    model = snap.margin_models[_MODEL_KEY[g.league][mtype]]
    mkt_mean = (fair_total_mean if mtype == "total" else fair_spread_mean)(quotes, now=snap.now)
    if mtype == "ml":
        fair = fair_two_way(quotes, "h2h", None, now=snap.now, stale_seconds=s.stale_seconds)
        if fair is None:
            return None
        p_mkt = fair[0] if home_side else 1.0 - fair[0]
    else:
        if mkt_mean is None:
            return None
        p_mkt = _p_yes(mtype, model, mkt_mean, m.line, home_side)

    pred = snap.predictions.get(g.game_id)
    model_mean = None if pred is None else (pred.mean_total if mtype == "total" else pred.mean_margin)
    if g.league == "cfb":
        # CFB bets need the model to disagree with the line; without both means we can't tell.
        if model_mean is None or mkt_mean is None or abs(model_mean - mkt_mean) < s.cfb_min_disagree:
            return None
    if model_mean is None:
        return p_mkt
    p_model = _p_yes(mtype, model, model_mean, m.line, home_side)
    hours = (g.kickoff - snap.now).total_seconds() / 3600
    return blend(p_model, p_mkt, default_weight(g.league, hours))


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
                description: str, snap: Snapshot, gate: str, tie_risk: bool) -> Recommendation:
    leg = Leg("kalshi", description, sd.side, sd.price, sd.p, g.game_id, ticker=m.ticker,
              player=m.player, stat=m.stat, line=m.line, team=m.team)
    return Recommendation(None, kind, [leg], 0, sd.qty, sd.ev, sd.p, None, snap.now, g.kickoff,
                          observe_only=snap.gates.get((g.league, gate), False), tie_risk=tie_risk)


def kalshi_game_candidates(snap: Snapshot, s: Settings) -> list[Recommendation]:
    """Best Kalshi ml/spread/total recommendation per game (unsized: stake 0, provisional qty)."""
    best: dict[str, tuple[KalshiMarket, Game, _Side]] = {}
    for m in snap.kalshi:
        if m.market_type not in _GAME_TYPES:
            continue
        g = _upcoming(snap, m.game_id)
        if g is None or g.game_id in snap.open_game_ids:  # one market per game, already held
            continue
        p_yes = _game_p_yes(m, g, snap, s)
        if p_yes is None:
            continue
        sd = _best_side(m, p_yes, snap, s)
        if sd is not None and (g.game_id not in best or sd.ev > best[g.game_id][2].ev):
            best[g.game_id] = (m, g, sd)

    out = []
    for m, g, sd in best.values():
        desc = {"ml": f"{m.team} ML", "spread": f"{m.team} wins by >{m.line}",
                "total": f"Total over {m.line}"}[m.market_type]
        tie = g.league == "nfl" and m.market_type == "ml"
        out.append(_kalshi_rec("kalshi_game", m, g, sd, desc, snap, m.market_type, tie))
    return out


def _stat_kind(stat: str) -> Literal["yards", "count"]:
    return "yards" if stat.endswith("_yds") else "count"


def _max_shift(stat: str, s: Settings) -> float:
    if stat == "pass_yds":
        return s.props_max_shift_pass_yds
    return s.props_max_shift_yds if _stat_kind(stat) == "yards" else s.props_max_shift_count


def _prop_fresh(q: PropQuote, snap: Snapshot, s: Settings) -> bool:
    """Prop lines are pulled once near kickoff: age them from fetch time against the props TTL."""
    return (snap.now - q.seen_at).total_seconds() <= s.props_stale_seconds


def _book_quotes(snap: Snapshot, s: Settings, game_id: str, player: str, stat: str) -> list[PropQuote]:
    """Fresh sportsbook (non-PrizePicks) prop quotes for one player and stat."""
    key = normalize_name(player)
    return [q for q in snap.props
            if q.book != "prizepicks" and q.decimal is not None and q.game_id == game_id and q.stat == stat
            and normalize_name(q.player) == key and _prop_fresh(q, snap, s)]


def _book_p_sides(snap: Snapshot, s: Settings, game_id: str, player: str, stat: str,
                  line: float) -> tuple[float, float] | None:
    """(P(over), P(under)) at the line from the consensus of books at the point nearest it.

    Each book's sides are devigged with their own most conservative method; the anchor is the median
    across books of each side (so P(over) + P(under) <= 1). Each side is then shifted to the line
    through its own implied mean, never further than the stat's max shift.
    """
    pairs: dict[tuple[str, float], dict[str, PropQuote]] = {}
    for q in _book_quotes(snap, s, game_id, player, stat):
        sides = pairs.setdefault((q.book, q.point), {})
        if q.side not in sides or q.last_update > sides[q.side].last_update:
            sides[q.side] = q
    both = {k: v for k, v in pairs.items() if "over" in v and "under" in v}
    if not both:
        return None
    point = min({pt for _, pt in both}, key=lambda pt: (abs(pt - line), pt))
    if abs(line - point) > _max_shift(stat, s):
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
    return (prop_p_over(line, mean_over, kind, stat=stat),
            1.0 - prop_p_over(line, mean_under, kind, stat=stat))


def _game_context(snap: Snapshot, projector: Projector, g: Game, player: str) -> dict:
    """Opponent and market game script (team implied total, team spread) for the projector."""
    team = _player_team(snap, projector, player)
    if team not in (g.home, g.away):
        return {}
    home = team == g.home
    quotes = [q for q in snap.quotes if q.game_id == g.game_id]
    margin = fair_spread_mean(quotes, now=snap.now)  # expected home margin
    total = fair_total_mean(quotes, now=snap.now)
    ctx: dict = {"opponent": g.away if home else g.home}
    if margin is not None:
        ctx["team_spread"] = -margin if home else margin
        if total is not None:
            ctx["team_total"] = (total + (margin if home else -margin)) / 2
    return ctx


def _model_p_over(projector: Projector, g: Game, player: str, stat: str, line: float,
                  ctx: dict | None = None) -> float | None:
    if g.week is None:
        return None
    if ctx and "opponent" in inspect.signature(projector.project).parameters:
        proj = projector.project(player, stat, g.season, g.week, **ctx)
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


def kalshi_prop_candidates(snap: Snapshot, s: Settings, projector: Projector) -> list[Recommendation]:
    """Best Kalshi prop recommendation per player (unsized). Projector-only picks are observe-only
    until the projector passes its backtest (snap.projector_live)."""
    best: dict[str, tuple[KalshiMarket, Game, _Side, bool]] = {}
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
        extra = 0.0 if anchored else s.pp_margin_model - s.pp_margin_market
        sd = _best_side(m, p_over, snap, s, extra, p_no=p_under)
        if sd is not None and (key not in best or sd.ev > best[key][2].ev):
            best[key] = (m, g, sd, anchored)
    out = []
    for m, g, sd, anchored in best.values():
        r = _kalshi_rec("kalshi_prop", m, g, sd, f"{m.player} {m.stat} over {m.line}", snap, "prop", False)
        if not anchored and not snap.projector_live:
            r = replace(r, observe_only=True)
        out.append(r)
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


def pp_entry_candidates(snap: Snapshot, s: Settings, projector: Projector) -> list[Recommendation]:
    """At most one PrizePicks entry: the best structure and legs by Kelly growth.

    Until the projector passes its backtest, the live entry uses book-anchored legs only; if none can
    be built, an entry including projector legs is logged observe-only.
    """
    picks: list[Pick] = []
    seen: set[tuple] = set()
    for q in snap.props:
        if q.book != "prizepicks" or not _pp_standard(q):
            continue
        if q.source != "manual" and not _prop_fresh(q, snap, s):
            continue
        key = (q.game_id, normalize_name(q.player), q.stat, q.point)
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

    unproven = False
    if snap.projector_live:
        found = build_best_entry(picks, s)
    else:
        found = build_best_entry([pk for pk in picks if pk.anchored], s)
        if found is None:
            found, unproven = build_best_entry(picks, s), True
    if found is None:
        return []
    legs, st, ev, _ = found
    ps = [pk.p for pk in legs]
    games = [snap.games[pk.game_id] for pk in legs]
    observe = unproven or any(snap.gates.get((g.league, "prop"), False) for g in games)
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
    anchored = bool(_book_quotes(snap, s, leg.game_id, leg.player, leg.stat))
    return 0.0 if anchored else s.pp_margin_model - s.pp_margin_market


def scan(snap: Snapshot, s: Settings, projector: Projector,
         league_of: Callable[[KalshiMarket], str]) -> list[Recommendation]:
    """Arbs, then sized Kalshi recs, then observe-only recs (stake 0), then the PrizePicks entry."""
    kalshi = kalshi_game_candidates(snap, s) + kalshi_prop_candidates(snap, s, projector)
    observe = [replace(r, stake_cents=0) for r in kalshi if r.observe_only]
    recs = (arb_candidates(snap, s, league_of) + size_slate(kalshi, snap, s) + observe
            + pp_entry_candidates(snap, s, projector))
    return [r for r in recs if r.leg_key not in snap.open_leg_keys]  # never re-recommend a placed bet
