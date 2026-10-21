"""Every text message and the main dashboard strings, rendered over representative fixtures, read as
plain English: ASCII and short (texts), one action line, and none of the internal jargon."""
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from textual.app import App

from gridiron_edge import describe as d
from gridiron_edge.bets import lifecycle as _lifecycle  # noqa: F401 - registers the bets table
from gridiron_edge.bets import report
from gridiron_edge.config import Settings
from gridiron_edge.notify.format import MAX_LEN, TEST_TEXT, drift_message, format_message
from gridiron_edge.pipeline import PPLine
from gridiron_edge.strategy.prizepicks import payout_table
from gridiron_edge.types import Game, KalshiMarket, Leg, Recommendation
from gridiron_edge.ui import home
from gridiron_edge.ui import prizepicks as ppv
from gridiron_edge.ui.commands import HELP, CommandError, parse_command
from gridiron_edge.ui.widgets import BalanceChart, BankrollHeader, KpiTiles, QuotaBar, compute_kpis

PT = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)  # 10:00 AM PT
GID = "nfl:20261025:DAL@PHI"
CFB = "cfb:20261128:AUB@ALA"
GAME = Game(GID, "nfl", 2026, 8, NOW + timedelta(hours=3), "PHI", "DAL")
CFB_GAME = Game(CFB, "cfb", 2026, 13, NOW + timedelta(hours=4), "ALA", "AUB")
NAMES = d.Names({("cfb", "ALA"): "Alabama", ("cfb", "AUB"): "Auburn"})

BANNED = re.compile(
    r"\bEV\b|\bCLV\b|\bfair\b|YES @|\bx\d|\b[ou]\d+\.?\d*\b|Brier|_yds|\bKX[A-Z]+|\bseg\b|\bgate\b"
    r"|devig|kelly|\bML\b", re.IGNORECASE)
CONFIRM = re.compile(r"When placed, type: confirm \d+$")


def clean(text: str) -> None:
    hits = BANNED.findall(text)
    assert not hits, f"jargon {hits} in: {text}"


def sentence(text: str) -> None:
    clean(text)
    assert text[:1].isupper() or text[:1].isdigit() or text[:1] in "#$", f"starts lowercase: {text}"


def text_ok(text: str, confirm: bool = True) -> None:
    clean(text)
    assert text.isascii(), text
    assert len(text) <= MAX_LEN, (len(text), text)
    assert text[:1].isupper(), text
    if confirm:
        assert CONFIRM.search(text), text
        assert text.count("type: confirm") == 1  # one action per text


def _rec(kind, legs, **kw):
    base = dict(id=41, kind=kind, legs=legs, stake_cents=1880, qty=40, ev=0.05, p_win=0.55, structure=None,
                snapshot_ts=NOW, expires_at=NOW + timedelta(hours=2, minutes=55))
    base.update(kw)
    return Recommendation(**base)


def _k(side="yes", gid=GID, **kw):
    base = dict(venue="kalshi", description="x", side=side, price=0.47, p_fair=0.53, game_id=gid,
                ticker="KXNFLSPREAD-26OCT25DALPHI-PHI3", team="PHI", line=3.5)
    base.update(kw)
    return Leg(**base)


def _pp(i, side="over", stat="pass_yds", line=249.5, player=None):
    return Leg("prizepicks", "x", side, 0.0, 0.58, GID, player=player or f"Player Number {i}", stat=stat,
               line=line, team="PHI" if i % 2 else "DAL")


