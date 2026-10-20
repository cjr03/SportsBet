# TheRundown API on the free tier: research report (as of 2026-10-20)

Almost everything below comes from TheRundown's own docs at docs.therundown.io, fetched today. The docs pages are pulled through a summarizing fetch tool, so exact wording may differ. I did not make any live API calls or check the OpenAPI spec directly.

## 1. Base URL, version, auth, keys
- **Base URL:** `https://therundown.io/api/v2` ([quickstart](https://docs.therundown.io/quickstart.md)).
- **V1 is legacy but still live.** The [changelog](https://docs.therundown.io/changelog.md) says no V1 deprecation has been announced and new features are V2-only.
- **Auth header:** `X-TheRundown-Key: <key>` ([authentication](https://docs.therundown.io/authentication.md)).
  - Query-param auth exists only for legacy V1 clients. The docs don't give its syntax, so don't use it.
  - `GET /api/v2/sports` and `GET /api/v2/affiliates` need no key.
- **Free keys:** sign up at https://therundown.io/api ([quickstart](https://docs.therundown.io/quickstart.md)). You don't go through RapidAPI. None of the docs pages I fetched mention RapidAPI, so I can't confirm whether RapidAPI access is still offered.
- **Trial caveat:** the [FAQ](https://docs.therundown.io/faq.md) says new Free accounts get a no-card, 7-day evaluation, but only in the stats context. It's unclear whether odds access on the free tier lapses after 7 days, so check your dashboard.

## 2. Free tier limits
Sources: [rate limits](https://docs.therundown.io/rate-limits.md) and the [pricing page](https://therundown.io/api).

| Item | Free tier |
|---|---|
| Data points | 20,000 per UTC day and 200,000 per UTC month. Once either cap is hit you get a 429 until the reset. |
| Burst rate | 1 request per second |
| Data delay | 5 minutes |
| Live odds | Not included (pre-match odds only) |
| Player props | Not included (removed from free in Jun 2026) |
| Odds history | Not included (starts on Starter) |
| Event and score history | 7 days |
| WebSocket | Not included |
| Overage billing | None |
| Sportsbooks | DraftKings (19), FanDuel (23), BetMGM (22) |

- **Sportsbooks:** the free-tier book list comes only from the pricing-page summary. The affiliates reference doc doesn't state tiers, so confirm with the `X-Bookmakers` response header.
- **Cost formula:** 2 × returned events + returned prices. Scores come with event responses at no extra charge. Catalog endpoints, the stats sample and the V1 schedule endpoint cost 0.
- **Cost tips:**
  - `hide_closed=true` on completed dates cuts cost.
  - `affiliate_ids=0` returns status and scores with no markets, about 2 points per event.
  - `market_ids=1`, one `affiliate_ids` value and `main_line=true` cost roughly 4 points per event.
  - REST requests with more than 12 market IDs now return a 400.
- **Response headers to log:** `X-Datapoints`, `X-Datapoints-Remaining`, `X-Datapoints-Monthly-Remaining`, `X-Tier`, `X-Data-Delay-Seconds`, `X-Bookmakers`, `X-Live-Odds-Access`, `X-Websocket-Access`.
- **Stale numbers:** a third-party tracker (pricingsaas) reports the monthly cap was added around 2026 week 36. The docs changelog dates it Aug 2026. Older 2025-era numbers you may find elsewhere are stale.

## 3. Sport IDs
From the [sports reference](https://docs.therundown.io/reference/sports.md):
- **NCAAF:** `1`
- **NFL:** `2`
- NFL Preseason is `25` and NFL Playoffs is `26`, both with NFL as the parent.
- For reference, MLB is `3` and NBA is `4`.

Confirm with `GET /api/v2/sports`, since the docs advise querying the catalog rather than assuming IDs.

## 4. Key endpoints
All paths below are under `/api/v2`, and all are tied to the [docs index](https://docs.therundown.io/llms.txt).

| Purpose | Path | Free? |
|---|---|---|
| Events by sport and date, with scores and markets | `GET /sports/{sport_id}/events/{YYYY-MM-DD}` | Yes, within the 7-day window. |
| Single event | `GET /events/{eventID}` | Yes. Not limited by the history window. |
| Opening lines | `GET /events/{id}/openers` and `GET /sports/{id}/openers/{date}` | No. "Pro and above" per the [historical odds guide](https://docs.therundown.io/guides/historical-odds.md). |
| Closing lines | `/closing` equivalents | Not stated. |
| Price history (line moves) | `GET /events/{id}/markets/history` and `/markets/{marketID}/history` | No. Starter and above. |
| Market delta | `GET /markets/delta?last_id=&sport_id=&market_ids=&affiliate_ids=` | Probably no (see below). |
| Event delta (status and scores) | `GET /delta` | Probably no (see below). |
| Teams | `GET /sports/{id}/teams` | Yes (catalog). |
| Schedule | V1 `get-event-schedule-for-a-sport` | Costs 0 data points. I didn't confirm the exact path. |
| Other | `/sports/{id}/dates`, `/sports/{id}/conferences`, `/sports/{id}/divisions`, `/events/{id}/markets` | Not checked. |

- **Delta endpoints:** the [efficient-polling guide](https://docs.therundown.io/guides/efficient-polling.md) says delta polling requires a known zero-delay entitlement. Delayed plans are told to use narrow repeated snapshots instead. That makes deltas likely unusable on free, but this is an inference, not an explicit statement.
- **Line-move tracking on free:** it has to be self-built. Snapshot the odds yourself and diff them.
- **Player props:** Starter or higher. The same docs say Free keys do not receive them.
- **Market IDs** (from the [markets reference](https://docs.therundown.io/reference/markets.md)):
  - Moneyline `1`, spread `2`, total `3`, team totals `94`.
  - The in-play equivalents are `41`, `42`, `43` and `96`.
  - The default `market_ids` is `1,2,3`.
  - NFL-specific prop IDs are not documented in the pages I read.

## 5. Response shape
The [data model page](https://docs.therundown.io/reference/data-model.md) has no verbatim JSON, so the shape below is assembled from its field tables.

**Score object** (`event.score`):
- `event_status` and `event_status_detail`, e.g. "3rd Quarter - 4:32".
- `score_home`, `score_away`, `score_home_by_period` and `score_away_by_period`.
- `game_period` (the current period number; the docs have no field named `period`), `game_clock` (seconds) and `display_clock` (string, e.g. "4:32").
- `updated_at`.

**Status values:** the [event statuses page](https://docs.therundown.io/reference/event-statuses.md) lists 21 values. The ones that matter for football are:
- `STATUS_SCHEDULED`, `STATUS_IN_PROGRESS`, `STATUS_FINAL`
- `STATUS_HALFTIME`, `STATUS_END_PERIOD`
- `STATUS_END_OF_REGULATION` and `STATUS_OVERTIME`
- `STATUS_POSTPONED`, `STATUS_CANCELED`, `STATUS_SUSPENDED`, `STATUS_DELAYED`
- `STATUS_FORFEIT`, `STATUS_TBD`, `STATUS_NOT_AVAILABLE`

Treat unknown values as unknown, because new ones are added over time. There is no `winner` field, so derive the winner from the scores once the status is final.

**Odds shape:**
- The path is `markets[] → participants[] → lines[] → prices{affiliate_id: {...}}`. This is V2.
- Each market has `market_id` and `period_id` (0 means full game).
- Participants have a type: `TYPE_TEAM` (id equals `teams[].team_id`), `TYPE_PLAYER` or `TYPE_RESULT` (Over/Under index).
- Each line has a `value` (the spread or total number), and its `prices` object is keyed by affiliate id. The price fields are `price` (American odds), `is_main_line`, `updated_at`, and optionally `closed_at`.
- Rows with `closed_at` are closed.
- **Sentinel:** `price = 0.0001` means the line is off the board ([sentinel values](https://docs.therundown.io/reference/sentinel-values.md)). Don't use it in math.
- V1 uses fields like `moneyline_home`, `point_spread_home` and `total_over`. I did not verify these field names, so check the V1 response before relying on them.

**Affiliate IDs** (from the [sportsbooks reference](https://docs.therundown.io/reference/sportsbooks.md)):
- 2 Bovada, 3 Pinnacle, 4 SportsBetting, 6 BetOnline
- 7 BookMaker, 9 BetCRIS, 11 LowVig, 14 Intertops
- 16 Matchbook, 18 YouWager, 19 DraftKings, 21 Unibet
- 22 BetMGM, 23 FanDuel, 24 theScore Bet, 25 Kalshi
- 26 Polymarket, 28 HardRock, 29 ProphetX, 30 Novig
- 31 Polymarket US, 32 Circa, 33 Bet105, 34 Heritage, 35 Caesars

## 6. WebSocket
- **Endpoint:** `wss://therundown.io/api/v2/ws`, plus `/ws/markets`.
- **Tiers:** Ultra ($399/month) and above only, with 1 connection and 3 subscriptions on Ultra.
- **Free:** no WebSocket.
- **Live game state and play-by-play:** also Ultra and above.

## Recommendation
**Use TheRundown free for live scores and The Odds API only for odds.** Scores cost almost nothing here, and the 5-minute delay is the main downside.

1. **Live scores (the main win).**
   - Poll `GET /sports/2/events/{date}?affiliate_ids=0`. That costs about 2 points per event with no markets or prices.
   - With about 16 NFL games on a Sunday, that is about 32 points per poll, and 20,000 per day is far more than enough.
   - The real constraints are the 1 request/second limit and the 200,000/month cap. Poll every 30 to 60 seconds during games only, and run NFL (`2`) and NCAAF (`1`) as separate calls.
   - The score data is delayed 5 minutes on free. The docs state the delay but don't say it applies to scores specifically. Check `X-Data-Delay-Seconds` and measure it against a live game.
   - Use `display_clock`, `game_period` and `event_status` for the live display.
2. **Pre-match line-move tracking.**
   - Snapshot `market_ids=1,2,3`, `affiliate_ids=19,23,22` and `main_line=true` every 5 to 15 minutes.
   - On NFL game days, 16 games with about 3 markets and 3 books costs roughly 200 to 300 points per snapshot. That is about 15 to 25 snapshots per day within the daily cap, and the monthly cap is the tighter limit.
   - Diff the snapshots yourself and store the history locally, since official history and openers are paid. The cost estimate is mine, built from the documented formula.
3. **Keep The Odds API (500 credits/month)** for books outside DraftKings, FanDuel and BetMGM and for player props, which free TheRundown doesn't provide. Spend its credits sparingly, such as a single weekly pull.
4. **Before building:**
   - Check `X-Tier`, `X-Bookmakers` and `X-Data-Delay-Seconds` on your first real response.
   - Confirm the free tier returns odds after the 7-day evaluation period.
   - Handle 429s using `Retry-After`, and use a 1.1-second request gate.

## Unverified
- How to get a free key beyond the signup page, and whether RapidAPI still works.
- Whether the free key's odds access expires after the 7-day evaluation.
- Whether the 5-minute delay applies to scores as well as odds.
- Whether the delta endpoints are blocked on free. The docs only imply it.
- The exact free-tier sportsbook list. It comes from the pricing-page summary only.
- V1 field names.
- The exact schedule endpoint path.

Sources:
- [Pricing page](https://therundown.io/api)
- [Rate limits](https://docs.therundown.io/rate-limits.md)
- [Docs index](https://docs.therundown.io/llms.txt)
- [Quickstart](https://docs.therundown.io/quickstart.md)
- [Authentication](https://docs.therundown.io/authentication.md)
- [Changelog](https://docs.therundown.io/changelog.md)
- [FAQ](https://docs.therundown.io/faq.md)
- [Sports reference](https://docs.therundown.io/reference/sports.md)
- [Sportsbooks reference](https://docs.therundown.io/reference/sportsbooks.md)
- [Markets reference](https://docs.therundown.io/reference/markets.md)
- [Event statuses](https://docs.therundown.io/reference/event-statuses.md)
- [Sentinel values](https://docs.therundown.io/reference/sentinel-values.md)
- [Data model](https://docs.therundown.io/reference/data-model.md)
- [Scores and results guide](https://docs.therundown.io/guides/scores-and-results.md)
- [Efficient polling guide](https://docs.therundown.io/guides/efficient-polling.md)
- [Historical odds guide](https://docs.therundown.io/guides/historical-odds.md)
- [Player props guide](https://docs.therundown.io/guides/player-props.md)
- [pricingsaas change tracker](https://pricingsaas.com/companies/therundown/diffs/2026W36) (third-party)