"""Order management system.

Responsibilities (FR-OMS): lifecycle tracking, idempotent tags, duplicate prevention,
throttling, exit-by-modify, invariant enforcement. Strategies never reach this module
directly; only the decision pipeline and position manager do.
"""
from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional, Protocol

from ..core.clock import Clock
from ..core.types import OrderRole, OrderStatus, OrderType, Side
from ..execution.base import ExecutionAdapter, OrderUpdate
from .order import Order, OrderStateError


class FillListener(Protocol):
    def on_fill(self, order: Order, qty: int, price: float, ts: datetime) -> None: ...
    def on_order_terminal(self, order: Order) -> None: ...


class InvariantViolation(RuntimeError):
    pass


@dataclass
class ThrottleConfig:
    per_second: int = 3          # entries
    per_minute: int = 120
    hard_per_second: int = 8     # protective actions (stops, exits) may use headroom up to this; SEBI line is 10/s


class Throttle:
    """Sliding-window order-rate limiter (FR-OMS-6), independent of broker limits."""

    def __init__(self, clock: Clock, cfg: ThrottleConfig) -> None:
        self.clock, self.cfg = clock, cfg
        self.sent: deque[datetime] = deque()

    def allow(self, priority: bool = False) -> bool:
        """Entries are limited to per_second; protective actions (priority) are never starved by
        entries and may use headroom up to hard_per_second (F17: exits before entries)."""
        now = self.clock.now()
        while self.sent and now - self.sent[0] > timedelta(minutes=1):
            self.sent.popleft()
        last_sec = sum(1 for t in self.sent if now - t < timedelta(seconds=1))
        if priority:
            if last_sec >= self.cfg.hard_per_second:
                return False
        elif last_sec >= self.cfg.per_second or len(self.sent) >= self.cfg.per_minute:
            return False
        self.sent.append(now)
        return True


