import pytest

from scalper.risk.engine import OpenRisk, RiskConfig, RiskEngine, RiskState

from .conftest import t


def eng(**kw):
    return RiskEngine(RiskConfig(capital=300_000, **kw))


def size(r, entry=100_000, stop=99_400, vol=1e9, open_risks=(), conf=1.0, regime=1.0, sector="IT", strategy="s"):
    return r.size(strategy=strategy, sector=sector, entry=entry, stop=stop, stop_slippage=5, est_round_trip_cost=0.0,
                  confidence_mult=conf, regime_mult=regime, avg_1min_volume=vol, open_risks=list(open_risks),
                  margin_pct=0.2)


def test_size_respects_risk_budget():
    r = eng()
    s = size(r)                                          # budget 750; per-share risk 6.05 -> 123 sh
    assert s.qty == 123 and s.limiting == "risk"
    assert s.qty * 6.05 <= 750


def test_multipliers_cannot_increase_risk():
    r = eng()
    assert size(r, conf=3.0, regime=5.0).qty == size(r).qty


def test_cost_floor_rejects_tiny_trades():
    r = eng()
    s = size(r, entry=100_000, stop=90_000)              # 10% stop -> tiny size
    assert s.rejection and s.rejection.code == "BELOW_COST_FLOOR"


def test_liquidity_cap():
    s = size(eng(), vol=5_000)                            # 1% of 1-min volume = 50 shares
    assert s.limiting == "liquidity" and s.qty == 50


def test_position_and_sector_limits():
    r = eng(max_positions=2)
    ors = [OpenRisk("A", "IT", 50_000, 500, "s"), OpenRisk("B", "BANK", 50_000, 500, "s")]
    assert size(r, open_risks=ors).rejection.code == "MAX_POSITIONS"
    r = eng()
    assert size(r, open_risks=ors[:1]).rejection.code == "SECTOR_LIMIT"


def test_daily_loss_projection_blocks_trade_that_could_breach_limit():
    r = eng()
    r.realized_net = -2_600                                # hard limit is -3,000
    s = size(r)                                            # room = 3,000 - 2,600 = 400 -> 66 sh
    assert s.rejection is None and s.qty == 66


def test_hard_daily_loss_halts_for_day():
    r = eng()
    r.on_trade_closed("s", "A", -3_100, t(11, 0))
    assert r.state == RiskState.HALTED_FOR_DAY
    assert r.entry_allowed("s", "B", 6, t(11, 1)).code == "RISK_HALTED"


def test_consecutive_losses_cooldown_then_halt():
    r = eng()
    for i in range(4):
        r.on_trade_closed(f"s{i}", "A", -10, t(10, i))
    assert r.entry_allowed("x", "B", 6, t(10, 5)).code == "RISK_COOLDOWN"
    r.on_trade_closed("s9", "A", -10, t(10, 6)); r.on_trade_closed("s8", "A", -10, t(10, 7))
    assert r.state == RiskState.HALTED_FOR_DAY


def test_strategy_consecutive_losses_cooldown_and_half_size():
    r = eng()
    for i in range(3):
        r.on_trade_closed("orb", "A", -10, t(10, i))
    assert r.entry_allowed("orb", "B", 6, t(10, 5)).code == "STRATEGY_COOLDOWN"
    assert size(r, strategy="orb").qty <= size(eng(), strategy="orb").qty // 2 + 1


def test_no_revenge_sizing_after_bad_day():
    r = eng()
    base = size(r).qty
    r.on_trade_closed("s", "A", -2_000, t(11, 0))          # soft loss
    r.new_session()
    assert size(r).qty <= base // 2 + 1                     # next day starts at half size
    r.new_session(); r.new_session()
    assert size(r).qty == base                              # recovers stepwise, never above base


def test_drawdown_stop():
    r = eng()
    r.on_trade_closed("s", "A", 1_000, t(10, 0))
    r.new_session()
    r.cum_net = 1_000 - 25_000                              # > 8% of capital below peak
    assert size(r).rejection.code == "MAX_DRAWDOWN"


def test_config_validation():
    with pytest.raises(AssertionError):
        RiskEngine(RiskConfig(risk_per_trade_pct=0.05))
