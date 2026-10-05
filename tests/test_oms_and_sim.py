"""OMS invariants and conservative fill-model behaviour."""
from datetime import timedelta

import pytest

from scalper.core.types import OrderRole, OrderStatus, OrderType, Side
from scalper.execution.base import ExecutionAdapter, OrderUpdate
from scalper.execution.sim_bar import SimBarExecution, SimConfig
from scalper.oms.oms import OMS, InvariantViolation, Throttle, ThrottleConfig

from .conftest import KEY, mkbar, t


class Recorder(ExecutionAdapter):
    def __init__(self, clock, ack=True):
        self.clock, self.ack, self.placed, self.modified = clock, ack, [], []

    def place(self, o):
        self.placed.append(o)
        if self.ack:
            self._emit(OrderUpdate(o.order_id, o.tag, OrderStatus.ACKNOWLEDGED, 0, 0, self.clock.now()))

    def modify(self, o, **kw):
        self.modified.append((o.order_id, kw))

    def cancel(self, o):
        self._emit(OrderUpdate(o.order_id, o.tag, OrderStatus.CANCELLED, o.filled_qty, o.filled_value, self.clock.now()))


def mk_oms(clock, pos=None, ack=True, per_second=100):
    pos = pos if pos is not None else {}
    ex = Recorder(clock, ack)
    oms = OMS(clock, ex, lambda k: pos.get(k, 0), Throttle(clock, ThrottleConfig(per_second, 1000)))
    return oms, ex, pos


def test_duplicate_entry_is_impossible(clock):
    oms, ex, pos = mk_oms(clock)
    oms.submit_entry(KEY, 10, 100_000, "s", "p1")
    with pytest.raises(InvariantViolation):
        oms.submit_entry(KEY, 10, 100_000, "s", "p2")


def test_no_accidental_short_closing_qty_capped_by_position(clock):
    oms, ex, pos = mk_oms(clock, {KEY: 100})
    stop = oms.place_stop(KEY, 100, 99_000, 98_700, "s", "p1")
    assert stop.status == OrderStatus.ACKNOWLEDGED
    with pytest.raises(InvariantViolation):
        oms.place_stop(KEY, 1, 99_000, 98_700, "s", "p1")
    with pytest.raises(InvariantViolation):
        oms.place_exit(KEY, 100, 99_500, "s", "p1")      # must use exit-by-modify while stop is working


def test_exit_by_modify_converts_stop_to_limit(clock):
    oms, ex, pos = mk_oms(clock, {KEY: 100})
    stop = oms.place_stop(KEY, 100, 99_000, 98_700, "s", "p1")
    assert oms.exit_by_modify(stop, 99_800, "THESIS_INVALID")
    assert stop.order_type == OrderType.LIMIT and stop.price == 99_800
    assert oms.working_closing_qty(KEY) == 100           # still exactly one closing order


def test_unknown_order_blocks_new_entries(clock):
    oms, ex, pos = mk_oms(clock, ack=False)
    o = oms.submit_entry(KEY, 10, 100_000, "s", "p1")
    o.transition(OrderStatus.UNKNOWN, clock.now(), "ack timeout")
    with pytest.raises(InvariantViolation):
        oms.submit_entry(KEY, 10, 100_000, "s", "p2")


def test_throttle_limits_orders_per_second(clock):
    oms, ex, pos = mk_oms(clock, per_second=3)
    keys = [f"K{i}" for i in range(5)]
    sent = [oms.submit_entry(k, 1, 100, "s", f"p{i}") for i, k in enumerate(keys)]
    assert sum(o is not None for o in sent) == 3
    clock.set(clock.now() + timedelta(seconds=2))
    assert oms.submit_entry("K9", 1, 100, "s", "p9") is not None


