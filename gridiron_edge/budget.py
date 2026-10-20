"""Per-API quota tracking so polling can back off before credits run out."""
import sqlite3
from datetime import datetime, timezone


class BudgetGuard:
    def __init__(self, conn: sqlite3.Connection, api: str, monthly_cap: int, reserve_frac: float):
        self._conn = conn
        self.api = api
        self.monthly_cap = monthly_cap
        self.reserve_frac = reserve_frac

    def record(self, remaining: int, used: int, last: int) -> None:
        self._conn.execute(
            "INSERT INTO quota (api, ts, remaining, used, last) VALUES (?,?,?,?,?)",
            (self.api, datetime.now(timezone.utc).isoformat(), remaining, used, last),
        )

    def remaining(self) -> int | None:
        row = self._conn.execute(
            "SELECT remaining FROM quota WHERE api=? ORDER BY id DESC LIMIT 1", (self.api,)
        ).fetchone()
        return None if row is None else row["remaining"]

    @property
    def exhausted(self) -> bool:
        rem = self.remaining()
        return rem is not None and rem <= 0

    def can_spend(self, cost: int, essential: bool = False) -> bool:
        rem = self.remaining()
        if rem is None:
            return True
        if rem <= 0:
            return False
        floor = 0 if essential else self.reserve_frac * self.monthly_cap
        return rem - cost >= floor
