from datetime import UTC, datetime, timedelta

import httpx
from textual.widgets import Input, Static, TabbedContent

from gridiron_edge.app import GridironApp
from gridiron_edge.scheduler import Job
from gridiron_edge.ui.screens import ConfirmScreen, WhyScreen
from gridiron_edge.ui.widgets import BankrollHeader, EventLog, InfoLine, OpenBetsList, PlaceNowList

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
        assert "\u25b2 $0.00 today" in app.query_one(BankrollHeader).text  # a deposit is not a win
        assert "deposit" in log_lines(app)[-1]
        assert app.query_one("#cmd", Input).value == ""


async def test_withdraw_too_much_shows_error(services_mocked):
    svc = services_mocked()
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=SIZE) as pilot:
        await submit(app, pilot, "deposit 10")
        await submit(app, pilot, "withdraw 50")
        last = log_lines(app)[-1]
        assert "error" in last and "Not enough money" in last
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
        place = app.query_one("#place-now", PlaceNowList)
        assert place.option_count == 1
        assert "Seahawks beat Bears" in place.get_option_at_index(0).prompt.plain
        assert len(services_mocked.sink) == 1 and services_mocked.sink[0].startswith("Bet idea #")
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
        app.query_one("#place-now", PlaceNowList).focus()
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
        assert "recorded from idea #" in log_lines(app)[-1]
        assert app.query_one(BankrollHeader).target_cents == svc.ledger.balance()


async def test_missing_keys_app_still_starts(services_mocked):
    svc = services_mocked(odds_api_key="", cfbd_api_key="", therundown_api_key="")
    assert svc.odds is None and svc.cfbd is None and svc.rundown is None
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=SIZE) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        text = "\n".join(log_lines(app))
        assert "Not set up (no key in .env)" in text
        assert all(name in text for name in ("The Odds API", "TheRundown", "College Football Data"))
        assert "Kalshi: working" in text
        await submit(app, pilot, "deposit 5")
        assert svc.ledger.balance() == 500 and app.is_running


async def test_auth_error_logged_once(services_mocked):
    services_mocked.router.get(url__regex=r"the-odds-api\.com/v4/sports/americanfootball_nfl/odds").mock(
        return_value=httpx.Response(401))
    svc = services_mocked()
    lines = []
    for _ in range(2):
        lines += await svc.run_job(EARLY, NOW)
    assert [ln for ln in lines if ln[0] == "error"] == [("error", "The Odds API (sportsbook prices) rejected the key in .env, so it is turned off.")]
    assert svc.odds.disabled


async def test_first_run_checks_logged_once(services_mocked):
    svc = services_mocked()
    lines = await svc.startup(NOW)
    info = [m for k, m in lines if k == "info"]
    assert any(m.startswith("Sportsbooks on the odds service:") and "Pinnacle" in m for m in info)
    assert "Scores service: free plan, scores about 5 minutes behind." in info
    assert "College Football Data: 998 free calls left this month." in info and "Kalshi: working." in info
    assert 'Texts: type "notify test" to check that your phone gets them.' in info
    again = [m for _, m in await svc.startup(NOW)]
    assert not any(m.startswith("Sportsbooks on the odds service:") for m in again)


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
    props_job = Job("odds_props", "nfl", {"game_id": gid, "window": "slate"})
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


JARGON = ("EV", "CLV", "fair", "Brier", "KXNFL", "ML", " x")


def home_text(app) -> str:
    parts = [app.query_one(BankrollHeader).text]
    for lst in (app.query_one("#place-now", PlaceNowList), app.query_one("#open-bets", OpenBetsList)):
        parts += [lst.get_option_at_index(i).prompt.plain for i in range(lst.option_count)]
    for sel in ("#place-empty", "#open-empty"):
        parts.append(str(app.query_one(sel, Static).render()))
    return "\n".join(parts)


