# 08 — Paper-Trading Model and Acceptance Criteria

## 1. Simulated execution model (shared by BACKTEST, REPLAY, PAPER)

The fill simulator must be *pessimistic by default* and is later calibrated against the live micro-pilot.

| Effect | Tick/depth data (REPLAY, PAPER) | 1-min bars (BACKTEST) |
|--------|-------------------------------|-----------------------|
| Latency | Order reaches "exchange" after `latency_ms` ~ lognormal(median 150 ms, p95 400 ms); the book at arrival time is used, not at decision time | Decision at bar close t; order active from t + 1 bar's open + latency; no fills inside the decision bar |
| Marketable buy limit | Walks the ask side of the depth snapshot at arrival; fills only up to available qty at prices ≤ limit; remainder rests | Fill at next bar open + slippage(spread/2 + impact) if open ≤ limit; else rests |
| Passive limit (rests) | Queue position = displayed qty at that price at arrival; fills only after traded volume at that price exceeds queue ahead (trade-through or volume at level); partial fills allowed | Fills only if a later bar's low < limit (strict trade-through), at the limit price; fill qty ≤ 10% of bar volume |
| Stop-limit sell | Triggers when LTP ≤ trigger; then behaves as a sell limit at `limit`; if the market gaps below limit → resting unfilled (realistic SL-limit risk) | Trigger if bar low ≤ trigger; fill at min(trigger, bar open) − slippage, but not below limit; if bar low < limit and open < limit → unfilled (escalation logic applies) |
| Intrabar ordering | n/a | If stop and target both inside one bar → **stop first** |
| Slippage/impact | Book-walk + extra `impact_bps = 10 × (qty / depth_top5)` capped | spread/2 (instrument median) + impact_bps per above using ADV proxy |
| Partial fills | Yes from book depth and queue | Yes, via volume cap |
| Missed fills | Yes (queue, TTL expiry, price moves away) | Yes (limit not reached next bar) |
| Rejections | Random rejection at configured rate (default 0.5%) + deterministic rejects (price band, qty freeze, insufficient margin) | same |
| Costs | Full Upstox cost model per fill | same |
| Acknowledgement delays | Random ack delay; 0.2% of orders produce "no ack" (UNKNOWN) to exercise recovery | same |

**Spread assumption in bar backtests (no historical depth exists):** half-spread = max(1 tick, 2 bps) for the top liquidity bucket (ADV ≥ ₹500 cr), max(1 tick, 3 bps) for ₹150–500 cr — i.e. full spread of 4–6 bps paid per round trip — stressed ×2 in robustness tests, and replaced by per-stock, per-time-of-day medians from our recorded depth once ≥ 20 sessions are available.

**Prohibited:** filling at the decision price, filling passive limits on touch, ignoring the spread, applying costs only on net P&L, using bar close to decide and fill in the same bar.

## 2. Paper-trading acceptance criteria (gate from paper → live pilot)

Measured per strategy (and for the portfolio). All must pass.

### Statistical
| Criterion | Threshold |
|-----------|-----------|
| Trading sessions in paper | ≥ 30 sessions (≈ 6 weeks), spanning ≥ 2 distinct regime labels |
| Trades per strategy | ≥ max(100, n_req) where n_req = (2.5·σ_R / E_R)² from the strategy's validated per-trade σ and expectancy in R (portfolio ≥ 200). The report prints the minimum detectable effect for the achieved n. |
| Net expectancy after costs (paper alone) | > 0 (point estimate) — paper's role is *consistency*, not standalone proof |
| Significance (holdout + paper combined) | block-bootstrap (by day) 95% lower bound of net expectancy > 0 |
| Profit factor (net) | ≥ 1.25 |
| Max drawdown | ≤ 4% of paper capital |
| Agreement with backtest | per-trade R distribution not significantly worse (Mann–Whitney p > 0.05) and expectancy ≥ 50% of validated backtest expectancy |
| Fee share | Total costs ≤ 50% of gross profit |
| Concentration | No stock > 20% and no day > 25% of net profit |

### Execution
| Criterion | Threshold |
|-----------|-----------|
| Modelled slippage stability | rolling slippage within ±25% of the model on the shadow-market check (§3) |
| Entry fill rate | within the backtest's expected range ±15 pp |
| Orders hitting UNKNOWN | all resolved automatically; none left unresolved > 10 s |

### Operational
| Criterion | Threshold |
|-----------|-----------|
| Unresolved reconciliation breaks | 0 |
| Residual positions at 15:15 | 0 (all sessions) |
| Unhandled exceptions in trading loop | 0 in last 10 sessions |
| Session uptime (09:15–15:15) | ≥ 99% across the period; all gaps handled by DEFENSIVE mode |
| Fault-injection suite (doc 10) | 100% pass, run in REPLAY on the final build |
| Kill-switch drills | exit-all and emergency tested in paper ≥ 3 times; complete flatten < 10 s simulated |
| Restart drills | ≥ 5 mid-session restarts with positions open; all recovered correctly |

### Process
- Daily post-session report reviewed and acknowledged by the operator for every paper session.
- No code/parameter changes to a strategy during its paper evaluation window; a change resets that strategy's window (infrastructure fixes are allowed if they don't touch strategy/risk logic and pass regression replays).

## 3. Shadow-market check for fill realism

While in PAPER, every simulated order is also scored against what *actually* traded afterward in the live feed: for a simulated passive buy at price P, did real trades occur at ≤ P with cumulative volume exceeding the displayed queue at P? This checks the queue model against reality without risking money. Before the live pilot, simulated-vs-shadow fill agreement must be ≥ 85% for passive orders.

## 4. Live micro-pilot calibration loop

During the live pilot (doc 09 stage L1), every real fill is compared with what the simulator *would have* produced for the same order at the same moment. The slippage and fill-probability parameters of the simulator are updated (offline, versioned) from these pairs, and all strategies are re-backtested with the calibrated model. If calibrated expectancy falls below the doc 05 thresholds, the strategy returns to research.
