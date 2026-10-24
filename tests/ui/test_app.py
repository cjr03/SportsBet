from datetime import UTC, datetime, timedelta

import httpx
from textual.widgets import Input, Static, TabbedContent

from gridiron_edge.app import GridironApp
from gridiron_edge.scheduler import Job
from gridiron_edge.ui.screens import ConfirmScreen, WhyScreen
from gridiron_edge.ui.widgets import (
    BankrollHeader, EventLog, InfoLine, OpenBetsList, PickList, PlaceNowList, PlayList, WatchList,
)

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
        assert "Not set up yet (add a key to use it)" in text
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
        assert tabs.active == "home"  # Home, with the PrizePicks boxes, opens first
        for key, tab in (("2", "performance"), ("3", "scores"), ("4", "log"), ("1", "home")):
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
        assert empty.region.height >= 1 and place.region.height >= 3 and place.region.right <= 100
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
        assert "1. Kalshi  Seahawks beat Bears" in text and "expected profit $" in text and "idea #1" in text
        assert "our chance " in text and "pays $" in text and " contract" in text
        assert not [w for w in JARGON if w in text], text
        await pilot.press("1", "down")  # Home, then from the empty command bar into PLACE NOW
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


async def test_prizepicks_boxes_on_home(services_mocked):
    svc = services_mocked()
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()  # first run probes one game's props (PrizePicks included)
        app.refresh_views()
        await pilot.pause()
        summary = str(app.query_one("#pp-summary", Static).render())
        board = "\n".join(r.text for r in app.query_one("#pp-board", PickList).rows)
        assert "Lines as of" in summary and "- Bears at Seahawks - team unknown" in board
        assert "needs about" in summary and "per pick" in summary
        titles = [app.query_one(f"#{i}").border_title
                  for i in ("pp-summary-box", "pp-plays-box", "pp-entry-box", "pp-scroll")]
        assert titles == ["PRIZEPICKS", "SUGGESTED PLAYS (best first)", "YOUR ENTRY", "ALL LINES (best first)"]
        assert app.focused is app.query_one("#cmd")  # the command bar keeps typing letters
        await pilot.press("down")
        await pilot.pause()
        assert isinstance(app.focused, (PlayList, PickList))  # PLACE NOW is empty: Down goes into the plays and picks


def _pp_dump(app) -> str:
    rows = [r.text for r in app.query_one("#pp-board", PickList).rows]
    return "\n".join(rows) + "\n---\n" + str(app.query_one("#pp-entry", Static).render())


def seed_pp(svc, game_id: str, seen: datetime = NOW - timedelta(minutes=5)) -> None:
    """Sportsbook-backed PrizePicks lines on the fixture game: two Seahawks and one Bears player."""
    from gridiron_edge.types import PropQuote
    out = []
    for player, team, stat, line, over in (("Geno Smith", "SEA", "pass_yds", 229.5, 1.52),
                                           ("Kenneth Walker", "SEA", "rush_yds", 64.5, 1.56),
                                           ("Caleb Williams", "CHI", "pass_yds", 219.5, 1.54)):
        out.append(PropQuote("odds_api", "prizepicks", game_id, player, stat, "over", line, None, seen,
                             fetched_at=seen))
        under = round(1 / (1.045 - 1 / over), 3)
        for book in ("draftkings", "fanduel"):
            out += [PropQuote("odds_api", book, game_id, player, stat, side, line, dec, seen, fetched_at=seen)
                    for side, dec in (("over", over), ("under", under))]
        svc.player_teams[player] = team
    svc.latest["props"] = out


