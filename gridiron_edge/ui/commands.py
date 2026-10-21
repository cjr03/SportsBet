"""Command-bar parser: text -> Command. Pure; raises one-line CommandError."""
import shlex
from dataclasses import dataclass, field, fields

from gridiron_edge.config import Settings
from gridiron_edge.db import InvalidAmount, parse_amount


@dataclass
class Command:
    name: str
    args: dict = field(default_factory=dict)


class CommandError(Exception):
    pass


COMMAND_WORDS: list[str] = [
    "deposit", "withdraw", "confirm", "skip",
    "scan", "scan games", "scan props", "scan pp", "scan arb", "scan all",
    "refresh", "refresh nfl", "refresh cfb",
    "bets", "bets open", "bets settled",
    "settle", "pp add", "quota",
    "notify test", "notify on", "notify off",
    "set", "venues", "backtest", "fit blend", "report", "drift reset", "help",
]

# (what to type, what it does) for the help command, in plain English
HELP: list[tuple[str, str]] = [
    ("confirm <id> [price] [contracts]", "you placed bet idea <id>; record it (price/contracts if different)"),
    ("skip <id>", "you are not placing bet idea <id>"),
    ("deposit <amount> / withdraw <amount>", "add money to or take money out of the bankroll"),
    ("scan [games|props|pp|arb|all]", "look for bets now"),
    ("refresh [nfl|cfb] [--force]", "pull fresh prices (--force also uses sportsbook credits)"),
    ("bets [open|settled]", "list your open or finished bets"),
    ("settle <id> <won|lost|refund> [payout]", "mark a bet as finished by hand"),
    ('pp add "<player>" <stat> <line> <team> <game_id>', "type in a PrizePicks line yourself"),
    ("quota", "how many free API calls are left"),
    ("notify test|on|off", "send a test text, or turn texts on or off"),
    ("set <setting> <value>", "change a setting"),
    ("venues", "which betting sites are in use"),
    ("backtest <nfl|cfb> <yyyy>-<yyyy>", "test the model on past seasons"),
    ("fit blend", "recheck which markets our model has earned"),
    ("report", "how the bets have done so far"),
    ("drift reset <nfl|cfb> <ml|spread|total|prop|prizepicks> / drift reset all",
     "turn a paused kind of bet back on after our prices fell behind the market"),
    ("keys", "1-4 switch tabs; Down arrow picks a bet; Enter confirms, s skips, d explains it"),
]

_SETTINGS_FIELDS = {f.name for f in fields(Settings)}


def _usage(text: str) -> CommandError:
    return CommandError(f"usage: {text}")


def _int(raw: str, usage: str, minimum: int = 1) -> int:
    try:
        v = int(raw)
    except ValueError:
        raise _usage(usage) from None
    if v < minimum:
        raise _usage(usage)
    return v


def _float(raw: str, usage: str) -> float:
    try:
        v = float(raw)
    except ValueError:
        raise _usage(usage) from None
    if v != v or v in (float("inf"), float("-inf")):
        raise _usage(usage)
    return v


def _amount(raw: str) -> int:
    try:
        return parse_amount(raw)
    except InvalidAmount as e:
        raise CommandError(str(e)) from None


def parse_command(text: str) -> Command:
    try:
        parts = shlex.split(text)
    except ValueError as e:
        raise CommandError(f"could not parse command: {e}") from None
    if not parts:
        raise CommandError("empty command (try help)")
    word, rest = parts[0].lower(), parts[1:]

    match word, rest:
        case ("deposit" | "withdraw"), [amt]:
            return Command(word, {"cents": _amount(amt)})
        case ("deposit" | "withdraw"), _:
            raise _usage(f"{word} <amount>")
        case "confirm", [i, *opt] if len(opt) <= 2:
            u = "confirm <id> [price] [qty]"
            price = qty = None
            if opt:
                price = _float(opt[0], u)
                if not 0 < price < 1:
                    raise CommandError("price must be between 0 and 1 (exclusive)")
            if len(opt) == 2:
                qty = _int(opt[1], u)
            return Command("confirm", {"id": _int(i, u), "price": price, "qty": qty})
        case "confirm", _:
            raise _usage("confirm <id> [price] [qty]")
        case "skip", [i]:
            return Command("skip", {"id": _int(i, "skip <id>")})
        case "skip", _:
            raise _usage("skip <id>")
        case "scan", ([] | [("games" | "props" | "pp" | "arb" | "all")]):
            return Command("scan", {"what": rest[0] if rest else "all"})
        case "scan", _:
            raise _usage("scan [games|props|pp|arb|all]")
        case "refresh", _ if all(r in ("nfl", "cfb", "--force") for r in rest) and len(
                [r for r in rest if r != "--force"]) <= 1 and len(set(rest)) == len(rest):
            league = next((r for r in rest if r != "--force"), None)
            return Command("refresh", {"league": league, "force": "--force" in rest})
        case "refresh", _:
            raise _usage("refresh [nfl|cfb] [--force]")
        case "bets", ([] | [("open" | "settled")]):
            return Command("bets", {"which": rest[0] if rest else "open"})
        case "bets", _:
            raise _usage("bets [open|settled]")
        case "settle", [i, ("won" | "lost" | "refund") as res, *opt] if len(opt) <= 1:
            cents = _amount(opt[0]) if opt else None
            return Command("settle", {"id": _int(i, "settle <id> <won|lost|refund> [payout]"),
                                      "result": res, "cents": cents})
        case "settle", _:
            raise _usage("settle <id> <won|lost|refund> [payout]")
        case "pp", ["add", player, stat, line, team, game_id]:
            u = 'pp add "<player>" <stat> <line> <team> <game_id>'
            return Command("pp add", {"player": player, "stat": stat,
                                      "line": _float(line, u), "team": team, "game_id": game_id})
        case "pp", _:
            raise _usage('pp add "<player>" <stat> <line> <team> <game_id>')
        case "quota" | "venues" | "report" | "help", []:
            return Command(word, {})
        case "notify", [("test" | "on" | "off") as mode]:
            return Command("notify", {"mode": mode})
        case "notify", _:
            raise _usage("notify test|on|off")
        case "set", [name, value]:
            if name not in _SETTINGS_FIELDS:
                raise CommandError(f"unknown setting: {name}")
            return Command("set", {"field": name, "value": value})
        case "set", _:
            raise _usage("set <field> <value>")
        case "backtest", [("nfl" | "cfb") as league, years]:
            u = "backtest <nfl|cfb> <yyyy>-<yyyy>"
            a, sep, b = years.partition("-")
            if not sep:
                raise _usage(u)
            start, end = _int(a, u), _int(b, u)
            if start > end:
                raise _usage(u)
            return Command("backtest", {"league": league, "start": start, "end": end})
        case "backtest", _:
            raise _usage("backtest <nfl|cfb> <yyyy>-<yyyy>")
        case "drift", ["reset", "all"]:
            return Command("drift", {"segment": "all"})
        case "drift", ["reset", ("nfl" | "cfb") as lg, ("ml" | "spread" | "total" | "prop" | "prizepicks") as typ]:
            return Command("drift", {"segment": f"{lg} {typ}"})
        case "drift", _:
            raise _usage("drift reset <nfl|cfb> <ml|spread|total|prop|prizepicks>  or  drift reset all")
        case "fit", ["blend"]:
            return Command("fit", {"what": "blend"})
        case "fit", _:
            raise _usage("fit blend")
        case ("quota" | "venues" | "report" | "help"), _:
            raise _usage(word)
        case _:
            raise CommandError(f"unknown command: {word} (try help)")
