# Quant methods audit (2026-10-20)

## fair-pricing

**Summary of current approach.** The code is in `/Users/cole03roberts/Desktop/SportsBet`; the Football folder is empty. All line numbers below are relative to SportsBet. Every book is devigged separately (`strategy/devig.py:13-35`): power for h2h, additive for spreads and totals (`fair.py:14`). `fair_two_way` (`fair.py:46-63`) returns Pinnacle if present, otherwise the mean of novig/prophetx, otherwise the median of every book. Kalshi is one of those books (`odds_api.py:11`, `therundown.py:19`). Quotes are dropped when the book's own `last_update` is more than 600 s old (`fair.py:27`, `pipeline.py:184`). The Kalshi spread and total fair value works in two steps:
1. `_implied_mean` takes the quoted point whose fair probability is closest to 0.5 and converts it to a mean with a linearised Normal(13) (`fair.py:66-85`). It calls `fair_two_way` without passing `stale_seconds`, so it always uses the default 600 s.
2. That mean is priced with the margin model's `p_greater`. For NFL spreads this is `EmpiricalMarginModel`, built from pooled, rounded nflverse residuals `result - spread_line` (`services.py:374-376`, `distributions.py:32-56`).

Kalshi moneyline uses the h2h no-vig probability directly (`pipeline.py:88-92`). Props take the nearest-line book, devig each side with the worst-case method and shift it to the target line (`pipeline.py:172-202`). Games that have kicked off are never priced (`pipeline.py:62-67`).

### Already sound

| Item | Evidence | Note |
|---|---|---|
| Devig methods for 2-way markets | `devig.py:13-35`, `fair.py:14` | Power for moneyline and additive for -110-style spreads and totals is standard (Clarke, Kovalchik & Ingram 2017). The auditor's NFL log-loss differences (~1e-4) are noise. |
| Totals and model-side moneyline sigmas | `distributions.py:10-13` | Normal totals are acceptable. `NFL_ML_SIGMA` is only used for P(win). |
| Prop worst-case devig | `pipeline.py:172-202` | Conservative and internally consistent: P(over)+P(under) ≤ 1. A multi-book median at the same line is an optional nice-to-have. |
| No in-play pricing | `pipeline.py:62-67` | Correct given the 5-min delay on the TheRundown free tier. |

### Ranked upgrades (impact / effort)

**1. Exclude Kalshi from its own reference set**
- **Verdict:** Confirmed, incorrect.
- **Evidence:** `REF_BOOKS` includes `kalshi` (`odds_api.py:11`) and `AFFILIATE` maps 25 to `kalshi` (`therundown.py:19`). `_book_probs` (`fair.py:38-43`) keeps every book, and the median branch (`fair.py:63`) includes Kalshi. With TheRundown's 4 books, Kalshi is one of the middle two in the median whenever its price sits in the middle of the four.
- **Practice:** The fair price must be independent of the venue being bet. This is basic and needs no source.
- **Impact:** Moderate. Real edges shrink toward zero, and a mispriced Kalshi line can partly validate itself. It matters more because of item 2, which makes the median branch the usual one.
- **Effort:** XS.
- **Change:** Skip `book in {"kalshi","polymarket"}` in `_book_probs`. Keep Kalshi quotes only for arb and ladder checks.

**2. Replace pooled, rounded residuals with a spread-conditional margin model**
- **Verdict:** Confirmed, incorrect.
- **Evidence:**
  - `distributions.py:40` uses `np.rint`, which is banker's rounding. Residuals ending in .5 go to the even neighbour, which inflates even residuals.
  - `distributions.py:45-46,51`: integer residuals plus a shift rounded to 0.5 means `p_greater(m, 3.5)` is constant for every m in [2.75, 3.75). I checked this by hand: shifts 3.0 and 3.5 both select residual ≥ 1.
  - `p_equal(m, 3)` is 0 whenever the shift is x.5, so push mass disappears at random.
  - Residuals pooled over all spreads put key-number mass in the wrong place, so the docstring "key-number mass survives" is false.
  - I did not re-run the auditor's walk-forward numbers (-4.6pp at 2.5, +3.1pp at 7.5). They are consistent with these mechanics.
- **Practice:** Use the empirical margin conditional on the closing spread (kernel or bucket weighting), the basis of practitioner half-point and push charts. Ballpark: a half point through 3 is worth about 4-5pp, through 7 about 3pp, and about 1.5pp at non-key numbers. I could not open the cited oddspapi URL, so treat its exact figures as unverified; the magnitudes match standard push charts.
- **Impact:** Large. It produces a systematic 3-5pp error at exactly the half-point lines Kalshi lists, against a 1.5pp edge filter and a 2.5% EV filter.
- **Effort:** S for NFL. CFB is S-M: it needs a CFBD lines history, and the claim of "0 extra calls" is unverified.
- **Change:** Add a `ConditionalMarginModel` with `P(M>x|s)` and `P(M=x|s)`, using Gaussian weights on |spread| with bandwidth about 1 pt, oriented to the favourite, fitted on 2015+ seasons, with integer margins and no rounding of the mean.

**3. Measure staleness by fetch time and anchor to the last sharp price**
- **Verdict:** Confirmed, incorrect.
- **Evidence:**
  - Pinnacle, novig and prophetx come only from the Odds API. It is pulled only at the 2 weekly anchors (early and mid) plus T-20 to T-5 min close pulls, and only for games with bets or active recs (`scheduler.py:143-173`).
  - Kalshi scans run every 3-30 min (`scheduler.py:125`).
  - `fair.py:27` drops quotes older than 600 s by the book's own timestamp, so sharp quotes are gone 10 minutes after each pull.
  - `Quote` has no fetch timestamp (`types.py:34-42`).
  - TheRundown uses `price.updated_at` (`therundown.py:195`). A price that has not changed is therefore also discarded after 10 min.
- **Correction to the auditor:** The consequence is twofold. Fair is often the soft-book median, and fair is often None, which means no rec. The second is fail-safe, not negative EV, so the "most recs are priced off the median" claim is plausible but not proven.
- **Practice:** Separate the age of our own data from market movement, and anchor to the last sharp no-vig price while invalidating it when the consensus moves. This is the standard Pinnacle-anchored screen practice.
- **Impact:** Large across all game markets. It restores the intended sharp tier at 0 extra credits.
- **Effort:** M.
- **Change:**
  - (a) Add `fetched_at` to `Quote` and test staleness on it.
  - (b) Build an anchored fair: the last sharp fair, shifted by the change in the no-vig median of the TheRundown books since that pull. Return None if that median moved more than about 1.5pp or crossed 3, 7 or 10.
  - (c) Gate or penalise recs whose source label is `median`.

**4. Invert the implied mean through a push-aware model and prefer exact sharp quotes**
- **Verdict:** Confirmed, incorrect.
- **Evidence:**
  - `fair.py:85` uses a linear Normal(13). The devigged probability at an integer line is P(cover | no push), but it is fed in as P(M > line).
  - `fair.py:72-79` chooses the point closest to 0.5 regardless of where its probability came from, so a median-sourced point can beat a Pinnacle point.
  - `fair.py:74` ignores `s.stale_seconds`.
  - The auditor's example (-3 at -125/+105 gives P(m>3.5) = 0.519 against a true ~0.42-0.47) is directionally right.
- **Practice:** Solve `P(M>L|s)/(1-P(M=L|s)) = p` on the same model, and use a sharp price directly when one exists at the target line or an adjacent half point.
- **Impact:** A further 2-5pp error when the main line sits on 3, 7 or 10 with off-standard juice.
- **Effort:** S, once item 2 exists.
- **Change:**
  - Use a `brentq` root on the conditional model.
  - In `_game_p_yes`, try `fair_two_way(quotes,"spreads",-line)` first.
  - Prefer points from the highest source tier over the point closest to 0.5.
  - Thread `stale_seconds` through.

**5. Sharp-book tiering and sources**
- **Verdict:** Partly confirmed, outdated.
- **Evidence:** `fair.py:58-63` is a hard Pinnacle, then exchanges, then median rule. Exchange quotes get no hold or width check. `REF_BOOKS` uses slots for espnbet and hardrockbet.
- **Corrections:**
  - LowVig and BetOnline are mid-tier, not Pinnacle-grade.
  - Swapping `REF_BOOKS` does not help scan-time freshness, because those books come on the same infrequent Odds API pulls. It only matters after item 3.
  - TheRundown's free-tier book list is unknown (`research/therundown.md:30`), so the "free sharp feed every 30 min" depends on `X-Bookmakers`.
- **Practice:** Use a tiered consensus with 1/hold weights, and drop exchange quotes with an overround above about 4-5%.
- **Impact:** Moderate, and only once item 3 is in place.
- **Effort:** S.
- **Change:**
  - Log `X-Bookmakers` and add affiliates 3, 6, 7 and 11 if they are allowed.
  - Swap in `lowvig`/`betonlineag`.
  - Add a hold cap on exchange quotes and label the tier in the source.

**6. NFL tie adjustment for Kalshi moneyline**
- **Verdict:** Confirmed, missing.
- **Evidence:** `pipeline.py:88-92` uses p and 1-p. `pipeline.py:163` only sets a flag. Kalshi's tie settlement is unconfirmed (`research/kalshi.md:79,140`).
- **Practice:** `P_kalshi = P(win | no tie) * (1 - P(tie))` if a tie settles both sides NO. P(tie) is about 0.3-0.5% per NFL game since 2012.
- **Impact:** Small, about 0.2-0.4pp, but always biased toward YES. It depends on what `rules_secondary` says.
- **Effort:** XS (read `rules_secondary`, one multiplier).
- **Change:** As the auditor proposed.

