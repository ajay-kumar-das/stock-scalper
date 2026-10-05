"""End-to-end backtest properties: determinism, no look-ahead, no residual positions, honest
costs, explainability. Synthetic data is used only to test the machinery, never for research."""
import dataclasses
from datetime import time

import pytest

from scalper.analytics.metrics import summarize
from scalper.backtest.runner import run_backtest
from scalper.core.types import Bar
from scalper.data.synthetic import INDEX_KEY, SynthConfig, generate
from scalper.decision.pipeline import research_config
from scalper.engine import EngineConfig
from scalper.selection.universe import UniverseConfig
from scalper.strategies.orb import OpeningRangeBreakout

SYN = SynthConfig(n_stocks=12, n_days=30, plant_edge=True, seed=5, in_play_prob=0.25, base_volume_per_min=60_000)


def strategies():
    return [OpeningRangeBreakout(min_breakout_vol=1.0, min_close_loc=0.5)]


def cfg():
    return EngineConfig(universe=UniverseConfig(min_adv_rupees=5e7), decision=research_config())


@pytest.fixture(scope="module")
def market():
    return generate(SYN)


@pytest.fixture(scope="module")
def result(market):
    insts, data, _ = market
    return run_backtest(insts, data, INDEX_KEY, strategies, cfg(), keep_engine=True)


def test_produces_trades_and_full_report(result):
    print("trades", len(result.trades), result.report["headline"])
    assert len(result.trades) >= 10
    rep = result.report
    for k in ("after_costs", "before_costs", "by_strategy", "by_stock", "by_time_of_day", "by_regime", "by_month"):
        assert k in rep
    net, gross = rep["after_costs"], rep["before_costs"]
    assert net["net_profit"] < gross["net_profit"]                      # costs always reduce P&L
    assert all(t["costs"] > 0 for t in result.trades)


def test_deterministic(market, result):
    insts, data, _ = market
    again = run_backtest(insts, data, INDEX_KEY, strategies, cfg())
    key = lambda tr: [(x["trade_id"], x["qty"], x["entry_price"], x["exit_price"], x["net_pnl"]) for x in tr]
    assert key(again.trades) == key(result.trades)


def test_no_residual_positions_or_anomalies(result):
    assert result.eod_residuals == []
    assert not [a for a in result.anomalies if "severity 1" in a or "NEGATIVE" in a]
    for t in result.trades:
        assert t["exit_ts"].time() <= time(15, 15)
        assert t["entry_ts"].time() <= time(14, 45)


def test_no_lookahead_future_days_cannot_change_past(market, result):
    insts, data, _ = market
    days = sorted(data)
    cut = days[20]
    perturbed = {}
    for d, day in data.items():
        if d < cut:
            perturbed[d] = day
        else:   # wildly different future: invert every price path
            perturbed[d] = {k: [Bar(b.key, b.ts, b.o, b.o + (b.o - b.l), b.o - (b.h - b.o), 2 * b.o - b.c, b.v * 3)
                                if b.o - (b.h - b.o) > 0 else b for b in bars] for k, bars in day.items()}
    alt = run_backtest(insts, perturbed, INDEX_KEY, strategies, cfg())
    past = lambda tr: [(x["trade_id"], x["entry_price"], x["exit_price"], x["qty"]) for x in tr if x["entry_ts"].date() < cut]
    assert past(result.trades), "test needs trades before the cut to be meaningful"
    assert past(alt.trades) == past(result.trades)


def test_no_lookahead_within_day(market, result):
    insts, data, _ = market
    traded_days = sorted({t["entry_ts"].date() for t in result.trades})
    d = traded_days[len(traded_days) // 2]
    first = min(t["entry_ts"] for t in result.trades if t["entry_ts"].date() == d)
    cutoff = first.replace(second=0, microsecond=0)
    altered = dict(data)
    altered[d] = {k: [b if b.ts <= cutoff else Bar(b.key, b.ts, b.o, b.h, b.l, b.o, 1) for b in bars]
                  for k, bars in data[d].items()}
    alt = run_backtest(insts, altered, INDEX_KEY, strategies, cfg())
    def decisions(tr):
        return [(x["trade_id"], x["entry_price"], x["qty"]) for x in tr
                if x["decision_ts"] <= cutoff]
    assert decisions(result.trades), "test needs a decision at/before the cutoff"
    assert decisions(alt.trades) == decisions(result.trades)


def test_every_trade_is_explainable(result):
    j = result.engine.journal
    text = j.explain_trade(result.trades[0]["trade_id"])
    assert "WHY BOUGHT" in text and "NET" in text and "SIZING" in text
    rejected_keys = [r[0] for r in j.conn.execute(
        "SELECT key FROM events WHERE kind='candidate_rejected' LIMIT 1").fetchall()]
    if rejected_keys:
        assert j.why_not(rejected_keys[0])


def test_null_market_does_not_produce_an_edge():
    """On a market with no planted structure, a correct engine must not show net profit
    beyond noise; systematic profit here would indicate look-ahead or fill-model bugs."""
    insts, data, _ = generate(dataclasses.replace(SYN, plant_edge=False, seed=21, n_days=40))
    res = run_backtest(insts, data, INDEX_KEY, strategies, cfg())
    s = summarize(res.trades)
    assert s["total_trades"] >= 10
    lo, hi = res.report["headline"]["net_expectancy_95ci"]
    assert lo < 0                                        # cannot claim a significant positive edge
    assert s["expectancy"] < summarize(res.trades, "gross_pnl")["expectancy"]