class OMS:
    def __init__(self, clock: Clock, execution: ExecutionAdapter, position_qty: Callable[[str], int],
                 throttle: Optional[Throttle] = None, run_tag: str = "sc",
                 on_event: Optional[Callable[[str, dict], None]] = None,
                 on_anomaly: Optional[Callable[[str], None]] = None) -> None:
        self.clock = clock
        self.execution = execution
        self.position_qty = position_qty
        self.throttle = throttle or Throttle(clock, ThrottleConfig())
        self.run_tag = run_tag
        self.orders: dict[str, Order] = {}
        self.by_tag: dict[str, Order] = {}
        self._seq = itertools.count(1)
        self.fill_listener: Optional[FillListener] = None
        self.on_event = on_event or (lambda kind, payload: None)
        self.on_anomaly = on_anomaly or (lambda msg: None)
        self.on_modify_rejected: Callable[[Order], None] = lambda o: None
        self._terminal_notified: set[str] = set()
        self.possibly_filled: set[str] = set()
        execution.set_listener(self)

    # ------------------------------------------------------------------ queries
    def working(self, key: str | None = None, role: OrderRole | None = None, side: Side | None = None) -> list[Order]:
        return [o for o in self.orders.values()
                if o.is_working and (key is None or o.key == key) and (role is None or o.role == role)
                and (side is None or o.side == side)]

    def has_unknown(self, key: str | None = None) -> bool:
        return any(o.status == OrderStatus.UNKNOWN and (key is None or o.key == key) for o in self.orders.values())

    def working_closing_qty(self, key: str) -> int:
        return sum(o.remaining for o in self.working(key, side=Side.SELL))

    # ------------------------------------------------------------------ creation
    def _new(self, key: str, side: Side, role: OrderRole, otype: OrderType, qty: int, price: int,
             trigger: int = 0, strategy: str = "", position_id: str = "") -> Order:
        seq = next(self._seq)
        date = self.clock.now().strftime("%y%m%d")
        tag = f"{self.run_tag}-{date}-{seq}-{role.value[0]}"[:40]
        oid = f"o{seq}"
        o = Order(order_id=oid, tag=tag, key=key, side=side, role=role, order_type=otype, qty=qty,
                  price=price, trigger=trigger, strategy=strategy, position_id=position_id,
                  created_ts=self.clock.now())
        self.orders[oid] = o
        self.by_tag[tag] = o
        return o

    def _send(self, o: Order) -> bool:
        if not self.throttle.allow(priority=o.side == Side.SELL):
            o.transition(OrderStatus.CANCELLED, self.clock.now(), "throttled (never sent)")
            self.on_event("order_throttled", {"order_id": o.order_id, "key": o.key, "role": o.role.value})
            return False
        o.transition(OrderStatus.SUBMITTED, self.clock.now(), "sent")
        self.on_event("order_submitted", self._order_payload(o))
        self.execution.place(o)
        return True

    def submit_entry(self, key: str, qty: int, limit_price: int, strategy: str, position_id: str) -> Optional[Order]:
        # Invariant 1: <= 1 working entry and no open position per instrument
        if self.working(key, role=OrderRole.ENTRY):
            raise InvariantViolation(f"duplicate entry order for {key}")
        if self.position_qty(key) != 0:
            raise InvariantViolation(f"entry while position open for {key}")
        if self.has_unknown(key):
            raise InvariantViolation(f"entry while order state UNKNOWN for {key}")
        if key in self.possibly_filled:
            raise InvariantViolation(f"entry while {key} may hold an unreconciled fill")
        o = self._new(key, Side.BUY, OrderRole.ENTRY, OrderType.LIMIT, qty, limit_price, 0, strategy, position_id)
        return o if self._send(o) else None

    def place_stop(self, key: str, qty: int, trigger: int, limit: int, strategy: str, position_id: str) -> Optional[Order]:
        self._check_closing_capacity(key, qty)
        o = self._new(key, Side.SELL, OrderRole.STOP, OrderType.SL, qty, limit, trigger, strategy, position_id)
        return o if self._send(o) else None

    def place_exit(self, key: str, qty: int, limit: int, strategy: str, position_id: str) -> Optional[Order]:
        """Only used when no protective stop is working (e.g. stop placement failed)."""
        if self.working(key, role=OrderRole.STOP):
            raise InvariantViolation("use exit-by-modify while a protective stop is working")
        self._check_closing_capacity(key, qty)
        o = self._new(key, Side.SELL, OrderRole.EXIT, OrderType.LIMIT, qty, limit, 0, strategy, position_id)
        return o if self._send(o) else None

    def _check_closing_capacity(self, key: str, qty: int) -> None:
        # Invariant 2: working closing qty <= |position|
        if self.working_closing_qty(key) + qty > self.position_qty(key):
            raise InvariantViolation(
                f"closing qty {self.working_closing_qty(key)}+{qty} exceeds position {self.position_qty(key)} for {key}")

    # ------------------------------------------------------------------ modification
    def modify(self, o: Order, *, order_type: OrderType | None = None, price: int | None = None,
               trigger: int | None = None, qty: int | None = None, note: str = "") -> bool:
        if not o.is_working or o.status == OrderStatus.UNKNOWN:
            return False
        if qty is not None and o.side == Side.SELL:
            other = self.working_closing_qty(o.key) - o.remaining
            if other + (qty - o.filled_qty) > self.position_qty(o.key):
                raise InvariantViolation("modify would exceed position")
        if not self.throttle.allow(priority=o.side == Side.SELL):
            self.on_event("modify_throttled", {"order_id": o.order_id})
            self.on_anomaly(f"modify throttled for {o.order_id} ({note})")
            return False
        o.pending_prev = (o.order_type, o.price, o.trigger, o.qty)
        o.pending_modify = True
        if order_type is not None:
            o.order_type = order_type
        if price is not None:
            o.price = price
        if trigger is not None:
            o.trigger = trigger
        if qty is not None:
            o.qty = qty
        o.n_modifications += 1
        o.history.append((self.clock.now(), "MODIFY", note))
        self.on_event("order_modified", {**self._order_payload(o), "note": note})
        self.execution.modify(o, order_type=order_type, price=price, trigger=trigger, qty=qty)
        return True

    def exit_by_modify(self, stop: Order, limit_price: int, note: str) -> bool:
        """Convert the protective stop into a marketable limit (doc 03 §4)."""
        return self.modify(stop, order_type=OrderType.LIMIT, price=limit_price, note=f"EXIT: {note}")

    def cancel(self, o: Order, note: str = "") -> None:
        if o.is_working and o.status != OrderStatus.UNKNOWN:
            o.history.append((self.clock.now(), "CANCEL_REQ", note))
            self.execution.cancel(o)

    # ------------------------------------------------------------------ broker updates
    def on_order_update(self, upd: OrderUpdate) -> None:
        o = self.orders.get(upd.order_id) or self.by_tag.get(upd.tag)
        if o is None:
            self.on_anomaly(f"update for unknown order {upd.order_id}/{upd.tag}")
            return
        prev_value = o.filled_value
        try:
            delta = o.apply_cumulative_fill(upd.cum_qty, upd.cum_value, upd.ts)
        except OrderStateError as e:
            self.on_anomaly(str(e))
            return
        if upd.triggered:
            o.triggered = True
        if delta == 0 and o.status == OrderStatus.UNKNOWN and upd.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            o.transition(upd.status, upd.ts, "resolved by reconciliation")
        if upd.reason == "MODIFY_REJECTED" and o.pending_modify and o.pending_prev:
            o.order_type, o.price, o.trigger, o.qty = o.pending_prev
            o.pending_modify = False
            self.on_anomaly(f"modify rejected for {o.order_id}; local state reverted to broker state")
            self._safe(self.on_modify_rejected, o)
        elif upd.reason == "MODIFY_ACK":
            o.pending_modify = False
        if delta and self.fill_listener:
            price = (upd.cum_value - prev_value) / delta
            self._safe(self.fill_listener.on_fill, o, delta, price, upd.ts)
        if upd.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and upd.status != o.status:
            try:
                o.transition(upd.status, upd.ts, upd.reason)
            except OrderStateError as e:
                self.on_anomaly(str(e))
                return
            if upd.status == OrderStatus.REJECTED:
                o.reject_reason = upd.reason
        self.on_event("order_update", {**self._order_payload(o), "reason": upd.reason})
        if o.is_terminal and self.fill_listener and o.order_id not in self._terminal_notified:
            self._terminal_notified.add(o.order_id)
            self._safe(self.fill_listener.on_order_terminal, o)

    def _safe(self, fn, *args) -> None:
        """Never let a listener exception escape into the broker adapter (review finding 2)."""
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001 - deliberate: convert to a defensive anomaly
            self.on_anomaly(f"LISTENER_ERROR {type(e).__name__}: {e}")

    # ------------------------------------------------------------------ uncertainty handling (FR-OMS-5)
    def check_ack_timeouts(self, timeout: timedelta = timedelta(seconds=2)) -> list[Order]:
        now = self.clock.now()
        late = [o for o in self.working() if o.status == OrderStatus.SUBMITTED and o.created_ts
                and now - (o.history[-1][0] if o.history else o.created_ts) >= timeout]
        for o in late:
            o.transition(OrderStatus.UNKNOWN, now, f"no ack within {timeout.total_seconds():.0f}s")
            self.on_anomaly(f"order {o.order_id} state UNKNOWN (no ack)")
        return late

    def reconcile_unknown(self, max_attempts: int = 3, backoff_s: tuple = (1.0, 2.0, 4.0)) -> None:
        """Ask the broker for the truth about UNKNOWN orders, with attempts spaced in time (1 s, 2 s, 4 s).
        If still not found: send a cancel by tag, mark it CANCELLED-assumed, and put the instrument in
        `possibly_filled`, which blocks new entries there until a position reconciliation clears it."""
        now = self.clock.now()
        for o in [o for o in self.working() if o.status == OrderStatus.UNKNOWN]:
            nxt = getattr(o, "recon_next", None)
            if nxt is not None and now < nxt:
                continue
            o.recon_attempts = getattr(o, "recon_attempts", 0) + 1
            truth = self.execution.query(o)
            if truth is not None:
                self.on_order_update(truth)
                continue
            if o.recon_attempts >= max_attempts:
                self.execution.cancel(o)          # defensive: make sure it cannot fill later
                o.transition(OrderStatus.CANCELLED, now, "not found at broker: CANCELLED-assumed")
                self.possibly_filled.add(o.key)
                self.on_anomaly(f"order {o.order_id} not found after {max_attempts} reconciliations; assumed "
                                f"cancelled; {o.key} blocked until position reconciliation")
                if self.fill_listener:
                    self._safe(self.fill_listener.on_order_terminal, o)
            else:
                from datetime import timedelta as _td
                o.recon_next = now + _td(seconds=backoff_s[min(o.recon_attempts - 1, len(backoff_s) - 1)])

    def clear_possibly_filled(self, key: str) -> None:
        """Call only after broker positions for `key` have been reconciled with local state."""
        self.possibly_filled.discard(key)

    # ------------------------------------------------------------------ housekeeping
    def expire_stale_entries(self, ttl: timedelta) -> list[Order]:
        """Entry TTL enforced in every mode, not only by the simulator (review finding 6)."""
        now = self.clock.now()
        stale = [o for o in self.working(role=OrderRole.ENTRY)
                 if o.status != OrderStatus.UNKNOWN and o.created_ts and now - o.created_ts >= ttl]
        for o in stale:
            self.cancel(o, "entry TTL")
        return stale

    def force_square_off(self, key: str, qty: int, price: int, strategy: str, position_id: str, ts) -> Order:
        """SIMULATION ONLY: model the broker's auto square-off of a residual position."""
        o = self._new(key, Side.SELL, OrderRole.EXIT, OrderType.LIMIT, qty, price, 0, strategy, position_id)
        o.transition(OrderStatus.SUBMITTED, ts, "broker auto square-off (simulated)")
        o.transition(OrderStatus.ACKNOWLEDGED, ts, "auto square-off")
        self.on_order_update(OrderUpdate(o.order_id, o.tag, OrderStatus.FILLED, qty, qty * price, ts, "AUTO_SQUARE_OFF"))
        return o

    @staticmethod
    def _order_payload(o: Order) -> dict:
        return {"order_id": o.order_id, "tag": o.tag, "key": o.key, "side": o.side.value, "role": o.role.value,
                "type": o.order_type.value, "qty": o.qty, "price": o.price, "trigger": o.trigger,
                "status": o.status.value, "filled": o.filled_qty, "strategy": o.strategy,
                "position_id": o.position_id}
