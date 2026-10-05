"""Early fault-injection tests (full F1-F32 suite is milestone M5) and metric correctness."""
from datetime import datetime

import pytest

from scalper.analytics.metrics import _drawdown, _streaks, required_sample, summarize
from scalper.backtest.runner import run_backtest
from scalper.core.types import OrderRole, OrderStatus
from scalper.data.synthetic import INDEX_KEY, generate
from scalper.execution.base import OrderUpdate
from scalper.execution.sim_bar import SimBarExecution

from .test_backtest_e2e import SYN, cfg, strategies


class StopRejectingSim(SimBarExecution):
    """F9: the broker rejects every protective stop order."""

    def place(self, order):
        if order.role == OrderRole.STOP:
            self._emit(OrderUpdate(order.order_id, order.tag, OrderStatus.REJECTED, 0, 0, self.clock.now(), "RMS: rejected"))
            return
        super().place(order)


def test_F9_stop_placement_failure_exits_immediately():
    insts, data, _ = generate(SYN)
    res = run_backtest(insts, data, INDEX_KEY, strategies, cfg(), execution_factory=StopRejectingSim)
    assert res.trades, "need trades to exercise the fault"
    assert any("protective stop failed twice" in a for a in res.anomalies)
    for t in res.trades:
        assert t["exit_reasons"][0]["code"] in ("STOP_PLACEMENT_FAILED", "STOP_ALREADY_THROUGH")
        assert t["holding_s"] <= 120            # unprotected exposure is minimal
    assert res.eod_residuals == []


def test_metrics_known_sequence():
    assert _drawdown([100, -50, -70, 30, 200]) == (120, 3)
    assert _streaks([1, 2, -1, -1, -1, 3]) == (2, 3)
    ts = datetime(2025, 1, 1, 10, 0)
    trades = [dict(net_pnl=p, gross_pnl=p + 50, holding_s=60, risk_rupees=100, r_net=p / 100, r_gross=(p + 50) / 100,
                   slippage_rupees=1.0, entry_slippage_bps=1.0, exit_slippage_bps=1.0, costs=50, entry_ts=ts)
              for p in (100, -50, 150, -50)]
    s = summarize(trades)
    assert s["win_rate"] == 0.5 and s["profit_factor"] == pytest.approx(2.5) and s["expectancy"] == pytest.approx(37.5)
    assert s["payoff_ratio"] == pytest.approx(2.5) and s["cost_per_trade"] == 50
    assert summarize(trades, "gross_pnl")["net_profit"] == pytest.approx(350)


def test_required_sample_size():
    assert required_sample(0.1, 1.2) == pytest.approx(900)
    assert required_sample(-0.1, 1.0) == float("inf")
