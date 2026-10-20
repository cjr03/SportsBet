# Paper Football Betting TUI: Implementation Design Brief

*Last checked against live sources: 2026-10-20. Target: macOS, regular CPython 3.14.x (not 3.14t). All betting uses paper money.*

---

## 0. What "optimal strategy" means here

All the research reached the same conclusion: a ratings model on its own does not beat the closing line.
- **NFL:** our walk-forward backtest went 49.4–51.0% against the spread (ATS). The public nfelo model averages 54.16% ATS against the close, and it lost in 2024 (45.9%).
- **College football (FBS):** Fair & Oster (2007) found that no ranking system adds information beyond the closing Las Vegas spread.

So the strategy that grows the bankroll fastest is:
1. **Build a fair line.** Take the no-vig, sharp-weighted market consensus (Pinnacle plus the low-hold books novig and prophetx) and nudge it lightly toward our model.
2. **Bet only where a specific book's price beats that fair line** by an EV threshold. This captures line shopping, stale prices and early-week or opening lines.
3. **Size the bets.** Use fractional, push-aware, slate-level simultaneous Kelly, with hard caps.
4. **Measure success mainly by no-vig CLV.** P&L is too noisy: you need about 9,100 bets to confirm a 2% ROI at 2σ.

A realistic target is 1–3% ROI on selected plays. The app must show this honestly.

---

## 1. Architecture and modules

```
football/
  pyproject.toml
  football/
    app.py               # Textual App, layout, command parsing (shlex + match)
    config.py            # settings (env + sqlite settings table), plan presets
    db.py                # sqlite3 WAL, migrations, integer-cents ledger helpers
    clients/
      http.py            # shared httpx.AsyncClient factory, tenacity retry, aiolimiter
      odds_api.py        # The Odds API v4 client + quota header capture
      cfbd.py            # raw-httpx CFBD client + own pydantic v2 models (NO cfbd SDK)
      nflverse.py        # nflreadpy wrappers (thread workers) + parquet fallback
    ingest/
      scheduler.py       # quota-aware allocator; drives polling from free /events
      aliases.py         # team_aliases mapping, game matching (date, home, away)
    models/
      nfl.py             # ridge margin + EPA ratings (or Elo), HFA, sigma
      cfb.py             # prior R0 + ridge-to-prior + stacker
      distributions.py   # Normal + empirical key-number margin distributions
      blend.py           # logit blend fit (walk-forward), auto-disable gate
    strategy/
      odds.py            # american<->decimal, implied prob
      devig.py           # multiplicative/additive/power/shin (brentq)
      fair.py            # sharp consensus fair line
      ev.py              # binary + push-aware EV
      kelly.py           # single, push-aware, simultaneous slate optimizer, caps
      filters.py         # thresholds, odds bounds, one-market-per-game
    bets/
      lifecycle.py       # state machine PROPOSED->...->SETTLED
      settle.py          # grading (spread/total/ML, pushes, voids)
      clv.py             # close capture + CLV
    backtest/
      walkforward.py     # nflverse + CFBD /lines based backtests and fits
    ui/
      widgets.py, screens.py, palette.py (command Provider)
  tests/
```

**Concurrency** (Textual 8.2.8):
- **Network I/O** (Odds API, CFBD) runs in `@work(exclusive=True, group="odds"|"cfbd")` async workers triggered by `set_interval`.
- **Blocking work** (nflreadpy/polars loads, which use `requests`, plus sqlite writes and model fits) runs in `@work(thread=True)`. These workers update the UI through `call_from_thread`.
- **CFBD requests run one at a time.** Several requests at once can trigger a Cloudflare block of about 10 minutes.

---

## 2. Data sources

### 2.1 The Odds API v4 (live and actionable prices, close snapshots)

**Connection and limits**
- Host: `https://api.the-odds-api.com` (IPv6 alternative: `https://ipv6-api.the-odds-api.com`).
- Authentication: the `apiKey` query parameter.
- Sport keys: `americanfootball_nfl` and `americanfootball_ncaaf`, both with scores coverage. `americanfootball_ncaaf_fcs` has no scores, so skip it.
- There is no official Python SDK. Use `httpx` 0.28.1 with `tenacity` 9.2.1: back off on 429 and 5xx at 1, 2, then 4 s plus jitter, for at most 4 tries.
- Throttle on the client side with `aiolimiter` 1.3.0 at 5 requests/s. The documented limit is 30 requests/s, and only for paid plans; no free-tier figure is published.
- Read the `x-requests-remaining`, `x-requests-used` and `x-requests-last` headers on every response, including errors, and store them in the `quota` table.
- On 401 or an out-of-credits response, stop polling and show a banner.

