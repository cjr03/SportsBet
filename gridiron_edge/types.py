"""Shared domain types."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

League = Literal["nfl", "cfb"]


def make_game_id(league: str, kickoff: datetime, away: str, home: str) -> str:
    """Canonical game id; away/home must already be canonical team codes."""
    return f"{league}:{kickoff:%Y%m%d}:{away}@{home}"


@dataclass(frozen=True)
class Game:
    game_id: str
    league: League
    season: int
    week: int | None
    kickoff: datetime
    home: str
    away: str
    neutral: bool = False
    status: str = "scheduled"
    home_pts: int | None = None
    away_pts: int | None = None
    period: str | None = None
    clock: str | None = None


@dataclass(frozen=True)
class Quote:
    source: str
    book: str
    game_id: str
    market: Literal["h2h", "spreads", "totals"]
    side: Literal["home", "away", "over", "under"]
    point: float | None
    decimal: float
    last_update: datetime  # the book's own stamp: when the price last changed
    fetched_at: datetime | None = None  # when we pulled it; staleness is measured from here

    @property
    def seen_at(self) -> datetime:
        return self.fetched_at or self.last_update


@dataclass(frozen=True)
class PropQuote:
    source: str
    book: str
    game_id: str
    player: str
    stat: str  # Odds API market key minus "player_", e.g. "reception_yds"
    side: Literal["over", "under"]
    point: float
    decimal: float | None
    last_update: datetime
    multiplier: float | None = None


@dataclass(frozen=True)
class KalshiMarket:
    ticker: str
    event_ticker: str
    series: str
    market_type: Literal["ml", "spread", "total", "prop"]
    game_id: str | None
    team: str | None
    line: float | None
    player: str | None
    stat: str | None
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    yes_ask_size: float
    no_ask_size: float
    status: str
    result: str
    fee_multiplier: float
    fetched_at: datetime
    rules_secondary: str = ""  # settlement fine print (tie handling for NFL moneylines)


@dataclass(frozen=True)
class Leg:
    venue: str
    description: str
    side: str
    price: float
    p_fair: float
    game_id: str | None
    ticker: str | None = None
    player: str | None = None
    stat: str | None = None
    line: float | None = None
    team: str | None = None

    def identity(self) -> str:
        if self.ticker:
            return self.ticker
        return f"{self.player}|{self.stat}|{self.line}|{self.side}"


@dataclass
class Recommendation:
    id: int | None
    kind: Literal["kalshi_game", "kalshi_prop", "pp_entry", "arb"]
    legs: list[Leg]
    stake_cents: int
    qty: int | None
    ev: float
    p_win: float
    structure: str | None
    snapshot_ts: datetime
    expires_at: datetime
    state: str = "RECOMMENDED"
    observe_only: bool = False
    tie_risk: bool = False
    # machine-readable reasons, e.g. {"fair_source": "pinnacle", "books": [...], "p_market": .52,
    # "p_model": .55, "model_weight": .3}; the UI renders it, nothing downstream depends on it
    why: dict | None = None

    @property
    def leg_key(self) -> str:
        """Hash of kind and leg identities, ignoring prices (detects price moves)."""
        ids = sorted(leg.identity() for leg in self.legs)
        return hashlib.sha1("\n".join([self.kind, *ids]).encode()).hexdigest()

    @property
    def dedupe_key(self) -> str:
        """Hash of kind, leg identities and prices rounded to 0.01."""
        parts = sorted(f"{leg.identity()}@{leg.price:.2f}" for leg in self.legs)
        return hashlib.sha1("\n".join([self.kind, *parts]).encode()).hexdigest()