### Dropped or downgraded claims
- **Switch moneyline to odds-ratio devig:** dropped. The log-loss gain is about 1e-5 and the calibration gain about 0.3pp, on the auditor's own numbers. Not worth it.
- **Guard against negative additive results:** dropped. Additive is only used on spreads and totals near even money, where it cannot go negative in practice.
- **Props median across books:** not ranked, because it is up to standard. It is a cheap optional variance reduction.
- **Live-lines "stop recommending in the last ~5 min":** dropped. The close window already ends at T-5 (`scheduler.py:34`).
- **Item 3(c) "require an extra 2pp of EV for median":** downgraded. The 2pp figure is arbitrary; suppressing median-sourced recs or flagging them observe-only is more defensible.
- **"sigma=13 used twice":** folded into item 4. It goes away with that fix and has no separate impact.
- **Exact oddspapi figures (4.2pp / 3.25pp) and the walk-forward log-loss numbers:** marked as unverified, not dropped. I did not re-run them; the code mechanics behind them are confirmed.
- **Effort for item 2:** corrected from S to S (NFL) / S-M (CFB). The CFBD lines history is not shown to be cached.

## team-models

**Code location:** the package lives at `/Users/cole03roberts/Desktop/SportsBet/gridiron_edge`. `/Users/cole03roberts/Desktop/Football` contains only `.remember` and `.superpowers`. All line numbers below refer to the SportsBet tree. No files were edited.

**How it works now.** The NFL model (`models/nfl.py`) is a weighted ridge on home-minus-away margins: LAMBDA=8, a 120-day half-life, and a free HFA column. It is blended 0.6/0.4 with a shrunk EPA/play differential. The EPA only uses weeks that finished before `asof`, so there is no leakage. Totals are a decayed average of points-for and points-against, with no opponent adjustment.

The CFB model (`models/cfb.py`) is a ridge on margins capped at ±28, with 0.97 weekly decay. It is shrunk toward a `cfb_prior` built from last season's fit plus returning PPA and talent, with lambda = max(2, 20-2·week). CFB never produces a totals prediction (`services.py:710` passes `None`).

`pipeline._game_p_yes` (l.77-108) blends logit(model) with the devigged consensus. It always uses the hard-coded `default_weight` (0.25 NFL, 0.35 CFB, 0.125 inside 24h). CFB also requires |model−market| ≥ 2.5 pts. Gated leagues still produce recommendations, but they are marked observe-only (`pipeline.py:140`).

The gate (`blend.walkforward_ats` and `services.fit_gates`) is a weekly walk-forward ATS against the closing line. It regresses the cover outcome on logit p_model under a Normal(13 / 15.75) margin. It is gated when n<200 or z<1.96, and it is refit every 7 days (`GATE_REFIT`). The NFL test covers seasons cur-2..cur. The CFB test covers cur-1 only and is fit with `priors={}`. The result is copied to ml, spread and total.

### Already sound

