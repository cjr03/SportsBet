"""Settlement of Kalshi, PrizePicks and arb bets: grading, payouts, voids (integer cents)."""
import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal

from gridiron_edge.bets.clv import clv
from gridiron_edge.bets.lifecycle import Lifecycle, LifecycleError
from gridiron_edge.clients.kalshi import series_type
from gridiron_edge.matching import GameRegistry, kalshi_ticker_team
from gridiron_edge.strategy.kalshi_pricing import taker_fee
from gridiron_edge.strategy.prizepicks import PAYOUTS, revert
from gridiron_edge.types import Game, KalshiMarket, Leg

Result = Literal["won", "lost", "refund", "pending"]
log = logging.getLogger(__name__)
VOID_AFTER = timedelta(hours=72)


def _yes_from_score(leg: Leg, market: KalshiMarket | None, game: Game) -> bool | None:
    """Did the leg's YES condition hold? None if it cannot be graded from the score."""
    if game.home_pts is None or game.away_pts is None:
        return None
    kind = series_type(leg.ticker)  # a spread leg with no team must never be graded as a total
    mtype = market.market_type if market else kind if kind in ("spread", "total") else (
        "prop" if leg.player else "ml" if leg.line is None else "total" if leg.team is None else "spread")
    team = leg.team or (market.team if market else None)
    if team is None and mtype in ("ml", "spread"):
        team = kalshi_ticker_team(leg.ticker or "", game.away, game.home)
    line = leg.line if leg.line is not None else (market.line if market else None)
    if mtype == "total" and line is not None:
        return game.home_pts + game.away_pts > line
    if mtype in ("ml", "spread") and team in (game.home, game.away):
        margin = (game.home_pts - game.away_pts) if team == game.home else (game.away_pts - game.home_pts)
        return margin > (0 if mtype == "ml" else line if line is not None else 0)
    return None


def grade_kalshi_leg(leg: Leg, market: KalshiMarket | None, game: Game | None,
                     now: datetime) -> Result:
    if market is not None and market.result in ("yes", "no"):
        return "won" if market.result == leg.side else "lost"
    if game is not None:
        if game.status == "final":
            yes = _yes_from_score(leg, market, game)
            if yes is not None:
                return "won" if yes == (leg.side == "yes") else "lost"
        elif game.status == "void_candidate" or now >= game.kickoff + VOID_AFTER:
            return "refund"
    return "pending"


def grade_pp_entry(legs: list[Leg], structure: tuple, stats: dict[tuple[str, str], float | None],
                   payouts: dict = PAYOUTS) -> tuple[Result, float]:
    """Grade from the payout table in use (arena: only a perfect card pays automatically; a
    group prize for a non-perfect card is settled by hand)."""
    hits = dnps = ties = 0
    teams: set[str] = set()
    for leg in legs:
        key = (leg.player, leg.stat)
        if key not in stats:
            return "pending", 0.0
        v = stats[key]
        if v is None:
            dnps += 1
        elif abs(v - leg.line) < 1e-9:  # exactly on a whole-number line: a push, the pick is dropped
            ties += 1
        else:
            teams.add(leg.team or leg.game_id or "")
            if (v > leg.line) == (leg.side == "over"):
                hits += 1
    new = revert(structure, dnps, ties, len(teams))
    if new is None:
        return "refund", 1.0
    mult = payouts.get(new, {}).get(hits, 0)
    return ("won" if mult > 0 else "lost"), float(mult)


def _structure(s: str) -> tuple:
    n, kind = s.split("-")
    return int(n), kind


