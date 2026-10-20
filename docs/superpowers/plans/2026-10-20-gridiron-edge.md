# Gridiron Edge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Textual terminal app that finds +EV football opportunities on Kalshi and PrizePicks. It sizes them with Kelly, texts the user via iMessage, and tracks a paper bankroll through confirm/settle.

**Architecture:**
- **Strategy core** (`strategy/`, `models/`) is pure, with no I/O. It is unit-tested against the spec's numbers.
- **Clients** (`clients/`) are thin async HTTP wrappers. They normalize every feed into the shared dataclasses in `types.py`, and a per-API budget guards each one.
- **`pipeline`** turns snapshots into `Recommendation`s.
- **`bets/`** moves those through the lifecycle against an append-only sqlite ledger.
- **The Textual app** schedules fetches in workers and renders everything.

**Tech Stack:** Python 3.14, uv, textual 8.2.8, textual-plotext 1.0.1, httpx 0.28.1, tenacity, aiolimiter, nflreadpy 0.1.5 + polars 2.0.0, numpy/scipy/scikit-learn, sqlite3, pytest + pytest-asyncio + respx.

**Spec:** `docs/superpowers/specs/2026-10-20-gridiron-edge-design.md`. Endpoint shapes and sources are in `docs/superpowers/specs/research/`.

## Global Constraints

**Python and dependencies**
- `requires-python = ">=3.14,<3.15"`.
- Pinned versions:
  - UI: `textual==8.2.8`, `textual-plotext==1.0.1`, `plotext==5.3.2`
  - HTTP: `httpx==0.28.1`, `tenacity==9.2.1`, `aiolimiter==1.3.0`
  - Data: `nflreadpy==0.1.5`, `polars==2.0.0`, `pyarrow==26.0.0`
  - Math: `numpy==2.5.4`, `scipy==1.18.1`, `scikit-learn==1.9.1`
  - Other: `pydantic==2.14.1`, `python-dotenv==1.2.4`
  - dev: `pytest==9.1.1`, `pytest-asyncio==1.4.0`, `respx==0.23.1`, `penaltyblog==1.13.1`
- Never add the `cfbd` SDK or `shin`.

**Money and venues**
- Stored money is integer cents everywhere. Floats are allowed only inside strategy math.
- Bettable venues default to `("kalshi", "prizepicks")`. Sportsbooks are pricing references only.
- The app never places bets and never logs into Kalshi or PrizePicks. It never scrapes PrizePicks.

**Secrets**
- Secrets come only from `.env`: `ODDS_API_KEY`, `CFBD_API_KEY`, `THERUNDOWN_API_KEY`, `NOTIFY_PHONE`, `NTFY_TOPIC` (optional), `KALSHI_BASE_URL` (optional).
- The phone number never appears in code or tests. Tests use `+15555550100`.

**HTTP endpoints**

| API | Base URL | Auth | Rate |
|---|---|---|---|
| Kalshi | `https://api.elections.kalshi.com/trade-api/v2` | none | ≤ 2 req/s |
| The Odds API | `https://api.the-odds-api.com/v4` | query param `apiKey` | — |
| TheRundown | `https://therundown.io/api/v2` | header `X-TheRundown-Key` | 1 request per 1.1 s |
| CFBD | `https://api.collegefootballdata.com` | header `Authorization: Bearer <key>` | serialized |

**Strategy defaults** (settings, changeable by the user)

| Setting | Default |
|---|---|
| Kelly fraction | 0.25 |
| Cap per bet/entry | 2% |
| Cap per game | 4% |
| Cap per slate | 20% |
| PrizePicks daily cap | 10% |
| Minimum stake | $1 |
| Kalshi minimum EV | 2.5% |
| Kalshi minimum edge | 1.5 pp |
| Kalshi price band | 0.10–0.90 |
| CFB disagreement | ≥ 2.5 pts |
| PrizePicks margin | +2 pp (market-anchored) / +3.5 pp (own model) |
| Arb bankroll fraction | 10% |
| Arb ROI rejected above | 8% |
| Odds API reserve | 10% |

**Notification text**
- ASCII only, prefixed `GE `.
- PrizePicks texts end with `confirm payout in app`.
- Arb texts include `verify`.
- Every text includes the lines' pull time in PT (`HH:MM PT`) and `id <n>`.

**Commits**
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

These failure modes are the most likely to bite the user. Each has a pinning test in the task that owns it.

1. **Bad command input.**
   - Cases: `withdraw` beyond the balance, negative or non-numeric amounts, `$1,000` style amounts, unknown ids.
   - Expected: a clear error line in the event log, no ledger change, no crash.
   - Tests: Task 2, Task 22.
2. **Missing or invalid API key, or quota exhausted** (401, or `x-requests-remaining: 0`).
   - Expected: the client stops polling that API and shows a banner/event. The rest of the app keeps working.
   - Tests: Task 8, Task 9.
3. **A feed returns a game we can't match** (team-name variants, neutral site, a Kalshi ticker for an unknown team).
   - Expected: it is skipped and logged. It never mis-settles a bet or crashes the scan.
   - Tests: Task 12, Task 17.
4. **Postponed, cancelled or never-final games.**
   - Expected: the bet is refunded after 72 h, with the stake returned.
   - An NFL tie pushes spreads/totals at integer lines and loses both Kalshi ML YES legs.
   - Test: Task 18.
5. **The same opportunity appears in consecutive scans.**
   - Expected: exactly one text and one PENDING row.
   - A moved price creates a new rec and expires the old one.
   - Quiet hours suppress texts other than arbs.
   - Tests: Task 17, Task 19.

---

## File Structure

```
pyproject.toml, .gitignore, .env.example, README.md
gridiron_edge/
  __init__.py
  __main__.py                 `python -m gridiron_edge` -> runs the app
  config.py                   Settings (env + defaults + overrides)
  types.py                    shared dataclasses (Game, Quote, KalshiMarket, PropQuote, Leg, Recommendation)
  db.py                       schema, connection, Ledger
  budget.py                   BudgetGuard per metered API
  matching.py                 team aliases, game registry, Kalshi ticker parsing
  data/nfl_teams.json
  clients/http.py             shared rate-limited client, retry, ApiError/AuthError/QuotaExhausted
  clients/odds_api.py, kalshi.py, therundown.py, cfbd.py, nflverse.py
  strategy/odds.py            conversions
  strategy/devig.py           multiplicative/additive/power/shin
  strategy/distributions.py   margin/total/prop probability models
  strategy/fair.py            sharp consensus fair probability
  strategy/kalshi_pricing.py  fees, effective price, EV, filters
  strategy/kelly.py           single, push-aware, slate optimizer, caps
  strategy/prizepicks.py      payouts, EV, breakeven, growth, entry builder, revert ladder
  strategy/arb.py             Kalshi pair, ladder, generic cross-venue
  models/nfl.py, cfb.py, blend.py, props.py
  pipeline.py                 snapshot -> candidates -> sized Recommendations
  scheduler.py                what to fetch when
  services.py                 wires clients/db/pipeline; runs jobs
  bets/lifecycle.py, settle.py, clv.py
  notify/format.py, imessage.py, ntfy.py, console.py, chain.py
  ui/theme.tcss, ui/widgets.py, ui/screens.py, ui/commands.py
  app.py                      GridironApp
tests/  (mirrors package; fixtures in tests/fixtures/*.json)
```

---

### Task 1: Project scaffold and config

**Files:** Create `pyproject.toml`, `.gitignore`, `.env.example`, `gridiron_edge/__init__.py`, `gridiron_edge/config.py`, `tests/conftest.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `Settings`, a frozen dataclass:
    - Strategy fields, one per default in Global Constraints: `kelly_fraction=0.25`, `cap_bet=0.02`, `cap_game=0.04`, `cap_slate=0.20`, `cap_pp_daily=0.10`, `min_stake_cents=100`, `kalshi_min_ev=0.025`, `kalshi_min_edge=0.015`, `kalshi_price_lo=0.10`, `kalshi_price_hi=0.90`, `cfb_min_disagree=2.5`, `pp_margin_market=0.02`, `pp_margin_model=0.035`, `arb_fraction=0.10`, `arb_max_roi=0.08`, `odds_reserve_frac=0.10`
    - Behaviour fields: `venues=("kalshi","prizepicks")`, `quiet_hours=(23,8)`, `notify_enabled=True`, `stale_seconds=600`
    - Secrets: `odds_api_key`, `cfbd_api_key`, `therundown_api_key`, `notify_phone`, `ntfy_topic` (all `str`, default `""`)
    - Paths and URLs: `kalshi_base_url`, `db_path=Path("~/.gridiron_edge/ge.sqlite3").expanduser()`
  - `load_settings(env_file: Path | None = None, overrides: dict[str, str] | None = None) -> Settings`
  - conftest fixtures:
    - `settings(tmp_path)`: fake keys, phone `+15555550100`, `db_path=tmp_path/"t.db"`
    - `now`: a fixed tz-aware UTC datetime `2026-10-25 17:00Z`

- [ ] **Step 1:** Run `git init`. Write `.gitignore` covering `.env`, `.venv/`, `__pycache__/`, `*.sqlite3`, `.pytest_cache/`.
- [ ] **Step 2:** Write `pyproject.toml`:
  - project `gridiron-edge` with the pinned deps; dev deps go in `[dependency-groups] dev`.
  - script `gridiron-edge = "gridiron_edge.__main__:main"`.
  - `[tool.pytest.ini_options] asyncio_mode = "auto"`.
- [ ] **Step 3:** Run `uv venv --python 3.14 && uv sync --group dev`. Expected: it resolves and installs with no errors.
- [ ] **Step 4:** Write `.env.example` with the six env vars blank. `KALSHI_BASE_URL` goes in commented out, with its default.
- [ ] **Step 5: Write the failing tests**
```python
def test_defaults(tmp_path):
    s = load_settings(env_file=tmp_path / "missing.env")
    assert s.kelly_fraction == 0.25 and s.cap_bet == 0.02 and s.venues == ("kalshi", "prizepicks")
    assert s.kalshi_base_url == "https://api.elections.kalshi.com/trade-api/v2"

def test_env_file_and_overrides(tmp_path):
    p = tmp_path / ".env"; p.write_text("ODDS_API_KEY=abc\nNOTIFY_PHONE=+15555550100\n")
    s = load_settings(env_file=p, overrides={"kelly_fraction": "0.5"})
    assert s.odds_api_key == "abc" and s.notify_phone == "+15555550100" and s.kelly_fraction == 0.5

def test_override_rejects_bad_value(tmp_path):
    with pytest.raises(ValueError):
        load_settings(env_file=tmp_path / "x", overrides={"kelly_fraction": "-1"})
```
- [ ] **Step 6:** Run `uv run pytest tests/test_config.py -v`. Expected: FAIL (ImportError).
- [ ] **Step 7:** Implement `load_settings`:
  - Read the file with `dotenv_values`, without mutating `os.environ`. Real environment variables win over the file.
  - Coerce overrides to the field's type.
  - Fractions must be in [0,1], and `kelly_fraction` in (0,1]. Anything else raises `ValueError`.
- [ ] **Step 8:** Run the tests. Expected: PASS.
- [ ] **Step 9:** Commit `chore: scaffold project and settings`.

### Task 2: Database and ledger

**Files:** Create `gridiron_edge/db.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: `Settings.db_path`
- Produces:
  - `connect(path: Path) -> sqlite3.Connection`
    - Uses WAL and `row_factory = sqlite3.Row`, and creates parent dirs.
    - Runs `migrate(conn)`. Its `CREATE TABLE IF NOT EXISTS` statements cover `settings`, `quota`, `api_cache` and `ledger`. Later tasks add their own tables through `register_schema(sql: str)`. That is a module-level list which `migrate` executes, and each module calls it at import.
  - `class InsufficientFunds(Exception)`, `class InvalidAmount(Exception)`
  - `class Ledger(conn)`:
    - `deposit(cents:int, note="") -> int` and `withdraw(cents:int, note="") -> int`, each returning the entry id.
    - `post(kind:str, cents:int, bet_id:int|None=None, note="") -> int`, where kind is one of `deposit|withdraw|stake|payout|refund`.
    - `balance() -> int`.
    - `history() -> list[tuple[datetime,int]]`: the cumulative balance after each entry.
    - `today_change(tz) -> int`.
  - `ledger` table columns: `id, ts, kind, amount_cents, bet_id, note`. It is append-only, with no update or delete methods.
  - `parse_amount(text:str) -> int` returns cents.
    - Accepts `100`, `100.5`, `$1,000.25`.
    - Raises `InvalidAmount` for a non-number, a value ≤ 0, more than 2 decimals, or a value over 10,000,000.
  - conftest fixture: `conn(tmp_path) = connect(tmp_path / "t.db")`.

