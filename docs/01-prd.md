# 01 — Product Requirements Document

## 1. Problem

Retail intraday trading in Indian cash equities is overwhelmingly loss-making. SEBI's July 2024 study found that over 70% of individual intraday traders in the equity cash segment lost money in FY23, and that loss-makers traded *more often*. The usual causes are well known: trading costs that are large relative to the moves being captured, overtrading, poor execution, no regime awareness, emotional loss-chasing, and strategies "validated" on the same data used to design them.

The product exists to test, with discipline, whether a systematic short-horizon long strategy set can earn **positive net expectancy after realistic costs** on liquid NSE stocks, and to run that strategy set only if the evidence supports it.

## 2. Product vision

A disciplined, automated intraday trading system for NSE cash equities via Upstox that:

- trades only when a measurable edge clearly exceeds total cost and risk,
- protects capital before seeking profit,
- explains every action and every non-action,
- fails safe under uncertainty,
- and treats "no trade today" as a successful outcome when conditions are poor.

## 3. Users

| User | Needs |
|------|-------|
| **Operator (Ajay)** — owns the account and capital | Clear status at a glance, hard safety controls, daily review, confidence the bot is not doing anything unexpected |
| **Researcher (Ajay wearing a different hat)** | Reproducible backtests, honest validation, per-strategy metrics, a journal usable for research |
| **Reviewer (future: anyone auditing the system)** | Traceability from every order to a decision, a rationale, and a rule |

## 4. Goals and measures

| Goal | Primary measure | Target before scaling |
|------|-----------------|-----------------------|
| Positive edge after costs | Net expectancy per trade (₹ and R-multiple), after all charges, spread and slippage | > 0 with 95% confidence (bootstrap lower bound > 0) across out-of-sample + paper |
| Controlled risk | Max drawdown; max daily loss | Max drawdown ≤ 8% of allocated capital; daily loss limit never breached by more than one trade's slippage |
| Execution quality | Slippage vs decision price; fill rate | Realised slippage ≤ 1.25× modelled slippage |
| Operational reliability | Reconciliation breaks; unhandled errors; uptime during session | Zero unresolved reconciliation breaks; zero residual overnight positions |
| Discipline | Trades/day, fee share of gross profit | Fees < 40% of gross profit; no overtrading alerts unaddressed |

Explicit non-goal metrics: win rate, number of trades, gross profit, "% per day". These are tracked but never optimised directly.

## 5. Scope

### In scope (v1)
- NSE cash equities, **intraday (product `I`) only, long only**.
- Universe restricted to highly liquid stocks (see doc 06), initially drawn from F&O-eligible stocks: they have dynamic price bands rather than hard 2–10% circuits, deep books and tight spreads.
- Holding period from ~30 seconds to ~30 minutes; typical 2–15 minutes.
- Four modes: BACKTEST, REPLAY, PAPER, LIVE.
- Multiple independent strategies, each with its own evaluation and kill switch.
- A live monitoring console with manual controls.
- Automatic daily post-session report.

### Designed for, not built in v1
- Intraday short selling (MIS short is permitted in India; architecture is direction-agnostic but v1 rejects `SELL`-to-open).
- Index/stock futures and options.
- Multi-broker support (broker access is behind an adapter interface).

### Out of scope
- Overnight positions of any kind.
- Market-making / HFT strategies that require co-location.
- Discretionary signals, news parsing, social media sentiment (may be researched later as filters only).
- Automatic strategy creation or live self-optimisation (explicitly forbidden by the brief).

## 6. Constraints

| Constraint | Consequence for design |
|-----------|------------------------|
| Costs: ₹20 or 0.1% per order + STT 0.025% (sell) + exchange 0.00307% + SEBI ₹10/cr + stamp 0.003% (buy) + 18% GST on brokerage+exchange+SEBI | Minimum order notional ~₹40–50k so the flat ₹20 stays small relative to the trade. Each strategy's expected gross move per trade must be several times the cost. |
| Retail latency (tens to hundreds of ms round trip, not co-located) | No strategies that depend on being first to a price level. Avoid queue-priority-dependent edges. |
| Upstox rate limit 25 req/s per API | Order throttle well below this; batch quote polling avoided in favour of the WebSocket feed. |
| SEBI: static IP for order APIs; <10 orders/sec to avoid registration; market-protection on market orders | Run from a host with a registered static IP; global order throttle ≤ 3 orders/sec; prefer limit / marketable-limit orders. |
| Daily token; human login required | Daily start-up checklist includes login; bot will not trade without a fresh valid token. |
| Upstox auto square-off 15:25 (15:10 CAS) with a fee | Exclude CAS-eligible stocks; our own flatten deadline is 15:10 with entry cutoff at 14:45. |
| Historical data = 1-min candles only | Bar-based strategies can be backtested from 2022; depth/tick strategies need our own recorded data (recorder runs from M1). |
| Capital is personal and finite | Tiny initial live allocation; scale only by evidence (doc 09). |

## 7. Principles (in priority order when they conflict)

1. **Don't lose track of money.** Unknown state → no new exposure.
2. **Protect capital.** Risk limits outrank strategy signals; manual controls outrank everything.
3. **Earn net, not gross.** Every decision is judged after costs.
4. **Prefer no trade over a marginal trade.**
5. **Be explainable.** If it can't be explained, it doesn't trade.
6. **Change through research, not live tinkering.**

## 8. Success definition

The product succeeds when there is *strong evidence* that:
1. at least one strategy has a repeatable edge after realistic costs, demonstrated out-of-sample and in paper trading with statistically meaningful sample sizes;
2. the system behaves safely during abnormal conditions, demonstrated by fault-injection tests;
3. it is operationally reliable enough for a small, controlled live pilot.

It is also a *legitimate and valuable outcome* for the research to conclude that no strategy clears the bar. In that case the product's job is to say so clearly and not trade.

## 9. Risks to the product itself

| Risk | Mitigation |
|------|-----------|
| No real edge exists at retail cost/latency | Research gates reject strategies early; budget limited before live money |
| Overfitting through many experiments | Experiment registry, counted trials, deflated performance, untouched holdout |
| Paper ≠ live (fills, latency, queue) | Conservative fill model calibrated against live micro-pilot; slippage monitoring with auto-throttle |
| Regulatory/broker API change | Adapter layer; daily pre-flight check of API health and order rejects |
| Operator complacency | Daily report, alerting, mandatory weekly review before scale-ups |