**Endpoints**

| Endpoint | Use | Cost |
|---|---|---|
| `GET /v4/sports/` | sanity check, in-season list | free |
| `GET /v4/sports/{sport}/events?commenceTimeFrom&commenceTimeTo&dateFormat=iso` | **drives the scheduler**: kickoff windows, event IDs | free |
| `GET /v4/sports/{sport}/odds/?bookmakers=…&markets=h2h,spreads,totals&oddsFormat=decimal&dateFormat=iso[&eventIds=…&commenceTimeFrom/To]` | all pricing, in **one** call per sport | markets × regions = **3**. An empty response costs 0. |
| `GET /v4/sports/{sport}/scores/?daysFrom=1` | settlement **fallback only** | 1 without `daysFrom`, 2 with it. Costs credits even when the response is empty. |
| `GET /v4/sports/{sport}/events/{id}/odds` | props, alternate lines and periods | per event. **Not used on the free tier.** |
| `GET /v4/historical/...` | paid only, 10 × markets × regions | see the historical-data notes below |

**Bookmaker set:** 10 keys count as 1 region, and `bookmakers` overrides `regions`.
- **Free tier:** `bookmakers=pinnacle,draftkings,fanduel,betmgm,betrivers,espnbet,hardrockbet,fliff,novig,prophetx`.
- **Paid-only books:** williamhill_us, fanatics, courtside and rebet. Add them only on a paid key. Some strategy notes listed williamhill_us and fanatics without that restriction; that was wrong.
- **Pinnacle** is in the `eu` region and comes "from public website which may incur a delay". It may not have a line for every FBS game. novig and prophetx (region `us_ex`) are the second low-hold reference.

**Request format:** always `oddsFormat=decimal`. American odds can carry rounding discrepancies.

**Update intervals:** featured markets refresh every 60 s pre-match and every 40 s in-play, and the interval starts tightening 6 h before kickoff. Set the odds cache's TTL floor to 60 s pre-match and 40 s in-play. Scores update about every 30 s while live.

**Historical data:** treat it as **paid-only**. The historical-odds page says so, even though the homepage table conflicts.
- At startup, optionally make one probe call to `GET /v4/historical/sports/{sport}/events?date=…` (cost 1, or 0 if nothing is found) and record whether it is allowed.
- Snapshots go back to 2020-06-06 at 10-minute intervals and to September 2022 at 5-minute intervals.
- Backtests use nflverse and CFBD `/lines` instead.

**Plans**

| Plan | Price | Credits/month |
|---|---|---|
| Starter | free | 500 ("most" bookmakers) |
| 20K | $30 | 20,000 |
| 100K | $59 | 100,000 |
| 5M | $119 | 5,000,000 |
| 15M | $249 | 15,000,000 |

#### Quota-aware polling plan

The **events endpoint is free**, so the scheduler always knows the kickoff windows. Odds calls are spent only around decision points.

**Free preset (500 credits/month, 3 credits per sport call)**

| When | NFL | CFB |
|---|---|---|
| Opener snapshot (NFL Tuesday, CFB Sunday/Monday) | 3 | 3 |
| Midweek (Thursday for NFL, Wednesday for CFB) | 3 | 3 |
| Friday (CFB) | — | 3 |
| Close snapshots, T−10 to T−5 min per kickoff window: TNF, Sun early, Sun late, SNF, MNF (NFL); Sat noon, 3:30, 7:30, late (CFB) | 5 × 3 = 15 | 4 × 3 = 12 |
| Subtotal | 21 | 21 |

- Odds total about 42 credits per week. Settlement-fallback scores add about 10 calls with `daysFrom=1`, roughly 20 credits. The total is about 62 per week, or about 270 per month.
- That leaves headroom for manual `refresh` commands. The research draft's fuller preset was about 101 per week, or about 435 per month, which is about 13% slack.
- Keep a **10% reserve** (50 credits). When reserve pressure hits, degrade in this order:
  1. live scores
  2. midweek snapshots
  3. never the close snapshots, because CLV needs them
