"""Execution adapter interface (ADR-3). Only the LIVE adapter may talk to broker order endpoints."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol

from ..core.types import OrderStatus, OrderType
from ..oms.order import Order


@dataclass(frozen=True)
class OrderUpdate:
    order_id: str
    tag: str
    status: OrderStatus
    cum_qty: int
    cum_value: int          # paise
    ts: datetime
    reason: str = ""
    triggered: bool = False


class UpdateListener(Protocol):
    def on_order_update(self, upd: OrderUpdate) -> None: ...


class ExecutionAdapter(ABC):
    listener: Optional[UpdateListener] = None

    def set_listener(self, listener: UpdateListener) -> None:
        self.listener = listener

    @abstractmethod
    def place(self, order: Order) -> None: ...

    @abstractmethod
    def modify(self, order: Order, *, order_type: OrderType | None = None, price: int | None = None,
               trigger: int | None = None, qty: int | None = None) -> None: ...

    @abstractmethod
    def cancel(self, order: Order) -> None: ...

    def query(self, order: Order) -> Optional[OrderUpdate]:
        """Return the broker's current view of an order (by id/tag) or None if unknown to the broker."""
        return None

    def _emit(self, upd: OrderUpdate) -> None:
        if self.listener is not None:
            self.listener.on_order_update(upd)
