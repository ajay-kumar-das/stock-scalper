# 11 — Milestone-Based Development Plan

Every milestone ends with a **go/no-go review** recorded in `docs/reviews/Mx.md`: what was built, which FRs are verified by tests, open risks, and whether proceeding is justified. Research outcomes can stop the project at M4 — that is a valid result.

| Milestone | Stage (brief §38) | Deliverables | Exit criteria (go/no-go) |
|-----------|-------------------|-------------|--------------------------|
| **M0 Design** ✅ | Discovery, product design | Docs 00–13, critical review | Review complete; gaps resolved or tracked |
| **M1 Foundation** ✅ | — | Core domain model; Upstox cost model; tick-size & price utilities; order state machine; risk engine & sizing; simulated execution (bar model); event-driven backtester; metrics (all §23); journal; mode guard; synthetic-data test harness; unit + invariant tests | All M1 tests green; look-ahead test passes; mode guard tests pass; cost model matches Upstox calculator within ₹0.05 on 10 reference cases |
| **M2 Data & replay** | Market research | Upstox v3 auth helper; historical candle downloader; NSE bhavcopy loader → point-in-time universe; WebSocket feed client (protobuf) + tick/depth **recorder**; REPLAY engine on recorded ticks; tick-level fill model (queue, latency, depth walk); data validators | 2022–2026 1-min data for universe stored; recorder captured ≥ 5 live sessions without gaps; replay deterministic; bars built from our feed agree with Upstox historical candles (OHLC within 1 tick on ≥ 99% of bars, volume within 2%); NSE holiday calendar and corporate-announcement calendar loaded |
| **M3 Market research** | Market research | Universe stats (spreads, RVOL distributions, intraday volatility profile by time of day), regime labelling of history, selection-score validation (doc 06 §6), opening-period study | Report: is there enough movement vs cost in the universe? Do selection scores predict larger moves? **No-go if tradable-move/cost ratio is insufficient.** |
| **M4 Strategy research** | Strategy research, backtesting | Hypothesis cards H1–H3 (+H5); R1 event studies; R2 specs; R3 walk-forward; R4 validation; R5 holdout; R6 robustness; experiment registry | ≥ 1 strategy passes R0–R6. **If none: stop, write the negative-result report, do not proceed to live.** |
| **M5 Simulation & hardening I** | Simulation | Full engine in REPLAY with recorded data; fault-injection suite F1–F32; recovery logic against simulated broker; console v1 (read-only) | All fault tests pass; replay of 20 recorded sessions matches bar-backtest expectations within tolerance |
| **M6 Paper trading** | Paper trading | PAPER mode live on feed; console with controls; alerts; post-session report; shadow-market fill check; drift monitors | Doc 08 acceptance criteria (≥ 30 sessions) |
| **M7 Performance review** | Performance review | Predicted-vs-actual analysis; cost/slippage review; adversarial review round 2; promotion dossier | Dossier supports promotion; operator sign-off |
| **M8 Hardening II** | Hardening | Live adapter (orders, modify, cancel, portfolio stream), static-IP deployment, L0 dry-run, runbook, backup/restore | L0 passes (doc 09) |
| **M9 Controlled live pilot** | Controlled live pilot | L1 micro-pilot; simulator calibration | Doc 09 L1 promotion criteria |
| **M10 Gradual scale** | Gradual scale | L2 → L3, more strategies one at a time | Doc 09 ladder |

## Calendar realism

- M1–M2: ~2–3 weeks of build effort.
- M3–M4: research duration depends on findings; ~3–6 weeks.
- M6 paper: **minimum 6 calendar weeks** (30 sessions) — cannot be compressed.
- M9 live micro-pilot: minimum 4 calendar weeks.
- Earliest realistic date for standard-size live trading: ~4–6 months from today, *if* research finds an edge.

## Dependencies on the operator

| Needed by | Item |
|-----------|------|
| M2 | Upstox API app (key/secret, redirect URI), daily login; a machine to run the recorder during market hours (the user's computer or a small VPS) |
| M8 | Static IP (VPS with static IP or ISP static IP) registered in Upstox My Apps |
| M8 | Phone alert channel (Telegram bot token or similar) |
| M7/M9 | Sign-off on promotion dossier; capital allocation for L1 |

## Working agreements
- Research code and live code share the library but research never writes live config.
- Every change to strategy logic bumps the strategy version and resets its paper window.
- `main` must always pass the full test suite, including fault tests from M5 onward.