async def test_number_keys_switch_tabs_only_when_bar_empty(services_mocked):
    app = GridironApp(services_mocked(), clock=lambda: NOW)
    async with app.run_test(size=(100, 30)) as pilot:
        await app.workers.wait_for_complete()
        tabs = app.query_one("#tabs", TabbedContent)
        assert tabs.active == "home"
        for key, tab in (("2", "performance"), ("3", "scores"), ("4", "log"), ("5", "prizepicks"), ("1", "home")):
            await pilot.press(key)
            await pilot.pause()
            assert tabs.active == tab
        await pilot.press("d", "e", "p", "o", "s", "i", "t", "space", "2")
        assert app.query_one("#cmd", Input).value == "deposit 2" and tabs.active == "home"
        await pilot.press("enter")
        await pilot.pause()
        assert "Added $2.00" in str(app.query_one("#status", Static).render())


async def test_home_empty_state_explains_itself(services_mocked):
    app = GridironApp(services_mocked(), clock=lambda: NOW)
    async with app.run_test(size=(100, 30)) as pilot:
        await app.workers.wait_for_complete()
        app.refresh_views()
        await pilot.pause()
        empty = app.query_one("#place-empty", Static)
        assert empty.display and not app.query_one("#place-now").display
        msg = str(empty.render())
        assert msg and ("Next check" in msg or "Checking now" in msg)
        assert "No open bets" in str(app.query_one("#open-empty", Static).render())
        place = app.query_one("#place-box")
        assert place.region.height >= 8 and place.region.right <= 100
        assert app.query_one(BankrollHeader).region.height == 1
        assert str(app.query_one("#watch-empty", Static).render()) == "Nothing on the watch list."
        info = app.query_one(InfoLine)
        assert info.text.startswith("Today: no bets settled yet") and "limit left" in info.text
        assert all(len(line) <= 98 for line in info.text.splitlines()) and info.region.right <= 100
        assert not app.query_one("#open-summary").display


async def test_home_place_why_skip_and_open_bets(services_mocked, load):
    mock_edge(services_mocked, load)
    svc = services_mocked()
    await prime(svc)
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=(100, 30)) as pilot:
        await app.workers.wait_for_complete()
        await svc.run_job(Job("scan"), NOW)
        app.refresh_views()
        await pilot.pause()
        text = home_text(app)
        assert "#1 Kalshi  Seahawks beat Bears" in text and "expected profit $" in text
        assert "our chance " in text and "pays $" in text and " contract" in text
        assert not [w for w in JARGON if w in text], text
        await pilot.press("down")  # from the empty command bar into the list
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, WhyScreen)
        assert "Our chance:" in app.screen.text and "price implies" in app.screen.text
        assert "KXNFLGAME" in app.screen.lines[-1]
        assert str(app.screen.query_one("#why-details", Static).render()).startswith("[details] id 1")
        await pilot.press("escape")
        await pilot.pause()
        app.query_one("#place-now", PlaceNowList).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#ok")
        await pilot.pause()
        opened = app.query_one("#open-bets", OpenBetsList)
        assert opened.option_count == 1
        assert "Seahawks beat Bears" in opened.get_option_at_index(0).prompt.plain
        assert "in open bets" in app.query_one(BankrollHeader).text
        summary = app.query_one("#open-summary")
        assert summary.display and "in 1 open bet" in summary.text and "back if it wins" in summary.text
        assert "pays $" in opened.get_option_at_index(0).prompt.plain
        assert not app.query_one("#place-now").display  # nothing left to place


async def test_skip_key_removes_idea(services_mocked, load):
    mock_edge(services_mocked, load)
    svc = services_mocked()
    await prime(svc)
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=(100, 30)) as pilot:
        await app.workers.wait_for_complete()
        await svc.run_job(Job("scan"), NOW)
        app.refresh_views()
        await pilot.pause()
        app.query_one("#place-now", PlaceNowList).focus()
        await pilot.press("s")
        await pilot.pause()
        assert svc.lifecycle.get_rec(1).state == "SKIPPED"
        assert app.query_one("#place-empty", Static).display