async def test_prizepicks_pick_two_lines_and_record_entry(services_mocked):
    """Press 1, Enter opens the top suggested play, x clears it; Enter on two picks from different teams in
    ALL LINES, see YOUR ENTRY, c records the bet."""
    svc = services_mocked(pp_structures=("2-power", "3-power", "4-flex", "5-flex", "6-flex"))
    svc.ledger.deposit(100_000)
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=(140, 60)) as pilot:
        await app.workers.wait_for_complete()
        seed_pp(svc, next(g.game_id for g in svc.registry.all_since(NOW - timedelta(days=1))
                          if g.home == "SEA"))
        app.refresh_views()
        await pilot.pause()
        await pilot.press("1", "down")
        await pilot.pause()
        plays = app.query_one("#pp-plays", PlayList)
        assert app.focused is plays, _pp_dump(app)
        assert plays.rows[0].startswith("1. 2-pick Power Play - buy in $"), plays.rows
        await pilot.press("enter")  # open the top play in YOUR ENTRY
        await pilot.pause()
        assert len(app._pp_selected) == 2 and "Opened suggested play #1" in log_lines(app)[-1]
        assert "Suggested amount: $" in str(app.query_one("#pp-entry", Static).render())
        await pilot.press("x")
        await pilot.pause()
        assert app._pp_selected == []
        best = app.query_one("#pp-board", PickList)
        best.focus()
        await pilot.pause()
        rows = [r for r in best.rows if r.key is not None]
        assert [r.key[0] for r in rows] == ["Geno Smith", "Caleb Williams", "Kenneth Walker"], _pp_dump(app)
        await pilot.press("enter")  # Geno Smith
        await pilot.pause()
        assert best.rows[0].text.startswith("[x] #1 Geno Smith MORE than 229.5 passing yards - our chance ")
        assert best.rows[0].text.endswith("- Bears at Seahawks - play #1, $2.09 buy-in"), best.rows[0].text
        await pilot.press("down", "down", "enter")  # Kenneth Walker: same team, so not ready yet
        await pilot.pause()
        entry = str(app.query_one("#pp-entry", Static).render())
        assert "Can't record yet: All your picks are on one team (Seahawks)" in entry, entry
        await pilot.press("c")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen) and "one team" in log_lines(app)[-1]
        await pilot.press("space", "up", "enter")  # out with Walker, in with Caleb Williams (Bears)
        await pilot.pause()
        assert app._pp_selected == [("Geno Smith", "pass_yds", 229.5, "over"),
                                    ("Caleb Williams", "pass_yds", 219.5, "over")]
        entry = str(app.query_one("#pp-entry", Static).render())
        assert "Best entry type for these 2 picks: 2-pick Power Play (every pick must hit)." in entry, entry
        assert "All 2 hit: pays back $" in entry and "Expected profit: about $" in entry, entry
        assert "linked" in entry  # same game: scored as linked picks
        assert app.query_one("#pp-entry-box").border_title == "YOUR ENTRY (2 picks)"
        stake = app._pp_eval.stake_cents
        assert stake > 0
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert app.screen.query_one("#stake", Input).value == f"{stake / 100:.2f}"
        app.screen.query_one("#stake", Input).value = "5"
        await pilot.click("#ok")
        await pilot.pause()
        (bet,) = svc.lifecycle.open_bets()
        assert bet["kind"] == "pp_entry" and bet["stake_cents"] == 500 and bet["structure"] == "2-power"
        assert svc.lifecycle.get_rec(bet["rec_id"]).state == "PLACED"
        assert svc.ledger.balance() == 100_000 - 500
        assert app._pp_selected == [] and "recorded from idea #" in "\n".join(log_lines(app))


async def test_prizepicks_why_and_clear_keys(services_mocked):
    svc = services_mocked()
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=(140, 60)) as pilot:
        await app.workers.wait_for_complete()
        seed_pp(svc, next(g.game_id for g in svc.registry.all_since(NOW - timedelta(days=1)) if g.home == "SEA"))
        app.refresh_views()
        await pilot.pause()
        await pilot.press("1")
        app.query_one("#pp-board", PickList).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert len(app._pp_selected) == 1
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, WhyScreen)
        text = app.screen.text
        assert "Why this pick?" in str(app.screen.query_one("#why-title").render())
        assert "DraftKings, line 229.5: MORE $1.52, LESS $2.58" in text and "Needed chance:" in text and "line pulled at" in text
        await pilot.press("escape")
        await pilot.pause()
        app.query_one("#pp-board", PickList).focus()
        await pilot.press("x")
        await pilot.pause()
        assert app._pp_selected == [] and "Cleared your PrizePicks entry." in log_lines(app)[-1]
        await pilot.press("enter")
        svc.latest["props"] = [q for q in svc.latest["props"] if q.player != "Geno Smith"]
        app.refresh_views()
        await pilot.pause()
        assert app._pp_selected == [] and "Removed Geno Smith MORE than 229.5" in log_lines(app)[-1]


