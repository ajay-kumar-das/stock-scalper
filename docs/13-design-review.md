# 13 — Critical Design Review (M0 gate)

Reviewed docs 01–12 from eight perspectives. Each finding is classified **Critical** (would produce false evidence or unsafe behaviour), **Major** (material weakness), **Minor**. Status: **Fixed** (doc updated), **Tracked** (milestone owner named), **Accepted** (risk consciously accepted).

## 1. Findings

| # | Perspective | Finding | Sev | Resolution | Status |
|---|------------|---------|-----|-----------|--------|
| 1 | Quant researcher | **Paper sample is statistically under-powered.** With per-trade σ ≈ 1.2R, 100 trades give SE ≈ 0.12R; a true edge of 0.10R would show t ≈ 0.8. Requiring "lower bound > 0 on paper alone" would reject real but modest edges, while a lucky streak could pass a looser test. | Critical | Doc 08 revised: paper's job is *consistency* (not worse than validated backtest), while *significance* is established on holdout + paper combined; required sample computed from the strategy's own σ and expectancy (n ≥ (2.5σ/E)²); minimum detectable effect is printed in every report. | Fixed |
| 2 | Quant researcher | Backtests have no historical spread data; a fixed spread assumption could flatter results for mid-liquidity names. | Critical | Bar backtests use a conservative spread by liquidity bucket (default 2 ticks or 4 bps, whichever larger, for top bucket; 6 bps next), stressed ×2 in R6. Replaced by calibrated per-stock values from recorded depth once ≥ 20 sessions exist (M3). | Fixed (doc 08 §1 note) |
| 3 | Quant researcher | Our live bars (built from feed snapshots) may differ from Upstox historical candles used in backtests — a hidden backtest/live gap. | Major | M2 exit criterion added: built bars vs Upstox candles agree (OHLC within 1 tick on ≥ 99% of bars, volume within 2%). | Fixed (doc 11) |
| 4 | Quant researcher | Regime permission matrices and selection weights derived from data are extra degrees of freedom. | Major | Counted as trials in the experiment registry; permission changes create a new strategy version requiring validation (already in doc 05 R6.3); selection weights fixed until a dedicated R1 study. | Fixed |
| 5 | Execution specialist | Upstox feed `full` mode is snapshot-based, not every trade; queue-position modelling using LTP/volume deltas is approximate. | Major | Fill model uses conservative queue assumptions (we are last in queue; fill only after volume at-or-through price ≥ queue ahead + our qty). Shadow-market check (doc 08 §3) quantifies error. v1 strategies use marketable-limit entries, so queue accuracy matters less. | Accepted |
| 6 | Execution specialist | Exit-by-modify assumes Upstox permits changing an `SL` order to `LIMIT` via modify. Not yet verified against the live API. | Critical | L0 dry-run must verify. Fallback protocol defined: cancel SL → wait for *confirmed* cancel → place LIMIT sell; during the gap the position is unprotected for ≤ 1 s; if cancel confirmation fails → treat as UNKNOWN and reconcile before placing any sell. | Fixed (doc 03) / verify in M8 |
| 7 | Execution specialist | A protective SL whose trigger is already above LTP (fast drop between fill and stop placement) is rejected by the exchange. | Major | If LTP ≤ intended trigger at placement time → exit immediately with marketable limit instead of placing SL. | Fixed (doc 03) |
| 8 | Execution specialist | Order-update stream can deliver fills *before* the REST placement response returns (race). | Major | OMS maps updates by client tag as well as order_id; unknown-order updates are buffered 5 s and matched; unmatched → reconcile. | Fixed (doc 03) |
| 9 | Execution specialist | Brokerage is per executed *order*; cancel/replace re-pricing would add orders and cost. | Minor | Re-pricing uses modify, not cancel/replace; cost model charges per order regardless of number of fills. | Fixed |
| 10 | Risk manager | Margin was not modelled: MIS margin varies by stock; a margin reject at entry is harmless, but at *exit* irrelevant; the real risk is the stop order being rejected for margin reasons in edge cases. | Major | Pre-trade margin check via Upstox margin API in LIVE; simulator applies per-stock MIS margin (default 20% notional) and rejects when exceeded. | Fixed (doc 04) |
| 11 | Risk manager | Behaviour on hard daily-loss breach ambiguous ("close or manage"). | Major | Decision: exit all open positions via the patient→aggressive ladder (30 s at bid, then marketable). Capital preservation outranks possible recovery. | Fixed (doc 04) |
| 12 | Risk manager | Per-strategy consecutive-loss limit had no action. | Minor | 3 consecutive losses → strategy cooldown 60 min and m_perf 0.5 for the rest of session. | Fixed (doc 04) |
| 13 | Risk manager | Brief §29 asks to "change selected risk parameters"; doc 04 allows tightening only intraday. | Minor | Deliberate: loosening intraday is how discipline erodes. Loosening requires restart + journalled config change, outside market hours by default. | Accepted |
| 14 | Professional trader | Exchange holidays, half-days and special sessions (Muhurat, Saturday budget sessions) not handled. | Major | Session calendar from NSE holiday list; special sessions are **no-trade** by default. | Fixed (doc 03) |
| 15 | Professional trader | Intraday results/board-meeting announcements cause discontinuous moves that stops cannot protect. | Major | M2 adds NSE corporate-announcement calendar; stocks with board meetings for results today are excluded until 30 min after the announcement time. Until available: accepted risk, mitigated by per-trade risk and SL-limit escalation. | Tracked (M2) |
| 16 | Product manager | No explicit traceability from brief sections to design. | Minor | Matrix in §2 below. | Fixed |
| 17 | Product manager | Low capital makes the system trade rarely due to the cost floor; user may perceive it as broken. | Minor | Console shows "rejections by gate" counts; daily report states the cost-floor effect; doc 04 states the capital implication. | Fixed |
| 18 | Software architect | Journalling every candidate every second will bloat the database. | Minor | Journal candidates at TRIGGERED and gate outcomes; SETUP_FORMING sampled once per setup id; rejections deduplicated per (instrument, strategy, reason) per minute with counts. | Fixed (doc 07) |
| 19 | Production engineer | Secrets handling not specified. | Major | API secret and tokens in OS keyring / env; never logged; log redaction filter; journal never stores tokens; console bound to 127.0.0.1. | Fixed (doc 12) |
| 20 | Production engineer | Host failure (VPS dies) — broker SL protects positions, but nobody is alerted if the process can't alert. | Major | External dead-man's switch: the engine pings a heartbeat every 30 s to an external monitor (e.g. healthchecks.io / Telegram watchdog); missing heartbeats during market hours alert the operator's phone. | Tracked (M8) |
| 21 | Adversarial reviewer | "Random-entry benchmark" could be gamed if random entries are not drawn from the same time-of-day and regime distribution. | Minor | Random entries are stratified by time bucket, regime label and stock. | Fixed (doc 05) |
| 22 | Adversarial reviewer | The edge gate uses `p` from calibration fit on dev data; in validation, a poorly calibrated `p` could still let marginal trades through. | Major | Validation reports realised vs predicted p per bucket (reliability diagram); calibration error > 0.08 → calibration disabled, conservative fixed p used. | Fixed (doc 07) |
| 23 | Adversarial reviewer | Short-selling later: invariant "working sell qty ≤ position" is long-specific. | Minor | Generalised invariant: working *closing* qty ≤ |position|, opening orders only when flat. | Fixed (doc 12) |
| 25 | Quant researcher (found during M1 build) | The edge gate threshold of 0.25R was unattainable: a 42% win rate with a 2R target yields only 0.26R *gross*; with ~0.15–0.25R of costs nearly every setup failed. Worse, enforcing a guessed win-rate prior during research would make research circular (the gate would decide which trades exist to measure). | Critical | Threshold revised to 0.10R net. Research runs measure — not enforce — the edge and regime gates and journal "would-fail" outcomes; paper/live enforce them with p taken from validated results. | Fixed (doc 07, `research_config`) |
| 26 | Production engineer (found during M1 build) | Pure-Python bar engine processes ~30k bars/s; a 150-stock × 4.5-year run (~60M bars) would take ~35 min, slowing walk-forward research. | Major | M2: vectorised pre-screen of candidate days (RVOL/breakout) so the event engine only replays days that can produce signals; parallelise by month for walk-forward windows. Correctness tests stay on the event engine. | Tracked (M2) |
| 27 | Professional trader (found reviewing M1 explain output) | ORB thesis exit fired on a single close one tick back inside the range — the same "stop inside noise" error that doc 07 §7 forbids for stops; the strategy also lacked the doc 07 §6 exhaustion checks. | Major | Thesis invalidation now needs two closes inside the range by more than half the median 1-min range (or close < VWAP − 0.3 ATR); exhaustion checks (> 3 daily ATR from open; 5-bar climax) added. Changed on rationale **before** any real-data research, so it is not a data-mined fix. | Fixed |
| 28 | Adversarial review of M1 code | A stop rejected/cancelled *asynchronously* by the broker went unnoticed; the position stayed unprotected indefinitely (invariant 3 broken). | Critical | Closing-order terminal events now trigger re-protection (≤ 3 attempts) or an aggressive exit; per-bar invariant check re-places/resizes stops. | Fixed + regression test |
| 29 | Adversarial review | A position could close while its entry remainder was still working; a late fill then raised inside the broker callback and left untracked shares (F29). | Critical | Close only when flat **and** no entry can still fill; stop fill cancels the entry remainder; late fills create/revive a tracked recovery position that is exited immediately; listener exceptions are caught and force DEFENSIVE. | Fixed + regression tests |
| 30 | Adversarial review | Protective actions shared the entry throttle and were silently dropped (stop resize after rapid partial fills; exits in a busy bar). | Critical | Protective actions get priority headroom up to 8/s (below the SEBI 10/s line); throttled protective modifies raise anomalies and are retried by the per-bar invariant check. | Fixed + regression tests |
| 31 | Adversarial review | Realised loss on a partially closed position was invisible to the daily-loss limit. | Critical | Risk marking includes realised P&L of open positions, all costs incurred and estimated exit costs. | Fixed + regression test |
| 32 | Adversarial review | `end_day` cancelled stops but left the residual open; P&L carried silently into the next day. | Major | Simulation models the broker auto square-off (last price − half-spread, ₹88.50 fee, trade recorded, severity-1 anomaly); LIVE keeps the stop working and alerts. | Fixed + regression test |
| 33 | Adversarial review | Unfilled entries survived exit-all / flatten; entry TTL existed only in the simulator; pending-entry remainder missing from open risk. | Major | `request_exit` cancels entries first; flatten/halt cancels unfilled entries; OMS enforces entry TTL in all modes; remainder risk counted. | Fixed + regression test |
| 34 | Adversarial review | Modifies updated local state before broker confirmation with no reject path; UNKNOWN stop silently blocked exits; sim-rejected entries consumed trade quotas. | Minor | Pending-modify with revert on `MODIFY_REJECTED`; UNKNOWN stop during exit raises anomaly → DEFENSIVE; only accepted entries count. | Fixed + regression test |
| 35 | Adversarial review of M2 tick simulator | Displayed depth was re-walked on every tick, so an order could fill 10× the visible size with zero traded volume. | Critical | Liquidity ledger: depth taken by us is subtracted until the level disappears; resting fills come only from new traded volume. | Fixed + regression test |
| 36 | Adversarial review | Resting orders (incl. stop-limit exits) were filled at better-than-limit book prices when the market moved through them. | High | Resting orders fill at their own limit; book-walk pricing only when we are the aggressor on arrival. | Fixed + regression test |
| 37 | Adversarial review | A genuine > 10% move quarantined the instrument for the rest of the day silently (validator compared with the last *good* tick), and EOD square-off priced off the stale close. | High | Jumps measured against the last received tick (one tick flagged, new level accepted); quarantine raises an anomaly and blocks entries for 60 s; square-off uses the last valid LTP. | Fixed + regression test |
| 38 | Adversarial review | Exchange-time lag at minute boundaries created duplicate, out-of-order stale bars every minute. | High | Bar builder never reopens a closed minute; late-tick volume carries into the next bar. | Fixed + regression test |
| 39 | Adversarial review | UNKNOWN orders were assumed cancelled after three queries on consecutive ticks (milliseconds), unblocking a duplicate entry. | High | Reconciliation attempts spaced 1 s / 2 s / 4 s; cancel-by-tag before assuming; instrument enters `possibly_filled` and stays blocked until position reconciliation. | Fixed + regression test |
| 40 | Adversarial review | One tick's traded volume was consumed in full by every resting order; queue position decreased by volume printed at other prices. | Medium | Per-tick volume budget shared in time priority; queue moves only by the observed decrease of displayed size at our price, bounded by current displayed size. | Fixed + regression tests |
| 41 | Found while fixing 35-40 | Acks/cancels were driven by ticks in the same instrument, so quiet instruments produced false UNKNOWN states; data-quarantine anomalies tripped system-wide DEFENSIVE. | Medium | Time-driven `on_time()` for arrivals/acks/cancels; data problems are instrument-level (quarantine), not system-level. | Fixed |
| 42 | Known residual optimism | Fill notifications reach the engine with zero latency in the tick simulator (stops placed after an entry fill leave slightly early). | Minor | Accepted for now; to be measured against live micro-pilot fills (doc 08 §4). | Accepted |
| 24 | Adversarial reviewer | Point-in-time F&O eligibility lists may be hard to source for 2022–2024. | Major | Primary universe rule falls back to bhavcopy liquidity/price filters (point-in-time by construction); F&O membership used when the archive is available; the difference is measured in M3. | Tracked (M2/M3) |

