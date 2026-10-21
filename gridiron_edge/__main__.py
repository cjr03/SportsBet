"""Entry point: `uv run gridiron-edge [--db PATH]`."""
import argparse
from pathlib import Path

from gridiron_edge.config import Settings, load_settings

# .env name -> (Settings field, what is lost without it)
KEYS = {
    "ODDS_API_KEY": ("odds_api_key", "sportsbook prices and PrizePicks lines are off"),
    "THERUNDOWN_API_KEY": ("therundown_api_key", "live scores and line watching are off"),
    "CFBD_API_KEY": ("cfbd_api_key", "college football ratings and team names are off"),
    "NOTIFY_PHONE": ("notify_phone", "texts show in the Log tab only"),
}


def missing_keys(settings: Settings) -> list[str]:
    return [f"Missing {env} in .env: {effect}." for env, (name, effect) in KEYS.items()
            if not getattr(settings, name)]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gridiron-edge",
                                     description="Find football bets with an expected profit and track your bankroll. It never places bets.")
    parser.add_argument("--db", type=Path, help="database path (default ~/.gridiron_edge/ge.sqlite3)")
    args = parser.parse_args(argv)
    settings = load_settings(overrides={"db_path": str(args.db)} if args.db else None)
    for line in missing_keys(settings):
        print(line)

    from gridiron_edge.app import GridironApp  # heavy imports after the quick checks
    from gridiron_edge.services import build_services

    GridironApp(build_services(settings)).run()


if __name__ == "__main__":
    main()
