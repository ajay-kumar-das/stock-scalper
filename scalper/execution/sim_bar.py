"""Conservative bar-based execution simulator (doc 08 §1, FR-SIM-1/2).

Timing model
  * The engine processes bar B (start B.ts) at B.end. Orders created at decision time
    t (= some bar's end) are active from t, i.e. from the NEXT bar. No decide-and-fill in
    the same bar.
  * Entry BUY limits have a 1-bar TTL (3 s in reality): they can only fill at the next bar's
    open, as a marketable order crossing the half-spread, and only if that price <= limit.
    No chasing. Remainder after the volume cap is cancelled.
  * Protective stops placed in response to an entry fill are checked against the SAME bar's
    low (we cannot know intrabar ordering, so we assume the worst).
  * Stop-limit: triggers on low <= trigger (or a gap open below trigger). Fill reference is
    the trigger, or the open if it gapped through. If the reference minus slippage is below the
    limit, it does not fill (realistic SL-limit risk) and rests as a sell limit.
  * Sell limits fill at max(limit, open - half_spread - impact) when marketable at the open,
    otherwise only on a strict trade-through (high - half_spread > limit).
  * Costs are not applied here; the portfolio applies the full cost model per order.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from ..core.clock import Clock
from ..core.types import Bar, Instrument, OrderRole, OrderStatus, OrderType, Side
from ..oms.order import Order
from .base import ExecutionAdapter, OrderUpdate


@dataclass
class SimConfig:
    default_half_spread_bps: float = 3.0
    min_half_spread_ticks: int = 1
    entry_volume_cap: float = 0.10
    exit_volume_cap: float = 0.25
    impact_coef_bps: float = 10.0      # impact_bps = coef * qty / (0.1 * bar_volume)
    impact_cap_bps: float = 25.0
    stop_extra_slip_ticks: int = 1
    reject_rate: float = 0.005
    seed: int = 7
    half_spread_bps: dict[str, float] = field(default_factory=dict)   # per-instrument override


@dataclass
class _Working:
    order: Order
    active_from: object  # datetime
    first_bar_done: bool = False


class SimBarExecution(ExecutionAdapter):
    def __init__(self, clock: Clock, instruments: dict[str, Instrument], cfg: Optional[SimConfig] = None) -> None:
        self.clock = clock
        self.instruments = instruments
        self.cfg = cfg or SimConfig()
        self.rng = random.Random(self.cfg.seed)
        self.working: dict[str, _Working] = {}
        self._processing: Optional[Bar] = None
        self._same_bar: list[str] = []
        self.stats = {"placed": 0, "rejected": 0, "filled_orders": 0, "expired": 0}

    # ------------------------------------------------------------------ adapter API
    def place(self, order: Order) -> None:
        self.stats["placed"] += 1
        now = self.clock.now()
        if self.rng.random() < self.cfg.reject_rate:
            self.stats["rejected"] += 1
            self._emit(OrderUpdate(order.order_id, order.tag, OrderStatus.REJECTED, 0, 0, now, "SIM_RANDOM_REJECT"))
            return
        self._emit(OrderUpdate(order.order_id, order.tag, OrderStatus.ACKNOWLEDGED, 0, 0, now, "ack"))
        self.working[order.order_id] = _Working(order, now)
        if self._processing is not None and order.key == self._processing.key and order.role == OrderRole.STOP:
            self._same_bar.append(order.order_id)

    def modify(self, order: Order, **kwargs) -> None:
        w = self.working.get(order.order_id)
        if w is None:
            return
        # params already updated on the shared Order object; they take effect from now
        w.active_from = max(w.active_from, self.clock.now())
        self._emit(OrderUpdate(order.order_id, order.tag, order.status, order.filled_qty, order.filled_value,
                               self.clock.now(), "MODIFY_ACK", triggered=order.triggered))

    def cancel(self, order: Order) -> None:
        w = self.working.pop(order.order_id, None)
        if w is None:
            return
        self._emit(OrderUpdate(order.order_id, order.tag, OrderStatus.CANCELLED, order.filled_qty,
                               order.filled_value, self.clock.now(), "cancel confirmed"))

    # ------------------------------------------------------------------ simulation
    def half_spread(self, key: str, price: int) -> int:
        inst = self.instruments[key]
        bps = self.cfg.half_spread_bps.get(key, self.cfg.default_half_spread_bps)
        return max(inst.tick * self.cfg.min_half_spread_ticks, int(round(price * bps / 1e4)))

    def _impact(self, price: int, qty: int, bar: Bar) -> int:
        if bar.v <= 0:
            return int(price * self.cfg.impact_cap_bps / 1e4)
        bps = min(self.cfg.impact_cap_bps, self.cfg.impact_coef_bps * qty / max(1.0, 0.1 * bar.v))
        return int(round(price * bps / 1e4))

    def process_bar(self, bar: Bar) -> None:
        self._processing = bar
        self._same_bar = []
        try:
            for oid in [oid for oid, w in self.working.items() if w.order.key == bar.key]:
                w = self.working.get(oid)
                if w is None or w.active_from > bar.ts:
                    continue
                self._evaluate(w, bar, same_bar=False)
            while self._same_bar:
                oid = self._same_bar.pop(0)
                w = self.working.get(oid)
                if w is not None:
                    self._evaluate(w, bar, same_bar=True)
        finally:
            self._processing = None

    def _fill(self, w: _Working, qty: int, price: int, ts, bar: Bar, reason: str) -> None:
        o = w.order
        qty = min(qty, o.remaining)
        if qty <= 0:
            return
        cum_qty = o.filled_qty + qty
        cum_val = o.filled_value + qty * price
        status = OrderStatus.FILLED if cum_qty == o.qty else OrderStatus.PARTIALLY_FILLED
        if status == OrderStatus.FILLED:
            self.working.pop(o.order_id, None)
            self.stats["filled_orders"] += 1
        self._emit(OrderUpdate(o.order_id, o.tag, status, cum_qty, cum_val, ts, reason, triggered=o.triggered))

    def _expire(self, w: _Working, ts, reason: str) -> None:
        o = w.order
        self.working.pop(o.order_id, None)
        self.stats["expired"] += 1
        self._emit(OrderUpdate(o.order_id, o.tag, OrderStatus.CANCELLED, o.filled_qty, o.filled_value, ts, reason))

    def _evaluate(self, w: _Working, bar: Bar, same_bar: bool) -> None:
        o = w.order
        hs = self.half_spread(o.key, bar.o)
        tick = self.instruments[o.key].tick
        if o.side == Side.BUY:
            if o.order_type != OrderType.LIMIT:
                raise NotImplementedError("v1 uses limit entries only")
            cap = max(0, int(self.cfg.entry_volume_cap * bar.v))
            ask = bar.o + hs
            if ask <= o.price and cap > 0:
                qty = min(o.remaining, cap)
                px = min(o.price, ask + self._impact(bar.o, qty, bar))
                self._fill(w, qty, px, bar.ts, bar, "entry@open")
            if o.order_id in self.working:     # TTL: one bar only, no chasing
                self._expire(w, bar.ts + timedelta(seconds=3), "entry TTL expired")
            return

        # SELL side: stop-limit (untriggered) or limit
        cap = max(1, int(self.cfg.exit_volume_cap * bar.v))
        ts_mid = bar.ts + timedelta(seconds=30)
        if o.order_type in (OrderType.SL, OrderType.SL_M) and not o.triggered:
            if same_bar:
                touched = bar.l <= o.trigger
                ref = o.trigger
            else:
                touched = bar.o <= o.trigger or bar.l <= o.trigger
                ref = bar.o if bar.o <= o.trigger else o.trigger
            if not touched:
                return
            o.triggered = True
            qty = min(o.remaining, cap)
            px = ref - hs - self.cfg.stop_extra_slip_ticks * tick - self._impact(ref, qty, bar)
            if o.order_type == OrderType.SL_M or px >= o.price:
                self._fill(w, qty, max(px, tick), ts_mid, bar, "stop triggered")
            else:
                # gapped through the limit: rests unfilled as a limit at o.price (escalation handled upstream)
                self._emit(OrderUpdate(o.order_id, o.tag, o.status, o.filled_qty, o.filled_value, ts_mid,
                                       "stop triggered, limit not reached", triggered=True))
            return

        # plain sell limit (exit / modified stop / triggered stop-limit resting)
        if same_bar:
            return
        bid_open = bar.o - hs
        if bid_open >= o.price:
            qty = min(o.remaining, cap)
            px = max(o.price, bid_open - self._impact(bar.o, qty, bar))
            self._fill(w, qty, px, bar.ts, bar, "sell limit marketable at open")
        elif bar.h - hs > o.price:
            qty = min(o.remaining, cap)
            self._fill(w, qty, o.price, ts_mid, bar, "sell limit trade-through")
