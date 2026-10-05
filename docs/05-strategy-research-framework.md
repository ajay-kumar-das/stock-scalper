# 05 — Strategy Research Framework

Research is treated as an experiment pipeline with pre-registered hypotheses and hard rejection rules. The researcher's job is to try to kill each idea; only survivors reach paper trading.

## 1. Pipeline (each stage is a gate)

| Stage | Output | Gate to proceed |
|-------|--------|-----------------|
| R0 Hypothesis card | Written card (template §6): mechanism, why it persists, who is on the other side, expected signature in data | Plausible mechanism; expected gross move per trade ≥ 3× round-trip cost at target notional |
| R1 Exploratory study (dev data only) | Event study: forward returns after the setup condition, 1–30 min horizons, vs unconditional baseline | Conditional mean forward return beats baseline by ≥ 2× cost, t-stat ≥ 2.5, in ≥ 60% of months |
| R2 Strategy spec | Exact entry, invalidation, exits, risk, ≤ 3 free parameters, others fixed from rationale | Spec frozen and version-tagged *before* validation run |
| R3 Dev backtest + walk-forward | Rolling 6-month fit / 1-month test across dev period | OOS walk-forward expectancy > 0 after costs; parameter plateau (neighbours within ±20% retain ≥ 70% of expectancy) |
| R4 Validation backtest | One run on validation period with frozen spec | Net expectancy > 0, profit factor ≥ 1.2, ≥ 150 trades, cost stress passes (§4) |
| R5 Holdout test | One run, once, on the untouched holdout | Same criteria as R4; failure = reject (no re-tuning on holdout, ever) |
| R6 Robustness | Regime, stock, month, time-of-day, cost/slippage stress, random-entry benchmark | All in §4 |
| R7 Paper trading | Live data, simulated fills (doc 08) | Doc 08 criteria |
| R8 Predicted vs actual | Compare paper trade distribution with backtest distribution | No significant deterioration (§5) |
| R9 Live pilot | Doc 09 | Doc 09 |

A strategy that fails a gate is **rejected and archived with the reason**. It may be resubmitted only as a *new* hypothesis with a materially different mechanism, and counts toward the trial budget.

## 2. Data partitioning (fixed now, before any research)

| Partition | Period | Use |
|-----------|--------|-----|
| Development | 2022-01-01 → 2024-12-31 | Exploration, walk-forward fitting |
| Validation | 2025-01-01 → 2025-12-31 | One frozen-spec run per strategy version |
| Holdout | 2026-01-01 → 2026-08-31 | Touched **once** per strategy, at R5 |
| Live-forward | 2026-09-01 → | Paper trading; never used for fitting |

The holdout is enforced by code: the data loader refuses holdout dates unless the run is tagged `--stage holdout` and the strategy version has a registered R4 pass; each holdout access is logged in the experiment registry.

Coverage note: 2022 (bearish/choppy H1, recovery H2), 2023 (steady bull), 2024 (bull then sharp correction from Oct), 2025, 2026 — multiple regimes are represented in each partition except holdout, which is accepted.

## 3. Bias controls

| Bias | Control |
|------|---------|
| Look-ahead | Event-driven engine: features computed from events with timestamp ≤ now; bar data is released only at bar close (a 09:16 bar is visible at 09:17:00.000+latency). Unit test: shuffling future bars cannot change past decisions. |
| Survivorship | Universe built daily from that day's NSE bhavcopy (includes subsequently delisted/suspended names) and that day's F&O eligibility list where available. |
| Unrealistic fills | Doc 08 fill model; bar backtests use conservative intrabar ordering; passive fills require trade-through. |
| Costs ignored | Full Upstox cost model on every fill; results always reported pre- and post-cost. |
| Data snooping | Experiment registry counts every backtest run per hypothesis; reported Sharpe is deflated for the number of trials (Bailey & López de Prado DSR); minimum trades scales with trials. |
| Parameter overfit | ≤ 3 free parameters; plateau requirement; walk-forward only. |
| Selection of best-of-many | Strategies are promoted on validation + holdout, not dev; the dev "best variant" is not automatically chosen — the *median-performing* plateau point is used. |
| Corporate actions | Use unadjusted intraday prices (intraday only, no overnight hold), but drop sessions with splits/bonus ex-dates for affected symbols. |
| Time-zone/timestamp errors | All timestamps stored as tz-aware IST; bar labelling convention (start-time) tested. |

## 4. Robustness requirements (R6)

All must pass:
1. **Cost stress:** expectancy remains > 0 with costs × 1.5 *and* slippage × 2.
2. **Latency stress:** entry delayed by an additional 1 bar (bar strategies) / 1 s (tick strategies) retains ≥ 50% of expectancy.
3. **Regime breakdown:** no permitted regime has significantly negative expectancy (if one does, it is removed from the permitted list and the strategy re-validated — counts as a new version).
4. **Concentration:** no single stock > 20% of net profit; no single month > 30% of net profit; top 5% of trades removed → profit factor still ≥ 1.0.
5. **Time stability:** positive net expectancy in ≥ 60% of months in validation+holdout.
6. **Random-entry benchmark:** same exit logic, random entries stratified to match the strategy's distribution of stock, time-of-day bucket and regime label (1,000 simulations): strategy expectancy exceeds the 95th percentile.
7. **Volatility doubling scenario:** run on top-decile volatility days only — must not lose more than 2× average daily loss limit per day on average.
8. **Statistical significance:** bootstrap (block, by day) 95% CI of net expectancy per trade excludes zero on validation+holdout combined; deflated Sharpe > 0.

