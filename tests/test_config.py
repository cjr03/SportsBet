import pytest

from gridiron_edge.config import load_settings


def test_defaults(tmp_path):
    s = load_settings(env_file=tmp_path / "missing.env")
    assert s.kelly_fraction == 0.25 and s.cap_bet == 0.02 and s.venues == ("kalshi", "prizepicks")
    assert s.kalshi_base_url == "https://api.elections.kalshi.com/trade-api/v2"


def test_env_file_and_overrides(tmp_path):
    p = tmp_path / ".env"
    p.write_text("ODDS_API_KEY=abc\nNOTIFY_PHONE=+15555550100\n")
    s = load_settings(env_file=p, overrides={"kelly_fraction": "0.5"})
    assert s.odds_api_key == "abc" and s.notify_phone == "+15555550100" and s.kelly_fraction == 0.5


def test_override_rejects_bad_value(tmp_path):
    with pytest.raises(ValueError):
        load_settings(env_file=tmp_path / "x", overrides={"kelly_fraction": "-1"})


def test_real_env_wins_over_file(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("ODDS_API_KEY=fromfile\n")
    monkeypatch.setenv("ODDS_API_KEY", "fromenv")
    assert load_settings(env_file=p).odds_api_key == "fromenv"


def test_does_not_mutate_environ(tmp_path, monkeypatch):
    monkeypatch.delenv("CFBD_API_KEY", raising=False)
    p = tmp_path / ".env"
    p.write_text("CFBD_API_KEY=k\n")
    assert load_settings(env_file=p).cfbd_api_key == "k"
    import os
    assert "CFBD_API_KEY" not in os.environ


def test_kelly_zero_rejected_but_one_ok(tmp_path):
    f = tmp_path / "x"
    with pytest.raises(ValueError):
        load_settings(env_file=f, overrides={"kelly_fraction": "0"})
    assert load_settings(env_file=f, overrides={"kelly_fraction": "1"}).kelly_fraction == 1.0


def test_fraction_out_of_range_and_non_numeric(tmp_path):
    f = tmp_path / "x"
    with pytest.raises(ValueError):
        load_settings(env_file=f, overrides={"cap_bet": "1.5"})
    with pytest.raises(ValueError):
        load_settings(env_file=f, overrides={"cap_game": "abc"})
    assert load_settings(env_file=f, overrides={"cap_bet": "0"}).cap_bet == 0.0


def test_unknown_override_and_int_coercion(tmp_path):
    f = tmp_path / "x"
    with pytest.raises(ValueError):
        load_settings(env_file=f, overrides={"nope": "1"})
    s = load_settings(env_file=f, overrides={"min_stake_cents": "250", "notify_enabled": "false"})
    assert s.min_stake_cents == 250 and s.notify_enabled is False
