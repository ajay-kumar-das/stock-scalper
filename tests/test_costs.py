import pytest

from scalper.core.types import Side
from scalper.costs.upstox import UpstoxCostModel

cm = UpstoxCostModel()


def test_round_trip_one_lakh_hand_computed():
    # 100 sh @ Rs 1000 each way: brokerage 20+20, STT 25, txn 3.07*2, SEBI 0.10*2, stamp 3,
    # GST 18% of (40 + 6.14 + 0.20) = 8.3412  -> 82.68
    assert cm.round_trip(100, 1000.0, 1000.0) == pytest.approx(82.68, abs=0.01)


def test_brokerage_is_min_of_flat_and_pct():
    small = cm.order_charges(Side.BUY, 10_000)        # 0.1% = Rs 10 < Rs 20
    big = cm.order_charges(Side.BUY, 500_000)         # capped at Rs 20
    assert small.brokerage == pytest.approx(10.0)
    assert big.brokerage == pytest.approx(20.0)


def test_stt_only_on_sell_and_stamp_only_on_buy():
    b = cm.order_charges(Side.BUY, 100_000)
    s = cm.order_charges(Side.SELL, 100_000)
    assert b.stt == 0 and s.stt == pytest.approx(25.0)
    assert s.stamp == 0 and b.stamp == pytest.approx(3.0)


@pytest.mark.parametrize("notional,expected_bps", [(20_000, 27.15), (50_000, 12.99), (100_000, 8.27), (500_000, 4.49)])
def test_cost_floor_table_matches_design_doc(notional, expected_bps):
    assert cm.round_trip_bps(notional) == pytest.approx(expected_bps, abs=0.02)


def test_zero_turnover_costs_nothing():
    assert cm.order_charges(Side.SELL, 0).total == 0
