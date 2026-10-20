# prizepicks-format (fact-checked 2026-10-20)

FACT-CHECK VERDICT
- Most of the draft holds up.
- The payout table and the breakeven/EV math are confirmed. I re-ran the binomial myself and every value matches; the only difference is 2P at 55%, which is -9.25% and rounds either way.
- Seven items needed correcting or extra caveats. They are marked [CORRECTED] or [ADDED] below.

1. FORMAT AND ENTRY TYPES
- Player Picks lineups have 2 to 6 picks. You call More or Less on each line. Stacks that include Team Picks are capped at 6 picks in total and are Power Play only (https://www.prizepicks.com/ways-to-pick, fetched today).
- Power Play: every pick must hit. Flex Play: pays on some misses.
- [CORRECTED] Arena / peer-to-peer switch:
  - PrizePicks announced on Aug 22, 2025 a "full transition to Arena in all states and territories in which we operate" (https://www.covers.com/industry/prize-picks-moves-to-peer-to-peer-daily-fantasy-sports-contests-nationwide-aug-27-2025 ; https://sbcamericas.com/2025/08/26/prizepicks-p2p-arena-only-us/).
  - Footprint is about 45 US states. The "35 states" figure is wrong: 35 is the number of those states that also have legal sports betting.
  - Covers describes Arena as matching users into head-to-head or group contests, with "cash prizes ... awarded to the user with the highest score."
  - The draft's claim that "a built-in payout still applies if your picks hit" is NOT verified by any source I found.
  - The official ways-to-pick page still publishes fixed Power/Flex multipliers and does not mention Arena. Treat the multipliers as nominal defaults that can be configured, not guaranteed returns.
- New York relaunched P2P in Feb 2026 after a $15M settlement (https://sbcamericas.com/2026/02/05/prizepicks-reenters-new-york-market/).
- The California AG's position on DFS and the "Streak-only states" list both come from 2025 articles and are UNVERIFIED for 2026. Drop them or label them as unverified. They are irrelevant to a paper-money simulator anyway.

2. PUBLISHED MULTIPLIERS (VERIFIED today on https://www.prizepicks.com/ways-to-pick)
- Power Play: 2-pick 3x, 3-pick 6x, 4-pick 10x, 5-pick 20x, 6-pick 37.5x.
- Flex Play:
  - 2-pick: 2/2 = 2x, 1/2 = 0.5x
  - 3-pick: 3/3 = 3x, 2/3 = 1x
  - 4-pick: 4/4 = 6x, 3/4 = 1.5x
  - 5-pick: 5/5 = 10x, 4/5 = 2x, 3/5 = 0.4x
  - 6-pick: 6/6 = 25x, 5/6 = 2x, 4/6 = 0.4x
- The page says the table "reflects lineups with picks on standard projections."
- Older third-party tables list 3-pick Power at 5x and are outdated. This confirms the payout table should live in config.

3. BREAKEVEN AND EV (VERIFIED by recomputation; binomial model with independent legs)

| Entry | Breakeven | EV at 55% | EV at 58% | EV at 60% |
|---|---|---|---|---|
| 2P | 57.74% | -9.25% | +0.9% | +8.0% |
| 3P | 55.03% | -0.2% | +17.1% | +29.6% |
| 4P | 56.23% | -8.5% | +13.2% | +29.6% |
| 5P | 54.93% | +0.7% | +31.3% | +55.5% |
| 6P | 54.66% | +3.8% | +42.8% | +75.0% |
| 2F | 61.80% | -14.7% | -8.4% | -4.0% |
| 3F | 57.74% | -9.2% | +0.9% | +8.0% |
| 4F | 55.03% | -0.2% | +17.1% | +29.6% |
| 5F | 54.25% | +5.0% | +26.9% | +43.4% |
| 6F | 54.21% | +7.5% | +40.2% | +66.4% |

- [CORRECTED] The draft said 5F/6F give the "best value per unit of variance." That is unsupported. What is true: 5F and 6F have the LOWEST breakeven (about 54.2%, roughly -118 American).
- 6-leg entries are high-variance. Size them with fractional Kelly or a flat small stake.
- 2-Flex is never +EV unless p is above 61.8%.

4. SETTLEMENT RULES (https://www.prizepicks.com/help-center/injuries-and-dnps, page shows "Last updated October 19, 2026")
- VERIFIED: a DNP drops the lineup one tier.
  - "3-Pick Flex → 2-Pick Power"
  - "2-Pick Flex → Refund"
  - "2-Pick Power → Refund"
- VERIFIED: ties count as neither correct nor incorrect and lower the payout like a DNP. Unlike DNPs, ties do NOT trigger the same-team or minimum-pick ineligibility.
- VERIFIED: if DNPs or Reboots leave only players from one team, the lineup is refunded automatically.
- VERIFIED: Reboots apply only to MORE selections.
- [CORRECTED] The page does NOT say NFL/CFB have Reboots. It says "Only specific sports have an active Reboot policy" and points to a separate Reboot Policy page. Mark NFL/CFB reboot eligibility as unverified.
- [ADDED] Lineups still eligible for a "Minimum Guarantee" payout keep that eligibility even with DNPs.

5. DEMONS AND GOBLINS (VERIFIED: https://www.prizepicks.com/playbook-article/demons-and-goblins-explained-less-picks-new-feature)
- Less on Demons and Goblins went live Aug 21, 2026 for select MLB and WNBA stat types. NFL is "coming soon."
- The maximum payout of 2000x is reachable only with Demons.
- No fixed per-pick factor is published, and the article does not say whether Demons and Goblins can be used in Flex.
- The recommendation holds: use only standard lines.

6. NFL AND CFB STAT CATEGORIES
- No official PrizePicks list was retrievable.
- [ADDED] The Odds API's NFL/NCAAF/CFL player-prop market keys are VERIFIED (https://the-odds-api.com/sports-odds-data/betting-markets.html). They include:
  - player_pass_yds, player_pass_tds, player_pass_attempts, player_pass_completions, player_pass_interceptions, player_pass_longest_completion
  - player_rush_yds, player_rush_attempts, player_rush_longest, player_rush_tds
  - player_receptions, player_reception_yds, player_reception_longest, player_reception_tds
  - player_rush_reception_yds, player_pass_rush_yds, player_pass_rush_reception_yds, player_rush_reception_tds, player_pass_rush_reception_tds
  - player_field_goals, player_kicking_points, player_pats
  - player_sacks, player_solo_tackles, player_tackles_assists, player_assists, player_defensive_interceptions
  - player_anytime_td, player_1st_td, player_last_td, player_tds_over, player_pass_yds_q1
  - Plus *_alternate variants of most of these. PrizePicks Demons and Goblins land in the *_alternate variants.
- Keep the stat list dynamic, based on what the feed actually returns.
- NCAAF prop coverage depth by bookmaker is unverified and is probably much thinner than NFL.

7. GETTING LINES PROGRAMMATICALLY
- [CORRECTED/VERIFIED] Live curl test today:
  - api.prizepicks.com/projections and /leagues returned HTTP 403.
  - partner-api.prizepicks.com/projections returned HTTP 200 once, but the body was an error JSON ("There was a problem processing your request"). The next call returned HTTP 429.
  - Conclusion: these endpoints are rate-limited and blocked, and unusable for a reliable app.
- League IDs NFL=9 and CFB=15 are UNVERIFIED. Only NBA=7 appears in third-party samples (https://apify.com/logiover/prizepicks-scraper).
- ToS (effective Aug 3, 2026) is VERIFIED:
  - §16(l) bans "any robot, spider, or other automatic device, process, or means to access the Site or App ... including monitoring or copying."
  - §4(a) bans commercial use.
  - §3(a) limits the license to personal, noncommercial use.
  (https://www.prizepicks.com/help-center/terms-of-service)
- SANCTIONED PATH, The Odds API (VERIFIED, https://the-odds-api.com/sports-odds-data/bookmaker-apis.html):
  - Region us_dfs carries bookmaker keys prizepicks, underdog, pick6 and betr picks.
  - The page notes: "Odds on DFS sites can vary based on user selections, therefore odds are indicative only."
  - PrizePicks: "Demons and goblins are included under _alternate markets." "Goblins have been assigned the default odds, demons have been assigned even odds (+100)."
- [CORRECTED SOURCE] includeMultipliers is documented in the v4 guide (https://the-odds-api.com/liveapi/guides/v4/), not on the bookmakers page. It is an optional event-odds parameter for US DFS sites, defaults to false, and returns "the multipliers in each bet selection outcome if available."
- Credit costs (VERIFIED, v4 guide):
  - /events/{id}/odds costs "[number of unique markets returned] x [number of regions specified]".
  - /odds costs 1 per region per market, and is free if no events are returned.
  - /sports and /sports/{sport}/events "do not count against the usage quota". Use these to discover event IDs for free.
  - /scores costs 1, or 2 with daysFrom.
- Free Starter plan: 500 credits/month, "Most bookmakers", "All betting markets" (https://the-odds-api.com/).
- [ADDED] It is UNVERIFIED whether prizepicks/us_dfs data is returned on the free tier. The bookmakers page flags only ReBet as paid-only. Do one test call with the key and check the bookmaker keys in the response.
- [ADDED] Budget example: 3 prop markets x 2 regions (us_dfs, us) = 6 credits per event. Covering all ~15 NFL games once a week is about 90 credits, roughly 360 to 390 per month. That leaves almost nothing for CFB, refreshes, or h2h arbitrage scans.
- The free tier realistically supports only about 4 to 6 targeted games per week with props, plus occasional featured-market arb scans.
- Alternatives (paid): SportsGameOdds sells a PrizePicks feed covering NFL and CFB (https://sportsgameodds.com/bookmakers/prizepicks-odds-api).

8. ARBITRAGE (logic confirmed)
- No true arbitrage exists within PrizePicks. Entries pay on combinations, and there is no opposing side to back.
- Implement it as +EV checking:
  - De-vig sportsbook player-prop prices (region us) to get the true per-side probability p.
  - Choose 2 to 6 standard-line legs with p above the entry breakeven (54.2% for 5F/6F).
  - Require at least 2 distinct teams.
  - Compute entry EV with the binomial over the config payout table.
- Run a separate true two-way arbitrage scan across sportsbooks on h2h, spreads and totals: flag when the sum of 1/decimal odds is below 1, using the same team, market and point.
- Caveat: same-game correlation is not modeled. Odds are "indicative only" and can be stale.
- SMS alert contents:
  - entry type and size
  - each leg: player, stat, line, More/Less, no-vig p
  - entry EV and stake
  - kickoff/expiry time
- The app only notifies the user and never places bets.

## Recommendations
1. Keep the payout table in config, using the verified official values. Label them "nominal": under Arena (P2P) the actual prizes may differ, and the claim that the fixed payout still applies is unverified.
2. Compute EV with an exact binomial, including the Flex tiers. Recommend an entry only when EV is above 0. Prefer 5F/6F (breakeven about 54.2%), never 2F. Size stakes with fractional Kelly or a small flat stake, because 6-leg variance is high.
3. Enforce at least 2 distinct teams per entry. Settle paper bets with the verified revert ladder: nF to (n-1)F, 3F to 2P, 2F/2P refund; ties revert like a DNP but do not trigger same-team refunds. Make the reboot rule configurable, because NFL/CFB eligibility is unverified.
4. Use only standard lines and skip *_alternate markets (Demons/Goblins).
5. Get PrizePicks lines from The Odds API: GET /v4/sports/{americanfootball_nfl|americanfootball_ncaaf}/events/{id}/odds?regions=us_dfs,us&markets=player_pass_yds,player_rush_yds,player_reception_yds&bookmakers=prizepicks,... Discover event IDs with the free /events endpoint. Do NOT scrape api.prizepicks.com: it is blocked (403/429) and violates ToS §16(l).
6. Before building on it, make one test call to confirm the free Starter key actually returns the prizepicks bookmaker. If it does not, fall back to manual line entry in the TUI.
7. Budget credits at about 6 per event (3 markets x 2 regions). Limit props to about 4 to 6 games a week. Track the x-requests-remaining header and stop fetching when it gets low. Reserve some credits for cross-book h2h/spreads/totals arbitrage scans.
8. Present "arbitrage" honestly. PrizePicks gets +EV checking against no-vig sportsbook odds. True arbitrage alerts come only from a separate cross-sportsbook scan (sum of 1/decimal odds below 1, same market and point).
9. The SMS (to the user's number) should include: entry type and size, each leg (player, stat, line, More/Less, no-vig p), entry EV, suggested stake, and kickoff/expiry time. State clearly that it is a simulation and the user must place the bet manually.
10. Remove or flag as unverified: the Streak-only state list, the California AG statement, league IDs 9/15, and the "best per unit of variance" wording.

# prizepicks-strategy (fact-checked and corrected)

VERIFICATION SUMMARY
Confirmed: the payout table, all break-evens, the EV and Kelly figures, the Odds API endpoint, cost formula, includeMultipliers and us_dfs/pinnacle regions, the 500-credit free plan, the prop market keys, the claim that Shin equals additive for two-way markets, the nflreadpy API, the CFBD 1,000-call free tier, and the CFBD routes (checked against the live OpenAPI spec).

CORRECTIONS
(a) The DNP help page was updated 2026-10-19, not 2026-08-04. On a DNP, a 3-pick Flex reverts to a 2-pick POWER, not a 2-pick Flex. A tie does NOT trigger the same-team or minimum-pick refund; only DNPs and Reboots do.
(b) 4-Flex was wrongly excluded. At p=0.58 it has the second-highest log growth: break-even 55.03%, EV +17.1%, full Kelly 5.99%, G* 0.48%/entry. That beats 3-Power (G* 0.28%) and 6-Power (0.22%).
(c) Budget math: the recommended regions us_dfs + us + eu make 3 regions. One event with 3 markets therefore costs 9 credits, not 6, which is about 55 prop pulls per month, not 80.
(d) The finding said "_alternate versions" for NFL and NCAAF. The Odds API lists the _alternate prop markets under NFL only, so NCAAF PrizePicks goblins and demons may not be retrievable.
(e) The Shin formula in the original text was garbled. The correct one is below.
(f) There was no mention of jurisdiction. The user's 408 number is California. PrizePicks pulled its house-banked Pick'em there on 2025-06-30 and moved to peer-to-peer "Arena". The CA Attorney General's opinion of 2025-07-03 says all DFS formats are illegal under state law. 2025 reporting also says PrizePicks went Arena-only across the US on 2025-08-22. Under the Arena format, a lineup that is not perfect can win only by topping its group, and tied group winners split the payout. I found no 2026 confirmation of current state status. Because the app only simulates with paper money, this does not block it, but the alerts should say the payouts may not be available as house-fixed multipliers.

CORRECTED SECTION

## 1. PrizePicks payout tables (standard projections; confirm in-app)
Source: PrizePicks Playbook, byline 2026-09-18, "last updated Friday, September 18 ... subject to change": https://www.prizepicks.com/playbook-article/2-pick-flex-plays-on-prizepicks-hit-one-pick-get-paid
- POWER: 2=3x, 3=6x, 4=10x, 5=20x, 6=37.5x
- FLEX: 6: 25/2/0.4x | 5: 10/2/0.4x | 4: 6/1.5x | 3: 3/1x | 2: 2/0.5x
- Demons, Goblins, correlated picks and other discounted projections carry altered payouts. The article advises: "Always confirm the payout on your Lineup detail screen." Keep the table in config.
- DNPs and ties (help center, last updated 2026-10-19, https://www.prizepicks.com/help-center/injuries-and-dnps):
  - A DNP or a tie removes the pick and drops the lineup one tier. Power goes 6→5→4→3→2. Flex goes 6→5→4→3, then 3-Flex→2-POWER.
  - A 2-pick lineup (Power or Flex) with a DNP is refunded.
  - If DNPs or Reboots leave only players from one team, the lineup is refunded. Ties do NOT trigger this refund.
  - A Reboot (MORE picks only) also reverts one tier.
- Jurisdiction caveat: the user's area code is California. In CA, PrizePicks has offered only peer-to-peer Arena since 2025-06-30 (https://sbcamericas.com/2025/07/02/prizepicks-california-p2p-arena-switch/). The AG opinion of 2025-07-03 treats all DFS as illegal. 2025 reports say Arena replaced house Pick'em US-wide on 2025-08-22 (https://sbcamericas.com/2025/08/26/prizepicks-p2p-arena-only-us/). Arena rules as described in 2024: a perfect lineup gets the full multiplier; otherwise the best score in the group wins and tied winners split the payout (https://www.actionnetwork.com/education/how-does-prizepicks-pickem-arena-work). Model the fixed-multiplier EV as an upper bound. Optionally add an "arena mode" that treats partial-hit payouts as uncertain.

## 2. Break-even per-pick probabilities (re-computed locally; all match)
Power 2 57.74 | P3 55.03 | P4 56.23 | P5 54.93 | P6 54.66
Flex 2 61.80 | F3 57.74 | F4 55.03 | F5 54.25 | F6 54.21
EV at p=0.58: P2 +0.9%, P3 +17.1%, P4 +13.2%, P5 +31.3%, P6 +42.8%, F2 -8.4%, F3 +0.9%, F4 +17.1%, F5 +26.9%, F6 +40.2%.
Rules:
- Never play 2-Flex or 3-Flex (3-Flex break-even = 2-Power).
- 4-Power is dominated.
- Usable structures: P3, F4, F5, F6, P5, P6.

## 3. Pipeline (The Odds API v4)
- Props: GET /v4/sports/{sport}/events/{eventId}/odds?apiKey=&regions=&markets=&oddsFormat=american[&includeMultipliers=true]. Get event ids from /v4/sports/{sport}/events.
- Cost = unique markets RETURNED x regions. An empty response costs nothing. Starter plan: free, 500 credits/month, "most bookmakers" (paid plans get all of them), so check that pinnacle and prizepicks actually come back on the free key.
- NFL/NCAAF/CFL base keys: player_pass_yds, player_pass_tds, player_pass_attempts, player_pass_completions, player_pass_interceptions, player_pass_longest_completion, player_rush_yds, player_rush_attempts, player_rush_longest, player_rush_tds, player_receptions, player_reception_yds, player_reception_tds, player_reception_longest, player_rush_reception_yds, player_pass_rush_yds, player_pass_rush_reception_yds, player_anytime_td, player_tds_over, player_1st_td, player_last_td, player_field_goals, player_kicking_points, player_pats, player_sacks, player_solo_tackles, player_tackles_assists, player_assists, player_defensive_interceptions, player_pass_yds_q1. The _alternate variants are listed for NFL only.
- Bookmakers:
  - us_dfs = prizepicks, underdog, pick6, dabble_us_dfs. PrizePicks demons and goblins appear in the _alternate markets; goblins carry default odds and demons +100. DFS odds are "indicative only".
  - pinnacle is in eu (scraped from its public site, so it may lag).
  - us_ex (kalshi, novig, prophetx, polymarket_us, betopenly, ...) is a further low-vig source.
- Budget: us_dfs + us + eu with 3 markets = 9 credits per event, about 55 pulls per month. Two regions (us_dfs + us) = 6 credits, about 80 pulls. Pull about 1 hour before kickoff, cache, and never refresh a prop pull.

Devig (two-way, q_i = 1/d_i, S = q_o + q_u):
- multiplicative q_o/S
- additive q_o - (S-1)/2
- power: solve q_o^k + q_u^k = 1
- Shin: find z in (0, 0.5) such that sum_i p_i = 1, where p_i = [sqrt(z^2 + 4(1-z) q_i^2/S) - z] / (2(1-z)). Verified locally: for two outcomes this equals additive (1.80/2.10 gives 0.53968 both ways).
- Use the worst-case (min) value for qualification. The methods differ by less than 1 percentage point near -110.
- Consensus: weighted log-odds average (Pinnacle about 0.4).
- Line mismatch: shift to the PrizePicks line with Normal/Gamma (yards) or Poisson/NegBin (counts).
- Qualify a pick when p ≥ break-even + 2 points (market-anchored) or + 3.5 points (own model).

## 4. Exact entry EV
- Power: M_n · prod p_i - 1.
- Flex: Poisson-binomial DP over the payout table.
- Greedy top-n is optimal without constraints, because the multipliers are non-decreasing in hits. With constraints (≥2 teams, 1 pick per player, correlation rules), brute-force C(N,n) for N ≤ 25.

## 5. Correlation
Unchanged: estimate residual correlations from nflverse, run a Gaussian-copula Monte Carlo, and default to 1 pick per game. PrizePicks says correlated picks "carry altered payout rates".

## 6. Kelly (re-computed by grid search; all match)
Full Kelly at p=0.58: F5 6.64% (G* 0.78%), F4 5.99% (0.48%) [previously omitted], F6 2.96% (0.50%), P3 3.41% (0.28%), P5 1.65% (0.24%), P6 1.17% (0.22%). Rank by G*. Use 1/4 Kelly, a 2% cap per entry and a 10% daily cap.

## 7. Own-model inputs
- nflreadpy: load_player_stats(seasons, summary_level in {"week","reg","post","reg+post"}) returns Polars. Also load_pbp, load_schedules, load_snap_counts, load_injuries, load_depth_charts, load_nextgen_stats, load_ff_opportunity, load_participation and load_ftn_charting all exist.
- CFBD (base https://api.collegefootballdata.com, Bearer key, free tier 1,000 calls/month per the v2 announcement; confirm with GET /info):
  - /games/players (year, week, team, conference, classification, seasonType, category, id)
  - /stats/player/season (year required)
  - /player/usage (year required): usage shares useful for CFB volume models
  - /lines (game lines only, no player props)
  - All routes verified in the live OpenAPI spec. There is no request throttling, but Cloudflare can block bursts.

## 8. Arbitrage (expanded; the user explicitly asked for profit-checking arbs)
- Cheapest real arbs are game lines. GET /v4/sports/{sport}/odds?markets=h2h,spreads,totals&regions=us,us2,us_ex costs 3 markets x 3 regions = 9 credits for the WHOLE slate, versus 9 per event for props. Run this first.
- Arb exists when sum 1/d_best < 1. Stake s_i = B(1/d_i)/sum(1/d). Profit = B/sum(1/d) - B.
- Check spread and total middles (L1 < L2) using a margin distribution.
- Prop arbs: same test on per-event pulls already made for picks, at no extra cost.
- PrizePicks entries cannot be one leg of an arb. Report them as "edge vs fair", never as risk-free.
- Note book limits, odds staleness (use last_update; skip quotes older than about 60s) and exchange commission (novig/prophetx/kalshi fees) in the profit calculation.

## 9. SMS alert
To 5555550100. Include:
- structure, picks (player/stat/line/O-U/p_fair/books)
- EV%, P(≥1x), G*, paper stake
- data timestamp
- "confirm multiplier in app"
- an Arena/jurisdiction caveat
Arb alerts: books, prices, stakes, guaranteed profit $ and %, and quote age.

## Recommendations
1. Payout table in config, including flex hit tiers and revert rules (3-Flex→2-Power). Override per lineup with includeMultipliers from us_dfs/prizepicks where available. Treat fixed-multiplier EV as an upper bound because PrizePicks may be Arena-only.
2. Allowed structures: P3, F4, F5, F6, P5, P6. Rank by Kelly growth G*; F5 and F4 lead at modest edges. Never F2, F3 or P4.
3. Credits: run the slate-wide game-line arb scan first (h2h, spreads, totals across us, us2, us_ex: 9 credits per slate). Then do prop pulls at 6-9 credits per event for 5-8 chosen games per week. Use /events for ids, cache, and never refresh prop pulls. Check on the first call that the free key actually returns pinnacle and prizepicks.
4. Devig with power and worst-case (Shin is the same as additive for two-way markets). Qualify picks at break-even + 2 points (market-anchored) or + 3.5 points (own model).
5. Model DNP and tie reverts in the simulator's settlement, including the 2-pick refund and the same-team refund that applies to DNPs only, not ties.
6. Arb module: compute profit net of exchange commissions. Reject quotes older than about 60s by last_update. Alert with stakes and guaranteed $ profit. PrizePicks entries are reported only as edge, never as arb.
7. CFB own model uses CFBD /games/players, /stats/player/season and /player/usage, checking quota via /info. NCAAF goblins and demons may not be available from the Odds API.
8. SMS to 5555550100 should carry an "in-app multiplier and availability (CA/Arena) not verified" caveat, since the number is a California area code and California treats DFS as illegal per the 2025 AG opinion.

# odds-api-props-arb: The Odds API v4 player props, us_dfs region (PrizePicks/Underdog), cross-book arbitrage detection, and a monthly credit budget for the free tier (fact-checked 2026-10-20)

VERDICT: I re-fetched the official pages on 2026-10-20 and the original section is substantially correct. I found one critical omission (`includeMultipliers`), one gap I have now filled (the PrizePicks payout tables), and a few small clarifications. Corrected full section below.

1) FREE TIER QUOTA: VERIFIED
- Starter (Free) is 500 credits/month and covers "Most bookmakers", all sports and all markets. Paid tiers: 20K credits for $30, 100K for $59, 5M for $119, 15M for $249 (https://the-odds-api.com/).
- Clarification: the pricing comparison on the homepage has a "Historical Odds" row, and my fetch tool could not tell me whether the free plan is ticked in it. The v4 docs are explicit, though: each historical endpoint "is only available on paid usage plans." So, as the original said, plan on no historical data on free.
- Historical costs, for reference:
  - Historical odds: 10 x markets x regions.
  - Historical event odds: 10 x unique markets returned x regions.
  - Historical events: 1 credit, and 0 if nothing is found.
- Headers x-requests-remaining, x-requests-used and x-requests-last are verified.
- Omission: rate limiting exists. Going over it returns HTTP 429, and the docs say to space requests "over several seconds". The client needs backoff on 429.

2) ENDPOINT COSTS (v4): VERIFIED
- /sports and /events are free.
- /odds costs 1 per region per market. If no events come back, it costs nothing.
- /events/{id}/odds costs unique markets RETURNED x regions. The doc example is 5 requested, 2 returned, cost 2. Exact quote: "Responses with empty data do not count towards the usage quota."
- /scores costs 1, or 2 with daysFrom (integer 1-3). Without daysFrom it returns only live and upcoming games. With it, completed games from up to 3 days back are included.
- /events/{id}/markets costs 1. /participants costs 1.
- Bookmakers parameter, verified: "Every group of 10 bookmakers is the equivalent of 1 region", so 11-20 books count as 2 regions. Mixing us, us2 and us_dfs keys inside one 10-book group costs 1x.
- oddsFormat: the docs warn that american "might" show small rounding discrepancies, so use decimal.
- Optional flags, verified: includeLinks, includeSids, includeBetLimits (mainly exchanges), includeRotationNumbers, commenceTimeFrom/commenceTimeTo (no effect when sport=upcoming), eventIds.
- CRITICAL OMISSION: includeMultipliers=true. It is "For US DFS sites": the response then "will include the multipliers in each bet selection outcome if available" (default false). Use it on every us_dfs call. It means the app does NOT need to treat the DFS price as pure noise:
  - It can read PrizePicks goblin/demon and Underdog/Pick6 multipliers directly.
  - It can price _alternate selections correctly.
  - No extra cost is documented for it.
  - The docs give no sample of the field, so parse it defensively and log the raw JSON on first use.

3) PLAYER PROP MARKET KEYS: VERIFIED
- All 33 keys under "NFL, NCAAF, CFL Player Props" are exactly as listed in the original section.
- The *_alternate keys appear under "Alternate NFL Player Props" only. They cover X+ milestone lines and lines books label "alternate", and there are 26 of them, including player_pass_yds_alternate, player_rush_yds_alternate, player_reception_yds_alternate and player_receptions_alternate.
- Props are fetched one event at a time via /events/{eventId}/odds. Coverage is "mainly limited to US sports and US bookmakers".
- Sport keys are americanfootball_nfl and americanfootball_ncaaf.
- NCAAF prop thinness is an inference, not a documented fact. It still makes sense to check market availability before committing NCAAF budget.

4) REGIONS / BOOKMAKERS: VERIFIED
- us: betonlineag, betmgm, betrivers, betus, bovada, draftkings, fanduel, lowvig, mybookieag. Paid-only: williamhill_us, fanatics.
- us2: ballybet, betanysports, betparx, espnbet, fliff, hardrockbet, hardrockbet_az, hardrockbet_fl, hardrockbet_oh. Paid-only: courtside, rebet.
- us_dfs: prizepicks, underdog, pick6, dabble_us_dfs. None are paid-only, so they are available on free.
- us_ex: betopenly, kalshi, limitless, novig, polymarket, polymarket_us, prophetx. None are paid-only.
- Betr is listed only as betr_au.
- DFS notes, verified verbatim: "Odds on DFS sites can vary based on user selections, therefore odds are indicative only."
  - PrizePicks demons and goblins sit under _alternate markets. Goblins get the default odds and demons get +100.
  - Pick6 and Underdog non-x1 multipliers sit under _alternate.
  - There is no note for Dabble.
- Correction to the original's guidance: use `point` as the PrizePicks line, and use includeMultipliers (not `price`) for any multiplier information.

5) ARBITRAGE MATH: VERIFIED (re-computed)
- d_a = 2.10 and d_b = 2.05 give S = 0.96399, ROI = 3.73%, stakes $49.40 / $50.60, and a return of about $103.74 on each leg (the original's $103.73 is a rounding difference).
- The rest stands:
  - Equal and opposite points only for spreads and totals.
  - Mismatched points are a "middle", not an arb. Never call a middle risk-free.
  - Flag the NFL tie risk on h2h.
  - Apply exchange commission: d_eff = 1 + (d - 1)(1 - c).

6) FALSE POSITIVES / STALE LINES: SOUND (heuristics, not doc facts)
- The last_update fields on bookmakers and markets exist.
- The thresholds (10 and 15 minutes staleness, minimum ROI about 1%, ROI above 5-8% treated as suspect) are design choices, not documented values. Keep them configurable.
- New point: with free-tier snapshots spaced hours apart, arb alerts are effectively theoretical. The SMS must say "verify the lines before placing".

7) PRIZEPICKS + "ARBITRAGE" FOR PROPS: GAP FILLED WITH VERIFIED PAYOUTS
- Strict S < 1 arbitrage is impossible against fixed-multiplier pick'em. Use a line-discrepancy / devig +EV approach instead.
- Current standard payouts come from the PrizePicks Playbook article dated 2026-09-18, which announced 2-pick Flex (https://www.prizepicks.com/playbook-article/2-pick-flex-plays-on-prizepicks-hit-one-pick-get-paid).

| Picks | Power (all hit) | Flex: all hit | Flex: 1 miss | Flex: 2 misses |
|---|---|---|---|---|
| 6 | 37.5x | 25x | 2x | 0.4x |
| 5 | 20x | 10x | 2x | 0.4x |
| 4 | 10x | 6x | 1.5x | - |
| 3 | 6x | 3x | 1x | - |
| 2 | 3x | 2x | 0.5x (new 2026-09) | - |

- Correction: the original's Power break-even (1/m)^(1/n) is right for Power, but Flex needs a binomial EV solve. Computed per-pick break-evens:

| Picks | Power | Flex |
|---|---|---|
| 2 | 57.7% | 61.8% |
| 3 | 55.0% | 57.7% |
| 4 | 56.2% | 55.0% |
| 5 | 54.9% | 54.2% |
| 6 | 54.7% | 54.2% |

- So 5-pick and 6-pick Flex have the lowest per-pick hurdle, and 2-pick Flex is the worst.
- The article says lineups with Demons/Goblins, correlated picks or other discounted projections "carry altered payout rates", and tells players to confirm the payout on the lineup screen. The multiplier table should stay user-editable, defaulting to the values above, and the SMS should tell the user to confirm the payout in the app.
- Third-party sources (e.g. WSN, Stokastic) and older help pages disagree on the 3-pick and 4-pick tiers (some list 3-pick Power at 5x and 4-pick Flex at 5x). The 2026-09-18 PrizePicks article is the most recent first-party source. The Intercom "payouts explained" page now returns 404.
- Unverified in this pass: PrizePicks entry-construction rules, such as whether picks must come from at least 2 different teams. Enforce whatever the app shows and treat these as config.

8) MONTHLY BUDGET: ARITHMETIC VERIFIED
- Line items: A 9 credits/wk is about 39, B 6/wk about 26, C 30/wk about 129, D 20/wk about 86, E 12/wk about 52, F 20, G 40. That sums to 392, leaving a 108 reserve, for 500 total.
- Clarification: in row A, "9/wk" means 9 credits per week (3 calls x 3 credits), not 9 calls.
- Caveats:
  1. C and D cover only 6 of roughly 14-16 NFL games a week, which is a coverage limit to tell the user about.
  2. Adding includeMultipliers costs nothing, but adding the _alternate markets roughly doubles prop cost, so keep them off on free.
  3. Not every month is the same: September and October can have 5 Sundays. The reserve covers this.
  4. Plan a 0-credit preflight. Pull /events for free, then only request event odds within about 24 hours of kickoff, when prop markets actually exist; empty responses cost 0.
- The upgrade path is unchanged: 20K credits for $30/month.

## Recommendations
1) Keep the original design: free /events first, one /odds call per sport with a 10-book list (3 credits per slate), oddsFormat=decimal and includeLinks.
2) NEW: add includeMultipliers=true to every event-odds call that includes us_dfs books. Parse it defensively, since the docs show no sample of the field, and log the raw JSON on first use.
3) Add HTTP 429 handling with exponential backoff, and space calls a few seconds apart.
4) Seed the PrizePicks payout config with the 2026-09-18 first-party table (Power 3/6/10/20/37.5x; Flex 2:2/0.5, 3:3/1, 4:6/1.5, 5:10/2/0.4, 6:25/2/0.4). Keep it user-editable.
5) Pick Power or Flex by binomial EV, not by the (1/m)^(1/n) rule. Per-pick break-evens are about 54.2% for 5-6-pick Flex and about 54.7% for 6-pick Power. Only build entries where every pick's devigged probability clears the hurdle with a margin of at least 2 points.
6) Exclude correlated or same-game picks, and goblins/demons unless includeMultipliers supplies the real multiplier. The SMS should say "confirm the payout on the lineup screen".
7) Keep the CreditBudget guard with caps of 39/26/129/86/52/20/40 and 108 in reserve. Only request props within about 24 hours of kickoff, since empty responses are free.
8) Every SMS for an arb or middle should be labeled "theoretical at snapshot — verify lines before placing", with leg-level stakes, books and links.
9) Tell the user about the limits: the free tier gives no historical data and no real-time arbitrage, and prop coverage is about 6 NFL games a week. The upgrade is $30/month for 20K credits.

# sms

VERIFICATION SUMMARY (2026-10-20). Most of the draft holds up. I made six material corrections or additions; they are marked [CORRECTED] or [ADDED] in the section below.
(a) Messages scripting on this Mac: I read the AppleScript dictionary on the user's own Mac (macOS 26.3.1, build 25D2128). It defines `account`, `participant`, `service type` with the values SMS, iMessage and RCS, and `send <text> to <participant|chat>`. `buddy` is not gone: it is an accepted synonym for `participant`. Error -10002 came from naming the service with `service "SMS"`, not from using `buddy`.
(b) There is a third service type, RCS. Texts to Android phones that go through an iPhone may now travel as RCS instead of SMS.
(c) Twilio's current trial does not allow custom message text. Per the "Try out SMS" doc: "You won't be able to write your own message"; API requests must use a template such as sms_2fa. The trial cannot send PrizePicks alerts at all.
(d) ntfy.sh's free public server allows 250 messages per day per visitor, plus a 60-request burst that refills one request every 5 seconds. That is enough for this app.
(e) Pushover's 10,000 messages per month is a shared account quota, not a per-app one. Emergency retries stop after 50 regardless of expire, so retry=30 lasts only about 25 minutes. Messages are limited to 1,024 characters.
(f) TextBelt's US prices are not published in static text; they load only after you pick a region. "About $3" comes from a 2019 Indie Hackers post, so treat it as unverified.

GOAL: A local Python app (Textual, running on the user's Mac, which runs macOS 26.3.1 Tahoe) texts the user's own US number, the user's own number. The text says which PrizePicks entry to place (up to 6 picks). The same channel announces arbitrage opportunities. The app never places bets.

RANKED OPTIONS

1) macOS Messages through `osascript`. Free, no account needed, and the text arrives on the phone number.
- How it works: AppleScript tells Messages to send to a participant of an account, chosen by `service type`. On this Mac's dictionary the valid service types are SMS, iMessage and RCS.
  - iMessage works if the number is an iPhone's iMessage address.
  - Green-bubble SMS or RCS needs an iPhone with Text Message Forwarding enabled for this Mac. Macs cannot send carrier SMS on their own.
  - An Apple Community thread from July 2026 (Tahoe 26.5.1/26.5.2) describes Mac-to-Android sends failing even with forwarding set up, and remains unresolved. Do not count on the SMS path; test it.
- Code. I checked these terms against the Messages.sdef on macOS 26.3.1:
```python
import subprocess
SCRIPT = '''
on run argv
  set targetNumber to item 1 of argv
  set msg to item 2 of argv
  set svcName to item 3 of argv
  tell application "Messages"
    if svcName is "SMS" then
      set svc to 1st account whose service type = SMS
    else if svcName is "RCS" then
      set svc to 1st account whose service type = RCS
    else
      set svc to 1st account whose service type = iMessage
    end if
    send msg to participant targetNumber of svc
  end tell
end run'''
def send_imessage(number: str, text: str, service: str = "iMessage") -> None:
    subprocess.run(["osascript", "-e", SCRIPT, number, text, service], check=True, timeout=30, capture_output=True)
```
  Pass the number and text as argv, never string-interpolated into the script. `buddy` still works as a synonym for `participant` on 26.3.1. The old -10002 "bad key" error came from writing `service "SMS"` by name, so look the service up by `service type` instead.
- Pitfalls:
  - The Mac must be awake and logged in, with Messages signed in.
  - The first run triggers an Automation prompt, and macOS grants it to the host app (Terminal, iTerm, or VS Code), not to Python. Re-grant it under System Settings > Privacy & Security > Automation if you change terminals.
  - [ADDED] Sending only needs Automation permission. Reading `~/Library/Messages/chat.db` to confirm delivery would also need Full Disk Access.
  - Texting your own Apple Account number may land in a note-to-self thread. It may not make a notification sound on the iPhone, because iMessage treats it as your own message. Test this first, and if it stays silent, use push (option 3 or 4) as the main alert.
  - There is no delivery receipt. osascript exits 0 even when the message ends up "Not Delivered".
  - Scripting behavior changes between macOS releases. Re-test after each OS update.

2) TextBelt (textbelt.com). The simplest true SMS API, with no carrier registration on your side.
- Free key: `key=textbelt` gives "1 free text per day" (verified on the homepage). Good only for a smoke test.
- Paid: a one-time charge with no subscription, and unused texts are refunded within 30 days (verified on /purchase). [CORRECTED] Prices appear only after you select "US/Canada" on the page. The "as small as $3" figure comes from a 2019 Indie Hackers post and is unverified for 2026.
- Restrictions: SMS with URLs need extra permission through textbelt.com/whitelist/. Messages come from shared numbers.
- API (verified):
  - POST https://textbelt.com/text with phone, message and key. The response contains success, quotaRemaining and textId, or error.
  - GET /status/<textId> returns DELIVERED, SENT, SENDING, FAILED or UNKNOWN.
  - GET /quota/<key> returns the remaining quota.
```python
r = requests.post("https://textbelt.com/text", data={"phone": "5555550100", "message": msg, "key": TEXTBELT_KEY}, timeout=15)
j = r.json()
if not j.get("success"): raise RuntimeError(j.get("error"))
```

3) ntfy.sh push. Free with no signup. This is app push, not SMS.
- `requests.post("https://ntfy.sh/<random-topic>", data=msg.encode(), headers={"Title":"Place entry","Priority":"high","Tags":"football"})`
- Priority values run 1-5. 5 ("max"/"urgent") gives long vibration bursts and a pop-over notification, which suits arbitrage alerts.
- [ADDED] Free ntfy.sh limits: 250 messages per day per visitor (IP), a 60-request burst refilling 1 request every 5 seconds, and messages up to 4,096 bytes. Going over the limits repeatedly can get the IP temporarily banned.
- The topic name works as a password. Use 32 or more random characters from letters, digits, _ and -, up to 64 characters.
- Paid tiers (verified):