- [ ] **Step 1: Write the failing tests**
```python
def test_deposit_withdraw_balance(conn):
    L = Ledger(conn); L.deposit(10_000); L.withdraw(2_550)
    assert L.balance() == 7_450 and [c for _, c in L.history()] == [10_000, 7_450]

def test_withdraw_more_than_balance_rejected(conn):
    L = Ledger(conn); L.deposit(500)
    with pytest.raises(InsufficientFunds): L.withdraw(501)
    assert L.balance() == 500

@pytest.mark.parametrize("txt,cents", [("100", 10_000), ("100.5", 10_050), ("$1,000.25", 100_025)])
def test_parse_amount_ok(txt, cents): assert parse_amount(txt) == cents

@pytest.mark.parametrize("txt", ["-5", "0", "abc", "1.234", "", "1e9", "nan", "inf"])
def test_parse_amount_bad(txt):
    with pytest.raises(InvalidAmount): parse_amount(txt)

def test_stake_cannot_exceed_balance(conn):
    L = Ledger(conn); L.deposit(1_000)
    with pytest.raises(InsufficientFunds): L.post("stake", -1_001, bet_id=1)
```
- [ ] **Step 2:** Run `uv run pytest tests/test_db.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement.
  - Signs: deposit, payout and refund are positive. Withdraw and stake are negative.
  - Parse amounts with `decimal.Decimal`.
  - `post` checks the balance and inserts under a `SAVEPOINT`, so callers can nest it inside a larger transaction on the same `conn`.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: sqlite schema and append-only ledger`.

### Task 3: Shared types, odds conversion, devig

**Files:** Create `gridiron_edge/types.py`, `strategy/__init__.py`, `strategy/odds.py`, `strategy/devig.py`, `tests/strategy/test_odds_devig.py`

**Interfaces:**
- Produces, in `types.py`. These are frozen dataclasses unless noted.
  - `League = Literal["nfl","cfb"]`
  - `Game(game_id, league, season:int, week:int|None, kickoff:datetime, home:str, away:str, neutral:bool=False, status:str="scheduled", home_pts:int|None=None, away_pts:int|None=None, period:str|None=None, clock:str|None=None)`
    - `make_game_id(league, kickoff, away, home) -> str` returns `f"{league}:{kickoff:%Y%m%d}:{away}@{home}"`, using canonical codes.
  - `Quote(source, book, game_id, market:Literal["h2h","spreads","totals"], side:Literal["home","away","over","under"], point:float|None, decimal:float, last_update:datetime)`
  - `PropQuote(source, book, game_id, player:str, stat:str, side:Literal["over","under"], point:float, decimal:float|None, last_update:datetime, multiplier:float|None=None)`
    - `stat` is the Odds API key minus `player_`, e.g. `reception_yds`.
  - `KalshiMarket(ticker, event_ticker, series, market_type:Literal["ml","spread","total","prop"], game_id:str|None, team:str|None, line:float|None, player:str|None, stat:str|None, yes_bid:float, yes_ask:float, no_bid:float, no_ask:float, yes_ask_size:float, no_ask_size:float, status:str, result:str, fee_multiplier:float, fetched_at:datetime)`
  - `Leg(venue, description, side, price:float, p_fair:float, game_id, ticker=None, player=None, stat=None, line=None, team=None)`
  - `Recommendation`, a mutable dataclass:
    - Fields: `id:int|None, kind:Literal["kalshi_game","kalshi_prop","pp_entry","arb"], legs:list[Leg], stake_cents:int, qty:int|None, ev:float, p_win:float, structure:str|None, snapshot_ts:datetime, expires_at:datetime, state:str="RECOMMENDED", observe_only:bool=False, tie_risk:bool=False`
    - Properties:
      - `dedupe_key`: sha1 of kind, plus the sorted leg identities (ticker, or player|stat|line|side), plus prices rounded to 0.01.
      - `leg_key`: the same, without prices. Used to detect price moves.
- Produces, in `odds.py`: `american_to_decimal(a)`, `decimal_to_american(d)`, `implied(d)`
- Produces, in `devig.py`:
  - `devig(decimals: Sequence[float], method) -> list[float]`, where method is one of multiplicative, additive, power or shin.
  - `devig_worst(decimals, side_index) -> float`: the minimum over the four methods.

- [ ] **Step 1: Write the failing tests**
```python
def test_american_decimal():
    assert american_to_decimal(-110) == pytest.approx(1.909090, 1e-5)
    assert american_to_decimal(150) == 2.5 and decimal_to_american(2.5) == 150

@pytest.mark.parametrize("m", ["multiplicative", "additive", "power", "shin"])
def test_symmetric_is_half(m): assert devig([1.9091, 1.9091], m) == pytest.approx([0.5, 0.5], abs=1e-6)

def test_additive_equals_shin_two_way():
    assert devig([1.80, 2.10], "additive")[0] == pytest.approx(0.53968, abs=1e-5)
    assert devig([1.80, 2.10], "shin")[0] == pytest.approx(0.53968, abs=1e-5)

def test_power_sums_to_one_and_matches_oracle():
    import penaltyblog as pb
    p = devig([1.40, 3.20], "power"); assert sum(p) == pytest.approx(1)
    assert p == pytest.approx(list(pb.implied.calculate_implied([1.40, 3.20], method="power").probabilities), abs=1e-6)

def test_worst_case():
    assert devig_worst([1.80, 2.10], 0) == min(devig([1.80, 2.10], m)[0] for m in ("multiplicative","additive","power","shin"))
```
  If penaltyblog 1.13.1's function name differs, change only the oracle call. The expected values stay as written.
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
  - **power:** solve Σ r_i^k = 1 with `scipy.optimize.brentq`, k ∈ [0.2, 5].
  - **shin:** solve for z ∈ (0, 0.5) such that Σ p_i(z) = 1, where p_i = [√(z² + 4(1−z)·r_i²/R) − z] / (2(1−z)).
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: shared types, odds conversion, devig`.

### Task 4: Distributions and fair probability

**Files:** Create `strategy/distributions.py`, `strategy/fair.py`, `tests/strategy/test_fair.py`

**Interfaces:**
- Produces, in `distributions.py`:
  - `MarginModel` protocol: `p_greater(mean, x) -> float`, meaning P(value > x).
  - `NormalModel(sigma)`.
  - `EmpiricalMarginModel(residuals: np.ndarray, sigma_fallback: float)`:
    - It is a discrete distribution over integer residuals, shifted by `mean` rounded to 0.5, so it keeps the key-number mass.
    - It falls back to normal when there are fewer than 500 residuals.
  - `p_push(model, mean, x) -> float`. Nonzero only at integer x for the empirical model.
  - Sigma constants: `NFL_ML_SIGMA=11.16`, `NFL_SPREAD_SIGMA=13.0`, `NFL_TOTAL_SIGMA=13.0`, `CFB_SIGMA=15.75`.
  - `prop_p_over(line, mean, kind:Literal["yards","count"], sd=None) -> float`:
    - yards: Normal(mean, sd), with sd defaulting to `0.6*mean + 5`.
    - count: Poisson(mean), giving P(X ≥ ceil(line)).
  - `implied_mean_from_line(line, p_over, kind, sd=None) -> float` inverts `prop_p_over` using brentq.
- Produces, in `fair.py`:
  - `fair_two_way(quotes, market, point, *, now, stale_seconds=600) -> tuple[float,str] | None`
    - It returns the probability for side A (home or over) and a source label `"pinnacle" | "exchanges" | "median"`.
    - It uses only books with both sides at the same point and `last_update` within the stale window.
    - Preference order: Pinnacle no-vig, then the mean of the no-vig novig/prophetx prices, then the median no-vig across books.
    - Devig method: h2h uses `power`; spreads and totals use `additive`.
  - `fair_spread_mean(quotes, *, now) -> float | None`
    - Returns the sharp home spread (home_line; negative means home favoured), taken at the point whose no-vig home probability is nearest 0.5.
    - Converted to an expected margin it is `-home_line + (0.5 - p_home_cover) * 2 * 13.0 * 1.2533` (a normal correction for the off-0.5 price). Return the expected home margin.
  - `fair_total_mean(quotes, *, now) -> float | None`: the same method applied to totals, returning the expected total.

- [ ] **Step 1: Write the failing tests**
```python
def test_normal_symmetry(): assert NormalModel(13).p_greater(0, 0) == pytest.approx(0.5)
def test_ml_sigma(): assert NormalModel(NFL_ML_SIGMA).p_greater(3, 0) == pytest.approx(0.6060, abs=1e-3)

def test_empirical_keeps_key_number_mass():
    res = np.array([3]*146 + [-3]*146 + [7]*88 + [-7]*88 + list(range(-20, 21))*10)
    m = EmpiricalMarginModel(res, 13)
    assert m.p_greater(0, 2.5) - m.p_greater(0, 3.5) > 0.10
    assert p_push(m, 0, 3) > 0.10 and p_push(NormalModel(13), 0, 3) == 0

def test_prop_roundtrip():
    mu = implied_mean_from_line(62.5, 0.55, "yards")
    assert prop_p_over(62.5, mu, "yards") == pytest.approx(0.55, abs=1e-6) and prop_p_over(70.5, mu, "yards") < 0.55

def q(book, mk, side, d, t, point=None): return Quote("odds_api", book, "g", mk, side, point, d, t)

def test_fair_prefers_pinnacle(now):
    qs = [q("pinnacle","h2h","home",1.8,now), q("pinnacle","h2h","away",2.1,now), q("draftkings","h2h","home",1.6,now), q("draftkings","h2h","away",2.4,now)]
    p, src = fair_two_way(qs, "h2h", None, now=now)
    assert src == "pinnacle" and p == pytest.approx(devig([1.8, 2.1], "power")[0])

def test_fair_ignores_stale_and_falls_back(now):
    old = now - timedelta(hours=2)
    qs = [q("pinnacle","h2h","home",1.8,old), q("pinnacle","h2h","away",2.1,old), q("novig","h2h","home",1.9,now), q("novig","h2h","away",1.95,now)]
    assert fair_two_way(qs, "h2h", None, now=now)[1] == "exchanges"

def test_fair_none_when_one_sided(now):
    assert fair_two_way([q("pinnacle","h2h","home",1.8,now)], "h2h", None, now=now) is None

def test_fair_spread_mean_even_price(now):
    qs = [q("pinnacle","spreads","home",1.95,now,-3.0), q("pinnacle","spreads","away",1.95,now,3.0)]
    assert fair_spread_mean(qs, now=now) == pytest.approx(3.0, abs=1e-6)
```
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: margin distributions and sharp fair probability`.

### Task 5: Kalshi pricing and Kelly sizing

**Files:** Create `strategy/kalshi_pricing.py`, `strategy/kelly.py`, `tests/strategy/test_kalshi_kelly.py`

**Interfaces:**
- Produces, in `kalshi_pricing.py`:
  - `taker_fee(contracts:int, price:float, multiplier=1.0) -> float`: dollars, ceil-to-cent of 0.07·C·P·(1−P)·multiplier.
  - `effective_price(price, contracts, multiplier=1.0) -> float`: P + taker_fee/C.
  - `kalshi_ev(p_fair, p_eff) -> float`: p_fair/p_eff − 1.
  - `passes_filters(p_fair, price, p_eff, size_available, qty, s: Settings, status) -> tuple[bool,str]`
    - It checks, in order: status, then price band, then EV ≥ `kalshi_min_ev`, then edge (p_fair − p_eff) ≥ `kalshi_min_edge`, then depth (size_available ≥ qty).
    - The reason it returns is one of `status | price_band | ev | edge | depth | ok`.
- Produces, in `kelly.py`:
  - `kelly_binary(p, d) -> float`, floored at 0.
  - `kelly_push(pw, pl, d) -> float`: (pw·b − pl)/(b·(pw+pl)), with b = d − 1, floored at 0.
  - `@dataclass Bet(p_win, p_push, d, game_id)`
  - `optimize_slate(bets, fraction, cap_bet, cap_game, cap_slate, seed=0) -> list[float]`
    - **Objective:** maximize E[log(1 + Σ f_i r_i)], where r is d − 1 on a win, 0 on a push, and −1 on a loss.
    - **Scenarios:** exact enumeration when 3^N ≤ 20,000; otherwise 20,000 Monte Carlo draws with a seeded rng.
    - **Solve:** SLSQP, bounds [0, cap_bet/fraction], constraints Σf ≤ cap_slate/fraction and per-game sums ≤ cap_game/fraction. Return the result × fraction.
    - **Fallback:** if SLSQP fails, take independent `kelly_push` × fraction, clip each to cap_bet, then scale per game and per slate down to the caps.
  - `to_cents(fractions, bankroll_cents, min_stake_cents) -> list[int]`: floors each stake, and zeroes any below the minimum.

