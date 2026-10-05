"""Strategy plug-in interface (FR-SIG-1). Strategies only *propose*; they cannot place orders."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import time
from typing import Optional

from ..core.types import Bar, Candidate, Instrument, Reason
from ..features.intraday import FeatureState
from ..regime.model import Regime


@dataclass
class StrategyContext:
    index: FeatureState
    regime: Regime


@dataclass
class ExitAdvice:
    exit_now: bool
    reason: Optional[Reason] = None


class Strategy(ABC):
    name: str = "base"
    version: str = "0"
    hypothesis: str = ""
    earliest_entry: time = time(9, 20)
    latest_entry: time = time(14, 45)
    t_expect_min: int = 10
    t_max_min: int = 30
    max_stop_pct: float = 0.01
    max_trades_per_day: int = 6

    # Regime permission matrix: sets of allowed labels per axis
    allowed_trend: frozenset = frozenset({"STRONG_UP", "WEAK_UP", "FLAT"})
    allowed_vol: frozenset = frozenset({"NORMAL", "HIGH"})
    allowed_breadth: frozenset = frozenset({"SUPPORTIVE", "NEUTRAL"})
    allowed_character: frozenset = frozenset({"TRENDING", "NEUTRAL"})

    def permitted(self, r: Regime) -> tuple[bool, str]:
        if r.abnormal:
            return False, "ABNORMAL regime"
        for axis, allowed, val in (("trend", self.allowed_trend, r.trend), ("vol", self.allowed_vol, r.vol),
                                   ("breadth", self.allowed_breadth, r.breadth),
                                   ("character", self.allowed_character, r.character)):
            if val not in allowed:
                return False, f"{axis}={val} not permitted"
        return True, "ok"

    @abstractmethod
    def on_bar(self, inst: Instrument, bar: Bar, f: FeatureState, ctx: StrategyContext) -> Optional[Candidate]:
        """Called at bar close for watchlist instruments. Return a candidate or None."""

    @abstractmethod
    def thesis_check(self, inst: Instrument, bar: Bar, f: FeatureState, entry: float, ctx: StrategyContext) -> ExitAdvice:
        """Is the original reason for the trade still valid?"""

    def new_day(self) -> None:
        pass
