"""Research backtest on REAL data from the local bar store (R3/R4/R5 runs).

Research mode measures (does not enforce) the edge and regime gates (doc 07 §5). The holdout
partition is locked by code (doc 05 §2). Monthly walk-forward windows with 25-session warm-up can
run in parallel processes.

    python scripts/run_research.py --partition development --bhavcopy data/bhavcopy \
        --master data/instruments/NSE-<date>.json [--events data/board_meetings.csv] [--workers 4]
"""
import argparse
import json

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))   # works from a checkout without installing

from scalper.backtest.partitions import PARTITIONS, ExperimentRegistry, date_filter
from scalper.backtest.runner import month_windows, run_windows
from scalper.data.bhavcopy import load_dir, point_in_time_universe
from scalper.data.instruments import NIFTY_KEY, parse_master
from scalper.data.store import BarStore, UniverseStoreLoader
from scalper.decision.pipeline import research_config
from scalper.engine import EngineConfig
from scalper.session.calendar import load_event_exclusions
from scalper.strategies.orb import OpeningRangeBreakout


def strategies():
    return [OpeningRangeBreakout()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--partition", default="development", choices=list(PARTITIONS))
    ap.add_argument("--data", default="data")
    ap.add_argument("--bhavcopy", required=True)
    ap.add_argument("--master", required=True)
    ap.add_argument("--events", default=None)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--version", default=OpeningRangeBreakout.version)
    a = ap.parse_args()
    reg = ExperimentRegistry(f"{a.data}/experiments.sqlite")
    allowed = date_filter(a.partition, strategy=OpeningRangeBreakout.name, version=a.version, registry=reg)
    universe = point_in_time_universe(load_dir(a.bhavcopy))
    if a.events:      # corporate events: drop those symbols on those days (doc 13 finding 15)
        for d, syms in load_event_exclusions(a.events).items():
            if d in universe:
                universe[d] = [s for s in universe[d] if s not in syms]
    insts = parse_master(json.load(open(a.master)))
    by_symbol = {i.symbol: k for k, i in insts.items()}
    all_days = BarStore(a.data).dates()
    days = [d for d in all_days if d.weekday() < 5]
    wins = [w for w in month_windows(days, 25) if allowed(w.trade_start) and allowed(w.trade_end)]
    print(f"{len(wins)} monthly windows in {a.partition}")
    loader = UniverseStoreLoader(a.data, universe, by_symbol, NIFTY_KEY)
    out = run_windows(insts, loader, NIFTY_KEY, strategies, wins, EngineConfig(decision=research_config()),
                      workers=a.workers)
    rep = out["report"]
    print(json.dumps(rep["headline"], default=str, indent=2))
    print(json.dumps({k: rep["after_costs"].get(k) for k in
                      ("total_trades", "win_rate", "expectancy", "expectancy_r", "profit_factor", "net_profit",
                       "max_drawdown", "total_costs")}, default=str, indent=2))
    print("before costs expectancy:", rep["before_costs"].get("expectancy"))
    print("by regime (net expectancy):", {k: round(v["net"].get("expectancy", 0), 1) for k, v in rep["by_regime"].items()})
    print("by month (net):", {k: round(v["net"].get("net_profit", 0)) for k, v in rep["by_month"].items()})
    stage = {"development": "R3", "validation": "R4", "holdout": "R5"}.get(a.partition, "R?")
    reg.record(f"{stage}-{a.partition}-{len(out['trades'])}", "H1", OpeningRangeBreakout.name, a.version, stage,
               a.partition, rep["headline"], passed=False)
    print("Recorded in the experiment registry. Pass/fail is decided at the doc 05 gate review, not by this script.")


if __name__ == "__main__":
    main()