RECS = {
    "kalshi spread yes": _rec("kalshi_game", [_k()]),
    "kalshi spread no": _rec("kalshi_game", [_k("no", price=0.55, p_fair=0.6)]),
    "kalshi spread negative line": _rec("kalshi_game", [_k(line=-2.5)]),
    "kalshi moneyline": _rec("kalshi_game", [_k(ticker="KXNFLGAME-26OCT25DALPHI-PHI", line=None)]),
    "kalshi total": _rec("kalshi_game", [_k(ticker="KXNFLTOTAL-26OCT25DALPHI-44", team=None, line=44.5)]),
    "kalshi cfb spread": _rec("kalshi_game", [_k(gid=CFB, ticker="KXNCAAFSPREAD-X-ALA7", team="ALA", line=6.5)]),
    "kalshi prop": _rec("kalshi_prop", [_k(ticker="KXNFLPASSYDS-X-250", team=None, player="Jalen Hurts",
                                           stat="pass_yds", line=249.5)]),
    "pp 3-power": _rec("pp_entry", [_pp(i, stat=s) for i, s in enumerate(("pass_yds", "rush_yds", "rec_yds"))],
                       structure="3-power", stake_cents=1000, qty=None, ev=0.08),
    "pp 6-flex long names": _rec("pp_entry", [_pp(i, "under" if i % 2 else "over", "rush_reception_yds", 100.5,
                                                  f"Christian McCaffrey-Longname {i}") for i in range(6)],
                                 structure="6-flex", stake_cents=2000, qty=None, ev=0.06),
    "arb ml pair": _rec("arb", [_k(ticker="KXNFLGAME-X-PHI", line=None, price=0.47),
                                _k(ticker="KXNFLGAME-X-DAL", team="DAL", line=None, price=0.48)],
                        qty=20, structure="kalshi_ml_pair", tie_risk=True, id=43, stake_cents=1900),
    "arb ladder legged": _rec("arb", [_k(line=2.5, price=0.52, worst_price=0.53),
                                      _k("no", line=6.5, price=0.44, worst_price=0.46)],
                              qty=20, structure="kalshi_ladder", id=44, stake_cents=1920),
    "middle ladder": _rec("middle", [_k(line=2.5, price=0.55), _k("no", line=6.5, price=0.5, worst_price=0.51)],
                          qty=20, p_win=0.18, id=45, stake_cents=2100),
    "middle cross cfb": _rec("middle", [_k("no", gid=CFB, team="ALA", line=3.5, price=0.55),
                                        _k("no", gid=CFB, team="AUB", line=1.5, price=0.5)],
                             qty=20, p_win=0.2, id=46, stake_cents=2100),
}


@pytest.mark.parametrize("name", list(RECS))
def test_every_bet_text_is_plain(name):
    text_ok(format_message(RECS[name], PT, NAMES, payout_table(Settings())))


def test_texts_use_full_names_and_12_hour_pacific_times():
    t = format_message(RECS["middle cross cfb"], PT, NAMES)
    assert "Alabama" in t and "Auburn" in t and "ALA" not in t
    t = format_message(RECS["kalshi spread yes"], PT, NAMES)
    assert "Place before 12:55 PM PT." in t and "Eagles" in t and "Cowboys" in t
    assert "0 or more" not in format_message(RECS["kalshi spread negative line"], PT, NAMES)


def test_singular_and_plural_in_texts():
    one = _rec("arb", [_k(ticker="KXNFLGAME-X-PHI", line=None), _k(ticker="KXNFLGAME-X-DAL", team="DAL",
                                                                  line=None)], qty=1, structure="kalshi_ml_pair")
    assert " 1 points" not in format_message(one, PT, NAMES)
    mid = _rec("middle", [_k(line=2.5), _k("no", line=3.5)], qty=5, p_win=0.1)
    assert "win by 3 points" in format_message(mid, PT, NAMES)


def test_notice_texts_are_plain():
    text_ok(TEST_TEXT, confirm=False)
    text_ok(drift_message(report.segment_label("nfl spread"), 50, -0.021, "nfl spread"), confirm=False)
    text_ok(drift_message(report.segment_label("cfb prizepicks"), 50, -0.04, "cfb prizepicks"), confirm=False)


# -- dashboard ---------------------------------------------------------------------------------------
def _market(**kw):
    base = dict(ticker="KXNFLSPREAD-26OCT25DALPHI-PHI3", event_ticker="E", series="S", market_type="spread",
                game_id=GID, team="PHI", line=3.5, player=None, stat=None, yes_bid=0.5, yes_ask=0.51,
                no_bid=0.48, no_ask=0.5, yes_ask_size=100, no_ask_size=100, status="active", result="",
                fee_multiplier=1.0, fetched_at=NOW)
    base.update(kw)
    return KalshiMarket(**base)


