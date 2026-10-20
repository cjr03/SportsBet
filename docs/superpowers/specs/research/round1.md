# odds-api

## The Odds API v4: fact-checked 2026-10-20

The draft is **largely correct**. The corrections and additions are listed first, followed by the full corrected section.

### Corrections and additions
1. **Historical access: conflicting pages.**
   - The historical-odds page says the data is "only available on paid usage plans" (https://the-odds-api.com/historical-odds-data/).
   - The homepage pricing table, as rendered on 2026-10-20, shows "Historical odds: Included" in every column, including Starter.
   - Treat historical as **paid-only** and confirm with a probe call. The 10× cost makes it impractical on 500 credits in any case.
2. **Historical additional markets cost per event.** They cost 10 × markets × regions **per event**, not per sport call. Their snapshot cadence is 5 minutes.
3. **Outrights refresh in-play too.** They update every 5 minutes pre-match and **every 60 s in-play**. The draft gave only the pre-match rate.
4. **More regions exist.** The full region list is us, us2, us_dfs, us_ex, uk, eu, au, **ca, fr, se, fi**.
5. **More paid-only US books.**
   - The `us` region has williamhill_us (Caesars) and fanatics as paid-only.
   - The `us2` region also has courtside and rebet as paid-only.
   - Free-tier `us` books: betonlineag, betmgm, betrivers, betus, bovada, draftkings, fanduel, lowvig, mybookieag.
   - Free-tier `us2` books: ballybet, betanysports, betparx, espnbet, fliff, hardrockbet (plus the az, fl and oh variants).
6. **Pinnacle caveat.** Pinnacle (`pinnacle`, region `eu`) is flagged as "Odds are from public website which may incur a delay." It is not guaranteed to have every FBS game.
   - The `us_ex` region adds exchange and prediction-market books: betopenly, kalshi, limitless, novig, polymarket, polymarket_us and prophetx.
   - `novig` and `prophetx` are low-hold books that could serve as a second sharp reference. Exchange odds also refresh faster: 20 s pre-match and 10 s in-play.
7. **Rate limit.** The documented limit is only "30 requests per second on paid usage plans." No free-tier figure is published.
8. **Market scope of `/odds`.** The sport-level `/odds` endpoint takes only featured markets (h2h, spreads, totals, outrights) and adds the lay variants automatically. Props, alternates and period markets need the per-event `/events/{eventId}/odds` endpoint, which accepts keys such as `player_pass_tds`.
9. **Empty responses are free.** This applies to `/odds`, event odds, historical odds, historical event odds, and historical events that find nothing. It does **not** apply to scores.
10. **Sport keys.** These are confirmed:
    - **Scores coverage:** americanfootball_nfl, americanfootball_ncaaf, americanfootball_nfl_preseason, americanfootball_cfl, americanfootball_ufl.
    - **No scores coverage:** americanfootball_ncaaf_fcs, americanfootball_nfl_super_bowl_winner, americanfootball_ncaaf_championship_winner.
    - Sports appear in the list only "when in season."
11. **Budget arithmetic.**
    - The free preset adds up to 36 + 10 + 55 = about 101 credits per week, or about 435 per month. That fits under 500, but with only about 13% slack.
    - The 20K preset at about 4,000 per week comes to about 17,200 per month. That is **about 86% of 20K**, and a heavy week (for example an MNF doubleheader, or a Saturday NFL slate in December, when NFL and CFB overlap) can overrun it. Cap it with the adaptive allocator, or use the 100K plan ($59) for 60 s live polling across both sports.
12. **Python 3.14.** There is no official Python SDK. Write the client yourself with `httpx` 0.28.1, which is the latest and pure-Python, so it works on 3.14. Note that its last release was Dec 2024. Retries can use `tenacity` 9.2.1, which declares Python 3.14 support. `aiolimiter` 1.3.0 can do client-side throttling.

---

### Corrected section

**Host and auth:** `https://api.the-odds-api.com` (IPv6: `https://ipv6-api.the-odds-api.com`), with the `apiKey` query param.

**Sport keys:** `americanfootball_nfl` and `americanfootball_ncaaf` both have scores coverage. `americanfootball_ncaaf_fcs` has no scores coverage.

| Endpoint | Key params | Cost |
|---|---|---|
| `GET /v4/sports/` | `all=true` | Free |
| `GET /v4/sports/{sport}/events` | `commenceTimeFrom/To`, `eventIds`, `dateFormat`, `includeRotationNumbers` | Free |
| `GET /v4/sports/{sport}/odds/` | `regions` or `bookmakers`, `markets` (h2h, spreads, totals, outrights), `oddsFormat` (decimal or american), `dateFormat`, `eventIds`, `commenceTimeFrom/To`, `includeLinks`, `includeSids`, `includeBetLimits`, `includeRotationNumbers` | markets × regions. Empty response costs 0. |
| `GET /v4/sports/{sport}/scores/` | `daysFrom` (1 to 3), `dateFormat`, `eventIds` | 1 without `daysFrom`, 2 with it |
| `GET /v4/sports/{sport}/events/{id}/odds` | as `/odds`, plus `includeMultipliers` and any market key | unique markets returned × regions. Empty costs 0. |
| `GET /v4/sports/{sport}/events/{id}/markets` | `regions`, `bookmakers` | 1 |
| `GET /v4/sports/{sport}/participants` | none | 1 |
| `GET /v4/historical/sports/{sport}/odds` | plus `date` | 10 × markets × regions. Paid. |
| `GET /v4/historical/sports/{sport}/events` | `date` | 1, or 0 if none found. Paid. |
| `GET /v4/historical/sports/{sport}/events/{id}/odds` | plus `date` | 10 × markets returned × regions, per event. Paid. |

**Historical data**
- Responses are wrapped as `{timestamp, previous_timestamp, next_timestamp, data}`.
- Featured markets go back to 2020-06-06 at 10-minute snapshots, and to September 2022 at 5-minute snapshots.
- Props, alternates and period markets go back to 2023-05-03T05:30Z at 5-minute snapshots.

**Bookmakers rule:** every 10 bookmakers count as 1 region, and `bookmakers` overrides `regions`.
- `bookmakers=pinnacle,draftkings,fanduel,betmgm,betrivers` costs 1 "region".
- Paid-only books: williamhill_us, fanatics, courtside, rebet.

**Quota headers:** `x-requests-remaining`, `x-requests-used`, `x-requests-last`.

**Update intervals**

| Market type | Pre-match | In-play |
|---|---|---|
| Featured | 60 s | 40 s |
| Additional (props, alternates, periods) | 60 s | 60 s |
| Outrights | 5 min | 60 s |
| Exchanges | 20 s | 10 s |

- Within 6 h of kickoff the interval "begins decreasing" from the pre-match rate.
- Scores update about every 30 s while live.

**Plans:** Starter is free with 500 credits per month and "most" bookmakers.

| Plan | Price | Credits per month |
|---|---|---|
| 20K | $30 | 20,000 |
| 100K | $59 | 100,000 |
| 5M | $119 | 5,000,000 |
| 15M | $249 | 15,000,000 |

All paid plans include all bookmakers. Higher plans are available after creating an account.

**Rate limit:** 30 requests per second on paid plans. A 429 means "space requests out over several seconds."

**Budget**
- **Featured snapshot cost:** 3 credits per sport (h2h, spreads and totals with one bookmaker group). Both sports cost 6.
- **Live scores cost:** 1 credit per sport per poll.
- **Weekly live time:** about 39 hours.
- **Live-score credits per week:**

| Poll cadence | Credits per week | Fits |
|---|---|---|
| 30 s | about 4,700 | No plan below 100K |
| 60 s | about 2,340 (about 10K per month) | 20K plan, only if odds polling stays modest |

## Recommendations
The original design holds up. These are the changes and additions it needs.

1. **Client**
   - Use one `httpx.AsyncClient` (0.28.1), with `tenacity` 9.2.1 handling backoff on 429 and 5xx: 1 s, 2 s, 4 s plus jitter, at most 4 tries.
   - Add an `aiolimiter.AsyncLimiter` set to 5 requests per second. The documented 30 requests per second applies to paid plans only, so stay well below it.
   - Parse the `x-requests-*` headers into the SQLite `quota` table on every response, error responses included.
   - On 401 or an out-of-credit response, hard-stop and show a banner.

2. **Market scope**
   - Use the sport-level `/odds` only for `h2h,spreads,totals`, all in ONE call.
   - Do not call the per-event odds endpoint on the free tier. Props cost per event.

3. **Bookmaker set**
   - On the free tier, use `bookmakers=pinnacle,draftkings,fanduel,betmgm,betrivers,espnbet,hardrockbet,fliff,novig,prophetx`. That is exactly 10 books, so it still costs 1 "region" and adds more price-shopping and a second low-hold reference.
   - Do NOT include williamhill_us, fanatics, courtside or rebet unless on a paid plan.
   - Pinnacle odds come from its public website and may be delayed. Build the fair line from a no-vig consensus of Pinnacle with novig/prophetx, falling back gracefully when Pinnacle has no line for an FBS game.

4. **Historical data**
   - Treat historical endpoints as paid-only. The historical-odds page says so even though the homepage table conflicts.
   - At startup, optionally probe once with a cheap historical events call and record whether it is allowed.
   - Backtest from nflverse lines and CFBD `/lines` instead.

5. **Budgets**
   - **Free preset:** about 101 credits per week, or about 435 per month. Hold the 10% reserve, and have the allocator degrade live-score polling first.
   - **Paid presets:** the 20K preset as specified (about 17K per month) is near the cap. Either drop odds polling inside 30 minutes of kickoff to every 5 minutes, or recommend the 100K plan ($59) for 60 s live polling plus frequent odds.
   - **Odds TTL floor:** 60 s, or about 40 s in-play. Polling faster yields nothing new.

6. **Settlement**
   - Settle from CFBD `/games` and nflverse, which are free and authoritative.
   - Use Odds API `scores?daysFrom=1` (2 credits) only as a fallback. Scores calls cost credits even when empty.

7. **Live betting**
   - Disable it in free mode. In paid mode, require the snapshot used for pricing to be under 60 s old.

8. **Keep everything else as written:** the state machine, the free events endpoint driving the scheduler, append-only `odds_snapshots`, CLV against the last pre-kick sharp line, and the `team_aliases` mapping keyed on (date, home, away).

# nfl-model

## Fact-check summary

I re-ran the claims on this machine using the existing venv (`/private/tmp/claude-501/-Users-cole03roberts-Desktop-Football/dffddf62-bd2b-4ca6-b23b-64817b4b44fd/scratchpad/nv`, Python 3.14.5, polars 2.0.0). I also checked PyPI, the GitHub API, the 538 source code, nflverse DATASETS.md and the nfelo pages. The verification script is `/private/tmp/claude-501/-Users-cole03roberts-Desktop-Football/dffddf62-bd2b-4ca6-b23b-64817b4b44fd/scratchpad/chk.py`.

Most claims hold up. There are six corrections and five additions:

| # | Original claim | What I found |
|---|---|---|
| 1 | nflreadpy has 21 loaders | **Wrong.** v0.1.5 exposes **25**. The list missed `load_stats`, `load_teams`, `load_pfr_advstats` and `load_ffverse`. |
| 2 | Calling a loader with no argument gives the current season | **Only partly true.** It depends on the loader (see §1). |
| 3 | Totals SD is missing ("—") for 2020 and 2026 | **The data exists.** SD(total − total_line) is 12.84 for 2020 and 13.04 for 2026 YTD. |
| 4 | Favourites win 61.4% at exactly −3 | **Wrong.** I get **58.0%**, against 58.8% under the SD-13.5 normal, so the normal is fine at 3. At 7 I get 75.5% (claimed 76.4%) and at 10 I get 83.3% (claimed 85.7%). The conclusion that SD 13.5 understates big favourites still holds, but the gap only appears from about 6 points up. |
| 5 | Schedule lines are closing lines | **Not documented.** DATASETS.md does not name the sportsbook or say whether lines are opening or closing. Treat them as approximate closing consensus lines. |
| 6 | Upcoming games already carry lines | **Only about 1–2 weeks out.** 25 of 179 unplayed 2026 games have a `spread_line`. For example, 2026_08_CHI_SEA has a total but no spread, and week 9 is all null. The app cannot use nflverse as its live odds source; that has to be The Odds API. |

Additions:
- nfelo publishes results against the **opening** line as well as the closing line, and they are better (see §3).
- There is a strategy problem with blend weights against a sharp line (see §4 of the recommendations).
- nflreadpy's default timeout is 30 seconds, and there is also an `NFLREADPY_USER_AGENT` setting.
- The nfl_data_py repo is confirmed archived; its last push was 2025-09-25.
- The 538 margin-of-victory multiplier uses an Elo difference that **includes** HFA.

## 1. Getting nflverse data from Python 3.14

### Packages
- **`nfl_data_py` is deprecated.** Confirmed in the README ("deprecated in favour of nflreadpy… No further nfl_data_py maintenance or updates are planned"). The GitHub API reports `archived: true`, last pushed 2025-09-25. https://github.com/nflverse/nfl_data_py
- **`nflreadpy` 0.1.5 is still the latest.** Confirmed on PyPI (uploaded 2025-11-19T14:26Z) and as GitHub release v0.1.5.
  - `requires_python >=3.10`
  - Dependencies: requests>=2.28, polars>=0.20, platformdirs>=3, tqdm>=4.64, pydantic>=2, pydantic-settings>=2
  - Optional extra: `pandas>=1.5`
  - Links: https://pypi.org/project/nflreadpy/ and https://nflreadpy.nflverse.com/
  - No release has come out in about 11 months, so pin it: `nflreadpy==0.1.5`.
- **Python 3.14:** confirmed working locally on 3.14.5 with polars 2.0.0. The PyPI classifiers stop at 3.13, so 3.14 support is tested by us only, not declared by the package.
- **Return type:** every loader returns a polars DataFrame. Call `.to_pandas()` if you need pandas.

### Loaders (25 in v0.1.5)
load_pbp, load_schedules, load_team_stats, load_player_stats, load_stats, load_players, load_teams, load_rosters, load_rosters_weekly, load_snap_counts, load_nextgen_stats, load_pfr_advstats, load_ftn_charting, load_participation, load_draft_picks, load_injuries, load_contracts, load_officials, load_combine, load_depth_charts, load_trades, load_ff_playerids, load_ff_rankings, load_ff_opportunity, load_ffverse.

### Default seasons (correction)
The signature is `seasons: int | list[int] | bool | None`, but the default differs by loader:
- **Default `True` (all seasons):** `load_schedules`, `load_officials`, `load_draft_picks`, `load_combine`.
- **Default `None` (current season):** `load_pbp`, `load_rosters`, `load_injuries`, `load_depth_charts`, `load_participation`, `load_snap_counts`, `load_ftn_charting`, `load_rosters_weekly`.

### Configuration
Environment variables, verified in `nflreadpy/config.py`:

| Variable | Default | Values |
|---|---|---|
| `NFLREADPY_CACHE` | memory | memory, filesystem, off |
| `NFLREADPY_CACHE_DIR` | `platformdirs.user_cache_dir("nflreadpy")` | path |
| `NFLREADPY_CACHE_DURATION` | 86400 | seconds |
| `NFLREADPY_VERBOSE` | False | true/false |
| `NFLREADPY_TIMEOUT` | 30 | seconds |
| `NFLREADPY_USER_AGENT` | — | string |

You can also set these in code: `nflreadpy.config.update_config(cache_mode="filesystem", timeout=60)`.

### Schedules columns
- The column list is as originally reported.
- Sign convention, confirmed in DATASETS.md:
  - `spread_line` positive means the home team is favoured, and it "lines up with the result column".
  - `result` = home score − away score; it is NA for unplayed games.
- The sportsbook and the open/close timing of the odds are **not documented**.
- Example, verified: 2026_07_NE_CHI (gameday 2026-10-22) has spread_line 3.0, total_line 50.5 and home ML −148.

### Fallback direct URLs
- https://github.com/nflverse/nflverse-data/releases/download/schedules/games.parquet (returns a 302 to the asset, as expected)
- https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.parquet
- Each release has a `timestamp.json` you can check for freshness.

## 2. Empirical numbers from nflverse schedules (re-verified)

Regular season, non-neutral games:

| Seasons | Games | Home margin | Mean spread | SD(result − spread) | SD(total − line) |
|---|---|---|---|---|---|
| 2000–09 | 2,538 | +2.55 | 2.52 | 13.41 | 13.65 |
| 2010–19 | 2,529 | +2.25 | 2.28 | 13.26 | 13.31 |
| 2020 | 253 | +0.14 | 1.08 | 12.80 | 12.84 |
| 2021–25 | 1,333 | +2.06 | 1.63 | 12.64 | 13.13 |
| 2026 YTD | 88 | +0.69 | 1.78 | 11.82 | 13.04 |

**Key final margins, REG 2015–2025 (share of games by absolute margin):**
- 3 points: 14.6%
- 7 points: 8.8%
- 6 points: 7.0%
- 14 points: 5.1%
- 10 points: 4.9%
- 1, 2 and 4 points: about 4.4–4.6% each
- 17 points: 3.5%
- ties: 0.4%
- ATS push rate against the spread: 2.6%
- If you include playoffs, 3 becomes 14.9% and 10 becomes 4.7%.

**Win-probability scale:**
- The maximum-likelihood σ for P(home win) = Φ(spread/σ) over 2010–2025 is **11.16**. I confirmed this, and it matches a logistic slope of about 0.149 per point.
- Favourite win rate at exact spreads, actual vs Φ(L/13.5):

| Spread | Actual | Φ(L/13.5) |
|---|---|---|
| 3 | 58.0% | 58.8% |
| 7 | 75.5% | 69.8% |
| 10 | 83.3% | 77.1% |

**Not re-run:** the market Brier score (0.2111), log-loss (0.6096), 66.4% straight-up accuracy and 2.9% overround. They are plausible and in line with published figures. The 2.9% overround reflects nflverse's moneyline source, not the best price across books.

**HFA:** about 1.6–2.0 points is supported. 2021–25 had +2.06 actual margin and a 1.63 mean spread. 2026 YTD shows only +0.69 over 88 games, which is noise-level. The nfelo HFA study links are as originally given.

## 3. Published model approaches (verified)

### 538 Elo
Source: `forecast.py` at https://github.com/fivethirtyeight/nfl-elo-game
- HFA = 65.0
- K = 20.0
- REVERT = 1/3 toward 1505
- P = 1/(10^(−diff/400) + 1)
- MOV multiplier = ln(max(PD,1)+1) × 2.2/(winner_elo_diff×0.001 + 2.2)
  - winner_elo_diff **includes HFA**.
  - For ties the denominator is 1.0.

### nfelo market regression
Source: https://www.nfeloapp.com/analysis/using-market-regression-to-improve-prediction-accuracy-in-the-nfl/
- The best flat weight is **65% market**. Confirmed.
- The dynamic version uses error-weighted regression:
  - Each team's error is an exponential average of squared errors over its last N games.
  - Relative accuracy = model error − market error.
  - That is divided by a "Base" parameter and clipped to 0–1 to scale the weight.
- The article does not publish N or Base, so we must tune them ourselves.

### nfelo self-reported performance
Source: https://www.nfeloapp.com/games/nfl-model-performance/
- Closing-line ATS: 2015 67.9%, 2017 48.9%, 2023 50.5%, 2024 45.9%, 2025 53.3% (92 bets, +1.7u), 2026 YTD 56.0% (26 bets). Confirmed.
- **All-time:** 54.16% against the close, but **56.97% against the open**, with 6.01% average CLV per play.
- 2024 was a losing year against both lines: −14.6u open, −13.3u close.
- 2026 open-line CLV so far is only 0.78%.
- The page shows only about 90–145 qualifying bets per season and no "break-even+" label. The original claim's "break-even-or-better plays" wording could not be confirmed.
- The gap between open and close results supports betting early.

### Other approaches
- Glickman & Stern state-space model, Massey/ridge ratings and EPA ratings are as originally described (https://arxiv.org/pdf/1701.05976).

## 4. Walk-forward backtest, 2017–2025, 2,388 games

- I did not re-run this. The script is at `/private/tmp/claude-501/-Users-cole03roberts-Desktop-Football/dffddf62-bd2b-4ca6-b23b-64817b4b44fd/scratchpad/scripts/bt.py`.
- Results claimed:
  - RMSE: closing spread 12.79, model 13.26.
  - ATS: 49.4–51.0% at every disagreement threshold.
  - Break-even at −110: 52.38% (110/210). The arithmetic is correct.
- These results agree with nfelo's: a ratings model cannot beat the closing line on its own.
- One caveat: nflverse `spread_line` may not be a true sharp close from one book, so "closing line" here means nflverse's recorded consensus line.

## Recommendations
## Recommendations (corrected)

### 1. Data layer
- **Install:**
  - Command: `pip install "nflreadpy[pandas]==0.1.5" scipy numpy`
  - Pin the version: nflreadpy has had no release in about 11 months, and 3.14 support has only been tested locally.
  - Work in polars throughout.
- **Cache:** set `NFLREADPY_CACHE=filesystem`, or call `update_config(cache_mode="filesystem", timeout=60)`.
- **Load seasons explicitly**, for example `load_schedules([2024, 2025, 2026])`. Do not rely on defaults: `load_schedules()` returns every season.
- **Fallback:** if the package fails, download the release parquet files directly and check `timestamp.json` for freshness.
- **Live odds:** nflverse lines cover only about the next 1–2 weeks and come from an undocumented source. Use nflverse lines only for historical training and backtests. Take all live and actionable prices from The Odds API.

### 2. Ratings
- Ridge margin ratings plus EPA ratings, as originally proposed:
  - recency weight = 0.5^(age/120 days)
  - λ ≈ 8
  - learned HFA of about 1.6–2.0 points; 0 at a neutral site
- If you use Elo instead:
  - K = 20, 1/3 reversion to 1505, points ≈ Elo difference/25
  - Use HFA ≈ 45 Elo for the modern era, not 538's 65.

### 3. Market blend (corrected)
- **Blend formula:** M = w·S_mkt + (1−w)·M_model.
- **The blend weight sets the bet threshold.** The edge against the line you blended with is (1−w)·(M_model − S). At −110 you need P(cover) ≥ 52.38%, which with σ ≈ 13 requires about a 0.8-point shift in M, and 2% EV (p ≥ 53.45%) requires about 1.1 points.
  - With w = 0.85, the model has to disagree by about 6–8 points before you bet.
  - My backtest shows those large disagreements are mostly model error.
  - So do **not** blend with and bet against the same sharp consensus number.
- **Better design:**
  - (a) Build a "fair line": a de-vigged, sharp-weighted consensus from The Odds API (Pinnacle if available, otherwise the median across books), lightly adjusted by the model with w ≈ 0.85–0.9.
  - (b) Bet only where a specific book's price beats that fair line by the EV threshold. That is line shopping and stale-price betting.
- **Against opening or early-week lines,** use w ≈ 0.65. nfelo hits 56.97% ATS against the open but only 54.16% against the close.
- **Dynamic weights:** nfelo's error-weighted w has unpublished N and Base parameters, so tune them with walk-forward validation.

### 4. Probabilities
- **Spread:** P(home covers L) = 1 − Φ((L − M)/13.0). Better: use an empirical distribution of (result − spread) bucketed by spread, so it keeps push mass on 3 and 7.
- **Moneyline:** Φ(M/11.16). This matters for spreads of 6 or more; at 3 points, SD 13.5 is fine.
- **Totals:** σ ≈ 13.1.

### 5. Bet selection and sizing
- Bet only when EV ≥ +2% at the best book price.
- Size with quarter-Kelly, f = 0.25·(b·p − q)/b, capped at 2% of bankroll.
- Log CLV for every bet against the actual closing price from The Odds API (snapshot shortly before kickoff), not against nflverse `spread_line`.

### 6. What to expect (show this honestly in the app)
- The model alone is about 50–51% ATS against the close.
- nfelo, a mature public model, averages 54% against the close, with losing seasons (2024: 45.9%).
- Realistic target: 1–3% ROI on selected plays, with the edge coming from:
  - early-week and opening lines
  - stale or outlier book prices
  - fast reaction to QB news
- Track CLV as the main success metric; season profit and loss is too noisy at about 100 bets a season.

# cfb-model: state-of-the-art public FBS game prediction, and frugal use of CollegeFootballData API v2 (verified 2026-10-20)

## Verification summary
I checked the CFBD facts against live sources on 2026-10-20: the API spec, PyPI JSON, the CFBD docs pages, and a real install of `cfbd` on Python 3.14.5. Most of the CFBD API facts are correct. Seven claims needed correcting or qualifying:
- **(a)** The "76% accuracy" figure comes from a separate win/loss model, not the spread model. The 15.72 RMSE spread model also used pregame Vegas spreads as inputs.
- **(b)** `/metrics/wp/pregame` is a market-derived number built from the point spread, not a CFBD rating forecast.
- **(c)** CORE, SP+ and FPI values pulled by `year` are retrospective or end-of-season. CORE's own docs say historical values are not point-in-time.
- **(d)** The SP+ article does not support "two-thirds of the projection" or "the prior 4 seasons." Those claims are removed.
- **(e)** The "lessons" post covers the 2021 season (published 2022-01-13), not 2022.
- **(f)** The FiveThirtyEight URL is dead (it redirects to abcnews.com/politics), so the "MAE under 12.5 is exceptional" claim can't be checked.
- **(g)** pydantic 1.10.25 adds only "minimal support" for 3.14, and 1.10.26 is the latest. It does work in practice (tested below).

## 1. CFBD API: verified facts

### Base URL and auth
- **Base URL:** `https://api.collegefootballdata.com`. The spec's `servers` entry is that host, `info.version` is 5.33.0, and the spec has 93 paths.
- **apinext alias:** `https://apinext.collegefootballdata.com/api-docs.json` returns the same file, 285,030 bytes. Verified with curl.
- **History:** The GA post is dated 2025-01-04, though its body still says "beta." Both hostnames were set to point at v2 from May 2025. (https://radsportsanalytics.com/blog/api-v2-is-now-in-general-availability/)
- **Auth:** `Authorization: Bearer <key>`. The securityScheme is `http`/`bearer`.
- **401 check:** an unauthenticated request returns 401 with `Unauthorized. Did you forget to add "Bearer " before your key?` (verified).
- **Key storage:** the official docs use `CFBD_API_KEY`. Never put the key in a URL. (https://api.collegefootballdata.com/authentication.md)

### Tiers
Verified on https://collegefootballdata.com/api-tiers on 2026-10-20:

| Tier | Price | Calls/mo | Adds |
|---|---|---|---|
| Free | $0 | 1,000 | Basic, historical, team/player stats, recruiting, betting lines, advanced metrics (EPA/PPA/WP) |
| Academic | $0 | 3,000 | Same as Free; needs a .edu email |
| Tier 1 | $1 | 5,000 | Opponent-adjusted metrics (`/wepa/*`, `/games/{id}/preview/adjusted`), weather (`/games/weather` says "Requires Patreon"), live scoreboard |
| Tier 2 | $5 | 30,000 | Live play-by-play (`/live/plays`) |
| Tier 3 | $10 | 75,000 | GraphQL with subscriptions |
| Tier 4 | $15 | 125,000 | Same features as Tier 3 |
| Tier 5 | $20 | 200,000 | Same features as Tier 3 |
| Tier 6 | $30 | 500,000 | Same features as Tier 3 |

### Throttling and usage tracking
- **No per-request throttle:** the GA post says "there is no request throttling in REST API v2", only monthly limits.
- **Cloudflare blocks:** many simultaneous requests can get you blocked by Cloudflare for about 10 minutes (verified on the GA post), so make requests one at a time.
- **`GET /info`:** returns the `UserInfo` schema: `patronLevel, tierName, monthlyLimit, remainingCalls, usedCalls, resetAt, sharedPool, products, features`.
- **`GET /info/usage`** (params `days, limit, api`): returns `window, api, totals{requests,cfbRequests,cbbRequests,uniqueEndpoints}, topEndpoints, recentRequests`. The spec says the CFB and CBB (basketball) call pool is shared.
- **Playground calls count:** requests from the docs playground count toward usage like any other client (https://api.collegefootballdata.com/usage-and-access, last modified 2026-10-18).
- **Error codes:** 400 = bad parameters, 401 = auth, 404 = wrong route. A "quota or entitlement response" means your tier can't make that request.

### Endpoints
All paths and parameters below were checked against the live spec (`*` = required).
- **`/games`**
  - Params: year, week, seasonType, classification, team, home, away, conference, id, competition, round.
  - The `Game` row includes `homePregameElo/awayPregameElo`, `homePostgameElo`, `neutralSite`, `conferenceGame`, `home/awayClassification`, `homePoints/awayPoints`, `homePostgameWinProbability` and `completed`.
- **`/games/schedule`** (year, seasonType, week, classification, conference). **`/games/teams`** (year, week, team, conference, classification, seasonType, id).
- **`/games/{gameId}/preview`:** free tier. **`/games/{gameId}/preview/adjusted`:** Tier 1.
- **`/lines`**
  - Params: gameId, year, seasonType, week, team, home, away, conference, provider.
  - Returns `BettingGame` with `lines[]` items of type `GameLine`: `provider, spread, formattedSpread, spreadOpen, overUnder, overUnderOpen, homeMoneyline, awayMoneyline`.
  - `spread` is quoted from the home team's side; a negative value means the home team is favored.
  - Coverage starts in 2013, and "provider and field coverage varies by game", so `spreadOpen` can be null. (https://api.collegefootballdata.com/data-availability.md)
- **Ratings**
  - **`/ratings/sp`** (year, team only, with no week param). Fields: rating, ranking, offense, defense, specialTeams, sos, secondOrderWins.
  - **`/ratings/elo`** (year, week, seasonType, team, conference, preseason). The `week` param returns the latest completed regular-season game at or before that week.
  - **`/ratings/fpi`** (year, team, conference). FPI is third-party data "following the source's publication schedule."
  - **`/ratings/srs` and `/ratings/srs/expanded`.** The expanded version adds a classification param and FCS teams. There is **no expanded SRS for 2020.**
  - **`/ratings/core`** (year, team, conference; needs year or team).
  - **`/ratings/sp/conferences`.**
- **CORE ratings in detail**
  - Fields: `year, throughSeasonType, throughWeek, team, conference, overall, offense, defense, offensePlays, defensePlays, modelVersion`.
  - `offense` is points above average per 100 qualifying plays (higher is better). `defense` is points allowed above average per 100 plays (lower is better). `overall` = offense − defense.
  - It uses only FBS-vs-FBS games and excludes overtime, garbage time and special teams. The model is proprietary.
  - Public ratings are retrospective from 2016 on, "not a record of what the model would have reported at that point in time." (https://api.collegefootballdata.com/core-ratings.md)
- **PPA**
  - `/ppa/teams` (year, team, conference, excludeGarbageTime, classification)
  - `/ppa/games` (year*, week, seasonType, team, conference, excludeGarbageTime, classification)
  - `/ppa/predicted` (down*, distance*)
- **`/metrics/wp/pregame`** (year, week, seasonType, team) returns `spread, homeWinProbability`. **Correction:** the docs say it "is derived from the available point spread... a market-based estimate rather than a CFBD team-rating forecast." (https://api.collegefootballdata.com/win-probability.md)
- **Priors**
  - `/talent` (year*): from 2015.
  - `/player/returning` (year, team, conference): from 2014. Fields: totalPPA, percentPPA, percentPassingPPA, percentReceivingPPA, percentRushingPPA, usage, passingUsage, receivingUsage, rushingUsage.
  - `/recruiting/teams` (year, team).
  - `/player/portal` (year*): from 2021.
  - `/coaches` (firstName, lastName, team, year, minYear, maxYear).
  - **New:** `/coaches/tenures` (coachId, team, year, active) and `/coaches/seasons`. These are a cleaner way to detect coaching changes.
- **Other:** `/stats/game/advanced` (year, team, week, opponent, excludeGarbageTime, seasonType), `/stats/season/advanced`, `/stats/game/havoc`, `/teams/fbs`, `/calendar` (year*), `/records`, `/teams/ats` (year*), `/scoreboard` (Tier 1), `/wepa/team/season` (Tier 1; team WEPA from 2008), `/drives`, `/plays` (year* and week*).

### Python client
- **Package:** `cfbd` 5.33.0, uploaded 2026-10-18T18:37. It requires `urllib3<3.0.0,>=1.25.3`, `python-dateutil`, `pydantic<2,>=1.10.5` and `aenum`, and declares no `requires_python`.
- **pydantic history on Python 3.14:**
  - 1.10.24 (2025-09-25) warned that pydantic V1 "will not work with Python 3.14".
  - 1.10.25 (2025-12-18) added "minimal support for Python 3.14".
  - 1.10.26 (2025-12-18) only fixes a Python 3.7 syntax error.
  - 1.10.25 and 1.10.26 are the first releases with cp314 wheels.
  - Sources: https://raw.githubusercontent.com/pydantic/pydantic/1.10.X-fixes/HISTORY.md and https://pypi.org/pypi/pydantic/json
- **Tested here:** `pip install cfbd` in a fresh venv on Python 3.14.5 (Homebrew, macOS) resolved to pydantic 1.10.26. `BettingGame.from_dict(...)` parsed correctly under `-W always` with no warnings.
- **Version conflict:** the latest pydantic is 2.14.1. `cfbd`'s `pydantic<2` pin conflicts with anything in the same environment that needs pydantic v2.
- **Method names:** all of these exist in the installed package:
  - `GamesApi.get_games/get_game_schedule/get_game_team_stats`
  - `BettingApi.get_lines`
  - `RatingsApi.get_sp/get_elo/get_fpi/get_core/get_srs/get_expanded_srs`
  - `MetricsApi.get_predicted_points_added_by_game/_by_team/get_pregame_win_probabilities`
  - `TeamsApi.get_talent`
  - `PlayersApi.get_returning_production/get_transfer_portal`
  - `RecruitingApi.get_team_recruiting_rankings`
  - `InfoApi.get_user_info/get_usage`
  - `CoachesApi.get_coaches`
  - `AdjustedMetricsApi.get_adjusted_team_season_stats`
- **Usage** (`cfbd.Configuration(access_token=...)`, `with cfbd.ApiClient(cfg)`) matches https://api.collegefootballdata.com/libraries/python.md.

## 2. State of the art: corrected

### SP+ (Connelly, ESPN, published 2026-03-27)
- **Four ingredients:**
  1. Returning production, with rosters updated for transfers and attrition and combined with last year's SP+.
  2. Recent history, a "program health" factor. The article does not say how many years it covers.
  3. Recent recruiting, which includes incoming transfers' quality. It is "about 1-2%" of the projection now, versus about 20-25% a decade ago.
  4. Coaching-change effects, measured against a 20-year SP+ baseline.
- **Removed claims:** "two-thirds of the projection" and "prior 4 seasons" are not in the article. It only says prior SP+ plus returning production gets you "pretty far down the projections road."
- **Team count:** 138 FBS teams. North Dakota State (72nd) and Sacramento State (130th) are new.
- Source: https://www.espn.com/college-football/story/_/id/48306284/2026-college-football-sp+-rankings-138-fbs-teams

### FPI (ESPN, post dated 2016-08-10)
- **Preseason prior:**
  - Uses 4 years of EPA-based prior performance; "the most recent year counts almost twice as much as the three years before it."
  - A starting QB is worth about 3.3 points per game for an average offense; a transfer QB counts half.
  - Recruiting is a "very minor" factor, drawn from 4 services.
  - A new head coach regresses the team slightly toward the mean.
- **In-season:**
  - Uses game-level EPA for offense, defense and special teams, adjusted for opponent and site.
  - Blowouts are capped.
  - Earlier games are re-weighted each week through Bayesian updating.
- **Caveat:** the methodology is about 10 years old.
- Source: https://www.espn.com/blog/statsinfo/post/_/id/122612/an-inside-look-at-college-fpi

### Elo
- **CFBD tutorial:** `E=1/(1+10^((Rb−Ra)/400))`, `R'=R+K(S−E)` with K=25.
  - Games are scored 1 for a win, 0.5 for a tie, 0 for a loss.
  - FBS teams start at 1500. The code starts non-FBS teams at 1200, though the summary text says 1500.
  - There is no MOV, no HFA and no offseason regression; the post suggests regressing toward 1500.
  - Source: https://radsportsanalytics.com/blog/talking-tech-elo-ratings/
- **Official CFBD Elo:**
  - Uses the result, the rating gap, the scoring margin and HFA (non-neutral games only).
  - Its formula, constants and starting values are proprietary.
  - An FBS team's rating does not change after a game against an opponent with no Elo rating.
  - Source: https://api.collegefootballdata.com/elo-ratings.md

### Opponent-adjusted EPA by ridge (CFBD blog)
- **Model:** target is per-play `ppa`, with one-hot offense and defense dummies.
- **HFA coding:** +1 when the home team is on offense, −1 when the away team is on offense, 0 at neutral sites.
- **Fit:** `RidgeCV` with alphas 75 to 325 in steps of 25. Alpha is "usually about 175" for a full season and higher for partial seasons. The 2019 example's chosen alpha was not reported.
- **Results and filters:** HFA was about 0.018 EPA per play in 2019. Only FBS-vs-FBS games are kept. The post does not mention filtering garbage time, so add that yourself.
- **Note:** CFBD's own WEPA (Tier 1) is a proprietary version of this method.
- Source: https://radsportsanalytics.com/blog/opponent-adjusted-stats-ridge-regression/

### Benchmarks (corrected)
- **CFBD LightGBM spread model (2020 post):**
  - Test RMSE 15.72, train RMSE 14.79.
  - 714 features across 17,662 games; trained on games before 2019, tested on 2019 onward.
  - Its features include pregame spreads and win probabilities, so it is not independent of the market.
  - Ed Feng's model had RMSE 16.0. **The 76% straight-up figure (AUC 0.87) comes from a separate winner-classification model.** No ATS results or comparison with Vegas were reported.
  - Source: https://radsportsanalytics.com/blog/predicting-spreads-gbdt/
- **Vegas line MAE:** SI (2021-11-04) gives 12.6 points for "the Vegas line", sourced only to a tweet, with no season or sample, and it doesn't say whether that is the closing line.
  - Source: https://www.si.com/betting/2021/11/04/college-football-week-10-best-bets-composite-ratings-picks-the-winners
- **FiveThirtyEight "under 12.5 exceptional":** UNVERIFIABLE, because the URL now 301-redirects to abcnews.com/politics. Compute your own benchmark: the MAE of the CFBD `/lines` closing `spread` against final margins for 2013-2025.
- **Lessons post:**
  - Covers the **2021** season (published 2022-01-13).
  - The author finished 1st straight-up, 3rd ATS, 1st absolute error and 4th MSE across 726 picks. The ATS record itself was not given.
  - The model used preseason only in week 1, a mix by week 4, and was dominated by in-season data late.
  - The post recommends adding market odds and updating often.
  - Source: https://radsportsanalytics.com/blog/lessons-from-picking-the-2022-cfb-season/

### Structural issues (still valid)
- **Sparse schedule:** there are 138 FBS teams and roughly 12 games each, so the schedule graph is sparse and clustered by conference. That calls for regularization and priors.
- **FCS opponents:** CORE, the ridge blog and CFBD's own SRS logic all exclude FCS or treat it separately. Use `/ratings/srs/expanded` as the FCS prior, but note it is missing 2020.

## 3. Leakage traps (critical, missing from the original)
- **No week parameter:** `/ratings/sp`, `/ratings/fpi` and `/ratings/core` have no `week` parameter. Calling them by year returns the current or final snapshot, and CORE history is explicitly retrospective. They can only be used point-in-time if **the app snapshots them itself each week** and stores the pull date.
- **Safe in backtests:**
  - `homePregameElo/awayPregameElo` from `/games`
  - `/ratings/elo?week=` (as of that week)
  - `/talent` and `/player/returning` (preseason by definition)
  - Prior-year SP+ and FPI
- **`/metrics/wp/pregame` is a market feature:** it comes from the spread. Using it as a feature and then also shrinking toward the market double-counts the market. If it is built from the closing line, it also leaks closing information into any comparison against the opener.

## 4. Call budget (verified shapes)
- **Season-wide pulls:** every endpoint in the plan accepts `year` with no team filter, so one call covers the whole season.
- **Backfill:** about 12 calls per season, so 2016-2025 is about 120 calls. Lines start in 2013, talent in 2015, returning production in 2014 and the portal in 2021.
- **Weekly refresh:** about 6 calls (see the Step 5 list in the recommendations); `/ratings/elo` is redundant with Elo in `/games`. That is about 6 × 17 ≈ 100 calls per season plus about 7 preseason calls.
- **Total:** about 230 calls per year, comfortably inside the free 1,000 per month.

## Recommendations
## Recommended CFB model (corrected): Prior + Ridge + Blend + Market-shrink

### Step 1. Preseason prior R0 (points vs. an average FBS team)
`R0 = b0 + b1*P + b2*P*(RP-0.5) + b3*hist + b4*talent_z + b5*newcoach*P`

Inputs:
- `P` = prior-year final rating (your own, plus prior-year SP+ from `/ratings/sp?year=Y-1`)
- `RP` = `percentPPA` from `/player/returning`. Also consider adding `percentPassingPPA` as a QB proxy, since FPI values a returning starting QB at about 3.3 points per game.
- `hist` = mean of the prior 2-4 years' ratings. Tune the window; SP+ does not publish a number of years.
- `talent_z` = z-score of the `/talent` composite
- `newcoach` = coaching-change flag from `/coaches/tenures` or `/coaches?year`

Fitting:
- Fit on 2016-2025 (2015+ is the talent limit).
- Expect `b1 < 1` and a small talent weight.
- FCS teams get their prior from `/ratings/srs/expanded`. It has no 2020 data, so fall back to 2019 or the FCS mean.

### Step 2. In-season ratings by ridge shrinkage toward R0
- Margin model: `margin_home = HFA*(1-neutral) + R_home - R_away`, with margins capped at ±28.
- Shrinkage: write `R = R0 + Δ` and apply a ridge penalty λ to `Δ`. Tune λ per week of season by walk-forward validation.
- Recency weighting: `w = 0.97^weeks_ago`.
- Efficiency version: the same fit on `/ppa/games` offense and defense values with `excludeGarbageTime=true`, FBS-only (as in the CFBD blog and CORE).
- Convert efficiency to points at about 65-72 plays per game, fitted.
- Blend: `R_model = a*R_pts + (1-a)*R_epa`, with `a` fitted (about 0.3-0.4).
- Fit `HFA` from data. Expect it to have shrunk in recent seasons, and do not hardcode 3.

### Step 3. Stacker, using only point-in-time features
Features:
- `R_model` differential
- Pregame Elo differential from `/games`
- Prior-year SP+ and FPI differentials
- **Your own weekly snapshots** of SP+, FPI and CORE, taken from the moment the app starts running. Never use year-level pulls in backtests: they are end-of-season and retrospective, and CORE's docs say so explicitly.
- Do **not** use `/metrics/wp/pregame` as a model feature. It is derived from the spread, and the market enters in Step 4.

Training: walk forward by week.

### Step 4. Probabilities and bet decision
- **Win probability:** `P(home win) = Φ(pred/σ)`.
- **Cover probability:** `P(home covers) = Φ((pred + spread_home)/σ)`, where `spread_home` is negative when the home team is favored (the CFBD `/lines` convention; normalize The Odds API to match).
- **σ:** fit it on residuals, starting near 15.5-16 (the RMSE of good models). Let it vary by week and spread size.
- **Key numbers:** adjust using the empirical margin distribution from `/games` (3, 7, 10, 14).
- **Market shrink:**
  - `final = α*model + (1-α)*market` (consensus from The Odds API), with `α` fitted by backtest. Expect 0.1-0.4.
  - Compute your own closing-line MAE from CFBD `/lines` for 2013-2025 rather than relying on the unsourced 12.6.
- **Bet only when:**
  - P(cover) > 0.5238 at -110 (110/210), plus a margin of about 1-2 percentage points.
  - `|final − line| ≥ 2.5-3` points.
  - Use the best available price across books.
- **Staking:** quarter-Kelly, capped at 2-3% of bankroll per bet.
- **Evaluation:**
  - Measure CLV against the CFBD `spread` (close) versus your bet line. `spreadOpen` and `spread` are both available, but `spreadOpen` is sometimes null, so record your own Odds API snapshots too.
  - Also test early weeks and G5 games, and evaluate totals separately.
  - Skip FCS games.

### Step 5. Frugal CFBD usage
**Client**
- Preferred: a thin `httpx` client with `Authorization: Bearer $CFBD_API_KEY`, which avoids the pydantic<2 pin.
- If you use `cfbd==5.33.0`, pin `pydantic>=1.10.26,<2`. 1.10.25+ adds only minimal 3.14 support, though it has been tested working on 3.14.5. Keep it in an isolated environment if the rest of the app uses pydantic v2.

**Cache**
- Store responses in SQLite keyed by (path, sorted params), plus the fetch timestamp.
- Completed seasons are immutable, so cache them forever.
- Store **weekly snapshots** of `/ratings/sp`, `/ratings/fpi` and `/ratings/core` (point-in-time history).

**Call plan**
- **Backfill:** about 12 calls per season × 10 ≈ 120. Lines start in 2013, talent in 2015, returning production in 2014 and the portal in 2021.
- **Weekly refresh (Tuesday), about 6 calls:**
  - `/games?year&week`
  - `/ppa/games?year&week&excludeGarbageTime=true`
  - `/lines?year&week`
  - `/ratings/sp?year`
  - `/ratings/fpi?year`
  - `/ratings/core?year`
  - Drop `/ratings/elo`, since pregame Elo is already in `/games`.
- **Preseason, once:** `/talent`, `/player/returning`, `/recruiting/teams`, `/player/portal`, `/coaches`, `/ratings/srs/expanded`.
- **Total:** about 230 calls per year, against the free tier's 1,000 per month.

**Request hygiene**
- Make requests serially, with no concurrency, to avoid Cloudflare's roughly 10-minute blocks.
- Call `/info` once per session and show `remainingCalls` and `resetAt` in the TUI. Remember the pool is shared with CBBD.
- Do not test endpoints in the docs playground, because those calls count too.

**Skip on the free tier**
- `/scoreboard`, `/wepa/*`, `/games/weather` and `/games/{id}/preview/adjusted` (Tier 1+), and `/live/plays` (Tier 2).
- Use The Odds API for odds and live scores.
- The optional $1 Tier 1 unlocks WEPA, a proprietary ridge-style adjustment that can replace your own Step 2 efficiency fit.

# strategy: bankroll-growth-optimal betting (Kelly, removing the vig, blending model and market, CLV, calibration) for an NFL/FBS paper-betting TUI. Fact-checked and corrected 2026-10-20.

## Fact-check summary (what changed)
- **Wrong: Python 3.14 risk for penaltyblog.** penaltyblog 1.13.1 was released 2026-10-05 and ships cp314 and cp314t wheels, including `macosx_11_0_arm64`, plus cp315 wheels. It installs cleanly on the user's Mac. The package that actually has the risk is **`shin` 0.2.2**, a Rust extension whose newest macOS wheels are cp311 to cp313. On 3.14 it builds from the sdist, which needs a Rust toolchain. SciPy 1.18.1 and NumPy 2.5.4 both have cp314 macOS arm64 wheels. Textual 8.2.8 is pure Python. (Checked against PyPI JSON.)
- **Wrong: significance claim.** With about 1,000 bets the standard error of ROI is about 3%, so a 2% edge cannot be told apart from luck. You need about 9,000 bets for 2 sigma (math below).
- **Wrong: parlay numbers.** A 2-leg -110 parlay pays decimal 3.645 (+264). The fair price is +300. The expected loss is **8.9%** of stake, versus 4.55% for a single bet. The booksum-style margin is 9.75%. The "9.1%" figure matched neither of these.
- **Wrong attribution.** arXiv 2604.24723 is by Ruslan Tepelyan and Daniel Lam (April 2026). The page lists no affiliation, so "Bloomberg" is unsupported and has been removed.
- **Missing: the Odds API free plan has NO historical odds.** "Historical Odds" is struck through for Starter on the pricing page, and the docs say historical endpoints are "only available on paid usage plans". Walk-forward calibration must therefore use free line history from nflverse and CFBD (see section 9).
- **Missing: pushes on integer spreads and totals** need a three-outcome Kelly formula and a three-outcome EV formula (section 5).
- **Missing: the `bookmakers=` parameter saves quota** (section 3).
- **Unverified:** Benter's .1016 → .1327 pseudo-R² comes only from a secondary blog. The primary PDF is a scanned image and could not be read. The encyclopedia.pub "18k markets" claim returned HTTP 403 and could not be checked. Treat both as indicative only.

## 1. Converting odds
- The Odds API `oddsFormat` accepts `decimal` (the default) or `american`. The docs warn that American odds can have rounding discrepancies, so request decimal.
- American to decimal: `d = 1 + A/100` if A > 0, else `d = 1 + 100/|A|`. Implied (vigged) probability `r = 1/d`.
- -110/-110 gives d = 1.9091 and r = 0.5238 on each side. The booksum is 1.0476, so the overround is 4.76% and the hold is 4.55%. Break-even is 52.38%. (Verified arithmetic.)

## 2. Removing the vig
Let r_i = 1/d_i, R = Σr_i, and n = number of outcomes.
- **Basic / multiplicative:** `p_i = r_i/R`. The implied vignette says this "tend[s] to be the least accurate of the methods in this package". (Verified.)
- **Additive:** `p_i = r_i - (R-1)/n`. It can go negative. The vignette says: "If there are only two outcomes, the additive method and Shin's method are equivalent." (Verified.)
- **Power:** `p_i = r_i^k` (the vignette writes the exponent as 1/k), with k solved by brentq so that Σp = 1. The vignette notes the Wisdom of the Crowds document calls this "the logarithmic method". (Verified.) penaltyblog also lists a separate `logarithmic` method, so do not assume the two names mean the same code.
- **Odds ratio (Cheung):** solve a single OR so that Σp = 1. (Verified.)
- **Shin:** the closed form `p_i = [sqrt(z² + 4(1-z) r_i²/R) - z] / (2(1-z))`, with z solved numerically. The vignette gives no formula, so this standard form comes from the Shin/Jullien-Salanié literature; unit-test it against penaltyblog. Secondary sources say Shin beats basic normalization (Springer 2022, citing Clarke et al. 2017 and Koning & Boot 2020).
- **Practical point for two-way NFL/CFB markets:** Shin equals additive. For near-symmetric -110/-110 spreads and totals, every method returns about 0.5/0.5. The choice only matters for lopsided moneylines. Default to power (or Shin) for ML.
- **Caveat:** the favourite-longshot evidence (encyclopedia.pub, about 18k markets) is unverified (403). US NFL moneyline evidence on FLB direction is mixed, so A/B the method choice on your own closing-line calibration rather than assuming one.
- **Python:**
  - `penaltyblog` 1.13.1 (Python >= 3.10, cp314 wheels available).
  - `shin` 0.2.2 (no cp314 macOS wheel; needs Rust to build). Avoid it or vendor 20 lines of SciPy instead.

## 3. Fair line, line shopping and quota
- The Odds API lists Pinnacle (`pinnacle`) only in region **`eu`**. (Verified on the bookmaker page.) US regions:
  - `us`: draftkings, fanduel, betmgm, williamhill_us, betrivers, fanatics, bovada, betonlineag, lowvig, mybookieag, betus
  - `us2`: espnbet, hardrockbet, fliff, betparx, ballybet, …
  - `us_ex`: kalshi, novig, prophetx, polymarket, …
- **Quota:**
  - Cost of `/v4/sports/{sport}/odds` = markets × regions. Empty responses are free.
  - **Every 10 keys in `bookmakers=` count as one region, and `bookmakers` overrides `regions`.**
  - So `bookmakers=pinnacle,draftkings,fanduel,betmgm,williamhill_us,espnbet,fanatics,betrivers,hardrockbet,lowvig` with `markets=h2h,spreads,totals` costs **3 credits** per sport call. Using `regions=us,us2,eu` would cost 9.
  - Sport keys: `americanfootball_nfl`, `americanfootball_ncaaf`.
  - Watch the `x-requests-remaining`, `x-requests-used` and `x-requests-last` headers.
- **Plans** (the-odds-api.com, checked 2026-10-20):

  | Plan | Price | Credits/month | Historical odds |
  |---|---|---|---|
  | Starter | Free | 500 | No |
  | 20K | $30/mo | 20k | Yes |
  | 100K | $59/mo | 100k | Yes |
  | 5M | $119/mo | 5M | Yes |
  | 15M | $249/mo | 15M | Yes |

  - Historical endpoint: `/v4/historical/sports/{sport}/odds?date=...`. It costs 10 × markets × regions and has 5-minute snapshots since September 2022.
- **Budget on the free plan:**
  - One full refresh of both sports costs 6 credits, so 500 credits is about 83 refreshes per month.
  - Closing snapshots per kickoff window (NFL about 4/week, FBS about 5-6/week) use most of that.
  - Ways to save: query only the sport whose window is closing, use the event-odds endpoint for single games, or use a fixed set of 10 or fewer books.
- Fair probability: vig-free Pinnacle. Fall back to the median vig-free probability across books.
- Bet price: `d_best = max d_i` over the books the user enables. EV is computed at d_best.
- Break-even: -105 needs 51.22% and -104 needs 50.98%. (Verified arithmetic.)

## 4. Blending model and market
- Blend in logit space, `logit p = a + b1·logit p_model + b2·logit p_market`, fitted on out-of-sample history. This is Benter's two-stage method. The reported pseudo-R² values (model .1016, public .1237, combined .1327, 2,313 races, 1988-93) come only from a secondary source (oddspapi blog); the primary scan could not be read.
- Fair & Oster, "College Football Rankings and Market Efficiency", *J. Sports Economics* 8(1):3-18 (2007). None of the ranking systems, including the optimal combination, adds information beyond the final Las Vegas spread. (Verified via the IDEAS abstract.) Expect b1 to be small.
- Prior weights before fitting are reasonable but ad hoc: w_model = 0.25 for NFL and 0.35 for FBS. If the fitted b1 is at or below 0, or not significant, stop betting that league/market.
- **Spreads:** `P(cover) = Φ((μ - line)/σ)`. Published NFL σ estimates:
  - 13.86: Stern (1981-84)
  - 13.45: FiveThirtyEight/PFR (1978-2012)
  - 13.59: arXiv 1211.4000 (2002-11)
  - about 14.4: Everson (2004-06)

  Start at 13.5 for NFL. Estimate FBS from residuals (larger, around 15-17, but fit it rather than assume it). Use a 0.5-point continuity correction, and compute P(push) explicitly on integer lines. An empirical, key-number-aware margin distribution (3, 7, 10, 6, 14) beats a pure normal near those numbers.

## 5. Kelly sizing
- Binary bet: `f* = (p·d - 1)/(d - 1)`. Example: `penaltyblog.betting.kelly_criterion(2.1, 0.55, fraction=0.5)` gives a 7.05% stake and 0.8177% expected growth. (Verified, and matches hand math: f* = 0.1409, halved = 0.0705.)
- **Push-aware (integer spread/total):** with p_w, p_p, p_l and b = d - 1,
  - `f* = (p_w·b - p_l) / (b·(p_w + p_l))`
  - `EV = p_w·b - p_l`
- **Fractional Kelly:**
  - Growth at fraction λ is (2λ - λ²)·G_max, so half Kelly keeps 75% of growth.
  - P(bankroll ever falls to fraction x) = x^(2/λ - 1). A 50% drawdown therefore has probability 50% at full Kelly and 12.5% at half Kelly. (Verified analytically.)
  - If you overestimate the edge by 1.5x, you are effectively betting 1.5x Kelly and keep only 75% of the growth.
  - **Default λ = 0.25**, with a maximum of 0.5. footix's `kelly_portfolio_torch` also defaults to `fraction_kelly=0.25`, `per_bet_cap=0.1` and `bankroll_cap=0.3`. (Verified.)
- **Simultaneous slate:** maximize E[log wealth] subject to f ≥ 0 and Σf ≤ cap.
  - Exact enumeration costs O(2^N), which is fine up to N of about 14. With pushes the scenario count is 3^N, so the practical limit drops to about 9.
  - Above that, use Monte Carlo (about 20k scenarios) with SLSQP.
  - Tepelyan & Lam, arXiv 2604.24723 (April 2026), give an O(N) integral-transform objective for independent bets.
  - penaltyblog's `multiple_kelly_criterion(decimal_odds, true_probs, fraction=1.0, max_total_stake=1.0, method="simultaneous"|"independent")` handles mutually exclusive outcomes of one event, not independent games. The proportional scale-down (`f_i ← f_i·cap/Σf`) is an acceptable fallback.
- **Correlation:** allow one market per game, or simulate margin and total jointly.

## 6. Thresholds and caps (defaults, judgment calls)
- EV at the best price ≥ 2% for ML and ≥ 2-3% for spreads/totals.
- Probability edge ≥ 1.5 percentage points.
- ML decimal odds between 1.30 and 5.0.
- Caps: 3% per bet, 4% per game, 20% per slate. Minimum stake $1.
- Size from current bankroll. Size a whole slate from the bankroll at slate start.
- Optionally halve the stake for bets placed more than 48 hours before kickoff.

## 7. Parlays: disabled by default
- A 2-leg -110 parlay pays 3.645 decimal (+264) against a fair +300. The EV is -8.9% at true 50/50 legs, versus -4.55% for a single bet, and the loss compounds with each leg.
- Moshrefi, arXiv 2607.14430 (July 2026, about 23M Kalshi moneyline trades): cross-game parlays are overpriced relative to the product of their legs' prices, and the markup grows with the number of legs. (Verified.)

## 8. Success metrics
- **CLV:** `CLV% = d_taken·p_close_fair - 1`, which equals d_taken/d_close_fair - 1 and is the expected ROI if the no-vig close is the true probability. Whelan (NBA moneylines, 3,670 games, 2022/23-2024/25) found that decile 9 had about +5% CLV but only +0.2% profit, and the top decile made +11.4%. The edge must beat a margin of about 4.5%. (Verified.) Report CLV against the no-vig close.
- **Calibration:** Brier score, log loss and a 10-bin reliability diagram for model vs. blend vs. no-vig close.
- **Significance (corrected):** per-bet return SD at -110 is about 0.955 stake units, so SE(ROI) = 0.955/√N.
  - N = 1,000 gives SE of about 3.0%.
  - Detecting a 2% ROI at 2σ needs about (2·0.955/0.02)², or about 9,100 bets.
  - Detecting 5% needs about 1,460 bets.
  - CLV has far lower variance, so it is the primary leading KPI.

## 9. Free historical line sources for backtesting and walk-forward fits
- **nflverse `games.csv`** (nflverse/nfldata) has these columns: `away_moneyline, home_moneyline, spread_line, away_spread_odds, home_spread_odds, total_line, under_odds, over_odds`. These are closing-ish lines, and `spread_line` is positive when the home team is favoured. (Verified from the CSV header.) The nflreadpy equivalent is `load_schedules()`.
- **CFBD `GET /lines`** (params `year` (required unless `gameId`), `week`, `seasonType`, `team`, `home`, `away`, `conference`, `provider`) returns `BettingGame` with `lines[]: GameLine{provider, spread, formattedSpread, spreadOpen, overUnder, overUnderOpen, homeMoneyline, awayMoneyline}`. (Verified from api-docs.json.)
- These replace the paid Odds API historical endpoint for fitting a, b1 and b2, for σ, and for backtests. Live CLV still comes from your own Odds API close snapshots.

## Recommendations
1. Build `strategy/` as pure, unit-tested NumPy/SciPy functions:
   - `american_to_decimal`
   - `devig(method='power'|'shin'|'additive'|'multiplicative')`, solved with brentq.
   - Default to power for ML; any method works for -110 spreads/totals.
   - penaltyblog 1.13.1 installs on Python 3.14 (cp314 arm64 wheel), so you may use it as a dependency or as a test oracle.
   - Do NOT depend on `shin` 0.2.2 (no cp314 macOS wheel).
2. Fetch odds with an explicit `bookmakers=` list of 10 or fewer keys. Include `pinnacle` for the fair line plus the user's enabled US books. Use `markets=h2h,spreads,totals` and `oddsFormat=decimal`, which costs 3 credits per sport call. Track quota with `x-requests-remaining`.
   - Fair line: vig-free Pinnacle, falling back to the median vig-free probability across books.
   - Bet price: the best price among enabled books, and record which book gave it.
3. Assume the free Starter plan (500 credits, no historical odds).
   - Snapshot closes yourself, about 5-10 minutes before each kickoff window, and only for the sport whose window is closing.
   - Warn when remaining credits are under about 60.
   - Offer the historical endpoint only when the user has a paid key.
4. Fit the logit blend `a + b1·logit p_model + b2·logit p_market` walk-forward on free historical lines: nflverse games.csv moneyline/spread/total columns (via nflreadpy `load_schedules`) and CFBD `GET /lines` (spread, spreadOpen, overUnder, homeMoneyline, awayMoneyline).
   - Prior weights before fitting: w = 0.25 for NFL, 0.35 for FBS.
   - If b1 ≤ 0 or is not significant, auto-disable that league/market.
5. Spreads/totals:
   - Use Φ with σ ≈ 13.5 for NFL. Fit FBS σ from residuals.
   - Apply a 0.5-point continuity correction.
   - Compute push probability explicitly on integer lines, with a key-number-aware empirical distribution for NFL.
   - Size with the push-aware Kelly `f = (p_w·b - p_l)/(b·(p_w+p_l))`.
6. Sizing:
   - λ = 0.25 by default, user-adjustable from 0.1 to 0.5.
   - Caps: 3% per bet, 4% per game, 20% per slate. One market per game unless margin and total are simulated jointly.
   - Slate optimizer: exact enumeration up to N ≈ 14 (≈ 9 with pushes), Monte Carlo + SLSQP above that, and proportional scale-down as the fallback.
7. Filters:
   - EV ≥ 2% at the best price, edge ≥ 1.5 percentage points, ML odds between 1.30 and 5.0.
   - Parlays off by default. If enabled, show the compounded house edge (2-leg -110 is -8.9% EV).
8. KPIs:
   - Primary: no-vig CLV (`d_taken·p_close_fair - 1`), mean and % positive.
   - Also show ROI with a ±2·0.955/√N confidence band, log-growth, max drawdown, and Brier/log loss/reliability plots for model vs. blend vs. close.
   - Label results under about 1,500 bets as statistically inconclusive for edges of 5% or less. A 2% edge needs about 9,000 bets.

# tui

## Fact-check summary for 2026-10-20 (Python 3.14.5 from Homebrew, macOS arm64)

I re-checked every package version on the PyPI JSON API and every Textual claim in the upstream CHANGELOG. I also re-ran the stack in local 3.14 venvs:
- Main venv: /private/tmp/claude-501/-Users-cole03roberts-Desktop-Football/dffddf62-bd2b-4ca6-b23b-64817b4b44fd/scratchpad/v
- Separate cfbd-only venv: /private/tmp/claude-501/-Users-cole03roberts-Desktop-Football/dffddf62-bd2b-4ca6-b23b-64817b4b44fd/scratchpad/vc

**Version numbers:** all correct.

**Corrections and additions:**

1. **Critical, missing from the original: cfbd and nflreadpy cannot be installed in the same environment.**
   - cfbd 5.33.0 requires `pydantic<2,>=1.10.5`, `urllib3<3`, `python-dateutil` and `aenum`.
   - nflreadpy 0.1.5 requires `pydantic>=2.0.0`.
   - Running `pip install --dry-run cfbd==5.33.0 nflreadpy==0.1.5` fails with `ResolutionImpossible`.
   - The original's recommendation to pin both `cfbd==5.33.0` and `pydantic==2.14.1` cannot be installed.
   - cfbd does work on 3.14 in its own venv: it pulls pydantic 1.10.26, and `Game.from_dict` parsed correctly with no warnings under `-W always`. It is also synchronous, because it uses urllib3.
   - **Fix:** don't depend on `cfbd`. Call the CFBD REST API (`https://api.collegefootballdata.com`, header `Authorization: Bearer <key>`) directly with `httpx.AsyncClient`, and write your own pydantic v2 models for the few endpoints you use (`/games`, `/lines`, `/scoreboard`, `/ratings/sp`, `/teams/fbs`).

2. **Free-threaded `python3.14t`: the advice to avoid it is right, but the stated reason is wrong.**
   - pandas 3.0.6, pyarrow 26.0.0, numpy 2.5.4, scipy 1.18.1, scikit-learn 1.9.1 and pydantic-core 2.50.1 all ship `cp314t` wheels.
   - The real blocker is polars 2.0.0. It now depends on `polars-runtime-32==2.0.0`, which ships only `cp310-abi3` wheels, and the stable ABI is not available on free-threaded builds.
   - So nflreadpy will not install cleanly on 3.14t. Use regular CPython 3.14.

3. **`Select.BLANK` in 8.x: it wasn't removed, it quietly changed meaning.**
   - In 8.2.8, `Select.BLANK` still exists, but it is the inherited `Widget.BLANK` class variable, a `bool` equal to `False`.
   - Old code that compares a select's value with `Select.BLANK` therefore gives the wrong answer silently instead of raising an error. Always use `Select.NULL`.

4. **nflreadpy on polars 2.0: more loaders now confirmed.**
   - Beyond `load_schedules([2026])`, these also work on 3.14 with polars 2.0.0:
     - `load_teams()`: 36x16
     - `load_player_stats([2025])`: 19422x150
     - `load_pbp([2025])`: 48771x372
     - `load_team_stats([2025])`: 570x138
   - The `polars<2` fallback pin is now a precaution only.
   - nflreadpy uses `requests`, which blocks, so `@work(thread=True)` is required for it.

5. **Textual API surface: confirmed in 8.2.8 by import and introspection.**
   - `BUILTIN_THEMES` has 21 themes: ansi-dark/light, atom-one-dark/light, catppuccin-frappe/latte/macchiato/mocha, dracula, flexoki, gruvbox, monokai, nord, rose-pine/-dawn/-moon, solarized-dark/light, textual-dark/light and tokyo-night.
   - The default `App.AUTO_FOCUS` is `"*"`, the first focusable widget. That confirms you need `AUTO_FOCUS = "#cmd"`.
   - Also present: `App.push_screen_wait(screen, *, mode=None)`, `App.notify(message, *, title, severity, timeout, markup)`, `SuggestFromList`, `command.Provider`/`Hit`, and `reactive(..., always_update=...)`.
   - There is no `Toast` export from `textual.widgets`, as the original said.

6. **Changelog items: all confirmed verbatim.**
   - 6.3.0 (2025-10-11): Python 3.14 support (PR #6121).
   - 7.3.0 (2026-01-15): `Sparkline` can be any height (PR #6171).
   - 8.0.0 (2026-02-16): Catppuccin Frappe and Macchiato themes (PR #6335); 50ms delay when switching screens (PR #6362); breaking change `Select.BLANK` -> `Select.NULL` (PR #6374).
   - 8.2.5 (2026-04-30): ansi-dark and ansi-light themes (PR #6513).

7. **Minor details.**
   - textual-dev 1.8.0 was released 2025-10-11.
   - Installing textual-dev also pulls in textual-serve 1.1.3.
   - plotext 6.1.0 was released 2026-09-07. textual-plotext 1.0.1 still requires `plotext<6.0.0,>=5.2.8` and `textual>=0.86.2`.
   - Textual itself requires `rich>=14.2.0` and `platformdirs<5`.
   - Python version floors: numpy 2.5.4 and scipy 1.18.1 need 3.12 or later; pandas 3.0.6, pyarrow 26 and scikit-learn 1.9.1 need 3.11 or later. So `requires-python = ">=3.12,<3.15"` is consistent.

## Corrected package table

| Package | Latest (PyPI) | Status / notes |
|---|---|---|
| textual | 8.2.8 (2026-06-30) | Verified. Requires Python >=3.9,<4. |
| rich | 15.0.0 (2026-04-12) | Verified. |
| textual-dev | 1.8.0 (2025-10-11) | Dev only. Also brings in textual-serve. |
| textual-plotext | 1.0.1 (2024-11-30) | Forces plotext 5.3.2, below 6. |
| plotext | 6.1.0 is the newest, but use 5.3.2 | Verified. |
| httpx | 0.28.1 (2024-12-06) | Still the latest stable release. HTTP/2 needs the `httpx[http2]` extra and is off by default. |
| nflreadpy | 0.1.5 (2025-11-19) | Requires pydantic>=2 and polars>=0.20 with no upper bound. |
| polars | 2.0.0 (2026-10-06) | Depends on polars-runtime-32 2.0.0, which ships abi3 wheels only, so it won't install on 3.14t. |
| pandas / pyarrow / numpy | 3.0.6 / 26.0.0 / 2.5.4 | cp314 and cp314t wheels verified. |
| scikit-learn / scipy | 1.9.1 / 1.18.1 | cp314 and cp314t wheels verified. |
| pydantic | 2.14.1 | Verified. |
| **cfbd** | **5.33.0 (2026-10-18)** | **Requires pydantic v1 (<2), so it conflicts with nflreadpy. Don't install it in the app env; use raw httpx.** |
| aiosqlite | 0.22.1 | Optional and not needed. |

## The rest of the original section still holds

These parts are unchanged:
- **Textual features:** workers, reactives, `ModalScreen` with `push_screen_wait` inside `@work`, the command palette, theming, and the focus and modal-timing pitfalls.
- **Charting ranking:** PlotextPlot first, then Sparkline, then a custom braille widget.
- **Layout grid:** 3 columns `2fr 3fr 2fr` by 3 rows `1fr 1fr auto`.
- **Validated sketch:** /private/tmp/claude-501/-Users-cole03roberts-Desktop-Football/dffddf62-bd2b-4ca6-b23b-64817b4b44fd/scratchpad/app/sketch.py
- **Persistence:** sqlite3 in WAL mode with the ledger, bets, odds_snapshots and games tables, plus integer cents.

The only design change is that the CFBD client layer becomes a small async httpx module, not the cfbd SDK.

## Recommendations
1. pyproject.toml, with `requires-python = ">=3.12,<3.15"`, on regular CPython 3.14, not 3.14t. Pin:
   - textual==8.2.8
   - textual-plotext==1.0.1
   - plotext==5.3.2
   - rich==15.0.0
   - httpx==0.28.1
   - nflreadpy==0.1.5
   - polars==2.0.0 (the four loaders tested worked; keep `polars>=1.34,<2` only as a fallback)
   - pyarrow==26.0.0
   - numpy==2.5.4
   - scipy==1.18.1
   - scikit-learn==1.9.1
   - pydantic==2.14.1
   - pandas==3.0.6, only if needed
   - dev group: textual-dev==1.8.0

   Do NOT add cfbd. It forces pydantic<2 and cannot co-install with nflreadpy.

2. CFBD access: write a small async `cfbd_client.py` with one shared httpx.AsyncClient.
   - Base URL: https://api.collegefootballdata.com
   - Header: `Authorization: Bearer $CFBD_API_KEY`
   - Parse responses into your own pydantic v2 models, covering only the endpoints you need: /games, /lines, /scoreboard, /ratings/sp, /teams/fbs.
   - If you ever want the official SDK, keep it in a separate venv or subprocess.

3. Concurrency:
   - Run all network I/O (The Odds API, CFBD, scores) in `@work(exclusive=True, group=...)` async workers driven by `set_interval`.
   - Run nflreadpy and polars loads (blocking, requests-based) and sqlite writes in `@work(thread=True)`, and update the UI with `call_from_thread`.
   - Set the Odds API polling rate from the `x-requests-remaining` header, and poll only while games are live.

4. Textual specifics:
   - Set `AUTO_FOCUS = "#cmd"`.
   - Use `Select.NULL`, never `Select.BLANK`; in 8.x `Select.BLANK` is a bool that equals False.
   - Use `push_screen_wait` only inside a `@work` worker.
   - Use `App.notify` for toasts.
   - Put commands in a command-palette Provider.
   - Default theme: tokyo-night or catppuccin-mocha, plus a custom registered Theme.
   - In tests, wait at least 0.3s with `pilot.pause` after screen pushes.

5. Keep the validated grid layout:
   - PlotextPlot braille balance chart
   - Live-scores DataTable spanning two rows
   - Bets DataTable
   - RichLog event log
   - Input with SuggestFromList

   Keep the shlex+match command parser (deposit, withdraw, bet, confirm, cancel) and the sqlite WAL ledger with integer cents.

