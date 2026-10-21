# Gridiron Edge: quant + UX upgrade — design

Date: 2026-10-20. Builds on `2026-10-20-gridiron-edge-design.md` (still authoritative where this doc is silent).
Requirements source for the quant work: `research/quant-audit.md` (verified audit, six areas).

## Intent

The user wants to open the app and immediately see (1) bets they can place right now and (2) the bets
they already have, with every number computed the way a modern quant desk would, and every text and
screen readable as plain English by someone who is not a quant.

## User decisions

- **Layout: "focus mode" (option C).** Full-width Home tab: header line, PLACE NOW list, OPEN BETS list.
  Other tabs: 2 Performance (balance chart, ROI, closing-line value, record, calibration),
  3 Scores, 4 Log. Number keys switch tabs; command bar stays at the bottom on every tab.
- **"When possible to make" = actionable only.** PLACE NOW shows only recs whose market is open,
  before kickoff, not expired, and whose edge still clears the bar after fees. Stale recs drop off.
  Sorted by expected profit. Arbs flagged as "locked profit".
- **Empty state explains itself**, e.g. "No good bets right now. Next check in 3 min." or
  "Kalshi lines for the 1:00 PM games aren't open yet." Never a blank panel.
- **Full audit list**, including low-payoff items (usage x efficiency props, ladder middles, drift
  monitoring). "Do the full list quickly."
- **Plain readable English** in texts and on the dashboard.

## Home tab

- Header: `Bankroll $1,284.50  ▲ $42.10 today · $96 in open bets (7.5% of bankroll) · Odds API 412/500 left`.
- PLACE NOW row: id, venue, the bet in words, price, stake in dollars, expected profit in dollars,
  countdown ("12 min left"), kickoff time. Keys: Enter = confirm (existing modal), `s` = skip,
  `d` = "Why this bet?" panel.
- "Why this bet?" panel, plain English: our chance vs the price's chance, where our number came from
  (which sportsbooks, model share), fee, how the stake was sized (Kelly fraction, caps that bound it),
  when it goes stale.
- OPEN BETS row: the bet in words, stake, live score + quarter (if live), current market price and
  unrealised profit/loss for Kalshi, legs hit/pending for PrizePicks, kickoff + closing-line value
  when known.

## Plain-English rules (texts and dashboard)

- No jargon in user-facing strings: not "EV", "CLV", "fair", "YES @", "Brier", "x40", ticker codes.
  Use: "expected profit", "our chance", "price", "beat the closing price by", "accuracy score".
  Internal identifiers may appear only in the "Why this bet?" panel's last line.
- Use full team names (`gridiron_edge/data/nfl_teams.json` first name; CFB from the registry/CFBD
  school name) and describe contracts as outcomes: "Eagles win by 4 or more points",
  "Cowboys vs Eagles total over 44.5 points", "Jalen Hurts 2 or more passing touchdowns".
- Times in 12-hour PT ("12:55 PM").
- Texts are ASCII, under ~480 characters, one action per text, always end with
  "When placed, type: confirm <id>". Examples:
  - Kalshi: `Bet idea #41 (Kalshi): Buy 40 "Yes" contracts on "Eagles win by 4 or more vs Cowboys" at 47 cents. Cost $18.80 + $0.69 fee. We put this at 52%; the price implies 47%. Expected profit about $1.15. Place before 12:55 PM PT. When placed, type: confirm 41`
  - PrizePicks: `Bet idea #42 (PrizePicks 5-pick Flex, $10): 1) Jalen Hurts MORE than 1.5 pass TDs (61%) 2) ... Expected profit about $0.94. Check the app shows 10x / 2x / 0.4x payouts. Lines as of 12:40 PM. When placed, type: confirm 42`
  - Arb: `Locked profit #43 (Kalshi): Buy 20 "Yes" on Bills win AND 20 "Yes" on Dolphins win. Total $19.40 + $0.62 fees; you get $20.00 back whichever team wins (profit about $0.0X). Place both right away; prices move fast. When placed, type: confirm 43`

## Quant upgrades

Implement every item in `research/quant-audit.md` (fair-pricing, team-models, props, sizing,
prizepicks-arb, evaluation), as corrected by its "verify" sections, including the low-payoff items.
Where the audit lists a dropped claim, do not implement it. Where an item needs data the free tiers
cannot provide, implement the code path behind a setting and log that it is inactive.

Constraints carried from the base spec: free API tiers only and the existing budget guards; never
place bets; never scrape PrizePicks; phone number only in `.env`; integer cents in the ledger;
append-only ledger; Python 3.14 / textual 8.2.8 stack.

## Builder's calls (mine, not the user's)

- Ladder "middles" are a new rec kind `middle`, labelled "Not risk-free" in plain English, observe-only
  until its own CLV evidence passes the gate.
- Drift monitoring = rolling 50-bet CLV and calibration vs backtest; a warning line on Performance
  and in the log, plus auto-observe-only when rolling CLV < 0 with significance.
- Skipped the separate builder check and plan document at the user's request for speed; the per-area
  review and final whole-branch review cover it.