async def test_place_now_e_opens_prizepicks_idea_and_c_reuses_its_id(services_mocked):
    from gridiron_edge.pipeline import pp_entry_rec
    from gridiron_edge.strategy.prizepicks import Pick, payout_table
    svc = services_mocked(pp_structures=("2-power", "3-power"))
    svc.ledger.deposit(100_000)
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=(140, 60)) as pilot:
        await app.workers.wait_for_complete()
        game = next(g for g in svc.registry.all_since(NOW - timedelta(days=1)) if g.home == "SEA")
        seed_pp(svc, game.game_id)
        app.refresh_views()
        await pilot.pause()
        live = {ln.player: ln for ln in app._pp_view.lines}
        legs = [Pick(ln.player, ln.team, ln.game_id, ln.stat, ln.line, ln.side, ln.p, ln.anchored)
                for ln in (live["Geno Smith"], live["Caleb Williams"])]
        rec = pp_entry_rec(legs, (2, "power"), 0.05, 1200, False, payout_table(svc.settings), NOW,
                           {game.game_id: game})
        saved, _ = svc.lifecycle.upsert_recommendation(rec, NOW)
        app.refresh_views()
        await pilot.pause()
        place = app.query_one("#place-now", PlaceNowList)
        assert [r.id for r in place.recs] == [saved.id]
        place.focus()
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert app.query_one("#tabs", TabbedContent).active == "home"
        assert isinstance(app.focused, (PlayList, PickList))  # from a list (not the bar): straight into the picks
        assert [k[0] for k in app._pp_selected] == ["Geno Smith", "Caleb Williams"]
        entry = str(app.query_one("#pp-entry", Static).render())
        assert f"Same picks as PrizePicks idea #{saved.id} in PLACE NOW." in entry, entry
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and app.screen.rec.id == saved.id
        await pilot.click("#ok")
        await pilot.pause()
        (bet,) = svc.lifecycle.open_bets()
        assert bet["rec_id"] == saved.id and bet["stake_cents"] == 1200
        assert svc.conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 1


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
        assert "Pp add" not in text and "Notify test" not in text and "Keys: 1-4 switch tabs" in text


async def test_set_message_uses_plain_words(services_mocked):
    svc = services_mocked()
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=SIZE) as pilot:
        await app.workers.wait_for_complete()
        await submit(app, pilot, "set kelly_fraction 0.2")
        assert log_lines(app)[-1].endswith("Setting changed: bet size (share of the ideal amount) is now 0.2.")


async def test_tab1_from_command_bar_keeps_letters_typing(services_mocked):
    """Pressing 1 in the empty command bar must not turn the next letters into PrizePicks keys."""
    svc = services_mocked()
    svc.ledger.deposit(100_000)
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=(100, 30)) as pilot:
        await app.workers.wait_for_complete()
        seed_pp(svc, next(g.game_id for g in svc.registry.all_since(NOW - timedelta(days=1)) if g.home == "SEA"))
        app.refresh_views()
        await pilot.pause()
        cmd = app.query_one("#cmd", Input)
        await pilot.press("2", "1")
        await pilot.pause()
        assert app.query_one("#tabs", TabbedContent).active == "home" and app.focused is cmd
        await pilot.press("c", "o", "n", "f", "i", "r", "m", "space", "1", "3", "x", "d")
        await pilot.pause()
        assert cmd.value == "confirm 13xd" and not isinstance(app.screen, (ConfirmScreen, WhyScreen))
        assert app._pp_selected == [] and app.query_one("#tabs", TabbedContent).active == "home"
        cmd.value = ""
        await pilot.press("down")
        await pilot.pause()
        assert app.query_one("#tabs", TabbedContent).active == "home"
        assert isinstance(app.focused, PlayList) and app.focused.id == "pp-plays"
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is cmd


