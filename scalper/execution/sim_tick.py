"""Tick-level execution simulator for REPLAY and PAPER (doc 08 §1, FR-SIM-1).

Pessimistic by default:
  * Latency: every place/modify/cancel reaches the "exchange" after a lognormal delay
    (median 150 ms, p95 ~400 ms). The book AT ARRIVAL is used, not the book at decision time.
  * Cancels race with fills: an order can still fill before its cancel arrives.
  * Marketable orders walk the displayed depth (5 levels in Upstox `full` mode) up to the limit;
    the remainder rests.
  * Resting orders join the BACK of the displayed queue at their price. They fill only when
    traded volume at that price exceeds the queue ahead, or when the market trades through.
  * Stop-limit sells trigger on LTP <= trigger and then behave as sell limits at the stop limit.
  * Random rejects and (for fault tests) missing acknowledgements.
Costs are applied by the portfolio, not here.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from ..core.clock import Clock
from ..core.types import Instrument, OrderStatus, OrderType, Side
from ..data.ticks import Tick
from ..oms.order import Order
from .base import ExecutionAdapter, OrderUpdate


@dataclass
class TickSimConfig:
    latency_median_ms: float = 150.0
    latency_sigma: float = 0.6            # lognormal sigma -> p95 ~ 2.7x median
    reject_rate: float = 0.005
    no_ack_rate: float = 0.0              # fault injection: order accepted but ack never arrives
    seed: int = 13


@dataclass
class _W:
    order: Order
    arrive_at: datetime
    arrived: bool = False
    queue_ahead: int = 0
    cancel_at: Optional[datetime] = None
    silent: bool = False                   # no-ack fault: fills still happen, acks don't
    seq: int = 0


class SimTickExecution(ExecutionAdapter):
    def __init__(self, clock: Clock, instruments: dict[str, Instrument], cfg: Optional[TickSimConfig] = None) -> None:
        self.clock, self.instruments = clock, instruments
        self.cfg = cfg or TickSimConfig()
        self.rng = random.Random(self.cfg.seed)
        self.working: dict[str, _W] = {}
        self.taken: dict[tuple, int] = {}     # (key, 'ask'|'bid', price) -> qty we consumed from that displayed level
        self._seq = 0
        self.last_vtt: dict[str, int] = {}
        self.last_tick: dict[str, Tick] = {}
        self.stats = {"placed": 0, "rejected": 0, "fills": 0, "no_ack": 0}

    # ------------------------------------------------------------------ adapter API
    def latency(self) -> timedelta:
        ms = self.cfg.latency_median_ms * math.exp(self.rng.gauss(0, self.cfg.latency_sigma))
        return timedelta(milliseconds=min(ms, 5_000))

    def place(self, order: Order) -> None:
        self.stats["placed"] += 1
        now = self.clock.now()
        if self.rng.random() < self.cfg.reject_rate:
            self.stats["rejected"] += 1
            self._emit(OrderUpdate(order.order_id, order.tag, OrderStatus.REJECTED, 0, 0, now + self.latency(),
                                   "SIM_RANDOM_REJECT"))
            return
        silent = self.rng.random() < self.cfg.no_ack_rate
        if silent:
            self.stats["no_ack"] += 1
        self._seq += 1
        self.working[order.order_id] = _W(order, now + self.latency(), silent=silent, seq=self._seq)

    def modify(self, order: Order, **kwargs) -> None:
        w = self.working.get(order.order_id)
        if w is None:
            self._emit(OrderUpdate(order.order_id, order.tag, order.status, order.filled_qty, order.filled_value,
                                   self.clock.now(), "MODIFY_REJECTED"))
            return
        # parameters already updated on the shared Order; they take effect on arrival, with a fresh queue position
        w.arrive_at = max(w.arrive_at, self.clock.now() + self.latency())
        w.arrived = False
        if not w.silent:
            self._emit(OrderUpdate(order.order_id, order.tag, order.status, order.filled_qty, order.filled_value,
                                   self.clock.now(), "MODIFY_ACK", triggered=order.triggered))

    def cancel(self, order: Order) -> None:
        w = self.working.get(order.order_id)
        if w is not None and w.cancel_at is None:
            w.cancel_at = self.clock.now() + self.latency()

    def query(self, order: Order) -> Optional[OrderUpdate]:
        """Broker-side truth for reconciliation (FR-OMS-5 / FR-REC-3)."""
        w = self.working.get(order.order_id)
        now = self.clock.now()
        if w is None:
            return None
        status = OrderStatus.PARTIALLY_FILLED if order.filled_qty else OrderStatus.ACKNOWLEDGED
        w.silent = False
        return OrderUpdate(order.order_id, order.tag, status, order.filled_qty, order.filled_value, now, "reconciled")

    # ------------------------------------------------------------------ simulation
    def on_time(self, now: datetime) -> None:
        """Time-driven events that must not wait for a tick in that instrument: order arrival/ack
        (evaluated against the latest known book) and cancel confirmations."""
        for w in sorted(self.working.values(), key=lambda w: w.seq):
            oid = w.order.order_id
            if oid not in self.working:
                continue
            if w.cancel_at is not None and now >= w.cancel_at:
                self._finish(w, OrderStatus.CANCELLED, w.cancel_at, "cancel confirmed")
                continue
            if not w.arrived and now >= w.arrive_at:
                t = self.last_tick.get(w.order.key)
                w.arrived = True
                if w.order.status == OrderStatus.SUBMITTED and not w.silent:
                    self._emit(OrderUpdate(oid, w.order.tag, OrderStatus.ACKNOWLEDGED, w.order.filled_qty,
                                           w.order.filled_value, w.arrive_at, "ack"))
                if t is not None:
                    self._on_arrival(w, t)

    def process_tick(self, t: Tick) -> None:
        prev_vtt = self.last_vtt.get(t.key, t.vtt)
        dv = max(0, t.vtt - prev_vtt)
        self.last_vtt[t.key] = t.vtt
        prev = self.last_tick.get(t.key)
        self.last_tick[t.key] = t
        self._refresh_ledger(t)
        budget = [dv]                               # traded volume this tick, shared by all resting orders
        ws = sorted((w for w in self.working.values() if w.order.key == t.key), key=lambda w: w.seq)
        for w in ws:
            oid = w.order.order_id
            if oid not in self.working:
                continue
            if w.cancel_at is not None and t.recv_ts >= w.cancel_at:
                self._finish(w, OrderStatus.CANCELLED, t.recv_ts, "cancel confirmed")
                continue
            if not w.arrived:
                if t.recv_ts < w.arrive_at:
                    continue
                w.arrived = True
                if w.order.status == OrderStatus.SUBMITTED and not w.silent:
                    self._emit(OrderUpdate(oid, w.order.tag, OrderStatus.ACKNOWLEDGED, w.order.filled_qty,
                                           w.order.filled_value, t.recv_ts, "ack"))
                self._on_arrival(w, t)
            else:
                self._on_resting(w, t, prev, budget)

    # displayed liquidity can be consumed only once (review finding: re-walking the same depth)
    def _refresh_ledger(self, t: Tick) -> None:
        present = {("ask", p) for p, q, *_ in t.asks} | {("bid", p) for p, q, *_ in t.bids}
        for k in [k for k in self.taken if k[0] == t.key and (k[1], k[2]) not in present]:
            del self.taken[k]

    def _levels(self, t: Tick, side: Side) -> list[tuple[int, int]]:
        """Opposite side of the book for an order of `side`, net of liquidity we already took."""
        book_side = "ask" if side == Side.BUY else "bid"
        lv = t.asks if side == Side.BUY else t.bids
        out = []
        for p, q, *_ in lv:
            avail = q - self.taken.get((t.key, book_side, p), 0)
            if avail > 0:
                out.append((p, avail))
        return out

    def _walk(self, w: _W, t: Tick, limit: int, at_limit: bool = False) -> None:
        """Take displayed liquidity up to `limit`. On arrival we pay the book's prices (we are the
        aggressor); a resting order hit by an incoming order is filled at its OWN limit (at_limit)."""
        o = w.order
        book_side = "ask" if o.side == Side.BUY else "bid"
        filled_qty, value = 0, 0
        for p, q in self._levels(t, o.side):
            if (o.side == Side.BUY and p > limit) or (o.side == Side.SELL and p < limit):
                break
            take = min(q, o.remaining - filled_qty)
            filled_qty += take
            value += take * (limit if at_limit else p)
            self.taken[(t.key, book_side, p)] = self.taken.get((t.key, book_side, p), 0) + take
            if filled_qty >= o.remaining:
                break
        if filled_qty:
            self._fill(w, filled_qty, value, t.recv_ts, "hit at limit" if at_limit else "book walk")

    def _effective_limit(self, o: Order) -> Optional[int]:
        if o.order_type == OrderType.LIMIT:
            return o.price
        if o.order_type == OrderType.SL:
            return o.price if o.triggered else None
        if o.order_type == OrderType.SL_M:
            return 1 if o.triggered else None           # market with protection: walk whatever is there
        return o.price

    @staticmethod
    def _displayed(t: Optional[Tick], side: Side, price: int) -> int:
        if t is None:
            return 0
        same = t.bids if side == Side.BUY else t.asks
        return sum(q for p, q, *_ in same if p == price)

    def _on_arrival(self, w: _W, t: Tick) -> None:
        o = w.order
        if o.order_type in (OrderType.SL, OrderType.SL_M) and not o.triggered:
            if t.ltp <= o.trigger:
                o.triggered = True
            else:
                return
        lim = self._effective_limit(o)
        if lim is None:
            return
        self._walk(w, t, lim)
        if o.order_id in self.working:
            w.queue_ahead = self._displayed(t, o.side, lim)          # join the back of the queue

    def _on_resting(self, w: _W, t: Tick, prev: Optional[Tick], budget: list) -> None:
        o = w.order
        if o.order_type in (OrderType.SL, OrderType.SL_M) and not o.triggered:
            if t.ltp <= o.trigger:
                o.triggered = True
                self._emit(OrderUpdate(o.order_id, o.tag, o.status, o.filled_qty, o.filled_value, t.recv_ts,
                                       "stop triggered", triggered=True))
                self._on_arrival(w, t)
            return
        lim = self._effective_limit(o)
        if lim is None:
            return
        # (a) the opposite side has moved to/through our limit: that incoming order hits us AT OUR LIMIT
        best_opp = t.best_ask if o.side == Side.BUY else t.best_bid
        if best_opp is not None and ((o.side == Side.BUY and best_opp <= lim) or (o.side == Side.SELL and best_opp >= lim)):
            self._walk(w, t, lim, at_limit=True)
            if o.order_id not in self.working:
                return
        # (b) fills from traded volume, shared across all resting orders in time priority
        if budget[0] <= 0:
            return
        through = t.ltp < lim if o.side == Side.BUY else t.ltp > lim
        if through:
            q = min(o.remaining, budget[0])
            budget[0] -= q
            self._fill(w, q, q * lim, t.recv_ts, "trade-through")
        elif t.ltp == lim:
            # queue moves only by the observed decrease of displayed size at our price (not by volume printed elsewhere)
            decrease = max(0, self._displayed(prev, o.side, lim) - self._displayed(t, o.side, lim))
            reduction = min(budget[0], decrease)
            before = w.queue_ahead
            # displayed size now is an upper bound on what can still be ahead of us
            w.queue_ahead = min(max(0, before - reduction), self._displayed(t, o.side, lim))
            q = min(o.remaining, budget[0], max(0, reduction - before))
            if self._displayed(t, o.side, lim) == 0 and before <= budget[0]:
                # our level was fully consumed while printing at our price: what traded beyond the queue hit us
                q = min(o.remaining, budget[0] - before)
            if q > 0:
                budget[0] -= q
                self._fill(w, q, q * lim, t.recv_ts, "queue reached")

    def _fill(self, w: _W, qty: int, value: int, ts: datetime, reason: str) -> None:
        o = w.order
        qty = min(qty, o.remaining)
        if qty <= 0:
            return
        cum_q = o.filled_qty + qty
        cum_v = o.filled_value + value
        status = OrderStatus.FILLED if cum_q == o.qty else OrderStatus.PARTIALLY_FILLED
        self.stats["fills"] += 1
        if status == OrderStatus.FILLED:
            self.working.pop(o.order_id, None)
        self._emit(OrderUpdate(o.order_id, o.tag, status, cum_q, cum_v, ts, reason, triggered=o.triggered))

    def _finish(self, w: _W, status: OrderStatus, ts: datetime, reason: str) -> None:
        o = w.order
        self.working.pop(o.order_id, None)
        self._emit(OrderUpdate(o.order_id, o.tag, status, o.filled_qty, o.filled_value, ts, reason))
