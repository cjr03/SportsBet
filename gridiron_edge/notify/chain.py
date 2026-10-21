"""Try notification channels in order, with dedupe, quiet hours and an attempt log."""
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from gridiron_edge.bets import lifecycle as _lifecycle  # noqa: F401 - registers the recommendations table
from gridiron_edge.db import register_schema
from gridiron_edge.notify import NotifyResult
from gridiron_edge.describe import DEFAULT_NAMES
from gridiron_edge.notify.format import PT, format_message
from gridiron_edge.types import Recommendation

register_schema(
    """CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, rec_id INTEGER,
        dedupe_key TEXT, channel TEXT NOT NULL, ok INTEGER NOT NULL, error TEXT,
        text TEXT NOT NULL)""")

COOLDOWN = timedelta(minutes=30)  # one text per contract per window, however the price drifts


class NotifyChain:
    def __init__(self, conn: sqlite3.Connection, channels: list, quiet_hours: tuple[int, int],
                 enabled: bool = True, tz: ZoneInfo = PT):
        self.conn = conn
        self.channels = channels
        self.quiet_hours = quiet_hours
        self.enabled = enabled
        self.tz = tz
        self.names = DEFAULT_NAMES  # build_services swaps in one that knows CFB school names
        self.payouts = None  # PrizePicks payout table in use (None: the built-in tables); set by Services

    def _quiet(self, now: datetime) -> bool:
        start, end = self.quiet_hours
        h = now.astimezone(self.tz).hour
        return (h >= start or h < end) if start > end else (start <= h < end)

    def _already_sent(self, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM notifications WHERE dedupe_key=? AND ok=1 LIMIT 1",
                                 (key,)).fetchone() is not None

    def _cooling_down(self, leg_key: str, now: datetime) -> bool:
        """A text already went out for the same contract(s) within COOLDOWN."""
        rows = self.conn.execute(
            "SELECT n.ts FROM notifications n JOIN recommendations r ON r.id = n.rec_id "
            "WHERE r.leg_key=? AND n.ok=1", (leg_key,)).fetchall()
        return any(now - datetime.fromisoformat(r["ts"]) < COOLDOWN for r in rows)

    def _send(self, text: str, now: datetime, rec: Recommendation | None, only_first_ok: bool) -> NotifyResult:
        result = NotifyResult(False, "none", "no channels configured")
        for ch in self.channels:
            r = ch.send(text)
            self.conn.execute(
                "INSERT INTO notifications (ts, rec_id, dedupe_key, channel, ok, error, text) VALUES (?,?,?,?,?,?,?)",
                (now.isoformat(), rec.id if rec else None, rec.dedupe_key if rec else None,
                 r.channel, int(r.ok), r.error, text))
            if not result.ok:  # first success wins; otherwise keep the latest failure
                result = r
            if r.ok and only_first_ok:
                break
        return result

    def notify(self, rec: Recommendation, now: datetime, force: bool = False) -> NotifyResult | None:
        if not self.enabled or rec.observe_only:
            return None
        if not force and (self._already_sent(rec.dedupe_key)
                          or (rec.kind != "arb" and (self._quiet(now) or self._cooling_down(rec.leg_key, now)))):
            return None
        return self._send(self.format(rec), now, rec, only_first_ok=rec.kind != "arb")

    def format(self, rec: Recommendation) -> str:
        return format_message(rec, self.tz, self.names, self.payouts)

    def send_text(self, text: str, now: datetime) -> NotifyResult:
        """For `notify test`: ignores dedupe and quiet hours."""
        return self._send(text, now, None, only_first_ok=True)