def _sea(svc):
    return next(g for g in svc.registry.all_since(NOW - timedelta(days=1)) if g.home == "SEA")


async def test_prizepicks_old_lines_can_still_be_picked_and_recorded(services_mocked):
    """Lines older than props_stale_seconds: rows show our chance from the older sportsbook prices and
    their age, Enter adds them, YOUR ENTRY warns, c records with the warning; nothing is built or texted."""
    from gridiron_edge import pipeline
    svc = services_mocked(pp_structures=("2-power", "3-power", "4-flex", "5-flex", "6-flex"))
    svc.ledger.deposit(100_000)
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=(140, 60)) as pilot:
        await app.workers.wait_for_complete()
        seed_pp(svc, _sea(svc).game_id, seen=NOW - timedelta(hours=7))
        app.refresh_views()
        await pilot.pause()
        summary = str(app.query_one("#pp-summary", Static).render())
        assert "7 hours old" in summary and "pp refresh" in summary, summary
        await pilot.press("1", "down")
        await pilot.pause()
        board = app.query_one("#pp-board", PickList)
        assert app.focused is board, _pp_dump(app)
        assert board.highlighted is not None and board.rows[board.highlighted].key is not None, _pp_dump(app)
        rows = [r for r in board.rows if r.key is not None]
        assert len(rows) == 3, _pp_dump(app)
        for r in rows:
            assert " MORE than " in r.text and "our chance " in r.text, r.text
            assert "(sportsbook prices from 7 hr ago)" in r.text, r.text
        await pilot.press("enter")
        await pilot.pause()
        assert len(app._pp_selected) == 1, log_lines(app)[-3:]
        first = app._pp_selected[0][0]
        other = next(i for i, r in enumerate(board.rows) if r.key is not None and r.key[0] != first
                     and (r.key[0] == "Caleb Williams") != (first == "Caleb Williams"))
        board.highlighted = other
        await pilot.press("enter")
        await pilot.pause()
        assert len(app._pp_selected) == 2, log_lines(app)[-3:]
        entry = str(app.query_one("#pp-entry", Static).render())
        warn = "Prices are 7 hours old and may have moved. Type pp refresh to load new ones before you place this."
        assert warn in entry and "Best entry type for these 2 picks" in entry, entry
        snap = svc.snapshot(NOW)
        assert pipeline.pp_entry_candidates(snap, svc.settings, svc.projector) == []
        assert pipeline.pp_blocker(snap, svc.settings, svc.projector)[0] == "stale"
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert warn in str(app.screen.query_one("#confirm-warning", Static).render())
        assert app.screen.query_one("#stake", Input).value == ""  # no amount suggested from old prices
        app.screen.query_one("#stake", Input).value = "5"
        await pilot.click("#ok")
        await pilot.pause()
        (bet,) = svc.lifecycle.open_bets()
        assert bet["kind"] == "pp_entry" and bet["stake_cents"] == 500


async def test_prizepicks_nothing_to_pick_says_why(services_mocked):
    """Down/Enter with no line you can pick log what to do instead of doing nothing."""
    from gridiron_edge.types import PropQuote
    svc = services_mocked()
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=(140, 60)) as pilot:
        await app.workers.wait_for_complete()
        svc.latest["props"] = []
        app.refresh_views()
        await pilot.pause()
        await pilot.press("1", "down")
        await pilot.pause()
        assert "No line can be picked yet: lines load about 3 hours before kickoff, or type pp refresh" \
            in log_lines(app)[-1], log_lines(app)[-3:]
        gid = _sea(svc).game_id
        seen = NOW - timedelta(minutes=5)
        svc.latest["props"] = [PropQuote("odds_api", "prizepicks", gid, "Nobody Known", "pass_yds", "over",
                                         199.5, None, seen, fetched_at=seen)]
        app.refresh_views()
        await pilot.pause()
        board = app.query_one("#pp-board", PickList)
        board.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app._pp_selected == []
        assert "No line can be picked yet: none of the loaded lines" in log_lines(app)[-1], log_lines(app)[-3:]


