"""The scan loop texts one idea per game, best first, under the text limits, then one summary."""
from datetime import UTC, datetime

from gridiron_edge.notify import NotifyResult
from gridiron_edge.notify.chain import MAX_PER_HOUR
from gridiron_edge.scheduler import Job
from gridiron_edge.types import Leg, Recommendation

NOW = datetime(2026, 10, 24, 19, 0, tzinfo=UTC)


class Phone:
    name = "imessage"

    def __init__(self, sink):
        self.sink = sink

    def send(self, text):
        self.sink.append(text)
        return NotifyResult(True, self.name)


def _arb(game, a, b, ev):
    legs = [Leg("kalshi", a, "yes", 0.28, 0.28, game, ticker=f"KXNCAAFSPREAD-{game}-{a}", line=5.5),
            Leg("kalshi", b, "no", 0.62, 0.62, game, ticker=f"KXNCAAFSPREAD-{game}-{b}", line=6.5)]
    return Recommendation(None, "arb", legs, 1800, 20, ev, 1.0, "kalshi_ladder", NOW, NOW.replace(hour=23))


async def _scan(svc, recs, monkeypatch):
    monkeypatch.setattr("gridiron_edge.services.pipeline.scan", lambda *a, **k: list(recs))
    return await svc._job_scan(Job("scan", None, {"what": "all"}), NOW)


async def test_scan_texts_best_pair_per_game_only(services_mocked, monkeypatch):
    svc, phone = services_mocked(), []
    svc.notifier.channels = [Phone(phone)]
    recs = [_arb("26OCT24AKRKENT", "AKR6", "KENT7", 0.02), _arb("26OCT24AKRKENT", "AKR3", "KENT10", 0.05),
            _arb("26OCT24MISSTEX", "MISS15", "TEX17", 0.03)]
    out = await _scan(svc, recs, monkeypatch)
    assert len(phone) == 2
    pending = {r["ev"] for r in svc.conn.execute("SELECT ev FROM recommendations WHERE state='PENDING'")}
    assert pending == {0.05, 0.03}  # the best AKR/KENT pair, not the first one listed
    texted = [m for k, m in out if k == "text"]
    assert len(texted) == 2 and not [m for k, m in out if k == "error"]
    assert any("1 more" in m for k, m in out if k == "info")


async def test_scan_burst_is_capped_with_one_summary(services_mocked, monkeypatch):
    svc, phone = services_mocked(), []
    svc.notifier.channels = [Phone(phone)]
    recs = [_arb(f"G{i}", "A6", "B7", 0.01 + i / 1000) for i in range(120)]
    out = await _scan(svc, recs, monkeypatch)
    assert len(phone) == MAX_PER_HOUR + 1 and "more bet ideas" in phone[-1]
    assert not [m for k, m in out if k == "error"]
    held = svc.conn.execute("SELECT COUNT(*) FROM recommendations WHERE state='PENDING'").fetchone()[0]
    assert held == MAX_PER_HOUR  # held ideas are not marked as texted
    best = svc.lifecycle.get_rec(1)
    assert abs(best.ev - 0.129) < 1e-9 and f"#{best.id}" in phone[0]
