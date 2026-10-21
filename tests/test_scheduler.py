from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from gridiron_edge.scheduler import PROPS_CFB_RESERVE, PROPS_COST, Job, Scheduler, cfb_props_floor
from gridiron_edge.types import Game

PT = ZoneInfo("America/Los_Angeles")
# Wednesday 2026-10-21 12:00 PT: NFL early (Tue) is past, mid (Thu) is not yet.
T0 = datetime(2026, 10, 21, 12, 0, tzinfo=PT).astimezone(timezone.utc)


def game(gid, kickoff, league="nfl", status="scheduled"):
    return Game(game_id=gid, league=league, season=2026, week=7, kickoff=kickoff,
                home="A", away="B", status=status)


class FakeRegistry:
    def __init__(self, games=(), live=()):
        self.games, self._live = list(games), list(live)

    def upcoming(self):
        return self.games

    def live(self):
        return self._live

    def has_open_bets(self):
        return False


class Guard:
    """ok: a flat yes/no; rem: credits left, spent down to a 50-credit floor like BudgetGuard."""

    def __init__(self, ok=True, rem=None):
        self.ok, self.rem = ok, rem

    def remaining(self):
        return self.rem

    def can_spend(self, cost, essential=False):
        return self.ok if self.rem is None else self.rem - cost >= 50


def sched(reg, ok=True, rem=None, **kw):
    return Scheduler(reg, {"odds_api": Guard(ok, rem)}, **kw)


def names(jobs, name):
    return [j for j in jobs if j.name == name]


def run_all(s, jobs, now):
    for j in jobs:
        s.mark_done(j, now)


def test_live_scores_every_minute_with_open_bet():
    live = game("g", T0 - timedelta(hours=1), status="live")
    s = sched(FakeRegistry(live=[live]), open_bet_games=lambda: {"g"})
    for dt, expect in ((0, True), (30, False), (61, True)):
        t = T0 + timedelta(seconds=dt)
        jobs = names(s.due(t), "rundown_scores")
        assert bool([j for j in jobs if j.league == "nfl"]) is expect
        run_all(s, jobs, t)


def test_idle_scores_hourly():
    s = sched(FakeRegistry([game("g", T0 + timedelta(days=5))]))
    run_all(s, s.due(T0), T0)
    assert not names(s.due(T0 + timedelta(minutes=59)), "rundown_scores")
    assert names(s.due(T0 + timedelta(minutes=61)), "rundown_scores")


def test_scores_quarter_hour_near_kickoff():
    s = sched(FakeRegistry([game("g", T0 + timedelta(hours=2))]))
    run_all(s, s.due(T0), T0)
    assert not [j for j in names(s.due(T0 + timedelta(minutes=14)), "rundown_scores") if j.league == "nfl"]
    assert [j for j in names(s.due(T0 + timedelta(minutes=16)), "rundown_scores") if j.league == "nfl"]


def _closes(jobs):
    return [j for j in names(jobs, "odds_games") if j.args["window"].startswith("close")]


