"""Core domain types.

Prices are integer paise (1 rupee = 100 paise) everywhere in OMS, execution,
costs and P&L to avoid floating-point tick errors (ADR-5). Features may use
floats internally.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))
BAR_SECONDS = 60


def rupees(paise: int | float) -> float:
    return paise / 100.0


def paise(rupee_value: float) -> int:
    return int(round(rupee_value * 100))


class Mode(str, Enum):
    BACKTEST = "BACKTEST"
    REPLAY = "REPLAY"
    PAPER = "PAPER"
    LIVE = "LIVE"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    SL = "SL"        # stop-limit
    SL_M = "SL-M"    # stop-market (carries mandatory market protection at Upstox)


class OrderRole(str, Enum):
    ENTRY = "ENTRY"
    STOP = "STOP"     # broker-resident protective stop
    EXIT = "EXIT"     # only used when no protective stop exists


class OrderStatus(str, Enum):
    INTENDED = "INTENDED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


TERMINAL_STATUSES = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
)
WORKING_STATUSES = frozenset(
    {OrderStatus.SUBMITTED, OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN}
)


@dataclass(frozen=True)
class Instrument:
    key: str                  # e.g. "NSE_EQ|INE002A01018"
    symbol: str               # e.g. "RELIANCE"
    tick: int = 5             # tick size in paise (loaded from instrument master in M2)
    sector: str = "UNKNOWN"
    fo_eligible: bool = True
    cas_eligible: bool = False
    is_index: bool = False
    mis_margin_pct: float = 0.20


@dataclass(frozen=True)
class Bar:
    """A 1-minute bar. `ts` is the bar START time (tz-aware IST).

    The bar is only knowable at `end` (= ts + 60s). The backtest engine releases
    it to features/strategies at `end`, never earlier (look-ahead control).
    """
    key: str
    ts: datetime
    o: int
    h: int
    l: int
    c: int
    v: int

    @property
    def end(self) -> datetime:
        return self.ts + timedelta(seconds=BAR_SECONDS)

    def __post_init__(self) -> None:
        if not (self.l <= min(self.o, self.c) and self.h >= max(self.o, self.c) and self.l > 0 and self.v >= 0):
            raise ValueError(f"inconsistent bar {self}")


@dataclass(frozen=True)
class Reason:
    code: str
    text: str
    values: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "text": self.text, "values": self.values}


@dataclass
class Candidate:
    """A strategy's proposal. Strategies never place orders themselves (invariant 6)."""
    key: str
    strategy: str
    strategy_version: str
    ts: datetime
    entry_ideal: int
    entry_max: int
    stop: int
    target: int
    confidence: float
    prior_win_rate: float
    expected_hold_min: int
    evidence: dict[str, float]
    reasons: list[Reason]
    setup_id: str

    @property
    def risk_per_share(self) -> int:
        return self.entry_max - self.stop
