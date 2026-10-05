"""REPLAY: drive the same Engine from recorded ticks (M2). Ticks feed the tick-level fill simulator
directly; closed 1-minute bars feed features/strategies/risk exactly as in BACKTEST and LIVE."""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date
from typing import Callable, Optional

from ..analytics.metrics import full_report
from ..core.clock import SimClock
from ..core.types import Instrument, Mode
from ..data.ticks import BarBuilder, Tick, TickValidator
from ..engine import Engine, EngineConfig
from ..execution.sim_tick import SimTickExecution, TickSimConfig
from ..journal.journal import Journal
from ..strategies.base import Strategy
from .runner import BacktestResult


def _half_spread_from_book(sim: SimTickExecution, instruments: dict[str, Instrument]):
    def hs(key: str, px: int) -> int:
        t = sim.last_tick.get(key)
        if t is not None and t.best_bid and t.best_ask:
            return max(instruments[key].tick, (t.best_ask - t.best_bid) // 2)
        return max(instruments[key].tick, int(px * 3 / 1e4))
    return hs


def run_replay(instruments: dict[str, Instrument], ticks_by_day: dict[date, list[Tick]], index_key: str,
               strategies_factory: Callable[[], list[Strategy]], cfg: Optional[EngineConfig] = None,
               sim_cfg: Optional[TickSimConfig] = None, journal_path: str = ":memory:",
               keep_engine: bool = False, execution_factory: Optional[Callable] = None) -> BacktestResult:
    cfg = cfg or EngineConfig()
    days = sorted(d for d, v in ticks_by_day.items() if v)
    clock = SimClock(min(t.recv_ts for t in ticks_by_day[days[0]]))
    sim = (execution_factory or SimTickExecution)(clock, instruments, sim_cfg or TickSimConfig())
    journal = Journal(journal_path, f"rp-{uuid.uuid4().hex[:8]}", Mode.REPLAY.value)
    eng = Engine(Mode.REPLAY, clock, instruments, index_key, strategies_factory(), sim, journal, cfg,
                 half_spread=_half_spread_from_book(sim, instruments))
    quarantined = 0
    for d in days:
        ticks = sorted(ticks_by_day[d], key=lambda t: (t.recv_ts, t.key))
        clock.set(max(clock.now(), ticks[0].recv_ts))
        eng.start_day(d)
        bb, val = BarBuilder(), TickValidator()
        pending: list = []
        cur_min = None
        last_recv = None

        def dispatch(upto):
            nonlocal pending
            pending += bb.flush(upto)
            ready = [b for b in pending if b.ts < upto.replace(second=0, microsecond=0)]
            pending = [b for b in pending if b not in ready]
            by_ts = defaultdict(dict)
            for b in ready:
                by_ts[b.ts][b.key] = b
            for ts in sorted(by_ts):
                eng.on_minute(by_ts[ts], lambda b: None)

        for t in ticks:
            clock.set(max(clock.now(), t.recv_ts))
            if hasattr(sim, "on_time"):
                sim.on_time(clock.now())
            m = t.recv_ts.replace(second=0, microsecond=0)
            if cur_min is not None and m > cur_min:
                dispatch(t.recv_ts)
            cur_min = m
            gap = (t.recv_ts - last_recv).total_seconds() if last_recv else 0.0
            last_recv = t.recv_ts
            bad = val.check(t)
            if bad:
                quarantined += 1
                val.release(t.key)
                eng.on_bad_data(t.key, bad, clock.now())
                continue
            eng.on_valid_tick(t.key, t.ltp)
            sim.process_tick(t)
            b = bb.on_tick(t)
            if b is not None:
                pending.append(b)
            eng.housekeeping(clock.now(), feed_gap_s=gap)
        dispatch(clock.now().replace(hour=23, minute=59))
        eng.end_day()
    trades = eng.portfolio.trades
    res = BacktestResult(journal.run_id, trades, full_report(trades), dict(eng.rejections), dict(eng.would_fail_edge),
                         eng.anomalies, eng.eod_residuals, eng.candidates_seen, journal_path, eng if keep_engine else None)
    res.quarantined_ticks = quarantined
    return res