def _z(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_odds_close_window_once_per_kickoff():
    k = datetime(2026, 10, 21, 22, 30, tzinfo=timezone.utc)
    s = sched(FakeRegistry([game("a", k), game("b", k)]), close_games=lambda: {"a"})
    assert not _closes(s.due(T0))
    t = k - timedelta(minutes=10)
    close = _closes(s.due(t))
    assert len(close) == 1 and close[0].essential
    assert close[0].args["commence_from"] == "2026-10-21T22:25:00Z"
    assert close[0].args["commence_to"] == "2026-10-21T22:35:00Z"
    run_all(s, close, t)
    assert not _closes(s.due(t + timedelta(minutes=1)))


def test_odds_close_one_job_per_wanted_kickoff():
    k1 = datetime(2026, 10, 24, 16, 0, tzinfo=timezone.utc)
    k2 = k1 + timedelta(minutes=30)
    k3 = k1 + timedelta(hours=1, minutes=30)
    games = [game("a", k1, "cfb"), game("b", k2, "cfb"), game("c", k3, "cfb"), game("d", k3, "cfb")]
    s = sched(FakeRegistry(games), close_games=lambda: {"a", "c", "d"})
    t = k1 - timedelta(minutes=10)
    close = _closes(s.due(t))
    assert len(close) == 1 and not close[0].essential  # CFB closes never dip into the reserve
    assert close[0].args["commence_from"] == _z(k1 - timedelta(minutes=5))
    run_all(s, close, t)
    assert not _closes(s.due(k2 - timedelta(minutes=10)))  # no bet or rec on b
    later = _closes(s.due(k3 - timedelta(minutes=10)))
    assert len(later) == 1 and later[0].args["commence_to"] == _z(k3 + timedelta(minutes=5))
    run_all(s, later, k3 - timedelta(minutes=10))
    assert not _closes(s.due(k3 - timedelta(minutes=9)))


def test_odds_close_skipped_without_bet_or_rec():
    k = T0 + timedelta(hours=3)
    s = sched(FakeRegistry([game("a", k), game("b", k)]))
    assert not _closes(s.due(k - timedelta(minutes=10)))


def test_live_cfb_slate_without_open_bets_polled_every_five_minutes():
    live = [game(f"c{i}", T0 - timedelta(hours=1), "cfb", status="live") for i in range(60)]
    s = sched(FakeRegistry(live=live), open_bet_games=lambda: {"other"})
    cfb = lambda t: [j for j in names(s.due(t), "rundown_scores") if j.league == "cfb"]  # noqa: E731
    run_all(s, cfb(T0), T0)
    assert not cfb(T0 + timedelta(seconds=61))
    assert not cfb(T0 + timedelta(minutes=4, seconds=59))
    assert cfb(T0 + timedelta(minutes=5))


def test_rundown_lines_every_thirty_minutes():
    s = sched(FakeRegistry([game("g", T0 + timedelta(hours=30))]))
    lines = lambda t: [j for j in names(s.due(t), "rundown_lines") if j.league == "nfl"]  # noqa: E731
    run_all(s, lines(T0), T0)
    assert not lines(T0 + timedelta(minutes=29))
    assert lines(T0 + timedelta(minutes=30))
    assert not names(sched(FakeRegistry([game("g", T0 + timedelta(hours=49))])).due(T0), "rundown_lines")


def test_early_window_once():
    s = sched(FakeRegistry([game("g", T0 + timedelta(days=4))]))
    early = [j for j in names(s.due(T0), "odds_games") if j.args["window"] == "early"]
    assert len(early) == 1 and not early[0].essential
    run_all(s, early, T0)
    assert not [j for j in names(s.due(T0 + timedelta(hours=5)), "odds_games")
                if j.args["window"] == "early"]


def test_mid_window_after_thursday_morning():
    s = sched(FakeRegistry([game("g", T0 + timedelta(days=4))]))
    mid = lambda t: [j for j in names(s.due(t), "odds_games") if j.args["window"] == "mid"]
    assert not mid(T0)
    thu = datetime(2026, 10, 22, 9, 5, tzinfo=PT)
    assert len(mid(thu)) == 1


def test_props_slate_pull_three_hours_before_kickoff():
    k = T0 + timedelta(hours=2, minutes=30)
    s = sched(FakeRegistry([game(f"g{i}", k) for i in range(14)]))
    assert not names(s.due(k - timedelta(hours=3, minutes=5)), "odds_props")
    props = names(s.due(T0), "odds_props")
    # every game in the kickoff slot loads together, so entries can mix games
    assert len(props) == 14 and {j.args["window"] for j in props} == {"slate"}
    run_all(s, props, T0)
    assert not names(s.due(T0 + timedelta(minutes=30)), "odds_props")


def test_props_slate_pull_late_start_still_runs_once():
    s = sched(FakeRegistry([game("g", T0 + timedelta(minutes=30))]))
    props = names(s.due(T0), "odds_props")
    assert [j.args["window"] for j in props] == ["slate"]
    run_all(s, props, T0)
    assert not names(s.due(T0 + timedelta(minutes=5)), "odds_props")


def test_props_near_kickoff_refresh_only_for_prizepicks_games():
    k = T0 + timedelta(minutes=45)
    games = [game("a", k), game("b", k)]
    s = sched(FakeRegistry(games), pp_games=lambda: {"a"})
    for g in games:  # slate pulls done 2.5 h before kickoff
        s.mark_done(Job("odds_props", "nfl", {"game_id": g.game_id, "window": "slate"}), k - timedelta(hours=2, minutes=30))
    props = names(s.due(T0), "odds_props")
    assert [(j.args["game_id"], j.args["window"]) for j in props] == [("a", "refresh")]
    run_all(s, props, T0)
    assert not names(s.due(T0 + timedelta(minutes=5)), "odds_props")


def test_props_refresh_waits_for_the_last_hour():
    k = T0 + timedelta(minutes=90)
    s = sched(FakeRegistry([game("a", k)]), pp_games=lambda: {"a"})
    s.mark_done(Job("odds_props", "nfl", {"game_id": "a", "window": "slate"}), k - timedelta(hours=3))
    assert not names(s.due(T0), "odds_props")


def test_props_no_refresh_right_after_a_late_slate_pull():
    k = T0 + timedelta(minutes=40)
    s = sched(FakeRegistry([game("a", k)]), pp_games=lambda: {"a"})
    s.mark_done(Job("odds_props", "nfl", {"game_id": "a", "window": "slate"}), T0 - timedelta(minutes=5))
    assert not names(s.due(T0), "odds_props")


def test_props_skip_when_budget_low():
    s = sched(FakeRegistry([game("g", T0 + timedelta(hours=1))]), ok=False)
    assert not names(s.due(T0), "odds_props")


def test_props_never_past_the_guard():
    games = [game(f"n{i}", T0 + timedelta(hours=2)) for i in range(10)]
    props = names(sched(FakeRegistry(games), rem=50 + 4 * PROPS_COST).due(T0), "odds_props")
    assert len(props) == 4


def test_props_nfl_first_cfb_only_above_reserve():
    floor = cfb_props_floor(T0)
    assert floor > PROPS_CFB_RESERVE  # also holds back the NFL props still to come this month
    games = [game(f"c{i}", T0 + timedelta(hours=1, minutes=i), "cfb") for i in range(8)]
    games += [game(f"n{i}", T0 + timedelta(hours=2)) for i in range(3)]
    props = names(sched(FakeRegistry(games), rem=floor + 3 * PROPS_COST + 2 * PROPS_COST).due(T0), "odds_props")
    assert [j.league for j in props] == ["nfl"] * 3 + ["cfb"] * 2
    props = names(sched(FakeRegistry(games), rem=floor).due(T0), "odds_props")
    assert [j.league for j in props] == ["nfl"] * 3
    assert len(names(sched(FakeRegistry(games)).due(T0), "odds_props")) == 11  # quota not known yet


def test_cfb_props_floor_shrinks_through_the_month():
    early = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    late = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    assert cfb_props_floor(early) > cfb_props_floor(late) >= PROPS_CFB_RESERVE


def test_props_not_due_outside_three_hours():
    s = sched(FakeRegistry([game("g", T0 + timedelta(hours=3, minutes=10))]))
    assert not names(s.due(T0), "odds_props")


def test_scan_debounced():
    s = sched(FakeRegistry())
    assert not names(s.due(T0), "scan")
    s.mark_done(Job("kalshi_refresh"), T0)
    scan = names(s.due(T0), "scan")
    assert len(scan) == 1
    s.mark_done(scan[0], T0)
    s.request_scan(T0 + timedelta(seconds=10))
    assert not names(s.due(T0 + timedelta(seconds=30)), "scan")
    assert names(s.due(T0 + timedelta(seconds=61)), "scan")


def test_settle_only_with_open_bets_every_five_minutes():
    assert not names(sched(FakeRegistry()).due(T0), "settle")
    s = sched(FakeRegistry(), has_open_bets=lambda: True)
    run_all(s, names(s.due(T0), "settle"), T0)
    assert not names(s.due(T0 + timedelta(minutes=4)), "settle")
    assert names(s.due(T0 + timedelta(minutes=5)), "settle")


def test_weekly_refresh_after_tuesday_six():
    s = sched(FakeRegistry())
    run_all(s, names(s.due(T0), "cfbd_weekly") + names(s.due(T0), "nflverse_refresh"), T0)
    assert not names(s.due(T0 + timedelta(days=2)), "cfbd_weekly")
    tue = datetime(2026, 10, 27, 6, 5, tzinfo=PT)
    assert names(s.due(tue), "cfbd_weekly") and names(s.due(tue), "nflverse_refresh")


def test_kalshi_cadence_and_expire():
    s = sched(FakeRegistry([game("g", T0 + timedelta(hours=5))]))
    run_all(s, s.due(T0), T0)
    assert not names(s.due(T0 + timedelta(seconds=59)), "expire")
    assert names(s.due(T0 + timedelta(seconds=61)), "expire")
    assert names(s.due(T0 + timedelta(minutes=4)), "kalshi_refresh")
    far = sched(FakeRegistry([game("g", T0 + timedelta(days=5))]))
    run_all(far, far.due(T0), T0)
    assert not names(far.due(T0 + timedelta(minutes=10)), "kalshi_refresh")
    assert names(far.due(T0 + timedelta(minutes=31)), "kalshi_refresh")


def test_next_check_follows_kalshi_refresh_pace():
    s = sched(FakeRegistry([game("g", T0 + timedelta(hours=2))]))
    assert s.next_check(T0) == T0  # never refreshed: due now
    s.mark_done(Job("kalshi_refresh"), T0)
    assert s.next_check(T0 + timedelta(minutes=1)) == T0 + timedelta(minutes=3)
    far = sched(FakeRegistry([game("g", T0 + timedelta(days=3))]))
    far.mark_done(Job("kalshi_refresh"), T0)
    assert far.next_check(T0) == T0 + timedelta(minutes=30)