- [ ] **Step 1: Write the failing tests**
```python
def test_taker_fee_examples():
    assert taker_fee(100, 0.50) == pytest.approx(1.75) and taker_fee(1, 0.50) == pytest.approx(0.02)
    assert taker_fee(10, 0.95) == pytest.approx(0.04)

def test_effective_price_and_ev():
    pe = effective_price(0.46, 40); assert pe == pytest.approx(0.46 + taker_fee(40, .46) / 40)
    assert kalshi_ev(0.512, pe) == pytest.approx(0.512 / pe - 1)

@pytest.mark.parametrize("price,reason", [(0.05, "price_band"), (0.95, "price_band")])
def test_price_band(settings, price, reason):
    assert passes_filters(0.99, price, effective_price(price, 10), 1000, 10, settings, "active") == (False, reason)

def test_filters_reasons(settings):
    assert passes_filters(0.60, 0.50, 0.52, 5, 10, settings, "active") == (False, "depth")
    assert passes_filters(0.60, 0.50, 0.52, 1000, 10, settings, "closed") == (False, "status")
    assert passes_filters(0.53, 0.50, 0.52, 1000, 10, settings, "active") == (False, "ev")
    assert passes_filters(0.60, 0.50, 0.52, 1000, 10, settings, "active") == (True, "ok")

def test_kelly_binary(): assert kelly_binary(0.55, 2.0) == pytest.approx(0.10) and kelly_binary(0.4, 2.0) == 0

def test_slate_single_matches_fractional_kelly():
    assert optimize_slate([Bet(0.55, 0, 2.0, "g1")], 0.25, 1, 1, 1)[0] == pytest.approx(0.025, abs=1e-3)

def test_slate_respects_caps():
    f = optimize_slate([Bet(0.65, 0, 2.0, f"g{i}") for i in range(12)], 0.25, 0.02, 0.04, 0.20)
    assert max(f) <= 0.02 + 1e-9 and sum(f) <= 0.20 + 1e-9

def test_slate_per_game_cap():
    assert sum(optimize_slate([Bet(0.65, 0, 2.0, "g1")] * 3, 0.25, 0.02, 0.04, 0.2)) <= 0.04 + 1e-9

def test_to_cents_min_stake(): assert to_cents([0.0005, 0.02], 100_000, 100) == [0, 2_000]
```
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement. Round fees to the cent with `math.ceil(round(x * 100, 9)) / 100`.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: Kalshi fee-aware pricing and slate Kelly`.

### Task 6: PrizePicks math and entry builder

**Files:** Create `strategy/prizepicks.py`, `tests/strategy/test_prizepicks.py`

**Interfaces:**
- Produces:
  - `Structure = tuple[int, Literal["power","flex"]]`.
  - `PAYOUTS: dict[Structure, dict[int,float]]`, mapping hits → multiplier, seeded from spec §6.3:
    - Power: `(2,"power"):{2:3}`, `(3,"power"):{3:6}`, `(4,"power"):{4:10}`, `(5,"power"):{5:20}`, `(6,"power"):{6:37.5}`
    - Flex: `(2,"flex"):{2:2,1:0.5}`, `(3,"flex"):{3:3,2:1}`, `(4,"flex"):{4:6,3:1.5}`, `(5,"flex"):{5:10,4:2,3:0.4}`, `(6,"flex"):{6:25,5:2,4:0.4}`
  - `ALLOWED = [(3,"power"),(4,"flex"),(5,"flex"),(6,"flex"),(5,"power"),(6,"power")]`.
  - `hit_distribution(ps) -> list[float]`: a Poisson-binomial DP giving the probability of each hit count.
  - `entry_ev(ps, structure, payouts=PAYOUTS) -> float`: Σ P(k)·mult(k) − 1.
  - `breakeven(structure, payouts=PAYOUTS) -> float`: the common per-pick p at which EV = 0, found with brentq on [0.3, 0.9].
  - `kelly_entry(ps, structure, payouts=PAYOUTS) -> tuple[float,float]`: (f*, G*), maximizing Σ P(k)·log(1 + f·(m_k − 1)) over f ∈ [0, 0.99] with `minimize_scalar` bounded.
  - `@dataclass(frozen=True) Pick(player, team, game_id, stat, line, side:Literal["over","under"], p:float, anchored:bool)`.
  - `build_best_entry(picks, s: Settings, allowed=ALLOWED, max_brute=25) -> tuple[list[Pick], Structure, float, float] | None`, returning (legs, structure, ev, G*):
    - **Candidate pool:** keep each player's single best pick, then take the top `max_brute` by p.
    - **Per structure:** a pick qualifies when p ≥ breakeven(structure) + (`pp_margin_market` if anchored, else `pp_margin_model`).
    - **Enumeration:** combinations of size n over the qualifying picks, with at most one pick per game_id and at least two distinct teams. Maximize G*.
    - **Limit:** if C(qualifying, n) exceeds 200,000, greedily take the top-p valid set instead.
    - **Result:** the overall max-G* entry with EV > 0, else None.
  - `revert(structure, dnps:int, ties:int, remaining_teams:int) -> Structure | None`, where None means refund:
    - Each DNP or tie lowers the pick count by one.
    - Flex at 3 → (2,"power").
    - Any 2-pick structure with a DNP → None.
    - If dnps > 0 and remaining_teams < 2 → None.
    - Ties alone never trigger the single-team refund.

- [ ] **Step 1: Write the failing tests**
```python
@pytest.mark.parametrize("st,be", [((2,"power"),.5774),((3,"power"),.5503),((4,"power"),.5623),((5,"power"),.5493),((6,"power"),.5466),
                                   ((2,"flex"),.6180),((3,"flex"),.5774),((4,"flex"),.5503),((5,"flex"),.5425),((6,"flex"),.5421)])
def test_breakevens(st, be): assert breakeven(st) == pytest.approx(be, abs=6e-4)

@pytest.mark.parametrize("st,ev", [((5,"flex"),.269),((6,"flex"),.402),((6,"power"),.428),((3,"power"),.171)])
def test_ev_at_58(st, ev): assert entry_ev([0.58] * st[0], st) == pytest.approx(ev, abs=2e-3)

def test_kelly_5flex():
    f, g = kelly_entry([0.58] * 5, (5, "flex"))
    assert f == pytest.approx(0.0664, abs=2e-3) and g == pytest.approx(0.0078, abs=5e-4)

def test_hit_distribution_sums(): assert sum(hit_distribution([.5, .6, .7])) == pytest.approx(1)

def test_builder_enforces_constraints(settings):
    picks = [Pick(f"P{i}", "KC" if i < 6 else "BUF", f"g{i}", "rush_yds", 50.5, "over", 0.60, True) for i in range(8)]
    legs, st, ev, g = build_best_entry(picks, settings)
    assert st in ALLOWED and len({p.team for p in legs}) >= 2 and len({p.game_id for p in legs}) == len(legs) and ev > 0

def test_builder_none_when_no_edge(settings):
    assert build_best_entry([Pick(f"P{i}", f"T{i}", f"g{i}", "x", 1.5, "over", 0.53, True) for i in range(6)], settings) is None

def test_builder_one_team_only_none(settings):
    assert build_best_entry([Pick(f"P{i}", "KC", f"g{i}", "x", 1.5, "over", 0.65, True) for i in range(6)], settings) is None

@pytest.mark.parametrize("st,dnp,tie,teams,exp", [((6,"flex"),1,0,2,(5,"flex")), ((3,"flex"),1,0,2,(2,"power")),
    ((2,"power"),1,0,2,None), ((2,"flex"),1,0,2,None), ((4,"power"),1,0,1,None), ((4,"power"),0,1,1,(3,"power"))])
def test_revert(st, dnp, tie, teams, exp): assert revert(st, dnp, tie, teams) == exp
```
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: PrizePicks payouts, EV, Kelly, entry builder, revert ladder`.

### Task 7: Arbitrage detection

**Files:** Create `strategy/arb.py`, `tests/strategy/test_arb.py`. Add the `mk` factory to `tests/conftest.py`.

**Interfaces:**
- Consumes: `KalshiMarket`, `Leg`, `effective_price`.
- Produces:
  - `@dataclass Arb(kind:Literal["kalshi_ml_pair","kalshi_ladder","cross_venue"], game_id:str|None, legs:list[Leg], qty:int, cost:float, payout:float, roi:float, tie_risk:bool)`
  - `kalshi_ml_pairs(markets, league_of: Callable[[KalshiMarket], str]) -> list[Arb]`
    - Group by `event_ticker` where `market_type == "ml"`, and keep only groups of exactly 2 active markets.
    - qty = int(min of the two yes_ask_size).
    - cost = Σ effective_price(yes_ask, qty)·qty, payout = qty.
    - Emit an Arb when roi = payout/cost − 1 > 0.
    - tie_risk = (league == "nfl").
  - `kalshi_ladders(markets) -> list[Arb]`
    - Group by (event_ticker, market_type ∈ {spread, total}, team) and sort by line.
    - For each adjacent pair (lo, hi) where `hi.yes_bid > lo.yes_ask`: buy YES on lo at lo.yes_ask and buy NO on hi at hi.no_ask.
    - qty = int(min(lo.yes_ask_size, hi.no_ask_size)).
    - The worst-case payout is qty, because the outcome is never "above hi and below lo".
    - Emit only when roi > 0 after fees.
  - `cross_venue(best: dict[str, tuple[float,float]]) -> tuple[float, dict[str,float]] | None`
    - Input is venue → (decimal for side A or 0, decimal for side B or 0).
    - Use the best A and best B. If S = 1/dA + 1/dB < 1, return roi = 1/S − 1 and stake fractions {venueA: (1/dA)/S, venueB: (1/dB)/S}.
  - `filter_arbs(arbs, max_roi) -> list[Arb]`: drops arbs with roi > max_roi or qty < 1.
  - conftest `mk(ticker, event, mtype, team=None, line=None, yes_bid=None, yes_ask=0.5, no_ask=None, yes_ask_size=1000, no_ask_size=1000, status="active", game_id="g")`. Defaults: `no_bid = 1 − yes_ask`, `no_ask = 1 − yes_bid` (with yes_bid defaulting to yes_ask − 0.01).

