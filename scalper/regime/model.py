"""Market regime model (doc 07 §2, FR-REG). Lagging by construction; strategies are evaluated
per regime label in research so that permission matrices are evidence-based."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from ..features.intraday import FeatureState


@dataclass(frozen=True)
class Regime:
    trend: str = "UNKNOWN"        # STRONG_UP / WEAK_UP / FLAT / DOWN
    vol: str = "UNKNOWN"          # LOW / NORMAL / HIGH / ABNORMAL
    breadth: str = "UNKNOWN"      # SUPPORTIVE / NEUTRAL / HOSTILE
    character: str = "UNKNOWN"    # TRENDING / MEAN_REVERTING / NEUTRAL
    values: dict = field(default_factory=dict, compare=False)

    @property
    def label(self) -> str:
        return f"{self.trend}/{self.vol}/{self.breadth}/{self.character}"

    @property
    def abnormal(self) -> bool:
        return self.vol == "ABNORMAL"


@dataclass
class RegimeConfig:
    strong_z: float = 1.0
    weak_z: float = 0.3
    low_vol: float = 0.7
    high_vol: float = 1.5
    abnormal_vol: float = 3.0
    abnormal_move_15m: float = 0.015
    breadth_supportive: float = 0.55
    breadth_hostile: float = 0.40
    vr_trending: float = 1.1
    vr_mean_rev: float = 0.9


class RegimeModel:
    def __init__(self, cfg: RegimeConfig | None = None) -> None:
        self.cfg = cfg or RegimeConfig()
        self.current = Regime()

    def update(self, index: FeatureState, universe: Iterable[FeatureState]) -> Regime:
        c = self.cfg
        typ = index.typical_abs_oc()
        ret = index.ret_since_open()
        if typ is None or index.last is None:
            self.current = Regime(values={"reason": "warming up"})
            return self.current
        z = ret / typ
        trend = "STRONG_UP" if z > c.strong_z else "WEAK_UP" if z > c.weak_z else "FLAT" if z >= -c.weak_z else "DOWN"
        vr_ = index.vol_ratio() or 1.0
        mv15 = abs(index.move_over(15))
        if vr_ > c.abnormal_vol or mv15 > c.abnormal_move_15m:
            vol = "ABNORMAL"
        elif vr_ > c.high_vol:
            vol = "HIGH"
        elif vr_ < c.low_vol:
            vol = "LOW"
        else:
            vol = "NORMAL"
        states = [f for f in universe if f.last is not None and f.cum_vol > 0]
        above = sum(1 for f in states if f.last.c > f.vwap)
        b = above / len(states) if states else 0.5
        breadth = "SUPPORTIVE" if b > c.breadth_supportive else "HOSTILE" if b < c.breadth_hostile else "NEUTRAL"
        var_ratio = index.variance_ratio()
        if var_ratio is None:
            character = "NEUTRAL"
        else:
            character = "TRENDING" if var_ratio > c.vr_trending else "MEAN_REVERTING" if var_ratio < c.vr_mean_rev else "NEUTRAL"
        self.current = Regime(trend, vol, breadth, character,
                              {"trend_z": round(z, 3), "vol_ratio": round(vr_, 3), "move15": round(mv15, 4),
                               "breadth": round(b, 3), "variance_ratio": var_ratio})
        return self.current
