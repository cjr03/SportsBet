from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from gridiron_edge.db import (
    InsufficientFunds, InvalidAmount, Ledger, connect, parse_amount, register_schema,
)


def test_deposit_withdraw_balance(conn):
    L = Ledger(conn); L.deposit(10_000); L.withdraw(2_550)
    assert L.balance() == 7_450 and [c for _, c in L.history()] == [10_000, 7_450]


def test_withdraw_more_than_balance_rejected(conn):
    L = Ledger(conn); L.deposit(500)
    with pytest.raises(InsufficientFunds): L.withdraw(501)
    assert L.balance() == 500


@pytest.mark.parametrize("txt,cents", [("100", 10_000), ("100.5", 10_050), ("$1,000.25", 100_025)])
def test_parse_amount_ok(txt, cents): assert parse_amount(txt) == cents


@pytest.mark.parametrize("txt", ["-5", "0", "abc", "1.234", "", "1e9", "nan", "inf", "10000000.01"])
def test_parse_amount_bad(txt):
    with pytest.raises(InvalidAmount): parse_amount(txt)


def test_parse_amount_max_ok(): assert parse_amount("10,000,000") == 1_000_000_000


def test_stake_cannot_exceed_balance(conn):
    L = Ledger(conn); L.deposit(1_000)
    with pytest.raises(InsufficientFunds): L.post("stake", -1_001, bet_id=1)


def test_post_signs_and_kinds(conn):
    L = Ledger(conn); L.deposit(1_000)
    L.post("stake", -400, bet_id=1); L.post("payout", 800, bet_id=1); L.post("refund", 5, bet_id=2)
    assert L.balance() == 1_405
    with pytest.raises(InvalidAmount): L.post("stake", 400, bet_id=1)
    with pytest.raises(InvalidAmount): L.post("payout", -1)
    with pytest.raises(InvalidAmount): L.post("bogus", 1)
    with pytest.raises(InvalidAmount): L.post("deposit", 0)
    assert L.balance() == 1_405


def test_post_nests_in_outer_transaction(conn):
    L = Ledger(conn); L.deposit(1_000)
    conn.execute("SAVEPOINT outer")
    L.post("stake", -300, bet_id=1)
    conn.execute("ROLLBACK TO outer"); conn.execute("RELEASE outer")
    assert L.balance() == 1_000


def test_today_change_uses_local_day(conn):
    L = Ledger(conn); L.deposit(1_000)
    la = ZoneInfo("America/Los_Angeles")
    assert L.today_change(la) == 1_000
    conn.execute("UPDATE ledger SET ts = ?", (datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat(),))
    assert L.today_change(la) == 0


def test_register_schema_runs_on_connect(tmp_path):
    register_schema("CREATE TABLE IF NOT EXISTS zz_test (id INTEGER PRIMARY KEY)")
    c = connect(tmp_path / "sub" / "dir" / "x.db")
    c.execute("INSERT INTO zz_test DEFAULT VALUES")
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    for t in ("settings", "quota", "api_cache", "ledger"):
        c.execute(f"SELECT 1 FROM {t}")


def test_register_column_migrates_existing_db(tmp_path):
    import sqlite3

    from gridiron_edge.db import register_column
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE widgets_t (id INTEGER PRIMARY KEY, name TEXT)")
    old.execute("INSERT INTO widgets_t (name) VALUES ('a')")
    old.commit()
    old.close()
    register_schema("CREATE TABLE IF NOT EXISTS widgets_t (id INTEGER PRIMARY KEY, name TEXT)")
    register_column("widgets_t", "score", "REAL")
    c = connect(path)
    assert c.execute("SELECT name, score FROM widgets_t").fetchone()[:] == ("a", None)
    c.close()
    connect(path).close()  # idempotent


def test_eval_columns_exist(conn):
    rec_cols = {r[1] for r in conn.execute("PRAGMA table_info(recommendations)")}
    bet_cols = {r[1] for r in conn.execute("PRAGMA table_info(bets)")}
    assert {"p_mkt", "p_close_fair", "clv_close", "clv_source", "clv_stale", "clv_at"} <= rec_cols
    assert {"p_close_fair", "clv_source"} <= bet_cols
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='leg_clv'").fetchone()
