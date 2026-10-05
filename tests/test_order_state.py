import pytest

from scalper.core.types import OrderRole, OrderStatus as S, OrderType, Side
from scalper.oms.order import Order, OrderStateError

from .conftest import t


def mk(qty=100):
    return Order("o1", "sc-1-E", "K", Side.BUY, OrderRole.ENTRY, OrderType.LIMIT, qty, 10_000)


def test_happy_path_with_partial_fills_cumulative():
    o = mk()
    o.transition(S.SUBMITTED, t(9, 20))
    o.transition(S.ACKNOWLEDGED, t(9, 20))
    assert o.apply_cumulative_fill(40, 40 * 10_000, t(9, 21)) == 40
    assert o.status == S.PARTIALLY_FILLED
    assert o.apply_cumulative_fill(40, 40 * 10_000, t(9, 21)) == 0          # duplicate update: idempotent
    assert o.apply_cumulative_fill(100, 100 * 10_000, t(9, 21)) == 60
    assert o.status == S.FILLED and o.is_terminal


def test_illegal_transition_rejected():
    o = mk()
    with pytest.raises(OrderStateError):
        o.transition(S.FILLED, t(9, 20))
    o.transition(S.SUBMITTED, t(9, 20)); o.transition(S.REJECTED, t(9, 20))
    with pytest.raises(OrderStateError):
        o.transition(S.ACKNOWLEDGED, t(9, 20))


def test_filled_qty_never_decreases_and_never_overfills():
    o = mk()
    o.transition(S.SUBMITTED, t(9, 20)); o.transition(S.ACKNOWLEDGED, t(9, 20))
    o.apply_cumulative_fill(50, 500_000, t(9, 21))
    with pytest.raises(OrderStateError):
        o.apply_cumulative_fill(40, 400_000, t(9, 21))
    with pytest.raises(OrderStateError):
        o.apply_cumulative_fill(101, 1_010_000, t(9, 21))


def test_fill_before_ack_race_is_handled():
    o = mk()
    o.transition(S.SUBMITTED, t(9, 20))
    o.apply_cumulative_fill(100, 1_000_000, t(9, 20))
    assert o.status == S.FILLED


def test_late_fill_after_cancel_keeps_quantity():
    o = mk()
    o.transition(S.SUBMITTED, t(9, 20)); o.transition(S.ACKNOWLEDGED, t(9, 20)); o.transition(S.CANCELLED, t(9, 21))
    assert o.apply_cumulative_fill(30, 300_000, t(9, 21)) == 30
    assert o.status == S.CANCELLED and o.filled_qty == 30


def test_unknown_resolves_by_reconciliation():
    o = mk()
    o.transition(S.SUBMITTED, t(9, 20)); o.transition(S.UNKNOWN, t(9, 20), "no ack in 2s")
    o.transition(S.ACKNOWLEDGED, t(9, 20), "found by tag")
    assert o.is_working


def test_tag_length_limit():
    with pytest.raises(ValueError):
        Order("o1", "x" * 41, "K", Side.BUY, OrderRole.ENTRY, OrderType.LIMIT, 1, 1)
