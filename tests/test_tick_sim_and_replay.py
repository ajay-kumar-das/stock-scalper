"""Tick-level fill model and REPLAY engine (M2)."""
from datetime import datetime, timedelta


from scalper.core.clock import SimClock
from scalper.core.types import IST, Instrument, OrderStatus, Side
from scalper.data.ticks import Tick
from scalper.execution.sim_tick import SimTickExecution, TickSimConfig
from scalper.oms.oms import OMS, Throttle, ThrottleConfig

K = "NSE_EQ|T"
T0 = datetime(2025, 3, 3, 10, 0, 0, tzinfo=IST)


def tk(ms, ltp, vtt, bids, asks):
    ts = T0 + timedelta(milliseconds=ms)
    return Tick(K, ts, ts, ltp, 1, vtt, ltp, 0, 0, tuple((p, q, 1) for p, q in bids), tuple((p, q, 1) for p, q in asks))


def setup(**cfg):
    clock = SimClock(T0)
    sim = SimTickExecution(clock, {K: Instrument(K, "T", tick=5)},
                           TickSimConfig(latency_median_ms=100, latency_sigma=0.0, reject_rate=0.0, **cfg))
    pos = {}
    oms = OMS(clock, sim, lambda k: pos.get(k, 0), Throttle(clock, ThrottleConfig(100, 1000, 100)))
    fills = []

    class L:
        def on_fill(self, o, q, p, ts):
            fills.append((o.side, q, p, ts))
            pos[o.key] = pos.get(o.key, 0) + (q if o.side == Side.BUY else -q)

        def on_order_terminal(self, o):
            pass

    oms.fill_listener = L()
    return clock, sim, oms, fills, pos


def feed(clock, sim, t):
    clock.set(max(clock.now(), t.recv_ts))
    sim.process_tick(t)


def test_marketable_order_uses_book_at_arrival_not_decision():
    clock, sim, oms, fills, _ = setup()
    feed(clock, sim, tk(0, 100_000, 1000, [(99_995, 500)], [(100_005, 300), (100_010, 300)]))
    o = oms.submit_entry(K, 500, 100_010, "s", "p")
    feed(clock, sim, tk(50, 100_000, 1000, [(99_995, 500)], [(100_005, 300), (100_010, 300)]))   # before arrival
    assert fills == []
    # by arrival (100 ms) the ask has moved up: only 200 sh available within the limit
    feed(clock, sim, tk(120, 100_010, 1100, [(100_005, 500)], [(100_010, 200), (100_020, 900)]))
    assert fills[0][1] == 200 and fills[0][2] == 200 * 100_010 / 200
    assert o.status == OrderStatus.PARTIALLY_FILLED


def test_passive_order_waits_for_queue_ahead():
    clock, sim, oms, fills, _ = setup()
    feed(clock, sim, tk(0, 100_000, 1000, [(99_995, 400)], [(100_005, 300)]))
    oms.submit_entry(K, 100, 99_995, "s", "p")
    feed(clock, sim, tk(150, 100_000, 1000, [(99_995, 400)], [(100_005, 300)]))   # arrives, joins behind 400
    feed(clock, sim, tk(300, 99_995, 1300, [(99_995, 100)], [(100_000, 300)]))     # 300 traded at our price
    assert fills == []
    feed(clock, sim, tk(400, 99_995, 1450, [(99_995, 50)], [(100_000, 300)]))      # displayed only fell by 50
    assert fills == []                                                              # conservative: not yet reached
    feed(clock, sim, tk(450, 99_995, 1550, [(99_995, 0)], [(100_000, 300)]))       # level emptied by trades
    assert sum(f[1] for f in fills) == 50
    feed(clock, sim, tk(500, 99_990, 1600, [(99_990, 50)], [(99_995, 300)]))       # ask at our price: executed
    assert sum(f[1] for f in fills) == 100 and all(f[2] == 99_995 for f in fills)


def test_displayed_depth_cannot_be_reused_and_volume_is_shared():
    clock, sim, oms, fills, _ = setup()
    feed(clock, sim, tk(0, 100_000, 1000, [(99_995, 500)], [(100_005, 100)]))
    oms.submit_entry(K, 1000, 100_005, "s", "p")
    for i in range(10):                                                             # nothing trades, same book
        feed(clock, sim, tk(150 + i * 50, 100_000, 1000, [(99_995, 500)], [(100_005, 100)]))
    assert sum(f[1] for f in fills) == 100                                          # the displayed 100, once


