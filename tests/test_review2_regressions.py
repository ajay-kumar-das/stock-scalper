"""Regression tests for the second adversarial review (tick fill model, replay, reconciliation):
doc 13 findings 35-41. Each reproduces the reviewer's failure scenario and asserts the fixed behaviour."""
from datetime import datetime, timedelta

from scalper.backtest.replay import run_replay
from scalper.core.types import IST, Instrument, OrderStatus
from scalper.data.ticks import Tick
from scalper.engine import Engine
from scalper.execution.sim_tick import TickSimConfig
from scalper.oms.oms import InvariantViolation

from .test_tick_sim_and_replay import K, feed, setup, tk


def test_volume_is_not_double_counted_across_resting_orders():
    """Two resting buys (e.g. from a future second account/strategy) share one tick's traded volume."""
    from scalper.core.clock import SimClock
    from scalper.core.types import OrderRole, OrderType, Side
    from scalper.execution.sim_tick import SimTickExecution
    from scalper.oms.order import Order
    T = datetime(2025, 3, 3, 10, 0, tzinfo=IST)
    clock = SimClock(T)
    sim = SimTickExecution(clock, {K: Instrument(K, "T")}, TickSimConfig(latency_median_ms=100, latency_sigma=0, reject_rate=0))
    got = []

    class L:
        def on_order_update(self, u):
            if u.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                got.append((u.order_id, u.cum_qty))
    sim.set_listener(L())
    orders = [Order(f"x{i}", f"t{i}", K, Side.BUY, OrderRole.ENTRY, OrderType.LIMIT, 100, 99_995) for i in (1, 2)]
    for o in orders:
        o.status = OrderStatus.SUBMITTED
        sim.place(o)
    for ms, ltp, vtt, bid, ask in [(0, 100_000, 1000, 99_990, 100_010), (150, 100_000, 1000, 99_990, 100_010),
                                   (300, 99_990, 1100, 99_985, 100_000)]:      # 100 sh trade through 99_995
        clock.set(T + timedelta(milliseconds=ms))
        sim.on_time(clock.now())
        sim.process_tick(tk(ms, ltp, vtt, [(bid, 10)], [(ask, 300)]))
        for o, (oid, cum) in [(o, g) for g in got for o in orders if o.order_id == g[0]]:
            o.filled_qty = cum
    total = sum(max(c for i, c in got if i == o.order_id) for o in orders if any(i == o.order_id for i, _ in got))
    assert total == 100                                                             # not 200


def test_unknown_order_is_not_assumed_cancelled_in_milliseconds_and_blocks_reentry():
    clock, sim, oms, fills, pos = setup(no_ack_rate=1.0)
    calls = {"n": 0}
    real_query = sim.query
    sim.query = lambda o: (calls.__setitem__("n", calls["n"] + 1), None if calls["n"] <= 5 else real_query(o))[1]
    feed(clock, sim, tk(0, 100_000, 1000, [(99_995, 400)], [(100_010, 300)]))
    o = oms.submit_entry(K, 100, 99_995, "s", "p")
    feed(clock, sim, tk(150, 100_000, 1000, [(99_995, 400)], [(100_010, 300)]))
    for ms in (2200, 2210, 2220):
        feed(clock, sim, tk(ms, 100_000, 1000, [(99_995, 400)], [(100_010, 300)]))
        oms.check_ack_timeouts(timedelta(seconds=2))
        oms.reconcile_unknown()
    assert o.status == OrderStatus.UNKNOWN and oms.has_unknown(K)                  # attempts are spaced in time
    try:
        oms.submit_entry(K, 100, 99_995, "s", "p2")
        raise AssertionError("re-entry must be blocked while state is uncertain")
    except InvariantViolation:
        pass


IDX = "NSE_INDEX|Nifty 50"
INSTS = {IDX: Instrument(IDX, "NIFTY", is_index=True), K: Instrument(K, "T")}
T0 = datetime(2025, 3, 3, 10, 0, 0, tzinfo=IST)


def _mk(key, recv, ts, ltp, vtt):
    return Tick(key, ts, recv, ltp, 10, vtt, ltp, 0, 0, ((ltp - 5, 500, 1),), ((ltp + 5, 500, 1),))


def _run(ticks, seen):
    orig = Engine.on_minute

    def rec(self, bars, fs):
        seen.append(sorted((k, b.ts, b.v) for k, b in bars.items()))
        return orig(self, bars, fs)
    Engine.on_minute = rec
    try:
        return run_replay(INSTS, {T0.date(): ticks}, IDX, lambda: [], sim_cfg=TickSimConfig(reject_rate=0))
    finally:
        Engine.on_minute = orig


def test_lagging_exchange_time_never_creates_duplicate_bars():
    ticks, vtt = [], 0
    for s in range(180):
        recv = T0 + timedelta(seconds=s, milliseconds=100)
        vtt += 10
        ticks.append(_mk(IDX, recv, recv, 2_200_000, 0))
        ticks.append(_mk(K, recv, recv - timedelta(milliseconds=300), 100_000 + s * 5, vtt))
    seen = []
    _run(ticks, seen)
    stock = [ts for bars in seen for k, ts, v in bars if k == K]
    assert len(stock) == len(set(stock)) and stock == sorted(stock)
    assert sum(v for bars in seen for k, ts, v in bars if k == K) <= vtt          # volume never invented


def test_genuine_large_move_is_accepted_after_one_flagged_tick():
    ticks = []
    for s in range(240):
        recv = T0 + timedelta(seconds=s)
        px = 100_000 if s < 60 else 88_000
        ticks.append(_mk(IDX, recv, recv, 2_200_000, 0))
        ticks.append(_mk(K, recv, recv, px, 100 * s))
    seen = []
    res = _run(ticks, seen)
    assert res.quarantined_ticks == 1
    last_k = [(ts, v) for bars in seen for k, ts, v in bars if k == K][-1]
    assert last_k[0].minute >= 2                                                   # engine keeps seeing the stock
