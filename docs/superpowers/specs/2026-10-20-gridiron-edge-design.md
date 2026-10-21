# Gridiron Edge — Design

*2026-10-20. Terminal app that finds +EV football bets on Kalshi and PrizePicks, texts them to the user, and tracks a paper bankroll.*
Research backing this design (fact-checked, with sources): [`research/`](research/). Files: `brief1.md`/`round1.md` (models, Odds API, CFBD, TUI), `round2.md` (PrizePicks, arbitrage, SMS), `therundown.md`, `kalshi.md`.

---

## 1. Intent

The user wants to **maximize long-run profit**. The picks should be good enough to place for real.

The app **never places bets**. It does four things:
- finds opportunities
- sizes them
- **texts the user** what to place
- records the result once the user confirms they placed it

The bankroll is a notional paper pool. The user adds to it with `deposit` and takes from it with `withdraw`.

**Where bets can be placed.** The user is in California and can only use **Kalshi** and **PrizePicks**, where they see fixed Power/Flex payouts. Sportsbooks are a **pricing reference only**: they tell us the fair price and are never a bet target. The venue list is a setting, so a book can be added later without redesign.

**What "optimal" means here**, per the research:
- A ratings model on its own does not beat the closing line. Public NFL models land around 50–54% ATS against the close, with losing seasons. In college football, no ranking system adds information beyond the closing spread.
- So the edge comes from **pricing**:
  - Build a no-vig fair probability from the sharpest books (Pinnacle and the low-hold exchanges).
  - Nudge that probability toward our model.
  - Bet only where a Kalshi ask (after fees) or a PrizePicks line beats fair value by a threshold.
- Size bets with fractional, simultaneous Kelly.
- Judge success mainly by **closing-line value (CLV)**. P&L is noise for the first ~1,500 or more bets.

## 2. Goals, non-goals, anti-goals

**Goals**
1. Find and rank four kinds of opportunity, all sized from one bankroll:
   - **Kalshi game contracts.** NFL and FBS moneyline (`KXNFLGAME`/`KXNCAAFGAME`), spread (`…SPREAD`), and total (`…TOTAL`).
   - **Kalshi player-prop contracts.** NFL prop series such as `KXNFLRECYDS` and `KXNFLPASSATT`, discovered from the series list.
   - **PrizePicks entries.** 2–6 player-prop picks, Power or Flex. Picks are chosen by comparing PrizePicks lines to no-vig sportsbook prop odds.
   - **Arbitrage on the bettable venues.** Inside Kalshi: the two moneyline asks summing to less than $1 net of fees, and violations in spread or total ladders. Every result is labelled exactly what it is.
2. Text the user over iMessage for each actionable recommendation. The text names the exact contract or entry, the price, the size, the EV, and when the line was pulled.
3. A beautiful, dynamic TUI with:
   - a balance chart
   - an event window (recommendations, texts, deposits/withdrawals, confirmations, settlements, API usage)
   - an open/settled bets table
   - live scores
   - a command bar
4. A **confirm-placed flow.** A recommendation stays *pending* until the user confirms they placed it, optionally at their actual fill price and size. Only confirmed bets move money and count toward results.
5. Results update in near real time within free-tier limits. Live quota meters are always visible.

**Non-goals**
- Placing bets automatically, or logging into Kalshi or PrizePicks. Kalshi is read only through its public market-data API.
- Scraping PrizePicks. Its endpoints return 403/429 and its ToS forbids it. PrizePicks lines come from The Odds API `us_dfs` region instead.
- Running in the background. The app works **only while the TUI is open**. On launch it catches up: it settles finished games and marks any CLV gaps.
- Sportsbook parlays. PrizePicks multi-pick entries are the exception, because that is the format the user asked for.
- In-game live betting.
- Sports other than NFL and FBS.

**Anti-goals** (failures even if everything "works")
- **Inflated paper profit.** Stale prices never count as fills, and Kalshi fees and top-of-book depth are always applied. Headline ROI shows a ±2σ band, and results under ~1,500 bets are labelled "inconclusive".
- **Burning a month of API credits in a day.** A hard budget guard stops this.
- **Spamming the phone.** Texts are deduplicated, with a cooldown and quiet hours. Arbitrage alerts are exempt.
- **Calling something "arbitrage" when it isn't risk-free.** PrizePicks entries are "edge". Kalshi NFL moneyline pairs carry a tie flag only when the market rules lack the 50/50 tie split. Ladder middles are labelled "middle" (not risk-free), never arbitrage.

