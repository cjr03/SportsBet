"""Try notification channels in order, with dedupe, quiet hours, re-text rules, text limits and an attempt log.

One bet idea is one text: a price tick does not re-text it (only RETEXT_GAP passing or a clearly
better EV does), every arb/middle pair on a game counts as one idea (family_key), and past
MAX_PER_HOUR / MAX_PER_DAY phone texts the rest are held for one summary text (flush_held)."""
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from gridiron_edge.bets import lifecycle as _lifecycle  # noqa: F401 - registers the recommendations table
from gridiron_edge.db import register_schema
from gridiron_edge.notify import NotifyResult
from gridiron_edge.describe import DEFAULT_NAMES
from gridiron_edge.notify.console import ConsoleNotifier
from gridiron_edge.notify.format import PT, format_message, notice
from gridiron_edge.types import Recommendation

register_schema(
    """CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, rec_id INTEGER,
        dedupe_key TEXT, channel TEXT NOT NULL, ok INTEGER NOT NULL, error TEXT,
        text TEXT NOT NULL, idea_key TEXT, family_key TEXT)""")

RETEXT_GAP = timedelta(hours=3)  # an idea already texted is texted again only after this...
MIN_EV_GAIN, MIN_EV_RATIO = 0.02, 1.25  # ...or when its EV is this much better (both must hold)
MAX_PER_HOUR, MAX_PER_DAY = 6, 30  # phone texts for bet ideas; past these, ideas go into one summary
SUMMARY_GAP = timedelta(hours=1)
SUMMARY_KEY = "summary"  # dedupe_key of summary rows (rec_id NULL), never matched by per-idea checks
SUMMARY_TOP = 5
KIND_WORDS = {"arb": "locked profit", "middle": "middle", "kalshi_game": "Kalshi game",
              "kalshi_prop": "Kalshi player", "pp_entry": "PrizePicks"}