- [ ] **Step 1: Write the failing tests**
```python
def test_cross_venue_example():
    roi, st = cross_venue({"a": (2.10, 0), "b": (0, 2.05)})
    assert roi == pytest.approx(0.0373, abs=1e-4) and st["a"] == pytest.approx(0.4940, abs=1e-3)

def test_ml_pair_found_with_fees(mk):
    arbs = kalshi_ml_pairs([mk("E1-SEA","E1","ml","SEA",yes_ask=0.45,yes_ask_size=100), mk("E1-CHI","E1","ml","CHI",yes_ask=0.45,yes_ask_size=50)], lambda m: "nfl")
    assert len(arbs) == 1 and arbs[0].qty == 50 and arbs[0].tie_risk and 0 < arbs[0].roi < 0.1

def test_ml_pair_none_when_fees_eat_it(mk):
    assert kalshi_ml_pairs([mk("E1-A","E1","ml","A",yes_ask=0.50), mk("E1-B","E1","ml","B",yes_ask=0.49)], lambda m: "nfl") == []

def test_ladder_violation(mk):
    lo = mk("E2-PHI7","E2","spread","PHI",line=6.5,yes_bid=0.30,yes_ask=0.31)
    hi = mk("E2-PHI8","E2","spread","PHI",line=7.5,yes_bid=0.36,yes_ask=0.37,no_ask=0.64)
    a = kalshi_ladders([lo, hi]); assert len(a) == 1 and a[0].roi > 0

def test_ladder_consistent_none(mk):
    lo = mk("E3-PHI7","E3","spread","PHI",line=6.5,yes_bid=0.16,yes_ask=0.18); hi = mk("E3-PHI8","E3","spread","PHI",line=7.5,yes_bid=0.13,yes_ask=0.14)
    assert kalshi_ladders([lo, hi]) == []

def test_filter_drops_absurd_roi(): assert filter_arbs([Arb("kalshi_ml_pair","g",[],10,5,10,1.0,False)], 0.08) == []
```
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: Kalshi pair/ladder and cross-venue arbitrage`.

### Task 8: HTTP base and budget guard

**Files:** Create `clients/__init__.py`, `clients/http.py`, `budget.py`, `tests/test_budget_http.py`

**Interfaces:**
- Produces, in `clients/http.py`:
  - Exceptions `ApiError`, `AuthError(ApiError)`, `QuotaExhausted(ApiError)`.
  - `RateLimitedClient(base_url, headers=None, rate=2.0, timeout=20, wait=wait_exponential_jitter(1, 8), transport=None)`
    - `async get(path, params=None) -> httpx.Response`.
    - **Retries:** up to 4 attempts on 429 or 5xx. Honour `Retry-After` when present, otherwise use `wait`.
    - **Errors:** 401 or 403 raises `AuthError`. Any other 4xx raises `ApiError`.
    - **Rate limit:** `aiolimiter.AsyncLimiter(rate, 1)`.
    - Also provides `aclose()`.
- Produces, in `budget.py`:
  - `BudgetGuard(conn, api, monthly_cap, reserve_frac)`:
    - `record(remaining, used, last)` inserts a `quota` row: `api, ts, remaining, used, last`.
    - `remaining() -> int | None`.
    - `can_spend(cost, essential=False) -> bool`:
      - Unknown `remaining` → True.
      - `remaining` ≤ 0 → False.
      - Non-essential spends need remaining − cost ≥ reserve_frac·monthly_cap.
      - Essential spends need remaining − cost ≥ 0.
    - `exhausted` property.

- [ ] **Step 1: Write the failing tests**
```python
async def test_retry_then_success(respx_mock):
    r = respx_mock.get("https://x.test/a").mock(side_effect=[httpx.Response(429), httpx.Response(200, json={"ok": 1})])
    c = RateLimitedClient("https://x.test", rate=100, wait=wait_none())
    assert (await c.get("/a")).json() == {"ok": 1} and r.call_count == 2

async def test_auth_error(respx_mock):
    respx_mock.get("https://x.test/a").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthError): await RateLimitedClient("https://x.test", rate=100, wait=wait_none()).get("/a")

def test_budget_reserve(conn):
    b = BudgetGuard(conn, "odds_api", 500, 0.10); b.record(60, 440, 3)
    assert b.can_spend(9) and not b.can_spend(11) and b.can_spend(11, essential=True)
    b.record(0, 500, 3); assert b.exhausted and not b.can_spend(1, essential=True)
```
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement. `register_schema` the `quota` table if Task 2 didn't create it.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: rate-limited HTTP client and API budget guard`.

### Task 9: The Odds API client

**Files:** Create `clients/odds_api.py`, `tests/clients/test_odds_api.py`, `tests/fixtures/odds_api_odds.json`, `tests/fixtures/odds_api_event_props.json`, `tests/fixtures/odds_api_events.json`. Add the `load(name)` fixture to conftest; it reads `tests/fixtures/<name>`.

**Interfaces:**
- Consumes: `RateLimitedClient`, `BudgetGuard`, `Quote`, `PropQuote`, a `resolve: Callable[[League, str, str, datetime], str | None]` (home name, away name, commence time → game_id).
- Produces:
  - `SPORT = {"nfl": "americanfootball_nfl", "cfb": "americanfootball_ncaaf"}`
  - `REF_BOOKS = "pinnacle,novig,prophetx,draftkings,fanduel,betmgm,betrivers,espnbet,hardrockbet,kalshi"`
  - `PROP_BOOKS = "prizepicks,draftkings,fanduel,betmgm,betrivers,espnbet"`
  - `PROP_MARKETS = ("player_pass_yds", "player_rush_yds", "player_reception_yds")`
  - `OddsApiClient(key, conn, resolve, client=None)` with attributes `budget` (BudgetGuard "odds_api", cap 500), `disabled: bool`, `seen_books: set[str]`.
  - `async events(league) -> list[dict]`. Costs 0 credits.
  - `async game_odds(league, commence_from=None, commence_to=None, essential=False) -> list[Quote]`
    - Request params: `markets=h2h,spreads,totals`, `oddsFormat=decimal`, `dateFormat=iso`, `bookmakers=REF_BOOKS`.
    - Before the request, check `budget.can_spend(3, essential)`. If it fails, return `[]` and set `last_status="skipped_budget"`.
    - h2h and spreads outcomes map to home/away by team name. Totals map Over/Under to over/under.
    - Games that don't resolve are skipped and counted in `unmatched`.
  - `async event_props(league, event_id, essential=False) -> list[PropQuote]`
    - Request params: `bookmakers=PROP_BOOKS`, `markets=",".join(PROP_MARKETS)`, `oddsFormat=decimal`, `includeMultipliers=true`.
    - Budget pre-check cost: 3.
    - Skip markets ending in `_alternate`.
    - For `prizepicks`, `decimal` is None, and `multiplier` comes from the outcome's `multiplier` key if present.
  - `async scores(league, days_from=1) -> list[dict]`. Cost 2.
  - Every response records the `x-requests-remaining`, `x-requests-used` and `x-requests-last` headers.
  - `AuthError` sets `disabled = True` and re-raises. Calls made while `disabled` return `[]` without HTTP.

- [ ] **Step 1:** Write the fixtures, minimal but realistic, per `research/brief1.md` §2.1 and `research/round2.md` §3:
  - One NFL game, Bears @ Seahawks, with pinnacle and draftkings on all three markets.
  - An event-props payload with prizepicks and draftkings `player_reception_yds` Over/Under at 62.5, plus one `player_reception_yds_alternate` market.
- [ ] **Step 2: Write the failing tests**
```python
@pytest.fixture
def odds_client(conn): return OddsApiClient("k", conn, resolve=lambda lg, h, a, t: "nfl:20261102:CHI@SEA")

async def test_game_odds_parses_and_records_quota(respx_mock, odds_client, load):
    respx_mock.get(url__regex=r".*/sports/americanfootball_nfl/odds.*").mock(return_value=httpx.Response(200, json=load("odds_api_odds.json"),
        headers={"x-requests-remaining": "497", "x-requests-used": "3", "x-requests-last": "3"}))
    qs = await odds_client.game_odds("nfl")
    assert {q.market for q in qs} == {"h2h", "spreads", "totals"} and all(q.decimal > 1 for q in qs)
    assert odds_client.budget.remaining() == 497 and "pinnacle" in odds_client.seen_books

async def test_budget_refusal_spends_nothing(respx_mock, odds_client):
    odds_client.budget.record(10, 490, 3); route = respx_mock.get(url__regex=r".*/odds.*")
    assert await odds_client.game_odds("nfl") == [] and route.call_count == 0

async def test_props_skip_alternate_and_mark_prizepicks(respx_mock, odds_client, load):
    respx_mock.get(url__regex=r".*/events/.*/odds.*").mock(return_value=httpx.Response(200, json=load("odds_api_event_props.json"), headers={"x-requests-remaining": "480"}))
    ps = await odds_client.event_props("nfl", "evt1")
    assert all(not p.stat.endswith("_alternate") for p in ps) and any(p.book == "prizepicks" and p.decimal is None for p in ps)

async def test_401_disables(respx_mock, odds_client):
    respx_mock.get(url__regex=r".*/odds.*").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthError): await odds_client.game_odds("nfl")
    assert odds_client.disabled and await odds_client.game_odds("nfl") == []

async def test_unmatched_game_skipped(respx_mock, odds_client, load):
    odds_client.resolve = lambda *a: None
    respx_mock.get(url__regex=r".*/odds.*").mock(return_value=httpx.Response(200, json=load("odds_api_odds.json")))
    assert await odds_client.game_odds("nfl") == [] and odds_client.unmatched == 1
```
- [ ] **Step 3:** Run `uv run pytest tests/clients/test_odds_api.py -v`. Expected: FAIL.
- [ ] **Step 4:** Implement.
- [ ] **Step 5:** Run the tests. Expected: PASS.
- [ ] **Step 6:** Commit `feat: The Odds API client with quota guard`.

### Task 10: Kalshi client

**Files:** Create `clients/kalshi.py`, `tests/clients/test_kalshi.py`, `tests/fixtures/kalshi_markets_p1.json`, `tests/fixtures/kalshi_markets_p2.json`, `tests/fixtures/kalshi_orderbook.json`, `tests/fixtures/kalshi_series.json`

**Interfaces:**
- Produces:
  - `GAME_SERIES = {"nfl": {"ml":"KXNFLGAME","spread":"KXNFLSPREAD","total":"KXNFLTOTAL"}, "cfb": {"ml":"KXNCAAFGAME","spread":"KXNCAAFSPREAD","total":"KXNCAAFTOTAL"}}`.
  - `PROP_SERIES_STAT = {"KXNFLRECYDS":"reception_yds","KXNFLRUSHYDS":"rush_yds","KXNFLPASSYDS":"pass_yds"}`. Only these are used; others are skipped.
  - `KalshiClient(base_url, resolve_ticker: Callable[[dict], tuple[str|None, str|None]], client=None)`. `resolve_ticker(raw_market)` returns (game_id, canonical team).
  - `async markets(series, status="open") -> list[KalshiMarket]` follows `cursor` until it is empty, with `limit=100`.
  - `async orderbook(ticker) -> dict[str, list[tuple[float,float]]]` returns `{"yes": [...], "no": [...]}` bids, sorted descending by price.
  - `async fee_multiplier(series) -> float`, from `GET /series/{series}`. Cached; defaults to 1.0.
  - `async market(ticker) -> KalshiMarket`.
  - Field parsing:
    - Prices come from the `*_dollars` strings, parsed with `float`. Sizes come from `*_size_fp`.
    - `market_type` is derived from the series.
    - `line = float(floor_strike)` for spread, total and prop; None for ml.
    - `team` comes from `resolve_ticker`.
    - For props, `player` comes from `yes_sub_title` with the trailing ": N+" stripped, and `stat` from `PROP_SERIES_STAT`.
    - `result` comes from `result` (`""` when unsettled).

- [ ] **Step 1:** Write the fixtures, per `research/kalshi.md` §2–3:
  - Page 1 has a cursor and `KXNFLGAME-26NOV02CHISEA-SEA` (yes_bid 0.58, yes_ask 0.62, no_bid 0.38, no_ask 0.42) plus `-CHI`.
  - Page 2 has an empty cursor and `KXNFLSPREAD-26OCT26DALPHI-PHI8` with floor_strike 7.5.
- [ ] **Step 2: Write the failing tests**
```python
@pytest.fixture
def kalshi(): return KalshiClient("https://k.test/trade-api/v2", resolve_ticker=lambda raw: ("g", raw.get("yes_sub_title")))

async def test_markets_paginates_and_parses(respx_mock, kalshi, load):
    respx_mock.get(url__regex=r".*/markets\?.*").mock(side_effect=[httpx.Response(200, json=load("kalshi_markets_p1.json")), httpx.Response(200, json=load("kalshi_markets_p2.json"))])
    ms = await kalshi.markets("KXNFLGAME")
    sea = next(m for m in ms if m.ticker == "KXNFLGAME-26NOV02CHISEA-SEA")
    assert (sea.yes_bid, sea.yes_ask, sea.no_ask, sea.market_type) == (0.58, 0.62, 0.42, "ml")

async def test_spread_line_from_floor_strike(respx_mock, kalshi, load):
    respx_mock.get(url__regex=r".*/markets\?.*").mock(return_value=httpx.Response(200, json=load("kalshi_markets_p2.json")))
    assert (await kalshi.markets("KXNFLSPREAD"))[0].line == 7.5

async def test_orderbook_bids_sorted(respx_mock, kalshi, load):
    respx_mock.get(url__regex=r".*/orderbook").mock(return_value=httpx.Response(200, json=load("kalshi_orderbook.json")))
    ob = await kalshi.orderbook("T"); assert ob["yes"][0][0] >= ob["yes"][-1][0]

async def test_unknown_team_gets_no_game_id(respx_mock, load):
    k = KalshiClient("https://k.test/trade-api/v2", resolve_ticker=lambda raw: (None, None))
    respx_mock.get(url__regex=r".*/markets\?.*").mock(return_value=httpx.Response(200, json=load("kalshi_markets_p2.json")))
    assert all(m.game_id is None for m in await k.markets("KXNFLSPREAD"))
```
- [ ] **Step 3:** Run the tests. Expected: FAIL.
- [ ] **Step 4:** Implement with `rate=2.0`.
- [ ] **Step 5:** Run the tests. Expected: PASS.
- [ ] **Step 6:** Commit `feat: Kalshi public market-data client`.