## 3. Constraints

**Runtime.** macOS, CPython 3.14, a local single-user app that runs only while open. This Mac is signed into Messages.

**Kalshi public API.**
- Base `https://api.elections.kalshi.com/trade-api/v2`. The base URL is configurable because `external-api.kalshi.com` is the newer documented host.
- No authentication is needed for markets, events, series, or orderbooks.
- Limits are about 20 requests per second sustained. We stay at 2 per second or less.
- Prices are strings in `*_dollars`, and sizes are strings in `*_fp`.

**The Odds API (free).** 500 credits a month.

| Call | Cost |
|---|---|
| `/sports`, `/events` | free |
| `/odds` | markets × regions (one call per sport covers the whole slate) |
| Per-event props | unique markets returned × regions |
| Empty responses | 0 |

There is no historical data on the free plan.

**CollegeFootballData (free).** 1,000 calls a month with Bearer auth. `/info` reports the remaining calls.

**TheRundown (free).**
- 20,000 data points per UTC day and 200,000 per month.
- 1 request per second.
- Data is delayed about 5 minutes.
- Only DraftKings, FanDuel, and BetMGM odds, pre-match only.
- No props, history, or websocket.
- Polling scores alone costs about 2 points per game.

**nflverse.** Free GitHub release files, read with `nflreadpy`.

**Jurisdiction.**
- California has no legal sportsbooks.
- The state's 2025 Attorney General opinion treats DFS as illegal.
- Kalshi's sports contracts in California face ongoing litigation, including 2026 Ninth Circuit rulings on tribal-lands claims.

The app is a simulator. Its texts carry a short "verify availability/payout before placing" note.

## 4. Architecture

```
gridiron_edge/
  app.py              Textual App: layout, command parsing, screens
  config.py           .env (keys, phone) + settings table (venues, thresholds, caps)
  db.py               sqlite (WAL), integer cents, append-only ledger
  clients/            kalshi.py, odds_api.py, therundown.py, cfbd.py (raw httpx, no cfbd SDK), nflverse.py
  budget.py           credit/call/data-point guard for every metered API
  scheduler.py        what to fetch when, driven by free event lists + kickoff windows
  matching.py         team aliases; maps Kalshi tickers / Odds API / TheRundown / CFBD / nflverse to one game id
  models/             nfl.py, cfb.py, props.py, distributions.py, blend.py
  strategy/           odds.py, devig.py, fair.py, kalshi_pricing.py (fees, depth), ev.py, kelly.py,
                      prizepicks.py, arb.py
  bets/               lifecycle.py, settle.py, clv.py
  notify/             imessage.py (osascript), ntfy.py, console.py, chain.py
  ui/                 widgets (chart, board, bets, scores, event log, quota bar), confirm modal, theme
tests/
```

**Concurrency.**
- Network fetches run in async Textual workers.
- Blocking work (polars loads, model fits, sqlite) runs in thread workers that update the UI with `call_from_thread`.
- CFBD calls run one at a time.
- TheRundown calls are gated at 1.1 seconds apart.
- Kalshi calls are limited to 2 per second.

## 5. Data sources and fetch plan

| Source | Job |
|---|---|
| **Kalshi** (free, unmetered) | Bettable prices and depth for game and prop contracts. Also Kalshi settlement results. |
| **The Odds API** (500/month) | Sharp fair-value inputs (Pinnacle, novig, prophetx, major books), PrizePicks lines, and sportsbook prop odds. Spent only at decision points. |
| **TheRundown** (free) | Live scores and settlement. Frequent DraftKings/FanDuel/BetMGM line snapshots used to detect line moves. |
| **CFBD** | CFB ratings, model inputs, results, and player box scores. |
| **nflverse** | NFL training data, results, player stats, and prop settlement. |

### 5.1 Kalshi

**Discovery** (on launch, then hourly):
- Call `GET /markets?series_ticker=<S>&status=open&limit=100` for each football series and follow the cursor.
- Prop series are found once a week via `GET /series?category=Sports`. That response is over 6 MB, so it is cached.

