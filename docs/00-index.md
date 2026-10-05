# Stock Scalper — Design Document Set

Status: **M0 design complete · M1 foundation complete (69 tests; see [reviews/M1.md](reviews/M1.md)) · next: M2 data & replay.**
Owner: Ajay · Prepared 2026-10-05

| # | Document | Purpose |
|---|----------|---------|
| 01 | [PRD](01-prd.md) | Why the product exists, who uses it, goals, non-goals, constraints, success definition |
| 02 | [Functional requirements](02-functional-requirements.md) | Numbered, testable requirements (FR-xxx) traced to the business brief |
| 03 | [Trading lifecycle](03-trading-lifecycle.md) | Session, opportunity, order and position state machines |
| 04 | [Risk-control model](04-risk-control-model.md) | Layered limits, sizing formula, kill switches, resume conditions |
| 05 | [Strategy research framework](05-strategy-research-framework.md) | Hypothesis → validation pipeline, data splits, rejection rules, initial hypothesis catalogue |
| 06 | [Stock-selection framework](06-stock-selection-framework.md) | Universe, hard filters, opportunity ranking |
| 07 | [Entry/exit decision framework](07-entry-exit-framework.md) | Edge-vs-cost gate, confidence score, stops, profit protection, time exits |
| 08 | [Paper-trading acceptance](08-paper-trading-acceptance.md) | Fill realism rules and the exit criteria from paper |
| 09 | [Live-promotion criteria](09-live-promotion-criteria.md) | Objective gates and the capital ladder |
| 10 | [Failure scenarios](10-failure-scenarios.md) | FMEA-style catalogue with detection + defensive response |
| 11 | [Milestone plan](11-milestone-plan.md) | Stage-gated plan with go/no-go reviews |
| 12 | [Architecture](12-architecture.md) | Components, data flow, technology choices (ADRs) |
| 13 | [Critical design review](13-design-review.md) | Gaps found in 01–12 and how each was resolved |

## Five decisions that shape everything

1. **Edge must clear ~15–30 bps of all-in cost.** Upstox intraday brokerage is min(₹20, 0.1%) per order; with STT, exchange, SEBI, stamp and GST a round trip is ≈27 bps at ≤₹20k notional per order, ≈13 bps at ₹50k, ≈8 bps at ₹1L — *before* spread and slippage. Sub-minute "tick scalping" against this cost floor without exchange co-location is not a credible edge. Target holding is **~1–20 minutes**, targeting moves of 0.4–1.5%.
2. **One code path for all modes.** Strategy, risk and order-management code is identical in BACKTEST, REPLAY, PAPER and LIVE; only the clock, market-data source and execution adapter change. This is the main defence against "it worked in backtest".
3. **The broker is the source of truth.** Local state is a cache. Every restart and every inconsistency triggers reconciliation against Upstox before any new exposure.
4. **A broker-resident protective stop exists for every open position.** If the bot dies, the account is still protected. Exits are done by *modifying* that stop order, never by placing a second sell — which prevents accidental shorts.
5. **Do-nothing is the default.** Trading is permitted only when the regime gate, the instrument gate, the setup gate, the cost gate and the risk gate all pass. Every rejection is journalled.

## Regulatory/operational facts the design depends on (verified Oct 2026)

- SEBI retail algo framework, live 1 Apr 2026: static IP mandatory for order place/modify/cancel endpoints; >10 orders/sec requires exchange registration (we stay far below); plain market orders replaced by market-price-protected orders (`market_protection`); one active API app per user.
- Upstox API rate limit: 25 req/s, 250 req/min, 1000 req/30 min per API per user.
- Upstox market data feed v3: protobuf over WebSocket; `full` mode = LTP, 5-level depth, 1-min OHLC, ATP (VWAP), volume, total buy/sell qty; 2 connections; up to 1,500–2,000 instruments in full mode.
- Historical candles v3: 1-minute bars since Jan 2022 (max 1 month per request). No historical ticks/depth → we record our own.
- Upstox intraday auto square-off: 15:25 (non-CAS stocks), 15:10 (CAS-eligible); intraday orders refused after 15:22 / 15:09; auto square-off fee ₹88.50.
- Access tokens are daily; a human login is required each trading day (fits the "user retains control" requirement).
