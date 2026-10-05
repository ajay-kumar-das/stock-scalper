# 07 — Entry / Exit Decision Framework

## 1. Decision gates (fixed order)

```
trigger ─▶ G1 session ─▶ G2 regime ─▶ G3 instrument health ─▶ G4 setup quality
        ─▶ G5 edge vs cost ─▶ G6 risk & sizing ─▶ G7 OMS (dup/throttle) ─▶ ORDER
```
Each gate returns PASS or a structured rejection `{gate, code, values, threshold}`. All triggered candidates (passed or rejected) are journalled — this is the "why didn't you buy" answer and research data for missed-opportunity analysis. To bound journal size, SETUP_FORMING is recorded once per setup id, and repeated rejections are deduplicated per (instrument, strategy, reason) per minute with a count.

## 2. Regime model (G2)

Computed every minute from NIFTY 50 (and sector indices), India VIX and breadth of the eligible universe.

| Axis | Measure | States |
|------|---------|--------|
| Trend | NIFTY return since open in units of its 20-day intraday σ, plus slope of 1-min VWAP-distance over 30 min | STRONG_UP (> +1σ), WEAK_UP (+0.3…+1σ), FLAT (±0.3σ), DOWN (< −0.3σ) |
| Volatility | 15-min realised vol of NIFTY vs its 20-day same-time median; VIX change | LOW (< 0.7×), NORMAL, HIGH (> 1.5×), ABNORMAL (> 3× or VIX +15% intraday or index ±1.5% in 15 min) |
| Breadth | % of eligible stocks above their VWAP | supportive (> 55%) / neutral / hostile (< 40%) |
| Character | Variance ratio VR(5-min/1-min) of index and median stock over last 60 min | TRENDING (VR > 1.1) / MEAN_REVERTING (VR < 0.9) / NEUTRAL |

Each strategy declares a permission matrix, e.g. H1 ORB: trend ∈ {STRONG_UP, WEAK_UP, FLAT}, vol ∈ {NORMAL, HIGH}, breadth ≠ hostile, character ≠ MEAN_REVERTING. ABNORMAL volatility blocks every strategy.

Regime detection is lagging by construction; the research framework measures each strategy's expectancy per regime label so the permission matrix is evidence-based (doc 05 R6.3).

## 3. Instrument health (G3)
Data fresh (< 5 s), not quarantined, spread ≤ 1.5× 20-min median and ≤ 8 bps, not in cooldown, below per-instrument trade cap, no unresolved order on this instrument, feature warm-up complete.

## 4. Setup confidence (G4)

Confidence is a calibrated probability-like score, built in two stages:

1. **Evidence score** — each strategy defines 4–8 evidence items with a hypothesis each; each item is mapped to [0, 1] by a fixed monotone function (e.g. RVOL 1.5→0, 4→1). Evidence score = weighted mean (weights fixed in spec).
2. **Calibration** — on development data, fit an isotonic mapping from evidence score → P(trade reaches +1R before −1R). Validation checks monotonicity across quintiles. If calibration fails (non-monotone), confidence = constant and m_conf = 0.75 (no confidence-based sizing).

Common evidence items:

| Evidence | Hypothesis |
|----------|-----------|
| Relative volume at time-of-day | participation → continuation |
| Breakout bar volume vs 20-bar mean | real demand vs drift |
| Close location in bar (close near high) | buyers in control at the trigger |
| Relative strength vs index & sector | institutional accumulation |
| Price vs VWAP and VWAP slope | buyers profitable on average, not trapped |
| Market/breadth alignment | rising tide |
| Room to next resistance in R | reward available |
| Spread & depth on the ask | executable without large slippage |
| Pullback quality (volume contraction %, depth of retrace in ATR) | absence of supply |