# ---------------------------------------------------------------------- fill model
def mk_sim(clock, inst, **cfg):
    sim = SimBarExecution(clock, {KEY: inst}, SimConfig(reject_rate=0.0, **cfg))
    pos = {}
    oms = OMS(clock, sim, lambda k: pos.get(k, 0), Throttle(clock, ThrottleConfig(100, 1000)))
    fills = []

    class L:
        def on_fill(self, o, q, p, ts):
            fills.append((o.role, q, p, ts))
            pos[o.key] = pos.get(o.key, 0) + (q if o.side == Side.BUY else -q)

        def on_order_terminal(self, o):
            pass

    oms.fill_listener = L()
    return sim, oms, fills, pos


def test_entry_never_fills_in_decision_bar_and_pays_half_spread(clock, inst):
    sim, oms, fills, pos = mk_sim(clock, inst, default_half_spread_bps=3.0)
    clock.set(t(9, 31))                                   # decision at close of 09:30 bar
    sim.process_bar(mkbar(t(9, 30), 100_000, 100_500, 99_900, 100_400))   # decision bar: already past
    oms.submit_entry(KEY, 100, 100_600, "s", "p1")
    sim.process_bar(mkbar(t(9, 30), 100_000, 100_500, 99_900, 100_400))   # replaying the same bar must not fill
    assert fills == []
    sim.process_bar(mkbar(t(9, 31), 100_400, 100_800, 100_300, 100_700))
    assert len(fills) == 1
    _, q, p, _ = fills[0]
    assert q == 100 and p >= 100_400 + 30                 # open + half spread (3 bps of 1004 = 0.30)


def test_entry_is_not_chased(clock, inst):
    sim, oms, fills, pos = mk_sim(clock, inst)
    clock.set(t(9, 31))
    o = oms.submit_entry(KEY, 100, 100_000, "s", "p1")
    sim.process_bar(mkbar(t(9, 31), 100_500, 100_800, 99_500, 100_700))   # opens above limit; trades below later
    assert fills == [] and o.status == OrderStatus.CANCELLED               # 1-bar TTL, no fill on later touch


def test_same_bar_protective_stop_assumes_worst_case(clock, inst):
    sim, oms, fills, pos = mk_sim(clock, inst)
    clock.set(t(9, 31))
    oms.submit_entry(KEY, 100, 101_000, "s", "p1")

    class L2:
        def on_fill(self, o, q, p, ts):
            fills.append((o.role, q, p, ts))
            pos[o.key] = pos.get(o.key, 0) + (q if o.side == Side.BUY else -q)
            if o.role == OrderRole.ENTRY:
                oms.place_stop(KEY, q, 99_500, 99_200, "s", "p1")

        def on_order_terminal(self, o):
            pass

    oms.fill_listener = L2()
    sim.process_bar(mkbar(t(9, 31), 100_000, 100_600, 99_300, 100_500))   # entry at open, low hits stop
    roles = [f[0] for f in fills]
    assert roles == [OrderRole.ENTRY, OrderRole.STOP]
    assert fills[1][2] < 99_500                                            # stop filled below trigger (slippage)


def test_stop_limit_gap_through_does_not_fill(clock, inst):
    sim, oms, fills, pos = mk_sim(clock, inst)
    pos[KEY] = 100
    clock.set(t(10, 0))
    stop = oms.place_stop(KEY, 100, 99_500, 99_200, "s", "p1")
    sim.process_bar(mkbar(t(10, 0), 98_000, 98_200, 97_800, 98_100))       # gap far below the limit
    assert fills == [] and stop.triggered and stop.is_working


def test_volume_cap_produces_partial_fill_and_remainder_cancelled(clock, inst):
    sim, oms, fills, pos = mk_sim(clock, inst, entry_volume_cap=0.10)
    clock.set(t(9, 31))
    o = oms.submit_entry(KEY, 1_000, 101_000, "s", "p1")
    sim.process_bar(mkbar(t(9, 31), 100_000, 100_100, 99_900, 100_000, v=5_000))
    assert fills[0][1] == 500 and o.status == OrderStatus.CANCELLED and o.filled_qty == 500