### Task 11: TheRundown, CFBD, nflverse clients

**Files:** Create `clients/therundown.py`, `clients/cfbd.py`, `clients/nflverse.py`, `tests/clients/test_therundown.py`, `tests/clients/test_cfbd.py`, `tests/clients/test_nflverse.py`, and fixtures `therundown_events.json`, `cfbd_games.json`, `cfbd_info.json`

**Interfaces:**
- Produces `TheRundownClient(key, conn, resolve, client=None)`
  - Rate is 1/1.1 requests per second. Sends header `X-TheRundown-Key`. `SPORT_ID = {"cfb": 1, "nfl": 2}`.
  - `async scores(league, day: date) -> list[Game]` uses `affiliate_ids=0`. Status mapping:

    | TheRundown status | Our status |
    |---|---|
    | `STATUS_SCHEDULED` | scheduled |
    | `STATUS_IN_PROGRESS`, `STATUS_HALFTIME`, `STATUS_END_PERIOD`, `STATUS_END_OF_REGULATION`, `STATUS_OVERTIME`, `STATUS_DELAYED` | live |
    | `STATUS_FINAL` | final |
    | `STATUS_POSTPONED`, `STATUS_CANCELED`, `STATUS_SUSPENDED`, `STATUS_FORFEIT` | void_candidate |
    | anything else | unknown |

    `period` comes from `game_period` and `clock` from `display_clock`.
  - `async lines(league, day) -> list[Quote]` uses `market_ids=1,2,3`, `affiliate_ids=19,23,22,25`, `main_line=true`.
    - Affiliate map: 19 → draftkings, 23 → fanduel, 22 → betmgm, 25 → kalshi.
    - Convert American prices to decimal. Skip a price of exactly 0.0001.
  - Headers:
    - `X-Datapoints-Monthly-Remaining` goes to BudgetGuard "therundown" (cap 200000).
    - `delay_seconds` comes from `X-Data-Delay-Seconds`.
    - `tier` comes from `X-Tier`.
    - `bookmakers_header` comes from `X-Bookmakers`.
- Produces `CfbdClient(key, conn, client=None)`
  - Requests are serialized with an `asyncio.Lock`.
  - Methods:
    - `async info()` records `remainingCalls` to BudgetGuard "cfbd" (cap 1000).
    - Data endpoints: `games(year, week=None, season_type="regular")`, `lines(year, week)`, `ppa_games(year, week=None)`, `ratings_sp(year)`, `ratings_fpi(year)`, `talent(year)`, `returning(year)`, `teams_fbs(year)`, `player_game_stats(year, week)`.
  - GET caching in the `api_cache` table (`key TEXT PK, fetched_at, immutable, body`):
    - The key is the path plus sorted params.
    - `immutable` is 1 when year < the current season. Otherwise the TTL is 6 h.
- Produces `NflverseSource(cache_dir: Path)`. Its methods are synchronous; callers run them in threads.
  - `schedules(seasons) -> pl.DataFrame` with columns `game_id, season, week, gameday, home_team, away_team, home_score, away_score, result, spread_line, total_line`.
  - `weekly_player_stats(seasons) -> pl.DataFrame`.
  - `team_epa(seasons) -> pl.DataFrame` returns one row per game and team: `season, week, team, off_epa, def_epa, plays`.
  - It uses `nflreadpy`. When a loader raises, it falls back to downloading `https://github.com/nflverse/nflverse-data/releases/download/<tag>/<file>.parquet`.

- [ ] **Step 1: Write the failing tests**, in the same respx style as Task 9:
  - `test_scores_status_mapping`: the fixture has FINAL, IN_PROGRESS and POSTPONED games. Their statuses map to `final`, `live` and `void_candidate`, and the final game's points are ints.
  - `test_lines_skip_sentinel`: a price of 0.0001 does not appear in the output.
  - `test_rundown_records_budget_and_delay`: with `X-Data-Delay-Seconds: 300`, `client.delay_seconds == 300`.
  - `test_cfbd_bearer_and_cache`: the request carries `Authorization: Bearer k`. A second identical `games(2024)` call makes no HTTP request.
  - `test_cfbd_records_info`: `info()` records remainingCalls = 998.
  - `test_nflverse_fallback(monkeypatch)`: monkeypatch `nflreadpy.load_schedules` to raise and `polars.read_parquet` to return a one-row frame. The returned columns include `spread_line`.
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: TheRundown, CFBD, nflverse clients`.

### Task 12: Team matching and game registry

**Files:** Create `matching.py`, `gridiron_edge/data/nfl_teams.json`, `tests/test_matching.py`

**Interfaces:**
- Produces:
  - `nfl_teams.json`: all 32 teams. Each key is the canonical nflverse abbreviation, and each value is `{"names": [...full, nickname, city...], "kalshi": [...codes]}`.
  - `TeamResolver(conn)`, backed by the `team_aliases(league, name_norm, canonical)` table:
    - It seeds NFL from the json. CFB is seeded via `add` from CFBD `teams_fbs` (school, mascot, abbreviation, alt names) in `Services.startup`.
    - `canonical(league, name) -> str | None` normalizes the name to lowercase alphanumerics.
    - `add(league, name, canonical)`.
  - `GameRegistry(conn, teams)` stores games in a table with columns `game_id PK, league, season, week, kickoff_utc, home, away, neutral, status, home_pts, away_pts, period, clock, odds_event_id, updated_at`. Methods:
    - `upsert(game)` and `get(game_id)`.
    - `resolve(league, home_name, away_name, kickoff) -> str | None` canonicalizes both names and matches a stored game within ±36 h of `kickoff`. Swapped home/away is accepted only when the stored game is `neutral`.
    - `upcoming(within, now)`, `live()`, `all_since(dt)`.
    - `update_score(game_id, status, home_pts, away_pts, period, clock)`.
    - `set_odds_event(game_id, event_id)`.
  - `parse_kalshi_event(event_ticker) -> tuple[date, str, str] | None`. The format is `SERIES-YYMONDD{AWAY}{HOME}`; try every split of the team block into two 2–4 letter codes. When `known_codes` is given, prefer the split where both codes are known.
  - `kalshi_resolver(registry, teams) -> Callable[[dict], tuple[str|None, str|None]]` takes the event ticker plus `yes_sub_title`. It resolves the team names via `teams.canonical`, matches the game by date via `registry.resolve`, and returns (game_id, canonical team). Totals have no team; their second value is None.

- [ ] **Step 1: Write the failing tests**
```python
def test_canonical_variants(resolver):
    for n in ["Kansas City Chiefs", "chiefs", "KC", "Kansas City"]: assert resolver.canonical("nfl", n) == "KC"
    assert resolver.canonical("nfl", "Springfield Isotopes") is None

def test_parse_kalshi_event():
    assert parse_kalshi_event("KXNFLGAME-26NOV02CHISEA") == (date(2026, 11, 2), "CHI", "SEA")
    assert parse_kalshi_event("garbage") is None

def test_resolve_game_window(registry, now):
    g = Game("nfl:20261102:CHI@SEA", "nfl", 2026, 9, now, "SEA", "CHI"); registry.upsert(g)
    assert registry.resolve("nfl", "Seattle Seahawks", "Chicago Bears", now + timedelta(hours=2)) == g.game_id
    assert registry.resolve("nfl", "Seattle Seahawks", "Chicago Bears", now + timedelta(days=5)) is None
    assert registry.resolve("nfl", "Chicago Bears", "Seattle Seahawks", now) is None

def test_neutral_site_swapped(registry, now):
    registry.teams.add("cfb", "Alpha", "A"); registry.teams.add("cfb", "Beta", "B")
    g = Game("cfb:20261024:A@B", "cfb", 2026, 8, now, "B", "A", neutral=True); registry.upsert(g)
    assert registry.resolve("cfb", "Alpha", "Beta", now) == g.game_id
```
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: team aliases, game registry, Kalshi ticker parsing`.

### Task 13: NFL and CFB rating models

**Files:** Create `models/__init__.py`, `models/nfl.py`, `models/cfb.py`, `tests/models/test_ratings.py`

**Interfaces:**
- Produces:
  - `@dataclass Prediction(game_id, mean_margin: float, mean_total: float | None, model_version: str)`. `mean_margin` is home minus away.
  - `NflRatings.fit(schedules: pl.DataFrame, team_epa: pl.DataFrame | None, asof: date) -> NflRatings`
    - Uses only games before `asof`.
    - Points model: ridge regression (λ = 8) on margins.
      - Recency weight is 0.5^(age_days/120).
      - Design matrix: +1 for the home team, −1 for the away team, and an HFA column that is 0 at neutral sites.
    - EPA model: per team, (off_epa − def_epa)·65, ridge-fit the same way.
    - Rating = 0.6·R_pts + 0.4·R_epa. With no EPA data, use R_pts alone.
    - The fitted object exposes `predict(home, away, neutral=False) -> float`, `predict_total(home, away) -> float | None` (from the mean of recent team points for and against), `hfa: float` and `ratings: dict[str, float]`.
  - `CfbRatings.fit(games: list[dict], priors: dict[str, float], asof: date, lam_by_week=lambda w: max(2.0, 20.0 - 2.0*w)) -> CfbRatings`
    - Solves (XᵀWX + λI)β = XᵀWy + λ·prior.
    - Margins are capped at ±28.
    - Recency weight is 0.97^weeks_ago.
    - HFA is a fitted column.
    - A team that is not in `priors` gets a prior of −20.
    - The fitted object exposes `predict(home, away, neutral)` and `ratings`.
  - `cfb_prior(prev_rating, returning_ppa, talent) -> dict[str, float]` computes `R0 = 0.6·P + 0.25·P·(RP − 0.5) + 0.15·z(talent)·7`. Defaults are RP = 0.5 and talent z = 0.

- [ ] **Step 1: Write the failing tests** on synthetic data (seed 0): 32 teams, true ratings drawn from N(0, 6), HFA 2.0, noise σ = 12, three 17-game seasons.
  - `test_nfl_recovers_ratings`: the Pearson correlation between fitted and true ratings is > 0.8, and 1.0 < `hfa` < 3.0.
  - `test_nfl_no_leakage`: changing the results of games on or after `asof` does not change `predict`.
  - `test_cfb_shrinks_to_prior_early`: after one game per team, every rating is within 3 points of its prior. By week 12 the correlation with the truth is > 0.7.
  - `test_cfb_neutral_antisymmetric`: `predict(a, b, True) == pytest.approx(-predict(b, a, True))`.
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement with numpy. Use a closed-form weighted ridge, so no sklearn is needed here.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: NFL and CFB rating models`.

### Task 14: Blend, auto-gate, backtest; props projector

**Files:** Create `models/blend.py`, `models/props.py`, `tests/models/test_blend_props.py`

**Interfaces:**
- Produces:
  - `@dataclass BlendFit(a, b_model, b_market, b_model_se, n)` with a property `gated`. It is true when `n < 200`, or `b_model <= 0`, or `b_model / b_model_se < 1.96`.
  - `fit_blend(p_model, p_market, y) -> BlendFit`. It runs logistic regression on [1, logit p_model, logit p_market] by Newton–Raphson, and takes the standard errors from the inverse Hessian.
  - `blend(p_model, p_market, w_model) -> float` = σ(w·logit p_model + (1 − w)·logit p_market).
  - `default_weight(league, hours_to_kickoff) -> float` returns:
    - NFL: 0.25 when more than 24 hours out, otherwise 0.125.
    - CFB: 0.35 when more than 24 hours out, otherwise 0.125.
  - `walkforward_ats(league, games: list[dict], predict_asof: Callable[[date], Callable[[dict], float]]) -> tuple[BlendFit, dict]`
    - `games` holds dicts with keys `date, home, away, neutral, home_line, margin`.
    - Each week, it fits on prior weeks and predicts P(home covers) for two sources:
      - the model: `NormalModel(sigma).p_greater(mean, -home_line)`
      - the market: 0.5
    - Pushes are excluded.
    - It returns the fit plus metrics `{"n", "ats_pct", "brier", "logloss"}`.
  - `PropProjector(weekly: pl.DataFrame)` with `project(player, stat, season, asof_week) -> tuple[float, float] | None`.
    - Mean and sd are an exponentially weighted mean and sd (halflife 4) over the last 12 games before `asof_week`.
    - It returns None for fewer than 4 games.
    - Stat map:

| stat | nflverse column |
|---|---|
| `reception_yds` | `receiving_yards` |
| `rush_yds` | `rushing_yards` |
| `pass_yds` | `passing_yards` |
| `receptions` | `receptions` |

    - Player matching is case-insensitive on `player_display_name`.

- [ ] **Step 1: Write the failing tests**
```python
def test_blend_weights(): assert blend(0.6, 0.5, 0.0) == pytest.approx(0.5) and blend(0.6, 0.5, 1.0) == pytest.approx(0.6)