Validation also produces a reliability diagram (predicted vs realised p per bucket); expected calibration error > 0.08 → calibration disabled and a conservative fixed p (strategy's validated win rate − 5 pp) is used.

`min_confidence` default 0.55 (calibrated probability of +1R before −1R), raised by +0.1 in soft-loss state.

## 5. Edge vs cost (G5)

```
p        = calibrated P(+target before stop)
W        = expected gain if win  (target − entry − exit slippage)       [₹/share]
L        = expected loss if lose (entry − stop + stop slippage)          [₹/share]
cost     = round_trip_cost(qty, entry, exit)/qty + entry_slippage + half_spread×2
E_net    = p·W − (1 − p)·L − cost
```
PASS iff **E_net ≥ 0.10 × L** (expected net ≥ 0.10R — revised from 0.25R during M1, see doc 13 finding 25) **and** (p·W) / cost ≥ k_cost (default 2.0) **and** W/L ≥ 1.5.

Before calibration exists (early research), p is replaced by the strategy's historical win rate in that regime, and the gate uses the more conservative of the two. **In research runs (R1–R4) the edge and regime gates are measured, not enforced** (`research_config()`): their inputs (p, permitted regimes) must come from evidence, so enforcing a guessed prior would make the research circular. Every candidate's would-be gate outcome is journalled so the gate's own value can be tested.

## 6. Entry mechanics (§7)

- **Entry zone:** each setup defines [ideal, max] — e.g. ORB: ideal = OR high + 1 tick, max = OR high + min(0.25 × ATR_1min×√5, 0.15%). Price above max → *extended* → no chase; setup may re-arm on a pullback into the zone.
- **Order:** BUY LIMIT at min(best_ask, zone_max), validity DAY, TTL 3 s; one re-price allowed within the zone; then cancel.
- **Anti-spike:** reject if the last 1-s return > 4× the 20-min median absolute 1-s return or if the ask side of the book thinned > 60% in the last 2 s (spoof/air pocket).
- **Resistance check:** compute nearest overhead level (day high, prior-day high, round number at ≥ ₹50 multiples for high-priced names, VWAP+2σ); if within 1R → reject (unless the setup *is* the break of that level).
- **Exhaustion check:** reject if price is > 3 daily ATRs from open, or if the last five 1-min bars are all up with expanding range and RSI(1-min,14) > 85 (climax).

## 7. Stop placement (§10)

```
structural_stop = setup-specific invalidation (e.g. OR midpoint, pullback low − 1 tick)
noise_floor     = entry − max(1.2 × median 1-min true range over 30 min, 3 × spread, 4 ticks)
stop            = min(structural_stop, noise_floor)            # never tighter than noise
if (entry − stop)/entry > max_stop_pct (strategy-specific, default 1.0%) → reject setup
```
Rationale: a stop tighter than ordinary 1-minute noise is a donation; a stop wider than the strategy's validated range invalidates the R-distribution assumptions.

## 8. Position management (§9, §11, §12)

Evaluated on every 1-s bar and every 1-min close. Actions are proposed by rules in priority order; the first applicable action executes.

| # | Rule | Action |
|---|------|--------|
| 1 | Hard stop touched (broker SL) | (broker executes) — monitor fill, escalate if SL-limit skipped |
| 2 | Session flatten time / operator exit / risk halt | Exit |
| 3 | **Thesis invalidation**: setup-specific (e.g. ORB: 1-min close back inside opening range; VWAP setup: 1-min close below VWAP − 0.3 ATR) | Exit |
| 4 | **Market shock**: NIFTY −0.4% in 5 min or breadth collapses to hostile | Tighten stop to max(stop, entry − 0.5R) or to break-even if MFE ≥ 1R |
| 5 | **Break-even**: MFE ≥ 1.0R | Stop → entry + costs/qty (true break-even) |
| 6 | **Profit lock**: MFE ≥ 1.5R | Stop → entry + 0.5R; then trail at max(highest high − 2 × ATR_1min, last higher-low − 1 tick) |
| 7 | **Momentum decay**: 3 consecutive 1-min bars with falling volume and closes in lower half, while in profit | Tighten trail to last bar low |
| 8 | **Resistance reached with rejection** (long upper wick ≥ 60% of bar at target zone) | Exit if open profit ≥ 1R, else tighten |
| 9 | **Time decay**: elapsed ≥ t_expect and MFE < 0.5R | Exit (capital trapped in a non-working trade) |
| 10 | **Max hold**: elapsed ≥ t_max | Exit |
| 11 | **Exceptional trend**: MFE ≥ 3R and efficiency ratio > 0.6 | Switch trail from 2×ATR to 3×ATR and extend t_max by 50% (let winners run) |

"Expected future gain < downside risk" (brief §11) is operationalised by rules 7–9: as momentum and time-in-trade evidence decays, the remaining expected gain falls, so the stop tightens or the position exits.

Partial profit-taking is *not* used in v1: with flat ₹20 per order brokerage, an extra exit order costs ₹23.6 incl. GST; it is researched later as a variant (must beat the single-exit baseline net of the extra order cost).

## 9. Market-open handling (§18)

Default rules, each to be validated in research (H1 R1 study compares variants):
- 09:00–09:15 pre-open: compute gap % vs previous close and pre-open indicative volume; gap > 2 × daily ATR → stock excluded from long setups for the first 30 min.
- 09:15–09:20: observe only for all strategies; opening range collection for ORB.
- 09:20–09:45: only strategies whose spec explicitly permits the open (ORB) may enter; spreads must be below 1.5× their *opening-period* median; size × 0.5.
- Volatility of the first 15 min is used to scale stops (via the ATR inputs) — never a fixed %.

## 10. End-of-day (§19)
Entry cutoff 14:45 (configurable earlier per strategy); t_max is truncated so no position is expected to be open at 15:00; flattening 15:00–15:10 with escalation: passive at bid for 30 s → bid − 1 tick → marketable limit at bid − 0.3%.

## 11. Explainability hooks
Every decision object carries `reasons: list[Reason(code, text, values)]`. Example: `EXIT/THESIS_INVALID: "1-min close 1012.40 back inside opening range [1004.00, 1012.55]"`. The journal query `why <trade_id>` renders the full chain: watchlist rank → setup → gates → sizing (with each limiting factor) → order events → each stop move → exit reason → costs → P&L.
