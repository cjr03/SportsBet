import pytest

from gridiron_edge.ui.commands import COMMAND_WORDS, Command, CommandError, parse_command


@pytest.mark.parametrize("text,expected", [
    ("deposit 100", Command("deposit", {"cents": 10000})),
    ("withdraw $1,000.50", Command("withdraw", {"cents": 100050})),
    ("confirm 42 0.47 30", Command("confirm", {"id": 42, "price": 0.47, "qty": 30})),
    ("confirm 42", Command("confirm", {"id": 42, "price": None, "qty": None})),
    ("skip 7", Command("skip", {"id": 7})),
    ("scan", Command("scan", {"what": "all"})),
    ("scan pp", Command("scan", {"what": "pp"})),
    ("refresh", Command("refresh", {"league": None, "force": False})),
    ("refresh cfb --force", Command("refresh", {"league": "cfb", "force": True})),
    ("bets", Command("bets", {"which": "open"})),
    ("bets settled", Command("bets", {"which": "settled"})),
    ("settle 3 won", Command("settle", {"id": 3, "result": "won", "cents": None})),
    ("settle 3 lost $12.50", Command("settle", {"id": 3, "result": "lost", "cents": 1250})),
    ('pp add "Patrick Mahomes" pass_yds 275.5 KC g1',
     Command("pp add", {"player": "Patrick Mahomes", "stat": "pass_yds", "line": 275.5,
                        "team": "KC", "game_id": "g1"})),
    ("pp refresh", Command("pp refresh", {"confirmed": False})),
    ("pp refresh yes", Command("pp refresh", {"confirmed": True})),
    ("quota", Command("quota", {})),
    ("notify test", Command("notify", {"mode": "test"})),
    ("set kelly_fraction 0.5", Command("set", {"field": "kelly_fraction", "value": "0.5"})),
    ("venues", Command("venues", {})),
    ("backtest nfl 2018-2025", Command("backtest", {"league": "nfl", "start": 2018, "end": 2025})),
    ("fit blend", Command("fit", {"what": "blend"})),
    ("props check", Command("props check", {})),
    ("report", Command("report", {})),
    ("drift reset nfl spread", Command("drift", {"segment": "nfl spread"})),
    ("drift reset all", Command("drift", {"segment": "all"})),
    ("help", Command("help", {})),
])
def test_parses(text, expected):
    assert parse_command(text) == expected


@pytest.mark.parametrize("text", [
    "withdraw -5", "deposit abc", "confirm x", "confirm 42 1.5", "frobnicate",
    "set nosuch 1", "", 'pp add "unterminated',
    "withdraw", "confirm 42 0.5 0", "scan bogus", "settle 3 maybe",
    "backtest nfl 2025-2018", "notify maybe", "drift reset nba spread", "drift", "skip", "quota extra",
    "pp refresh now", "pp",
])
def test_rejects(text):
    with pytest.raises(CommandError) as e:
        parse_command(text)
    assert "\n" not in str(e.value)


def test_unknown_message_and_words():
    with pytest.raises(CommandError, match=r"Unknown command: frobnicate \(try: help\)"):
        parse_command("frobnicate")
    with pytest.raises(CommandError, match=r"Usage: withdraw <amount>"):
        parse_command("withdraw")
    assert "pp add" in COMMAND_WORDS and "notify test" in COMMAND_WORDS


def test_help_covers_every_command_in_plain_english():
    from gridiron_edge.ui.commands import HELP

    usages = " ".join(u for u, _ in HELP)
    for word in {w.split()[0] for w in COMMAND_WORDS} - {"help"}:
        assert word in usages, word
    for _, what in HELP:
        assert not any(j in what for j in ("EV", "CLV", "Brier", "fair"))


def test_backtest_years_backwards_says_what_is_wrong():
    with pytest.raises(CommandError, match=r"^The first year must come before the second, e\.g\. backtest nfl 2018-2025$"):
        parse_command("backtest nfl 2025-2018")
