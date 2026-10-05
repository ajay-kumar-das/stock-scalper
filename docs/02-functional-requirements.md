# 02 — Functional Requirements

Each requirement is testable. `§n` refers to the section of the original business brief. Priority: **M** = must for paper, **L** = must before live, **S** = should.

## FR-MODE — Operating modes (§30)
| ID | Requirement | Pri |
|----|-------------|-----|
| FR-MODE-1 | The system runs in exactly one of BACKTEST, REPLAY, PAPER, LIVE per process. | M |
| FR-MODE-2 | LIVE requires all of: config `mode: LIVE`, env var `SCALPER_LIVE_CONFIRM` equal to today's date (YYYY-MM-DD), a valid broker token, a passing pre-flight check, and a non-zero `live.capital_allocation`. Any missing item → refuse to start (not fall back to another mode). | M |
| FR-MODE-3 | Only the LIVE execution adapter can reach broker order endpoints. Non-LIVE modes construct no live order client at all. | M |
| FR-MODE-4 | Mode is shown on every UI screen, every log line, every journal record and every report. LIVE uses a distinct red banner. | M |
| FR-MODE-5 | Strategy, risk, sizing and OMS code paths are identical across modes. | M |

## FR-DATA — Market data (§3, §27)
| ID | Requirement | Pri |
|----|-------------|-----|
| FR-DATA-1 | Ingest real-time LTP, 5-level depth, volume, ATP, total buy/sell quantity via Upstox market data feed v3 (`full` mode) for the watchlist; `ltpc` for the wider scan universe. | M |
| FR-DATA-2 | Build 1-second and 1-minute bars from ticks; record raw ticks and depth snapshots to disk (Parquet, partitioned by date/instrument). | M |
| FR-DATA-3 | Detect staleness per instrument (no update for > `stale_after_s`, default 5 s during market hours for watchlist names) and globally (feed silent > 3 s). Stale instruments cannot be entered; global staleness triggers DEGRADED mode. | M |
| FR-DATA-4 | Detect inconsistent data (crossed book, LTP outside band, non-monotonic cumulative volume, negative prices, timestamp regressions) and quarantine the instrument. | M |
| FR-DATA-5 | Load historical 1-min candles (Upstox v3) and NSE daily bhavcopies (point-in-time universe, incl. later-delisted symbols). | M |
| FR-DATA-6 | All derived features are computed only from data timestamped ≤ the decision time (enforced by the event clock). | M |

## FR-SEL — Stock selection (§3)
| ID | Requirement | Pri |
|----|-------------|-----|
| FR-SEL-1 | Build a daily *eligible universe* pre-market from point-in-time liquidity data (doc 06 hard filters). | M |
| FR-SEL-2 | Continuously (every 60 s, configurable) rank eligible stocks by opportunity score and maintain a watchlist of top N (default 25) in full-depth mode. | M |
| FR-SEL-3 | Exclude: CAS-eligible stocks, ASM/GSM-flagged stocks, stocks within 3% of a fixed circuit limit, stocks with spread above threshold, stocks with corporate action/result announcement in session (if data available). | M |
| FR-SEL-4 | Every inclusion/exclusion is explainable (scores and failing filter recorded). | M |

## FR-REG — Market regime (§4)
| ID | Requirement | Pri |
|----|-------------|-----|
| FR-REG-1 | Classify the market every minute on two axes — trend (strong up / weak up / flat / down) and volatility (low / normal / high / abnormal) — plus breadth and a breakout-vs-mean-reversion character measure (doc 07 §2). | M |
| FR-REG-2 | Each strategy declares the regimes in which it is permitted; the engine blocks entries outside them. | M |
| FR-REG-3 | "Abnormal" regime (index move > 1.5% in 15 min, India VIX jump > 15% intraday, market-wide circuit, feed anomalies) blocks all new entries. | M |

