# Kalshi football market data for a read-only +EV and arbitrage app (as of 2026-10-20)

Most facts below come from live calls I made today to `api.elections.kalshi.com` and from docs.kalshi.com. The fetch tool summarizes pages, so I did not see raw JSON in every case. Sources are linked inline, and anything unverified is flagged.

## 1. Public market-data API

**Base URL.** `https://api.elections.kalshi.com/trade-api/v2` still works and returned live data today. It serves all markets, not just elections.
- The current quick-start documents `https://external-api.kalshi.com/trade-api/v2` instead ([quick start](https://docs.kalshi.com/getting_started/quick_start_market_data)).
- The [changelog](https://docs.kalshi.com/changelog) says dedicated external hosts were added on 2026-05-07 and the shared hosts remain supported.
- Trade API REST and standard WebSocket URLs are unchanged by the 2026-10-22 move of the external WebSocket feeds to `external-feeds.kalshi.com`.
- Make the base URL configurable.

**Endpoints that work without auth.** I confirmed these live today:
- `GET /markets?series_ticker=…&status=open&limit=…`
- `GET /markets/{ticker}/orderbook`
- `GET /markets/trades?ticker=…&limit=…`
- `GET /events/{event_ticker}`
- `GET /series/{series_ticker}`
- `GET /series?category=Sports`
- `GET /series/fee_changes`

The quick-start also lists the market, event, series and orderbook endpoints as unauthenticated. It says the WebSocket needs auth. The changelog shows `GET /markets/orderbooks` (batch of up to 100 tickers, added 2026-03-30). I did not test it, so I don't know whether it needs auth.

**Pagination.** Responses carry a `cursor` string. Pass it back as `?cursor=` and stop when it is null or empty. `limit` defaults to 100 and is typically 1–100 ([pagination guide](https://docs.kalshi.com/getting_started/pagination)).

**Rate limits** ([docs](https://docs.kalshi.com/getting_started/rate_limits)):
- Basic tier allows 200 read tokens per second, and a typical request costs 10 tokens. That is about 20 requests per second sustained.
- Basic and Advanced read buckets can burst up to 3 seconds of budget.
- A 429 returns `{"error":"too many requests"}`, so use exponential backoff.
- The docs don't state a separate limit for unauthenticated calls. I only know the tier table. Treat an anonymous client as Basic or worse. Some docs pages returned 429 to me, so cache and be polite.

**Schema warning.** Integer-cent price fields are gone from current responses. Prices are strings such as `"0.5800"` in `*_dollars`, and sizes are strings in `*_fp`. Parse them with `Decimal` or `float`.
- The [changelog](https://docs.kalshi.com/changelog) shows `tick_size` removed (scheduled 2026-05-07) and `liquidity_dollars` removed (2026-10-01).
- Don't assume `yes_bid` or `yes_ask` integer fields exist.

## 2. Football market structure

All of these series exist and are live now. NCAAF series use `KXNCAAF…`.

| Product | Series | Example ticker |
|---|---|---|
| NFL moneyline | `KXNFLGAME` | `KXNFLGAME-26NOV02CHISEA-SEA` |
| NFL spread | `KXNFLSPREAD` | `KXNFLSPREAD-26OCT26DALPHI-PHI8` |
| NFL total | `KXNFLTOTAL` | `KXNFLTOTAL-26OCT26DALPHI-66` |
| NCAAF moneyline | `KXNCAAFGAME` | `KXNCAAFGAME-26OCT31OSUUSC-USC` |
| NCAAF spread | `KXNCAAFSPREAD` | `KXNCAAFSPREAD-26OCT24COLOOKST-OKST8` |
| NCAAF total | `KXNCAAFTOTAL` | exists per `fee_changes`; I did not fetch a market |
| NFL player props | e.g. `KXNFLRECYDS` | `KXNFLRECYDS-26OCT22NECHI-NETHENDERSON32-25` |
| NFL 2H | `KXNFL2HSPREAD`, `KXNFL2HWINNER` | not fetched |

- `KXNFLPASSATT` also exists. I did not test other prop series. Enumerate them with `GET /series?category=Sports` (the response is very large, over 6 MB).
- An OddsPapi post ([oddspapi.io](https://oddspapi.io/blog/?p=3660)) said game lines only were quoted on the opener. Its coverage claims are secondary and dated Aug 2026. Live props exist now.

**Ticker format.** `SERIES-{YY}{MON}{DD}{AWAY}{HOME}-{SUFFIX}`.
- The date is the game date in the ticker, e.g. `26NOV02`.
- The event is `KXNFLGAME-26NOV02CHISEA`, which is Chicago at Seattle. Team codes are away then home, and I inferred this from the title "Chicago vs Seattle".
- The moneyline suffix is the team code (`-SEA`, `-CHI`).
- The spread suffix is the team plus a rounded points number, e.g. `PHI8` for "wins by over 7.5". The `7` and `8` suffixes are rounded and not reliable. Use `floor_strike`.
- The total suffix is the rounded line (`-66` means over 65.5).

**Mapping a market to teams and kickoff.** Use these fields:
- `GET /events/{event_ticker}` gives `title` ("Chicago vs Seattle"), `sub_title` ("CHI vs SEA (Nov 2)"), `series_ticker`, and `mutually_exclusive: true`.
- `yes_sub_title` and `no_sub_title` give the team name on moneyline and spread markets ("Seattle"). On totals they give "Over 65.5 points".
- `custom_strike.football_team` is a team UUID. I did not find a public UUID-to-name map, so map by name.
- `floor_strike` and `strike_type: "greater"` give the number for spreads and totals. A spread market reads "TEAM wins by over X". The "team" is named in the title.
- `rules_primary` states the exact condition.
- `occurrence_datetime` and `expected_expiration_time` were identical on every market I fetched. For KXNFLGAME CHI@SEA they were `2026-11-03T04:15:00Z`, while the event date is Nov 2. That is roughly the expected game-end time (UTC), not kickoff.
- `close_time` and `expiration_time` are also later than the game (`2026-11-05T01:15:00Z`).
- I found no explicit kickoff field, so parse the date from the ticker and the time from `occurrence_datetime` minus about 3 hours.
- Verify the kickoff time against your odds feed.

**Status and result.**
- The market `status` was `active` in responses. The API filter `status=open` also worked.
- Other documented values are `initialized`, `inactive`, `closed`, `determined`, `settled`. This is from my memory of the docs and is unverified here.
- `result` is `""` until settled. After settlement it is `"yes"` or `"no"`, with `expiration_value` set (also from memory, unverified).
- `can_close_early: true` and `early_close_condition` say a moneyline "closes and expires after a winner is declared".
- `settlement_timer_seconds` is 60 for NFL and 90 for NCAAF.
- NCAAF `rules_secondary`: a postponed game that starts within 48 hours stays open. A cancelled game or one not started within 48 hours resolves "to a fair price".
- NFL tie handling is in `rules_secondary`, but my summary truncated it. Read it from the API before treating the two moneylines as exhaustive.

## 3. Price fields

Observed live on `KXNFLGAME-26NOV02CHISEA-SEA`:
- `yes_bid_dollars` 0.58 and `yes_ask_dollars` 0.62.
- `no_bid_dollars` 0.38 and `no_ask_dollars` 0.42.
- `yes_bid_size_fp` 3200.00 and `yes_ask_size_fp` 200.00. These are top-of-book sizes in contracts.
- `last_price_dollars` 0.72, `previous_price_dollars`, `volume_fp`, `volume_24h_fp` and `open_interest_fp`.
- `notional_value_dollars` 1.0000.
- `price_level_structure: "linear_cent"` with `price_ranges` of 0.00–1.00 in steps of 0.01.

Things to note:
- The relation is `yes_ask = 1 - no_bid` and `no_ask = 1 - yes_bid`. A "YES ask + NO ask" check on one market is always at least 1 (here 0.62 + 0.42 = 1.04), so there is no arbitrage inside one binary market.
- `last_price` can sit far from the current bid/ask, as it does on this market, so don't use it as a price.
- The changelog says `center_deci_edge_centi_cent` sub-cent price structures were introduced on 2026-08-13, with combo markets moved to a finer tick on 2026-09-03. Read `price_level_structure` and don't hardcode a 1-cent tick.

**Orderbook.** `GET /markets/{ticker}/orderbook` returns `orderbook_fp` with `yes_dollars` and `no_dollars`.
- Each is an array of `[price, size]` pairs and the API returns bids only.
- To get asks, flip the other side: the YES ask at price p is the NO bid at 1 - p.
- The book is very deep on the 0.01 level, e.g. 2.4M contracts as resting lowball bids. Ignore it.
- I did not verify the exact JSON nesting. It was summarized for me.

## 4. Fees

The schedule is [Kalshi's fee schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf). That URL returned 429 to me, so the formulas below come from the secondary summary at [chudi.dev](https://chudi.dev/blog/kalshi-fees). That page claims "effective 07/07/2026 (checked 10/10/2026)". Treat it as unconfirmed against Kalshi's own page.

- **Taker fee:** `ceil_to_cent(0.07 × C × P × (1−P)) × multiplier`, with P in dollars and C the number of contracts. The default multiplier is 1.
- **Maker fee:** `0.0175 × C × P × (1−P)`, rounded up, charged only where the series has a maker multiplier. The default maker multiplier is 0.
- **Sports series.** Kalshi's own API says `KXNFLGAME` and `KXNCAAFGAME` have `fee_type: quadratic_with_maker_fees` and `fee_multiplier: 1`. `fee_changes` shows the same for `KXNCAAFSPREAD` and `KXNCAAFTOTAL` (effective 2026-01-01).
  - Takers pay 0.07 × C × P × (1−P).
  - Resting makers pay 0.0175 × C × P × (1−P).
  - There is no sports-specific discount to the taker rate that I could find.
  - `KXNFLSPREAD`, `KXNFLTOTAL` and `KXNCAAFGAME` returned no entries from `fee_changes`. The `KXNFLGAME` and `KXNCAAFGAME` series endpoints still returned `quadratic_with_maker_fees`. I did not fetch the `KXNFLSPREAD` or `KXNFLTOTAL` series objects, so confirm them with `GET /series/{ticker}`.
- **Per-market rates.** The [changelog](https://docs.kalshi.com/changelog) says `GET /margin/fee_tiers` returns per-market `maker_fee_rates` and `taker_fee_rates` (2026-05-11). That is a margin endpoint, so I don't know if it covers these markets.
- **Combos.** Combo markets have a separate maker multiplier of 2, and combos made only of independent NFL legs have no maker fee since Aug 20, 2026. These don't affect single-game markets.
- **Fee at the midpoint.** At 50 cents, the unrounded taker fee is 1.75 cents per contract. For 100 contracts at 50 cents, the schedule table shows $1.75. The table shows $0.02 for one contract. Fees are rounded up to the cent per order, so small orders overpay.
- **Fee peak.** Fees peak at 50 cents and shrink toward 0 and 100.
- **Rounding.** Per Kalshi's [fee-rounding guide](https://docs.kalshi.com/getting_started/fee_rounding), non-direct members' balances align to $0.01, and accumulated rounding overpayment is rebated in $0.01 steps. Rebates cannot make a fill's net fee negative.
- **Settlement fee.** I found none on the fee page or in the docs. ACH is free, and card deposits can cost up to 2%.

## 5. California availability and position limits

- Kalshi's public API serves California IPs fine. Whether the app lets a California account trade sports is a separate, account-side question.
- Two Ninth Circuit rulings matter:
  - On Aug 28, 2026, in *KalshiEX v. Assad*, the Ninth Circuit held that sports event contracts aren't "swaps" and don't get CEA preemption. This is from a [JURIST summary](https://www.jurist.org/news/2026/09/the-ninth-circuit-rules-kalshi-sports-contracts-on-tribal-lands-likely-violate-federal-law/) and a [Buchalter note](https://www.buchalter.com/insights/ninth-circuit-kalshis-sports-contracts-are-class-iii-gaming-on-tribal-land-and-tribes-can-sue-to-stop-them/). I only saw secondary search summaries.
  - On Sep 16, 2026, in *Blue Lake Rancheria v. Kalshi*, a panel held tribes are likely to win their IGRA claim for users on tribal lands. It remanded for the remaining injunction factors rather than imposing an injunction ([SBC Americas](https://sbcamericas.com/2026/09/17/ninth-circuit-rules-ca-tribes-kalshi/)).
- I found no California statewide cease-and-desist or statewide geofence for Kalshi sports. A [CoinStats tracker](https://coinstats.app/prediction-markets/legal/kalshi-ca/) says Kalshi switched off markets on three tribes' lands after Sep 16. Its dates conflict with others, so treat it as weak.
- California sports betting is otherwise illegal (Cal. Penal Code 337a), and voters rejected Props 26 and 27 in 2022.
- Nevada is geofenced under court order, with a vendor deadline of Aug 12, 2026 ([gaming.net](https://www.gaming.net/th/nevada-forces-kalshi-to-geofence-its-sports-markets/)). That is a precedent, not a California action.
- The legal picture is moving, and a Supreme Court review looks likely. Check the account-side state before you rely on being able to bet.
- **Position limits.** I couldn't confirm per-game limits. [OddsShopper](https://www.oddsshopper.com/articles/prediction-markets/kalshi-position-limits) says most markets carry a "position accountability level" rather than a hard limit. It cites $25,000 per strike for title futures, which is not a game market.
- Sizing is bounded in practice by top-of-book depth. Check each market's rules PDF (`contract_terms_url`) for exact figures.

## 6. Arbitrage-like structures inside Kalshi

- **Same-market YES + NO.** No arbitrage, since the NO ask is derived from the YES bid (see section 3).
- **Two-team moneyline.** Check `yes_ask(A) + yes_ask(B) < 1.00`. The event is `mutually_exclusive: true` with two markets.
  - Buying both at takers' asks costs `ask_A + ask_B` plus two taker fees, and pays $1 if exactly one wins.
  - CHI@SEA right now: SEA ask 0.62 plus CHI ask unknown, but SEA's NO ask is 0.42. CHI's YES ask should be about 0.42 or better. That sums to about 1.04, so no arbitrage.
  - Two fees at about 1.7 cents each push the breakeven to a sum of about 0.966. You need a real dislocation, and cross-market fees make it rare.
  - A tie is a risk (the NFL has ties). Both moneylines could settle NO, and I haven't confirmed how Kalshi settles them.
- **Spread ladder monotonicity.** Markets for one team are "wins by over X" for several X.
  - P(over 7.5) must be less than or equal to P(over 6.5), so require `ask(higher X) ≥ ...`. A violation is `bid(higher X) > ask(lower X)`, meaning you can buy the lower line and sell the higher one for a guaranteed profit.
  - The same applies to totals. P(over 65.5) is at most P(over 62.5).
  - Live today, `KXNFLSPREAD` PHI 7.5 was bid 0.13 / ask 0.14, and PHI 6.5 was bid 0.16 / ask 0.18. That is correctly ordered, with no violation.
  - You can also check that the opposing team's ladder is consistent with this one, though a margin of 0 splits the two.
- These checks need `status=active` and non-trivial `*_size_fp`. Quoted depth is the binding constraint. Both legs of a monotonicity trade need size on both sides.
- The worst fills come from `yes_ask_size_fp` values that look huge but sit at a different price level. Walk the real orderbook.
- Anything that lasts only seconds will be gone before a human, or a polling script at 20 req/s, can act.

## 7. Aggregators

- **The Odds API.** Kalshi is listed as bookmaker `kalshi` in region `us_ex` ([bookmaker list](https://the-odds-api.com/sports-odds-data/bookmaker-apis.html)). That page does not say which markets or sports are covered, and the v4 guide mentions no Kalshi at all. Exchanges use `h2h_lay` for lay odds.
  - Quota is 1 per region per market per call.
  - Test whether `kalshi` returns `spreads` and `totals` for `americanfootball_nfl` and `americanfootball_ncaaf` before relying on it.
- **TheRundown.** Kalshi is affiliate ID 25 ([sportsbooks reference](https://docs.therundown.io/reference/sportsbooks)). Pass `affiliate_ids=25`.
  - The docs say "Availability varies by source, sport, and market", so check `GET /api/v2/affiliates` for status.
  - Their docs don't list which markets Kalshi supports.
- **Other aggregators** (secondary and vendor-biased, so treat with caution):
  - [odds-api.io](https://docs.odds-api.io/guides/prediction-markets) converts Kalshi prices to decimal odds and gives top-of-book only, no ladder. It reports ML, spread and totals.
  - OddsPapi returns the `exchangeMeta` depth ladder.
- **Versus reading Kalshi directly.**
  - Aggregators give one schema and use team matching you don't have to write, which is the main benefit.
  - They give delayed or unverified data with unknown latency, often top-of-book only, and lose size, fees, tick structure and tickers.
  - You need the ticker anyway to bet.
  - Aggregator conversions can be off by a cent (the odds-api.io docs example is off by 1 cent).

## Recommendation

1. Read Kalshi directly and use aggregators only for cross-book comparison.
   - Per week, list events with `GET /markets?series_ticker=KXNFLGAME|KXNFLSPREAD|KXNFLTOTAL|KXNCAAFGAME|KXNCAAFSPREAD|KXNCAAFTOTAL&status=open`, following the cursor.
   - Use `GET /markets/{ticker}/orderbook` only for candidates.
   - Poll at 1 to 5 requests per second at most, so you stay well under 20 per second sustained.
2. Normalize to `(event, market_type, side/team, line, yes_ask, yes_bid, size)`.
   - Parse teams from `title`, `yes_sub_title` and `rules_primary`.
   - Take lines from `floor_strike`.
   - Map `KXNFLSPREAD` "wins by over X" to the sportsbook spread of -(X) for that team.
   - For a moneyline, treat YES ask as the price of betting on that team.
3. Build the EV on the ask you can actually fill, including the taker fee `0.07 × P × (1−P)`.
   - Use the orderbook to size fills.
   - A resting limit order pays the lower 0.0175 maker rate but may not fill.
   - A bettor should use the fee-adjusted price of about P + 0.07·P·(1−P).
4. Devig a sharp book (Pinnacle or a consensus) to a fair probability, then compare it with Kalshi's fee-adjusted ask.
   - Kalshi lines are coarse, and half-point spreads and totals can look like value that is just a different push-free line.
   - Fair value for "wins by over 7.5" must come from a ladder or a distribution, not a quoted spread.
5. Add in-Kalshi checks: the two-moneyline sum, and ladder monotonicity with fees.
6. Add guards.
   - Skip markets with `status` other than `active`.
   - Skip anything with thin top-of-book size.
   - Re-fetch before acting.
   - Flag NFL ties, and the NCAAF "fair price" cancellation rule.
7. Before you rely on the fee numbers, call `GET /series/{ticker}` for `fee_type` and `fee_multiplier`, and re-check fees at kalshi.com.

## Unverified or weak
- The fee formulas rest on a secondary page because the official PDF returned 429.
- Kickoff-time mapping, `result` values, the full status list and tie settlement.
- Per-game position limits.
- California statewide status.
- Aggregator Kalshi coverage of spreads and totals.
- Whether unauthenticated rate limits differ from the Basic tier.

Sources: [Kalshi quick start](https://docs.kalshi.com/getting_started/quick_start_market_data), [rate limits](https://docs.kalshi.com/getting_started/rate_limits), [pagination](https://docs.kalshi.com/getting_started/pagination), [changelog](https://docs.kalshi.com/changelog), [fee rounding](https://docs.kalshi.com/getting_started/fee_rounding), live calls to `api.elections.kalshi.com/trade-api/v2` (markets, events, series, trades, orderbook, fee_changes), [chudi.dev](https://chudi.dev/blog/kalshi-fees), [JURIST](https://www.jurist.org/news/2026/09/the-ninth-circuit-rules-kalshi-sports-contracts-on-tribal-lands-likely-violate-federal-law/), [SBC Americas](https://sbcamericas.com/2026/09/17/ninth-circuit-rules-ca-tribes-kalshi/), [Buchalter](https://www.buchalter.com/insights/ninth-circuit-kalshis-sports-contracts-are-class-iii-gaming-on-tribal-land-and-tribes-can-sue-to-stop-them/), [OddsShopper](https://www.oddsshopper.com/articles/prediction-markets/kalshi-position-limits), [The Odds API bookmakers](https://the-odds-api.com/sports-odds-data/bookmaker-apis.html), [TheRundown sportsbooks](https://docs.therundown.io/reference/sportsbooks), [odds-api.io](https://docs.odds-api.io/guides/prediction-markets), [OddsPapi](https://oddspapi.io/blog/?p=3660), [botforkalshi](https://www.botforkalshi.com/tools/kalshi-nfl-markets).