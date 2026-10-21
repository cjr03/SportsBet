"""Settings: strategy defaults, behaviour flags, secrets from .env."""
import json
import math
import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from dotenv import dotenv_values

# Fractions that must lie in [0, 1]; kelly_fraction is stricter, (0, 1].
_FRACTIONS = (
    "kelly_fraction", "cap_bet", "cap_game", "cap_slate", "cap_pp_daily",
    "kalshi_price_lo", "kalshi_price_hi", "arb_fraction", "arb_max_roi",
    "odds_reserve_frac", "nfl_tie_prob", "shadow_clv_min_budget",
    "cap_day", "cap_same_dir", "copula_rho", "shrink_game",
    "shrink_prop_anchored", "shrink_prop_model", "pp_unverified_mult", "arb_min_roi", "middle_max_over",
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
    # model vs market gaps beyond these (pts) are treated as missing information: market only.
    # Fallbacks for the backtest's 95th percentile of |model - closing line|.
    nfl_max_disagree: float = 5.0
    cfb_max_disagree: float = 9.0
    qb_veto: bool = True  # NFL: no model weight when the depth-chart QB1 is not last game's starter
    market_only_live: bool = False  # a gated league's market-only (no model) edges are texted, not observe-only
    pp_margin_market: float = 0.02
    pp_margin_model: float = 0.035
    arb_fraction: float = 0.10
    arb_max_roi: float = 0.08
    odds_reserve_frac: float = 0.10
    nfl_tie_prob: float = 0.004  # P(tie) per NFL game since 2012: Kalshi YES on either team loses it
    # game recs whose fair price is only the soft-book median: observe (log, no stake), block or allow
    median_fair_policy: str = "observe"
    # cumulative caps (fractions of bankroll incl. open stakes): stakes placed per PT day, and
    # same-direction Kalshi legs on one game (stopgap for same-game correlation)
    cap_day: float = 0.20
    cap_same_dir: float = 0.02
    copula_rho: float = 0.3  # Gaussian-copula correlation of same-direction legs in a game; 0 = off
    # Kelly multipliers per probability source (parameter-uncertainty shrinkage)
    shrink_game: float = 1.0
    shrink_prop_anchored: float = 0.75
    shrink_prop_model: float = 0.5
    # rolling closing-line-value throttle for game bets, per league
    clv_throttle_window: int = 50
    clv_throttle_min: int = 10
    # PrizePicks payouts: "fixed" tables or "arena" (only a perfect card pays); structures as
    # "n-kind"; optional JSON table override {"5-flex": {"5": 10, "4": 2, "3": 0.4}}
    pp_mode: str = "fixed"
    pp_payouts_verified: bool = False
    pp_unverified_mult: float = 0.5
    pp_structures: tuple[str, ...] = ("3-power", "4-flex", "5-flex", "6-flex", "5-power", "6-power")
    pp_payouts_json: str = ""
    pp_max_entries: int = 3
    pp_brute_force: bool = False
    pp_max_per_game: int = 2  # picks from one game in an entry (1-3); same-game picks are correlated
    # arbs: quotes older than this are not texted; smallest ROI worth legging into
    arb_stale_seconds: int = 90
    arb_min_roi: float = 0.01
    # ladder middles (not risk-free): max cost over $1 per pair; live = sized and texted
    middle_max_over: float = 0.10
    middles_live: bool = False
    # behaviour
    venues: tuple[str, ...] = ("kalshi", "prizepicks")
    quiet_hours: tuple[int, int] = (23, 8)
    notify_enabled: bool = True
    # game quotes stay fresh past TheRundown's 30-minute lines poll (plus slack)
    stale_seconds: int = 2100
    # props are pulled once near kickoff, so their anchors age from fetch time over a longer window
    props_stale_seconds: int = 10800
    # furthest a prop is repriced from the book's point L (yards / pass yards / counts); rushing and
    # receiving yards also stay within max(props_min_shift_yds, props_max_shift_frac * L) of it
    props_max_shift_yds: float = 15.0
    props_min_shift_yds: float = 5.0
    props_max_shift_frac: float = 0.5
    props_max_shift_pass_yds: float = 30.0
    props_max_shift_count: float = 1.0
    # projector inputs: nflverse snap counts / injury reports (free), usage x efficiency mean
    props_use_snaps_injuries: bool = True
    props_usage_model: bool = True
    # evaluation
    shadow_clv_min_budget: float = 0.5  # extra close pulls for not-placed signals only above this Odds API share
    prop_close_stale_minutes: int = 60  # a prop close older than this before kickoff is labelled stale
    drift_window: int = 50  # rolling bets per segment for drift checks
    cfb_gate_seasons: int = 2  # CFB seasons in the gate backtest (past seasons cache forever)
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


def parse_pp_structure(text: str) -> tuple[int, str]:
    """'5-flex' -> (5, 'flex'); 2-6 picks, power or flex."""
    try:
        n, kind = text.strip().lower().split("-")
        size = int(n)
    except ValueError:
        raise ValueError(f"bad PrizePicks structure {text!r} (use e.g. 5-flex or 3-power)") from None
    if kind not in ("power", "flex") or not 2 <= size <= 6:
        raise ValueError(f"bad PrizePicks structure {text!r} (use e.g. 5-flex or 3-power)")
    return size, kind


def parse_pp_payouts(raw: str) -> dict[tuple[int, str], dict[int, float]]:
    """pp_payouts_json -> {(n, kind): {hits: multiplier}}; tiers 0..n, multipliers finite and >= 0."""
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError("pp_payouts_json is not valid JSON") from None
    if not isinstance(data, dict):
        raise ValueError('pp_payouts_json must look like {"5-flex": {"5": 10, "4": 2}}')
    out: dict[tuple[int, str], dict[int, float]] = {}
    for key, tiers in data.items():
        st = parse_pp_structure(str(key))
        if not isinstance(tiers, dict):
            raise ValueError(f"pp_payouts_json {key}: tiers must be an object of hits -> payout")
        table = {}
        for k, v in tiers.items():
            try:
                hits, mult = int(k), float(v)
            except (TypeError, ValueError):
                raise ValueError(f"pp_payouts_json {key}: {k!r}: {v!r} is not a number") from None
            if not 0 <= hits <= st[0] or not math.isfinite(mult) or mult < 0:
                raise ValueError(f"pp_payouts_json {key}: {k!r}: {v!r} out of range")
            table[hits] = mult
        out[st] = table
    return out


def _validate(s: Settings) -> None:
    for name in _FRACTIONS:
        v = getattr(s, name)
        if not 0.0 <= v <= 1.0:  # also False for NaN
            raise ValueError(f"{name} must be in [0, 1], got {v}")
    if s.kelly_fraction <= 0.0:
        raise ValueError(f"kelly_fraction must be in (0, 1], got {s.kelly_fraction}")
    if s.min_stake_cents < 0 or s.stale_seconds < 0 or s.props_stale_seconds < 0:
        raise ValueError("min_stake_cents, stale_seconds and props_stale_seconds must be >= 0")
    if s.median_fair_policy not in MEDIAN_FAIR_POLICIES:
        raise ValueError(f"median_fair_policy must be one of {', '.join(MEDIAN_FAIR_POLICIES)}")
    if s.pp_mode not in ("fixed", "arena"):
        raise ValueError(f"pp_mode must be fixed or arena, got {s.pp_mode!r}")
    if s.arb_stale_seconds < 0 or s.pp_max_entries < 0 or s.clv_throttle_window < 1:
        raise ValueError("arb_stale_seconds and pp_max_entries must be >= 0, clv_throttle_window >= 1")
    if len(s.quiet_hours) != 2 or not all(0 <= h <= 23 for h in s.quiet_hours):
        raise ValueError(f"quiet_hours must be two hours 0-23, got {s.quiet_hours}")
    if not 1 <= s.pp_max_per_game <= 3:
        raise ValueError(f"pp_max_per_game must be 1, 2 or 3, got {s.pp_max_per_game}")
    for st in s.pp_structures:
        parse_pp_structure(st)
    if s.pp_payouts_json:
        parse_pp_payouts(s.pp_payouts_json)


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
