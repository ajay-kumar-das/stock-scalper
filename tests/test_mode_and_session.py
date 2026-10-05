from datetime import date

import pytest

from scalper.core.types import Mode
from scalper.session.controller import SessionController, SessionState
from scalper.session.mode_guard import LIVE_CONFIRM_ENV, ModeGuardError, build_execution, resolve_mode

from .conftest import t

TODAY = date(2025, 3, 3)
LIVE_CFG = {"mode": "LIVE", "live": {"capital_allocation": 50_000, "i_understand_real_money_is_at_risk": True}}
ENV = {LIVE_CONFIRM_ENV: TODAY.isoformat()}


def test_live_with_all_confirmations():
    assert resolve_mode(LIVE_CFG, ENV, TODAY, token_valid=True, preflight_ok=True).mode == Mode.LIVE


@pytest.mark.parametrize("mutate", [
    lambda c, e: e.pop(LIVE_CONFIRM_ENV),
    lambda c, e: e.update({LIVE_CONFIRM_ENV: "2025-03-02"}),          # stale confirmation from yesterday
    lambda c, e: c["live"].update({"capital_allocation": 0}),
    lambda c, e: c["live"].update({"suspended": True}),
    lambda c, e: c["live"].pop("i_understand_real_money_is_at_risk"),
])
def test_live_refused_when_any_check_missing(mutate):
    import copy
    c, e = copy.deepcopy(LIVE_CFG), dict(ENV)
    mutate(c, e)
    with pytest.raises(ModeGuardError):
        resolve_mode(c, e, TODAY, token_valid=True, preflight_ok=True)


def test_live_refused_without_token_or_preflight():
    with pytest.raises(ModeGuardError):
        resolve_mode(LIVE_CFG, ENV, TODAY, token_valid=False, preflight_ok=True)
    with pytest.raises(ModeGuardError):
        resolve_mode(LIVE_CFG, ENV, TODAY, token_valid=True, preflight_ok=False)


def test_typo_mode_and_confused_env_refused():
    with pytest.raises(ModeGuardError):
        resolve_mode({"mode": "LIVEE"}, {}, TODAY)
    with pytest.raises(ModeGuardError):
        resolve_mode({"mode": "PAPER"}, ENV, TODAY)


def test_paper_never_constructs_live_adapter():
    def live_factory():
        raise AssertionError("live adapter must not be constructed in PAPER")
    assert build_execution(Mode.PAPER, sim_factory=lambda: "sim", live_factory=live_factory) == "sim"


def test_session_timeline():
    s = SessionController()
    s.start_day(t(9, 0))
    assert s.entries_allowed(t(9, 16))[0] is False           # before earliest entry
    assert s.entries_allowed(t(9, 25))[0] is True
    assert s.entries_allowed(t(14, 46))[0] is False and s.state == SessionState.NO_NEW_ENTRIES
    assert s.tick(t(15, 1)) == SessionState.FLATTENING


def test_manual_and_risk_controls_take_precedence():
    s = SessionController()
    s.start_day(t(9, 0))
    s.pause(t(10, 0)); assert s.entries_allowed(t(10, 0))[0] is False
    s.resume(t(10, 1)); assert s.entries_allowed(t(10, 1))[0] is True
    s.enter_defensive(t(10, 2), "FEED_STALE"); assert s.state == SessionState.DEFENSIVE
    s.clear_defensive(t(10, 3), "FEED_STALE"); assert s.state == SessionState.TRADING
    s.halt_for_day(t(11, 0), "daily loss"); s.resume(t(11, 1))
    assert s.state == SessionState.HALTED_FOR_DAY                    # resume cannot undo a risk halt
    s.emergency_stop(t(11, 2)); assert s.state == SessionState.EMERGENCY
