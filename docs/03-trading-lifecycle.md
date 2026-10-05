# 03 — Trading Lifecycle

Four nested state machines. All transitions are journalled with timestamp, cause and actor (strategy / risk / OMS / broker / operator).

## 1. Session state machine (system-wide)

```
          ┌──────────┐ login+preflight ok ┌────────────┐ reconcile ok + feed healthy 30s ┌────────┐
 start ──▶│ STARTING │───────────────────▶│ RECOVERING │────────────────────────────────▶│ ARMED  │ (pre-09:15)
          └──────────┘                    └────────────┘                                 └───┬────┘
               │ preflight fail                 │ mismatch unresolved                       │ 09:15
               ▼                                ▼                                           ▼
          ┌──────────┐                    ┌────────────┐                               ┌─────────┐
          │ REFUSED  │                    │  DEFENSIVE │◀──── uncertainty anywhere ────│ TRADING │
          └──────────┘                    └────────────┘                               └────┬────┘
                                               │ cause cleared + reconcile ok               │
                                               └──────────────▶ TRADING                     │
   TRADING ──operator pause──▶ PAUSED (no entries, positions managed) ──resume──▶ TRADING   │
   TRADING ──risk limit──▶ HALTED_FOR_DAY (no entries, positions exited) ── next session only
   TRADING ──14:45──▶ NO_NEW_ENTRIES ──15:00──▶ FLATTENING ──flat verified──▶ CLOSED ──report──▶ DONE
   any ──emergency──▶ EMERGENCY (cancel all, exit all, stop) 
```

| State | New entries | Manage positions | Notes |
|-------|-------------|------------------|-------|
| STARTING | ✗ | ✗ | Load config, verify mode guard, token, static-IP egress, instrument master |
| RECOVERING | ✗ | protective only | Broker state is fetched and adopted (FR-REC-1) |
| ARMED | ✗ | ✓ | Pre-open: build universe, warm features from history |
| TRADING | ✓ (subject to gates) | ✓ | Normal |
| PAUSED | ✗ | ✓ | Operator action |
| DEFENSIVE | ✗ | protective only (stops stay; exits allowed; no modifications that add risk) | Data stale, order UNKNOWN, recon mismatch, broker errors |
| HALTED_FOR_DAY | ✗ | exit all safely | Daily loss / drawdown / consecutive-loss / repeated-failure triggers |
| NO_NEW_ENTRIES | ✗ | ✓ | After entry cutoff |
| FLATTENING | ✗ | exit all, escalating aggressiveness | 15:00–15:10 |
| EMERGENCY | ✗ | cancel all + exit all at marketable prices | Operator or catastrophic fault |
| CLOSED/DONE | ✗ | ✗ | Recon + report |

**DEFENSIVE vs HALTED:** DEFENSIVE is recoverable intraday once the cause clears and a fresh reconciliation passes; HALTED_FOR_DAY is not.

## 2. Opportunity lifecycle (per instrument × strategy)

```
WATCHING → SETUP_FORMING → ARMED_SETUP → TRIGGERED → (ENTRY_PENDING) → IN_POSITION → EXITING → DONE
                 │               │            │
                 └─ invalidated ─┴─ rejected ─┘ (reason journalled)  → COOLDOWN (re-entry blocked)
```

- **SETUP_FORMING**: structural conditions present (e.g. opening range defined, stock in play).
- **ARMED_SETUP**: entry zone, stop, target, and confidence computed; waiting for trigger.
- **TRIGGERED**: trigger fired → passes through gates in fixed order: *session → regime → instrument health → setup quality (confidence) → edge-vs-cost → risk/sizing → OMS duplicate check*. First failing gate is journalled as the rejection reason; later gates are also evaluated for research ("would also have failed X").
- **COOLDOWN**: after a stop-out, the same instrument cannot be re-entered by the same strategy for `cooldown_min` (default 15 min) and needs a *new* setup (not the same breakout level).

## 3. Order state machine

| From | Event | To |
|------|-------|----|
| INTENDED | sent to broker | SUBMITTED |
| INTENDED | blocked by throttle/risk | CANCELLED (never sent) |
| SUBMITTED | broker ack (order_id) | ACKNOWLEDGED |
| SUBMITTED | broker reject | REJECTED |
| SUBMITTED | no ack within 2 s | UNKNOWN |
| ACKNOWLEDGED | partial fill | PARTIALLY_FILLED |
| ACKNOWLEDGED / PARTIALLY_FILLED | fill completes qty | FILLED |
| ACKNOWLEDGED / PARTIALLY_FILLED | cancel confirmed | CANCELLED (filled qty retained) |
| ACKNOWLEDGED | exchange reject / RMS reject | REJECTED |
| ACKNOWLEDGED / PARTIALLY_FILLED | modify sent | same state with `pending_modify` flag; modify ack/reject clears it |
| ACKNOWLEDGED | validity end (IOC) | EXPIRED |
| UNKNOWN | reconciliation finds order by tag | → its true state |
| UNKNOWN | reconciliation finds nothing after 3 attempts / 10 s | CANCELLED-ASSUMED **and** treated as "may have filled" for risk until next position reconciliation confirms |
| any terminal | — | no transitions (late events are logged as anomalies and trigger reconciliation) |

