# 12 — Architecture

## 1. Shape

A single-process, event-driven core with deterministic replay, plus out-of-process tooling (research notebooks, console UI).

```
                ┌───────────────────────── Engine (one process, asyncio) ─────────────────────────┐
 MarketData ──▶ │ DataHub ─▶ Validators ─▶ BarBuilder ─▶ FeatureStore                             │
 Adapter        │      │                                    │                                       │
 (Upstox WS /   │      ▼                                    ▼                                       │
  Replay /      │  RegimeModel     Selector (universe, ranking, watchlist)                          │
  Historical)   │      │                │                                                           │
                │      └──────▶ StrategyHost (plug-ins) ─▶ candidates ─▶ DecisionPipeline (gates)  │
                │                                                           │                       │
                │                RiskEngine ◀───────────────────────────────┤ sizing / limits       │
                │                                                           ▼                       │
                │      PositionManager ◀──────────────▶ OMS (order state machines, throttle,        │
                │                                          idempotency, exit-by-modify)             │
                │                                                           │                       │
                │   Reconciler ◀───── broker truth ─────────────────────────┤                       │
                │   SessionController (state machine, clock, cutoffs)       ▼                       │
                │   Journal (SQLite WAL, append-only)        ExecutionAdapter                       │
                └─────────────────────────────────────────── (Sim / Paper / UpstoxLive) ────────────┘
                         │ events                                    ▲ commands
                         ▼                                           │
                 Console API (FastAPI, localhost) ◀──── operator UI (browser) / CLI
```

## 2. Key design decisions (ADRs)

**ADR-1 Python 3.11+ with asyncio.** Holding periods are minutes and decision latency budgets are ~50 ms; Python is ample, and the research stack (pandas/numpy) shares code with the engine. Hot loops (feature updates) use incremental O(1) calculators. *Rejected:* Rust/Go core (faster but splits research and live code, raising the "backtest ≠ live" risk that matters more here than microseconds).

**ADR-2 Event-driven core with an injectable clock.** All components receive events with timestamps and query `clock.now()`. BACKTEST uses a simulated clock advanced by the data; LIVE uses wall time. Same strategy/risk/OMS code in all modes (FR-MODE-5).

**ADR-3 Broker adapter interface.** `ExecutionAdapter` (place/modify/cancel/order_book/positions/trades) and `MarketDataAdapter` (subscribe/unsubscribe/stream). Implementations: `SimExecution` (backtest/replay/paper), `UpstoxExecution` (live only, constructed only by the mode guard). Upstox specifics (tags ≤ 40 chars, `market_protection`, rate limits, static-IP errors) live only in the adapter.

**ADR-4 Storage.** SQLite (WAL) for journal, orders, positions, experiment registry — transactional, zero-ops, inspectable. Parquet (pyarrow) for ticks, depth and bars, partitioned `data/{kind}/date=YYYY-MM-DD/`. *Rejected:* Postgres/Timescale (ops burden without benefit at this scale).

**ADR-5 Decimal-safe prices.** Prices handled as integer paise internally in OMS/costs (float only in features). Avoids tick-rounding bugs (e.g. 1012.55 vs 1012.5499999).

**ADR-6 Broker-resident protective stops + exit-by-modify** (doc 03 §4). Safety does not depend on the bot being alive.

**ADR-7 Configuration.** YAML, schema-validated at start; risk parameters can be tightened at runtime via console, loosened only via restart + config change (journalled). Strategy parameters are versioned files hashed into every journal record.

**ADR-8 Console.** FastAPI + server-sent events + a single static HTML page served on localhost. No cloud exposure of controls. Phone alerts via Telegram bot (outbound only).

**ADR-9 Deployment.** Recorder/paper can run on the operator's machine. LIVE runs on a small VPS (Mumbai region for latency) with a static IP registered at Upstox, systemd-managed, with NTP. Daily login via a local helper that completes the OAuth redirect and writes the token to the VPS over SSH.

**ADR-10 Secrets.** API key/secret and daily access token are read from the OS keyring or environment, never written to config files, logs or the journal; a logging filter redacts anything token-shaped. The console binds to 127.0.0.1 only.

**ADR-11 Dead-man's switch.** The engine sends a heartbeat every 30 s to an external monitor; missing heartbeats during market hours alert the operator's phone even if the host is down.

## 3. Package layout

```
scalper/
  core/        types (Instrument, Order, Fill, Position, Bar, Tick), clock, events, money/tick utils
  costs/       Upstox cost model (versioned)
  data/        adapters (historical, replay, upstox ws), bar builder, validators, recorder
  features/    incremental indicators (VWAP, ATR, RVOL, efficiency ratio, opening range...)
  regime/      regime model
  selection/   universe filters, opportunity score
  strategies/  base interface + plug-ins (orb, vwap_pullback, ...)
  decision/    gate pipeline, confidence, edge-vs-cost
  risk/        limits, sizing, risk state machine
  oms/         order state machine, OMS, throttle, idempotency
  execution/   adapters: sim (bar + tick fill models), upstox (live)
  portfolio/   positions, position manager, P&L
  session/     session controller, mode guard, reconciler
  journal/     SQLite journal, explain queries
  backtest/    engine runner, walk-forward, experiment registry
  analytics/   metrics, reports, drift monitors
  console/     API + UI
tests/         unit, invariants, look-ahead, faults
docs/
config/
```

## 4. Invariants (asserted in code and tested)

1. Per instrument: ≤ 1 working entry order and ≤ 1 open position.
2. Working *closing* quantity ≤ |position| (no accidental reversal/short); opening orders only when flat in that instrument.
3. Every open position has a non-terminal protective stop within 3 s of the first fill, or is exiting.
4. Order `filled_qty` is monotonic non-decreasing and ≤ `qty`.
5. No new entry order is created unless session state = TRADING and risk state allows.
6. Strategy code cannot call the execution adapter (only emits candidates).
7. No feature reads data with timestamp > clock.now().