## FR-SIG — Signals and confidence (§5, §6)
| ID | Requirement | Pri |
|----|-------------|-----|
| FR-SIG-1 | Strategies are plug-ins implementing a fixed interface: declared hypothesis, permitted regimes, features consumed, `evaluate()` → candidate setup or none. | M |
| FR-SIG-2 | Each candidate carries: entry zone, invalidation (stop) level, target logic, expected holding time, evidence list with per-item values, and a confidence score in [0, 1]. | M |
| FR-SIG-3 | Confidence is calibrated: in validation, realised win rate / expectancy by confidence bucket must be monotonic (doc 07 §4). Uncalibrated scores are not used for sizing. | L |
| FR-SIG-4 | Candidates below `min_confidence` or failing the edge-vs-cost gate are rejected and journalled with reasons. | M |

## FR-ENT — Entry (§7)
| ID | Requirement | Pri |
|----|-------------|-----|
| FR-ENT-1 | Enter only if expected net edge ≥ `k_cost` × all-in round-trip cost (default k = 2.0) and reward:risk ≥ 1.5 after costs. | M |
| FR-ENT-2 | Reject if: price extended > `max_extension_atr` above the trigger level, spread > 1.5× its 20-minute median, resistance within 1R of entry, last 1-s move > 4× typical (spike filter). | M |
| FR-ENT-3 | Entry orders are limit orders with a maximum chase (price may be re-priced ≤ `max_chase_ticks`, default 2 ticks or 0.05%, whichever is smaller) and a time-to-live (default 3 s). Unfilled → cancel; setup re-evaluated, not chased. | M |

## FR-OMS — Order management (§8)
| ID | Requirement | Pri |
|----|-------------|-----|
| FR-OMS-1 | Every order has a lifecycle state machine: INTENDED → SUBMITTED → ACKNOWLEDGED → (PARTIALLY_FILLED) → FILLED / CANCELLED / REJECTED / EXPIRED, plus UNKNOWN for uncertain state (doc 03). | M |
| FR-OMS-2 | Every order has a unique client tag (`tag`, ≤ 40 chars) = idempotency key; resubmission after timeout first queries by tag. | M |
| FR-OMS-3 | At most one working entry order per instrument and at most one position per instrument; enforced in OMS, not strategy. | M |
| FR-OMS-4 | Partial fills: unfilled remainder of an entry is cancelled at TTL; protective stop sized to actual filled qty and resized on each fill. | M |
| FR-OMS-5 | Acknowledgement timeout (default 2 s) → state UNKNOWN → freeze new entries on that instrument and reconcile via order book + portfolio stream. | M |
| FR-OMS-6 | A global order-rate throttle (default ≤ 3 orders/s, ≤ 120/min) independent of the broker's limits. | M |
| FR-OMS-7 | Rejections are classified (margin, price band, RMS, rate limit, auth, unknown); repeated rejections (3 in 5 min) disable entries and alert. | M |

## FR-POS — Position management (§9–12)
| ID | Requirement | Pri |
|----|-------------|-----|
| FR-POS-1 | Immediately after the first fill, place a broker-resident SL (stop-limit) sell for the filled qty at the protective stop. | M |
| FR-POS-2 | All exits are executed by modifying the protective stop order (to a marketable limit), never by placing an additional sell order while the stop is live. | M |
| FR-POS-3 | Position manager re-evaluates thesis every bar/second: momentum, volume, structure, market direction, time-in-trade, distance to resistance (doc 07 §6). | M |
| FR-POS-4 | Stops only move in the direction that reduces risk. | M |
| FR-POS-5 | Time stop: exit if MFE < 0.5R by `t_expect` or holding > `t_max` (strategy-specific). | M |

## FR-RISK — Risk (§13–15)
| ID | Requirement | Pri |
|----|-------------|-----|
| FR-RISK-1 | Position size = floor(risk budget / per-share risk incl. slippage), capped by notional, liquidity (≤ 1% of 1-min avg volume and ≤ 20% of top-5 depth on the ask), and exposure limits (doc 04). | M |
| FR-RISK-2 | Account-level limits: per-trade risk, per-position capital, open positions, gross exposure, sector exposure, correlated exposure, daily loss, consecutive losses, drawdown. | M |
| FR-RISK-3 | Daily loss limit breach → HALTED_FOR_DAY: cancel working entries, manage open positions to exit, no new entries; resume only next session after a written acknowledgement. | M |
| FR-RISK-4 | No mechanism exists that increases size after losses. Size multipliers are ≤ 1.0 after losses and recover only with time/evidence. | M |