**Normalizing** each market to `(game_id, market_type, team/side, line, yes_bid, yes_ask, no_bid, no_ask, sizes)`:
- Teams come from the event `title` and `yes_sub_title`.
- The line comes from `floor_strike`, never from the ticker suffix.
- Kickoff comes from the ticker date plus the matched game in the other feeds.

**Pricing:**
- Use the ask we would actually pay. Never use `last_price`.
- Fetch `GET /markets/{ticker}/orderbook` only for candidates. A Kalshi orderbook lists bids only, so the ask on one side is 1 minus the best bid on the other.
- Read `fee_type`/`fee_multiplier` per series from `GET /series/{ticker}`.

**Refresh:** every 2–5 minutes for games within 24 hours of kickoff, every 30 minutes otherwise. This costs nothing beyond the rate limit.

**Settlement:** the market's `result` (`yes`/`no`) is the ground truth for Kalshi bets.

### 5.2 TheRundown: live scores and line watch

**Scores:**
- `GET https://therundown.io/api/v2/sports/{2=NFL|1=NCAAF}/events/{date}?affiliate_ids=0` with the header `X-TheRundown-Key`.
- Every 60 seconds while games are live, every 15 minutes otherwise.
- The UI shows a "~5m delayed" badge.

**Line watch:**
- `market_ids=1,2,3&affiliate_ids=19,23,22,25&main_line=true` every 15–30 minutes before games. Affiliate 25 is Kalshi, used as a cross-check.
- We diff the snapshots ourselves.
- A move past a threshold triggers a re-scan. If the budget allows, it also triggers a targeted Odds API refresh of sharp prices.
- The price `0.0001` means off-board.

**Budget:** pace usage against the 200,000-point monthly cap. Log the `X-Datapoints-*`, `X-Tier`, `X-Data-Delay-Seconds`, and `X-Bookmakers` headers.

**First run:** confirm the tier, the books, and that odds still flow after the 7-day evaluation window. If odds stop, TheRundown becomes a scores-only source.

### 5.3 The Odds API: fair-value inputs and PrizePicks lines

**Request settings:**
- `oddsFormat=decimal`.
- Use a `bookmakers=` list of at most 10 books, which bills as one region: `pinnacle,novig,prophetx,draftkings,fanduel,betmgm,betrivers,espnbet,hardrockbet,kalshi`.
- Read the `x-requests-*` headers on every response.
- Back off on HTTP 429.

**Monthly budget (~500).** The guard enforces caps and keeps a 10% reserve.

| Use | Call | Credits/month |
|---|---|---|
| Sharp game-line snapshots for NFL and CFB, taken early-week, midweek, and at each kickoff window's close (the close also drives CLV) | `/odds` with h2h, spreads, totals (3 credits per sport call) | ~200 |
| PrizePicks plus sportsbook props for about 5–8 chosen games a week, pulled once within ~2 hours of kickoff and never refreshed. These also price the Kalshi prop contracts in the same games. | `/events/{id}/odds` with books `prizepicks` and the `us` books, 3 markets, `includeMultipliers=true` | ~220 |
| Reserve, plus refreshes triggered by line moves or by the user | — | ~80 |

**Choosing prop games.** NFL games whose props are listed on Kalshi come first, then primetime NFL, then the CFB games with the highest totals. Probe first; an empty response costs nothing.

**First-run check.** Confirm that `prizepicks`, `pinnacle`, and `kalshi` come back on the free key. If PrizePicks lines are missing, PrizePicks mode falls back to **manual line entry** (`pp add "Player" stat line`).

### 5.4 CFBD and nflverse

**CFBD:**
- `/games` provides results, pregame Elo, and the completed flag.
- `/lines`, `/ppa/games`, `/ratings/sp|fpi`, `/talent`, and `/player/returning` feed the model.
- `/games/players` and `/player/usage` feed the props model and prop settlement.
- Completed seasons are cached forever. Expect about 6 calls a week plus 2–4 per settlement day.

**nflverse:** schedules (results and historical lines), play-by-play EPA, and weekly player stats. Player stats drive the prop projections and prop settlement. Parquet files serve as the fallback.

## 6. Models and the decision pipeline

### 6.1 Fair probability (shared by everything)

**1. Market fair price:**
- Use no-vig Pinnacle.
- If that's missing, use the mean of no-vig novig/prophetx.
- If those are missing, use the median of no-vig across all books.
- Devig method:
  - moneylines: power
  - spreads and totals: additive, which equals Shin for two-way markets

