# 09 — Live-Promotion Criteria and Capital Ladder

Running successfully is not evidence of an edge. Promotion is per strategy and requires *all* gates. The system produces a machine-generated **promotion dossier** (metrics, tests, drills, logs) — the operator makes the final decision and signs it (`config/promotions/<strategy>-<version>.yaml`).

## 1. Prerequisites for any real money (gate G-LIVE-0)

1. Strategy passed research stages R0–R8 (doc 05) and paper criteria (doc 08).
2. Infrastructure checklist:
   - Static IP registered with Upstox; order endpoints tested from it; `X-Algo-Name`/algo configuration as required by Upstox at the time.
   - Orders/second hard cap ≤ 3 verified by test.
   - Mode guard test: LIVE cannot start with any missing confirmation (automated test).
   - Daily login/pre-flight procedure documented and rehearsed.
   - Alerts reach the operator's phone (tested).
   - Broker-side protective stop placement verified in sandbox/smallest live qty.
3. Fault-injection suite 100% pass on the exact build to be deployed (build hash recorded).
4. Operator has read the dossier and the current failure-scenario list.

## 2. Capital ladder

| Stage | Capital at risk | Risk/trade | Duration / exit criterion | Promote when | Demote when |
|-------|-----------------|------------|---------------------------|-------------|------------|
| **L0 Live dry-run** | ₹0 — live data, real auth, orders placed as *non-marketable* limit orders 5% away and cancelled immediately | — | 3 sessions | All order lifecycle paths observed against the real broker (ack, cancel, modify, reject), 0 recon breaks | Any unexplained broker behaviour |
| **L1 Micro-pilot** | Allocation ₹50,000–₹1,00,000; 1 position max | ₹100–₹150 (fixed) | ≥ 20 sessions and ≥ 40 trades | Live slippage ≤ 1.25× model; live expectancy not significantly below paper (p > 0.1); 0 operational incidents of severity ≥ 2; simulator recalibrated and strategy still passes | Daily loss limit hit twice in 5 sessions; slippage > 2× model; any severity-1 incident; drawdown > 5% |
| **L2 Small** | ₹2–3 lakh; 2 positions | 0.20% | ≥ 30 sessions, ≥ 100 live trades | Net expectancy lower 90% bound > 0 on live trades alone; drawdown ≤ 5% | as above, or rolling-50 expectancy CUSUM alarm |
| **L3 Standard** | per operator; 3 positions | 0.25% | ongoing; quarterly review | Scale by ≤ 1.5× per step, ≥ 60 sessions between steps, only if capacity analysis shows slippage unchanged at the larger size | as above; any step-up that raises slippage > 20% is reverted |

Rules:
- One strategy at a time enters L1. Additional strategies join only after the first reaches L2 (keeps attribution clean).
- Demotion is automatic (config flag written by the system) and moves down one level; a severity-1 incident moves to paper.
- Scaling decisions use **live after-cost** results only; paper results are not used to justify scale-ups past L1.
- Position size growth is capped by liquidity: qty ≤ 1% of 1-min volume (doc 04) regardless of capital.

## 3. Incident severity

| Sev | Definition | Example |
|-----|-----------|---------|
| 1 | Money/position state wrong or uncontrolled | Accidental short; unprotected position > 10 s; residual overnight position; duplicate position |
| 2 | Safety mechanism failed but no loss resulted | Stop placement retry exhausted; DEFENSIVE mode not entered when it should have been |
| 3 | Degraded operation handled correctly | Feed disconnect with correct DEFENSIVE behaviour |

Each sev-1/2 incident requires a written post-mortem and a regression test before trading resumes.

## 4. What does *not* justify promotion
- A great week. Minimum sample sizes are non-negotiable.
- Gross profitability.
- Win rate.
- Backtest results alone.
- "The bot ran without errors."
