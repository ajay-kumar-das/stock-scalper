# 10 — Major Failure Scenarios

Governing rule: **uncertainty about money, positions or orders ⇒ no new exposure.** Each scenario has an automated fault-injection test (`tests/faults/…`) run in REPLAY before every release.

| # | Scenario | Detection | Defensive response | Test |
|---|----------|-----------|--------------------|------|
| F1 | Broker API unavailable (5xx/timeouts) | consecutive failures, health ping | DEFENSIVE; broker-resident stops still protect; retry with backoff; if positions cannot be managed for 60 s → alert RED | F1_broker_down |
| F2 | Market data feed disconnects | no message > 3 s; WS close | DEFENSIVE; reconnect + resubscribe; positions rely on broker SL; resume after 30 s healthy + recon | F2_feed_drop |
| F3 | Data freezes but connection alive (stale) | per-instrument last-update age; LTP unchanged while index moves | instrument quarantined; global → DEFENSIVE; position in a stale name: tighten nothing (can't see), rely on broker SL, alert | F3_stale |
| F4 | Inconsistent data (crossed book, price outside band, volume decreases) | validators | quarantine instrument; journal anomaly | F4_bad_ticks |
| F5 | Order ack never arrives | 2 s timeout | UNKNOWN; reconcile by tag; instrument frozen; treat as possibly filled for risk | F5_no_ack |
| F6 | Partial fill of entry then price runs away | fill events | SL resized to filled qty; remainder cancelled at TTL; position managed at actual size (small positions below cost floor are exited at next sensible opportunity if expected net < 0) | F6_partial |
| F7 | Partial fill of exit | fill events | keep modifying remaining qty more aggressively; escalate after 15 s | F7_partial_exit |
| F8 | Duplicate order due to retry | tag-based idempotency; OMS one-working-order invariant | never resend without querying by tag first | F8_dup_retry |
| F9 | Protective stop placement fails | reject/timeout | retry once; then immediate exit with marketable limit; alert | F9_stop_fail |
| F10 | SL-limit skipped by a gap/flash move | LTP < SL limit, SL still open | modify to aggressive marketable limit; repeat; alert | F10_gap_through_stop |
| F11 | Exit and stop both fill → accidental short | invariant: working sell qty ≤ position qty; exit-by-modify protocol | structurally prevented; if a negative position is ever observed: EMERGENCY buy-to-cover and halt | F11_double_sell |
| F12 | Broker position ≠ local position | 30-s reconciliation | DEFENSIVE; adopt broker truth; orphan → protective stop + exit plan; alert | F12_recon_mismatch |
| F13 | Process crash / restart mid-trade | startup always enters RECOVERING | rebuild from broker; never trust local state; adopt positions | F13_restart |
| F14 | Internet loss on host | both feed & API fail | DEFENSIVE; broker SLs protect; on reconnect → RECOVERING | F14_net_loss |
| F15 | Auth token expires / revoked mid-day | 401 | DEFENSIVE; alert operator to re-login; no trading until new token + recon | F15_auth |
| F16 | Repeated order rejections (margin, RMS, band) | reject classifier | 3 in 5 min → DEFENSIVE; margin rejects → reduce max exposure for the day | F16_rejects |
| F17 | Rate limit hit (HTTP 429) | response codes | internal throttle tightened; exits prioritised over entries in the queue | F17_rate_limit |
| F18 | Flash crash in a held stock / market-wide | price vs bands, index shock | ABNORMAL regime: no entries; positions protected by SL; escalate exits | F18_flash |
| F19 | Market-wide circuit breaker halt | market_info status from feed | DEFENSIVE; orders queued are cancelled; resume on re-open only after recon + 5 min | F19_mwcb |
| F20 | Stock hits upper/lower band (frozen) | quote shows one-sided book | no entries; if long and lower-band hit: keep sell order at band, alert | F20_band_lock |
| F21 | Clock drift | NTP check each 5 min | > 1 s → DEFENSIVE | F21_clock |
| F22 | Config mistake activates LIVE | mode guard (FR-MODE-2) | refuse start | F22_mode_guard |
| F23 | Fat-finger sizing bug | pre-trade sanity: notional ≤ max_position_notional, qty ≤ liquidity cap, price within 1% of LTP | reject order + DEFENSIVE + alert | F23_fat_finger |
| F24 | Five consecutive losses | risk engine | cooldown at 4; halt at 6; m_perf down-steps | F24_loss_streak |
| F25 | Previously profitable setup stops working | CUSUM drift | throttle → suspend strategy | F25_drift |
| F26 | Volatility suddenly doubles | regime vol axis | HIGH: strategies not permitted stop; stops widen via ATR → size shrinks automatically; ABNORMAL: all stop | F26_vol_double |
| F27 | Spread blows out | spread veto | entries blocked; exits become more patient-then-aggressive per escalation ladder | F27_spread |
| F28 | Disk full / journal write fails | write errors | DEFENSIVE (can't record decisions = can't explain them) | F28_disk |
| F29 | Late fill update after a cancel | cumulative-qty logic | position updated; SL resized; recon | F29_late_fill |
| F30 | Broker auto square-off at 15:25 (we failed to flatten) | position at 15:15 | RED alert at 15:15; escalating exit; broker backstop at 15:25 | F30_eod_residual |
| F31 | Static IP change / order from unregistered IP | order rejects with IP error | DEFENSIVE; alert (ops issue) | F31_static_ip |
| F32 | Exchange/broker sends unexpected order status string | parser | map to UNKNOWN; reconcile; log | F32_unknown_status |

## Adversarial questions from the brief, answered

| Question | Answer in the design |
|----------|---------------------|
| What assumption could make a strategy look profitable when it isn't? | Fill realism (passive fills on touch), same-bar decide/fill, intrabar ordering, costs, survivorship, snooping. Each has a control in doc 05 §3 and doc 08 §1; the random-entry benchmark and holdout catch residual issues. |
| Volatility doubles? | F26 — ATR-based stops widen → risk-based size shrinks; regime permissions may block; ABNORMAL stops all. |
| Spread increases? | F27 — spread veto blocks entries; the cost gate uses live spread so marginal setups fail automatically. |
| Market reverses immediately? | Stop at structural/noise-floor level limits loss to ~1R + slippage; market-shock rule tightens stops on index breakdown. |
| Partial fill? | F6/F7 — SL sized to actual fill; remainder cancelled; small residual managed at cost-aware rules. |
| Data freezes? | F3 — quarantine; DEFENSIVE; broker SL protects. |
| Broker and local disagree? | F12 — broker wins; DEFENSIVE; orphan adoption. |
| Five consecutive losses? | F24 — cooldown and halt rules; size steps down, never up. |
| Flash move? | F10/F18 — SL-limit escalation; ABNORMAL regime. |
| Profitable setup stops working? | F25 — CUSUM/binomial drift monitors throttle then suspend; changes go back through research. |
