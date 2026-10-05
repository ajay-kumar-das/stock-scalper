"""Positions, fills and trade records with full cost attribution (FR-JRN-2)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional

from ..core.types import OrderRole, Reason, Side
from ..costs.upstox import UpstoxCostModel
from ..oms.order import Order


class PosState(str, Enum):
    OPENING = "OPENING"
    PROTECTED = "PROTECTED"
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    ABANDONED = "ABANDONED"   # entry never filled


@dataclass
class Position:
    position_id: str
    key: str
    symbol: str
    strategy: str
    strategy_version: str
    setup_id: str
    decision_ts: datetime
    decision_price: int           # reference price at decision (paise)
    initial_stop: int
    stop: int                     # current protective stop trigger
    target: int
    confidence: float
    expected_hold_min: int
    t_max_min: int
    regime: str
    evidence: dict[str, float]
    entry_reasons: list[Reason]
    planned_qty: int
    planned_risk_rupees: float
    planned_reward_rupees: float
    state: PosState = PosState.OPENING
    qty: int = 0
    entry_qty: int = 0
    entry_value: int = 0
    exit_qty: int = 0
    exit_value: int = 0
    opened_ts: Optional[datetime] = None
    closed_ts: Optional[datetime] = None
    high_water: int = 0
    low_water: int = 0
    order_ids: list[str] = field(default_factory=list)
    stop_order_id: str = ""
    exit_reasons: list[Reason] = field(default_factory=list)
    stop_moves: list[dict[str, Any]] = field(default_factory=list)
    exit_decision_price: int = 0
    exit_requested_ts: Optional[datetime] = None
    exit_escalations: int = 0
    stop_attempts: int = 0
    recovered: bool = False

    @property
    def avg_entry(self) -> float:
        return self.entry_value / self.entry_qty if self.entry_qty else 0.0

    @property
    def avg_exit(self) -> float:
        return self.exit_value / self.exit_qty if self.exit_qty else 0.0

    @property
    def risk_per_share(self) -> float:
        return max(1.0, self.avg_entry - self.initial_stop) if self.entry_qty else float(self.decision_price - self.initial_stop)

    def r_multiple(self, price: float) -> float:
        return (price - self.avg_entry) / self.risk_per_share

    @property
    def mfe_r(self) -> float:
        return self.r_multiple(self.high_water) if self.entry_qty else 0.0

    @property
    def mae_r(self) -> float:
        return self.r_multiple(self.low_water) if self.entry_qty else 0.0


class Portfolio:
    def __init__(self, cost_model: UpstoxCostModel, orders: Callable[[str], Order],
                 on_trade_closed: Optional[Callable[[dict], None]] = None,
                 on_entry_fill: Optional[Callable[[Position, Order, int], None]] = None,
                 on_event: Optional[Callable[[str, dict], None]] = None) -> None:
        self.cost_model = cost_model
        self._order = orders
        self.positions: dict[str, Position] = {}
        self.open_by_key: dict[str, Position] = {}
        self.trades: list[dict] = []
        self.on_trade_closed = on_trade_closed or (lambda t: None)
        self.on_entry_fill = on_entry_fill or (lambda p, o, q: None)
        self.on_event = on_event or (lambda k, p: None)
        self.realized_net_today = 0.0
        self.redirect: dict[str, str] = {}
        self.extra_costs: dict[str, float] = {}
        self.on_closing_terminal: Callable[[Position, Order], None] = lambda p, o: None
        self.on_flat_with_working_entry: Callable[[Position], None] = lambda p: None

    # ------------------------------------------------------------------ queries
    def position_qty(self, key: str) -> int:
        p = self.open_by_key.get(key)
        return p.qty if p else 0

    def open_positions(self) -> list[Position]:
        return [p for p in self.open_by_key.values() if p.state not in (PosState.CLOSED, PosState.ABANDONED)]

    # ------------------------------------------------------------------ lifecycle
    def open(self, pos: Position) -> None:
        if pos.key in self.open_by_key:
            raise RuntimeError(f"position already open for {pos.key}")
        self.positions[pos.position_id] = pos
        self.open_by_key[pos.key] = pos

    def _entry_working(self, pos: Position) -> bool:
        return any(self._order(oid).role == OrderRole.ENTRY and self._order(oid).is_working for oid in pos.order_ids)

    def _maybe_close(self, pos: Position, ts: datetime) -> None:
        """A position closes only when flat AND no entry order can still add to it (review finding 2)."""
        if pos.qty == 0 and pos.entry_qty > 0 and not self._entry_working(pos) and pos.state not in (
                PosState.CLOSED, PosState.ABANDONED):
            self._close(pos, ts)

    def on_fill(self, order: Order, qty: int, price: float, ts: datetime) -> None:
        pid = self.redirect.get(order.position_id, order.position_id)
        pos = self.positions.get(pid)
        if pos is None:
            self.on_event("anomaly", {"text": f"fill for unknown position {order.position_id}"})
            return
        px = int(round(price))
        if pos.state in (PosState.CLOSED, PosState.ABANDONED):
            pos = self._recover_late_fill(pos, order, ts)
        if order.side == Side.BUY:
            first = pos.entry_qty == 0
            pos.qty += qty
            pos.entry_qty += qty
            pos.entry_value += qty * px
            if first:
                pos.opened_ts = ts
                pos.high_water = pos.low_water = px
            self.on_event("fill", {"position_id": pos.position_id, "side": "BUY", "qty": qty, "price": px})
            self.on_entry_fill(pos, order, qty)
        else:
            pos.qty -= qty
            pos.exit_qty += qty
            pos.exit_value += qty * px
            if pos.qty < 0:
                self.on_event("anomaly", {"text": f"NEGATIVE POSITION {pos.key} {pos.qty}", "severity": 1})
            self.on_event("fill", {"position_id": pos.position_id, "side": "SELL", "qty": qty, "price": px})
            if pos.qty == 0 and self._entry_working(pos):
                # protective stop filled while the entry remainder still works: the thesis failed -> cancel it
                self.on_flat_with_working_entry(pos)
            self._maybe_close(pos, ts)

    def _recover_late_fill(self, pos: Position, order: Order, ts: datetime) -> Position:
        """F29: a fill arrived for a position already CLOSED/ABANDONED. Track it (never drop shares),
        re-register it so OMS/risk see the exposure, and let the manager exit it."""
        self.on_event("anomaly", {"text": f"LATE_FILL for {pos.state.value} position {pos.position_id} ({pos.key})"})
        if pos.state == PosState.ABANDONED:
            pos.state = PosState.OPENING
            self.open_by_key[pos.key] = pos
            pos.recovered = True
            return pos
        rec = Position(position_id=f"{pos.position_id}-R", key=pos.key, symbol=pos.symbol, strategy=pos.strategy,
                       strategy_version=pos.strategy_version, setup_id=pos.setup_id + ":recovery", decision_ts=ts,
                       decision_price=pos.decision_price, initial_stop=pos.initial_stop, stop=pos.initial_stop,
                       target=pos.target, confidence=0.0, expected_hold_min=0, t_max_min=0, regime=pos.regime,
                       evidence={}, entry_reasons=[Reason("LATE_FILL_RECOVERY", "orphan quantity from a late fill")],
                       planned_qty=0, planned_risk_rupees=0.0, planned_reward_rupees=0.0)
        rec.recovered = True
        rec.order_ids.append(order.order_id)
        self.positions[rec.position_id] = rec
        self.open_by_key[rec.key] = rec
        self.redirect[pos.position_id] = rec.position_id
        return rec

    def on_order_terminal(self, order: Order) -> None:
        pos = self.positions.get(self.redirect.get(order.position_id, order.position_id))
        if pos is None:
            return
        ts = order.history[-1][0] if order.history else pos.decision_ts
        if order.role == OrderRole.ENTRY:
            if pos.state == PosState.OPENING and pos.entry_qty == 0:
                pos.state = PosState.ABANDONED
                self.open_by_key.pop(pos.key, None)
                self.on_event("position_abandoned", {"position_id": pos.position_id, "key": pos.key})
                return
            if pos.state == PosState.OPENING and pos.qty > 0:
                pos.state = PosState.PROTECTED
            self._maybe_close(pos, ts)
        elif pos.state not in (PosState.CLOSED, PosState.ABANDONED) and pos.qty > 0:
            # a closing order (stop/exit) ended while we still hold shares: protection lost (review finding 1)
            self.on_closing_terminal(pos, order)

    def mark(self, key: str, high: int, low: int) -> None:
        p = self.open_by_key.get(key)
        if p and p.entry_qty:
            p.high_water = max(p.high_water, high)
            p.low_water = min(p.low_water, low)

    # ------------------------------------------------------------------ closing
    def order_costs(self, pos: Position) -> tuple[float, float]:
        """(entry-side costs, exit-side costs) in rupees; brokerage per executed order."""
        entry_c = exit_c = 0.0
        for oid in pos.order_ids:
            o = self._order(oid)
            if o.filled_qty == 0:
                continue
            c = self.cost_model.order_charges(o.side, o.filled_value / 100.0).total
            if o.side == Side.BUY:
                entry_c += c
            else:
                exit_c += c
        return entry_c, exit_c

    def _close(self, pos: Position, ts: datetime) -> None:
        pos.state = PosState.CLOSED
        pos.closed_ts = ts
        self.open_by_key.pop(pos.key, None)
        entry_c, exit_c = self.order_costs(pos)
        gross = (pos.exit_value - pos.entry_value) / 100.0
        costs = entry_c + exit_c + self.extra_costs.get(pos.position_id, 0.0)
        net = gross - costs
        risk_rupees = pos.risk_per_share * pos.entry_qty / 100.0
        entry_slip = pos.avg_entry - pos.decision_price
        exit_ref = pos.exit_decision_price or pos.stop
        exit_slip = exit_ref - pos.avg_exit if exit_ref else 0.0
        hold_s = (ts - pos.opened_ts).total_seconds() if pos.opened_ts else 0.0
        self.realized_net_today += net
        trade = {
            "trade_id": pos.position_id, "key": pos.key, "symbol": pos.symbol,
            "strategy": pos.strategy, "strategy_version": pos.strategy_version, "setup_id": pos.setup_id,
            "regime": pos.regime, "decision_ts": pos.decision_ts, "entry_ts": pos.opened_ts, "exit_ts": ts,
            "qty": pos.entry_qty, "entry_price": pos.avg_entry / 100.0, "exit_price": pos.avg_exit / 100.0,
            "initial_stop": pos.initial_stop / 100.0, "final_stop": pos.stop / 100.0, "target": pos.target / 100.0,
            "planned_risk": pos.planned_risk_rupees, "planned_reward": pos.planned_reward_rupees,
            "risk_rupees": risk_rupees, "confidence": pos.confidence,
            "gross_pnl": round(gross, 2), "costs": round(costs, 2), "net_pnl": round(net, 2),
            "entry_costs": round(entry_c, 2), "exit_costs": round(exit_c, 2),
            "r_gross": gross / risk_rupees if risk_rupees else 0.0,
            "r_net": net / risk_rupees if risk_rupees else 0.0,
            "mfe_r": pos.mfe_r, "mae_r": pos.mae_r,
            "entry_slippage_bps": entry_slip / pos.decision_price * 1e4 if pos.decision_price else 0.0,
            "exit_slippage_bps": exit_slip / exit_ref * 1e4 if exit_ref else 0.0,
            "slippage_rupees": round((entry_slip * pos.entry_qty + exit_slip * pos.exit_qty) / 100.0, 2),
            "holding_s": hold_s, "evidence": pos.evidence,
            "entry_reasons": [r.as_dict() for r in pos.entry_reasons],
            "exit_reasons": [r.as_dict() for r in pos.exit_reasons],
            "stop_moves": pos.stop_moves, "n_orders": sum(1 for oid in pos.order_ids if self._order(oid).filled_qty),
        }
        self.trades.append(trade)
        self.on_event("trade_closed", {k: trade[k] for k in ("trade_id", "key", "strategy", "net_pnl", "r_net")})
        self.on_trade_closed(trade)

    def new_day(self) -> None:
        self.realized_net_today = 0.0