class Settler:
    def __init__(self, conn: sqlite3.Connection, lifecycle: Lifecycle, registry: GameRegistry,
                 payouts: dict | None = None):
        self.conn = conn
        self.lc = lifecycle
        self.registry = registry
        self.payouts = payouts if payouts is not None else PAYOUTS

    # -- grading ---------------------------------------------------------
    def _leg_refund_cents(self, bet: sqlite3.Row, leg: Leg) -> int:
        return round(bet["qty"] * leg.price * 100) + round(taker_fee(bet["qty"], leg.price, leg.fee_multiplier) * 100)

    def _grade_bet(self, bet: sqlite3.Row, now: datetime, kalshi_lookup, stat_lookup
                   ) -> tuple[Result, int, int]:
        """Return (result, payout_cents, refund_part_cents); refund_part is the share of an
        arb payout that is a refund of a voided leg rather than a winning payout."""
        legs = [Leg(**d) for d in json.loads(bet["legs_json"])]
        refund_all = bet["stake_cents"] + bet["fee_cents"]
        if bet["kind"] == "pp_entry":
            stats = {}
            for leg in legs:
                v = stat_lookup(leg.game_id, leg.player, leg.stat)
                if v != "pending":
                    stats[(leg.player, leg.stat)] = v
            result, mult = grade_pp_entry(legs, _structure(bet["structure"]), stats, self.payouts)
            if result == "pending" and any(
                    (g := self.registry.get(leg.game_id)) is not None and g.status != "final"
                    and now >= g.kickoff + VOID_AFTER for leg in legs):
                return "refund", refund_all, refund_all
            if result == "refund":
                return result, refund_all, refund_all
            return result, round(bet["stake_cents"] * mult), 0
        grades = [grade_kalshi_leg(leg, kalshi_lookup(leg.ticker), self.registry.get(leg.game_id), now)
                  for leg in legs]
        if "pending" in grades:
            return "pending", 0, 0
        if bet["kind"] not in ("arb", "middle"):  # a middle settles like a pair: each leg pays alone
            result = grades[0]
            if result == "won":
                return result, bet["qty"] * 100, 0
            return (result, refund_all, refund_all) if result == "refund" else (result, 0, 0)
        if all(g == "refund" for g in grades):
            return "refund", refund_all, refund_all
        won = sum(bet["qty"] * 100 for g in grades if g == "won")
        refunded = sum(self._leg_refund_cents(bet, leg) for g, leg in zip(grades, legs) if g == "refund")
        if bet["kind"] == "middle":  # won only if the pairs returned more than they cost
            return ("won" if won + refunded > refund_all else "lost"), won + refunded, refunded
        result = "won" if "won" in grades else "refund" if "refund" in grades else "lost"
        return result, won + refunded, refunded

    # -- writing ---------------------------------------------------------
    def _write(self, bet_id: int, result: str, payout: int, now: datetime,
               refund_part: int = 0) -> None:
        with self.conn:
            self.conn.execute("SAVEPOINT settle_bet")
            try:
                cur = self.conn.execute(
                    "UPDATE bets SET state='SETTLED', result=?, payout_cents=?, settled_at=? "
                    "WHERE id=? AND state='PLACED'", (result, payout, now.isoformat(), bet_id))
                if cur.rowcount != 1:
                    raise LifecycleError("bet is not open")
                if result == "refund":
                    refund_part = payout
                refund_part = min(max(refund_part, 0), payout)
                if payout - refund_part > 0:
                    self.lc.ledger.post("payout", payout - refund_part, bet_id=bet_id, note=result)
                if refund_part > 0:
                    self.lc.ledger.post("refund", refund_part, bet_id=bet_id, note=result)
            except BaseException:
                self.conn.execute("ROLLBACK TO settle_bet")
                self.conn.execute("RELEASE settle_bet")
                raise
            self.conn.execute("RELEASE settle_bet")

    def settle_ready(self, now: datetime,
                     kalshi_lookup: Callable[[str], KalshiMarket | None],
                     stat_lookup: Callable[[str, str, str], float | None | Literal["pending"]]
                     ) -> list[tuple[int, str, int]]:
        done = []
        for bet in self.lc.open_bets():
            try:
                result, payout, refund_part = self._grade_bet(bet, now, kalshi_lookup, stat_lookup)
                if result == "pending":
                    continue
                self._write(bet["id"], result, payout, now, refund_part)
            except (ValueError, KeyError, TypeError, AttributeError, LifecycleError, sqlite3.Error) as exc:
                log.warning("skipping bet %s: cannot settle (%s: %s)", bet["id"], type(exc).__name__, exc)
                continue
            done.append((bet["id"], result, payout))
        return done

    def manual_settle(self, bet_id: int, result: str, payout_cents: int | None = None,
                      now: datetime | None = None) -> None:
        bet = self.conn.execute("SELECT * FROM bets WHERE id=?", (bet_id,)).fetchone()
        if bet is None:
            raise LifecycleError("unknown id")
        if bet["state"] != "PLACED":
            raise LifecycleError("bet is not open")
        if result not in ("won", "lost", "refund"):
            raise LifecycleError("result must be won, lost or refund")
        if payout_cents is None:
            if result == "won":
                if bet["kind"] == "pp_entry":
                    raise LifecycleError("payout is required for a won PrizePicks entry")
                payout_cents = bet["qty"] * 100
            elif result == "refund":
                payout_cents = bet["stake_cents"] + bet["fee_cents"]
            else:
                payout_cents = 0
        if payout_cents < 0:
            raise LifecycleError("payout must not be negative")
        self._write(bet_id, result, payout_cents, now or datetime.now().astimezone())

    def record_clv(self, bet_id: int, p_close_fair: float | None, source: str | None = None) -> float | None:
        bet = self.conn.execute("SELECT * FROM bets WHERE id=?", (bet_id,)).fetchone()
        if bet is None or bet["kind"] not in ("kalshi_game", "kalshi_prop") or not bet["qty"]:
            return None
        value = clv((bet["stake_cents"] + bet["fee_cents"]) / (bet["qty"] * 100), p_close_fair)
        if value is not None:
            with self.conn:
                self.conn.execute("UPDATE bets SET clv=?, p_close_fair=?, clv_source=? WHERE id=?",
                                  (value, p_close_fair, source, bet_id))
        return value

    def record_rec_clv(self, rec_id: int, p_close_fair: float | None, now: datetime,
                       source: str | None = None, stale: bool = False) -> float | None:
        """Shadow CLV for any Kalshi rec (placed or not), from its quoted ask plus the taker fee."""
        rec = self.conn.execute("SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
        if rec is None or rec["kind"] not in ("kalshi_game", "kalshi_prop"):
            return None
        leg = json.loads(rec["legs_json"])[0]
        qty = rec["qty"] or 100
        cost = round(qty * leg["price"] * 100) + round(taker_fee(qty, leg["price"], 1.0) * 100)
        value = clv(cost / (qty * 100), p_close_fair)
        if value is not None:
            with self.conn:
                self.conn.execute(
                    "UPDATE recommendations SET p_close_fair=?, clv_close=?, clv_source=?, clv_stale=?, clv_at=? "
                    "WHERE id=?", (p_close_fair, value, source, int(stale), now.isoformat(), rec_id))
        return value

    def record_leg_clv(self, rec_id: int, leg_idx: int, p_rec: float | None, p_close: float | None,
                       now: datetime, stale: bool = False) -> float | None:
        """PrizePicks legs have no price: this is line movement, P(close)/P(at rec) - 1 for our side."""
        value = clv(p_rec, p_close)
        if value is not None:
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO leg_clv (rec_id, leg_idx, p_rec, p_close, clv, stale, ts) "
                    "VALUES (?,?,?,?,?,?,?)", (rec_id, leg_idx, p_rec, p_close, value, int(stale), now.isoformat()))
        return value