async def test_prizepicks_r_asks_before_loading_lines(services_mocked):
    from gridiron_edge.ui.screens import AskScreen
    svc = services_mocked()
    clock = NOW + timedelta(hours=10)  # the SEA game is ~21 h away
    app = GridironApp(svc, clock=lambda: clock)
    async with app.run_test(size=(140, 60)) as pilot:
        await app.workers.wait_for_complete()
        seed_pp(svc, _sea(svc).game_id, seen=clock - timedelta(hours=7))
        app.refresh_views()
        await pilot.pause()
        summary = str(app.query_one("#pp-summary", Static).render())
        assert "press r" in summary and "about 3 Odds API credits" in summary, summary
        before = svc.budgets["odds_api"].remaining()
        await pilot.press("1", "down", "r")
        await pilot.pause()
        assert isinstance(app.screen, AskScreen)
        assert "3 Odds API credits" in app.screen.text
        assert str(app.screen.query_one("#ask-text", Static).render()).endswith("(press y or n)")
        await pilot.click("#no")
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert not isinstance(app.screen, AskScreen) and svc.budgets["odds_api"].remaining() == before
        await pilot.press("r")
        await pilot.pause()
        await pilot.click("#yes")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "Loading PrizePicks lines for 1 game" in "\n".join(log_lines(app))


def seed_pp6(svc, sea_game) -> None:
    """Six sportsbook-backed lines over two games (three per game, both teams), for a full 6-pick entry."""
    from dataclasses import replace as _replace

    from gridiron_edge.types import PropQuote
    phi = _replace(sea_game, game_id="nfl:20261102:DAL@PHI", home="PHI", away="DAL")
    svc.registry.upsert(phi)
    seen = NOW - timedelta(minutes=5)
    out = []
    for i, (gid, player, team, stat, line) in enumerate((
            (sea_game.game_id, "Geno Smith", "SEA", "pass_yds", 229.5),
            (sea_game.game_id, "Caleb Williams", "CHI", "pass_yds", 219.5),
            (sea_game.game_id, "Kenneth Walker", "SEA", "rush_yds", 64.5),
            (phi.game_id, "Jalen Hurts", "PHI", "pass_yds", 239.5),
            (phi.game_id, "Dak Prescott", "DAL", "pass_yds", 249.5),
            (phi.game_id, "Saquon Barkley", "PHI", "rush_yds", 84.5))):
        over = 1.52 + 0.01 * i
        out.append(PropQuote("odds_api", "prizepicks", gid, player, stat, "over", line, None, seen, fetched_at=seen))
        under = round(1 / (1.045 - 1 / over), 3)
        for book in ("draftkings", "fanduel"):
            out += [PropQuote("odds_api", book, gid, player, stat, side, line, dec, seen, fetched_at=seen)
                    for side, dec in (("over", over), ("under", under))]
        svc.player_teams[player] = team
    svc.latest["props"] = out


async def test_prizepicks_fits_at_100_by_30_with_six_picks(services_mocked):
    """ALL LINES and the 'Press c' line stay on screen with a full entry on a small terminal: Home scrolls
    YOUR ENTRY to the top while picking."""
    svc = services_mocked(pp_max_per_game=3)
    svc.ledger.deposit(100_000)
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=(100, 30)) as pilot:
        await app.workers.wait_for_complete()
        seed_pp6(svc, next(g for g in svc.registry.all_since(NOW - timedelta(days=1)) if g.home == "SEA"))
        app.refresh_views()
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        board = app.query_one("#pp-board", PickList)
        board.focus()
        await pilot.pause()
        for _ in range(20):
            if len(app._pp_selected) >= 6:
                break
            await pilot.press("down", "enter")
            await pilot.pause()
        assert len(app._pp_selected) == 6, log_lines(app)[-3:]
        pane = app.query_one("#home-scroll").region
        keys = app.query_one("#pp-entry-keys", Static)
        assert "Press c after you place it on PrizePicks" in str(keys.render())
        for w in (keys, board, app.query_one("#pp-entry-box")):  # Home scrolls on below ALL LINES
            assert w.region.height > 0 and pane.contains_region(w.region), (w.id, w.region, pane)
        assert board.region.height >= 4 and app.focused is board