- Warn when remaining credits fall below 60.
- **No live scoreboard and no live betting in free mode.**
- Query only the sport whose window is closing. Use `commenceTimeFrom/To` so the call returns only that window's games; an empty result is free.

**Paid presets**
- **20K plan:** about 17,200 credits per month as designed, which is about 86% of the cap. Heavy weeks (MNF doubleheaders, December Saturday NFL games) can overrun it.
  - Fix: reduce odds polling within 30 minutes of kickoff to every 5 minutes, and let the allocator enforce the cap.
- **Live scores at 60 s** cost about 2,340 credits per week. At 30 s they cost about 4,700 per week, which only fits on 100K or higher.
- **100K plan ($59):** recommended if you want 60 s live polling plus frequent odds for both sports.
- **Live betting** is allowed only on paid plans, and only when the pricing snapshot is less than 60 s old.

### 2.2 CollegeFootballData (CFBD) API v2

**Connection**
- Base URL: `https://api.collegefootballdata.com`. The spec is v5.33.0 with 93 paths; `apinext.` serves the same spec.
- Header: `Authorization: Bearer $CFBD_API_KEY`. Never put the key in a URL. A request without it returns 401 `Did you forget to add "Bearer "...`.
- **Do not install the `cfbd` SDK.** cfbd 5.33.0 pins `pydantic<2`, nflreadpy needs `pydantic>=2`, and pip fails with `ResolutionImpossible`. Use a raw-httpx client with our own pydantic v2 models.

**Tiers**

| Tier | Price | Calls/month | Adds |
|---|---|---|---|
| Free | $0 | 1,000 | — |
| Academic | $0 | 3,000 | needs a .edu email |
| Tier 1 | $1 | 5,000 | `/scoreboard` (live), `/wepa/*`, `/games/weather`, `/games/{id}/preview/adjusted` |
| Tier 2 | $5 | 30,000 | `/live/plays` |
| Tier 3 to Tier 6 | $10 to $30 | 75k to 500k | GraphQL |

**Rate and usage**
- There is no per-request throttle, only monthly limits. Docs-playground calls count toward the limit.
- `GET /info` returns `remainingCalls`, `usedCalls`, `resetAt` and `tierName`. Show these in the status bar. The pool is shared with CBBD (basketball).

**Endpoints used**
- `/games?year&week&seasonType&classification`: results, `homePregameElo/awayPregameElo`, `neutralSite`, `completed` and points. Used for settlement and features.
- `/lines?year&week` → `BettingGame.lines[]: GameLine{provider, spread, formattedSpread, spreadOpen, overUnder, overUnderOpen, homeMoneyline, awayMoneyline}`.
  - A negative `spread` means the home team is favoured.
  - Data starts in 2013. Coverage varies by provider, and `spreadOpen` can be null.
- `/ppa/games?year&week&excludeGarbageTime=true`, `/ratings/sp?year`, `/ratings/fpi?year`, `/ratings/core?year`, `/ratings/srs/expanded` (FCS prior; no 2020).
- `/talent` (from 2015), `/player/returning` (from 2014), `/recruiting/teams`, `/player/portal` (from 2021), `/coaches` and `/coaches/tenures`, `/teams/fbs`, `/calendar`.

**Call plan** (about 230 calls per year; the free tier allows 1,000 per month)
- **Backfill:** about 12 calls per season × 10 seasons ≈ 120. Completed seasons are cached forever in `api_cache`, keyed by path and sorted params.
- **Weekly (Tuesday), 6 calls:** `/games`, `/ppa/games`, `/lines`, `/ratings/sp`, `/ratings/fpi`, `/ratings/core`.
  - Store the ratings as **point-in-time snapshots** in `ratings_snapshots` along with the pull date.
  - Drop `/ratings/elo`; pregame Elo already comes with `/games`.
- **Saturday and Sunday settlement:** about 2 to 4 `/games?year&week` calls.
- **Preseason:** `/talent`, `/player/returning`, `/recruiting/teams`, `/player/portal`, `/coaches`, `/ratings/srs/expanded`.

### 2.3 nflverse (via nflreadpy)

**Package**
- Use `nflreadpy==0.1.5`; `nfl_data_py` is deprecated and archived.
- It returns polars DataFrames and has 25 loaders. It works on 3.14.5 with polars 2.0.0. Its classifiers stop at 3.13, so 3.14 support is tested by us, not declared by the package.

