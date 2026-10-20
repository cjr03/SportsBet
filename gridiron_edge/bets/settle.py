"""Settlement of Kalshi, PrizePicks and arb bets: grading, payouts, voids (integer cents)."""
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal

from gridiron_edge.bets.clv import clv
from gridiron_edge.bets.lifecycle import Lifecycle, LifecycleError
from gridiron_edge.matching import GameRegistry
from gridiron_edge.strategy.kalshi_pricing import taker_fee
from gridiron_edge.strategy.prizepicks import PAYOUTS, revert
from gridiron_edge.types import Game, KalshiMarket, Leg

Result = Literal["won", "lost", "refund", "pending"]
VOID_AFTER = timedelta(hours=72)


def _yes_from_score(leg: Leg, market: KalshiMarket | None, game: Game) -> bool | None:
    """Did the leg's YES condition hold? None if it cannot be graded from the score."""
    if game.home_pts is None or game.away_pts is None:
        return None
    mtype = market.market_type if market else (
        "prop" if leg.player else "ml" if leg.line is None else "total" if leg.team is None else "spread")
    team = leg.team or (market.team if market else None)
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


def grade_pp_entry(legs: list[Leg], structure: tuple, stats: dict[tuple[str, str], float | None]
                   ) -> tuple[Result, float]:
    hits = dnps = ties = 0
    teams: set[str] = set()
    for leg in legs:
        key = (leg.player, leg.stat)
        if key not in stats:
            return "pending", 0.0
        v = stats[key]
        if v is None:
            dnps += 1
        elif v == leg.line:
            ties += 1
        else:
            teams.add(leg.team or leg.game_id or "")
            if (v > leg.line) == (leg.side == "over"):
                hits += 1
    new = revert(structure, dnps, ties, len(teams))
    if new is None:
        return "refund", 1.0
    mult = PAYOUTS[new].get(hits, 0)
    return ("won" if mult > 0 else "lost"), float(mult)


def _structure(s: str) -> tuple:
    n, kind = s.split("-")
    return int(n), kind


class Settler:
    def __init__(self, conn: sqlite3.Connection, lifecycle: Lifecycle, registry: GameRegistry):
        self.conn = conn
        self.lc = lifecycle
        self.registry = registry

    # -- grading ---------------------------------------------------------
    def _leg_refund_cents(self, bet: sqlite3.Row, leg: Leg) -> int:
        return round(bet["qty"] * leg.price * 100) + round(taker_fee(bet["qty"], leg.price, 1.0) * 100)

    def _grade_bet(self, bet: sqlite3.Row, now: datetime, kalshi_lookup, stat_lookup
                   ) -> tuple[Result, int]:
        legs = [Leg(**d) for d in json.loads(bet["legs_json"])]
        refund_all = bet["stake_cents"] + bet["fee_cents"]
        if bet["kind"] == "pp_entry":
            stats = {}
            for leg in legs:
                v = stat_lookup(leg.game_id, leg.player, leg.stat)
                if v != "pending":
                    stats[(leg.player, leg.stat)] = v
            result, mult = grade_pp_entry(legs, _structure(bet["structure"]), stats)
            if result == "pending" and any(
                    (g := self.registry.get(leg.game_id)) is not None and g.status != "final"
                    and now >= g.kickoff + VOID_AFTER for leg in legs):
                return "refund", refund_all
            if result == "refund":
                return result, refund_all
            return result, round(bet["stake_cents"] * mult)
        grades = [grade_kalshi_leg(leg, kalshi_lookup(leg.ticker), self.registry.get(leg.game_id), now)
                  for leg in legs]
        if "pending" in grades:
            return "pending", 0
        if bet["kind"] != "arb":
            result = grades[0]
            return result, (bet["qty"] * 100 if result == "won" else refund_all if result == "refund" else 0)
        payout = sum(bet["qty"] * 100 if g == "won" else self._leg_refund_cents(bet, leg) if g == "refund" else 0
                     for g, leg in zip(grades, legs))
        if all(g == "refund" for g in grades):
            return "refund", refund_all
        return ("won" if "won" in grades or payout > 0 else "lost"), payout

    # -- writing ---------------------------------------------------------
    def _write(self, bet_id: int, result: str, payout: int, now: datetime) -> None:
        with self.conn:
            self.conn.execute("SAVEPOINT settle_bet")
            try:
                cur = self.conn.execute(
                    "UPDATE bets SET state='SETTLED', result=?, payout_cents=?, settled_at=? "
                    "WHERE id=? AND state='PLACED'", (result, payout, now.isoformat(), bet_id))
                if cur.rowcount != 1:
                    raise LifecycleError("bet is not open")
                if payout > 0:
                    self.lc.ledger.post("refund" if result == "refund" else "payout", payout,
                                        bet_id=bet_id, note=result)
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
            result, payout = self._grade_bet(bet, now, kalshi_lookup, stat_lookup)
            if result == "pending":
                continue
            self._write(bet["id"], result, payout, now)
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

    def record_clv(self, bet_id: int, p_close_fair: float | None) -> float | None:
        bet = self.conn.execute("SELECT * FROM bets WHERE id=?", (bet_id,)).fetchone()
        if bet is None or bet["kind"] not in ("kalshi_game", "kalshi_prop") or not bet["qty"]:
            return None
        value = clv((bet["stake_cents"] + bet["fee_cents"]) / (bet["qty"] * 100), p_close_fair)
        if value is not None:
            self.conn.execute("UPDATE bets SET clv=? WHERE id=?", (value, bet_id))
        return value
