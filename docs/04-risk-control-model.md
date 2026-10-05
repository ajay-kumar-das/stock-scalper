# 04 — Risk-Control Model

Risk is controlled in five layers. A lower layer can never override a higher one. Manual controls sit above all of them.

```
L0  Operator controls (pause, exit-all, emergency)           ── absolute precedence
L1  System integrity  (state certainty, data health, broker) ── DEFENSIVE / EMERGENCY
L2  Account limits    (daily loss, drawdown, exposure)       ── HALTED_FOR_DAY / block entry
L3  Strategy limits   (per-strategy drawdown, drift)         ── throttle / suspend strategy
L4  Trade limits      (sizing, stop, liquidity)              ── size down / reject trade
```

## 1. Definitions

- **Capital (C):** capital *allocated to the bot* (`risk.capital`), not the full account balance. Read from config, cross-checked against broker available margin at start; the bot uses min(config, available funds).
- **R:** the rupee amount at risk on a trade = qty × (entry − stop + expected stop slippage) + round-trip costs.
- **Day P&L:** realised net P&L + unrealised P&L marked at *bid* (the price we could actually sell at), after estimated exit costs.

## 2. Default limits (configurable, validated at start; tighter-only changes allowed intraday)

| Limit | Default | Breach action |
|-------|---------|---------------|
| Risk per trade | 0.25% of C | size reduced to fit |
| Max capital per position (notional) | 25% of C × leverage cap | size reduced |
| Leverage cap (gross notional / C) | 2.0× (even though MIS allows ~5×) | block new entries |
| Max simultaneous positions | 3 | block |
| Max positions per sector | 1 | block |
| Max correlated exposure | sum of notional in positions with 20-day intraday return correlation > 0.7 ≤ 35% of gross cap | block |
| Max open risk (sum of R across open positions) | 0.75% of C | block |
| **Daily loss limit (hard)** | **1.0% of C** (net, incl. unrealised at bid) | HALTED_FOR_DAY |
| Soft daily loss | 0.6% of C | size × 0.5, min confidence +0.1 |
| Max consecutive losses | 4 (system) / 3 (per strategy) | System: 30-min cooldown at 4; halt-for-day at 6. Strategy: 60-min cooldown at 3 and m_perf = 0.5 for the rest of the session |
| Margin | pre-trade check: required MIS margin (Upstox margin API in LIVE; simulator uses per-stock margin %, default 20% of notional) ≤ available margin × 0.8 | reject trade |
| Max drawdown from equity peak (rolling) | 5% → strategy review mode (half size); 8% → stop live, back to paper | |
| Max trades per day | 20 (system), 6 per strategy, 2 per instrument | block |
| Order rate | ≤ 3/s, ≤ 120/min | queue/deny |
| Max single-trade loss realised | 2R (slippage blow-out) | flag + review; 2 such events/day → DEFENSIVE |

Rationale: with 0.25% risk per trade and a 1% daily stop, the worst normal day is ~4 full losers. Daily loss 1% with expectancy-positive strategies means a typical bad month is −5% to −8%, which the drawdown limit catches.

**Daily-loss projection check (pre-trade):** a new trade is blocked if *(current day loss + open risk + new trade R) > hard daily limit*. This ensures the hard limit is not breached by trades already permitted.

## 3. Position sizing

```
per_share_risk   = (entry − stop) + expected_stop_slippage(instrument, volatility)
risk_budget      = C × risk_per_trade × m_conf × m_regime × m_perf × m_dd
qty_risk         = floor(risk_budget / per_share_risk)
qty_notional     = floor(max_position_notional / entry)
qty_liquidity    = floor(min(0.01 × avg_1min_volume_20d_same_time_bucket,
                             0.20 × ask_qty_top5))
qty_exposure     = floor(remaining_gross_capacity / entry)
qty              = min(qty_risk, qty_notional, qty_liquidity, qty_exposure)
reject if qty × entry < min_order_notional (default ₹40,000)  — cost floor
```

Multipliers (all ≤ 1.0 — **no multiplier can increase risk above the base**):

| Multiplier | Values |
|-----------|--------|
| m_conf (setup confidence) | 0.5 at threshold → 1.0 at high confidence (linear); only used if confidence is calibrated (FR-SIG-3), else fixed 0.75 |
| m_regime | 1.0 in strategy's best regime, 0.5 in permitted-but-weaker regimes |
| m_perf (recent strategy performance) | 1.0 normal; 0.5 if rolling-20 expectancy below control limit; 0 if suspended |
| m_dd (account state) | 1.0; 0.5 after soft daily loss or drawdown > 5% |

**Anti-revenge rule:** m_perf and m_dd only recover at session boundaries and only one step at a time (0.5 → 0.75 → 1.0), after the triggering condition has cleared for a full session.

**Minimum notional implication:** with C = ₹2,00,000 and 0.25% risk (₹500), a ₹50k position needs per-share risk ≤ 1% of price. Setups with wider stops are sized below the cost floor and are rejected. This is intentional: the cost floor and the risk budget jointly define the tradable set. The operator should expect the system to need C ≥ ~₹2–3 lakh for meaningful activity; at lower capital it will (correctly) trade rarely.

## 4. Kill switches and resume conditions

| Trigger | State | Resume condition |
|---------|-------|------------------|
| Hard daily loss | HALTED_FOR_DAY: cancel working entries; **exit all open positions** via the ladder (30 s passive at bid → marketable limit); no new entries | Next session, after operator acknowledges the post-session report (`scalper ack-halt <date>`) |
| 6 consecutive losses | HALTED_FOR_DAY | as above |
| Drawdown > 8% | LIVE disabled (config flag `live.suspended=true` written) | Root-cause review + paper period of ≥ 10 sessions meeting doc 08 criteria |
| Order state UNKNOWN | DEFENSIVE | Reconciliation resolves it |
| Position mismatch broker vs local | DEFENSIVE | Broker state adopted + operator notified; auto-resume only if mismatch is explained (e.g. late fill update) |
| Data stale (global) | DEFENSIVE | Feed healthy for 30 s + fresh recon |
| 3 order rejects in 5 min | DEFENSIVE | Operator resume, or 15 min elapsed with a successful test (non-marketable, immediately cancelled) order in LIVE |
| Broker API errors > 5 in 1 min / auth failure | DEFENSIVE → EMERGENCY if positions cannot be managed | Operator |
| Strategy drift breach | strategy suspended | Research review only |
| Slippage > 2× model over rolling 10 trades | system size × 0.5; > 3× → DEFENSIVE | Next session + review |
| Clock drift > 1 s | DEFENSIVE | Re-sync |

## 5. What risk management will *not* do

- Average down. Adding to a losing position is prohibited.
- Widen a stop. Stops can only tighten.
- Increase size to recover losses; trade more frequently after losses.
- Hold past 15:10 for any reason.
- Trade a stock the operator has manually blacklisted that day.

## 6. Correlation and sector data

- Sector mapping from NSE industry classification (instrument master + static mapping file, refreshed monthly).
- Correlation matrix of 5-minute returns over the trailing 20 sessions, recomputed pre-market for the eligible universe.
- Index beta tracked: if all open positions have beta > 1 to NIFTY and NIFTY breaks down (−0.4% in 5 min), the position manager tightens all stops (doc 07 §6).
