# Gridiron Edge

A terminal app that looks for +EV football bets on Kalshi and PrizePicks, texts you when it finds one,
and tracks a **paper** bankroll. It never places bets and never logs in to Kalshi or PrizePicks. You
place any bet yourself, then confirm it in the app so the paper ledger matches.

## Setup

```sh
uv sync
cp .env.example .env    # then fill it in (see below)
uv run gridiron-edge    # optional: --db PATH to use another database file
```

`.env` keys:

| Key | Needed for | Without it |
|---|---|---|
| `ODDS_API_KEY` | sharp sportsbook prices, PrizePicks lines (the-odds-api.com, free tier) | no fair prices, no PrizePicks lines |
| `THERUNDOWN_API_KEY` | live scores and line watching (therundown.io, free tier) | no live scores |
| `CFBD_API_KEY` | college teams and the CFB model (collegefootballdata.com, free) | no CFB |
| `NOTIFY_PHONE` | iMessage/SMS texts through Messages.app | texts show only in the event log |
| `NTFY_TOPIC` | optional push fallback via ntfy.sh | — |
| `KALSHI_BASE_URL` | optional; Kalshi's public market data needs no key | — |

The app starts with any key missing. It prints what is disabled, and the event log says so too. On
the first run it logs a check of each source: which books the Odds API key returns, TheRundown's tier
and delay, CFBD calls left, and whether Kalshi is reachable. Then run `notify test`.

### Messages Automation permission (macOS)

Texts go out through Messages.app with `osascript`. The first `notify test` makes macOS ask whether
your terminal may control Messages. Allow it. If you missed the prompt, open **System Settings >
Privacy & Security > Automation**, find your terminal app (Terminal, iTerm, Ghostty...), and turn on
**Messages**. If your phone still does not alert, set `NTFY_TOPIC` in `.env` and subscribe to that
topic in the ntfy app.

## Commands

Type in the command bar at the bottom; it autocompletes.

| Command | What it does |
|---|---|
| `deposit <amt>`, `withdraw <amt>` | change the paper bankroll (`250`, `$1,000.50`) |
| `confirm <id> [price] [qty]` | record that you placed recommendation `<id>` (or press Enter on its row) |
| `skip <id>` | dismiss a recommendation |
| `scan [games\|props\|pp\|arb\|all]` | look for opportunities now |
| `refresh [nfl\|cfb] [--force]` | re-pull Kalshi and TheRundown; `--force` also pulls sportsbook odds and shows the credit cost |
| `bets [open\|settled]` | list bets |
| `settle <id> <won\|lost\|refund> [payout]` | settle a bet by hand (a won PrizePicks entry needs the payout) |
| `pp add "<player>" <stat> <line> <team> <game_id>` | enter a PrizePicks line by hand |
| `quota` | API credits left |
| `notify test`, `notify on`, `notify off` | test or toggle texts |
| `set <field> <value>` | change a strategy setting, e.g. `set kelly_fraction 0.2` (saved in the database) |
| `venues` | show the bettable venues (`set venues kalshi` to drop PrizePicks) |
| `backtest <nfl\|cfb> <yyyy>-<yyyy>` | walk-forward ATS backtest of the model |
| `fit blend` | refit the auto-gate now (it refits weekly on its own) |
| `report` | ROI, CLV, record, drawdown, Brier |
| `help` | list commands |

## How it decides

- Fair prices come from no-vig Pinnacle (then other sharp books), shifted to Kalshi's lines with margin
  distributions, blended with our own NFL/CFB ratings.
- A Kalshi contract is recommended only after fees when EV ≥ 2.5%, the edge is ≥ 1.5 points, and the
  price is between 0.10 and 0.90. Sizing is quarter Kelly with per-bet, per-game and per-slate caps.
- A league whose backtested model has not shown real skill is **observe-only**: its picks are logged
  for CLV but never texted.
- The same opportunity is texted once. Quiet hours (23:00–08:00 PT) hold texts, except arbitrage.

## Known limits

- **Free tiers.** The Odds API free key has about 500 credits a month, so sharp lines are pulled a few
  times a week and props for only about 8 games a week. PrizePicks lines may not be on your key; if
  the first-run check says so, use `pp add`.
- **Delayed scores.** TheRundown's free scores run about 5 minutes behind.
- **Maker mode is not built.** Every Kalshi recommendation assumes you take the ask (taker fee);
  resting orders are not modelled.
- College props and college prop settlement are manual (`settle`).

## Disclaimer

This is a simulation and research tool. It tracks paper money only and gives no guarantee of profit;
most bettors lose. Recommendations can be wrong, prices move, and data feeds lag. You are responsible
for checking that sports betting and prediction-market trading are legal where you are, that you are
of legal age, and for every bet you choose to place. If gambling stops being fun, call
1-800-GAMBLER.