def _add_key_columns(conn: sqlite3.Connection) -> None:
    """Older databases predate notifications.idea_key/family_key: add them (nullable) and their index,
    and fill them in for texts already sent so an upgrade does not re-text those ideas."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(notifications)")}
    if cols and "family_key" not in cols:
        with conn:
            for col in ("idea_key", "family_key"):
                if col not in cols:
                    conn.execute(f"ALTER TABLE notifications ADD COLUMN {col} TEXT")
            for r in conn.execute("SELECT * FROM recommendations WHERE id IN "
                                  "(SELECT rec_id FROM notifications WHERE rec_id IS NOT NULL)").fetchall():
                rec = _lifecycle._row_to_rec(r)
                conn.execute("UPDATE notifications SET idea_key=?, family_key=? WHERE rec_id=?",
                             (rec.idea_key, rec.family_key, rec.id))
    conn.execute("CREATE INDEX IF NOT EXISTS notifications_idea_ts ON notifications (idea_key, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS notifications_family_ts ON notifications (family_key, ts)")


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
        self.held: list[Recommendation] = []  # ideas over the text limits, waiting for flush_held
        _add_key_columns(conn)

    def _quiet(self, now: datetime) -> bool:
        start, end = self.quiet_hours
        h = now.astimezone(self.tz).hour
        return (h >= start or h < end) if start > end else (start <= h < end)

    def _already_sent(self, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM notifications WHERE dedupe_key=? AND ok=1 LIMIT 1",
                                 (key,)).fetchone() is not None

    def _should_retext(self, rec: Recommendation, now: datetime) -> bool:
        """No text yet for this idea's family, or the last one is RETEXT_GAP old, or EV is clearly better."""
        last = self.conn.execute(
            "SELECT n.ts, r.ev FROM notifications n LEFT JOIN recommendations r ON r.id = n.rec_id "
            "WHERE n.family_key=? AND n.ok=1 AND n.rec_id IS NOT NULL ORDER BY n.id DESC LIMIT 1",
            (rec.family_key,)).fetchone()
        if last is None or now - datetime.fromisoformat(last["ts"]) >= RETEXT_GAP:
            return True
        ev = last["ev"]
        return ev is not None and rec.ev >= ev + MIN_EV_GAIN and rec.ev >= ev * MIN_EV_RATIO

    def _delivered_since(self, t: datetime) -> int:
        """Bet-idea texts that reached a phone channel since t (one per send, however many channels)."""
        rows = self.conn.execute(
            "SELECT ts FROM notifications WHERE ok=1 AND channel != 'console' AND rec_id IS NOT NULL "
            "GROUP BY ts, rec_id ORDER BY MAX(id) DESC LIMIT ?", (MAX_PER_DAY * 4,)).fetchall()
        return sum(datetime.fromisoformat(r["ts"]) >= t for r in rows)

    def _over_hour_cap(self, now: datetime) -> bool:
        return self._delivered_since(now - timedelta(hours=1)) >= MAX_PER_HOUR

    def _over_day_cap(self, now: datetime) -> bool:
        return self._delivered_since(now - timedelta(days=1)) >= MAX_PER_DAY

    def _send(self, text: str, now: datetime, rec: Recommendation | None, only_first_ok: bool,
              channels: list | None = None, dedupe_key: str | None = None) -> NotifyResult:
        result = NotifyResult(False, "none", "no channels configured")
        for ch in self.channels if channels is None else channels:
            r = ch.send(text)
            self.conn.execute(
                "INSERT INTO notifications (ts, rec_id, dedupe_key, channel, ok, error, text, idea_key, family_key)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (now.isoformat(), rec.id if rec else None, rec.dedupe_key if rec else dedupe_key,
                 r.channel, int(r.ok), r.error, text, rec.idea_key if rec else None,
                 rec.family_key if rec else None))
            if not result.ok:  # first success wins; otherwise keep the latest failure
                result = r
            if r.ok and only_first_ok:
                break
        return result

    def notify(self, rec: Recommendation, now: datetime, force: bool = False) -> NotifyResult | None:
        """None: not texted (off, already sent, quiet hours, or not different enough from the last text).
        channel 'held': over the text limits; it goes into the next flush_held summary."""
        if not self.enabled or rec.observe_only:
            return None
        if not force:
            if (self._already_sent(rec.dedupe_key) or (rec.kind != "arb" and self._quiet(now))
                    or not self._should_retext(rec, now)):
                return None
            if self._over_hour_cap(now) or self._over_day_cap(now):
                if all(h.id != rec.id for h in self.held):
                    self.held.append(rec)
                return NotifyResult(False, "held", "rate limit")
        return self._send(self.format(rec), now, rec, only_first_ok=True)

    def flush_held(self, now: datetime) -> NotifyResult | None:
        """One summary text for the held ideas (at most one an hour; console only past the daily cap)."""
        live = [r for r in self.held if now < r.expires_at]
        if not live:
            self.held = []
            return None
        last = self.conn.execute(
            "SELECT ts FROM notifications WHERE dedupe_key=? AND rec_id IS NULL AND ok=1 "
            "ORDER BY id DESC LIMIT 1", (SUMMARY_KEY,)).fetchone()
        if last is not None and now - datetime.fromisoformat(last["ts"]) < SUMMARY_GAP:
            self.held = live
            return None
        best: dict[str, Recommendation] = {}
        for r in live:  # one line per family, the best EV
            if r.family_key not in best or r.ev > best[r.family_key].ev:
                best[r.family_key] = r
        top = sorted(best.values(), key=lambda r: r.ev, reverse=True)
        lines = "; ".join(f"#{r.id} {KIND_WORDS.get(r.kind, r.kind)}, about {r.ev * 100:.0f}% expected return"
                          for r in top[:SUMMARY_TOP])
        more = f" and {len(top) - SUMMARY_TOP} more" if len(top) > SUMMARY_TOP else ""
        text = notice(f"{len(top)} more bet idea{'s' * (len(top) != 1)} (held back so your phone isn't "
                      f"flooded): {lines}{more}. See the app for details.")
        channels = self.channels
        if self._over_day_cap(now):
            channels = [ch for ch in self.channels if ch.name == ConsoleNotifier.name]
        self.held = []
        return self._send(text, now, None, only_first_ok=True, channels=channels, dedupe_key=SUMMARY_KEY)

    def format(self, rec: Recommendation) -> str:
        return format_message(rec, self.tz, self.names, self.payouts)

    def send_text(self, text: str, now: datetime) -> NotifyResult:
        """For `notify test`: ignores dedupe and quiet hours."""
        return self._send(text, now, None, only_first_ok=True)