@pytest.mark.parametrize("name", list(RECS))
def test_home_and_why_panel_are_plain(name):
    rec = RECS[name]
    games = {GID: GAME, CFB: CFB_GAME}
    for cell in home.place_now_cells(rec, NOW, games, NAMES):
        clean(cell)
    lines = d.why_lines(rec, NOW, NAMES, 0.25, 0.02, settings=Settings())
    for line in lines[:-1]:  # the last line is the [details] line, where internal names are allowed
        sentence(line)
    assert lines[-1].startswith("[details]")


WHYS = (None, {"fair_source": "pinnacle", "books": ["pinnacle", "draftkings"], "p_market": 0.51, "p_model": 0.55,
               "model_weight": 0.3}, {"fair_source": "anchored-median", "books": ["fanduel"]},
        {"kalshi_mid": 0.4, "weight": 0.3, "p_ours": 0.5, "book_line": None},
        {"source": "sportsbook player lines (DraftKings) with the bookmakers' cut removed"})


@pytest.mark.parametrize("name", list(RECS))
def test_home_detail_and_watch_rows_are_plain(name):
    """The PLACE NOW detail line and WATCHING rows, for every kind of idea and every source of our number."""
    from dataclasses import replace
    games = {GID: GAME, CFB: CFB_GAME}
    markets = {m.ticker: m for m in (_market(), _market(ticker="KXNFLGAME-X-PHI", market_type="ml", line=None),
                                     _market(ticker="KXNFLGAME-X-DAL", market_type="ml", team="DAL", line=None))}
    for why in WHYS:
        rec = replace(RECS[name], why=why)
        detail = home.place_now_detail(rec, markets, games, 0.025)
        clean(detail)
        assert "None" not in detail and "_" not in detail, detail
        reason = home.watch_reason(rec, games, {("cfb", "spread"): True}, {("nfl", "ml"): True}, False)
        for cell in home.watch_cells(rec, NOW, games, reason, NAMES):
            clean(cell)
            assert " PT" in cell or not cell.startswith("    "), cell


def test_info_line_and_open_summary_are_plain():
    segs = home.info_segments(NOW, record=(2, 1, 0), day_left=1240, day_cap=3580,
                              next_check=NOW + timedelta(minutes=4), kalshi_at=NOW - timedelta(minutes=1),
                              books_at=NOW - timedelta(minutes=12), props_at=None)
    lines = home.pack(segs, 98)
    sentence(lines[0])
    for line in lines[1:]:
        clean(line)
    assert "$12.40 of today's $35.80 limit left" in segs and "next check 10:04 AM PT" in segs
    assert "no player lines yet" in segs and "Kalshi prices 1 min old" in segs
    assert home.watch_summary(0, 0) == "Nothing on the watch list."
    sentence(home.watch_summary(8, 9))


def test_open_bets_and_empty_reasons_are_plain():
    import json
    from dataclasses import asdict
    live = Game(GID, "nfl", 2026, 8, NOW - timedelta(hours=1), "PHI", "DAL", status="live", home_pts=14,
                away_pts=10, period="Q2", clock="3:00")
    for legs, kind in (([_k()], "kalshi_game"), ([_pp(i) for i in range(3)], "pp_entry")):
        row = dict(id=7, rec_id=41, kind=kind, legs_json=json.dumps([asdict(g) for g in legs]), price=0.47, qty=40,
                   stake_cents=1880, fee_cents=70, structure="3-power", placed_at=NOW.isoformat(), state="PLACED",
                   result=None, payout_cents=None, settled_at=None, clv=0.012)
        v = home.open_bet_view(row, {GID: live}, {_market().ticker: _market()}, NOW, NAMES,
                               lambda g, p, s: 250.0)
        for cell in v.cells():
            clean(cell)
        for seg in home.open_summary([v]):
            clean(seg)
    for kw in (dict(odds_key=False), dict(games=[]), dict(markets=[]), dict(gates={("nfl", m): True for m in
                                                                                   ("ml", "spread", "total")})):
        args = dict(odds_key=True, games=[GAME], markets=[_market()], gates={}, next_check=NOW + timedelta(minutes=2))
        sentence(home.empty_reason(NOW, **{**args, **kw}))