Rules:
- Fills are applied by *cumulative filled quantity* from the broker, not by summing deltas (idempotent against duplicate/out-of-order updates).
- `filled_qty` never decreases; an update showing a decrease is an anomaly → DEFENSIVE.
- Every order references its parent intent (entry, protective stop, exit) and position ID.
- Order intents carry a deterministic client tag: `sc-<date>-<strategy>-<seq>-<role>` (≤ 40 chars).

## 4. Position lifecycle

```
OPENING (entry working, qty may be 0..N)
  └─ first fill ──▶ place protective SL for filled qty ──▶ PROTECTED
PROTECTED ─ additional entry fills ─▶ modify SL qty
PROTECTED ─ thesis/target/trail/time exit ─▶ EXITING (modify SL → marketable LIMIT)
EXITING ─ fill complete ─▶ CLOSED ─ journal trade record
EXITING ─ not filled within 2 s ─▶ re-price up to 3 times by ≥ 1 tick toward bid, then escalate to aggressive marketable limit (bid − 0.3%) ─▶ alert if still open after 15 s
PROTECTED ─ broker SL triggers ─▶ EXITING (watch SL-limit not filling through gap → escalate)
```

### Protective stop details
- Order type `SL` (stop-limit) with trigger = stop price, limit = trigger − `stop_limit_buffer` (default max(0.3%, 3 ticks)). A pure SL-M would be preferred for guaranteed exit, but SL-M now carries mandatory market protection anyway; the stop-limit with a wide buffer behaves similarly while bounding worst-case fill. If price gaps through the limit, the position manager escalates with a marketable limit.
- If placing the protective stop fails twice → exit the position immediately with a marketable limit; a position must never remain unprotected for more than ~3 s.
- If LTP ≤ the intended trigger at placement time (price already through the stop), do **not** place the SL (the exchange would reject it); exit immediately with a marketable limit.
- **Fallback if the broker refuses SL→LIMIT modification** (to be verified in L0): cancel SL → wait for *confirmed* cancellation → place LIMIT sell. The unprotected window is ≤ ~1 s. If cancel confirmation does not arrive within 2 s, the SL is treated as UNKNOWN and reconciled before any sell is placed.
- **Update-before-ack race:** order updates from the portfolio stream can arrive before the REST placement response. The OMS matches updates by client tag as well as order_id; unmatched updates are buffered for 5 s, then trigger reconciliation.
- **Exit-by-modify protocol:** to exit, modify the existing SL order: `order_type=LIMIT`, `price = best_bid − exit_buffer`. Only if modify is rejected because the SL has already triggered/filled do we re-read the order; never place a new sell while the SL order is non-terminal. This guarantees total working sell quantity ≤ position quantity, so the bot cannot accidentally go short.
- Partial exits (scale-out): modify SL qty down to the remaining qty *after* the partial-exit order has filled; the partial exit is itself done as a separate LIMIT sell only for ≤ (position − SL qty). In v1, partial exits are disabled until the invariant tests in M2 pass.

## 5. Daily timeline (IST)

Trading days come from the NSE holiday calendar. Special sessions (Muhurat trading, Saturday budget sessions, any shortened session) are **no-trade** by default; the recorder still runs.

| Time | Action |
|------|--------|
| 08:30 | Operator login → token; start process; pre-flight (token, static IP egress test, instrument master, clock sync < 250 ms drift via NTP) |
| 08:45 | Universe built from previous-day bhavcopy + history; features warmed; recon (expect flat) |
| 09:00–09:15 | Pre-open: record indicative prices, gap computation |
| 09:15 | Feed live; observe only (opening auction effects, price discovery) |
| 09:20 | Earliest entry for non-ORB strategies (default; research may change) |
| every 30 s | Reconciliation |
| every 60 s | Universe re-rank, regime update |
| 14:45 | Entry cutoff |
| 15:00 | Flattening begins (patient → aggressive) |
| 15:10 | Hard flatten deadline |
| 15:15 | Verify flat; residual → RED alert, keep flattening (Upstox auto square-off at 15:25 is the last-resort backstop) |
| 15:35 | Post-session report; data compaction; backup |
