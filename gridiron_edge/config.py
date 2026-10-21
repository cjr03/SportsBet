"""Settings: strategy defaults, behaviour flags, secrets from .env."""
import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from dotenv import dotenv_values

# Fractions that must lie in [0, 1]; kelly_fraction is stricter, (0, 1].
_FRACTIONS = (
    "kelly_fraction", "cap_bet", "cap_game", "cap_slate", "cap_pp_daily",
    "kalshi_price_lo", "kalshi_price_hi", "arb_fraction", "arb_max_roi",
    "odds_reserve_frac", "nfl_tie_prob",
)
MEDIAN_FAIR_POLICIES = ("observe", "block", "allow")

# env var name -> Settings field (secrets and the optional URL)
_ENV_FIELDS = {
    "ODDS_API_KEY": "odds_api_key",
    "CFBD_API_KEY": "cfbd_api_key",
    "THERUNDOWN_API_KEY": "therundown_api_key",
    "NOTIFY_PHONE": "notify_phone",
    "NTFY_TOPIC": "ntfy_topic",
    "KALSHI_BASE_URL": "kalshi_base_url",
}


@dataclass(frozen=True)
class Settings:
    # strategy
    kelly_fraction: float = 0.25
    cap_bet: float = 0.02
    cap_game: float = 0.04
    cap_slate: float = 0.20
    cap_pp_daily: float = 0.10
    min_stake_cents: int = 100
    kalshi_min_ev: float = 0.025
    kalshi_min_edge: float = 0.015
    kalshi_price_lo: float = 0.10
    kalshi_price_hi: float = 0.90
    cfb_min_disagree: float = 2.5
    pp_margin_market: float = 0.02
    pp_margin_model: float = 0.035
    arb_fraction: float = 0.10
    arb_max_roi: float = 0.08
    odds_reserve_frac: float = 0.10
    nfl_tie_prob: float = 0.004  # P(tie) per NFL game since 2012: Kalshi YES on either team loses it
    # game recs whose fair price is only the soft-book median: observe (log, no stake), block or allow
    median_fair_policy: str = "observe"
    # behaviour
    venues: tuple[str, ...] = ("kalshi", "prizepicks")
    quiet_hours: tuple[int, int] = (23, 8)
    notify_enabled: bool = True
    stale_seconds: int = 600
    # secrets
    odds_api_key: str = ""
    cfbd_api_key: str = ""
    therundown_api_key: str = ""
    notify_phone: str = ""
    ntfy_topic: str = ""
    # paths and urls
    kalshi_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    db_path: Path = field(default_factory=lambda: Path("~/.gridiron_edge/ge.sqlite3").expanduser())


def _coerce(name: str, raw: str, default):
    """Parse a string into the type of the field's default."""
    try:
        if isinstance(default, bool):
            v = raw.strip().lower()
            if v in ("1", "true", "yes", "on"):
                return True
            if v in ("0", "false", "no", "off"):
                return False
            raise ValueError(raw)
        if isinstance(default, int):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
        if isinstance(default, Path):
            return Path(raw).expanduser()
        if isinstance(default, tuple):
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            return tuple(int(p) for p in parts) if default and isinstance(default[0], int) else tuple(parts)
        return raw
    except ValueError:
        raise ValueError(f"invalid value for {name}: {raw!r}") from None


def _validate(s: Settings) -> None:
    for name in _FRACTIONS:
        v = getattr(s, name)
        if not 0.0 <= v <= 1.0:  # also False for NaN
            raise ValueError(f"{name} must be in [0, 1], got {v}")
    if s.kelly_fraction <= 0.0:
        raise ValueError(f"kelly_fraction must be in (0, 1], got {s.kelly_fraction}")
    if s.min_stake_cents < 0 or s.stale_seconds < 0:
        raise ValueError("min_stake_cents and stale_seconds must be >= 0")
    if s.median_fair_policy not in MEDIAN_FAIR_POLICIES:
        raise ValueError(f"median_fair_policy must be one of {', '.join(MEDIAN_FAIR_POLICIES)}")
    if len(s.quiet_hours) != 2 or not all(0 <= h <= 23 for h in s.quiet_hours):
        raise ValueError(f"quiet_hours must be two hours 0-23, got {s.quiet_hours}")


def load_settings(env_file: Path | None = None, overrides: dict[str, str] | None = None) -> Settings:
    """Defaults < .env file < real environment < overrides (keyed by field name)."""
    file_vals = dotenv_values(env_file) if env_file is not None else dotenv_values()
    base = Settings()
    updates: dict = {}
    for env_name, fname in _ENV_FIELDS.items():
        val = os.environ.get(env_name) or file_vals.get(env_name)
        if val:
            updates[fname] = val
    names = {f.name for f in fields(Settings)}
    for fname, raw in (overrides or {}).items():
        if fname not in names:
            raise ValueError(f"unknown setting: {fname}")
        updates[fname] = _coerce(fname, str(raw), getattr(base, fname))
    s = replace(base, **updates)
    _validate(s)
    return s


def apply_override(s: Settings, name: str, raw: str) -> Settings:
    """A copy of s with one field parsed from text (the `set` command); validates the result."""
    if name not in {f.name for f in fields(Settings)}:
        raise ValueError(f"unknown setting: {name}")
    new = replace(s, **{name: _coerce(name, str(raw), getattr(Settings(), name))})
    _validate(new)
    return new