def test_prizepicks_tab_and_status_are_plain():
    ne_chi = Game("nfl:20261025:NE@CHI", "nfl", 2026, 8, NOW + timedelta(hours=5), "CHI", "NE")
    lines = [PPLine(ne_chi.game_id, "Caleb Williams", "CHI", "pass_yds", 249.5, "over", 0.56, True, NOW, True),
             PPLine(ne_chi.game_id, "D'Andre Swift", "CHI", "rush_yds", 60.5, "under", 0.58, False, NOW, True),
             PPLine(ne_chi.game_id, "Drake Maye", None, "pass_tds", 1.5, None, None, False, NOW, True)]
    games = {ne_chi.game_id: ne_chi}
    for blocker in (None, ("one_team", {}), ("few_lines", {"priced": 1, "need": 3, "no_team": 1}),
                    ("few_good", {"good": 0, "need": 3}), ("paused", {}), ("daily_cap", {})):
        view = ppv.PPView(lines, games, blocker, 0, Settings(), {"nfl": True})
        for line in ppv.board_lines(view, NOW, NAMES):
            if line:
                sentence(line.strip())
        sentence(ppv.status_line(view, NOW, NAMES))
    sentence(ppv.status_line(ppv.PPView([], games, ("no_lines", {}), 0, Settings()), NOW, NAMES))


def test_report_summary_is_plain(conn):
    r = report.build_report(conn, NOW)
    r["drift"]["segments"] = {"nfl spread": {"alarm": True, "label": "NFL point-spread bets", "n": 50,
                                             "mean": -0.02}}
    r["drift"]["calibration"]["worse"] = True
    for line in report.summary_lines(r):
        sentence(line)
    assert not any("drift reset" in ln for ln in report.summary_lines(r))  # the pause line comes from services


def test_command_help_and_errors_are_plain():
    for usage, what in HELP:
        clean(what)
    for bad in ("frobnicate", "withdraw", "confirm x", "confirm 3 1.5", "set nope 1", "", "scan nope"):
        with pytest.raises(CommandError) as e:
            parse_command(bad)
        assert str(e.value)[:1].isupper(), str(e.value)


class _Harness(App):
    def __init__(self, *widgets):
        super().__init__()
        self.widgets = widgets

    def compose(self):
        yield from self.widgets


async def test_performance_widgets_are_plain():
    rows = [{"kind": "kalshi_game", "state": "SETTLED", "result": "won", "stake_cents": 100, "fee_cents": 2,
             "payout_cents": 200, "clv": 0.02, "p_win": 0.6, "settled_at": NOW.isoformat()}]
    app = _Harness(KpiTiles(), QuotaBar(), BankrollHeader(), BalanceChart())
    async with app.run_test(size=(160, 40)) as pilot:
        tiles = app.query_one(KpiTiles)
        tiles.update_kpis({**compute_kpis(rows), "warnings": [report.CALIBRATION_WARNING]})
        for line in tiles.text.splitlines():
            sentence(line)
        q = app.query_one(QuotaBar)
        q.update_quota(480, 998, 199000, True)
        shown = str(q.render())
        clean(shown)
        assert "199,000 data points left this month" in shown and "998 calls left this month" in shown
        h = app.query_one(BankrollHeader)
        h.set_balance(10000, 0, 1880, (480, 500))
        await pilot.pause(0.8)
        clean(h.text)
        chart = app.query_one(BalanceChart)
        chart.update_series([(NOW, 10000)], [])
        assert chart.status == "No bankroll history yet"


