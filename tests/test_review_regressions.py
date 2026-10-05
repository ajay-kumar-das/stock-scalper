"""Regression tests for defects found by the adversarial review of M1 (doc 13 findings 28-34).
Each test reproduces the original failure scenario with a manually driven broker adapter."""
from datetime import datetime, timedelta

from scalper.core.clock import SimClock
from scalper.core.types import IST, Bar, Instrument, Mode, OrderStatus, OrderType, Reason, Side
from scalper.engine import Engine, EngineConfig
from scalper.execution.base import ExecutionAdapter, OrderUpdate
from scalper.journal.journal import Journal
from scalper.portfolio.portfolio import PosState, Position
from scalper.risk.engine import RiskState
from scalper.strategies.orb import OpeningRangeBreakout

KEY, IDX = "NSE_EQ|T", "NSE_INDEX|NIFTY"


class Manual(ExecutionAdapter):
    def __init__(self, clock):
        self.clock = clock

    def place(self, o):
        self._emit(OrderUpdate(o.order_id, o.tag, OrderStatus.ACKNOWLEDGED, 0, 0, self.clock.now(), "ack"))

    def modify(self, o, **kw):
        pass

    def cancel(self, o):
        self._emit(OrderUpdate(o.order_id, o.tag, OrderStatus.CANCELLED, o.filled_qty, o.filled_value, self.clock.now(), "cxl"))

    def fill(self, o, cum, px, status=None):
        st = status or (OrderStatus.FILLED if cum == o.qty else OrderStatus.PARTIALLY_FILLED)
        self._emit(OrderUpdate(o.order_id, o.tag, st, cum, o.filled_value + (cum - o.filled_qty) * px, self.clock.now(), "fill"))


def make():
    clock = SimClock(datetime(2025, 3, 3, 10, 0, tzinfo=IST))
    insts = {KEY: Instrument(KEY, "T", tick=5, sector="IT"), IDX: Instrument(IDX, "NIFTY", is_index=True)}
    ex = Manual(clock)
    eng = Engine(Mode.PAPER, clock, insts, IDX, [OpeningRangeBreakout()], ex, Journal(":memory:", "r", "PAPER"), EngineConfig())
    eng.start_day(clock.now().date())
    return clock, ex, eng


def open_pos(eng, qty=1000, entry=100_000, stop=99_000, pid="p1", key=KEY):
    pos = Position(pid, key, key[-1], "orb_inplay", "1", "s", eng.clock.now(), entry, stop, stop, entry + 2000, 0.5, 10, 30,
                   "x", {}, [], qty, qty * (entry - stop) / 100, qty * 2000 / 100)
    eng.portfolio.open(pos)
    o = eng.oms.submit_entry(key, qty, entry, "orb_inplay", pid)
    pos.order_ids.append(o.order_id)
    return pos, o


def minute(eng, clock, m, px):
    clock.set(datetime(2025, 3, 3, 10, m, tzinfo=IST))
    ts = clock.now() - timedelta(minutes=1)
    eng.on_minute({KEY: Bar(KEY, ts, px, px + 100, px - 100, px, 50_000),
                   IDX: Bar(IDX, ts, 2_200_000, 2_200_500, 2_199_500, 2_200_000, 1)}, lambda b: None)


def sells(eng):
    return sum(o.remaining for o in eng.oms.working(KEY, side=Side.SELL))


def test_async_stop_rejection_is_reprotected():
    clock, ex, eng = make()
    pos, e = open_pos(eng)
    ex.fill(e, 1000, 100_000)
    stop = eng.oms.orders[pos.stop_order_id]
    clock.set(clock.now() + timedelta(milliseconds=200))
    ex._emit(OrderUpdate(stop.order_id, stop.tag, OrderStatus.REJECTED, 0, 0, clock.now(), "RMS"))
    assert sells(eng) == 1000 and eng.anomalies


def test_late_fill_on_abandoned_position_is_tracked_and_exited():
    clock, ex, eng = make()
    pos, e = open_pos(eng)
    eng.oms.cancel(e, "TTL")
    ex.fill(e, 400, 100_000, status=OrderStatus.CANCELLED)
    assert eng.portfolio.position_qty(KEY) == 400 and sells(eng) == 400
    assert not [a for a in eng.anomalies if "LISTENER_ERROR" in a]