## 2. Traceability matrix (brief § → design)

| Brief § | Where satisfied |
|---------|-----------------|
| 1 Objective | 01 §4, §7 |
| 2 Scope | 01 §5 |
| 3 Stock selection | 06; FR-SEL |
| 4 Market condition | 07 §2; FR-REG |
| 5 Opportunity identification | 05 §7, 07 §4; FR-SIG |
| 6 Confidence | 07 §4; FR-SIG-2/3 |
| 7 Entry | 07 §5–6; FR-ENT |
| 8 Execution | 03 §3–4; FR-OMS |
| 9 Position management | 07 §8; FR-POS |
| 10 Stop loss | 07 §7; 03 §4 |
| 11 Profit protection | 07 §8 rules 5–8, 11 |
| 12 Time exit | 07 §8 rules 9–10 |
| 13 Sizing | 04 §3 |
| 14 Portfolio risk | 04 §2 |
| 15 Daily loss | 04 §2, §4; FR-RISK-3 |
| 16 Adaptive | 05 §5; FR-ADAPT |
| 17 Multiple strategies | 12 (plug-ins); 05 per-strategy gates; 09 one-at-a-time |
| 18 Market open | 07 §9 |
| 19 End of day | 03 §5; 07 §10; FR-SESS |
| 20 Paper | 08 |
| 21 Historical validation | 05 §2–4 |
| 22 Walk-forward | 05 §1 R3, §2 |
| 23 Metrics | FR-RPT-1; `scalper/analytics/metrics.py` |
| 24 Journal | FR-JRN |
| 25 Monitoring | FR-MON; 12 ADR-8 |
| 26 Explainability | 07 §11; FR-JRN-3 |
| 27 Failure safety | 10 |
| 28 Recovery | FR-REC; 10 F13 |
| 29 Manual control | FR-MON-2/3 |
| 30 Modes | FR-MODE |
| 31 Promotion | 09 |
| 32 Research process | 05 |
| 33 Continuous analysis | FR-RPT-2 |
| 34 Anti-overtrading | FR-ADAPT-2; 04 trade caps |
| 35 Cost awareness | 07 §5; costs module; all metrics pre/post cost |
| 36 No guarantees | 01 §4 (non-goal metrics) |
| 37 Adversarial review | 10; this document |
| 38 Staged development | 11 |

## 3. Gate decision

No unresolved Critical findings remain at design level (finding 6 has a defined fallback and a verification step in M8). **Decision: proceed to M1 (foundation).**