async def test_startup_and_job_log_lines_are_plain(services_mocked):
    from gridiron_edge.scheduler import Job
    svc = services_mocked()
    lines = await svc.startup(NOW)
    for job in (Job("kalshi_refresh"), Job("rundown_scores", "nfl"), Job("odds_games", "nfl"), Job("scan")):
        lines += await svc.run_job(job, NOW)
    lines += [("api", ln) for ln in svc.quota_lines()]
    assert lines
    for kind, msg in lines:
        sentence(msg)
    text = "\n".join(m for _, m in lines)
    assert " 1 games" not in text and "+28.0" not in text
    for line in svc.report(NOW)["summary"]:
        sentence(line)


def test_scores_tab_is_plain():
    from dataclasses import replace

    from gridiron_edge.types import Quote
    from gridiron_edge.ui import scores as sv
    live = replace(GAME, status="live", home_pts=10, away_pts=14, period="2", clock="4:12",
                   kickoff=NOW - timedelta(hours=1))
    quotes = [Quote("odds_api", "dk", CFB, "spreads", "home", -6.5, 1.9, NOW, NOW),
              Quote("odds_api", "dk", CFB, "totals", "over", 51.5, 1.9, NOW, NOW)]
    m = _market()
    rows = sv.score_rows([live, CFB_GAME, replace(GAME, game_id="nfl:x", status="final", home_pts=3, away_pts=7)],
                         NOW, quotes=quotes, markets=[replace(m, market_type="ml", team="PHI")],
                         chances={CFB: ("AUB", 0.3)}, bet_counts={CFB: 1}, bet_status={GID: "losing"},
                         names=NAMES)
    text = []
    for r in rows:
        sentence(r.head)
        text += [r.head, r.state, *r.details]
        for x in (r.state, *r.details):
            clean(x)
    joined = " ".join(text)
    assert "2nd quarter, 4:12 left" in joined and "qtr" not in joined
    assert "Alabama by 6.5" in joined and "total 51.5 points" in joined and "our win chance: Alabama 70%" in joined
    assert "ALA" not in joined and "your bets: 1" in joined
    assert sv.state_text(GAME, NOW) == "kickoff Sun 1:00 PM PT"


def test_performance_tables_are_plain():
    from gridiron_edge.ui import performance as pv
    rows = [{"id": 3, "kind": "kalshi_game", "legs_json": json.dumps([asdict(_k())]), "structure": None,
             "result": "won", "stake_cents": 1880, "fee_cents": 30, "payout_cents": 4000,
             "settled_at": NOW.isoformat(), "p_win": 0.55},
            {"id": 4, "kind": "pp_entry", "legs_json": json.dumps([asdict(_pp(i)) for i in range(3)]),
             "structure": "3-power", "result": "lost", "stake_cents": 1000, "fee_cents": 0, "payout_cents": 0,
             "settled_at": NOW.isoformat(), "p_win": 0.2}]
    for line in pv.type_table_lines(report.by_type(rows)):
        clean(line)
    for line in pv.money_lines(report.profit_windows(rows, NOW, PT), 10_000, 0):
        sentence(line)
    for line in pv.recent_lines(report.recent_settled(rows), NOW, 100, NAMES):
        clean(line)
        assert " PT " in line
    sentence(pv.type_table_lines([])[0])


def test_prizepicks_best_picks_are_plain():
    ne_chi = Game("nfl:20261025:NE@CHI", "nfl", 2026, 8, NOW + timedelta(hours=5), "CHI", "NE")
    lines = [PPLine(ne_chi.game_id, "Caleb Williams", "CHI", "pass_yds", 249.5, "over", 0.60, True, NOW, True),
             PPLine(ne_chi.game_id, "Drake Maye", "NE", "pass_tds", 1.5, "under", 0.52, True, NOW, True)]
    view = ppv.PPView(lines, {ne_chi.game_id: ne_chi}, None, 0, Settings(), {"nfl": True})
    out = ppv.board_lines(view, NOW, NAMES)
    assert "Best picks now:" in out and "1 of 2 lines clear the needed chance." in out
    for line in out:
        if line:
            sentence(line.strip())