| Tier | Price | Messages/day | Emails/day | Reserved topics | Phone calls |
|---|---|---|---|---|---|
| Supporter | $5/mo | 2,500 | 50 | 3 | 3 |
| Pro | $10/mo | 20,000 | 250 | 10 | 20 |
| Business | $20/mo | 50,000 | 500 | 50 | 50 |

  The page also shows struck-through prices of $6, $12 and $25. Phone calls are paid-tier only.

4) Pushover. $4.99 one-time per platform (iOS, Android and Desktop are separate purchases), with a 30-day free trial.
- [CORRECTED] The 10,000 messages per month (25,000 for Teams) are shared by every app on the account and reset on the 1st at 00:00 Central Time.
- Response headers: X-Limit-App-Limit, X-Limit-App-Remaining and X-Limit-App-Reset. Going over the quota returns HTTP 429.
- Emergency priority=2 needs retry>=30 and expire<=10800. [ADDED] Retries are capped at 50, so retry=30 stops after about 25 minutes.
- Messages are limited to 1,024 characters and titles to 250.
- API: POST https://api.pushover.net/1/messages.json with token, user and message.

5) Twilio. Not viable for this use without paying and registering.
- [CORRECTED/ADDED] The current trial ("Try out SMS") does not allow custom bodies: "You won't be able to write your own message." API sends must use preset templates such as sms_2fa or sms_order_confirmation, so a trial cannot carry pick details.
- Other trial limits: sends go only to verified US numbers (up to 5; the sign-up number is verified automatically), the trial expires after 30 days, and you get product-specific free units rather than a dollar balance.
- Trial messages carry a "Sent from a Twilio Trial account" prefix. Older help pages word it "Sent from your Twilio trial account -".
- A2P 10DLC registration requires a paid account.
- Toll-free numbers must be verified before they can text US/Canada, and verification is free to file. Unverified toll-free traffic has been blocked since Jan 31, 2024 with error 30032.
- Net: upgrade to paid, buy a number, and wait days to weeks for verification.