def test_gate_on_useless_model():
    rng = np.random.default_rng(0); pm = rng.uniform(.3, .7, 2000); y = rng.binomial(1, pm)
    assert fit_blend(rng.uniform(.3, .7, 2000), pm, y).gated

def test_no_gate_on_informative_model():
    rng = np.random.default_rng(1); truth = rng.uniform(.3, .7, 3000); y = rng.binomial(1, truth)
    market = np.clip(truth + rng.normal(0, .08, 3000), .05, .95)
    assert not fit_blend(truth, market, y).gated

def test_default_weight(): assert default_weight("nfl", 48) == 0.25 and default_weight("cfb", 2) == 0.125

def test_projector_needs_4_games(weekly_df): assert PropProjector(weekly_df).project("Rookie Guy", "rush_yds", 2026, 3) is None
def test_projector_mean(weekly_df): m, sd = PropProjector(weekly_df).project("Vet Back", "rush_yds", 2026, 9); assert 60 < m < 90 and sd > 0
```
  The `weekly_df` fixture is a polars frame. "Vet Back" has 8 games of rushing yards in [60..90], and "Rookie Guy" has 2 games.
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: logit blend with auto-gate, walk-forward backtest, prop projector`.

### Task 15: Recommendation pipeline

**Files:** Create `pipeline.py`, `tests/test_pipeline.py`

**Interfaces:**
- Consumes: Tasks 3–7, 13 and 14, plus `Settings`.

**Produces**
- `@dataclass Snapshot(now, games: dict[str, Game], quotes: list[Quote], props: list[PropQuote], kalshi: list[KalshiMarket], predictions: dict[str, Prediction], gates: dict[tuple[str, str], bool], bankroll_cents: int, open_exposure_cents: int, pp_staked_today_cents: int, margin_models: dict[str, MarginModel])`
  - `gates` is keyed by (league, `"ml" | "spread" | "total" | "prop"`). True means observe-only.
  - `margin_models` is keyed by `"nfl_spread"`, `"nfl_total"`, `"cfb"` and `"nfl_ml"`.

**`kalshi_game_candidates(snap, s) -> list[Recommendation]`**

Consider every market with a `game_id`, a game `status == "scheduled"`, `kickoff > now`, and `market_type` in {ml, spread, total}. For each one:

1. **Market probability for the YES side.**
   - **ml:** p_home from `fair_two_way(h2h)`, oriented to `team`.
   - **spread:** `m = fair_spread_mean` (expected home margin). If `team` is the home team, p = `model.p_greater(m, line)`; otherwise p = `model.p_greater(-m, line)`. The model is `margin_models["nfl_spread"]` or `["cfb"]`.
   - **total:** p = `model.p_greater(fair_total_mean, line)`.
   - If the fair input is missing, skip the market.
2. **Model probability.** Use the same formula with `predictions[game_id].mean_margin` or `.mean_total`. Combine as `p_yes = blend(p_model, p_mkt, default_weight(...))`. If there is no prediction, use `p_yes = p_mkt`.
3. **Price each side.**
   - YES: price `yes_ask`, p `p_yes`.
   - NO: price `no_ask`, p `1 - p_yes`.
   - Provisional qty = floor(cap_bet · bankroll / price), capped at depth.
   - Compute `p_eff` with that qty.
4. **Filter.** `passes_filters` must hold. For CFB, also require |model mean − market mean| ≥ `cfb_min_disagree`.
5. **Pick one.** Keep the best side by EV per market, then the best market per game.
6. **Build the Recommendation.**
   - Leg description uses the strings `"{team} ML"`, `"{team} wins by >{line}"` or `"Total over {line}"`.
   - `observe_only = gates.get((league, type), False)`.
   - `tie_risk` is true for NFL ml.

**`kalshi_prop_candidates(snap, s, projector) -> list[Recommendation]`**
- For each prop market, collect sportsbook `PropQuote`s for the same game, player and stat, excluding prizepicks. Use the book whose point is nearest the Kalshi line.
  - Devig with `devig_worst`, which gives p_over at that book's point.
  - Convert to the Kalshi line: `implied_mean_from_line`, then `prop_p_over(kalshi_line)`.
  - `anchored = True`.
- If there is no book quote, fall back to `projector.project`. That requires an extra `pp_margin_model - pp_margin_market` of EV; set `anchored = False`.
- Filters are the same as for game candidates. Keep at most one recommendation per player.

**`pp_entry_candidates(snap, s, projector) -> list[Recommendation]`**
- Each `PropQuote` with `book == "prizepicks"` becomes a `Pick`, over or under, with p taken from book devig (shifted to the PrizePicks line) or from the projector.
- Team comes from the player's team in `projector`, or from the PropQuote game: home if unknown, flagged.
- Call `build_best_entry`.
- Stake = `kelly_entry` f* × `kelly_fraction` × bankroll, capped at `cap_bet`·bankroll and at `cap_pp_daily`·bankroll − `pp_staked_today`.
- Return zero or one Recommendation, with `structure = f"{n}-{type}"`.

**`arb_candidates(snap, s, league_of) -> list[Recommendation]`**
- Built from `filter_arbs(kalshi_ml_pairs + kalshi_ladders, s.arb_max_roi)`.
- qty = min(arb.qty, floor(arb_fraction · bankroll / (cost/qty))).
- Skip when qty < 1.

**`size_slate(recs, snap, s) -> list[Recommendation]`**
- Takes the Kalshi game and prop recs that are not observe-only.
- `Bet(p_win = leg.p_fair, p_push = 0, d = 1 / p_eff)`.
- Runs `optimize_slate` with `bankroll - open_exposure`, then `to_cents`, then sets `qty = floor(stake / p_eff)` and `stake_cents = qty · price` in cents.
- Drops recs whose qty is 0.

**`scan(snap, s, projector, league_of) -> list[Recommendation]`**
- Returns arbs, then the sized Kalshi recs, then observe-only recs (stake 0), then the PrizePicks entry.
- `expires_at` is the earliest kickoff among the rec's legs. `snapshot_ts` is `now`.

- [ ] **Step 1: Write the failing tests.** Build each Snapshot with a helper `snap_with(**overrides)`:
  - `test_kalshi_ml_edge_found`: Pinnacle h2h home 1.80 / away 2.10. A Kalshi home YES ask of 0.48 gives one rec: kind `kalshi_game`, `legs[0].side == "yes"`, `ev > 0.025`, `stake_cents > 0`.
  - `test_no_rec_without_edge`: a Kalshi ask of 0.55 gives `[]`.
  - `test_takes_no_side_when_yes_overpriced`: a home YES ask of 0.70 with no_ask 0.31 gives a rec with side `"no"`.
  - `test_started_game_skipped`: kickoff earlier than now gives `[]`.
  - `test_observe_only_not_sized`: `gates[("nfl", "ml")] = True` gives `observe_only` with `stake_cents == 0`.
  - `test_spread_uses_distribution`: a fair spread with expected home margin 3.0. A Kalshi home "wins by over 2.5" YES at 0.40 gives a rec; at 0.60 it gives none.
  - `test_unmatched_market_ignored`: a market with `game_id=None` gives `[]`.
  - `test_pp_entry_built`: 8 prizepicks lines across 8 games and 2 teams, each with DK Over 1.70 / Under 2.25, gives one `pp_entry` rec with 3 to 6 legs and a structure in ALLOWED.
  - `test_pp_daily_cap_exhausted`: `pp_staked_today_cents = cap` gives no pp rec.
  - `test_arb_rec`: a Kalshi ML pair at 0.45 and 0.45 gives kind `arb`, two legs, and `tie_risk=True`.
  - `test_slate_caps_hold`: 15 strong ML candidates. Σ stake ≤ 20% of bankroll and every stake ≤ 2%.
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: recommendation pipeline for Kalshi, PrizePicks, arbitrage`.

### Task 16: Notifications

**Files:** Create `notify/__init__.py`, `notify/format.py`, `notify/imessage.py`, `notify/ntfy.py`, `notify/console.py`, `notify/chain.py`, `tests/test_notify.py`

**Interfaces:**
- Produces `format_message(rec, tz=ZoneInfo("America/Los_Angeles")) -> str`. It is ASCII only: run NFKD normalization, then drop any non-ASCII characters. The format depends on the kind:

| Kind | Format |
|---|---|
| `kalshi_*` | `GE KALSHI BUY {YES\|NO} {desc} ({ticker}) @ ${price:.2f} x{qty} = ${cost:.2f} w/fee \| fair {p:.1%} EV +{ev:.1%} \| {HH:MM} PT \| id {id}` |
| `pp_entry` | `GE PP {n}-{POWER\|FLEX} ${stake:.0f} \| EV +{ev:.1%} \| 1) {player} {Stat} {MORE\|LESS} {line} ({p:.0%}) 2) ... \| lines {HH:MM} PT \| confirm payout in app \| id {id}` |
| `arb` | `GE ARB KALSHI +{roi:.1%} \| {leg1} + {leg2} x{qty} \|{" tie risk \|" if tie_risk} verify now \| {HH:MM} PT \| id {id}` |

  Observe-only recs are never formatted for sending.
- Produces `@dataclass NotifyResult(ok: bool, channel: str, error: str | None = None)`.
- Produces `IMessageNotifier(phone, runner=subprocess.run)` with `.send(text) -> NotifyResult`.
  - It runs `["osascript", "-e", SCRIPT, phone, text, "iMessage"]` with `timeout=30`, `capture_output=True` and `check=True`.
  - `SCRIPT` is the `on run argv` AppleScript from `research/round2.md`, in the sms section, copied verbatim.
  - Any exception produces `NotifyResult(False, "imessage", str(e))`.
- Produces `NtfyNotifier(topic, post=httpx.post)` with `.send(text, priority=4)`. It POSTs to `https://ntfy.sh/{topic}` with headers `Title: Gridiron Edge` and `Priority`.
- Produces `ConsoleNotifier(sink: list[str])`.
- Produces `NotifyChain(conn, channels, quiet_hours, enabled=True, tz=...)`.
  - It uses the table `notifications(id, ts, rec_id, dedupe_key, channel, ok, error, text)`.
  - `notify(rec, now, force=False) -> NotifyResult | None` returns None, without sending, when:
    - notifications are not enabled,
    - `rec.observe_only` is set, or
    - it is not `force`, and either the `dedupe_key` already has an ok row, or it is quiet hours and the kind isn't `arb`. Quiet hours are local hours h where start ≤ h or h < end, with the window wrapping past midnight.
  - Otherwise it tries channels in order until one succeeds. `arb` sends to every channel.
  - Every attempt is logged.
  - `send_text(text, now) -> NotifyResult` is used by `notify test`. It forces past dedupe and quiet hours.

