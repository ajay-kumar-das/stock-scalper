"""Trading engine: composes all components. The SAME engine runs in BACKTEST, REPLAY, PAPER and
LIVE; only the clock, data source and execution adapter differ (FR-MODE-5, ADR-2)."""
from __future__ import annotations

import itertools
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from .core.clock import Clock
from .core.types import Bar, Instrument, Mode, OrderRole, OrderStatus, Reason, Side
from .costs.upstox import UpstoxCostModel
from .decision.pipeline import DecisionConfig, DecisionPipeline
from .execution.base import ExecutionAdapter
from .features.intraday import FeatureState
from .journal.journal import Journal
from .oms.oms import OMS, InvariantViolation, Throttle, ThrottleConfig
from .portfolio.manager import ManagerConfig, PositionManager
from .portfolio.portfolio import Portfolio, PosState, Position
from .regime.model import RegimeConfig, RegimeModel
from .risk.engine import OpenRisk, RiskConfig, RiskEngine, RiskState
from .selection.universe import UniverseConfig, Watchlist, eligibility, opportunity_scores
from .session.controller import SessionController, SessionState, SessionTimes
from .strategies.base import Strategy, StrategyContext


@dataclass
class EngineConfig:
    risk: RiskConfig = field(default_factory=RiskConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    manager: ManagerConfig = field(default_factory=ManagerConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    times: SessionTimes = field(default_factory=SessionTimes)
    throttle: ThrottleConfig = field(default_factory=ThrottleConfig)
    rank_every_min: int = 1
    or_minutes: int = 15
    entry_ttl_s: int = 3
    auto_squareoff_fee: float = 88.50


class Engine:
    def __init__(self, mode: Mode, clock: Clock, instruments: dict[str, Instrument], index_key: str,
                 strategies: list[Strategy], execution: ExecutionAdapter, journal: Journal,
                 cfg: Optional[EngineConfig] = None, half_spread=None) -> None:
        self.mode, self.clock, self.cfg = mode, clock, cfg or EngineConfig()
        self.instruments, self.index_key = instruments, index_key
        self.strategies = {s.name: s for s in strategies}
        self.execution, self.journal = execution, journal
        self.cost_model = UpstoxCostModel()
        self.half_spread = half_spread or (lambda key, px: max(instruments[key].tick, int(px * 3 / 1e4)))
        self.anomalies: list[str] = []
        self.feats = {k: FeatureState(k, or_minutes=self.cfg.or_minutes) for k in instruments}
        self.regime_model = RegimeModel(self.cfg.regime)
        self.risk = RiskEngine(self.cfg.risk)
        self.session = SessionController(self.cfg.times)
        self.watch = Watchlist(self.cfg.universe)
        self.eligible: set[str] = set()
        self.watchlist: set[str] = set()
        self.oms = OMS(clock, execution, self._position_qty, Throttle(clock, self.cfg.throttle),
                       run_tag="sc", on_event=self._event, on_anomaly=self._anomaly)
        self.portfolio = Portfolio(self.cost_model, lambda oid: self.oms.orders[oid],
                                   on_trade_closed=self._on_trade_closed, on_entry_fill=self._on_entry_fill,
                                   on_event=self._event)
        self.oms.fill_listener = self.portfolio
        self.manager = PositionManager(self.cfg.manager, self.oms, self.portfolio, instruments, self.strategies,
                                       self.half_spread, self._event, self._anomaly)
        self.portfolio.on_closing_terminal = self.manager.on_closing_terminal
        self.portfolio.on_flat_with_working_entry = self.manager.on_flat_with_working_entry
        self.oms.on_modify_rejected = self.manager.on_modify_rejected
        self.pipeline = DecisionPipeline(self.cfg.decision, self.risk, self.cost_model, self.half_spread)
        self._pos_seq = itertools.count(1)
        self.rejections: Counter = Counter()
        self.would_fail_edge: Counter = Counter()
        self.candidates_seen = 0
        self.current_day: Optional[date] = None
        self.minute = 0
        self.eod_residuals: list[str] = []
        self._first_session = True
        from .session.calendar import TradingCalendar
        self.calendar = TradingCalendar()
        self.warmup = False
        self.quarantined: dict = {}              # key -> last time bad data was seen (blocks entries 60 s)
        self.mark_prices: dict = {}              # key -> last valid LTP from the tick stream
        self.event_exclusions: dict = {}         # {date: {symbols}} (results/board meetings): excluded that day

    # ------------------------------------------------------------------ callbacks
    def _position_qty(self, key: str) -> int:
        return self.portfolio.position_qty(key)

    def _event(self, kind: str, payload: dict) -> None:
        ref = payload.get("position_id", "")
        self.journal.record(self.clock.now(), kind, payload, key=payload.get("key", ""),
                            strategy=payload.get("strategy", ""), ref=ref)

    def _anomaly(self, msg: str) -> None:
        self.anomalies.append(f"{self.clock.now()} {msg}")
        self.journal.record(self.clock.now(), "anomaly", {"text": msg})
        if "UNKNOWN" in msg:
            self.session.enter_defensive(self.clock.now(), "ORDER_UNKNOWN")      # auto-clears once reconciled
        elif any(k in msg for k in ("NEGATIVE POSITION", "decreased", "overfill", "LISTENER_ERROR", "unknown order")):
            self.session.enter_defensive(self.clock.now(), "STATE_ANOMALY")      # needs operator attention

    def _on_entry_fill(self, pos: Position, order, qty: int) -> None:
        self.manager.on_entry_fill(pos, order, qty)

    def _on_trade_closed(self, trade: dict) -> None:
        self.risk.on_trade_closed(trade["strategy"], trade["key"], trade["net_pnl"], self.clock.now())
        self.journal.record_trade(trade)

    # ------------------------------------------------------------------ session boundaries
    def start_day(self, d: date) -> None:
        self.current_day = d
        if not self._first_session:
            self.risk.new_session()
        self._first_session = False
        self.portfolio.new_day()
        for s in self.strategies.values():
            s.new_day()
        self.quarantined.clear()
        # previous session ended flat and reconciled (end_day), so earlier possible fills are resolved
        for k in list(self.oms.possibly_filled):
            if self.portfolio.position_qty(k) == 0:
                self.oms.clear_possibly_filled(k)
        self.session.start_day(self.clock.now(), reconciled=True, trade_allowed=self.calendar.trade_allowed(d))
        self.eligible = set()
        self.watch = Watchlist(self.cfg.universe)
        self.minute = 0
        self._day_init_pending = True

    def _build_universe(self) -> None:
        reasons = Counter()
        self.eligible = set()
        excluded_today = self.event_exclusions.get(self.current_day, set())
        for k, inst in self.instruments.items():
            r = eligibility(inst, self.feats[k], self.cfg.universe)
            if r is None and inst.symbol in excluded_today:
                r = Reason("CORPORATE_EVENT", "board meeting / corporate action today")
            if r is None:
                self.eligible.add(k)
            else:
                reasons[r.code] += 1
        self.journal.record(self.clock.now(), "universe_built", {"eligible": sorted(self.eligible),
                                                                "excluded_by": dict(reasons)})

    def end_day(self) -> None:
        """Verify flat (FR-SESS-3). Any residual is a severity-1 anomaly. In simulation modes the
        broker's auto square-off is modelled: residual closed at the last price minus half-spread and
        charged the auto square-off fee, so P&L is never silently carried. In LIVE the protective stop
        stays working and the operator is alerted (the broker backstop is at 15:25)."""
        now = self.clock.now()
        for pos in list(self.portfolio.open_positions()):
            for o in self.oms.working(pos.key, role=OrderRole.ENTRY):
                self.oms.cancel(o, "end of day")
            if pos.qty > 0:
                self.eod_residuals.append(f"{self.current_day} {pos.key} qty={pos.qty}")
                self._anomaly(f"RESIDUAL POSITION at EOD {pos.key} qty={pos.qty} (severity 1)")
                if self.mode != Mode.LIVE:
                    self.manager.suspended = True
                    for o in self.oms.working(pos.key, side=Side.SELL):
                        self.oms.cancel(o, "broker auto square-off")
                    last = self.feats[pos.key].last
                    ref = self.mark_prices.get(pos.key) or (last.c if last else pos.stop)
                    px = ref - self.half_spread(pos.key, ref)
                    pos.exit_reasons.append(Reason("AUTO_SQUARE_OFF", "broker auto square-off of residual (simulated)"))
                    self.portfolio.extra_costs[pos.position_id] = self.cfg.auto_squareoff_fee
                    o = self.oms.force_square_off(pos.key, pos.qty, px, pos.strategy, pos.position_id, now)
                    pos.order_ids.append(o.order_id)
                    self.manager.suspended = False
        if self.mode != Mode.LIVE:
            for o in self.oms.working():
                self.oms.cancel(o, "end of day")
        self.journal.record(now, "eod_reconciliation",
                            {"open_positions": [p.key for p in self.portfolio.open_positions()],
                             "working_orders": [o.order_id for o in self.oms.working()],
                             "risk_events": self.risk.events[-10:], "session_log": self.session.log[-10:]})
        self.journal.flush()

    # ------------------------------------------------------------------ main step
    def on_minute(self, bars: dict[str, Bar], fill_step) -> None:
        """Process one closed minute. `fill_step(bar)` lets the execution adapter match working
        orders against the bar BEFORE features update (orders were placed earlier)."""
        now = self.clock.now()
        for b in bars.values():
            fill_step(b)
        for k, b in bars.items():
            self.feats[k].update(b)
        if getattr(self, "_day_init_pending", False):
            self._build_universe()
            self._day_init_pending = False
        self.minute += 1
        idx = self.feats[self.index_key]
        tradable = {k: self.feats[k] for k in self.eligible}
        regime = self.regime_model.update(idx, tradable.values())
        ctx = StrategyContext(index=idx, regime=regime)
        if self.minute % self.cfg.rank_every_min == 0 and tradable:
            cost_bps = {k: 2 * self.half_spread(k, f.last.c) / f.last.c * 1e4 + 10 for k, f in tradable.items() if f.last}
            scores = opportunity_scores(tradable, idx, cost_bps, self.cfg.universe)
            pinned = {p.key for p in self.portfolio.open_positions()}
            self.watchlist = self.watch.update(scores, pinned)

        # risk marks and kill switches
        # open-position P&L for the risk engine: realised part of partially closed positions, the
        # remainder marked at the bid, all costs incurred so far and estimated exit costs (review finding 4)
        unreal = 0.0
        for p in self.portfolio.open_positions():
            if p.entry_qty == 0:
                continue
            b = bars.get(p.key)
            last = b.c if b else (self.feats[p.key].last.c if self.feats[p.key].last else int(p.avg_entry))
            bid = last - self.half_spread(p.key, last)
            entry_c, exit_c = self.portfolio.order_costs(p)
            unreal += (p.exit_value - p.avg_entry * p.exit_qty) / 100.0
            unreal += (bid - p.avg_entry) * p.qty / 100.0
            unreal -= entry_c + exit_c
            if p.qty > 0:
                unreal -= self.cost_model.order_charges(Side.SELL, p.qty * bid / 100.0).total
        prev_state = self.risk.state
        self.risk.update_marks(unreal, now)
        if self.risk.state == RiskState.HALTED_FOR_DAY and prev_state != RiskState.HALTED_FOR_DAY:
            self.session.halt_for_day(now, self.risk.halt_reason)
            self._event("risk_halt", {"reason": self.risk.halt_reason})
            for o in self.oms.working(role=OrderRole.ENTRY):
                self.oms.cancel(o, "risk halt")
        state = self.session.tick(now)

        # manage open positions first (exits before entries)
        force: Optional[Reason] = None
        if state == SessionState.FLATTENING:
            force = Reason("FLATTEN_DEADLINE" if now.time() >= self.cfg.times.flatten_deadline else "FLATTEN",
                           f"end-of-day flatten ({now:%H:%M})")
        elif state == SessionState.HALTED_FOR_DAY:
            force = Reason("RISK_HALT", self.risk.halt_reason)
        elif state == SessionState.EMERGENCY:
            force = Reason("EMERGENCY", "emergency shutdown")
        for p in list(self.portfolio.open_positions()):
            b = bars.get(p.key)
            if b is None:
                continue
            self.manager.manage(p, b, self.feats[p.key], ctx, now, force_exit=force)

        from datetime import timedelta as _td
        self.oms.expire_stale_entries(_td(seconds=self.cfg.entry_ttl_s))

        # new entries
        if self.warmup:
            return          # warm-up days build features/regime/universe state only (walk-forward windows)
        session_ok = self.session.entries_allowed(now)
        if not session_ok[0]:
            return
        for key in sorted(self.watchlist):
            b = bars.get(key)
            if b is None:
                continue
            inst = self.instruments[key]
            for strat in self.strategies.values():
                if not (strat.earliest_entry <= now.time() <= strat.latest_entry):
                    continue
                cand = strat.on_bar(inst, b, self.feats[key], ctx)
                if cand is None:
                    continue
                self.candidates_seen += 1
                self._decide(cand, inst, strat, ctx, now, session_ok)

    def _decide(self, cand, inst: Instrument, strat: Strategy, ctx: StrategyContext, now: datetime, session_ok) -> None:
        f = self.feats[inst.key]
        open_risks = []
        for p in self.portfolio.open_positions():
            if p.qty <= 0:
                continue
            pending = sum(o.remaining for o in self.oms.working(p.key, role=OrderRole.ENTRY))
            open_risks.append(OpenRisk(p.key, self.instruments[p.key].sector,
                                       (p.qty * p.avg_entry + pending * p.decision_price) / 100.0,
                                       max(0.0, (p.avg_entry - p.stop) * p.qty / 100.0)
                                       + max(0.0, (p.decision_price - p.initial_stop) * pending / 100.0), p.strategy))
        open_risks += [OpenRisk(p.key, self.instruments[p.key].sector, p.planned_qty * p.decision_price / 100.0,
                                p.planned_risk_rupees, p.strategy)
                       for p in self.portfolio.open_positions() if p.qty == 0 and p.state == PosState.OPENING]
        dup = None
        if self.oms.working(inst.key, role=OrderRole.ENTRY) or self._position_qty(inst.key) or inst.key in self.portfolio.open_by_key:
            dup = f"position or working entry already exists for {inst.symbol}"
        elif self.oms.has_unknown(inst.key) or inst.key in self.oms.possibly_filled:
            dup = "order state UNKNOWN / unreconciled possible fill for instrument"
        stale = f.last is None or (now - f.last.end).total_seconds() > 5
        q_since = self.quarantined.get(inst.key)
        if q_since is not None and (now - q_since).total_seconds() < 60:
            stale = True
        d = self.pipeline.evaluate(cand, inst, now=now, session_ok=session_ok, regime_ok=strat.permitted(ctx.regime),
                                   regime=ctx.regime, strategy_max_trades=strat.max_trades_per_day,
                                   instrument_ok=(not stale, "stale data" if stale else "ok"),
                                   avg_1min_volume=f.vol_avg20, open_risks=open_risks, duplicate=dup)
        for gate, res in d.gate_trace:
            if res.startswith("WOULD_FAIL"):
                self.would_fail_edge[res.split()[1]] += 1
        base = {"setup_id": cand.setup_id, "candidate": {"entry_max": cand.entry_max, "stop": cand.stop,
                "target": cand.target, "confidence": cand.confidence, "evidence": cand.evidence},
                "regime": ctx.regime.label, "gate_trace": d.gate_trace,
                "edge": d.edge.__dict__ if d.edge else None}
        if not d.accepted:
            self.rejections[d.rejection.code] += 1
            self.journal.record_rejection(now, inst.key, cand.strategy, d.rejection.code,
                                          {**base, "rejection": d.rejection.as_dict(),
                                           "also_failed": [r.as_dict() for r in d.also_failed]})
            return
        pid = f"{now:%Y%m%d}-{next(self._pos_seq)}"
        hs = self.half_spread(inst.key, cand.entry_max)
        pos = Position(position_id=pid, key=inst.key, symbol=inst.symbol, strategy=cand.strategy,
                       strategy_version=cand.strategy_version, setup_id=cand.setup_id, decision_ts=now,
                       decision_price=cand.entry_ideal + hs, initial_stop=cand.stop, stop=cand.stop,
                       target=cand.target, confidence=cand.confidence, expected_hold_min=strat.t_expect_min,
                       t_max_min=strat.t_max_min, regime=ctx.regime.label, evidence=cand.evidence,
                       entry_reasons=cand.reasons, planned_qty=d.qty,
                       planned_risk_rupees=d.qty * (cand.entry_max - cand.stop) / 100.0,
                       planned_reward_rupees=d.qty * (cand.target - cand.entry_max) / 100.0)
        # F23 fat-finger guard: independent sanity check right before the order leaves
        last = f.last.c if f.last else cand.entry_max
        max_notional = self.cfg.risk.capital * self.cfg.risk.leverage_cap * self.cfg.risk.max_position_frac
        if abs(d.limit_price - last) > 0.01 * last or d.qty * d.limit_price / 100.0 > max_notional * 1.001 or d.qty <= 0:
            self._anomaly(f"FAT_FINGER blocked {inst.symbol} qty={d.qty} limit={d.limit_price} last={last}")
            self.session.enter_defensive(now, "FAT_FINGER")
            return
        try:
            self.portfolio.open(pos)
            self.journal.record(now, "decision_accepted", {**base, "qty": d.qty, "limit": d.limit_price,
                                "sizing": {"limiting": d.sizing.limiting, **d.sizing.detail} if d.sizing else {}},
                                key=inst.key, strategy=cand.strategy, ref=pid)
            o = self.oms.submit_entry(inst.key, d.qty, d.limit_price, cand.strategy, pid)
        except InvariantViolation as e:
            self._anomaly(f"invariant: {e}")
            self.portfolio.open_by_key.pop(inst.key, None)
            return
        if o is None:
            self.portfolio.open_by_key.pop(inst.key, None)
            pos.state = PosState.ABANDONED
            return
        pos.order_ids.append(o.order_id)
        if o.status != OrderStatus.REJECTED:
            self.risk.record_entry(cand.strategy, inst.key)
        if o.is_terminal and o.filled_qty == 0:
            self.portfolio.on_order_terminal(o)

    def on_bad_data(self, key: str, reason: str, now: datetime) -> None:
        """Quarantine an instrument (FR-DATA-4): entries blocked for 60 s after the last bad tick."""
        if key not in self.quarantined:
            # instrument-level data problem: alert, but do not trip system-wide DEFENSIVE
            msg = f"data quarantine {key}: {reason}"
            self.anomalies.append(f"{now} {msg}")
            self.journal.record(now, "data_quarantine", {"key": key, "reason": reason})
        self.quarantined[key] = now

    def on_valid_tick(self, key: str, ltp: int) -> None:
        self.mark_prices[key] = ltp

    # ------------------------------------------------------------------ continuous housekeeping (tick-driven modes)
    def housekeeping(self, now: datetime, feed_gap_s: float = 0.0) -> None:
        """Called on every tick in REPLAY/PAPER/LIVE: ack timeouts, reconciliation of UNKNOWN orders,
        entry TTL, and feed-health DEFENSIVE handling (F2/F3/F5)."""
        from datetime import timedelta as _td
        self.oms.check_ack_timeouts(_td(seconds=2))
        self.oms.reconcile_unknown()
        self.oms.expire_stale_entries(_td(seconds=self.cfg.entry_ttl_s))
        if not self.oms.has_unknown():
            self.session.clear_defensive(now, "ORDER_UNKNOWN")
        in_session = self.cfg.times.market_open <= now.time() <= self.cfg.times.market_close
        if in_session and feed_gap_s > 3.0:
            if "FEED_STALE" not in self.session.defensive_reasons:
                self._event("feed_stale", {"gap_s": round(feed_gap_s, 1)})
            self.session.enter_defensive(now, "FEED_STALE")
            self._feed_healthy_since = None
        elif "FEED_STALE" in self.session.defensive_reasons:
            if getattr(self, "_feed_healthy_since", None) is None:
                self._feed_healthy_since = now
            elif (now - self._feed_healthy_since).total_seconds() >= 30:
                self.session.clear_defensive(now, "FEED_STALE")
                self._event("feed_recovered", {})

    # ------------------------------------------------------------------ manual controls (L0)
    def pause(self):
        self.session.pause(self.clock.now()); self._event("manual", {"action": "pause"})

    def resume(self):
        self.session.resume(self.clock.now()); self._event("manual", {"action": "resume"})

    def exit_all(self, ref_prices: dict[str, int]):
        self._event("manual", {"action": "exit_all"})
        for p in list(self.portfolio.open_positions()):
            self.manager.request_exit(p, Reason("MANUAL_EXIT", "operator exit-all"), ref_prices.get(p.key, p.stop), aggressive=True)

    def emergency(self, ref_prices: dict[str, int]):
        self.session.emergency_stop(self.clock.now())
        for o in self.oms.working(role=OrderRole.ENTRY):
            self.oms.cancel(o, "emergency")
        self.exit_all(ref_prices)
