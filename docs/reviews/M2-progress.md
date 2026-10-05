# M2 Progress — Data & Replay (in progress)

Date: 2026-10-05 · Build: 95 tests passing (~100 s incl. replay and parallel-window tests)

## Done (tested offline with fixtures)
| Item | Module | Test |
|------|--------|------|
| Upstox v3 historical candle client (month windows, retries, paise conversion) | `scalper/data/upstox_history.py` | `test_parse_candles_*`, `test_month_windows_*` |
| Parquet bar store + loader for the backtester | `scalper/data/store.py` | `test_bar_store_round_trip` |
| NSE bhavcopy parser (legacy + UDiFF) → point-in-time universe (no survivorship, no look-ahead) | `scalper/data/bhavcopy.py` | `test_point_in_time_universe_*`, `test_udiff_format_parses` |
| Normalised Tick, validators (crossed book, volume regression, timestamp regression, jumps), staleness monitor | `scalper/data/ticks.py` | `test_validator_*`, `test_staleness_monitor` |
| Tick → 1-min bar builder (no invented bars) + bar-agreement check (finding 3) | `scalper/data/ticks.py` | `test_bar_builder_and_agreement` |
| Tick/depth recorder (Parquet) | `scalper/data/ticks.py` | `test_tick_recorder_round_trip` |
| Feed v3 normaliser (full mode, index feed, market_info) + SDK wrapper (market data only) | `scalper/data/upstox_feed.py` | `test_feed_normaliser_full_mode_and_market_info` |
| Instrument master + daily token loader | `scalper/data/instruments.py` | — (needs network) |
| Operator scripts: `upstox_login.py`, `download_history.py`, `record_feed.py`, `run_research.py` | `scripts/` | syntax-checked only |
| Engine speed-up (incremental efficiency ratio, precomputed RVOL profile, no `statistics.pvariance`) | features | suite runtime 30 s → 20 s |

## Added in the second M2 session
| Item | Module | Test |
|------|--------|------|
| Tick-level fill simulator: lognormal latency, book at arrival, depth walk with liquidity ledger, queue position, trade-through at own limit, shared per-tick volume, cancel/fill races, time-driven acks, no-ack faults, broker `query()` | `scalper/execution/sim_tick.py` | `test_tick_sim_and_replay.py`, `test_review2_regressions.py` |
| REPLAY runner: same Engine driven by recorded ticks; minute-bar dispatch; data quarantine; feed-gap DEFENSIVE | `scalper/backtest/replay.py`, `Engine.housekeeping` | `test_replay_runs_same_engine_on_ticks_without_residuals` |
| UNKNOWN-order handling: ack timeout → UNKNOWN → spaced reconciliation → possibly-filled block | `scalper/oms/oms.py` | `test_missing_ack_becomes_unknown_then_reconciles`, regression tests |
| NSE trading calendar (2026 holidays, special sessions = no-trade) and corporate-event exclusions | `scalper/session/calendar.py` | `test_calendar_and_event_exclusions`, `test_session_respects_calendar_policy` |
| Walk-forward monthly windows with 25-session warm-up, parallel processes | `scalper/backtest/runner.py` (`month_windows`, `run_windows`) | `test_windows.py` (matches continuous run; parallel == sequential) |
| Second adversarial review: 7 defects proved and fixed (doc 13 findings 35-41) | — | `test_review2_regressions.py` |

## Not yet verified (needs Upstox access from the operator's machine)
- Live response shapes: candle JSON, instrument-master `tick_size` units, and the decoded feed dict from the SDK. The normaliser and parser follow the documented shapes but are **unverified against real responses**. The first real run must confirm them.
- The bar-agreement exit criterion (≥ 99%) needs at least 5 recorded sessions.

## Remaining M2 work
Only items that need real Upstox access remain: verify response shapes; record ≥ 5 sessions; pass the bar-agreement check; calibrate the tick simulator's latency and queue model against the shadow-market check (doc 08 §3).

## M2 gate status
Code complete for M2. The **gate cannot be passed until real data has been recorded and checked**; that depends on the operator's Upstox app and a machine that can reach Upstox.
