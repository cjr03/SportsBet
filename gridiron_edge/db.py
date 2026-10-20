"""SQLite schema and the append-only paper-bankroll ledger (integer cents)."""
import re
import sqlite3
from datetime import datetime, timezone, tzinfo
from decimal import Decimal
from pathlib import Path

MAX_AMOUNT_CENTS = 10_000_000 * 100

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS quota (
        api TEXT PRIMARY KEY, remaining INTEGER, used INTEGER, updated_ts TEXT)""",
    """CREATE TABLE IF NOT EXISTS api_cache (
        key TEXT PRIMARY KEY, fetched_ts TEXT NOT NULL, payload TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('deposit','withdraw','stake','payout','refund')),
        amount_cents INTEGER NOT NULL,
        bet_id INTEGER,
        note TEXT NOT NULL DEFAULT '')""",
]


def register_schema(sql: str) -> None:
    """Later modules add their tables here at import; migrate() runs them."""
    if sql not in _SCHEMA:
        _SCHEMA.append(sql)


def migrate(conn: sqlite3.Connection) -> None:
    for sql in _SCHEMA:
        conn.execute(sql)


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # autocommit mode: transactions are explicit (SAVEPOINT), so post() nests safely
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    migrate(conn)
    return conn


class InsufficientFunds(Exception):
    pass


class InvalidAmount(Exception):
    pass


_AMOUNT_RE = re.compile(r"\d+(\.\d{1,2})?|\.\d{1,2}")


def parse_amount(text: str) -> int:
    """'100', '100.5', '$1,000.25' -> cents. Raises InvalidAmount otherwise."""
    s = text.strip().replace("$", "").replace(",", "")
    if not _AMOUNT_RE.fullmatch(s):
        raise InvalidAmount(f"not a valid dollar amount: {text!r}")
    cents = int(Decimal(s) * 100)
    if cents <= 0:
        raise InvalidAmount("amount must be greater than 0")
    if cents > MAX_AMOUNT_CENTS:
        raise InvalidAmount("amount is over $10,000,000")
    return cents


_SIGN = {"deposit": 1, "withdraw": -1, "stake": -1, "payout": 1, "refund": 1}


class Ledger:
    """Append-only: entries are only ever inserted."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def deposit(self, cents: int, note: str = "") -> int:
        return self.post("deposit", cents, note=note)

    def withdraw(self, cents: int, note: str = "") -> int:
        return self.post("withdraw", -cents, note=note)

    def post(self, kind: str, cents: int, bet_id: int | None = None, note: str = "") -> int:
        if kind not in _SIGN:
            raise InvalidAmount(f"unknown ledger kind: {kind!r}")
        if cents == 0 or (cents > 0) != (_SIGN[kind] > 0):
            raise InvalidAmount(f"{kind} amount has the wrong sign or is zero: {cents}")
        ts = datetime.now(timezone.utc).isoformat()
        self.conn.execute("SAVEPOINT ledger_post")
        try:
            if cents < 0 and self.balance() + cents < 0:
                raise InsufficientFunds(
                    f"{kind} of ${-cents / 100:,.2f} exceeds balance ${self.balance() / 100:,.2f}")
            cur = self.conn.execute(
                "INSERT INTO ledger (ts, kind, amount_cents, bet_id, note) VALUES (?,?,?,?,?)",
                (ts, kind, cents, bet_id, note))
        except BaseException:
            self.conn.execute("ROLLBACK TO ledger_post")
            self.conn.execute("RELEASE ledger_post")
            raise
        self.conn.execute("RELEASE ledger_post")
        return cur.lastrowid

    def balance(self) -> int:
        return self.conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM ledger").fetchone()[0]

    def history(self) -> list[tuple[datetime, int]]:
        out, total = [], 0
        for r in self.conn.execute("SELECT ts, amount_cents FROM ledger ORDER BY id"):
            total += r["amount_cents"]
            out.append((datetime.fromisoformat(r["ts"]), total))
        return out

    def today_change(self, tz: tzinfo) -> int:
        """Net ledger movement since local midnight in tz."""
        start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        start = start.astimezone(timezone.utc).isoformat()
        return self.conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM ledger WHERE ts >= ?", (start,)
        ).fetchone()[0]