def test_resting_order_hit_fills_at_own_limit_not_better():
    clock, sim, oms, fills, _ = setup()
    feed(clock, sim, tk(0, 100_000, 1000, [(99_990, 500)], [(100_005, 500)]))
    oms.submit_entry(K, 100, 99_995, "s", "p")
    feed(clock, sim, tk(150, 100_000, 1000, [(99_990, 500)], [(100_005, 500)]))
    feed(clock, sim, tk(300, 99_950, 1000, [(99_940, 500)], [(99_950, 500)]))      # market collapses through us
    assert sum(f[1] for f in fills) == 100 and fills[0][2] == 99_995


def test_cancel_races_with_fill():
    clock, sim, oms, fills, _ = setup()
    feed(clock, sim, tk(0, 100_000, 1000, [(99_995, 0)], [(100_010, 300)]))
    o = oms.submit_entry(K, 100, 100_000, "s", "p")
    feed(clock, sim, tk(150, 100_000, 1000, [(99_990, 10)], [(100_010, 300)]))      # resting
    oms.cancel(o)                                                                   # cancel arrives at +100 ms
    feed(clock, sim, tk(200, 99_990, 1500, [(99_985, 10)], [(99_995, 300)]))        # market comes to us first
    assert sum(f[1] for f in fills) == 100 and o.status == OrderStatus.FILLED


def test_stop_limit_triggers_on_ltp_and_can_gap_through():
    clock, sim, oms, fills, pos = setup()
    pos[K] = 100
    feed(clock, sim, tk(0, 100_000, 1000, [(99_995, 500)], [(100_005, 500)]))
    stop = oms.place_stop(K, 100, 99_500, 99_300, "s", "p")
    feed(clock, sim, tk(150, 100_000, 1000, [(99_995, 500)], [(100_005, 500)]))
    feed(clock, sim, tk(300, 99_100, 1500, [(99_000, 500)], [(99_150, 500)]))       # gaps below the 993 limit
    assert stop.triggered and fills == [] and stop.is_working
    feed(clock, sim, tk(400, 99_350, 1600, [(99_320, 500)], [(99_360, 500)]))       # recovers above the limit
    assert sum(f[1] for f in fills) == 100 and fills[-1][2] >= 99_300


def test_missing_ack_becomes_unknown_then_reconciles():
    clock, sim, oms, fills, _ = setup(no_ack_rate=1.0)
    feed(clock, sim, tk(0, 100_000, 1000, [(99_995, 500)], [(100_005, 500)]))
    o = oms.submit_entry(K, 10, 99_000, "s", "p")              # far from market: rests silently
    feed(clock, sim, tk(150, 100_000, 1000, [(99_995, 500)], [(100_005, 500)]))
    clock.set(T0 + timedelta(seconds=3))
    oms.check_ack_timeouts(timedelta(seconds=2))
    assert o.status == OrderStatus.UNKNOWN and oms.has_unknown(K)
    oms.reconcile_unknown()
    assert o.status == OrderStatus.ACKNOWLEDGED and not oms.has_unknown()


# ---------------------------------------------------------------------- replay end-to-end
def test_replay_runs_same_engine_on_ticks_without_residuals():
    from scalper.backtest.replay import run_replay
    from scalper.data.synthetic import INDEX_KEY, SynthConfig, generate, ticks_from_bars
    from scalper.decision.pipeline import research_config
    from scalper.engine import EngineConfig
    from scalper.selection.universe import UniverseConfig
    from scalper.strategies.orb import OpeningRangeBreakout

    insts, data, _ = generate(SynthConfig(n_stocks=6, n_days=10, plant_edge=True, seed=10, in_play_prob=0.3,
                                          base_volume_per_min=60_000))
    ticks = {d: ticks_from_bars(day, seed=i) for i, (d, day) in enumerate(sorted(data.items()))}
    cfg = EngineConfig(universe=UniverseConfig(min_adv_rupees=5e7), decision=research_config())
    strat = lambda: [OpeningRangeBreakout(min_breakout_vol=1.0, min_close_loc=0.5, min_rvol=1.3)]
    res = run_replay(insts, ticks, INDEX_KEY, strat, cfg, keep_engine=True)
    res2 = run_replay(insts, ticks, INDEX_KEY, strat, cfg)
    assert res.eod_residuals == []
    assert [t["net_pnl"] for t in res.trades] == [t["net_pnl"] for t in res2.trades]      # deterministic
    assert not [a for a in res.anomalies if "NEGATIVE" in a or "LISTENER_ERROR" in a]
    for t in res.trades:
        assert t["costs"] > 0 and t["exit_ts"] > t["entry_ts"]
    assert res.trades, "replay should exercise the full trade lifecycle"
    assert "FEED_STALE" not in res.engine.session.defensive_reasons
    print("replay trades", len(res.trades), res.rejections)