**Loading**
- Always pass seasons explicitly: `load_schedules([2024, 2025, 2026])`. The default is **all seasons**.
- `load_pbp([season])` for EPA.

**Configuration:** set `NFLREADPY_CACHE=filesystem`, or call `update_config(cache_mode="filesystem", timeout=60)`.

**Fallback:** if nflreadpy fails, download the release files directly, checking `timestamp.json` for freshness:
- `https://github.com/nflverse/nflverse-data/releases/download/schedules/games.parquet`
- `.../pbp/play_by_play_{season}.parquet`

**Conventions**
- `spread_line > 0` means the home team is favoured.
- `result` = home score − away score.
- The odds source and whether lines are opening or closing are **undocumented**. Treat them as an approximate closing consensus.

**Training and backtests only:** only about 1 to 2 weeks of upcoming games carry lines (25 of 179 unplayed 2026 games). All actionable prices come from The Odds API.

### 2.4 Identity mapping and line convention

**Matching games across sources**
- The `team_aliases` table maps Odds API names, CFBD names and nflverse abbreviations to one canonical team.
- Games are matched on (local date, home, away), with a check that the neutral-site flag agrees.

**Line convention**
- Store every line as `home_line`, with negative meaning the home team is favoured. The Odds API's spread `point` and CFBD's `spread` already use this.
- nflverse is the exception: `home_line = -spread_line`.

---

## 3. NFL model

**Ratings, in this order of preference**

1. **Ridge margin ratings plus EPA ratings**
   - Recency weight = 0.5^(age_days/120).
   - λ ≈ 8.
   - Learned HFA of about 1.6 to 2.0 points (2021–25: actual +2.06, mean spread 1.63), set to 0 at neutral sites.
2. **Elo alternative**
   - K = 20.
   - Revert 1/3 toward 1505 each offseason.
   - Points ≈ Elo difference / 25.
   - HFA ≈ 45 Elo for the modern era, not 538's 65.
   - Margin-of-victory multiplier: `ln(max(PD,1)+1) × 2.2/(winner_elo_diff×0.001 + 2.2)`, where the Elo difference includes HFA.