6) Carrier email-to-SMS gateways. Do not use them.
- AT&T and Cricket (@txt.att.net, @mms.att.net, @txt.cricket.net, @mms.cricket.net) shut down June 17, 2025. Confirmed by a Princeton OIT notice and Android Authority.
- Verizon's own page says the vtext/vzwpix shutdown should be "completed by 03/31/2027" and that some senders may lose access earlier.
- T-Mobile @tmomail.net has been reported dead since about Nov-Dec 2024. These reports come only from SMS vendors, and T-Mobile has made no official statement.

MESSAGE FORMAT:
- Use GSM-7 characters only. One emoji or smart quote switches the message to UCS-2, which cuts a segment from 160 characters to 70 (67 when concatenated).
- TextBelt and Twilio may bill per segment.
- A 6-pick entry takes about 2-3 segments of 153 characters, so use a compact form.
- Examples:
  - "PP 6-PICK FLEX $10 | EV+8.2% | 1) Mahomes PassYds O 262.5 2) J.Jefferson RecYds O 78.5 ... | lines 13:05 ET"
  - "ARB +2.1% | KC@BUF ML: KC +135 DK $42.55 / BUF -125 FD $57.45"
- Always include the time the lines were pulled and the exact lines, so you can see at the book whether a line has moved.
- Leave URLs out of TextBelt messages.

