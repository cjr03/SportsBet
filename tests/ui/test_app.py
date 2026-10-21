from datetime import UTC, datetime

import httpx
from textual.widgets import DataTable, Input

from gridiron_edge.app import GridironApp
from gridiron_edge.scheduler import Job
from gridiron_edge.ui.screens import ConfirmScreen
from gridiron_edge.ui.widgets import BankrollHeader, EventLog

# Five minutes after the fixtures' odds were pulled; Sunday's SEA game is ~31 h away.
NOW = datetime(2026, 10, 31, 18, 5, tzinfo=UTC)
SIZE = (160, 50)
EARLY = Job("odds_games", "nfl", {"window": "early"})


def edge_markets(load) -> dict:
    """The Kalshi ML fixture repriced so SEA YES at 0.50 is well under the ~0.59 sharp fair."""
    body = load("kalshi_markets_p1.json")
    sea, chi = body["markets"]
    sea.update(yes_bid_dollars="0.4800", yes_ask_dollars="0.5000",
               no_bid_dollars="0.5000", no_ask_dollars="0.5200")
    chi.update(yes_bid_dollars="0.4000", yes_ask_dollars="0.5200",
               no_bid_dollars="0.4800", no_ask_dollars="0.6000")
    body["cursor"] = ""
    return body


def mock_edge(services_mocked, load) -> None:
    services_mocked.router.get(host="api.elections.kalshi.com", path__regex=r"/markets$",
                               params__contains={"series_ticker": "KXNFLGAME"}).mock(
        return_value=httpx.Response(200, json=edge_markets(load)))


async def prime(svc) -> None:
    """Fund the bankroll, register games, and pull Kalshi plus sharp lines."""
    svc.ledger.deposit(100_000)
    await svc.startup(NOW)
    for job in (Job("kalshi_refresh"), EARLY):
        lines = await svc.run_job(job, NOW)
        assert not [ln for ln in lines if ln[0] == "error"], lines


def log_lines(app) -> list[str]:
    return app.query_one(EventLog).lines_text


async def submit(app, pilot, text: str) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()
    app.query_one("#cmd", Input).value = text
    await pilot.press("enter")
    await pilot.pause()


async def test_deposit_updates_header_and_log(services_mocked):
    app = GridironApp(services_mocked(), clock=lambda: NOW)
    async with app.run_test(size=SIZE) as pilot:
        await submit(app, pilot, "deposit 250")
        assert app.query_one(BankrollHeader).target_cents == 25000
        assert "deposit" in log_lines(app)[-1]
        assert app.query_one("#cmd", Input).value == ""


async def test_withdraw_too_much_shows_error(services_mocked):
    svc = services_mocked()
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=SIZE) as pilot:
        await submit(app, pilot, "deposit 10")
        await submit(app, pilot, "withdraw 50")
        last = log_lines(app)[-1]
        assert "error" in last and "insufficient" in last
        assert svc.ledger.balance() == 1000
        assert app.query_one(BankrollHeader).target_cents == 1000


async def test_bad_command_no_crash(services_mocked):
    app = GridironApp(services_mocked(), clock=lambda: NOW)
    async with app.run_test(size=SIZE) as pilot:
        await submit(app, pilot, "frobnicate")
        assert "error" in log_lines(app)[-1] and "frobnicate" in log_lines(app)[-1]
        await submit(app, pilot, "withdraw $1,0x0")
        assert "error" in log_lines(app)[-1]
        assert app.is_running


async def test_scan_job_creates_rec_and_texts_once(services_mocked, load):
    mock_edge(services_mocked, load)
    svc = services_mocked()
    await prime(svc)
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=SIZE) as pilot:
        await app.workers.wait_for_complete()
        first = await svc.run_job(Job("scan"), NOW)
        second = await svc.run_job(Job("scan"), NOW)
        app.refresh_views()
        await pilot.pause()
        assert app.query_one("#tab-kalshi_game", DataTable).row_count == 1
        assert len(services_mocked.sink) == 1 and services_mocked.sink[0].startswith("GE KALSHI BUY YES")
        assert {k for k, _ in first} >= {"rec", "text"}
        assert not [ln for ln in second if ln[0] in ("rec", "text")]
        (rec,) = svc.lifecycle.pending()
        assert rec.state == "PENDING" and rec.legs[0].ticker == "KXNFLGAME-26NOV02CHISEA-SEA"


async def test_confirm_flow_from_board(services_mocked, load):
    mock_edge(services_mocked, load)
    svc = services_mocked()
    await prime(svc)
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=SIZE) as pilot:
        await app.workers.wait_for_complete()
        await svc.run_job(Job("scan"), NOW)
        app.refresh_views()
        await pilot.pause()
        before = svc.ledger.balance()
        table = app.query_one("#tab-kalshi_game", DataTable)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#qty")
        app.screen.query_one("#qty", Input).value = "10"
        await pilot.click("#ok")
        await pilot.pause()
        (bet,) = svc.lifecycle.open_bets()
        assert bet["state"] == "PLACED" and bet["qty"] == 10 and bet["price"] == 0.5
        assert svc.ledger.balance() == before - bet["stake_cents"] - bet["fee_cents"] < before
        assert "confirm" in log_lines(app)[-1]
        assert app.query_one(BankrollHeader).target_cents == svc.ledger.balance()