**Distributions**
- **Spread:** P(home covers L) = 1 − Φ((L' − M)/13.0) as the baseline, where L' is the line expressed as points the home team must win by.
  - Preferred: an **empirical (result − spread) distribution bucketed by spread**, so key-number mass survives.
  - Margins of 3 occur 14.6% of the time, 7 occur 8.8%, 6 occur 7.0%, 14 occur 5.1% and 10 occur 4.9%. Ties are 0.4% and ATS pushes are 2.6%.
- **Moneyline:** P(home win) = Φ(M/11.16), the maximum-likelihood σ for 2010–25.
  - A normal with SD 13.5 understates favourites from about 6 points up. Actual favourite win rates vs Φ(L/13.5): at 7, 75.5% vs 69.8%; at 10, 83.3% vs 77.1%.
- **Totals:** σ ≈ 13.1.
- **Recent SD(result − spread):** 12.64 for 2021–25 and 11.82 for 2026 so far. Refit σ every season.

**Market blend:** see §5.3. Model weight against the sharp close is about 0.10 to 0.15, and about 0.35 against openers (nfelo: 56.97% ATS against the open vs 54.16% against the close).

---

## 4. CFB model (FBS only; skip FCS-vs-FCS)

**Step 1. Preseason prior R0**, in points relative to an average FBS team:

`R0 = b0 + b1·P + b2·P·(RP−0.5) + b3·hist + b4·talent_z + b5·newcoach·P`

- P = prior-year final rating (our own, plus prior-year SP+).
- RP = `percentPPA` from returning production; `percentPassingPPA` serves as a QB proxy. FPI values a returning starting QB at about 3.3 points per game, and a transfer QB at half that.
- hist = mean rating over the last 2 to 4 years (tune the window).
- talent_z = z-score of `/talent`.
- newcoach = coaching-change flag from `/coaches/tenures`.
- Fit on 2016–2025. FCS opponents get their prior from `/ratings/srs/expanded`; for 2020 use 2019 or the FCS mean.

**Step 2. In-season ratings by ridge shrinkage toward R0**
- Model: `margin_home = HFA·(1−neutral) + R_home − R_away`, with margins capped at ±28 and recency weight 0.97^weeks_ago.
- Tune λ by week of season.
- Efficiency version: the same fit on `/ppa/games` with garbage time excluded and FBS-only games, converted to points at about 65 to 72 plays per game.
- `R_model = a·R_pts + (1−a)·R_epa`, with a ≈ 0.3 to 0.4.
- HFA is fitted from data, not hardcoded at 3.

**Step 3. Stacker, using point-in-time features only**
- Features:
  - R_model differential
  - `/games` pregame Elo differential
  - prior-year SP+ and FPI differentials
  - **our own** weekly snapshots of SP+, FPI and CORE from launch onward
- **Leakage rules:**
  - Never use season-level pulls of SP+, FPI or CORE in a backtest. They are end-of-season and retrospective.
  - Never use `/metrics/wp/pregame` as a feature. It is derived from the market spread.

**Step 4. Probabilities**
- P(home win) = Φ(pred/σ).
- P(home covers) = Φ((pred + home_line)/σ).
- σ is fitted from residuals, starting at about 15.5 to 16, and varies by week and spread size.
- Adjust for key numbers using the empirical `/games` margin distribution.
- Compute our own benchmark: the MAE of the CFBD closing `spread` against final margins for 2013–2025. The 12.6 figure in circulation is unsourced.

**Extra filter for CFB:** require |final − line| ≥ 2.5 to 3 points as well as the EV threshold. Evaluate early weeks, G5 games and totals separately.

---

## 5. Probability-to-bet pipeline

### 5.1 Odds conversion
- American to decimal: `d = 1 + A/100` if A > 0, else `d = 1 + 100/|A|`.
- Implied probability: `r = 1/d`.
- At −110/−110: d = 1.9091, r = 0.5238, overround 4.76%, hold 4.55%, break-even 52.38%. At −105 break-even is 51.22%; at −104 it is 50.98%.

### 5.2 Devig (per book, per market)
Let R = Σr_i.

| Method | Formula |
|---|---|
| Multiplicative | `p_i = r_i/R`. The least accurate method. |
| Additive | `p_i = r_i − (R−1)/n`. Equal to Shin for two outcomes. |
| Power (the default for moneylines) | `p_i = r_i^k`, with k solved by `scipy.optimize.brentq` so that Σp = 1 |
| Shin | `p_i = [√(z² + 4(1−z)·r_i²/R) − z] / (2(1−z))`, with z solved numerically |

- For near-symmetric −110/−110 spreads and totals, every method gives about 0.5/0.5. A/B test the method choice for moneylines on our own closing-line calibration.
- Implement it in-house with NumPy/SciPy. Use `penaltyblog==1.13.1`, which has cp314 arm64 wheels, as a test oracle. **Do not use `shin` 0.2.2**: it has no cp314 macOS wheel and needs Rust to build.

### 5.3 Fair line and blend
- **Market fair probability:** the no-vig Pinnacle line. If Pinnacle is missing or its snapshot is stale, use the mean of the no-vig novig and prophetx lines. Failing that, use the median no-vig probability across all books.
- **Blend**, fitted walk-forward:
  - `logit p_final = a + b1·logit p_model + b2·logit p_market`.
  - Fit it on nflverse `load_schedules` (moneyline, spread, total and odds columns) and on CFBD `/lines`.
- **Weights before fitting:** model weight about 0.25 for NFL and 0.35 for FBS against opening or early-week lines. Within about 24 h of kickoff, use about 0.10 to 0.15 against the sharp line. The CFB research expects the fitted α in the 0.1 to 0.4 range.
- **Auto-disable gate:** if the fitted b1 ≤ 0 or is not significant for a league and market, that league and market becomes **observe-only**. Proposals are still logged for CLV tracking, but no stakes are placed.
- **Pitfall to avoid:** do not blend heavily with a number and then bet against that same number. With w_market = 0.85, the model needs to disagree by 6 to 8 points before a bet triggers, and our backtest shows such large disagreements are mostly model error. The edge comes from a **specific book's price against the fair line**.

### 5.4 EV and edge
- **Bet price:** `d_best = max d_i` across the books the user has enabled. Record which book gave it.
- **Binary bets:** `EV = p·d − 1`.
- **Integer spreads and totals (push-aware):** with b = d − 1, `EV = p_w·b − p_l`. Use a 0.5-point continuity correction for normal distributions, or take push probability directly from the empirical distribution.

**Filters (defaults)**

| Filter | Default |
|---|---|
| EV at d_best, moneyline | ≥ 2% |
| EV at d_best, spreads and totals | ≥ 2–3% (start at 2.5%) |
| Probability edge over break-even | ≥ 1.5 pp |
| Moneyline decimal odds | between 1.30 and 5.0 |
| CFB disagreement | \|final − line\| ≥ 2.5 |
| Markets per game | one (unless margin and total are simulated jointly) |
| Parlays | off (see below) |

**Parlays** are disabled because the house edge compounds. A 2-leg −110 parlay pays 3.645 (+264) against a fair +300, for an EV of −8.9% versus −4.55% for a single bet.

### 5.5 Sizing

**Formulas**
- Binary bet: `f* = (p·d − 1)/(d − 1)`.
- Push-aware bet: `f* = (p_w·b − p_l)/(b·(p_w + p_l))`.

**Fractional Kelly**
- The default is λ = 0.25, adjustable from 0.1 to 0.5.
- Growth at fraction λ is (2λ − λ²)·G_max, so half-Kelly keeps 75% of the maximum growth.
- The chance of the bankroll ever falling to fraction x is x^(2/λ − 1). For a 50% drawdown that is 50% at full Kelly and 12.5% at half Kelly.
- If the edge is overestimated by 1.5×, you are effectively betting 1.5× Kelly and keep only 75% of the growth. This is why the default is a quarter.

**Simultaneous slate sizing:** maximize E[log W] subject to f ≥ 0, the per-bet and per-game caps, and Σf ≤ the slate cap.
- Use exact enumeration up to N ≈ 14 for binary bets (2^N scenarios), or N ≈ 9 when pushes are included (3^N).
- Above that, use Monte Carlo with about 20k scenarios and SLSQP.
- If the optimizer fails, fall back to proportional scale-down: `f_i ← f_i·cap/Σf`.
- penaltyblog's `multiple_kelly_criterion` handles mutually exclusive outcomes of one event, not independent games, so do not use it for slates.
- Tepelyan & Lam (arXiv 2604.24723) give an O(N) objective for future work.

**Caps (defaults)**
- 2% per bet. The NFL research uses 2%; the strategy research allows up to 3%, so the user decides.
- 4% per game and 20% per slate.
- Minimum stake $1.
- Size a slate from the bankroll at slate start, net of open exposure.
- Optionally halve the stake for bets placed more than 48 h before kickoff.

---

## 6. Bet lifecycle

```
PROPOSED --confirm--> CONFIRMED --price check ok--> PLACED --final score--> SETTLED{WON|LOST|PUSH}
   |                     |                            |
   +--reject/expire--> REJECTED/EXPIRED   price moved--+--> REPRICED (back to PROPOSED)   +--> VOID (postponed/cancelled/no result)
```

1. **PROPOSED.** The scanner writes `proposals` rows that freeze the inputs: snapshot ID, book, line, decimal price, p_model, p_market, p_final, EV, the Kelly fraction and the capped stake.
   - A proposal expires at kickoff, or when a newer snapshot moves the price below the threshold.
2. **CONFIRMED.** The user runs `confirm <id> [stake]`. Auto-confirm is a setting and is **off by default**.
3. **PLACED.** The stake is debited in integer cents through an append-only `ledger` row, and the price is locked. **Paper-fill honesty rule:**
   - If the pricing snapshot is older than the staleness limit (10 minutes pre-match by default; under 60 s for live on paid plans), the app either re-fetches the odds (3 credits) or marks the bet `price_unverified`.
   - `price_unverified` bets are excluded from headline ROI. Without this rule, stale paper fills inflate profit.
4. **Close capture.** The last pre-kickoff close snapshot provides the no-vig closing probability:
   - `CLV% = d_taken·p_close_fair − 1`.
   - Also record the line movement in points.
5. **SETTLED.**
   - Sources, in order: CFBD `/games` (`completed`, points) for CFB, then nflverse `load_schedules` (`result`, `total`) for NFL, then Odds API `scores?daysFrom=1` (2 credits) as a fallback, then manual `settle <id> <home> <away>`.
   - Grading:
     - Spread: compare home_margin + home_line against 0 (greater wins, equal pushes, less loses).
     - Totals: compare the combined score with the line.
     - Moneyline: winner; a tie counts as a push.
     - Overtime counts.
   - Payout = stake·d on a win and the stake back on a push.
6. **VOID.** No result within 72 h of the scheduled kickoff, or postponement past the market rules, refunds the stake. The 72 h window is a default the user can change.

---

## 7. TUI (Textual 8.2.8)

**Layout:** a 3 × 3 grid, columns `2fr 3fr 2fr`, rows `1fr 1fr auto`.
- **Left top:** a `PlotextPlot` braille bankroll chart from textual-plotext. A `Sparkline` is a fallback and can be any height since 7.3.0.
- **Centre, spanning both rows:** a board `DataTable`. Columns: games, kickoff, fair line, best book and price, model line, EV, proposed stake. Free mode shows no live scores; paid mode adds live scores.
- **Right top:** a bets `DataTable` with ID, state, pick, price, stake, CLV and P/L.
- **Left bottom:** KPI panel: bankroll, ROI ± 2·0.955/√N, mean CLV and % positive, log-growth, maximum drawdown, Brier score and log loss.
- **Right bottom:** a `RichLog` event log.
- **Bottom row:** an `Input#cmd` with `SuggestFromList`, plus a status bar showing Odds API credits remaining, CFBD `remainingCalls` and `resetAt`, plan mode, and last snapshot age.

**Textual specifics**
- `AUTO_FOCUS = "#cmd"`; the default `"*"` grabs the first focusable widget.
- Use `Select.NULL`. In 8.x, `Select.BLANK` is an inherited bool that silently equals False.
- `push_screen_wait` works only inside `@work`.
- Use `App.notify` for toasts.
- Commands also go into a command-palette `Provider`.
- Theme: tokyo-night or catppuccin-mocha.
- In tests, `pilot.pause` for at least 0.3 s after screen pushes; there is a 50 ms delay when switching screens.

**Commands** (parsed with shlex and `match`)

| Command | Action |
|---|---|
| `deposit <amt>` / `withdraw <amt>` | ledger adjustments |
| `refresh [nfl\|cfb] [--force]` | odds pull; shows the credit cost first |
| `scan [nfl\|cfb]` | recompute proposals from the latest snapshots |
| `picks` | list proposals |
| `confirm <id> [stake]` / `reject <id>` / `cancel <id>` | lifecycle actions; `cancel` works only before PLACED |
| `bets [open\|settled]`, `settle [id home away]` | bet listing and settlement |
| `clv`, `report [season]`, `calib` | KPIs and reliability diagram |
| `quota` | credit status for both APIs |
| `set kelly 0.25`, `set cap bet 2`, `set ev 2.5`, `books …`, `mode free\|paid`, `autoconfirm on\|off` | settings |
| `backtest nfl 2017-2025`, `backtest cfb 2016-2025`, `fit blend` | backtests and blend fitting |
| `help` | command help |

---

## 8. Persistence (sqlite3, WAL, integer cents)

| Table | Contents |
|---|---|
| `settings` | key and value |
| `quota` | api, ts, remaining, used, last |
| `api_cache` | path, params_hash, fetched_at, body, immutable flag |
| `team_aliases` | source, name, canonical_id |
| `games` | id, league, season, week, kickoff_utc, home, away, neutral, odds_event_id, cfbd_id, nflverse_id, status, home_pts, away_pts |
| `odds_snapshots` | **append-only**: snapshot_id, ts, game_id, book, market, side, point, decimal_price, last_update |
| `ratings_snapshots` | source, team, season, week, pulled_at, values_json |
| `predictions` | game_id, ts, model_version, mu, sigma, p_model, p_market, p_final |
| `proposals` | the frozen inputs from §6 |
| `bets` | id, proposal_id, state, book, market, side, line, decimal_price, stake_cents, placed_at, price_unverified, close_snapshot_id, clv, result, payout_cents, settled_at |
| `ledger` | **append-only**: id, ts, kind (deposit, withdraw, stake, payout, refund), amount_cents, bet_id |

- Bankroll = SUM(ledger.amount_cents).
- Settlement and ledger writes happen in one transaction.

---

## 9. Pinned dependencies (`pyproject.toml`)

```toml
requires-python = ">=3.12,<3.15"   # regular CPython 3.14; NOT 3.14t (polars-runtime-32 2.0.0 ships abi3 only)
dependencies = [
  "textual==8.2.8", "rich==15.0.0",
  "textual-plotext==1.0.1", "plotext==5.3.2",   # textual-plotext requires plotext<6
  "httpx==0.28.1", "tenacity==9.2.1", "aiolimiter==1.3.0",
  "nflreadpy==0.1.5", "polars==2.0.0", "pyarrow==26.0.0",
  "numpy==2.5.4", "scipy==1.18.1", "scikit-learn==1.9.1",
  "pydantic==2.14.1",
]
[dependency-groups]
dev = ["textual-dev==1.8.0", "pytest", "penaltyblog==1.13.1"]  # penaltyblog = devig/Kelly test oracle
# optional: pandas==3.0.6 only if needed
# DO NOT add: cfbd (pydantic<2 conflict), shin (no cp314 macOS wheel)
```

If polars 2.0 breaks an nflreadpy loader, fall back to `polars>=1.34,<2`. Four loaders have been verified working on 2.0.0.

---

## 10. Risks and limitations

1. **The edge is thin.** Expect about 50 to 51% ATS from the model alone against the close; nfelo averages 54% with losing years. Profit comes mostly from line shopping and early prices.
2. **Results are statistically weak.** SE(ROI) = 0.955/√N, about 3% at 1,000 bets. A 2% edge needs about 9,100 bets and a 5% edge about 1,460. Results under about 1,500 bets are labelled inconclusive, and CLV is the main KPI.
3. **Paper fills are optimistic.** There are no limits, no rejected bets and no line moves while placing. The staleness rule and the `price_unverified` flag reduce but do not remove this. Pinnacle's feed can be delayed.
4. **Credits are scarce on the free plan.** That rules out live scores and live betting, and the snapshots are coarse, which inflates stale-price "edges".
5. **Historical lines are of uncertain quality.** nflverse lines have an undocumented source and timing. CFBD `/lines` varies by provider and `spreadOpen` can be null. Backtested CLV is therefore approximate.
6. **CFB data leaks easily.** SP+, FPI and CORE are only point-in-time from the app's own snapshots, so the CFB stacker's history starts at launch.
7. **Package risk.** nflreadpy has had no release in about 11 months and declares support only up to 3.13. httpx's last release was December 2024. Python 3.14 support was tested locally only.
8. **Mismatched games across sources** (neutral sites, renamed teams) can mis-settle bets. Mitigations: the aliases table, matching on date, home and away, and a manual `settle` override.
9. **Unverified research claims** (indicative only): Benter's pseudo-R² numbers, the favourite-longshot evidence, and the 12.6 Vegas-line MAE.
10. **Legal note:** the app only simulates bets and must never place real wagers. Book availability varies by state.

---

## 11. Open decisions for the user

1. **Odds API plan:**
   - Free (500 credits; no live scores or live betting; coarse snapshots)
   - 20K at $30 (near the cap; needs throttling)
   - 100K at $59 (60 s live polling plus frequent odds for both sports)
2. **CFBD tier:** Free, 1,000 calls (enough), or Tier 1 at $1 for the live `/scoreboard`, WEPA and weather.
3. **Books to shop:** which sportsbooks to treat as bettable. This should match books available in your state; otherwise the line-shopping edge is fictional.
4. **Starting bankroll** and whether deposits and withdrawals are allowed mid-season.
5. **Risk settings:** Kelly fraction (default 0.25, range 0.1 to 0.5); per-bet cap of 2% or 3%; 4% per game and 20% per slate.
6. **EV threshold:** 2% vs 2.5–3% for spreads and totals. A lower threshold means more bets and more noise.
7. **Markets:** spreads, totals or moneyline (all three by default; props off). Should totals start as observe-only until their blend fit passes the gate?
8. **Confirmation mode:** manual confirm (the default) or auto-confirm.
9. **Stale fills:** re-fetch odds on confirm (costs credits) or accept the bet as `price_unverified`.
10. **CFB scope:** all FBS games, or only games with a Pinnacle or low-hold reference line; include G5 conferences and early weeks?
11. **Parlays and live betting:** keep both off (recommended), or enable live betting on a paid plan only.
12. **Go-live gate:** require a passing walk-forward backtest (b1 > 0 and significant) before a league or market can place stakes, or allow paper betting immediately.
13. **Display time zone** and whether the scheduler should run in the background when the TUI is closed.