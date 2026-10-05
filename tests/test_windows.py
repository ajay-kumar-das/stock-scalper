"""Walk-forward windows with warm-up: features fully formed, trades only inside trade periods,
and results consistent with a single continuous run."""
import functools

from scalper.backtest.runner import month_windows, run_backtest, run_windows
from scalper.data.synthetic import INDEX_KEY, SynthConfig, generate
from scalper.decision.pipeline import research_config
from scalper.engine import EngineConfig
from scalper.selection.universe import UniverseConfig
from scalper.strategies.orb import OpeningRangeBreakout

SYN = SynthConfig(n_stocks=8, n_days=70, plant_edge=True, seed=5, in_play_prob=0.25, base_volume_per_min=60_000)


def strategies():
    return [OpeningRangeBreakout(min_breakout_vol=1.0, min_close_loc=0.5)]


def loader(data, start, end):
    return {d: v for d, v in data.items() if start <= d <= end}


def test_windows_match_continuous_run_on_entries():
    insts, data, _ = generate(SYN)
    cfg = EngineConfig(universe=UniverseConfig(min_adv_rupees=5e7), decision=research_config())
    full = run_backtest(insts, data, INDEX_KEY, strategies, cfg)
    wins = month_windows(sorted(data), warmup_sessions=25)[1:]           # first month has no warm-up history
    out = run_windows(insts, functools.partial(loader, data), INDEX_KEY, strategies, wins, cfg)
    start = wins[0].trade_start
    full_entries = {(t["key"], t["entry_ts"], t["entry_price"]) for t in full.trades if t["entry_ts"].date() >= start}
    win_entries = {(t["key"], t["entry_ts"], t["entry_price"]) for t in out["trades"]}
    assert all(any(w.trade_start <= t["entry_ts"].date() <= w.trade_end for w in wins) for t in out["trades"])
    assert full_entries, "need trades to compare"
    overlap = len(full_entries & win_entries) / len(full_entries | win_entries)
    assert overlap >= 0.8, overlap      # differences only from risk state that doesn't carry across windows


def test_parallel_workers_give_identical_results():
    insts, data, _ = generate(SYN)
    cfg = EngineConfig(universe=UniverseConfig(min_adv_rupees=5e7), decision=research_config())
    wins = month_windows(sorted(data), warmup_sessions=25)[1:]
    seq = run_windows(insts, functools.partial(loader, data), INDEX_KEY, strategies, wins, cfg, workers=1)
    par = run_windows(insts, functools.partial(loader, data), INDEX_KEY, strategies, wins, cfg, workers=2)
    key = lambda r: [(t["key"], t["entry_ts"], t["net_pnl"]) for t in r["trades"]]
    assert key(seq) == key(par)