- [ ] **Step 1: Write the failing tests**
```python
def test_format_kalshi_ascii_and_fields(rec_kalshi):
    t = format_message(rec_kalshi); assert t.isascii() and t.startswith("GE KALSHI BUY YES") and t.endswith(" PT | id 42")

def test_format_pp_has_confirm(rec_pp): assert "confirm payout in app" in format_message(rec_pp)
def test_format_arb_has_verify_and_tie(rec_arb): t = format_message(rec_arb); assert "verify" in t and "tie risk" in t

def test_imessage_passes_argv_not_interpolated():
    calls = []
    n = IMessageNotifier("+15555550100", runner=lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0))
    evil = 'hi "; do shell script "rm -rf ~"'
    assert n.send(evil).ok and calls[0][0] == "osascript" and calls[0][-3:] == ["+15555550100", evil, "iMessage"]

def test_chain_dedupe_and_fallback(conn, rec_kalshi, noon):
    def boom(*a, **k): raise subprocess.TimeoutExpired("osascript", 30)
    sink = []; ch = NotifyChain(conn, [IMessageNotifier("+15555550100", runner=boom), ConsoleNotifier(sink)], (23, 8))
    assert ch.notify(rec_kalshi, noon).channel == "console" and ch.notify(rec_kalshi, noon) is None and len(sink) == 1

def test_quiet_hours_suppress_non_arb(conn, rec_kalshi, rec_arb, midnight):
    sink = []; ch = NotifyChain(conn, [ConsoleNotifier(sink)], (23, 8))
    assert ch.notify(rec_kalshi, midnight) is None and ch.notify(rec_arb, midnight).ok

def test_observe_only_never_sent(conn, rec_kalshi, noon):
    rec_kalshi.observe_only = True; assert NotifyChain(conn, [ConsoleNotifier([])], (23, 8)).notify(rec_kalshi, noon) is None
```
  The fixtures `noon` and `midnight` are tz-aware datetimes at 12:00 and 00:30 PT.
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: iMessage/ntfy/console notifications with dedupe and quiet hours`.

### Task 17: Bet lifecycle

**Files:** Create `bets/__init__.py`, `bets/lifecycle.py`, `tests/bets/test_lifecycle.py`

**Interfaces:**
- Consumes `Ledger`, `Recommendation`, `taker_fee`.
- Produces two tables:
  - `recommendations(id, kind, dedupe_key UNIQUE, leg_key, legs_json, stake_cents, qty, ev, p_win, structure, snapshot_ts, expires_at, state, observe_only, tie_risk, created_at)`
  - `bets(id, rec_id, kind, legs_json, price, qty, stake_cents, fee_cents, structure, placed_at, state, result, payout_cents, settled_at, clv)`
- Produces `class LifecycleError(Exception)`.
- Produces `Lifecycle(conn, ledger)` with these methods:
  - `upsert_recommendation(rec, now) -> tuple[Recommendation, bool]`
    - A matching `dedupe_key` returns the existing rec with `False`.
    - Same `leg_key` but a different `dedupe_key`: set any RECOMMENDED or PENDING rec with that `leg_key` to EXPIRED, then insert the new one.
    - Observe-only recs are inserted as `LOGGED`.
  - `mark_pending(rec_id)`
  - `confirm(rec_id, now, price=None, qty=None, stake_cents=None) -> int` (returns the bet id)
    - Errors: an unknown id raises `LifecycleError("unknown id")`. A state other than RECOMMENDED or PENDING raises `LifecycleError("not pending")`. `now >= expires_at` raises `LifecycleError("expired")`.
    - **Kalshi cost:** `cost_cents = round(qty * price * 100)` plus `fee_cents = round(taker_fee(qty, price, 1.0) * 100)`.
    - **Arb:** qty is the same on both legs. The cost is the sum over legs at the leg prices, plus fees.
    - **pp:** cost is `stake_cents`.
    - **Transaction:** one `with conn:` block that writes the ledger `stake` row (`-(cost + fee)`), inserts the `bets` row with state `PLACED`, and sets the rec state to `PLACED`. `InsufficientFunds` propagates and leaves no rows.
  - `skip(rec_id)` and `expire_due(now) -> list[int]`
  - `pending() -> list[Recommendation]` (states RECOMMENDED or PENDING), `board(limit=100) -> list[Recommendation]` (all states, newest first)
  - `open_bets() -> list[sqlite3.Row]`, `settled_bets(limit=200) -> list[sqlite3.Row]`
  - `open_exposure_cents() -> int` and `pp_staked_today_cents(now, tz) -> int`
  - `get_rec(rec_id) -> Recommendation | None`

- [ ] **Step 1: Write the failing tests**
  - `test_dedupe_same_key`: upserting the same rec twice returns is_new = (True, False) and leaves one row.
  - `test_price_move_expires_old`: re-upserting the same ticker at a new price leaves the old rec EXPIRED and the new one RECOMMENDED.
  - `test_confirm_debits_with_fee`: bankroll 10000 cents, Kalshi rec at 0.46 for 40 contracts. The balance afterwards is 10000 − 1840 − round(taker_fee(40, .46)·100).
  - `test_confirm_override_price_qty`: `confirm(id, now, price=0.47, qty=30)` stores price 0.47 and qty 30.
  - `test_confirm_expired_raises`: raises "expired" and leaves the balance unchanged.
  - `test_confirm_unknown_id`: raises "unknown id".
  - `test_confirm_insufficient_funds_atomic`: no bets row is written and the rec stays PENDING.
  - `test_observe_only_logged_not_confirmable`: the rec's state is LOGGED, and confirming it raises "not pending".
  - `test_expire_due`: past-kickoff RECOMMENDED and PENDING recs become EXPIRED.
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: recommendation and bet lifecycle with atomic confirm`.

### Task 18: Settlement and CLV

**Files:** Create `bets/settle.py`, `bets/clv.py`, `tests/bets/test_settle.py`

**Interfaces:**
- Consumes `Lifecycle`, `GameRegistry`, `revert`, `PAYOUTS`.
- Produces `grade_kalshi_leg(leg, market: KalshiMarket | None, game: Game | None, now) -> Literal["won", "lost", "refund", "pending"]`. It grades the leg's side (yes/no):
  - **Kalshi result.** If `market.result` is "yes" or "no", the leg wins when the result matches the leg's side.
  - **Final score**, used only when `game.status == "final"`, with these YES conditions:
    - ml: the team scored more. A tie means every YES is lost.
    - spread: the team's margin > line.
    - total: the total > line.
    - NO is the inverse of YES.
  - **Refund.** `void_candidate` status, or no final result 72 h after kickoff, gives `refund`.
  - **Otherwise** `pending`.
- Produces `grade_pp_entry(legs, structure: Structure, stats: dict[tuple[str, str], float | None]) -> tuple[Literal["won", "lost", "refund", "pending"], float]`. It returns a result and a multiplier:
  - A stat of None is a DNP. A stat equal to the line is a tie.
  - A missing key means the result is pending.
  - Apply `revert` with the remaining distinct teams.
  - The multiplier is `PAYOUTS[new].get(hits, 0)`.
  - The result is "won" when the multiplier > 0, otherwise "lost". A revert to None gives "refund" with multiplier 1.0.
- Produces `clv(price_eff, p_close_fair) -> float | None`, equal to p_close_fair/price_eff − 1, or None if either input is missing.
- Produces `Settler(conn, lifecycle, registry)` with these methods:
  - `settle_ready(now, kalshi_lookup: Callable[[str], KalshiMarket | None], stat_lookup: Callable[[str, str, str], float | None | Literal["pending"]]) -> list[tuple[int, str, int]]`
    - Returns (bet_id, result, payout_cents) for each bet it settles.
    - Payouts:
      - Kalshi win: qty·100 cents.
      - Arb: the sum of winning legs at qty·100 each.
      - PP: round(stake·mult).
      - Refund: stake plus fee.
      - Loss: 0.
    - One transaction writes the ledger `payout` or `refund` row (when > 0) and updates the bet to state `SETTLED`, result, payout_cents and settled_at.
  - `manual_settle(bet_id, result, payout_cents=None)`.
  - `record_clv(bet_id, p_close_fair)`.

- [ ] **Step 1: Write the failing tests**
  - `test_kalshi_result_yes_pays_qty`: 40 contracts pay out 4000 cents and the bankroll goes up 4000.
  - `test_kalshi_no_side_wins_on_no`.
  - `test_spread_grading_from_score`: line 7.5, margin 8 is won; margin 7 is lost.
  - `test_nfl_tie_ml_yes_loses`.
  - `test_postponed_refund_after_72h`: still pending at 71 h. At 73 h it is a refund, and the stake plus fee is returned.
  - `test_pp_flex_one_miss`: 5-flex with 4 hits is won at 2.0.
  - `test_pp_dnp_reverts`: 3-flex with 1 DNP and 2 hits pays 3.0.
  - `test_pp_single_team_after_dnp_refund`.
  - `test_pp_pending_until_all_stats`.
  - `test_clv`: `clv(0.50, 0.55) == pytest.approx(0.10)` and `clv(0.5, None) is None`.
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: settlement for Kalshi, PrizePicks, voids; CLV`.

### Task 19: Scheduler

**Files:** Create `scheduler.py`, `tests/test_scheduler.py`

**Interfaces:**
- Produces `@dataclass Job(name: Literal["kalshi_refresh", "rundown_scores", "rundown_lines", "odds_games", "odds_props", "cfbd_weekly", "nflverse_refresh", "settle", "scan", "expire"], league: str | None = None, args: dict = field(default_factory=dict), essential: bool = False)`
  - `Job.key` is `(name, league, tuple(sorted(args.items())))`.
- Produces `Scheduler(registry, budgets: dict[str, BudgetGuard], state: dict | None = None, tz = ZoneInfo("America/Los_Angeles"))`.
  - `due(now) -> list[Job]` is pure, given `state` and the registry contents.
  - `mark_done(job, now)` records the job, and `request_scan(now)` asks for a scan.
- Rules for `due`:

| Job | Rule |
|---|---|
| `expire` | Every 60 s. |
| `rundown_scores` (per league) | Every 60 s if any game is live. Every 15 min if a game kicks off within 3 h. Otherwise hourly. |
| `rundown_lines` (per league) | Every 20 min when the league has a game within 48 h. |
| `kalshi_refresh` | Every 3 min when any game is within 24 h. Otherwise every 30 min. |
| `odds_games` (per league) | One job per window, keyed `args={"window": ...}`. See the windows below. |
| `odds_props` (per game) | Due once per game when its kickoff is within 2 h. See the limits below. |
| `scan` | Pending after any `kalshi_refresh`, `odds_*` or `rundown_lines` completes, debounced so it runs at most once per 60 s. |
| `settle` | Every 5 min while there are open bets (the registry callable `has_open_bets()` is injected). |
| `cfbd_weekly`, `nflverse_refresh` | Once per week, the first time `due` runs after Tuesday 06:00 PT. |

  - `odds_games` windows:
    - `early` is the first time `due` runs after Tue 09:00 PT (NFL) or Sun 12:00 PT (CFB) in the current week.
    - `mid` is Thu 09:00 PT (NFL) or Wed 09:00 PT (CFB).
    - `close:<kickoff iso>` is due from T−20 min to T−5 min for each distinct kickoff, uses `commence_from` and `commence_to` equal to that kickoff ±1 min, and is `essential=True`.
  - `odds_props` limits:
    - At most 8 per ISO week.
    - Ranking is NFL before CFB, then earlier kickoff.
    - It is not repeated for a game.
    - It is skipped when `budgets["odds_api"].can_spend(3)` is False.

- [ ] **Step 1: Write the failing tests.** Use a fake registry with `upcoming()`, `live()` and `has_open_bets()`.
  - `test_live_scores_every_minute`: with a game live, `due` at t and t+61 s returns `rundown_scores` both times, but t+30 s does not.
  - `test_idle_scores_hourly`
  - `test_odds_close_window_once_per_kickoff`: two games with the same kickoff produce one `odds_games` close job, which is essential.
  - `test_early_window_once`
  - `test_props_respect_weekly_cap`: the 9th game gets no job.
  - `test_props_not_repeated`
  - `test_props_skip_when_budget_low`
  - `test_scan_debounced`
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: quota-aware fetch scheduler`.

### Task 20: Command parser

**Files:** Create `ui/__init__.py`, `ui/commands.py`, `tests/ui/test_commands.py`

**Interfaces:**
- Produces:
  - `@dataclass Command(name, args: dict)`.
  - `class CommandError(Exception)`.
  - `parse_command(text) -> Command`, which uses `shlex.split` and `match`.
  - `COMMAND_WORDS: list[str]`, the full command strings, used for autocomplete.
- Grammar and the `args` each command produces:

| Command | args |
|---|---|
| `deposit <amt>` | `{"cents"}`, parsed with `parse_amount` |
| `withdraw <amt>` | `{"cents"}`, parsed with `parse_amount` |
| `confirm <id> [price] [qty]` | `{"id", "price", "qty"}`. price is a float in (0, 1) or None; qty is an int ≥ 1 or None |
| `skip <id>` | `{"id"}` |
| `scan [games\|props\|pp\|arb\|all]` | `{"what"}`, default "all" |
| `refresh [nfl\|cfb] [--force]` | `{"league", "force"}` |
| `bets [open\|settled]` | `{"which"}`, default "open" |
| `settle <id> <won\|lost\|refund> [payout]` | `{"id", "result", "cents"}` |
| `pp add "<player>" <stat> <line> <team> <game_id>` | |
| `quota` | `{}` |
| `notify test\|on\|off` | |
| `set <field> <value>` | field must be a `Settings` field |
| `venues` | |
| `backtest <nfl\|cfb> <yyyy>-<yyyy>` | `{"league", "start", "end"}` |
| `fit blend` | |
| `report` | |
| `help` | |

- `CommandError` messages are a single line, for example `usage: withdraw <amount>` or `unknown command: frobnicate (try help)`.

