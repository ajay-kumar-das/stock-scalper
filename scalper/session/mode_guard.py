"""LIVE-mode guard (FR-MODE-2/3, failure scenario F22).

LIVE starts only if *every* confirmation is present. Any missing item refuses to start; there is
no fallback to another mode, because silently falling back hides configuration mistakes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping

from ..core.types import Mode

LIVE_CONFIRM_ENV = "SCALPER_LIVE_CONFIRM"


class ModeGuardError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModeDecision:
    mode: Mode
    checks: dict[str, bool]


def resolve_mode(config: Mapping, env: Mapping[str, str], today: date, *, token_valid: bool = False,
                 preflight_ok: bool = False) -> ModeDecision:
    raw = str(config.get("mode", "")).strip().upper()
    if raw not in Mode.__members__:
        raise ModeGuardError(f"invalid or missing mode: {raw!r} (must be one of {list(Mode.__members__)})")
    mode = Mode[raw]
    if mode != Mode.LIVE:
        if env.get(LIVE_CONFIRM_ENV):
            # A live confirmation with a non-live config is a sign of confusion: refuse.
            raise ModeGuardError(f"{LIVE_CONFIRM_ENV} is set but config mode is {mode.value}; refusing to guess intent")
        return ModeDecision(mode, {"non_live": True})
    live = config.get("live", {}) or {}
    checks = {
        "env_confirm_matches_today": env.get(LIVE_CONFIRM_ENV, "") == today.isoformat(),
        "capital_allocation_positive": float(live.get("capital_allocation", 0) or 0) > 0,
        "not_suspended": not bool(live.get("suspended", False)),
        "token_valid": bool(token_valid),
        "preflight_ok": bool(preflight_ok),
        "explicit_ack": live.get("i_understand_real_money_is_at_risk") is True,
    }
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        raise ModeGuardError(f"LIVE refused; failed checks: {failed}")
    return ModeDecision(mode, checks)


def build_execution(mode: Mode, *, sim_factory, live_factory=None):
    """Only LIVE may construct the live adapter; other modes never instantiate it (FR-MODE-3)."""
    if mode == Mode.LIVE:
        if live_factory is None:
            raise ModeGuardError("LIVE execution adapter not available in this build")
        return live_factory()
    return sim_factory()