## 5. Predicted-vs-actual monitoring (R8, and continuously live)

For each strategy the validated baseline gives distributions for: trades/day, win rate, avg win/loss in R, holding time, slippage. Paper/live results are tested against these:
- Expectancy: CUSUM on per-trade R vs baseline mean; alarm at h = 4σ.
- Win rate: binomial test after every 20 trades; alarm at p < 0.05 (below baseline).
- Slippage: mean of last 10 vs model; alarm at 2×.
- Trade frequency: outside [0.5×, 2×] baseline over 5 sessions → investigate (often a bug, not a market change).

## 6. Hypothesis card template

```
ID / name / version:
Market hypothesis:          what behaviour is exploited
Mechanism:                  why it exists (who is forced/slow/uninformed)
Why it persists:            why arbitrage has not removed it at our horizon/cost
Counterparty:               who loses on the other side
Signature in data:          what an event study should show
Entry:                      exact trigger + entry zone
Invalidation:               the price/condition that proves the idea wrong
Exits:                      target logic, trail, thesis-decay, time stop
Risk:                       stop placement rule, expected R distribution
Permitted regimes:
Free parameters (≤3):       with rationale for ranges
Fixed parameters:           with rationale
Expected trades/day, holding time, gross move/trade:
Kill criteria:
```

## 7. Initial hypothesis catalogue (prioritised)

Priority reflects (a) published/empirical support, (b) testability with 1-min bars, (c) gross move size relative to cost.

### H1 — Stocks-in-play opening-range breakout (priority 1)
- **Hypothesis:** Stocks with abnormal early participation (relative volume in the first 5–15 min ≥ 3× normal for that time of day, typically due to news/results/flows) tend to continue in the direction of the opening-range break.
- **Mechanism:** Information is absorbed gradually; institutions split large orders over the day; attention-driven flows cluster. Published US evidence (Zarattini, Barbon & Aziz, 2024, 5-min ORB on "stocks in play") reports strong gross results concentrated in high relative-volume names; independent replications report that edge on *indices* disappears after costs, which is why the relative-volume filter is essential, not optional.
- **Counterparty:** liquidity providers fading early moves; late-informed participants.
- **Entry:** break above the 5-min (or 15-min) opening range high with breakout bar volume ≥ 1.5× rolling average, price above VWAP, only for top-ranked relative-volume names; long only in v1.
- **Invalidation:** back inside range below OR midpoint, or below VWAP.
- **Exits:** trail by structure / ATR; thesis decay if relative volume collapses; time stop 30–60 min.
- **Risk to idea:** Indian opening is noisy (pre-open auction, gap effects). The opening 5 minutes may need to be observed rather than traded → R1 compares 5-min vs 15-min ranges.

### H2 — VWAP pullback continuation in relative-strength leaders (priority 1)
- **Hypothesis:** In stocks trending up intraday with strong relative strength vs NIFTY and sector, pullbacks to VWAP (or the 9/20-period EMA band on 1-min) on declining volume are bought, and the trend resumes.
- **Mechanism:** VWAP is the dominant execution benchmark for institutional orders; buy-side algos accumulate near/below VWAP. Declining pullback volume indicates absence of supply.
- **Entry:** first or second pullback to VWAP zone after a confirmed trend (higher highs, price > VWAP for ≥ 30 min), entry on reclaim of prior 1-min bar high with volume uptick.
- **Invalidation:** 1-min close below VWAP − k·ATR or below pullback low.
- **Exits:** prior high retest (partial, later), trail below higher lows, time stop 20 min.

### H3 — Volatility-contraction breakout with volume expansion (priority 2)
- **Hypothesis:** After an intraday range contraction (narrow 1-min ranges, Bollinger width in bottom decile of the day), a break with volume expansion in a stock with positive relative strength continues for several minutes.
- **Mechanism:** compression reflects balance of orders; resolution with volume reflects arrival of a directional participant; stop orders above the range add fuel.
- **Risk:** false breakouts are frequent → requires the failed-breakout statistics from H6 research to set expectations.

### H4 — Market-level intraday momentum as a regime filter (priority 2, filter not strategy)
- **Evidence:** Gao, Han, Li & Zhou (2018, JFE): the first half-hour market return predicts the last half-hour return. We test its Indian analogue and, more importantly, use market direction/breadth as a *permission* filter for long setups.

### H5 — Failed breakdown / VWAP reclaim (long reversal) (priority 3)
- **Hypothesis:** When a stock in an uptrending market breaks a key intraday low (trapping shorts / triggering stops) and quickly reclaims it with volume, it squeezes up.
- **Note:** mean-reversion setups are only permitted in range/mean-reverting regimes.

### H6 — Order-book imbalance timing (priority 4, research only)
- **Evidence:** Cont, Kukanov & Stoikov (2014): order-flow imbalance explains short-horizon price changes. At retail latency and with ~10–15 bps costs, OFI alone is very unlikely to pay. Tested only as an *entry-timing filter* for H1–H3 using our own recorded depth data (≥ 40 sessions needed).

### Explicitly deprioritised
- Pure indicator crossovers (MACD/RSI/EMA cross) without a mechanism — no hypothesis, high snooping risk.
- Sub-minute scalps targeting < 20 bps — below the cost floor.
- Strategies dependent on queue priority — not achievable without co-location.

## 8. Experiment registry

Each run writes: run_id, hypothesis_id, strategy version (git hash + param hash), data partition and date range, universe definition hash, cost model version, fill model version, metrics, and whether the holdout was touched. The registry (SQLite) is the source of the trial count for deflated statistics.
