"""Recommendation and bet lifecycle: dedupe, atomic confirm, expiry (integer cents)."""
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone, tzinfo

from gridiron_edge.db import Ledger, register_column, register_schema
from gridiron_edge.strategy.kalshi_pricing import taker_fee
from gridiron_edge.types import Leg, Recommendation

register_schema(
    """CREATE TABLE IF NOT EXISTS recommendations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        dedupe_key TEXT NOT NULL UNIQUE,
        leg_key TEXT NOT NULL,
        legs_json TEXT NOT NULL,
        stake_cents INTEGER NOT NULL,
        qty INTEGER,
        ev REAL NOT NULL,
        p_win REAL NOT NULL,
        structure TEXT,
        snapshot_ts TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        state TEXT NOT NULL,
        observe_only INTEGER NOT NULL DEFAULT 0,
        tie_risk INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL)""")
register_schema(
    """CREATE TABLE IF NOT EXISTS bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rec_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        legs_json TEXT NOT NULL,
        price REAL,
        qty INTEGER,
        stake_cents INTEGER NOT NULL,
        fee_cents INTEGER NOT NULL DEFAULT 0,
        structure TEXT,
        placed_at TEXT NOT NULL,
        state TEXT NOT NULL,
        result TEXT,
        payout_cents INTEGER,
        settled_at TEXT,
        clv REAL)""")
register_schema("CREATE INDEX IF NOT EXISTS idx_recs_leg_key ON recommendations (leg_key)")
# evaluation: market p at rec time and the close for every signal (shadow CLV), per-leg CLV for PrizePicks
for _col, _decl in (("p_mkt", "REAL"), ("p_close_fair", "REAL"), ("clv_close", "REAL"),
                    ("clv_source", "TEXT"), ("clv_stale", "INTEGER"), ("clv_at", "TEXT")):
    register_column("recommendations", _col, _decl)
register_column("bets", "p_close_fair", "REAL")
register_column("bets", "clv_source", "TEXT")
register_schema(
    """CREATE TABLE IF NOT EXISTS leg_clv (
        rec_id INTEGER NOT NULL,
        leg_idx INTEGER NOT NULL,
        p_rec REAL NOT NULL,
        p_close REAL NOT NULL,
        clv REAL NOT NULL,
        stale INTEGER NOT NULL DEFAULT 0,
        ts TEXT NOT NULL,
        PRIMARY KEY (rec_id, leg_idx))""")


class LifecycleError(Exception):
    pass


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _row_to_rec(r: sqlite3.Row) -> Recommendation:
    return Recommendation(
        id=r["id"], kind=r["kind"], legs=[Leg(**d) for d in json.loads(r["legs_json"])],
        stake_cents=r["stake_cents"], qty=r["qty"], ev=r["ev"], p_win=r["p_win"],
        structure=r["structure"], snapshot_ts=_dt(r["snapshot_ts"]), expires_at=_dt(r["expires_at"]),
        state=r["state"], observe_only=bool(r["observe_only"]), tie_risk=bool(r["tie_risk"]),
        p_mkt=r["p_mkt"] if "p_mkt" in r.keys() else None,
        why=json.loads(r["why_json"]) if "why_json" in r.keys() and r["why_json"] else None)