## Recommendations
PRIMARY: macOS Messages through osascript using the service-type lookup verified on this Mac. Prefer `service type = iMessage`, and pass the number and text as argv with a 30-second timeout.
- On first run, the Textual UI's "Send test" action must confirm that the iPhone actually sounds an alert. Self-addressed iMessages can arrive silently as note-to-self. If no alert sounds, make push the primary channel.

STRONGLY RECOMMENDED SECOND CHANNEL: ntfy.sh, free (250 messages per day), with a 32+ character random topic.
- Use Priority 5 for arbitrage and 4 for PrizePicks entries.
- Pushover ($4.99 one-time) is a more polished alternative. Use priority 2 for arbitrage, with expire set to about 600 seconds, since an arb is dead within minutes anyway.
- Push works whether or not the phone is an iPhone, and gives a reliable alert sound.

SMS FALLBACK: TextBelt with a paid key. Check the US price on /purchase first; do not rely on the $3 figure.
- Trigger it when osascript fails or times out.
- Keep URLs out of the text.
- Check `success` and `quotaRemaining`, and optionally poll /status/<textId>.

AVOID:
- Twilio. The trial cannot send custom text, and paid use needs A2P 10DLC or Toll-Free Verification.
- All carrier email-to-SMS gateways.

IMPLEMENTATION:
- Define a Notifier protocol (`send(text) -> NotifyResult`) with OsascriptNotifier, NtfyNotifier, PushoverNotifier, TextbeltNotifier and ConsoleNotifier (dry-run).
- Wrap them in a FallbackNotifier chain, and fan arbitrage alerts out to SMS plus push at the same time.
- Keep config in .env: PHONE_E164=+15555550100, NTFY_TOPIC, TEXTBELT_KEY, PUSHOVER_TOKEN and PUSHOVER_USER.
- Dedupe alerts by a hash of the legs plus lines, with a cooldown.
- Rate-limit sends and set quiet hours, exempting arbitrage alerts.
- Log every attempt to SQLite next to the paper ledger.
- Use GSM-7 text only, and include the lines and the time they were pulled.
- Re-test the AppleScript after every macOS update.

