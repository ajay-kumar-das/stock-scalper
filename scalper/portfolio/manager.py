"""Position manager (doc 07 §8, doc 03 §4).

Owns the protective-stop protocol:
  * a broker-resident SL for the filled qty right after the first fill;
  * stops only tighten;
  * exits are done by modifying the stop into a marketable limit (exit-by-modify), so the
    working sell quantity can never exceed the position (no accidental short).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

from ..core.prices import round_down_to_tick
from ..core.types import Bar, Instrument, OrderRole, OrderStatus, OrderType, Reason
from ..features.intraday import FeatureState
from ..oms.oms import OMS
from ..oms.order import Order
from ..strategies.base import Strategy, StrategyContext
from .portfolio import Portfolio, PosState, Position


@dataclass
class ManagerConfig:
    stop_limit_buffer_pct: float = 0.003     # SL limit = trigger - max(0.3%, 3 ticks)
    stop_limit_buffer_ticks: int = 3
    exit_aggressive_pct: float = 0.003       # escalated exit: bid - 0.3%
    breakeven_r: float = 1.0
    lock_r: float = 1.5
    lock_to_r: float = 0.5
    trail_atr: float = 2.0
    exceptional_r: float = 3.0
    exceptional_trail_atr: float = 3.0
    time_decay_mfe_r: float = 0.5
    market_shock_move: float = -0.004
    max_exit_escalations: int = 3


class PositionManager:
    def __init__(self, cfg: ManagerConfig, oms: OMS, portfolio: Portfolio, instruments: dict[str, Instrument],
                 strategies: dict[str, Strategy], half_spread: Callable[[str, int], int],
                 on_event: Callable[[str, dict], None], on_anomaly: Callable[[str], None]) -> None:
        self.cfg, self.oms, self.portfolio = cfg, oms, portfolio
        self.instruments, self.strategies, self.half_spread = instruments, strategies, half_spread
        self.on_event, self.on_anomaly = on_event, on_anomaly
        self.extended_tmax: set[str] = set()
        self._placing: set[str] = set()
        self.suspended = False      # set during simulated EOD square-off so protection logic doesn't fight it
        self.last_price: dict[str, int] = {}

    # ------------------------------------------------------------------ helpers
    def _stop_limit(self, key: str, trigger: int) -> int:
        tick = self.instruments[key].tick
        buf = max(int(trigger * self.cfg.stop_limit_buffer_pct), self.cfg.stop_limit_buffer_ticks * tick)
        return max(tick, round_down_to_tick(trigger - buf, tick))

    def stop_order(self, pos: Position) -> Optional[Order]:
        if not pos.stop_order_id:
            return None
        o = self.oms.orders.get(pos.stop_order_id)
        return o if o and not o.is_terminal else None

    # ------------------------------------------------------------------ entry fills
    def on_entry_fill(self, pos: Position, order: Order, qty: int) -> None:
        if pos.state == PosState.EXITING:
            # late entry fill while exiting: grow the working closing order to cover the position
            closing = [o for o in self.oms.working(pos.key) if o.side.value == "SELL"]
            if closing:
                self.oms.modify(closing[0], qty=closing[0].filled_qty + pos.qty, note="late entry fill while exiting")
            else:
                o = self.oms.place_exit(pos.key, pos.qty, self.last_price.get(pos.key, pos.stop), pos.strategy, pos.position_id)
                if o is not None:
                    pos.order_ids.append(o.order_id)
            return
        if pos.recovered:
            self.request_exit(pos, Reason("LATE_FILL_RECOVERY", "late fill on a closed/abandoned position"),
                              int(round(order.avg_price)), aggressive=True)
            return
        stop = self.stop_order(pos)
        if stop is not None:
            # resize protective stop to the full position qty
            self.oms.modify(stop, qty=stop.filled_qty + pos.qty, note="resize stop to position qty")
            return
        ref = int(round(order.avg_price))
        if ref <= pos.stop:
            # price already through the stop: an SL would be rejected -> exit immediately (doc 03 §4)
            self.request_exit(pos, Reason("STOP_ALREADY_THROUGH", f"fill {ref/100:.2f} <= stop {pos.stop/100:.2f}"), ref,
                              aggressive=True)
            return
        self._place_stop(pos)

    def _place_stop(self, pos: Position, attempt: int = 0) -> None:
        if self.suspended or pos.qty <= 0:
            return
        pos.stop_attempts += 1
        qty = pos.qty - self.oms.working_closing_qty(pos.key)
        if qty <= 0:
            return
        self._placing.add(pos.position_id)
        try:
            o = self.oms.place_stop(pos.key, qty, pos.stop, self._stop_limit(pos.key, pos.stop), pos.strategy,
                                    pos.position_id)
        finally:
            self._placing.discard(pos.position_id)
        if o is not None:
            pos.order_ids.append(o.order_id)
        if o is None or o.status == OrderStatus.REJECTED:
            if pos.stop_attempts < 2:
                self._place_stop(pos)
            else:
                self.on_anomaly(f"protective stop failed twice for {pos.key}; exiting")
                self.request_exit(pos, Reason("STOP_PLACEMENT_FAILED", "protective stop rejected twice"),
                                  self.last_price.get(pos.key, pos.stop), aggressive=True)
            return
        pos.stop_order_id = o.order_id
        if pos.state == PosState.OPENING:
            pos.state = PosState.PROTECTED

    # ------------------------------------------------------------------ protection lost / reconciled
    def on_closing_terminal(self, pos: Position, order: Order) -> None:
        """A stop/exit order ended (rejected, cancelled, expired or filled short of the position) while
        shares remain. Re-establish protection or exit (review finding 1, invariant 3)."""
        if self.suspended or pos.position_id in self._placing:
            return      # synchronous rejections during placement are handled by _place_stop itself
        if self.oms.working_closing_qty(pos.key) >= pos.qty:
            return
        if pos.state == PosState.EXITING:
            self.on_anomaly(f"exit order {order.order_id} ended ({order.status.value}) with {pos.qty} left; re-placing exit")
            self.request_exit(pos, Reason("EXIT_REPLACE", "exit order ended unfilled"),
                              self.last_price.get(pos.key, pos.stop), aggressive=True)
            return
        self.on_anomaly(f"protective {order.role.value} {order.order_id} ended ({order.status.value}: "
                        f"{order.reject_reason}) with {pos.qty} sh open; re-protecting")
        if pos.stop_attempts < 3:
            self._place_stop(pos)
        else:
            self.request_exit(pos, Reason("STOP_LOST", "protective stop could not be maintained"),
                              self.last_price.get(pos.key, pos.stop), aggressive=True)

    def on_flat_with_working_entry(self, pos: Position) -> None:
        for o in self.oms.working(pos.key, role=OrderRole.ENTRY):
            self.oms.cancel(o, "stop filled: thesis failed, cancel entry remainder")

    def on_modify_rejected(self, o: Order) -> None:
        pos = self.portfolio.positions.get(o.position_id)
        if pos and o.role == OrderRole.STOP and o.order_type in (OrderType.SL, OrderType.SL_M):
            pos.stop = o.trigger          # keep local view equal to what the broker actually holds

    # ------------------------------------------------------------------ exits
    def request_exit(self, pos: Position, reason: Reason, ref_price: int, aggressive: bool = False) -> None:
        for o in self.oms.working(pos.key, role=OrderRole.ENTRY):
            self.oms.cancel(o, f"exit requested: {reason.code}")     # also when nothing has filled yet
        if pos.state in (PosState.CLOSED, PosState.ABANDONED) or pos.qty <= 0:
            return
        tick = self.instruments[pos.key].tick
        hs = self.half_spread(pos.key, ref_price)
        bid = ref_price - hs
        limit = bid - int(bid * self.cfg.exit_aggressive_pct) if aggressive else bid
        limit = max(tick, round_down_to_tick(limit, tick))
        first = pos.state != PosState.EXITING
        if first:
            pos.exit_reasons.append(reason)
            pos.exit_decision_price = ref_price
            pos.exit_requested_ts = self.oms.clock.now()
        pos.state = PosState.EXITING
        stop = self.stop_order(pos)
        if stop is not None:
            if stop.status == OrderStatus.UNKNOWN:
                self.on_anomaly(f"cannot exit {pos.key}: protective stop {stop.order_id} state UNKNOWN; reconcile")
            elif not self.oms.exit_by_modify(stop, limit, reason.code):
                self.on_anomaly(f"exit modify not sent for {pos.key}; will retry next bar")
        else:
            existing = [o for o in self.oms.working(pos.key) if o.role == OrderRole.EXIT]
            if existing:
                self.oms.modify(existing[0], price=limit, note=f"re-price exit: {reason.code}")
            elif pos.qty > 0:
                o = self.oms.place_exit(pos.key, pos.qty, limit, pos.strategy, pos.position_id)
                if o is not None:
                    pos.order_ids.append(o.order_id)
        self.on_event("exit_requested", {"position_id": pos.position_id, "key": pos.key, "reason": reason.as_dict(),
                                         "limit": limit, "aggressive": aggressive})

    def move_stop(self, pos: Position, new_stop: int, reason: Reason, price_now: int) -> None:
        tick = self.instruments[pos.key].tick
        new_stop = round_down_to_tick(new_stop, tick)
        if new_stop <= pos.stop:
            return     # stops only tighten (FR-POS-4)
        if new_stop >= price_now - tick:
            self.request_exit(pos, Reason("STOP_ABOVE_MARKET", f"{reason.code}: new stop {new_stop/100:.2f} at/above market"),
                              price_now)
            return
        stop = self.stop_order(pos)
        if stop is None or stop.triggered:
            return
        old = pos.stop
        if self.oms.modify(stop, trigger=new_stop, price=self._stop_limit(pos.key, new_stop), note=reason.code):
            pos.stop = new_stop
            pos.stop_moves.append({"ts": str(self.oms.clock.now()), "from": old / 100, "to": new_stop / 100,
                                   "reason": reason.as_dict()})
            self.on_event("stop_moved", {"position_id": pos.position_id, "from": old, "to": new_stop,
                                         "reason": reason.as_dict()})

    # ------------------------------------------------------------------ per-bar management
    def manage(self, pos: Position, bar: Bar, f: FeatureState, ctx: StrategyContext, now: datetime,
               force_exit: Optional[Reason] = None) -> None:
        self.last_price[pos.key] = bar.c
        if pos.state in (PosState.CLOSED, PosState.ABANDONED):
            return
        if pos.entry_qty == 0:
            if force_exit is not None:
                for o in self.oms.working(pos.key, role=OrderRole.ENTRY):
                    self.oms.cancel(o, f"{force_exit.code}: cancel unfilled entry")
            return
        # invariant 3: an open position always has protection covering its full quantity
        if pos.state in (PosState.PROTECTED, PosState.OPENING) and pos.qty > 0:
            stop_o = self.stop_order(pos)
            if stop_o is None and self.oms.working_closing_qty(pos.key) == 0 and pos.stop_attempts < 3:
                self.on_anomaly(f"{pos.key} open without protective stop; re-placing")
                self._place_stop(pos)
            elif stop_o is not None and not stop_o.pending_modify and stop_o.remaining < pos.qty:
                self.oms.modify(stop_o, qty=stop_o.filled_qty + pos.qty, note="resize stop to cover position")
        self.portfolio.mark(pos.key, bar.h, bar.l)
        inst = self.instruments[pos.key]
        c = self.cfg
        if pos.state == PosState.EXITING:
            self._escalate(pos, bar)
            return
        if force_exit is not None:
            self.request_exit(pos, force_exit, bar.c, aggressive=force_exit.code in ("FLATTEN_DEADLINE", "EMERGENCY", "RISK_HALT"))
            return
        strat = self.strategies[pos.strategy]
        stop_o = self.stop_order(pos)
        if stop_o is not None and stop_o.triggered:
            # broker stop triggered but resting (gapped through limit): escalate
            self.request_exit(pos, Reason("STOP_GAPPED", "stop-limit triggered but not filled"), bar.c, aggressive=True)
            return
        r_ps = pos.risk_per_share
        mfe_r = pos.mfe_r
        elapsed = (now - pos.opened_ts) if pos.opened_ts else timedelta(0)
        t_max = pos.t_max_min * (1.5 if pos.position_id in self.extended_tmax else 1.0)

        # 3 thesis invalidation
        adv = strat.thesis_check(inst, bar, f, pos.avg_entry, ctx)
        if adv.exit_now:
            self.request_exit(pos, adv.reason, bar.c)
            return
        # 8 rejection at target zone
        rng = bar.h - bar.l
        if bar.h >= pos.target and rng > 0 and (bar.h - max(bar.o, bar.c)) / rng >= 0.6 and mfe_r >= 1.0:
            self.request_exit(pos, Reason("TARGET_REJECTION", f"rejection wick at target zone {pos.target/100:.2f}"), bar.c)
            return
        # 9 time decay / 10 max hold
        if elapsed >= timedelta(minutes=pos.expected_hold_min) and mfe_r < c.time_decay_mfe_r:
            self.request_exit(pos, Reason("TIME_DECAY", f"{elapsed.seconds//60} min elapsed, MFE {mfe_r:.2f}R < {c.time_decay_mfe_r}R"), bar.c)
            return
        if elapsed >= timedelta(minutes=t_max):
            self.request_exit(pos, Reason("MAX_HOLD", f"held {elapsed.seconds//60} min >= {t_max:.0f}"), bar.c)
            return
        # 4 market shock
        idx_move = ctx.index.move_over(5)
        if idx_move <= c.market_shock_move:
            tgt = pos.avg_entry if mfe_r >= 1.0 else pos.avg_entry - 0.5 * r_ps
            self.move_stop(pos, int(tgt), Reason("MARKET_SHOCK", f"index {idx_move:.2%} in 5 min"), bar.c)
        # 11 exceptional trend
        if mfe_r >= c.exceptional_r and f.efficiency_ratio() > 0.6:
            self.extended_tmax.add(pos.position_id)
        # 5 break-even, 6 lock + trail
        atr = f.atr or 0.0
        if mfe_r >= c.lock_r:
            mult = c.exceptional_trail_atr if pos.position_id in self.extended_tmax else c.trail_atr
            lows = list(f.lows)
            higher_low = lows[-2] - inst.tick if len(lows) >= 2 else 0
            trail = max(pos.avg_entry + c.lock_to_r * r_ps, pos.high_water - mult * atr, higher_low)
            self.move_stop(pos, int(trail), Reason("PROFIT_LOCK_TRAIL", f"MFE {mfe_r:.2f}R: lock/trail"), bar.c)
        elif mfe_r >= c.breakeven_r:
            entry_c, _ = self.portfolio.order_costs(pos)
            be = pos.avg_entry + 2 * (entry_c * 100.0 / max(1, pos.qty))
            self.move_stop(pos, int(be) + inst.tick, Reason("BREAKEVEN", f"MFE {mfe_r:.2f}R >= {c.breakeven_r}R"), bar.c)
        # 7 momentum decay while in profit
        vols, closes, highs, lows = list(f.vols), list(f.closes), list(f.highs), list(f.lows)
        if len(vols) >= 4 and bar.c > pos.avg_entry:
            falling = vols[-1] < vols[-2] < vols[-3]
            weak = all((closes[-i] - lows[-i]) < 0.5 * (highs[-i] - lows[-i]) for i in (1, 2, 3) if highs[-i] > lows[-i])
            if falling and weak:
                self.move_stop(pos, bar.l - inst.tick, Reason("MOMENTUM_DECAY", "3 bars of falling volume, weak closes"), bar.c)

    def _escalate(self, pos: Position, bar: Bar) -> None:
        pos.exit_escalations += 1
        if pos.exit_escalations > self.cfg.max_exit_escalations:
            self.on_anomaly(f"exit for {pos.key} unfilled after {pos.exit_escalations} escalations")
        self.request_exit(pos, Reason("EXIT_ESCALATION", f"escalation #{pos.exit_escalations}"), bar.c, aggressive=True)