**2. Converting to Kalshi's lines.** A Kalshi spread reads "team wins by over X" and a total reads "over X". Fair probability at any X comes from a margin or total distribution centred on the sharp fair spread or total:
- NFL spreads: an empirical distribution that keeps the key numbers 3/7/6/10/14.
- NFL totals: normal with σ≈13.
- CFB: σ about 15.5–16.

**3. Our model:**
- **NFL:**
  - ridge margin ratings plus EPA ratings, recency-weighted
  - home-field advantage learned from data, about 1.6–2.0 points
- **CFB:**
  - preseason prior built from last season's rating, returning production, talent, and coaching change
  - in-season ridge shrinkage toward that prior
  - a stacker that adds pregame Elo and point-in-time SP+/FPI snapshots
- **Blend** in logit space:
  - model weight starts at about 0.25 (NFL) and 0.35 (CFB) against early-week lines
  - it drops to about 0.10–0.15 near kickoff
  - weights are refit walk-forward
- **Auto-gate.** A league or market becomes **observe-only** if its backtested model coefficient is ≤0 or not significant. Observe-only means it is still logged for CLV but never texted.

**4. Props:**
- Devig sportsbook prop odds at the same player and stat.
- Shift between lines with a Normal (yards) or Poisson/NegBin (counts) distribution.
- Use the worst case across devig methods.
- Fallback when no sportsbook line exists: our own projection from nflverse/CFBD stats. This needs a larger margin.

### 6.2 Kalshi contracts (game and prop)

**Cost of a YES or NO contract at ask P:**
- The effective cost is P_eff = P + taker fee per contract, where the fee is ceil-to-cent(0.07·C·P·(1−P)) / C, read from the series fee settings.
- The payout is $1, so the decimal-odds equivalent is d = 1/P_eff.

**Bet when all of these hold:**

| Check | Threshold |
|---|---|
| EV = p_fair/P_eff − 1 | ≥ 2.5% |
| p_fair − P_eff | ≥ 1.5 pp |
| P | between 0.10 and 0.90 |
| top-of-book size | covers the stake |
| market status | `active` |
| CFB only: model and line disagree by | ≥ 2.5 points |
| markets per game | one, unless props on different players |

**Flags:**
- NFL ties: Kalshi rules (`rules_primary`) settle a tie at $0.50 per side, so YES+YES and NO+NO moneyline pairs both pay $1; without that clause a YES+YES pair can lose on a tie.
- CFB cancelled games: a "fair price" resolution.

**Optional maker mode, off by default.** Suggest a resting limit price at the lower 0.0175 maker fee. It is shown as "may not fill".

### 6.3 PrizePicks entries (fixed Power/Flex payouts)

**Qualifying picks:** at least breakeven + 2 pp when the probability is market-anchored, and breakeven + 3.5 pp when it comes from our own model. Breakeven for 5- and 6-pick Flex is about 54.2%.

**Allowed structures:** 3-Power, 4-Flex, 5-Flex, 6-Flex, 5-Power, 6-Power. Never 2-Flex, 3-Flex, or 4-Power.

**Constraints:**
- at least 2 teams
- one pick per player
- by default at most one pick per game, because correlation isn't modelled

**Choosing an entry:** pick the structure and legs that maximise Kelly log-growth G*. Power EV is computed exactly; Flex uses a Poisson-binomial dynamic program.

**Payout table** (in config, seeded from PrizePicks 2026-09-18, overridden by `includeMultipliers`):

| Picks | Power (all hit) | Flex |
|---|---|---|
| 2 | 3× | 2 / 0.5 |
| 3 | 6× | 3 / 1 |
| 4 | 10× | 6 / 1.5 |
| 5 | 20× | 10 / 2 / 0.4 |
| 6 | 37.5× | 25 / 2 / 0.4 |

Only standard lines are used; no Demons or Goblins.

**Settlement rules:**
- A DNP or a tie drops the entry one tier.
- A 3-Flex drops to a 2-Power.
- A 2-pick entry with a DNP is refunded.
- If DNPs leave only one team, the entry is refunded. Ties do not trigger this.

### 6.4 Arbitrage (executable on Kalshi)

