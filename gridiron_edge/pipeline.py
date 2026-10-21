"""Recommendation pipeline: turn a market snapshot into Kalshi, PrizePicks and arb recommendations."""
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal, NamedTuple, Protocol
from zoneinfo import ZoneInfo

from gridiron_edge.config import Settings
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
)
from gridiron_edge.strategy.devig import devig_worst
from gridiron_edge.strategy.distributions import (
    EmpiricalMarginModel,
    MarginModel,
    implied_mean_from_line,
    prop_p_over,
)
from gridiron_edge.strategy.fair import fair_spread_mean, fair_total_mean, fair_two_way
from gridiron_edge.strategy.kalshi_pricing import effective_price, kalshi_ev, passes_filters, taker_fee
from gridiron_edge.strategy.kelly import Bet, optimize_slate, to_cents
from gridiron_edge.strategy.prizepicks import (
    Pick,
    allowed_structures,
    build_best_entry,
    hit_distribution,
    kelly_entry,
    payout_table,
)
from gridiron_edge.types import Game, KalshiMarket, Leg, PropQuote, Quote, Recommendation

_GAME_TYPES = ("ml", "spread", "total")
_PT = ZoneInfo("America/Los_Angeles")
_HEDGED = ("arb", "middle")  # capital tied up, but not directional risk on the game
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


def slate_window(g: Game) -> str:
    """A game's slate: its kickoff date in Pacific time."""
    return f"{g.kickoff.astimezone(_PT):%Y-%m-%d}"


def _leg_type(leg: Leg) -> str:
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
                description: str, snap: Snapshot, gate: str, tie_risk: bool,
                team: str | None = None) -> Recommendation:
    leg = Leg("kalshi", description, sd.side, sd.price, sd.p, g.game_id, ticker=m.ticker,
              player=m.player, stat=m.stat, line=m.line, team=team or m.team, fee_multiplier=m.fee_multiplier)
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
        tie = m.market_type == "ml" and ml_tie_risk(m, g.league)
        out.append(_kalshi_rec("kalshi_game", m, g, sd, desc, snap, m.market_type, tie))
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
    return [_kalshi_rec("kalshi_prop", m, g, sd, f"{m.player} {m.stat} over {m.line}", snap, "prop", False,
                        team=_player_team(snap, projector, m.player))
            for m, g, sd in best.values()]


def _player_team(snap: Snapshot, projector: Projector, player: str) -> str | None:
    return snap.player_teams.get(player) or projector.team_of(player)




def _pp_picks(snap: Snapshot, s: Settings, projector: Projector) -> list[Pick]:
    """Best side of every PrizePicks line, skipping players and games already in open entries."""
    picks: list[Pick] = []
    seen: set[tuple] = set()
    for q in snap.props:
        if q.book != "prizepicks":
            continue
        if q.multiplier not in (None, 1.0):  # a non-standard payout line (Demon/Goblin style)
            continue
        if q.player.lower() in snap.pp_open_players or q.game_id in snap.pp_open_games:
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
    return picks


def pp_entry_candidates(snap: Snapshot, s: Settings, projector: Projector,
                        budget: Budget | None = None) -> list[Recommendation]:
    """PrizePicks entries with no shared game or player, best Kelly growth first, until nothing
    qualifies, pp_max_entries is reached or the caps run out. Stakes come from the available
    bankroll, shrunk per pick source and while the payout table is unverified."""
    picks = _pp_picks(snap, s, projector)
    table, allowed = payout_table(s), allowed_structures(s)
    budget = budget or Budget.from_snap(snap, s)
    daily_left = math.floor(s.cap_pp_daily * snap.bankroll_cents) - snap.pp_staked_today_cents
    out: list[Recommendation] = []
    while picks and len(out) < s.pp_max_entries:
        found = build_best_entry(picks, s, allowed, payouts=table, brute=s.pp_brute_force)
        if found is None:
            break
        legs, st, ev, _ = found
        ps = [pk.p for pk in legs]
        games = [snap.games[pk.game_id] for pk in legs]
        observe = any(snap.gates.get((g.league, "prop"), False) for g in games)
        stake = 0
        if not observe:
            f, _ = kelly_entry(ps, st, table)
            mult = s.kelly_fraction * min(s.shrink_prop_anchored if pk.anchored else s.shrink_prop_model
                                          for pk in legs)
            if not s.pp_payouts_verified:
                mult *= s.pp_unverified_mult
            avail = budget.available
            cap = min(s.cap_bet * avail, daily_left, budget.cap_for(pk.game_id for pk in legs))
            stake = math.floor(min(f * mult * avail, cap))
            if stake < max(s.min_stake_cents, 1):
                break
            daily_left -= stake
            budget.charge(stake, {pk.game_id for pk in legs})
        dist = hit_distribution(ps)
        p_win = sum(dist[k] for k, mult in table[st].items() if mult > 1)
        out_legs = [Leg("prizepicks", f"{pk.player} {pk.side} {pk.line} {pk.stat}", pk.side, 0.0, pk.p,
                        pk.game_id, player=pk.player, stat=pk.stat, line=pk.line, team=pk.team)
                    for pk in legs]
        out.append(Recommendation(None, "pp_entry", out_legs, stake, None, ev, p_win, f"{st[0]}-{st[1]}",
                                  snap.now, min(g.kickoff for g in games), observe_only=observe))
        used_games = {pk.game_id for pk in legs}
        used_players = {pk.player.lower() for pk in legs}
        picks = [pk for pk in picks if pk.game_id not in used_games and pk.player.lower() not in used_players]
    return out


def _plain_legs(legs: list[Leg], snap: Snapshot, g: Game) -> list[Leg]:
    """Describe each Kalshi leg as the outcome its "Yes" pays on, not by ticker."""
    mtype = {m.ticker: m.market_type for m in snap.kalshi}
    return [replace(leg, description=contract_text(mtype.get(leg.ticker or "", "spread"), leg.team, leg.line,
                                                   g.away, g.home)) for leg in legs]


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
        if g is None or a.qty < 1 or a.cost <= 0:
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
        rec = Recommendation(None, "arb", _plain_legs(_legging(a.legs, qty, depth), snap, g), round(cost * 100),
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
    if g is None:
        return None
    mtype = "total" if a.legs[0].team is None else "spread"
    model = snap.margin_models.get(_MODEL_KEY[g.league][mtype])
    if not _empirical(model):  # a normal fallback understates key-number mass
        return None
    quotes = [q for q in snap.quotes if q.game_id == g.game_id]
    mean = (fair_total_mean if mtype == "total" else fair_spread_mean)(quotes, now=snap.now)
    if mean is None:
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
    return _book_p_sides(snap, s, leg.game_id, leg.player, leg.stat, leg.line) is not None


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