- [ ] **Step 1: Write the failing tests**, parametrized.
  - These must parse:
    - `("deposit 100", Command("deposit", {"cents": 10000}))`
    - `("withdraw $1,000.50", Command("withdraw", {"cents": 100050}))`
    - `("confirm 42 0.47 30", Command("confirm", {"id": 42, "price": 0.47, "qty": 30}))`
    - `("confirm 42", Command("confirm", {"id": 42, "price": None, "qty": None}))`
    - `("set kelly_fraction 0.5", Command("set", {"field": "kelly_fraction", "value": "0.5"}))`
    - `("backtest nfl 2018-2025", Command("backtest", {"league": "nfl", "start": 2018, "end": 2025}))`
  - These must raise `CommandError`:
    - `"withdraw -5"`
    - `"deposit abc"`
    - `"confirm x"`
    - `"confirm 42 1.5"`
    - `"frobnicate"`
    - `"set nosuch 1"`
    - `""`
    - `'pp add "unterminated'`
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: command parser`.

### Task 21: Widgets and theme

**Files:** Create `ui/theme.tcss`, `ui/widgets.py`, `ui/screens.py`, `tests/ui/test_widgets.py`

**Interfaces:**
- Produces, in `widgets.py`:
  - **`BalanceChart(PlotextPlot)`**: `update_series(history: list[tuple[datetime, int]], markers: list[tuple[datetime, int, str]])`.
    - Braille line of dollars over time, colored with `profit` when the latest value ≥ the first and `loss` otherwise.
    - Deposit markers are `+` and withdraw markers are `-`.
    - Empty history shows "no history yet".
  - **`KpiTiles(Static)`**: `update_kpis(k: dict)` with keys `roi, roi_band, clv_mean, clv_pos, record, max_dd, brier, n`.
    - Shows `inconclusive (n<1500)` when n < 1500.
    - `compute_kpis(settled_rows, history) -> dict` is a module-level function.
  - **`QuotaBar(Static)`**: `update_quota(odds: int | None, cfbd: int | None, rundown: int | None, kalshi_ok: bool)`.
    - Mini block bars against caps of 500, 1000 and 200000.
    - The CSS class `low` applies below 10% and pulses via a `styles.animate("opacity")` loop.
  - **`OpportunityBoard(TabbedContent)`**: tabs `Kalshi Games`, `Kalshi Props`, `PrizePicks`, `Arbitrage`.
    - Each tab is a `DataTable(cursor_type="row")` with columns `id, what, price, fair, EV, stake, state`.
    - `set_recs(recs)` adds any row id not seen before to `self.flash_ids` for 1.5 s and styles it with the `flash` component class.
    - `class ConfirmRequested(Message)` carries `rec_id`. It is posted on `DataTable.RowSelected`.
  - **`ScoresPanel(DataTable)`**: `set_games(games, bet_status: dict[str, Literal["winning", "losing", "none"]], delay_s: int | None)`.
    - A pulsing `LIVE` cell marks live games.
    - `border_title` is `"Scores ~5m delay"` when delay_s ≥ 60, otherwise `"Scores"`.
  - **`EventLog(RichLog)`**: `event(kind, msg)`.
    - Kinds: `rec | text | confirm | deposit | withdraw | settle | api | error | info`.
    - Each line is `HH:MM:SS` followed by a colored kind tag and the message.
    - The widget keeps `self.lines_text: list[str]` for tests.
  - **`BankrollHeader(Static)`**: `set_balance(cents, today_cents)`.
    - A reactive float `shown` is animated toward the target over 0.6 s with `self.animate`.
    - Renders `GRIDIRON EDGE  $X,XXX.XX  ±$Y today` plus a clock.
    - Also has `.target_cents`.
- Produces, in `screens.py`:
  - **`ConfirmScreen(ModalScreen[tuple[float | None, int | None] | None])`**: `__init__(rec)`.
    - Shows the legs, an `Input#price` prefilled with the leg price, and an `Input#qty` prefilled with qty.
    - Buttons are `#ok` and `#cancel`.
    - It dismisses with the parsed values, or None on cancel. Invalid input shows an inline error and the modal stays open.
- Produces, in `theme.tcss`:
  - Variables: `$bg #0b0f1a`, `$surface #121829`, `$accent #22d3ee`, `$profit #22c55e`, `$loss #ef4444`, `$pending #f59e0b`.
  - Screen grid: 3 columns `2fr 3fr 2fr`.
  - Rows: header `auto`, body `1fr`, command `auto`.
  - Panels use rounded borders in `$accent 40%`. `.flash` uses background `$accent 30%`.

- [ ] **Step 1: Write the failing tests.** Each uses a small harness App and `run_test()`, with `await pilot.pause(0.3)` after a screen push.
  - `test_board_flash_and_confirm_message`: after `set_recs([r1])` and then `set_recs([r1, r2])`, `r2.id` is in `flash_ids`. Pressing Enter on the first row posts `ConfirmRequested(r1.id)`.
  - `test_kpi_inconclusive`
  - `test_confirm_modal_returns_values`: type 0.47 and 30, then click `#ok`. The result is `(0.47, 30)`.
  - `test_confirm_modal_rejects_bad_price`: 1.7 keeps the modal open.
  - `test_scores_delay_title`
  - `test_chart_empty_history_ok`
  - `test_event_log_records_kind`
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: TUI widgets, confirm modal, neon theme`.

### Task 22: Services, app assembly, first-run checks

**Files:** Create `services.py`, `app.py`, `__main__.py`, `README.md`, `tests/ui/test_app.py`, and a `tests/conftest.py` fixture `services_mocked`

**Interfaces:**
- Consumes: everything above.
- Produces `Services`, built by `build_services(settings, transports: dict[str, httpx.AsyncBaseTransport] | None = None) -> Services`. Transports let tests inject `respx` mock transports for each API.
  - Fields:
    - core: `settings, conn, ledger, lifecycle, settler, teams, registry, budgets, scheduler, notifier`
    - clients: `odds, kalshi, rundown, cfbd, nflverse`
    - models: `projector, nfl_model, cfb_model, gates, margin_models`
    - state: `latest: dict` holding the latest `quotes`, `props`, `kalshi` and `stats`
  - Clients whose key is missing are `None`. Those jobs are skipped.
- `async startup(now) -> list[tuple[str, str]]` does the following in order:
  1. Seeds the teams. CFB comes from CFBD `teams_fbs`, cached.
  2. Registers games from the Odds API `/events` (free) for both leagues, plus TheRundown scores for today and the next 7 days.
  3. Loads nflverse schedules, weekly stats and EPA for the last 3 seasons in a thread.
  4. Fits the NFL and CFB models and `margin_models["nfl_spread"]`, built from nflverse `result + spread_line` residuals.
  5. Fits the gates with `walkforward_ats` on cached history, once per week, stored in settings.
  6. Runs `settle` and `expire` to catch up.
  7. Runs the first-run checks once, recorded in the `settings` table as `first_run_done`.
  It returns event-log lines.
- `async run_job(job, now) -> list[tuple[str, str]]` dispatches by `job.name` to the client calls, then updates `registry` and `latest`. A `scan` job:
  - builds a `Snapshot`,
  - calls `pipeline.scan`,
  - calls `lifecycle.upsert_recommendation` for each recommendation,
  - for each new recommendation that is not observe-only, calls `notifier.notify` and then `mark_pending` on success,
  - emits a `("rec", ...)` line and a `("text", ...)` line.
  Errors: `AuthError` disables the client and logs one `("error", "<api>: invalid key - disabled")` line. Other exceptions become `("error", f"{job.name}: {e}")` and are never raised.
- First-run check lines, all `info`:
  - `odds_api books: <seen_books>`. If `prizepicks` is not among them: `PrizePicks lines unavailable on this key - use "pp add"`.
  - `therundown tier=<X-Tier> delay=<s>s books=<X-Bookmakers>`
  - `cfbd remaining=<n>`
  - `kalshi reachable=<bool>`
  - `notify: run "notify test" to confirm your phone alerts`
- `kpis() -> dict` returns `compute_kpis(settled, history)`.

**`GridironApp(App)`**
- Settings: `CSS_PATH = "ui/theme.tcss"`, `AUTO_FOCUS = "#cmd"`, `TITLE = "Gridiron Edge"`.
- `__init__(services, clock = lambda: datetime.now(UTC))`.
- Compose:
  - row 1: `BankrollHeader` and `QuotaBar`
  - left column: `BalanceChart` above `KpiTiles`
  - centre: `OpportunityBoard` above `ScoresPanel`
  - right: `EventLog`
  - bottom: `Input(id="cmd", suggester=SuggestFromList(COMMAND_WORDS))` and `Footer`
- `on_mount` starts `startup` in a worker. On completion it calls `refresh_views()` and `set_interval(15, self.tick)`.
- `tick` runs each `scheduler.due(clock())` job in `run_worker(..., group=job.name, exclusive=True)`, then calls `scheduler.mark_done` and `refresh_views()`.
- `refresh_views()` updates every widget from the db and the services.
- On `Input.Submitted`:
  - Parse the command, dispatch it, and clear the input.
  - Each handler writes an EventLog line.
  - `CommandError`, `LifecycleError`, `InsufficientFunds` and `InvalidAmount` become `("error", str(e))`. `InsufficientFunds` messages contain "insufficient".
- On `OpportunityBoard.ConfirmRequested`, push `ConfirmScreen(rec)`. Its callback calls `lifecycle.confirm` and logs `confirm`.
- `notify test` calls `notifier.send_text("GE test - if your phone did not alert, enable ntfy (NTFY_TOPIC in .env)", now)`.
- `backtest` and `fit blend` run `walkforward_ats` in a thread worker and log the metrics. `report` logs the KPIs.

**`__main__.main()`**
- Loads settings and prints any missing keys, then runs the app anyway.
- `--db PATH` overrides the database path.

**`README.md`**
- Setup: `uv sync`, `cp .env.example .env` and fill it in, `uv run gridiron-edge`.
- The command table.
- Messages Automation permission: grant it to the terminal app.
- A simulation and legal disclaimer.
- Known limits: the free-tier coverage, the ~5 min score delay, and that maker mode is not built.

- [ ] **Step 1: Write the failing tests**, using `services_mocked`. Every HTTP route is mocked with respx from the fixtures of Tasks 9–11, the database is temporary, and notifications go to `ConsoleNotifier(sink)`.
  - `test_deposit_updates_header_and_log`: type `deposit 250` and press Enter. Then `header.target_cents == 25000` and the last line of `log.lines_text` contains "deposit".
  - `test_withdraw_too_much_shows_error`: the log line contains "insufficient" and the balance is unchanged.
  - `test_bad_command_no_crash`: `frobnicate` logs an error line, and `app.is_running` stays true.
  - `test_scan_job_creates_rec_and_texts_once`: with the Kalshi fixture priced to give an edge, calling `run_job(Job("scan"))` twice produces one board row and `len(sink) == 1`.
  - `test_confirm_flow_from_board`: select the row, type into the modal and confirm. The bet is `PLACED` and the balance drops.
  - `test_missing_keys_app_still_starts`: the app mounts and logs which sources are disabled.
  - `test_auth_error_logged_once`: an Odds API 401 on two jobs produces exactly one matching error line.
- [ ] **Step 2:** Run `uv run pytest tests/ui/test_app.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the whole suite with `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5:** Smoke test with the user's real `.env`: `uv run gridiron-edge`. Expected:
  - the layout renders,
  - the quota bars populate,
  - the first-run lines are logged,
  - `notify test` sends a text.
  This step needs the user's keys. If the `.env` is absent, report the step as skipped.
- [ ] **Step 6:** Commit `feat: assemble Gridiron Edge app with scheduler workers and first-run checks`.

---

## Spec coverage

| Spec section | Tasks |
|---|---|
| §1–2 goals | 15–22 |
| §3 constraints | 1, 8–11, 19 |
| §4 architecture | File Structure |
| §5 data | 9–12, 19 |
| §6.1 fair | 4, 13, 14 |
| §6.2 Kalshi | 5, 15 |
| §6.3 PrizePicks | 6, 15, 18 |
| §6.4 arbitrage | 7, 15 |
| §6.5 sizing | 5, 6, 15 |
| §7 lifecycle | 17, 18 |
| §8 notifications | 16 |
| §9 TUI | 20–22 |
| §10 persistence | 2, plus per-task tables |
| §11 stack | 1 |

Kalshi maker mode (§6.2, "optional, off by default") is deliberately left out of v1, and the README says so.
