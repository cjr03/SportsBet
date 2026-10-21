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


def test_fair_pricing_settings(tmp_path):
    s = load_settings(env_file=tmp_path / "missing.env")
    assert s.median_fair_policy == "observe" and s.nfl_tie_prob == pytest.approx(0.004)
    assert load_settings(env_file=tmp_path / "x", overrides={"median_fair_policy": "block"}).median_fair_policy == "block"
    with pytest.raises(ValueError):
        load_settings(env_file=tmp_path / "x", overrides={"median_fair_policy": "sometimes"})
    with pytest.raises(ValueError):
        load_settings(env_file=tmp_path / "x", overrides={"nfl_tie_prob": "2"})

@pytest.mark.parametrize("name,raw", [
    ("pp_payouts_json", "{bad"),
    ("pp_payouts_json", '{"5-flx": {"5": 10}}'),
    ("pp_payouts_json", '{"abc": {"5": 10}}'),
    ("pp_payouts_json", '{"5-flex": {"5": "x"}}'),
    ("pp_payouts_json", '{"5-flex": {"5": -1}}'),
    ("pp_payouts_json", '{"5-flex": {"9": 2}}'),
    ("pp_payouts_json", '["5-flex"]'),
    ("pp_structures", "5-flx"),
    ("pp_structures", "abc"),
    ("pp_structures", "x-power"),
])
def test_prizepicks_settings_validated_before_save(name, raw):
    from gridiron_edge.config import Settings, apply_override
    with pytest.raises(ValueError):
        apply_override(Settings(), name, raw)


def test_prizepicks_settings_accept_good_values():
    from gridiron_edge.config import Settings, apply_override
    s = apply_override(Settings(), "pp_payouts_json", '{"5-flex": {"5": 9.5, "4": 2, "3": 0.4}}')
    assert s.pp_payouts_json
    assert apply_override(s, "pp_structures", "3-power, 5-flex").pp_structures == ("3-power", "5-flex")
    assert apply_override(s, "pp_payouts_json", "").pp_payouts_json == ""


def test_stale_seconds_outlasts_the_lines_poll():
    from gridiron_edge.config import Settings
    from gridiron_edge.scheduler import RUNDOWN_LINES_EVERY
    # TheRundown refreshes lines every 30 min: game quotes must stay fresh until the next pull
    assert Settings().stale_seconds > RUNDOWN_LINES_EVERY.total_seconds()