**Moneyline pair:**
- The test is P_eff(A) + P_eff(B) < 1.
- The size is the minimum of the two top-of-book sizes.
- An NFL-tie flag is shown only when the market rules have no 50/50 tie split (then both YES legs can lose on a tie). NO+NO pairs never carry it.

**Ladder monotonicity:**
- For one team's spread ladder, or a total ladder, a violation is bid(higher X) > ask(lower X) net of fees.
- The trade buys the lower line and sells the higher one, which means buying NO on the higher line.

**Cross-check:** if a configured venue is ever added, the generic Σ1/d<1 cross-venue scan also runs. It is off while only Kalshi and PrizePicks are enabled. A PrizePicks entry can never be an arb leg.

**Rejections and labelling:**
- Reject quotes older than the staleness limit.
- Reject ROI above about 8%, which is likely a data error.
- Every arb text says "theoretical at snapshot, verify before placing". Kalshi arbs tend to vanish within seconds.

### 6.5 Sizing (one bankroll for everything)

**Kalshi bets:** fractional Kelly, ¼ by default. A slate's bets are sized jointly by maximising E[log W]:
- exact enumeration for small slates
- Monte Carlo plus SLSQP for large slates
- proportional scale-down as a fallback

Stakes are rounded to whole contracts and capped by depth.

**PrizePicks entries:** multi-outcome Kelly on the payout distribution.

**Arbitrage:** a fixed bankroll fraction, 10% by default, also capped by depth.

**Caps:**

| Cap | Default |
|---|---|
| Per bet or entry | 2% |
| Per game | 4% |
| Per slate | 20% |
| PrizePicks per day | 10% |
| Minimum stake | $1 |

The bankroll used is the one at slate start, net of open exposure.

## 7. Bet lifecycle and confirmation

```
RECOMMENDED --(text sent)--> PENDING --confirm--> PLACED --final--> SETTLED (WON/LOST/PUSH/REFUND)
     |                          |-- skip ---------> SKIPPED
     |                          '-- kickoff/price moved -> EXPIRED
     '-- auto-gated (observe-only) -> LOGGED (CLV only, no money)
```

**Recommendation:**
- Its inputs are frozen: snapshot id, venue, ticker or legs, price, size, probabilities, EV, and stake.
- It is texted once, deduplicated by a hash of the contract and price.

**Confirmation:**
- The user runs `confirm <id> [price] [qty|stake]`, or presses Enter on the row. Enter opens a modal with editable fill price and quantity.
- Confirming debits the cost, including the computed fee, through the append-only ledger and marks the bet PLACED.
- An unconfirmed recommendation expires at kickoff.

**Settlement** sources, in order:

| Bet type | Sources |
|---|---|
| Kalshi | Kalshi market `result`, then scores |
| PrizePicks and props | nflverse/CFBD player stats |
| Games | TheRundown, then CFBD/nflverse, then Odds API `/scores`, then manual `settle` |

Settlement and the ledger write happen in one transaction.

**CLV:** the last sharp snapshot before kickoff gives p_close_fair, and CLV% = p_close_fair/P_eff − 1. If the app was closed through kickoff, CLV is marked missing rather than guessed.

## 8. Notifications

**Primary: iMessage through `osascript`.**
- The service is looked up by `service type`.
- The number and text are passed as argv, with no string interpolation.
- The phone number lives in `.env` as `NOTIFY_PHONE`, never in code.

**Test:** `notify test` checks that the phone actually alerts. Self-sent iMessages can arrive silently as a note-to-self.

**Fallback and second channel:** ntfy.sh push, which is free.

**Rules:**
- ASCII/GSM-7 only, kept compact.
- Deduplication, a cooldown, and quiet hours. Arbitrage alerts are exempt.
- Every attempt is logged.

**Example texts:**
```
GE KALSHI BUY YES PHI wins by >7.5 (KXNFLSPREAD-26OCT26DALPHI-PHI8) @ $0.46 x40 = $18.97 w/fee | fair 51.2% EV +7.9% | 12:41 PT | id 42
GE PP 5-FLEX $12 | EV +9.1% | 1) J.Allen PassYds MORE 247.5 (58%) 2) ... | lines 12:41 PT | confirm payout in app | id 43
GE ARB KALSHI +1.8% | CHI@SEA ML: YES SEA $0.55 + YES CHI $0.41 x50 | tie risk | verify now | id 44
```

## 9. TUI