## FR-ADAPT — Self-monitoring (§16, §34)
| ID | Requirement | Pri |
|----|-------------|-----|
| FR-ADAPT-1 | Per strategy, track rolling expectancy, win rate, slippage, stop-out rate vs validated baseline; drift beyond control limits → throttle (½ size) then suspend. | L |
| FR-ADAPT-2 | Overtrading monitor: trades/hour, trades/stock, re-entries after stop-out, fee share of gross P&L; breach → raise thresholds / cooldown. | M |
| FR-ADAPT-3 | No live parameter optimisation. Parameters are versioned; changes require a new validated version. | M |

## FR-SESS — Session handling (§18, §19)
| ID | Requirement | Pri |
|----|-------------|-----|
| FR-SESS-1 | No entries before 09:20 by default (opening-range strategies may observe from 09:15); configurable per strategy with research justification. | M |
| FR-SESS-2 | Entry cutoff 14:45; begin flattening 15:00; hard flatten by 15:10 with marketable limits; verify flat by 15:15. | M |
| FR-SESS-3 | End-of-session reconciliation: broker positions = 0, open orders = 0; otherwise alert loudly and keep retrying flatten. | M |

## FR-SIM — Simulation (§20, §21)
| ID | Requirement | Pri |
|----|-------------|-----|
| FR-SIM-1 | Simulated execution models latency, spread crossing, queue position for passive orders, partial fills, missed fills, rejections and full costs (doc 08). | M |
| FR-SIM-2 | Bar-based backtests use conservative intrabar assumptions (stop before target when both touched; limit fills require trade-through). | M |
| FR-SIM-3 | Backtests are reproducible: same data + config + code version → identical results; run ID records all three. | M |

## FR-JRN — Journal and explainability (§24, §26)
| ID | Requirement | Pri |
|----|-------------|-----|
| FR-JRN-1 | Append-only event journal (SQLite WAL) of every decision: candidate, rejection, order event, fill, stop move, size adjustment, risk-state change, manual action. | M |
| FR-JRN-2 | Trade records include all fields in §24 plus pre-cost and post-cost P&L, MFE/MAE, regime and feature snapshot at entry. | M |
| FR-JRN-3 | A query interface answers: why bought / not bought / exited / stop moved / size reduced / stopped trading / which strategy. | M |

## FR-MON — Monitoring and control (§25, §29)
| ID | Requirement | Pri |
|----|-------------|-----|
| FR-MON-1 | Local web console showing everything listed in §25 with a single overall health indicator (GREEN / AMBER / RED). | M |
| FR-MON-2 | Controls: pause, resume, disable new entries, exit one, exit all, cancel pending, disable strategy, change selected risk params (only to more conservative values without restart), emergency shutdown. | M |
| FR-MON-3 | Manual commands are journalled and take precedence over strategy decisions immediately. | M |
| FR-MON-4 | Alerts (desktop + phone push/Telegram) for RED health, risk halts, reconciliation breaks, residual positions. | L |

## FR-REC — Recovery (§27, §28)
| ID | Requirement | Pri |
|----|-------------|-----|
| FR-REC-1 | On start or restart: state = RECOVERING; fetch positions, order book, trades from broker; rebuild internal state from broker; adopt orphan positions under a conservative default management policy (immediate protective stop + exit plan). | M |
| FR-REC-2 | No new entries until reconciliation passes and data feed is healthy for ≥ 30 s. | M |
| FR-REC-3 | Periodic reconciliation every 30 s during session and on every UNKNOWN order event. | M |

## FR-RPT — Analysis (§23, §33)
| ID | Requirement | Pri |
|----|-------------|-----|
| FR-RPT-1 | All metrics in §23, per strategy / stock / time-of-day bucket / regime, before and after costs. | M |
| FR-RPT-2 | Automatic post-session report covering every item in §33, with flagged anomalies. | M |
| FR-RPT-3 | Research proposals produced by the report go to a backlog file; they never change live config. | M |
