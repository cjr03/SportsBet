from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from gridiron_edge.bets import prop_log
from gridiron_edge.db import connect
from gridiron_edge.matching import GameRegistry, TeamResolver
from gridiron_edge.types import Game, PropQuote

KICK = datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)
GID = "nfl:20261025:DAL@PHI"


@pytest.fixture
def db(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def q(side, dec, point=60.5, player="A.J. Brown", book="draftkings", gid=GID):
    return PropQuote("odds_api", book, gid, player, "reception_yds", side, point, dec, KICK - timedelta(days=1))


def test_record_keeps_open_and_updates_close(db):
    first, later = KICK - timedelta(days=2), KICK - timedelta(hours=1)
    prop_log.record(db, [q("over", 1.95), q("under", 1.87)], {GID: KICK}, first)
    prop_log.record(db, [q("over", 1.80), q("under", 2.02)], {GID: KICK}, later)
    row = db.execute("SELECT * FROM prop_lines WHERE side='over'").fetchone()
    assert (row["open_dec"], row["close_dec"]) == (1.95, 1.80)
    assert row["open_at"] == first.isoformat() and row["close_at"] == later.isoformat()
    assert prop_log.logged_count(db) == 2


def test_record_ignores_lines_after_kickoff_and_unknown_games(db):
    prop_log.record(db, [q("over", 1.9)], {GID: KICK}, KICK + timedelta(minutes=5))
    prop_log.record(db, [q("over", 1.9, gid="nfl:other")], {GID: KICK}, KICK - timedelta(hours=1))
    assert prop_log.logged_count(db) == 0


class FixedProjector:
    def __init__(self, mean):
        self.mean = mean

    def project(self, player, stat, season, week):
        return (self.mean, 20.0)


def _setup(db, actual_yards):
    GameRegistry(db, TeamResolver(db)).upsert(Game(GID, "nfl", 2026, 8, KICK, "PHI", "DAL"))
    prop_log.record(db, [q("over", 1.91), q("under", 1.91)], {GID: KICK}, KICK - timedelta(days=2))
    prop_log.record(db, [q("over", 1.80), q("under", 2.02)], {GID: KICK}, KICK - timedelta(hours=1))
    return pl.DataFrame({"season": [2026], "week": [8], "player_display_name": ["A.J. Brown"],
                         "receiving_yards": [actual_yards]})


def test_graded_lines_waits_until_played(db):
    weekly = _setup(db, 75.0)
    assert prop_log.graded_lines(db, weekly, FixedProjector(70.0), KICK + timedelta(hours=1)) == []
    lines = prop_log.graded_lines(db, weekly, FixedProjector(70.0), KICK + timedelta(hours=13))
    assert len(lines) == 1
    g = lines[0]
    assert g.over and g.p_open == pytest.approx(0.5) and g.p_close > 0.5 and g.p_model > g.p_close


def test_graded_lines_drops_pushes_and_missing_players(db):
    weekly = _setup(db, 60.5)
    assert prop_log.graded_lines(db, weekly, FixedProjector(70.0), KICK + timedelta(days=1)) == []
    other = weekly.with_columns(pl.lit("Someone Else").alias("player_display_name"))
    assert prop_log.graded_lines(db, other, FixedProjector(70.0), KICK + timedelta(days=1)) == []


def _line(p_model, p_close, over, p_open=None):
    return prop_log.Graded(GID, "x", "reception_yds", 50.5, p_open, p_close, p_model, over)


def test_grade_scores_picks_and_moves():
    lines = [_line(0.60, 0.50, True, 0.45), _line(0.40, 0.50, True, 0.55), _line(0.51, 0.50, False)] * 40
    r = prop_log.grade(lines)
    assert r["n"] == 120 and r["picks"] == 80 and not r["enough"]
    assert r["pick_hit"] == pytest.approx(0.5) and r["pick_fair"] == pytest.approx(0.5)
    assert r["moves"] == 80 and r["moved_toward"] == pytest.approx(1.0)
    text = " ".join(prop_log.summary_lines(r, 500))
    assert "120 graded lines (of 500 logged)" in text and "Too few lines" in text


def test_grade_empty():
    assert prop_log.grade([]) == {"n": 0}
    assert "none graded yet" in prop_log.summary_lines({"n": 0}, 12)[0]