async def test_missing_keys_app_still_starts(services_mocked):
    svc = services_mocked(odds_api_key="", cfbd_api_key="", therundown_api_key="")
    assert svc.odds is None and svc.cfbd is None and svc.rundown is None
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=SIZE) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        text = "\n".join(log_lines(app))
        assert "disabled" in text
        assert all(name in text for name in ("odds_api", "therundown", "cfbd"))
        assert "kalshi reachable=True" in text
        await submit(app, pilot, "deposit 5")
        assert svc.ledger.balance() == 500 and app.is_running


async def test_auth_error_logged_once(services_mocked):
    services_mocked.router.get(url__regex=r"the-odds-api\.com/v4/sports/americanfootball_nfl/odds").mock(
        return_value=httpx.Response(401))
    svc = services_mocked()
    lines = []
    for _ in range(2):
        lines += await svc.run_job(EARLY, NOW)
    assert [ln for ln in lines if ln[0] == "error"] == [("error", "odds_api: invalid key - disabled")]
    assert svc.odds.disabled


async def test_first_run_checks_logged_once(services_mocked):
    svc = services_mocked()
    lines = await svc.startup(NOW)
    info = [m for k, m in lines if k == "info"]
    assert any(m.startswith("odds_api books:") and "pinnacle" in m for m in info)
    assert "therundown tier=free delay=300s books=19,23,22" in info
    assert "cfbd remaining=998" in info and "kalshi reachable=True" in info
    assert 'notify: run "notify test" to confirm your phone alerts' in info
    again = [m for _, m in await svc.startup(NOW)]
    assert not any(m.startswith("odds_api books:") for m in again)


async def test_scheduler_state_survives_restart(services_mocked):
    svc = services_mocked()
    svc.scheduler.mark_done(Job("cfbd_weekly"), NOW)
    assert services_mocked().scheduler.state[Job("cfbd_weekly").key] == NOW


def _frozen(monkeypatch):
    from gridiron_edge import services as services_mod

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(services_mod, "datetime", Frozen)


async def test_quotes_and_props_survive_restart(services_mocked, monkeypatch):
    _frozen(monkeypatch)
    svc = services_mocked()
    assert not [ln for ln in await svc.run_job(EARLY, NOW) if ln[0] == "error"]
    (gid,) = [r["game_id"] for r in svc.conn.execute("SELECT game_id FROM games WHERE odds_event_id='evt1'")]
    props_job = Job("odds_props", "nfl", {"game_id": gid})
    assert not [ln for ln in await svc.run_job(props_job, NOW) if ln[0] == "error"]
    for job in (EARLY, props_job):
        svc.scheduler.mark_done(job, NOW)
    assert svc.snapshot(NOW).quotes and svc.snapshot(NOW).props

    again = services_mocked()  # a relaunch on the same db: the scheduler won't re-pull either
    assert EARLY.key in again.scheduler.state and props_job.key in again.scheduler.state
    snap = again.snapshot(NOW)
    assert snap.quotes and snap.props
    assert {q.game_id for q in snap.quotes} == {q.game_id for q in svc.snapshot(NOW).quotes}


async def test_stale_persisted_quotes_dropped_on_restart(services_mocked, monkeypatch):
    from datetime import timedelta

    _frozen(monkeypatch)
    svc = services_mocked()
    await svc.run_job(EARLY, NOW)
    assert svc.latest["quotes"]
    later = services_mocked()
    later.load_latest(NOW + timedelta(days=30))
    assert later.latest["quotes"] == []


async def test_placed_bet_not_rerecommended_or_retexted(services_mocked, load):
    mock_edge(services_mocked, load)
    svc = services_mocked()
    await prime(svc)
    await svc.run_job(Job("scan"), NOW)
    (rec,) = svc.lifecycle.pending()
    svc.lifecycle.confirm(rec.id, NOW)
    assert svc.snapshot(NOW).open_game_ids == {rec.legs[0].game_id}
    assert svc.scheduler.open_bet_games() == {rec.legs[0].game_id}
    # the price ticks a cent: without the open-position check this would be a new rec and a new text
    svc.latest["kalshi"] = [m if m.ticker != rec.legs[0].ticker else
                            __import__("dataclasses").replace(m, yes_ask=m.yes_ask - 0.01)
                            for m in svc.latest["kalshi"]]
    lines = await svc.run_job(Job("scan"), NOW)
    assert not [ln for ln in lines if ln[0] in ("rec", "text")]
    assert len(services_mocked.sink) == 1


async def test_rundown_lines_only_for_dates_within_48h(services_mocked):
    svc = services_mocked()
    await svc.run_job(EARLY, NOW)  # registers SEA game at 2026-11-02 01:15 UTC (~31 h away)
    route = services_mocked.router.routes
    before = sum(r.call_count for r in route if "therundown" in str(r.pattern))
    await svc.run_job(Job("rundown_lines", "nfl"), NOW)
    after = sum(r.call_count for r in route if "therundown" in str(r.pattern))
    assert after - before == 1


async def test_report_and_drift_reset_commands_log_plain_english(services_mocked):
    svc = services_mocked()
    svc._put("drift.nfl spread", "2026-10-31T18:00:00+00:00")
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test() as pilot:
        await submit(app, pilot, "report")
        await submit(app, pilot, "drift reset nfl spread")
        text = "\n".join(log_lines(app))
    assert "No settled bets yet." in text
    assert "NFL point-spread bets are back on" in text
    assert svc.drift_paused() == {}