**Theme:** Textual with a custom dark theme. Neon accents sit on a deep background: green for profit, red for loss, amber for pending.

**Layout:**
- **Header:**
  - app name and a clock
  - bankroll with today's gain or loss
  - quota meters drawn as small bars: Odds API credits, CFBD calls, TheRundown points, and Kalshi request rate
- **Left column:**
  - **Balance chart.** A braille line chart (textual-plotext) of bankroll over time, with deposits and withdrawals marked.
  - **KPI tiles.** ROI ±2σ, mean CLV and percent positive, record, max drawdown, and Brier score.
- **Centre:**
  - An **Opportunities board** with tabs: Kalshi Games / Kalshi Props / PrizePicks / Arbitrage.
  - **Live scores** below it. Each score is coloured by whether our open bets are currently winning, and shows a "~5m delay" badge.
- **Right: Event window.** A scrolling log of:
  - recommendations and texts sent
  - confirmations
  - deposits and withdrawals
  - settlements
  - API calls with their cost
  - errors
- **Bottom: command bar** with autocomplete, plus a footer of key bindings. Every command is also in the command palette.
- **Motion:**
  - a brief flash on new rows and settled bets
  - an animated count-up of the balance
  - a pulsing LIVE badge

**Commands:**

| Command | Purpose |
|---|---|
| `deposit <amt>`, `withdraw <amt>` | change the bankroll |
| `confirm <id> [price] [qty]`, `skip <id>` | confirm or skip a recommendation |
| `scan [games\|props\|pp\|arb]` | look for opportunities |
| `refresh [nfl\|cfb] --force` | shows the credit cost first |
| `bets [open\|settled]`, `settle <id> ...` | view or settle bets |
| `pp add ...` | enter a PrizePicks line by hand |
| `quota` | API usage |
| `notify test`, `notify on\|off` | test or toggle texts |
| `set kelly\|ev\|cap ...`, `venues ...` | settings |
| `backtest nfl\|cfb <years>`, `fit blend`, `report` | backtesting and reports |
| `help` | command help |

## 10. Persistence

**sqlite tables:**
- settings, quota, api_cache, team_aliases
- games, kalshi_markets
- odds_snapshots (append-only; covers every odds source, including Kalshi quotes)
- ratings_snapshots, predictions, recommendations
- bets, entries + entry_legs
- notifications
- ledger (append-only, integer cents)

The bankroll is SUM(ledger).

**Secrets** live in `.env`, which is git-ignored. A `.env.example` is committed.

## 11. Stack

**Packages:**
- UI: textual, textual-plotext/plotext
- HTTP: httpx, tenacity, aiolimiter
- Data: nflreadpy, polars, pyarrow
- Modelling: numpy, scipy, scikit-learn, pydantic v2
- Dev: pytest, plus penaltyblog as a test oracle for devig and Kalshi

**Do not use:**
- the `cfbd` SDK, because it needs pydantic<2
- `shin`, because it has no cp314 wheel

**Pinning:** versions are pinned at build time, after checking that they install on Python 3.14.

## 12. Decided by the user

| Decision | Choice |
|---|---|
| Objective | Maximize profit through optimal strategy |
| API tiers | Free; keys held for CFBD, TheRundown, The Odds API |
| Placing bets | The app never places bets; it notifies the user |
| Notification | Messages on this Mac, to the user's number (kept in `.env`) |
| Bet types | PrizePicks format (up to 6 picks) for now, plus any game bet, plus arbitrage checks |
| Bettable venues | **PrizePicks and Kalshi only** |
| PrizePicks payouts | **Fixed Power/Flex** |
| Runtime | **Only while the TUI is open** |
| Bankroll | Notional paper pool (deposit/withdraw), from "simulates sports betting" |

## 13. Builder's calls (changeable)

**Product:**
- Name: "Gridiron Edge".
- Python + Textual.
- Texts go out only for actionable items above threshold.
- Layout details.

**Strategy:**
- The default thresholds and caps above, including ¼ Kelly.
- The observe-only auto-gate.
- Kalshi taker pricing by default, with maker mode optional.
- Kalshi props are included as a bet type, because they are bettable on an allowed venue.

**Data and fallbacks:**
- Sportsbooks are used only as fair-value references.
- TheRundown is the main source for scores.
- ntfy is the fallback notification channel.
- Manual PrizePicks line entry is available as a fallback.