def test_position_not_closed_while_entry_working_and_remainder_cancelled():
    clock, ex, eng = make()
    pos, e = open_pos(eng)
    ex.fill(e, 300, 100_000)
    stop = eng.oms.orders[pos.stop_order_id]
    ex.fill(stop, 300, 98_900)
    assert not e.is_working                      # entry remainder cancelled when the stop filled
    assert pos.state == PosState.CLOSED and len(eng.portfolio.trades) == 1


def test_protective_actions_not_starved_by_throttle():
    clock, ex, eng = make()
    pos, e = open_pos(eng)
    for cum in (200, 500, 1000):
        clock.set(clock.now() + timedelta(milliseconds=100))
        ex.fill(e, cum, 100_000)
    # the stop may be resized by modify (unacked here) or re-placed; total protection must cover the position
    stop = eng.oms.orders[pos.stop_order_id]
    assert stop.qty - stop.filled_qty == 1000


def test_exit_not_dropped_when_many_actions_in_one_instant():
    clock, ex, eng = make()
    ps = []
    for i, k in enumerate("ABC"):
        key = f"NSE_EQ|{k}"
        eng.instruments[key] = Instrument(key, k, tick=5, sector=f"S{i}")
        ps.append(open_pos(eng, pid=f"p{i}", key=key))
    clock.set(clock.now() + timedelta(minutes=1))
    for p, e in ps:
        ex.fill(e, e.qty, 100_000)
    p0 = ps[0][0]
    eng.manager.request_exit(p0, Reason("THESIS", "x"), 100_500)
    assert eng.oms.orders[p0.stop_order_id].order_type == OrderType.LIMIT


def test_partial_exit_loss_counts_toward_daily_limit():
    clock, ex, eng = make()
    pos, e = open_pos(eng)
    ex.fill(e, 1000, 100_000)
    ex.fill(eng.oms.orders[pos.stop_order_id], 900, 99_600)   # Rs -3,600 realised on 900 sh; limit Rs -3,000
    minute(eng, clock, 1, 99_600)
    assert eng.risk.state == RiskState.HALTED_FOR_DAY


def test_exit_all_and_flatten_cancel_unfilled_entries():
    clock, ex, eng = make()
    pos, e = open_pos(eng)
    eng.exit_all({KEY: 100_000})
    assert not e.is_working
    pos2, e2 = open_pos(eng, pid="p2")
    clock.set(datetime(2025, 3, 3, 15, 5, tzinfo=IST))
    ts = clock.now() - timedelta(minutes=1)
    eng.on_minute({KEY: Bar(KEY, ts, 100_000, 100_100, 99_900, 100_000, 50_000),
                   IDX: Bar(IDX, ts, 2_200_000, 2_200_500, 2_199_500, 2_200_000, 1)}, lambda b: None)
    assert not e2.is_working


def test_eod_residual_is_squared_off_and_charged_in_simulation():
    clock, ex, eng = make()
    pos, e = open_pos(eng)
    ex.fill(e, 1000, 100_000)
    eng.end_day()
    assert pos.state == PosState.CLOSED and eng.portfolio.open_positions() == []
    t = eng.portfolio.trades[-1]
    assert t["exit_reasons"][-1]["code"] == "AUTO_SQUARE_OFF" and t["costs"] > 88.5
    assert eng.eod_residuals


def test_rejected_modify_reverts_local_state():
    clock, ex, eng = make()
    pos, e = open_pos(eng)
    ex.fill(e, 1000, 100_000)
    stop = eng.oms.orders[pos.stop_order_id]
    eng.manager.move_stop(pos, 99_500, Reason("BREAKEVEN", "x"), 101_000)
    assert pos.stop == 99_500 and stop.pending_modify
    ex._emit(OrderUpdate(stop.order_id, stop.tag, stop.status, 0, 0, clock.now(), "MODIFY_REJECTED"))
    assert stop.trigger == 99_000 and pos.stop == 99_000 and not stop.pending_modify
