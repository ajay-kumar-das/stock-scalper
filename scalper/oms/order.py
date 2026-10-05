"""Order with an explicit lifecycle state machine (doc 03 §3, FR-OMS-1)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..core.types import (
    TERMINAL_STATUSES,
    WORKING_STATUSES,
    OrderRole,
    OrderStatus,
    OrderType,
    Side,
)

S = OrderStatus
ALLOWED: dict[OrderStatus, set[OrderStatus]] = {
    S.INTENDED: {S.SUBMITTED, S.CANCELLED},
    S.SUBMITTED: {S.ACKNOWLEDGED, S.REJECTED, S.UNKNOWN, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED},
    S.ACKNOWLEDGED: {S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED, S.UNKNOWN},
    S.PARTIALLY_FILLED: {S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.EXPIRED, S.UNKNOWN},
    # UNKNOWN resolves to whatever reconciliation finds
    S.UNKNOWN: {S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED},
    S.FILLED: set(),
    S.CANCELLED: set(),
    S.REJECTED: set(),
    S.EXPIRED: set(),
}


class OrderStateError(RuntimeError):
    pass


@dataclass
class Order:
    order_id: str                 # internal id
    tag: str                      # client tag / idempotency key (<= 40 chars, Upstox limit)
    key: str
    side: Side
    role: OrderRole
    order_type: OrderType
    qty: int
    price: int                    # limit price (paise); for SL = limit after trigger
    trigger: int = 0              # trigger price for SL / SL-M (paise)
    strategy: str = ""
    position_id: str = ""
    created_ts: Optional[datetime] = None
    status: OrderStatus = OrderStatus.INTENDED
    broker_order_id: str = ""
    filled_qty: int = 0
    filled_value: int = 0         # sum(qty * price) in paise, for avg price
    triggered: bool = False
    pending_modify: bool = False
    reject_reason: str = ""
    history: list[tuple[datetime, str, str]] = field(default_factory=list)
    n_modifications: int = 0
    pending_prev: Optional[tuple] = None

    def __post_init__(self) -> None:
        if len(self.tag) > 40:
            raise ValueError("Upstox tag must be <= 40 chars")
        if self.qty <= 0:
            raise ValueError("qty must be positive")

    # --- derived -----------------------------------------------------------------
    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_working(self) -> bool:
        return self.status in WORKING_STATUSES

    @property
    def remaining(self) -> int:
        return self.qty - self.filled_qty

    @property
    def avg_price(self) -> float:
        return self.filled_value / self.filled_qty if self.filled_qty else 0.0

    # --- transitions -------------------------------------------------------------
    def transition(self, new: OrderStatus, ts: datetime, note: str = "") -> None:
        if new not in ALLOWED[self.status]:
            raise OrderStateError(f"{self.order_id}: illegal {self.status.value} -> {new.value} ({note})")
        self.history.append((ts, f"{self.status.value}->{new.value}", note))
        self.status = new

    def apply_cumulative_fill(self, cum_qty: int, cum_value: int, ts: datetime) -> int:
        """Apply broker-reported *cumulative* fill. Idempotent; returns newly filled qty.

        filled_qty must never decrease (FR / invariant 4); a decrease is an anomaly.
        """
        if cum_qty < self.filled_qty:
            raise OrderStateError(f"{self.order_id}: filled qty decreased {self.filled_qty} -> {cum_qty}")
        if cum_qty > self.qty:
            raise OrderStateError(f"{self.order_id}: overfill {cum_qty} > {self.qty}")
        delta = cum_qty - self.filled_qty
        if delta == 0:
            return 0
        self.filled_qty = cum_qty
        self.filled_value = cum_value
        if self.is_terminal:
            # late fill after cancel/expiry (F29): keep the quantity, keep the terminal status
            self.history.append((ts, f"LATE_FILL in {self.status.value}", f"cum={cum_qty}"))
            return delta
        new_status = OrderStatus.FILLED if cum_qty == self.qty else OrderStatus.PARTIALLY_FILLED
        if self.status == OrderStatus.SUBMITTED:
            # update-before-ack race (doc 03 §4): an ack is implied by a fill
            self.history.append((ts, "SUBMITTED->ACKNOWLEDGED", "implied by fill"))
            self.status = OrderStatus.ACKNOWLEDGED
        self.transition(new_status, ts, f"cum={cum_qty}")
        return delta
