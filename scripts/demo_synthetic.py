"""Demo of the M1 engine on SYNTHETIC data (machinery check only — not evidence of any edge).

    python scripts/demo_synthetic.py [--null]

--null runs the same strategy on a market with no planted structure: it should not show a
significant net edge. The default plants a momentum effect so the full trade lifecycle is exercised.
"""
import argparse
import json

from scalper.backtest.runner import run_backtest
from scalper.data.synthetic import INDEX_KEY, SynthConfig, generate
from scalper.decision.pipeline import research_config
from scalper.engine import EngineConfig
from scalper.selection.universe import UniverseConfig
from scalper.strategies.orb import OpeningRangeBreakout


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--null", action="store_true")
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--stocks", type=int, default=16)
    a = ap.parse_args()
    insts, data, _ = generate(SynthConfig(n_stocks=a.stocks, n_days=a.days, plant_edge=not a.null, seed=5,
                                          in_play_prob=0.25, base_volume_per_min=60_000))
    cfg = EngineConfig(universe=UniverseConfig(min_adv_rupees=5e7), decision=research_config())
    res = run_backtest(insts, data, INDEX_KEY, lambda: [OpeningRangeBreakout(min_breakout_vol=1.0, min_close_loc=0.5)],
                       cfg, keep_engine=True, journal_path="demo_journal.sqlite")
    net, gross = res.report["after_costs"], res.report["before_costs"]
    print(f"== SYNTHETIC {'NULL' if a.null else 'PLANTED-EDGE'} MARKET — machinery check, not research ==")
    print(json.dumps(res.report["headline"], default=str, indent=2))
    for k in ("total_trades", "win_rate", "expectancy", "expectancy_r", "profit_factor", "net_profit",
              "total_costs", "max_drawdown", "median_holding_s"):
        print(f"{k:>22}: after costs {net.get(k)!s:>24} | before costs {gross.get(k)!s:>24}")
    print("rejections by gate:", res.rejections)
    print("research-mode would-fail:", res.would_fail_edge)
    if res.trades:
        print("\n" + res.engine.journal.explain_trade(res.trades[0]["trade_id"]))


if __name__ == "__main__":
    main()