def _watched(svc, n: int) -> list:
    """n watch-only Kalshi ideas on the SEA game (LOGGED, not texted), as the scan saves them."""
    from gridiron_edge.types import Leg, Recommendation
    g = _sea(svc)
    out = []
    for i in range(n):
        leg = Leg("kalshi", f"SEA by {i}.5", "yes", 0.40, 0.47, g.game_id, ticker=f"KXNFLSPREAD-SEA{i}", line=i + 0.5)
        rec = Recommendation(None, "kalshi_game", [leg], 0, 25, 0.05, 0.47, None, NOW, NOW + timedelta(hours=3),
                             observe_only=True)
        out.append(svc.lifecycle.upsert_recommendation(rec, NOW)[0])
    return out


async def test_watching_shows_every_idea_and_records_one_you_placed(services_mocked):
    svc = services_mocked()
    svc.ledger.deposit(100_000)
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=(120, 50)) as pilot:
        await app.workers.wait_for_complete()
        recs = _watched(svc, 11)
        app.refresh_views()
        await pilot.pause()
        watch = app.query_one("#watching", WatchList)
        assert watch.option_count == 11  # the whole list, not the first 8
        assert app.query_one("#watch-summary").text == "Click an idea or press Enter on it to record that you " \
                                                       "placed it; d explains it."
        await pilot.press("down")  # nothing to place or pick: Down from the command bar lands in WATCHING
        await pilot.pause()
        assert app.focused is watch
        top = watch.highlighted_option.id
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, WhyScreen) and app.screen.lines[0].startswith("Watch-only: not texted")
        await pilot.press("escape", "enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and str(app.screen.rec.id) == top
        assert app.screen.query_one("#qty", Input).value == ""  # watch-only: we suggest no amount
        await pilot.click("#ok")
        await pilot.pause()
        assert "Type how many contracts" in str(app.screen.query_one("#error", Static).render())
        app.screen.query_one("#qty", Input).value = "10"
        await pilot.pause(0.6)  # past the double-click window, so this is a fresh click
        await pilot.click("#ok")
        await pilot.pause()
        (bet,) = svc.lifecycle.open_bets()
        assert str(bet["rec_id"]) == top and bet["qty"] == 10 and bet["stake_cents"] == 400
        assert "recorded from idea #" + top in "\n".join(log_lines(app))
        assert watch.option_count == 10 and app.query_one("#open-bets", OpenBetsList).option_count == 1
        await pilot.click(watch, offset=(4, 1))  # a click on a row records it too
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and app.screen.rec.id in {r.id for r in recs}
        await pilot.press("escape")
        await pilot.pause()
        assert "not recorded" in log_lines(app)[-1]


async def test_watched_pair_records_at_its_leg_prices_without_a_price_box(services_mocked):
    from gridiron_edge.types import Leg, Recommendation
    svc = services_mocked()
    svc.ledger.deposit(100_000)
    app = GridironApp(svc, clock=lambda: NOW)
    async with app.run_test(size=(120, 50)) as pilot:
        await app.workers.wait_for_complete()
        g = _sea(svc)
        legs = [Leg("kalshi", "SEA by 3.5", "yes", 0.40, 0.5, g.game_id, ticker="KXA"),
                Leg("kalshi", "CHI by 2.5", "yes", 0.55, 0.5, g.game_id, ticker="KXB")]
        rec, _ = svc.lifecycle.upsert_recommendation(Recommendation(
            None, "middle", legs, 0, 5, 0.03, 0.1, None, NOW, NOW + timedelta(hours=3), observe_only=True), NOW)
        app.refresh_views()
        await pilot.pause()
        app.query_one("#watching", WatchList).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and not app.screen.query("#price")
        assert "Recorded at 40 cents and 55 cents" in str(app.screen.query_one("#pair-prices", Static).render())
        app.screen.query_one("#qty", Input).value = "5"
        await pilot.click("#ok")
        await pilot.pause()
        (bet,) = svc.lifecycle.open_bets()
        assert bet["rec_id"] == rec.id and bet["stake_cents"] == 475
