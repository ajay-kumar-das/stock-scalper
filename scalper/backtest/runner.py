"""Event-driven bar backtester. Bars are released at their close; orders fill no earlier than the
next bar (doc 05 §3, doc 08 §1). Deterministic: same data + config + seed -> identical results."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Callable, Optional

from ..analytics.metrics import full_report
from ..core.clock import SimClock
from ..core.types import Bar, Instrument, Mode
from ..costs.upstox import COST_MODEL_VERSION
from ..engine import Engine, EngineConfig
from ..execution.sim_bar import SimBarExecution, SimConfig
from ..journal.journal import Journal
from ..strategies.base import Strategy

FILL_MODEL_VERSION = "sim-bar-1.0"


@dataclass
class BacktestResult:
    run_id: str
    trades: list[dict]
    report: dict
    rejections: dict
    would_fail_edge: dict
    anomalies: list[str]
    eod_residuals: list[str]
    candidates_seen: int
    journal_path: str
    engine: Optional[Engine] = field(default=None, repr=False)


def config_hash(cfg: EngineConfig, sim: SimConfig, strategies: list[Strategy]) -> str:
    blob = json.dumps({"engine": asdict(cfg), "sim": asdict(sim),
                       "strategies": [(s.name, s.version, sorted(vars(s).items(), key=str)) for s in strategies]},
                      default=str, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def run_backtest(instruments: dict[str, Instrument], data: dict[date, dict[str, list[Bar]]], index_key: str,
                 strategies_factory: Callable[[], list[Strategy]], cfg: Optional[EngineConfig] = None,
                 sim_cfg: Optional[SimConfig] = None, journal_path: str = ":memory:",
                 run_id: Optional[str] = None, keep_engine: bool = False,
                 allowed_dates: Optional[Callable[[date], bool]] = None,
                 execution_factory: Optional[Callable] = None, trade_from: Optional[date] = None) -> BacktestResult:
    cfg = cfg or EngineConfig()
    sim_cfg = sim_cfg or SimConfig()
    strategies = strategies_factory()
    days = sorted(d for d in data if allowed_dates is None or allowed_dates(d))
    if not days:
        raise ValueError("no data")
    first_bar = next(iter(data[days[0]].values()))[0]
    clock = SimClock(first_bar.ts)
    run_id = run_id or f"bt-{config_hash(cfg, sim_cfg, strategies)}-{uuid.uuid4().hex[:6]}"
    journal = Journal(journal_path, run_id, Mode.BACKTEST.value)
    sim = (execution_factory or SimBarExecution)(clock, instruments, sim_cfg)
    hs = sim.half_spread
    eng = Engine(Mode.BACKTEST, clock, instruments, index_key, strategies, sim, journal, cfg, half_spread=hs)
    for d in days:
        day = data[d]
        clock.set(min(v[0].ts for v in day.values() if v))
        eng.warmup = trade_from is not None and d < trade_from
        eng.start_day(d)
        by_minute: dict = {}
        for k, bars in day.items():
            for b in bars:
                by_minute.setdefault(b.ts, {})[k] = b
        for ts in sorted(by_minute):
            bars = by_minute[ts]
            clock.set(next(iter(bars.values())).end)      # the bar becomes knowable at its close
            eng.on_minute(bars, sim.process_bar)
        eng.end_day()
    journal.record_run({"engine": asdict(cfg), "sim": asdict(sim_cfg)}, "m1", f"{days[0]}..{days[-1]}",
                       COST_MODEL_VERSION, FILL_MODEL_VERSION, None)
    trades = eng.portfolio.trades
    return BacktestResult(run_id, trades, full_report(trades), dict(eng.rejections), dict(eng.would_fail_edge),
                          eng.anomalies, eng.eod_residuals, eng.candidates_seen, journal_path,
                          eng if keep_engine else None)


# ---------------------------------------------------------------------- walk-forward / parallel windows
@dataclass
class Window:
    warm_start: date
    trade_start: date
    trade_end: date


def month_windows(days: list[date], warmup_sessions: int = 25) -> list[Window]:
    """One window per calendar month; each starts with `warmup_sessions` prior sessions of warm-up
    so 20-session features (RVOL profile, ADV, daily ATR) are fully formed before trading."""
    days = sorted(days)
    months: dict = {}
    for d in days:
        months.setdefault((d.year, d.month), []).append(d)
    out = []
    for (_, _), md in sorted(months.items()):
        i = days.index(md[0])
        out.append(Window(days[max(0, i - warmup_sessions)], md[0], md[-1]))
    return out


def _run_window(args) -> list[dict]:
    instruments, loader, index_key, strategies_factory, cfg, sim_cfg, w = args
    data = loader(w.warm_start, w.trade_end)
    if not data:
        return []
    res = run_backtest(instruments, data, index_key, strategies_factory, cfg, sim_cfg, trade_from=w.trade_start)
    return [t for t in res.trades if w.trade_start <= t["entry_ts"].date() <= w.trade_end]


def run_windows(instruments: dict[str, Instrument], loader: Callable, index_key: str,
                strategies_factory: Callable[[], list[Strategy]], windows: list[Window],
                cfg: Optional[EngineConfig] = None, sim_cfg: Optional[SimConfig] = None, workers: int = 1) -> dict:
    """Run independent monthly windows (optionally in parallel processes) and merge the trades.
    `loader(start, end)` returns {date: {key: [Bar]}}; with workers > 1 it, the strategies factory and
    configs must be picklable (module-level functions / dataclasses). Risk state does not carry across
    windows, so this mode is for strategy research statistics, not for account-path simulation."""
    jobs = [(instruments, loader, index_key, strategies_factory, cfg, sim_cfg, w) for w in windows]
    if workers > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=workers) as ex:
            parts = list(ex.map(_run_window, jobs))
    else:
        parts = [_run_window(j) for j in jobs]
    trades = sorted((t for p in parts for t in p), key=lambda t: t["entry_ts"])
    return {"trades": trades, "report": full_report(trades), "windows": len(windows)}