def _bet(svc, placed: str, cost: int, settled: str | None = None, payout: int | None = None) -> None:
    """A bet row plus its ledger moves, times given in PT on the app clock."""
    from gridiron_edge.notify.format import PT
    p = datetime.fromisoformat(placed).replace(tzinfo=PT)
    s = datetime.fromisoformat(settled).replace(tzinfo=PT) if settled else None
    cur = svc.conn.execute(
        "INSERT INTO bets (rec_id, kind, legs_json, stake_cents, fee_cents, placed_at, state, result, "
        "payout_cents, settled_at) VALUES (0, 'kalshi', '[]', ?, 0, ?, ?, ?, ?, ?)",
        (cost, p.isoformat(), "SETTLED" if s else "PLACED", None if s is None else
         "won" if payout else "lost", payout, s.isoformat() if s else None))
    rows = [(p, "stake", -cost)] + ([(s, "payout", payout)] if s and payout else [])
    for ts, kind, cents in rows:
        svc.conn.execute("INSERT INTO ledger (ts, kind, amount_cents, bet_id) VALUES (?,?,?,?)",
                         (ts.astimezone(UTC).isoformat(), kind, cents, cur.lastrowid))


def test_today_profit_counts_bets_settled_today_that_were_placed_yesterday(services_mocked):
    svc = services_mocked()
    svc.ledger.deposit(100_000)
    _bet(svc, "2026-10-30T20:00", 1000, "2026-10-31T09:00")          # lost today: -$10
    _bet(svc, "2026-10-30T20:00", 1000, "2026-10-31T10:00", 1900)    # won today: +$9
    _bet(svc, "2026-10-30T19:00", 1000, "2026-10-30T23:00", 1900)    # won yesterday: not today
    _bet(svc, "2026-10-31T08:00", 500)                               # placed today, still open
    _bet(svc, "2026-10-30T18:00", 700)                               # placed yesterday, still open
    app = GridironApp(svc, clock=lambda: NOW)  # 11:05 PT on Oct 31
    assert app._today_profit(NOW) == -100


async def test_prizepicks_tab_and_home_status_line(services_mocked):
    svc = services_mocked()
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()  # first run probes one game's props (PrizePicks included)
        app.refresh_views()
        await pilot.pause()
        status = str(app.query_one("#pp-status", Static).render())
        assert status.startswith("PrizePicks: ") and "loaded for 1 game (Bears at Seahawks)" in status
        assert "See tab 5." in status
        await pilot.press("5")
        await pilot.pause()
        board = str(app.query_one("#pp-board", Static).render())
        assert "Lines as of" in board and "Bears at Seahawks - kickoff" in board
        assert "needs about" in board and "per pick" in board


async def test_pp_refresh_asks_first_then_loads(services_mocked):
    svc = services_mocked()
    app = GridironApp(svc, clock=lambda: NOW + timedelta(hours=10))  # the SEA game is ~21 h away
    async with app.run_test(size=SIZE) as pilot:
        await app.workers.wait_for_complete()
        await submit(app, pilot, "pp refresh")
        last = log_lines(app)[-1]
        assert "This uses about 3 Odds API credits (1 game) out of" in last and "Type: pp refresh yes" in last
        await submit(app, pilot, "pp refresh yes")
        await app.workers.wait_for_complete()
        await pilot.pause()
        text = "\n".join(log_lines(app))
        assert "Player lines for Bears at Seahawks:" in text and "PrizePicks" in text


async def test_help_lines_keep_commands_lowercase(services_mocked):
    svc = services_mocked()
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=SIZE) as pilot:
        await app.workers.wait_for_complete()
        await submit(app, pilot, "help")
        text = "\n".join(log_lines(app))
        assert "Type: pp add " in text and "Type: confirm <id> [price] [contracts]  -  You placed" in text
        assert "Pp add" not in text and "Notify test" not in text and "Keys: 1-5 switch tabs" in text


async def test_set_message_uses_plain_words(services_mocked):
    svc = services_mocked()
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=SIZE) as pilot:
        await app.workers.wait_for_complete()
        await submit(app, pilot, "set kelly_fraction 0.2")
        assert log_lines(app)[-1].endswith("Setting changed: bet size (share of the ideal amount) is now 0.2.")