def _add_why_column(conn: sqlite3.Connection) -> None:
    """Older databases predate recommendations.why_json: add it (nullable, so existing rows are fine)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(recommendations)")}
    if cols and "why_json" not in cols:
        conn.execute("ALTER TABLE recommendations ADD COLUMN why_json TEXT")


class Lifecycle:
    def __init__(self, conn: sqlite3.Connection, ledger: Ledger):
        self.conn = conn
        self.ledger = ledger
        _add_why_column(conn)

    def upsert_recommendation(self, rec: Recommendation, now: datetime) -> tuple[Recommendation, bool]:
        row = self.conn.execute("SELECT id FROM recommendations WHERE dedupe_key=?",
                                (rec.dedupe_key,)).fetchone()
        if row:
            return self.get_rec(row["id"]), False
        state = "LOGGED" if rec.observe_only else "RECOMMENDED"
        with self.conn:
            self.conn.execute(
                "UPDATE recommendations SET state='EXPIRED' "
                "WHERE leg_key=? AND state IN ('RECOMMENDED','PENDING')", (rec.leg_key,))
            cur = self.conn.execute(
                "INSERT INTO recommendations (kind, dedupe_key, leg_key, legs_json, stake_cents, qty,"
                " ev, p_win, structure, snapshot_ts, expires_at, state, observe_only, tie_risk,"
                " created_at, p_mkt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec.kind, rec.dedupe_key, rec.leg_key, json.dumps([asdict(g) for g in rec.legs]),
                 rec.stake_cents, rec.qty, rec.ev, rec.p_win, rec.structure,
                 rec.snapshot_ts.isoformat(), rec.expires_at.isoformat(), state,
                 int(rec.observe_only), int(rec.tie_risk), now.isoformat(), rec.p_mkt))
            if rec.why is not None:
                self.conn.execute("UPDATE recommendations SET why_json=? WHERE id=?",
                                  (json.dumps(rec.why), cur.lastrowid))
        return self.get_rec(cur.lastrowid), True

    def get_rec(self, rec_id: int) -> Recommendation | None:
        r = self.conn.execute("SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
        return _row_to_rec(r) if r else None

    def mark_pending(self, rec_id: int) -> None:
        self.conn.execute("UPDATE recommendations SET state='PENDING' "
                          "WHERE id=? AND state='RECOMMENDED'", (rec_id,))

    def skip(self, rec_id: int) -> None:
        self.conn.execute("UPDATE recommendations SET state='SKIPPED' "
                          "WHERE id=? AND state IN ('RECOMMENDED','PENDING')", (rec_id,))

    def expire_due(self, now: datetime) -> list[int]:
        due = [r["id"] for r in self.conn.execute(
            "SELECT id, expires_at FROM recommendations WHERE state IN ('RECOMMENDED','PENDING')")
            if now >= _dt(r["expires_at"])]
        with self.conn:
            for i in due:
                self.conn.execute("UPDATE recommendations SET state='EXPIRED' WHERE id=?", (i,))
        return due

    def confirm(self, rec_id: int, now: datetime, price: float | None = None,
                qty: int | None = None, stake_cents: int | None = None) -> int:
        rec = self.get_rec(rec_id)
        if rec is None:
            raise LifecycleError("unknown id")
        if rec.state == "EXPIRED":
            # a price tick re-issues the rec under a new id; the id in the text should still work
            rec = self._live_successor(rec, now) or rec
        if rec.state not in ("RECOMMENDED", "PENDING"):
            raise LifecycleError("not pending")
        if now >= rec.expires_at:
            raise LifecycleError("expired")

        bet_price: float | None = None
        bet_qty = qty if qty is not None else rec.qty
        fee = 0
        if rec.kind == "pp_entry":
            cost = stake_cents if stake_cents is not None else rec.stake_cents
        elif rec.kind in ("arb", "middle"):
            # qty is shared by both legs; prices are the leg prices; fees use each market's multiplier
            cost = sum(round(bet_qty * g.price * 100) for g in rec.legs)
            fee = sum(round(taker_fee(bet_qty, g.price, g.fee_multiplier) * 100) for g in rec.legs)
        else:
            bet_price = price if price is not None else rec.legs[0].price
            cost = round(bet_qty * bet_price * 100)
            fee = round(taker_fee(bet_qty, bet_price, rec.legs[0].fee_multiplier) * 100)

        legs_json = json.dumps([asdict(g) for g in rec.legs])
        with self.conn:
            self.conn.execute("SAVEPOINT confirm_bet")
            try:
                cur = self.conn.execute(
                    "INSERT INTO bets (rec_id, kind, legs_json, price, qty, stake_cents, fee_cents,"
                    " structure, placed_at, state) VALUES (?,?,?,?,?,?,?,?,?,'PLACED')",
                    (rec.id, rec.kind, legs_json, bet_price, bet_qty, cost, fee, rec.structure,
                     now.isoformat()))
                bet_id = cur.lastrowid
                self.ledger.post("stake", -(cost + fee), bet_id=bet_id, note=f"rec {rec.id}")
                self.conn.execute("UPDATE recommendations SET state='PLACED' WHERE id=?", (rec.id,))
            except BaseException:
                self.conn.execute("ROLLBACK TO confirm_bet")
                self.conn.execute("RELEASE confirm_bet")
                raise
            self.conn.execute("RELEASE confirm_bet")
        return bet_id

    def _live_successor(self, rec: Recommendation, now: datetime) -> Recommendation | None:
        for r in self.conn.execute(
                "SELECT * FROM recommendations WHERE leg_key=? AND id>? "
                "AND state IN ('RECOMMENDED','PENDING') ORDER BY id DESC", (rec.leg_key, rec.id)):
            live = _row_to_rec(r)
            if now < live.expires_at:
                return live
        return None

    def pending(self) -> list[Recommendation]:
        return [_row_to_rec(r) for r in self.conn.execute(
            "SELECT * FROM recommendations WHERE state IN ('RECOMMENDED','PENDING') ORDER BY id")]

    def board(self, limit: int = 100) -> list[Recommendation]:
        return [_row_to_rec(r) for r in self.conn.execute(
            "SELECT * FROM recommendations ORDER BY id DESC LIMIT ?", (limit,))]

    def open_bets(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM bets WHERE state='PLACED' ORDER BY id").fetchall()

    def settled_bets(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM bets WHERE state!='PLACED' ORDER BY settled_at DESC, id DESC LIMIT ?",
            (limit,)).fetchall()

    def open_exposure_cents(self) -> int:
        return self.conn.execute(
            "SELECT COALESCE(SUM(stake_cents), 0) FROM bets WHERE state='PLACED'").fetchone()[0]

    def open_positions(self) -> list[tuple[str, int, list[Leg]]]:
        """(kind, stake_cents, legs) of every PLACED bet, for cumulative exposure caps."""
        return [(r["kind"], r["stake_cents"], [Leg(**d) for d in json.loads(r["legs_json"])])
                for r in self.open_bets()]

    def placed_today_cents(self, now: datetime, tz: tzinfo, exclude: tuple[str, ...] = ("arb", "middle")) -> int:
        """Stakes placed on the local calendar day of `now` (hedged kinds excluded)."""
        start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return sum(r["stake_cents"] for r in self.conn.execute("SELECT kind, stake_cents, placed_at FROM bets")
                   if r["kind"] not in exclude and start <= _dt(r["placed_at"]) < end)

    def pp_staked_today_cents(self, now: datetime, tz: tzinfo) -> int:
        start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return sum(r["stake_cents"] for r in self.conn.execute(
            "SELECT stake_cents, placed_at FROM bets WHERE kind='pp_entry'")
            if start <= _dt(r["placed_at"]) < end)