| Item | Evidence | Note |
|---|---|---|
| Ridge-with-recency rating core | `nfl.py:36-56`, `cfb.py:32-55`, `ridge.py` | This is a standard approximation to dynamic state-space ratings. Replacing it would not move ROI against the close. Minor point: the EPA term is not opponent-adjusted. |
| Walk-forward date filtering and leakage | `blend.py:83-96` (Monday buckets, strictly-before refit); `nfl.py:85-89`; `services.py:175-183` | No leakage found. Pushes are excluded, and the benchmark is the closing line. |
| NFL CFB-style neutral handling in the CFB *training* data | `cfb.py:47` reads `neutralSite` from CFBD; `services.py:440` | Correct in both training and backtest. The bug is only at live prediction time (see #2). |
| Blend architecture (logit pool vs devigged consensus, EV at the Kalshi ask) | `pipeline.py:86-108` | Sound and matches brief1 §5.3. The weight values and the CFB filter are handled as separate items below. |
| Fail-closed gating | `services.py:488-500` | Backtest errors, or a model with no history, default to observe-only. |

### Ranked upgrades (by impact/effort)

**1. CFB prior: the returning-production term has the wrong sign because last-season ratings are centred at −20**
- **Verdict:** incorrect. Confirmed.
- **Evidence:** `services.py:401` fits last season with `priors={}`. Every team is then shrunk toward `UNKNOWN_PRIOR=-20` (`cfb.py:12,52`). The design is home-minus-away, so the mean is pinned to the prior mean. I re-ran the simulation: mean −20.0, max −5.7, min −36.4. `cfb.py:72` computes `0.25*p*(rp-0.5)`, and with every p < 0, a high RP lowers every team's prior. The best team with RP 0.9 vs 0.5 came out −0.57 pts. For an average team (p≈−20) the swing is about −2 pts at RP 0.9 and +2 at RP 0.1, so the spread between them is about 4 pts and inverted.
- **Practice and source:** SP+ and FPI regress toward the FBS mean, and returning production scales how much regression a team gets, not the sign of its rating (ESPN SP+ preseason methodology).
- **Impact:** High. Through lambda 20 down to 8 (weeks 0-6), this produces 1-4 pt errors that track RP. Those errors are close to the 2.5-pt `cfb_min_disagree` trigger, so they become bogus CFB bets.
- **Effort:** S.
- **Proposed change:** Centre first: `p_c = p - mean(p over teams with at least N games)`. Then compute `0.6*p_c + 0.25*p_c*(rp-0.5) + talent`. Express `UNKNOWN_PRIOR` relative to that centre. Add a unit test: a top team's prior must not fall as RP rises. Fitting the coefficients on past CFBD seasons is optional and can come later.

**2. Live neutral-site games always get HFA; NFL training treats neutral games as home games**
- **Verdict:** incorrect. Confirmed.
- **Evidence:** `types.py:25` defaults `neutral=False`. Neither `services.py:157` nor `clients/therundown.py:149` sets it, so `_predictions` (`services.py:707,710`) adds HFA to every live game. `SCHEDULE_COLS` (`clients/nflverse.py:8-9`) has no `location` column, so `nfl.py:49` `r.get("neutral")` is always None. The NFL backtest also omits neutral (`services.py:423`). CFB training and backtest are correct.
- **Practice and source:** brief1.md:228 says HFA should be "set to 0 at neutral sites". That is standard.
- **Impact:** High for CFB bowls, CFP and neutral kickoff games. The fake 2-3 pt swing alone nearly clears the 2.5-pt CFB disagreement filter. NFL impact is small: international games plus the Super Bowl, about 6-8 games a year.
- **Effort:** S.
- **Proposed change:** Add `location` to `SCHEDULE_COLS` and map `neutral = location == "Neutral"` into `nfl.py` and `_nfl_history`. On the live side, copy CFBD `neutralSite` (already fetched in `_fit_cfb`) and nflverse `location` onto `Game.neutral` in the registry. `matching.py` already stores the field.

**3. No guard against model/market disagreements that are really missing information (QB changes, injuries)**
- **Verdict:** missing. Partly corrected (see below).
- **Evidence:** the NFL model is team-level only. `SCHEDULE_COLS` drops the QB columns. Nothing caps large disagreements.
- **Correction to the auditor:** nflverse `home_qb_id`/`away_qb_id` are, as far as I know, reliably filled only for completed games, so they cannot detect *upcoming* starter changes. A QB veto needs `load_depth_charts` or `load_injuries` (also free). The cheaper and broader fix is the disagreement cap. brief1 §5.3 already notes that large disagreements are mostly model error.
- **Practice and source:** nfelo and 538 apply QB adjustments; FPI values a starting QB at about 3.3 pts (brief1.md:256).
- **Impact:** Medium-high once NFL ungates. A 5-pt disagreement at w=0.25 gives about 4pp of fictitious edge, which is more than the fee. This is adverse selection on exactly the games where the market knows more than the model.
- **Effort:** S for the cap; M for a QB veto from depth charts.
- **Proposed change:** If |model_mean − mkt_mean| exceeds about the 95th percentile of historical disagreement (roughly 2σ), set w=0 for that game. Later, add a QB-change veto from nflverse depth charts.

**4. NFL totals inherit the spread gate and use an unvalidated naive model**
- **Verdict:** incorrect. Confirmed, with a scope correction.
- **Evidence:** `services.py:506` copies the spread gate to `total`. `predict_total` (`nfl.py:110-113`) has no opponent, pace or weather adjustment. `total_line` is loaded but never backtested. The spec itself says gating is per "league and market" (brief1 §5.3).
- **Correction to the auditor:** CFB totals are never bet. `mean_total=None` makes the CFB branch return None, so the problem is NFL-only.
- **Impact:** Medium once NFL ungates. This is a naive model at 25% weight against an efficient market.
- **Effort:** S.
- **Proposed change:** Set `w=0` for NFL totals now, so they run as pure market (Kalshi vs books). Add a `walkforward_totals` on nflverse `total_line` and gate `('nfl','total')` separately later.

**5. CFB CLV is never recorded**
- **Verdict:** incorrect. Confirmed.
- **Evidence:** `_record_clv` passes `predictions={}` (`services.py:685`). The CFB branch in `pipeline.py:100-103` then always returns None, so every CFB bet is skipped at l.692-693.
- **Practice and source:** CLV against the devigged close is the standard early validation signal.
- **Impact:** Medium, on the risk side. It removes the fastest way to tell whether CFB recommendations, observe-only or live, are any good.
- **Effort:** S.
- **Proposed change:** Factor out a `market_p_yes` with no CFB disagreement check and use it in `_record_clv`. Add a regression test.

**6. CFB cannot bet pure Kalshi-vs-book edges (`cfb_min_disagree`)**
- **Verdict:** a new item. I moved it out of the auditor's "up_to_standard" blend item.
- **Evidence:** `pipeline.py:100-103` returns None unless the model disagrees by at least 2.5 pts. NFL falls through to `p_mkt` (l.104-105), but CFB never does.
- **Practice and source:** brief1.md:317 says "the edge comes from a specific book's price against the fair line". Large model disagreements are mostly model error.
- **Impact:** Medium, on the opportunity side. The most realistic edge, Kalshi mispricing against sharp books, is switched off for CFB. CFB bets are also concentrated in the riskiest case: large model disagreement while the model is gated or weak.
- **Effort:** S.
- **Proposed change:** When CFB is gated or the disagreement is below the threshold, return `p_mkt` (w=0) the way NFL does. Keep model disagreement as an additive bonus only after the CFB gate passes.

**7. The gate backtest does not reproduce the live model (CFB priors, cold starts)**
- **Verdict:** incorrect. Confirmed.
- **Evidence:** `services.py:456-460` fits CFB with `{}` priors over one season (`_gate_spans`, l.517). The first weeks raise LinAlgError, which returns 0.0, or are shrunk hard toward a common −20. NFL `sched` is filtered to the backtest years (l.417), so week 1 of the first season has no history. Production uses `cfb_prior` and 2+ seasons.
- **Impact:** Medium. Junk early-season predictions dilute the test toward gating, which is the safe direction. The prior-driven early-season CFB model is never validated.
- **Effort:** M.
- **Proposed change:** Build the season-Y prior exactly as `_fit_cfb` does, from Y-1 plus `/returning` and `/talent`, over 2-3 seasons. Past seasons cache forever. Add one burn-in NFL season that is not scored. Report results per season and early vs late.

**8. The fitted b_model is discarded and the fixed default weights are used instead**
- **Verdict:** incorrect against the spec, but the impact is overstated.
- **Evidence:** `pipeline.py:108` always calls `default_weight`. `services.py:502` only logs b_model. brief1 §5.3 calls 0.25/0.35 "weights before fitting", and design.md:235 says "weights are refit walk-forward".
- **Correction to the auditor:** with `logit(0.5)=0`, b_model is indeed the close-line blend weight. But a league only ungates when b/se ≥ 1.96. As a rough estimate, se ≈ 0.25-0.4 for NFL (n about 600, model/close disagreement SD about 2.5 pts on σ=13) and about 0.15 for CFB. Any ungated NFL fit therefore has b ≳ 0.5, above the 0.25 default. The auditor's scenario of "w=0.10 fitted while 0.25 is used" cannot occur while live. The main residual risks are:
  - CFB near the threshold, with b ≈ 0.3 against w = 0.35.
  - A scale mismatch: the backtest uses Normal(13), while live NFL spreads use `EmpiricalMarginModel` (`services.py:376`).
  - The >24h weight, which is never validated.
- **Impact:** Low-medium.
- **Effort:** S.
- **Proposed change:** Persist the fit and use `w = clip(b_model − se, 0, default_weight)`. Make the backtest use the production margin model.

**9. Gate criterion: re-tested weekly with no hysteresis, and not tied to profit**
- **Verdict:** weak. "Outdated" is the wrong label.
- **Evidence:** `blend.py:34-38`; `GATE_REFIT=7 days` (`services.py:51`).
- **Correction to the auditor:** the refits use heavily overlapping data, so the tests are highly correlated. The false-positive inflation above 2.5% is real but modest.
- **Impact:** Low-medium. It mainly prevents a noisy ungate flip.
- **Effort:** S.
- **Proposed change:** Require 2 consecutive passing refits to ungate. Also require b>0 in each scored season. Report simulated ROI at Kalshi fees next to the ATS%. Record where the hyperparameters came from.

**10. No market-implied early-season prior**
- **Verdict:** missing.
- **Evidence:** NFL carries over decayed last-season results; CFB uses the heuristic prior. CFBD `homePregameElo` is unused, though the spec mentions it (design.md §3).
- **Practice and source:** "implied SRS", meaning ridge fitted on closing spreads (Football Perspective; Lopez et al. 2018).
- **Impact:** Moderate. It reduces false disagreements in weeks 1-4, but it does not create edge.
- **Effort:** M.
- **Proposed change:** Fit `weighted_ridge` on `y = closing spread` from data already fetched, and use it as the CFB prior or as a third NFL component. Do #1 and #6 first.

### Dropped or corrected claims
- **"Code at /Users/cole03roberts/Desktop/SportsBet":** correct. Noted at the top.
- **#3 worked example (fitted w=0.10 against 0.25 used, an "N largest direct profit leak"):** dropped. It cannot coexist with an ungated league under the z≥1.96 gate. The finding is kept at reduced impact (#8).
- **#4 "applied to … total for each league":** narrowed to NFL. CFB has no totals model, so CFB totals never produce a p_yes.
- **#8 QB veto using `home_qb_id`/`away_qb_id`:** corrected. As far as I know these columns are reliably filled only after the game, so use depth charts or injuries, or the disagreement cap.
- **#7 "outdated" verdict and the "repeated tests inflate FP" magnitude:** relabelled as weak. The inflation is modest because the weekly tests are highly correlated.
- **#1 "0.6·p regression shifts known teams about 8 pts vs UNKNOWN_PRIOR teams":** partly dropped. Ratings enter only as differences, so the 0.6 scaling affects only matchups against teams never seen last season, mostly FCS, where a lower prior is roughly right anyway. The RP sign inversion is the real bug.
- **Up-to-standard "Blend architecture":** its `cfb_min_disagree` suggestion was promoted to upgrade #6 rather than left as a footnote.

## props

**Current approach.** The code lives in `/Users/cole03roberts/Desktop/SportsBet/gridiron_edge`, not in the Football folder. I checked every cited line and they all match. Prop probabilities come from one of two paths, and the two are never blended.

- **Book-anchored path** (`pipeline.py:172-202`):
  - It takes one book's over/under pair at the point nearest the target line. When books tie, the alphabetically first book name wins.
  - It devigs each side with the most conservative of four methods, solves an implied mean, and reprices at the target line. Yards use a Normal with sd = 0.6·mean+5 (`distributions.py:73`); counts use Poisson.
  - Any book quote whose `last_update` is more than 600 s before the scan time is dropped (`pipeline.py:184`, `config.py:49`).
- **Projector fallback** (`models/props.py`): exact lowercase name match, the last 12 games (at least 4) weighted with a half-life of 4 games, then each player's own weighted mean and population SD as a Normal (yards) or Poisson (counts). It has no shrinkage and no injury, usage or opponent inputs. Projector-only picks need +1.5 pp more margin (`pipeline.py:241`; `prizepicks.py:100`).
- **Refresh:** props are pulled once per game, within 2 h of kickoff, for at most 8 games a week (`scheduler.py:175-190`). PrizePicks quotes stay usable for 2 days (`services.py:52,540`).
- **Gate:** the auto-gate only writes ml/spread/total keys (`services.py:506-507`), so `(league,"prop")` is never gated. That covers both Kalshi props (`pipeline.py:140` via gate `"prop"`) and PrizePicks entries (`pipeline.py:284`).
- **Spec gap:** spec §2 line 37 says PrizePicks picks are chosen "by comparing PrizePicks lines to no-vig sportsbook prop odds". The projector fallback for PrizePicks therefore goes beyond what the spec says.

### Already sound

| Item | Evidence | Note |
|---|---|---|
| Worst-case devig and a separate implied mean per side | `devig.py:38-40`, `pipeline.py:193-202` | P(over)+P(under) ≤ 1, so neither side is the optimistic complement. Keep it. |
| Kalshi prop strike mapping | `kalshi.py:37-46` uses `floor_strike` (x.5, "greater") | Correct. Adding an assert that `strike_type == "greater"` would cost almost nothing. |
| Poisson for counts | `distributions.py:75` | This path is effectively unused today: `receptions` is not in `PROP_MARKETS` (`odds_api.py:13`). If receptions are added, an integer line counts a push as an over and needs handling. |
| PrizePicks independence and structure rules | `prizepicks.py:83-84` (distinct games, ≥2 teams); exact Kelly over a Poisson-binomial hit count | Sound. The multiplier gap was split out as upgrade #3 below. |

### Ranked upgrades (impact ÷ effort)

**1. The book anchor expires after 10 minutes, and its age is measured from the book's own timestamp, so pricing silently falls back to the projector**
- **Verdict:** incorrect (confirmed).
- **Evidence:**
  - `pipeline.py:184` drops a quote when `snap.now - q.last_update > 600` s.
  - `q.last_update` is the book's market `last_update` (`odds_api.py:118`), not the time we fetched it. Prop markets often update rarely, so a quote can already be older than 600 s when it arrives. The anchor may then be missing on the very first scan.
  - Props are never refreshed. PrizePicks quotes skip the age check and stay usable for 2 days (`services.py:52,540`).
  - Every later scan therefore prices PrizePicks lines and Kalshi ladders with the projector (`pipeline.py:220-223`). `_extra_ev` (`pipeline.py:362-369`) re-runs the same stale check, so sizing agrees with this wrong fallback.
- **Practice:** for a one-shot pull, measure age from fetch time and set the limit near the gap to kickoff. A known market price should never be replaced by a weaker model. The spec's own PrizePicks rule (§2, §5.3 line 190: "These also price the Kalshi prop contracts") relies on the anchor.
- **Impact:** High. This is the main path by which uncalibrated picks get texted.
- **Effort:** S.
- **Change:**
  - Add `props_stale_seconds` (about 3 h) and age prop quotes from fetch time.
  - Apply the same age limit to PrizePicks quotes.
  - If any book quote exists for the player and stat inside the age limit, never fall back to the projector.

**2. Projector-only props bypass the auto-gate**
- **Verdict:** missing (confirmed).
- **Evidence:** `services.py:506-507` only writes ml/spread/total keys. `gates.get((lg,"prop"))` is always False, which contradicts the spec §4 auto-gate rule (spec line 236).
- **Practice:** walk-forward or calibration validation before staking. Outcome-only calibration (PIT, reliability bins) on nflverse needs no odds history.
- **Impact:** High. The auditor's backtest shows projector picks the model scored at p ≥ 0.577 realized about 0.54, against a claimed margin of ≥3.5 pp. The model shows roughly zero edge.
- **Effort:** S for the simple version.
- **Change:**
  - Correction to the auditor: start simpler than a weekly `fit_prop_gate`. Mark every unanchored prop pick observe-only by default: log it for CLV and never text it. Anchored picks stay live.
  - Add the backtest-based gate only once the projector has been improved (#6).

**3. PrizePicks multiplier is parsed but ignored, so non-standard (Demon/Goblin-type) picks are priced at standard payouts**
- **Verdict:** incorrect. It also violates the spec: §6.3 says "Only standard lines are used; no Demons or Goblins" and that the payout table is "overridden by `includeMultipliers`".
- **Evidence:**
  - `odds_api.py:123-127` stores `multiplier`.
  - `pipeline.py:258-276` and `prizepicks.py` never read it.
  - `pipeline.py:261` dedups by `(game, player, stat, point)` only.
- **Practice:** the spec's own rule.
- **Impact:** Medium. An adjusted-payout leg priced as a standard pick overstates EV directly.
- **Effort:** S.
- **Change:** skip PrizePicks quotes whose `multiplier` is neither None nor 1.0.

**4. The Normal line shift (sd = 0.6·mean+5) misprices Kalshi ladder strikes far from the book point**
- **Verdict:** incorrect (confirmed, impact corrected).
- **Evidence:**
  - `distributions.py:73`; `pipeline.py:192` puts no limit on |strike − point|.
  - I re-derived the auditor's examples:
    - Receiving line 85.5 at 50/50 → sd 56.3 → P(>149.5) = 0.128.
    - Passing line 245.5 → sd 152 → P(>299.5) = 0.361.
  - Either way, a skewed shape gives materially different tails.
- **Practice:** yardage is right-skewed and books set lines near the median. The best reference for far strikes is the book's own alternate ladder (`player_*_yds_alternate` on the Odds API).
- **Impact:** Medium to high on Kalshi prop recommendations, but smaller than claimed:
  - `kalshi_price_lo/hi` (0.10/0.90) already excludes the extreme strikes.
  - The auditor's comparison CVs (0.69 receiving / 0.37 passing) were fitted on projector residuals. They include the projector's own mean error, so they overstate dispersion around a sharp book line. The 0.203 / 0.293 alternatives are illustrative, not ground truth.
- **Effort:** S for the cap, M for the replacement shape.
- **Change:**
  - Now: skip strikes more than a set distance from the anchor point (for example 15 receiving/rushing yards, 30 passing yards; tune by backtest).
  - Later: switch to a gamma or lognormal with the median pinned at the book line, with dispersion fitted to book-line residuals rather than projector residuals.
  - Add the `_alternate` markets only if the budget allows (about +100 credits a month).

**5. Anchor uses one book chosen alphabetically, not a consensus**
- **Verdict:** outdated (confirmed).
- **Evidence:** `pipeline.py:192` picks the minimum of `(|point−line|, book_name)`, so BetMGM beats DraftKings and FanDuel by name alone. Other books at the same point are discarded.
- **Practice:** a no-vig consensus across market-making books (the idea behind the Unabated Line).
- **Impact:** Medium. Single-book noise of about 1–3 pp is the same size as the 2 pp PrizePicks margin. A consensus costs no credits, since those books are already in the response.
- **Effort:** S.
- **Change:**
  - At the nearest point, take the median of each book's worst-case per-side probability.
  - Adding novig/prophetx/pinnacle to `PROP_BOOKS` only helps if the free key actually returns props for them. Check `seen_books` on the first run before counting on it. The list currently has 6 books, so 2–3 more still bill as one region.

**6. The projector's distribution is miscalibrated: Normal on skewed yardage, a noisy per-player SD, and no shrinkage**
- **Verdict:** incorrect (confirmed; I merged the auditor's findings 3 and 4).
- **Evidence:**
  - `props.py:35-37`.
  - I re-derived the SD bias: n_eff is about 9.0 at 12 games (SD about 6% low) and about 3.9 at 4 games (about 14% low).
  - Backtest: P(actual > mean) ≈ 0.42–0.43, and the SD of the z-scores is 1.18–1.38. A gamma with one pooled CV per stat had a lower Brier score (0.2433→0.2382 receiving, 0.2419→0.2363 rushing).
- **Methodology caveat:** `calib2.py` scores each player-game at 5 synthetic lines, so the stated n (807 / 394) is about 5x the number of independent games. "Below breakeven" is therefore not statistically established; "about zero edge where it claims ≥3.5 pp" is the defensible reading. The synthetic lines (0.8–1.2× the projection) are also not real PrizePicks lines.
- **Practice:** skewed distributions for yardage, pooled dispersion, and empirical-Bayes shrinkage of the mean.
- **Impact:** Medium, but only while the projector can stake. Once #1 and #2 are in, this becomes the work that lets the projector pass the gate.
- **Effort:** M.
- **Change:** gamma per stat with a pooled CV fitted on past seasons; shrink the mean toward a position prior with weight n_eff/(n_eff+k); down-weight games from before a team change.

**7. Exact lowercase name matching across Odds API, Kalshi and nflverse**
- **Verdict:** outdated (confirmed).
- **Evidence:** `props.py:19,26`; `pipeline.py:183,251`; `services.py:677`.
- **Correction to the auditor's impact:** a mismatch at settlement returns `None` (`services.py:677-679`), and PrizePicks settlement treats `None` as a DNP (`settle.py:60-61`). That can wrongly drop an entry a tier in the paper ledger. Kalshi bets are unaffected because they settle from the market result.
- **Practice:** normalize names the way nflreadr's `clean_player_names` does, then join through an ID crosswalk.
- **Impact:** Low to medium: lost picks plus mis-settled paper PrizePicks entries.
- **Effort:** S.
- **Change:** a `normalize_name()` helper used for every key, plus logging of names that still fail to match.

**8. No injury, inactive or snap-share inputs**
- **Verdict:** missing (confirmed).
- **Evidence:** `props.py` uses only past stat values.
- **Impact:** Low once #1 and #2 are in, because books and PrizePicks pull props for players ruled out. The main value is excluding games with low snap share from the history window.
- **Effort:** M.
- **Change:** use nflverse snap counts to exclude games below about 50% snap share, and skip players listed Out or IR on the model path. The auditor's rule to skip a player because a teammate at the same position changed status is overreach, and I dropped it.

**9. No usage × efficiency split, opponent adjustment or game script**
- **Verdict:** missing.
- **Impact:** Low and uncertain; the sharp books already price these factors.
- **Effort:** L.
- **Change:** defer until a prop gate exists that can measure it. I agree with the auditor here.

### Dropped or corrected claims
- **Finding 4 "probably most of the Kalshi prop candidates" and "4–8 pp errors":** overstated. The 0.10–0.90 price band filters the extreme strikes, and the comparison gamma CV includes projector error, so the tail differences are upper bounds.
- **"Projector picks hit 0.543 / 0.538, below the 0.542 breakeven":** n is inflated about 5x by reusing each game at 5 synthetic lines. Kept only as "about zero realized edge versus the claimed ≥3.5 pp".
- **Finding 2's `fit_prop_gate` weekly backtest as the first step:** replaced by a simpler default of observe-only for unanchored picks.
- **Injury finding sub-claims:**
  - Not verified: the nflreadpy issue #6 citation, "2025 injury feed reported broken", and the Kalshi no-snap settlement wording. Treat them as unverified.
  - Dropped: the "teammate status change → skip" rule.
- **PrizePicks entry "up_to_standard":** the multiplier gap was moved out as upgrade #3, because it breaks an explicit spec rule.
- **Poisson receptions "up_to_standard":** kept, but this path is inactive because receptions are not pulled.

## sizing

**Paths:** the auditor's path note is correct. `/Users/cole03roberts/Desktop/Football` is empty, and the code is in `/Users/cole03roberts/Desktop/SportsBet/gridiron_edge/`. All file:line references below are relative to that folder, and every one was checked against the code.

**Current approach.** Each scan builds Kalshi candidates: at most one game market per game that isn't already held, and one prop per player who isn't already held. The provisional quantity is 2% of the total bankroll, limited by order-book depth (pipeline.py:124). `size_slate` (pipeline.py:329-359) turns each candidate into `Bet(p_fair, 0, 1/p_eff)`, where `p_eff` includes the fee at that provisional quantity. It then runs `optimize_slate` (kelly.py:72-105), which uses SLSQP to maximise E[log W] in full-Kelly units with the caps divided by the Kelly fraction, and multiplies the result by 0.25. Outcomes are enumerated exactly over win/push/loss (3^n) while n ≤ 9; above that it uses 20k Monte Carlo draws, with every bet treated as independent. Stakes are applied to `available = bankroll − open exposure`, which equals the ledger cash. That matches spec §6.5: "bankroll at slate start, net of open exposure". Stakes are floored to cents and to whole contracts, and every filter is re-checked at the final quantity. PrizePicks is sized separately: one-dimensional Kelly on the Poisson-binomial payout, times 0.25, capped at min(2%, 10%/day remaining), on the gross bankroll (pipeline.py:286-291). Arbs get 10% of the gross bankroll (pipeline.py:311). There is no exposure-aware cap budget across scans, no per-bet uncertainty shrinkage, no correlation model and no CLV or drawdown throttle.

### Already sound

| Item | Evidence | Note |
|---|---|---|
| ¼-Kelly default and the 2/4/20%/$1 cap levels | config.py:29-34. config.py:90 rejects fraction ≤ 0, so dividing by it is safe | Standard practitioner levels. The 2% cap binds whenever ¼-Kelly exceeds 2% (full Kelly > 8%). |
| Joint E[log W] with SLSQP, analytic gradient and fallback | kelly.py:81-105. Gradient `-R^T(w/g)` is correct, `_EPS` clamps log arguments, `res.success` is checked, fallback uses `kelly_push` | Standard simultaneous-Kelly method (Whitrow 2007, JRSS-C 56(5); Busseti, Ryu & Boyd 2016). Cap overshoot within SLSQP tolerance is ~1e-6, and `to_cents` floors it away. |
| Fee-inclusive Kelly odds | kalshi_pricing.py:7-15 is ceil-to-cent of 0.07·C·P(1−P)·mult; d = 1/p_eff | Matches the Kalshi taker formula. Using maker or limit orders is an execution idea, not a sizing bug. |
| Flooring stakes and contracts, integer-cents append-only ledger | kelly.py:108-114, pipeline.py:344-347 | Flooring always rounds below Kelly, which is the safe direction. |

### Ranked upgrades (impact ÷ effort)

**1. Caps are enforced per scan, not on cumulative exposure.** Verdict: **gap (partly confirmed; auditor overstated it).**
- **Evidence:** `optimize_slate` gets a fresh `cap_slate`/`cap_game` budget on every scan (pipeline.py:339), sized on the cash that remains (pipeline.py:338). Scans re-trigger after a 60s debounce whenever data refreshes (scheduler.py:14, 78-81, 138-140). Held positions only block the same game market (pipeline.py:150) or the same player (pipeline.py:232). Props on new players in an already-held game, PrizePicks legs and arbs never count toward that game's 4%.
- **Corrections to the auditor:**
  - The code follows the spec literally. The spec never defines "slate", so this is a spec gap rather than "incorrect".
  - 1−0.8^k is an upper bound. It only happens if each rescan finds roughly 10 fresh 2% bets on games and players not already held, which is plausible on a busy Sunday but not typical.
  - The PENDING/RECOMMENDED sub-claim is mostly moot. A new rec on the same `leg_key` expires the old one (lifecycle.py:77-79), and unplaced recs are re-sized jointly in the next scan.
  - Counting a 10% arb as same-game risk overstates the 8-16% per-game figure, because an arb is hedged apart from execution and tie risk.
- **Practice:** caps are limits on total capital at risk. New sizing should use the remaining headroom, or include open positions in the joint E[log W] (Thorp 2006, simultaneous bets).
- **Impact:** high for tail risk, and it removes the main way to end up 30-50% exposed on one correlated Sunday. It costs almost nothing in growth at ¼ Kelly.
- **Effort:** S.
- **Change:**
  1. Add `open_game_exposure: dict[game_id, cents]` to the Snapshot, built from the legs of PLACED bets of every kind.
  2. Add a slate window (for example a kickoff date or NFL week) and the stake already placed in it.
  3. Pass per-game headroom `max(0, cap_game·B − open_game)` and slate headroom `max(0, cap_slate·B − placed_in_window)` into `optimize_slate`, which means making the per-game constraint take a per-game value. Express both as fractions of the same base used for the stakes.
  4. Add a regression test: two scans with the first slate PLACED must stay at or below 20% and 4%.

**2. PrizePicks sized outside the joint budget, on payouts that are only an upper bound.** Verdict: **gap (confirmed).**
- **Evidence:** pipeline.py:288-290 uses `snap.bankroll_cents` (gross) while Kalshi uses cash. The entry is never charged against the game or slate caps. `kelly_entry` treats the nominal Power/Flex table as certain.
- **Practice and source:**
  - round2.md:147/161/230 says that in California and US-wide, PrizePicks is Arena/P2P, so fixed-multiplier EV is an upper bound. That is confirmed in the repo's own research.
  - Spec §6.5 says "one bankroll for everything".
  - Note: gross wealth is arguably the more correct Kelly base, so the real issue is the inconsistency and the missing budget, not gross versus net in itself.
- **Impact:** low-moderate in stake terms, since the cap is 2%. The payout-uncertainty part matters more, because every PrizePicks stake may be sized on payouts that don't exist.
- **Effort:** S once item 1 exists.
- **Change:** charge the entry against the headroom from item 1 for each of its games. Apply a 0.5 multiplier while Arena status is unverified, as a setting.

**3. No uncertainty shrinkage per source.** Verdict: **missing (confirmed).**
- **Evidence:** one global `kelly_fraction` (config.py:29). Projector-only props only get a higher EV hurdle (pipeline.py:241). Once past it, they are sized like props anchored to sportsbook prices.
- **Practice and source:**
  - Baker & McHale, "Optimal Betting Under Parameter Uncertainty", *Decision Analysis* 10(3), 2013, is a real paper.
  - Chu, Wu & Swartz, "Modified Kelly criteria", JQAS 2018, is real.
  - The `μ²/(μ²+σ²)` multiplier is a reasonable normal approximation.
  - The Metel arXiv 1701.02814 citation could not be verified and should be dropped.
- **Impact:** moderate. It moves stake away from the noisiest edges, where the winner's curse is worst. It does change stakes in practice: for example a 55% / 50c bet has a ¼-Kelly stake of 2.5%, which with s = 0.5 becomes 1.25%, below the cap.
- **Effort:** S for per-source multipliers; M for per-leg σ.
- **Change:** start with a per-source multiplier, for example 1.0 for anchored game legs, 0.75 for anchored props and 0.5 for projector-only props. Pass it to `optimize_slate` as a per-bet cap/objective scale. Move to per-leg σ later, from devig dispersion, projector SD and `BlendFit.b_model_se` (blend.py:30).

**4. Same-game correlation ignored.** Verdict: **gap (confirmed); impact correctly rated low.**
- **Evidence:** kelly.py:37-52 treats outcomes as independent. Spec line 260 allows one game market plus props on different players for the same game.
- **Practice and source:**
  - The citation is misattributed. "Optimal Betting Strategies for Simultaneous Games" is by Grant, Johnstone & Kwon, *Decision Analysis* 5(1):10-18, 2008, not "Grant & Buchen".
  - The Gaussian-copula plan is in round2.md:198.
- **Impact:** low at current caps. The 10% → 7.7% full-Kelly example still binds at 2% each under ¼ Kelly. The real risk is same-direction stacks building up across scans (see item 1).
- **Effort:** S for the stopgap, M for a copula.
- **Change:** stopgap of a 2% total cap for same-direction Kalshi legs on one game. Later, correlated Monte Carlo inside `_scenarios`.

**5. No CLV-based throttle.** Verdict: **missing (confirmed); the auditor's proposal is partly infeasible.**
- **Evidence:** `kelly_fraction` is static. CLV is recorded only for open single-leg Kalshi **game** bets (services.py:682-699). There is no CLV for props or PrizePicks, so the proposed per-source throttle can't run for those sources.
- **Practice:** the ruin math is right: at ¼ Kelly, P(ever being down 50%) is 0.5^7 ≈ 0.8%. CLV is the standard practitioner leading indicator.
- **Impact:** moderate, mainly in season 1. It protects against an edge that isn't real, not against variance.
- **Effort:** S for game bets only; M once prop closing lines are captured, which also costs Odds API credits.
- **Change:** use rolling CLV over the last ~50 settled game bets per league. If t < 0, halve that league's multiplier; if t < −2, set observe-only through the existing gates. For props, use the Brier/calibration drift already in the KPIs.

**6. Fee odds use the provisional quantity, and freed budget is not re-optimised.** Verdict: **gap (confirmed), small.**
- **Evidence:** pipeline.py:336-337 derives `p_eff` from the provisional quantity sized on the total bankroll (pipeline.py:124-127). The final quantity is usually smaller and pays a higher per-contract fee. The auditor's fee figures check out: at 10c, 1c/contract for 1 contract vs 0.65c for 40; at 50c, 2c vs 1.75c. The re-check at pipeline.py:351-357 prevents negative-EV recs, but budget from dropped bets is not reassigned.
- **Impact:** small. Slight oversizing on low-priced, few-contract bets, and the occasional bet dropped late.
- **Effort:** S.
- **Change:** after the first optimisation pass, recompute d at the final quantity, drop failures and run `optimize_slate` once more on the survivors.

**7. Scenario generation is wasteful.** Verdict: **gap (confirmed), small.**
- **Evidence:** `p_push` is always 0 (pipeline.py:337), yet the code enumerates 3^n, so exact enumeration stops at n = 9. Anything larger falls to 20k Monte Carlo draws, which a busy Sunday slate will always do.
- **Practice:** enumerate 2^n exactly up to about 2^15, or use more draws (Whitrow 2007).
- **Impact:** small, about 0.3 points of bankroll per bet at ¼ Kelly, mostly hidden by the 2% cap. It matters once correlation is added (item 4).
- **Effort:** S.
- **Change:** use a two-outcome alphabet when every push probability is 0, raise the exact limit, and use 100k draws.

**8. Fee consistency in the ledger and texts.** Verdict: **minor, reduced from the auditor's version.**
- **Evidence:**
  - The texted stake is `qty·price` without the fee (pipeline.py:345), while the ledger debits cost + fee (lifecycle.py:148).
  - The auditor missed this one: `confirm` computes the fee with multiplier 1.0 (lifecycle.py:132, 136) instead of the market's `fee_multiplier`, so the ledger fee can differ from the fee used in sizing.
- **Impact:** small, but it affects P&L and ROI accuracy.
- **Effort:** XS.
- **Change:** text "stake $X + fee $Y", and store or use the market `fee_multiplier` in `confirm`.

### Dropped or corrected claims

- **Finding 2, "Kelly bankroll understated by open fees": dropped.** Fees are sunk. Wealth = cash + stake at cost (services.py:728) is the correct mark. Adding fees back would overstate it, because they can't be lost twice. Only the display and multiplier issue (item 8) remains.
- **Finding 1, "PENDING recs reserve nothing": dropped.** Same-leg recs supersede each other (lifecycle.py:77-79), and unplaced recs are re-sized jointly in each scan.
- **Finding 1, "incorrect vs spec": softened.** The code matches §6.5 word for word. The flaw is that "slate" is undefined.
- **Finding 1, "8-16% per game": corrected.** That figure counts a hedged 10% arb as directional risk. Real same-game directional overrun comes from props on new players added across scans, plus a PrizePicks leg.
- **Finding 1, "59-67% at-risk after 4-5 scans": corrected** to an upper bound that needs fresh candidates in every scan.
- **Finding 4 citation "Grant & Buchen 2008": corrected** to Grant, Johnstone & Kwon, *Decision Analysis* 5(1):10-18, 2008.
- **Finding 3 citation "Metel 2017, arXiv 1701.02814": dropped.** It could not be verified.
- **Finding 5, per-source CLV for props and PrizePicks: dropped as proposed.** That data isn't recorded; only Kalshi game-bet CLV exists.

Sources:
- [Grant, Johnstone & Kwon 2008, INFORMS](https://pubsonline.informs.org/doi/abs/10.1287/deca.1080.0106)
- [RePEc listing](https://ideas.repec.org/a/inm/ordeca/v5y2008i1p10-18.html)

## prizepicks-arb

**Current approach.** The code is in `/Users/cole03roberts/Desktop/SportsBet/gridiron_edge`. `/Users/cole03roberts/Desktop/Football` is empty apart from the memory files.

- **PrizePicks payouts.** The fixed Power/Flex multipliers are hard-coded in `strategy/prizepicks.py:13-24`. Allowed structures are 3P, 4F, 5F, 6F, 5P and 6P.
- **Hit odds and sizing.** Hit counts come from an exact Poisson-binomial calculation that assumes independent picks. Stakes use multi-outcome Kelly over the whole payout distribution.
- **Entry choice.** `build_best_entry` brute-forces every 3-6 leg combination with one pick per game. Each pick must clear the breakeven plus a 2pp margin (market-anchored) or 3.5pp (model). It returns the single entry with the highest full-Kelly growth.
- **Entry output.** `pipeline.pp_entry_candidates` emits at most one entry per scan, sized at quarter-Kelly of the gross bankroll and capped by `cap_bet` and the daily PrizePicks cap.
- **Settlement.** DNPs and ties are settled through `revert` and `settle.grade_pp_entry`.
- **Arbs.** `strategy/arb.py` checks two things on Kalshi:
  - YES+YES moneyline pairs, flagged `tie_risk` for NFL;
  - adjacent-strike pairs within a single (event, type, team) spread/total ladder.
- **Arb fees and sizing.** Fees are the per-order rounded-up taker fee. Arbs are re-priced at the final quantity, sized to 10% of bankroll, and any ROI from 0% to 8% is accepted.
- **Staleness.** Kalshi quotes are never checked for age. They refresh every 3 minutes near games and every 30 minutes otherwise (`scheduler.py:125`). After a failed refresh the old list is kept.

### Already sound

| Item | Evidence | Note |
|---|---|---|
| Multi-outcome Kelly for entries | `prizepicks.py:66-80` maximises Σ P(k)·log(1+f(m_k−1)) over [0, 0.99] | Correct. One small fix is listed below (#6). |
| One pick per game, one per player | `_valid` at `prizepicks.py:83-84`; best pick per player at 89-93 | Keeps the independence assumption honest. A copula model for same-game picks is optional (effort L) and low priority. |
| DNP/tie revert ladder | `prizepicks.py:126-137`, `bets/settle.py:51-72` | Matches the rules in `research/round2.md`. An Arena mode would need its own grader. |
| Kalshi fee math | `kalshi_pricing.py:7-15`; re-priced per order at `arb.py:41,65` and `pipeline.py:315` | Matches the published 0.07·C·P·(1−P) fee, rounded up to the cent per order. |
| Demons/Goblins excluded (in practice) | `clients/odds_api.py:117` skips any market key ending in `_alternate`. The Odds API bookmaker docs say PrizePicks Demons and Goblins "are included under _alternate markets", and that DFS picks with non-default multipliers appear there. | The spec rule (§6.3, "no Demons or Goblins") is already met. Only a defensive guard is left (#7). |

### Ranked upgrades (impact / effort)

**1. PrizePicks payout mode is fixed only; Arena may be what this user actually gets**
- **Verdict:** valid, but the 2026 status is unverified.
- **Evidence:**
  - `PAYOUTS` and `ALLOWED` are hard-coded, and `pipeline.py:293-298` reports EV and p_win from them.
  - The project's own research (`research/round2.md:11-16, 125, 147, 161, 230`) says CA went Arena-only on 2025-06-30 and the whole US on 2025-08-22. It recommended keeping payouts in config and treating fixed-payout EV as an upper bound. None of that was implemented.
- **Practice and sources:**
  - SBC Americas reported both switches: [CA](https://sbcamericas.com/2025/07/02/prizepicks-california-p2p-arena-switch/), [US-wide](https://sbcamericas.com/2025/08/26/prizepicks-p2p-arena-only-us/).
  - Action Network describes Arena: a perfect lineup gets the full multiplier, otherwise the group winner takes the prize ([Arena explainer](https://www.actionnetwork.com/education/how-does-prizepicks-pickem-arena-work)).
- **Impact:**
  - I re-ran the math. A 5-Flex at p=0.58 has EV +26.9% under fixed payouts and −34.4% if only a perfect lineup pays.
  - The auditor's formula says "20x" but its arithmetic uses the 5F 10x payout. The −34% figure is for 5-Flex. For 5-Power at 20x, a perfect-only payout leaves EV at +31%, so Power structures are unaffected.
  - In Arena, Flex entries would therefore be systematically −EV. This is the largest possible error in the area, though it is conditional on how the user's app actually pays.
- **Effort:** S.
- **Proposed change:**
  - Move `PAYOUTS` and `ALLOWED` into Settings.
  - Add `pp_mode = fixed | arena`. In arena mode, set every non-perfect tier to 0, which effectively restricts entries to Power.
  - On first run, prompt the user to confirm the multipliers they see in the app.
  - Keep the "confirm payout in app" caveat in the SMS.

**2. Open PrizePicks entries are ignored by later scans, so the next scan shows no new entry or an overlapping one**
- **Verdict:** valid.
- **Evidence:**
  - `services.py:732` builds `open_players` from `kalshi_prop` legs only.
  - `pp_entry_candidates` (`pipeline.py:254-276`) never reads `open_players`.
  - `scan` (`pipeline.py:379`) drops only an exact `leg_key` match.
  - So after entry E is placed, the next scan rebuilds E and filters it out, and no PrizePicks rec is shown. If one leg's line or probability moves, an entry overlapping E in 4 of 5 legs is sized as if independent of E.
  - Kalshi prop exclusion also ignores players already in open PrizePicks entries.
- **Practice:** Correlated positions must be sized jointly or excluded (Thorp 2006, MacLean–Thorp–Ziemba 2011). This is standard.
- **Impact:**
  - Risk: roughly double exposure on the shared legs.
  - Profit: disjoint +EV entries are never shown. The 10% daily cap goes mostly unused, since one entry is about 0.4-1.7% of bankroll.
- **Effort:** S for the exclusion, M for the multi-entry loop.
- **Proposed change:**
  - Add `pp_open_players` and `pp_open_games` to the Snapshot (from open `pp_entry` legs) and filter picks against them.
  - Add PrizePicks players to the Kalshi `open_players` set too.
  - Then loop `build_best_entry` over the remaining picks until nothing qualifies or the daily cap is reached.

**3. Arb execution: no staleness check, no minimum ROI, no legging guidance**
- **Verdict:** valid.
- **Evidence:**
  - `KalshiMarket.fetched_at` exists (`types.py:79`) but nothing in `arb.py` or `pipeline.py` reads it. Quotes can be 30+ minutes old off-peak, or older after a failed refresh.
  - The spec at §6.4 (line 313) says to "reject quotes older than the staleness limit".
  - `arb_candidates` accepts any ROI > 0 at 10% of bankroll.
- **Practice:**
  - Two-leg manual arbs carry legging risk.
  - [Prediction-market arb after fees](https://pm.wiki/it/learn/prediction-market-arbitrage-after-fees) notes that thin arbs vanish after fees and latency.
  - The fee schedule rounds per order ([Kalshi fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf)).
- **Impact:** Avoids SMS-ing phantom or stale arbs. One leg filling and the other moving 3¢ wipes out many 0.3% arbs. Moderate risk reduction.
- **Effort:** S.
- **Proposed change:**
  - Drop arbs where `now − fetched_at > arb_stale_seconds` (about 60s). Optionally trigger an immediate `kalshi_refresh` before sending the SMS.
  - Add `arb_min_roi` (about 1%).
  - Include in the SMS the limit price for each leg, which leg to fill first, and the worst acceptable price for the second leg.

**4. Ladder scan checks adjacent strikes only and never pairs the two teams**
- **Verdict:** valid.
- **Evidence:**
  - `arb.py:58-59` loops `zip(ms, ms[1:])` within (event, type, team).
  - The auditor's 2.5/3.5/4.5 counter-example is correct: b(4.5)=0.65 > a(2.5)=0.60 is missed.
  - NO "A by >x" + NO "B by >y" pays at least $1 whenever x ≥ −y, which always holds for positive half-point lines. Grouping by team never checks this pair.
- **Practice:** A no-arbitrage check on a strike ladder compares every pair i<j. A running minimum of the asks below each strike does it in O(n). This is standard.
- **Impact:** More genuinely risk-free detections at no API cost. They are rare and fleeting, so the dollar total is small.
- **Effort:** S.
- **Proposed change:**
  - Replace the zip loop with a running-minimum scan.
  - Add a cross-team NO/NO spread pair grouped by `game_id`.
  - Keep the per-order fee re-pricing.

**5. NFL moneyline `tie_risk` flag is wrong; the NO+NO moneyline pair is missing**
- **Verdict:** valid. The tie rule is confirmed only through third-party mirrors of Kalshi's rulebook.
- **Evidence:**
  - `arb.py:47` and `pipeline.py:163` flag NFL moneylines as tie risk.
  - Spec lines 61, 263 and 304 say "both legs can lose on a tie".
  - Only YES+YES is checked.
- **Practice:**
  - Kalshi NFL game rules, as mirrored by [Prediction Hunt](https://www.predictionhunt.com/odds/nfl/jacksonville-jaguars-vs-new-orleans-saints-2026-08-15/34190) for several 2026 games, settle a tie at $0.50 per side.
  - Under that rule YES_A+YES_B and NO_A+NO_B both always pay $1.
  - The auditor's CFTC link appears to describe a different (tie-strike) product. I found no Kalshi-hosted page.
- **Impact:** Small. Removes a false warning that discourages valid arbs, and roughly doubles the moneyline arb surface.
- **Effort:** S.
- **Proposed change:**
  - Parse `rules_primary` from the market JSON and set `tie_risk` only when there is no 50/50 tie clause.
  - Add `kalshi_ml_no_pairs`.
  - Fix the spec text. The `pipeline.py:163` single-moneyline tie flag is also wrong for the same reason.

**6. PrizePicks stake uses the gross bankroll, while Kalshi sizing uses available bankroll**
- **Verdict:** valid. This came out of the auditor's "up to standard" Kelly item.
- **Evidence:** `services.py:727` adds open exposure back into `bankroll_cents`. `pipeline.py:288` sizes from that figure, while `size_slate` (`pipeline.py:338`) uses bankroll − exposure.
- **Practice:** Kelly sizing should use capital you can actually lose (Kelly 1956; Baker & McHale 2013 on fractional Kelly). This is standard.
- **Impact:** Small. A slight over-stake when many positions are open.
- **Effort:** XS.
- **Proposed change:** Use `max(0, bankroll_cents − open_exposure_cents)` as the base for the PrizePicks stake.

**7. Defensive multiplier guard**
- **Verdict:** downgraded from the auditor's "high impact" claim.
- **Evidence:** `PropQuote.multiplier` is stored but never read. Standard markets should carry multiplier 1 or none, since Demons and Goblins sit in `_alternate` markets that are already skipped.
- **Impact:** Low. It is protection in case the Odds API changes its format.
- **Effort:** XS.
- **Proposed change:** In `pp_entry_candidates`, skip a quote when `multiplier not in (None, 1.0)`, and add a test for it.

**8. Greedy entry selector in place of brute force**
- **Verdict:** valid but low value.
- **Evidence:**
  - I benchmarked 25 picks: 0.8s at 6 games, 2.3s at 8 games, and 11.5s at 14 games. The auditor's 33s used 14 games, which the 500-credit budget rarely allows.
  - The greedy fallback at lines 103-112 is dead code.
- **Practice:**
  - With independent picks and payouts that never decrease with more hits, EV and growth both rise with every p.
  - So for each structure, "filter to qualifying picks, keep the best per game, take the top n" is optimal. The margin filter has to run before the per-game best is taken, because a model pick may fail where an anchored pick in the same game passes.
  - Distinct games already imply at least 2 teams.
- **Impact:** EV is unchanged, scans run faster, and the multi-entry loop in #2 becomes cheap.
- **Effort:** S.
- **Proposed change:** Implement the greedy selector per structure and keep brute force behind a flag.

**9. Price ladder middles (cost slightly above $1)**
- **Verdict:** plausible, but it depends on the model.
- **Evidence:**
  - `arb.py:65-70` rejects any pair costing $1 or more.
  - `EmpiricalMarginModel` (`distributions.py:32`) already keeps key-number mass. The normal fallback would understate P(margin exactly 3).
- **Practice:** Middle EV = P(middle) − (cost − 1). NFL key numbers 3 and 7 are well established. The auditor's specific 9% and 6% figures are unverified approximations.
- **Impact:** Modest, and it adds model risk to what is otherwise a risk-free module. It must be labelled "middle", never "arb".
- **Effort:** M. It needs the margin mean wired in, a new rec kind, sizing through the Kelly slate, and settlement.
- **Proposed change:** Enable it only when the empirical margin model is active (not the normal fallback). Size it through `optimize_slate`, not `arb_fraction`.

### Dropped or corrected claims

- **"Demons/Goblins are not filtered" (rated high impact).** Dropped as a defect. `odds_api.py:117` already skips `_alternate` markets, which is where the Odds API places Demons, Goblins and other non-default-multiplier picks ([Odds API bookmakers page](https://the-odds-api.com/sports-odds-data/bookmaker-apis.html)). What remains is the cheap guard in #7.
- **"Arena is a California-only issue."** Corrected. The project's own research says Arena became US-wide on 2025-08-22, so a California-specific setting is the wrong framing; the mode should apply for any user. I also corrected the auditor's "20x" to the 5-Flex 10x payout. The −34% EV figure holds for 5-Flex only.
- **"33s scan time."** Corrected. It is 1-2s at the realistic 6-8 games per week, so this is low priority rather than a responsiveness problem.
- **CFTC filing cited for the 50/50 tie rule.** Corrected. That filing appears to cover a tie-strike product. The tie rule is supported only by Kalshi rulebook text mirrored on aggregator sites, so confirm it against `rules_primary` in the API.
- **"Middles have low variance relative to single bets"**, with specific P(margin=3) ≈ 9% and P(margin=7) ≈ 6%. Softened. The worst-case loss is bounded, but the edge rests entirely on the margin model, and the percentages are unverified approximations.
- **Same-game copula upgrade (rated L effort).** Not ranked. Few games fit within the API budget, and PrizePicks changes payouts for same-game picks, so it is not worth the effort.

Sources:
- [Odds API bookmakers page](https://the-odds-api.com/sports-odds-data/bookmaker-apis.html)
- [Odds API betting markets page](https://the-odds-api.com/sports-odds-data/betting-markets.html)
- [Stokastic: Demons and Goblins](https://stokastic.com/news/what-are-demons-and-goblins-at-prizepicks-ac11)
- [Prediction Hunt, Kalshi NFL rules mirror (JAX-NO)](https://www.predictionhunt.com/odds/nfl/jacksonville-jaguars-vs-new-orleans-saints-2026-08-15/34190)
- [Prediction Hunt, Kalshi NFL rules mirror (GB-DEN)](https://www.predictionhunt.com/odds/nfl/green-bay-packers-vs-denver-broncos-2026-08-21/34195)
- [CFTC Kalshi filing](https://www.cftc.gov/filings/orgrules/rules09182627968.pdf)
- [SBC Americas: PrizePicks CA Arena switch](https://sbcamericas.com/2025/07/02/prizepicks-california-p2p-arena-switch/)
- [SBC Americas: PrizePicks Arena-only US](https://sbcamericas.com/2025/08/26/prizepicks-p2p-arena-only-us/)
- [Action Network: how PrizePicks Arena works](https://www.actionnetwork.com/education/how-does-prizepicks-pickem-arena-work)
- [Prediction-market arbitrage after fees](https://pm.wiki/it/learn/prediction-market-arbitrage-after-fees)
- [Kalshi fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf)

Benchmark script: `/private/tmp/claude-501/-Users-cole03roberts-Desktop-Football/dffddf62-bd2b-4ca6-b23b-64817b4b44fd/scratchpad/bench.py`

## evaluation

**Summary of the current approach.** The code is at `/Users/cole03roberts/Desktop/SportsBet/gridiron_edge`. `/Users/cole03roberts/Desktop/Football` is empty, so the auditor's path note is correct.

- **CLV.** CLV is `p_close_fair / price_eff − 1`, where `price_eff` includes the fee (`bets/clv.py:4-8`, `bets/settle.py:189-197`). It is recorded only for PLACED `kalshi_game` bets that kick off within 30 minutes, after a `close:` Odds API pull (`services.py:608, 682-701`). The fair close comes from the Pinnacle, then exchanges, then median hierarchy (`strategy/fair.py:46-63`).
- **Gate.** The auto-gate is a leak-free weekly walk-forward. It runs a logistic regression of the ATS cover outcome on logit(p_model), with a market column fixed at logit(0.5)=0 (`models/blend.py:41-112`), and requires n≥200 and z≥1.96. One result per league is reused for ml, spread and total (`services.py:506`), and it fails closed on errors. Live blending ignores the fitted slope and uses fixed weights of 0.125, 0.25 and 0.35.
- **KPIs.** `compute_kpis` (`ui/widgets.py:28-74`) reports:
  - stake-weighted ROI with an unweighted ±2·SE band
  - mean CLV and the share of bets with positive CLV
  - a single Brier score on `p_win` against `result=='won'`
  - max drawdown on the raw ledger balance
  - an "inconclusive" label while n<1500
- **Missing.** There is no shadow (unplaced) evaluation, no breakdown by bet type, and no drift monitoring.

### Already sound

| Item | Evidence | Note |
|---|---|---|
| CLV formula and reference price | `clv.py:4-8`; fee-inclusive `price_eff` at `settle.py:193`; Pinnacle-first no-vig close at `fair.py:46-63`; CLV left missing when there is no close (spec line 370) | Standard practice. Minor issue: `fair_spread_mean`/`fair_total_mean` always use the NFL sigma (`fair.py:90-95`), including for CFB. This only affects the small shift from the nearest quoted line, so the impact is low. Recording the close source (pinnacle/exchanges/median) is a cheap improvement. |
| No look-ahead in the walk-forward | `nfl.py` fit filters `gameday < asof`; EPA filters `_end < asof`; CFB inputs filter `d >= before` | Confirmed. |
| Gate direction and fail-closed design | `blend.py:33-38`; `services.py:490-500` | A slope test on cover outcomes against the closing line is a valid "beats the close" test. The market column is constant zero, so `b_market` and the ridge only stop the Hessian from going singular. Dead, but harmless. |
| Arb and refund handling in the ledger | `lifecycle.py:148` debits cost+fee; refunds return stake+fee (`settle.py:182`) | The cash ledger is correct. Only the KPI layer is wrong. |

### Ranked upgrades (expected impact / effort)

**1. ROI leaves out Kalshi taker fees.** Verdict: incorrect. **Confirmed.**
- **Evidence:** `lifecycle.py:135-136` stores the contract cost in `stake_cents` and the fee in `fee_cents`. `widgets.py:46-52` uses `profit = payout − stake_cents` and a denominator of `stake_cents` only. This applies to `kalshi_game`, `kalshi_prop` and arbs (`lifecycle.py:131-132`).
- **Practice and source:** Report ROI net of all costs. Kalshi's fee is ceil(0.07·C·P·(1−P)), which is 1.75¢ on a 50¢ contract, or 3.5% of cost. Fee schedule; Bürgi, Deng & Whelan 2025.
- **Impact:** High. ROI is overstated by about 1.5–3.5 percentage points on Kalshi bets, which is the same size as the target edge. Arb profit is also overstated, and arbs are thin by design.
- **Effort:** S.
- **Change:** Set `s = stake_cents + fee_cents` everywhere in `compute_kpis`, including the per-bet return.

**2. CFB bets never get a CLV value.** Verdict: incorrect. **Confirmed.**
- **Evidence:** `services.py:686` builds the snapshot with `predictions={}`. For CFB, `pipeline._game_p_yes` (pipeline.py:100-103) returns None whenever `model_mean is None`. So every CFB bet is skipped at `services.py:694`.
- **Practice:** The close should be measured from the market alone. The disagreement filter decides whether to bet, not whether to measure.
- **Impact:** High. The league with the highest model weight (0.35) has no skill signal at all.
- **Effort:** S.
- **Change:** Extract `market_p_yes(m, g, snap, s)`, covering the ml devig path and the `mkt_mean→_p_yes` path, and use it in `_record_clv`. Add a regression test with a CFB `kalshi_game` bet.

**3. Totals and ML go live on the spread backtest alone.** This is a sub-point of the auditor's "up_to_standard" gate finding, promoted here because it is a real risk item.
- **Evidence:** `services.py:506` gives `ml`, `spread` and `total` the same `gated` value from the spread-only `walkforward_ats`. No totals backtest exists.
- **Practice:** Model paths that have not been tested should start observe-only and graduate on evidence from data the model was not fitted on.
- **Impact:** Medium-high risk reduction. Today a passing NFL spread fit ungates NFL totals, whose model has never been validated.
- **Effort:** S. Gate totals closed. Building a totals walk-forward from nflverse `total_line` is M.
- **Change:** Set `(lg,'total')=True` until a totals backtest passes. Optionally keep ML tied to spread, since both come from the same margin model.

**4. Non-anchored props and PrizePicks entries are never gated.** Verdict: missing. **Confirmed.**
- **Evidence:** `fit_gates` never writes `(lg,'prop')`. `pipeline.py:146` (via `_kalshi_rec` with gate `"prop"`) and `pipeline.py:284` read `.get(...,False)`, so props default to live. The projector fallback at `pipeline.py:216-223` has no backtest. Its only protection is the extra margin `pp_margin_model − pp_margin_market`.
- **Practice:** Same as item 3. Props priced from devigged sharp books are a market-derived edge and can stay live.
- **Impact:** Medium-high. It stops real money going on the least validated probability source.
- **Effort:** S.
- **Change:** Mark recs observe-only when `anchored is False`, for Kalshi props and for any PrizePicks entry that includes a non-anchored leg. Set the prop gate key explicitly in `fit_gates`.

**5. Observe-only and unconfirmed recs never get CLV.** Verdict: missing. **Confirmed, and the spec requires it.**
- **Evidence:** `_record_clv` iterates only `open_bets()` (state PLACED). `recommendations` has no CLV column. `close_games()` excludes `observe_only=1` (`services.py:563-566`). Spec lines 236 and 348 say observe-only recs are "still logged for CLV".
- **Practice:** Shadow or paper tracking of every signal the system emits. It removes selection bias from the user choosing which recs to confirm.
- **Impact:** High. This is the only practical route for a gated market to show evidence, and it fixes a gap against the spec.
- **Correction:** The auditor's "3–10x sample" figure is unsupported, so it is dropped here. The real gain depends on how often the user confirms recs.
- **Credit caveat:** An extra close pull for a kickoff window with only observe-only games costs credits from a 500/month budget. The proposed "only when a pull is already scheduled or the budget is above a reserve" rule is the right guard.
- **Effort:** M.
- **Change:** As proposed. Add `clv_close` to `recommendations`, computed from the quoted ask plus fee, and report CLV separately for "placed" and "all signals".

**6. Max drawdown counts deposits, withdrawals and open stakes.** Verdict: incorrect. **Confirmed.**
- **Evidence:** `widgets.py:64-67` runs over `Ledger.history()` (`db.py:114-119`), the cumulative sum of all ledger rows. That includes deposits, withdrawals, and stakes posted at placement (`lifecycle.py:148`). Payouts and refunds are posted at settlement (`settle.py:141-143`).
- **Practice:** Drawdown should be measured on the settled P&L curve, net of external cash flows.
- **Impact:** Medium. A withdrawal currently reads as a drawdown, and Sunday's open exposure shows as a temporary loss. That makes the KPI useless for tuning Kelly or a stop-loss.
- **Effort:** S.
- **Change:** Build the series as the cumulative sum of `payout − (stake+fee)` per bet, ordered by `settled_at`. Report max and current drawdown in dollars and as a percentage of peak.

**7. The PrizePicks Brier score compares two different events.** Verdict: incorrect. **Confirmed, with a narrower scope.**
- **Evidence:** `p_win` is P(mult>1) (`pipeline.py:294`). `settle.py:72` grades 'won' when mult>0.
- **Correction:** Among the allowed structures (`prizepicks.py:25`), the mismatch occurs only for the 0.4x payouts (5-flex with 3 hits, 6-flex with 4 hits). The 3-flex 1x push is not in the allowed list. Arbs carry `p_win=1.0` and add zero-error points to the pool.
- **Practice:** A proper score needs the forecast and the outcome to refer to the same event (Gneiting & Raftery 2007).
- **Impact:** Low-medium. Brier is a secondary KPI.
- **Effort:** S.
- **Change:** For `pp_entry`, use the outcome `1[payout > stake]`, or score each leg separately. Exclude arbs. Break out Brier by kind.

**8. The ROI band does not match the ROI estimate and mixes bet types.** Verdict: outdated. **Partly confirmed, but overstated.**
- **Evidence:** ROI is stake-weighted, but the band is computed on unweighted returns (`widgets.py:58-63`). Arbs, Kalshi bets and PrizePicks entries are pooled. Arbs count toward `MIN_CONCLUSIVE_N`.
- **Correction:** The band is not systematically too narrow. Low-variance arbs shrink it, while PrizePicks entries paying up to 25x widen it, so the direction is ambiguous. The real problems are the inconsistency and arbs inflating n.
- **Practice:** Delta-method or bootstrap confidence intervals for a ratio estimator, computed per segment.
- **Impact:** Medium. It reduces the chance of concluding "proven edge" too early.
- **Effort:** S.
- **Change:** Per kind, report ROI with a delta-method CI. Report arbs only as realized profit. Add a CLV mean ± 2SE and a t-stat. Decide "conclusive" from the CI rather than a fixed n.

**9. Calibration has a single Brier number with no baseline or reliability table.** Verdict changed from outdated to missing.
- **Evidence:** `widgets.py:55-73` reports a raw Brier. `blend.py:103-111` reports no baseline and no 52.38% breakeven.
- **Practice:** Brier skill score against the market, plus a reliability table. Walsh & Joshi (2024, MLWA 16:100539) is real and is characterised correctly (about +35% ROI selecting on calibration versus about −35% selecting on accuracy). The Kalshi favorite-longshot bias is documented in Bürgi et al. 2025.
- **Impact:** Medium-low. The cheap part is adding the 0.25/0.693 baselines and the 52.4% breakeven to the backtest output (S). Storing `p_mkt_at_rec` needs a schema change, which is effort M.
- **Change:** Do the backtest baselines now. Add BSS and a 5-bin reliability table once item 5 adds the extra columns.

**10. No drift or degradation monitoring.** Verdict: missing. **Confirmed.**
- **Evidence:** There is no rolling or sequential logic in `services.py`, `widgets.py` or `app.py`.
- **Practice:** Rolling CLV, or a one-sided CUSUM on the CLV mean, with an alert when it drifts below 0.
- **Correction:** "Detects a dead edge within 50–100 bets" is optimistic for props and PrizePicks. A realistic range is about 100–300 signals, and it depends on item 5 for sample size.
- **Impact:** Medium.
- **Effort:** M.
- **Change:** As proposed. Add a rolling-100 CLV ± 2SE per segment, send a NotifyChain alert when the upper bound drops below 0, and auto-set that segment observe-only (fail closed) until the user resets it.

**11. No CLV for Kalshi props or PrizePicks legs.** Verdict: missing. **Confirmed.**
- **Evidence:** `services.py:689` filters to `kalshi_game`, while `settle.py:191` already accepts `kalshi_prop`.
- **Correction:** The claim that this is "the standard evaluation used by PrizePicks" is unsupported. It is a practitioner habit, not something PrizePicks itself does. For PrizePicks legs there is no price, so this measures how much the line moved, not price CLV.
- **Caveat:** Prop pulls are per-event and costly, so the "close" may be hours stale. The proposed staleness label is necessary.
- **Impact:** Medium.
- **Effort:** M.
- **Change:** As proposed. Use the last paid prop pull before kickoff as the close, record per-leg CLV for `kalshi_prop` and each PrizePicks leg, and label it as a "last pre-kick snapshot" when it is stale.

**12. Optional gate tweaks** (still up_to_standard overall). Effort S each.
- Drop the dead market column.
- **Correction on burn-in:** Burn-in is needed only for the early weeks of the first season in the NFL span, and for the single-season CFB span (`services.py:516-517`). Later NFL seasons fit on the prior season's age-weighted data.
- An empty fit predicts a margin of 0. That is not a neutral 0.5 forecast; it systematically picks underdogs, which adds noise to the slope.
- Consider giving CFB two seasons.
- The repeated-weekly-testing inflation is real but small.

### Dropped or corrected claims
- **"Calibration…" labelled 'outdated':** relabelled as missing. Nothing outdated exists to replace.
- **"Band can be too narrow":** the direction is ambiguous because PrizePicks tails widen it. Item 8 keeps only the inconsistency and the arb-inflated n.
- **"3–10x evaluation sample":** removed as unsupported.
- **"PrizePicks Flex partial payouts of 0.5x":** the 0.5x payout (2-flex) is not an allowed structure. Only the 0.4x tiers cause the mismatch.
- **"Skip the first 3–4 weeks of each season":** burn-in is needed only at the start of each span, not every season.
- **"Prop CLV is the standard used by PrizePicks":** removed as an unsourced attribution.
- **"Drift caught within ~50–100 bets":** softened to about 100–300 signals for props.