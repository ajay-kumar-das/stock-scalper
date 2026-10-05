"""Injectable clocks (ADR-2). Components must call clock.now(), never datetime.now()."""
from __future__ import annotations

from datetime import datetime

from .types import IST


class Clock:
    def now(self) -> datetime:  # pragma: no cover - interface
        raise NotImplementedError


class SimClock(Clock):
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("SimClock requires tz-aware datetime")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, t: datetime) -> None:
        if t < self._now:
            raise ValueError(f"clock cannot move backwards: {t} < {self._now}")
        self._now = t


class WallClock(Clock):
    def now(self) -> datetime:
        return datetime.now(IST)
