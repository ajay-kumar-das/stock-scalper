"""H1 — Stocks-in-play opening-range breakout (doc 05 §7). Long only.

Hypothesis: stocks with abnormal early participation (high relative volume) continue in the
direction of an opening-range break, because information is absorbed gradually and large
participants split orders over the session. The relative-volume filter is essential: published
replications find the edge disappears after costs without it.

Free parameters (<= 3): or_minutes, min_rvol, target_r. Everything else is fixed by rationale.
"""
from __future__ import annotations

from datetime import time
from typing import Optional

from ..core.prices import round_down_to_tick, round_up_to_tick
from ..core.types import Bar, Candidate, Instrument, Reason
from ..features.intraday import FeatureState, minute_of_session
from .base import ExitAdvice, Strategy, StrategyContext


def _ramp(x: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 1.0 if x >= hi else 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


class OpeningRangeBreakout(Strategy):
    name = "orb_inplay"
    version = "1.0.0"
    hypothesis = "In-play stocks continue after breaking the opening range (H1)"
    t_expect_min = 15
    t_max_min = 45
    max_stop_pct = 0.012
    max_trades_per_day = 6
    latest_entry = time(11, 30)   # ORB is an opening-session phenomenon

    def __init__(self, or_minutes: int = 15, min_rvol: float = 2.0, target_r: float = 2.0,
                 prior_win_rate: float = 0.42, min_breakout_vol: float = 1.5, min_close_loc: float = 0.6) -> None:
        self.min_breakout_vol = min_breakout_vol   # fixed by rationale (real demand vs drift)
        self.min_close_loc = min_close_loc         # fixed by rationale (buyers in control at trigger)
        self.or_minutes = or_minutes
        self.min_rvol = min_rvol
        self.target_r = target_r
        self.prior_win_rate = prior_win_rate   # conservative prior until calibration exists (doc 07 §5)
        self.earliest_entry = time(9, 15 + or_minutes) if or_minutes < 45 else time(10, 0)
        self._fired: set[tuple[str, object]] = set()

    def new_day(self) -> None:
        self._fired.clear()

    def on_bar(self, inst: Instrument, bar: Bar, f: FeatureState, ctx: StrategyContext) -> Optional[Candidate]:
        if not f.or_complete or minute_of_session(bar.ts) < self.or_minutes:
            return None
        if (inst.key, bar.ts.date()) in self._fired:
            return None
        rv = f.rvol()
        if rv is None or rv < self.min_rvol:
            return None
        # Trigger: close above OR high on this bar (first break), not already extended
        if not (bar.c > f.or_high and (f.closes[-2] if len(f.closes) >= 2 else bar.o) <= f.or_high):
            return None
        rng = bar.h - bar.l
        close_loc = (bar.c - bar.l) / rng if rng else 0.5
        vol_ratio = bar.v / f.vol_avg20 if f.vol_avg20 else 0.0
        vwap = f.vwap
        if bar.c <= vwap or close_loc < self.min_close_loc or vol_ratio < self.min_breakout_vol:
            return None
        atr = f.atr or rng
        tick = inst.tick
        # Exhaustion checks (doc 07 §6): don't buy a move that has already happened
        datr = f.daily_atr_pct()
        if datr and f.ret_since_open() > 3 * datr:
            return None
        if len(f.closes) >= 6 and len(f.highs) >= 5:
            c, h, l = list(f.closes)[-6:], list(f.highs)[-5:], list(f.lows)[-5:]
            ranges = [hh - ll for hh, ll in zip(h, l)]
            if all(c[i] > c[i - 1] for i in range(1, 6)) and all(ranges[i] > ranges[i - 1] for i in range(1, 5)):
                return None   # five straight up-bars with expanding range = climax, not a base breakout
        entry_ideal = bar.c
        entry_max = round_down_to_tick(int(min(bar.c + 0.25 * atr, bar.c * 1.0015)), tick)
        or_mid = (f.or_high + f.or_low) / 2
        noise_floor = bar.c - max(1.2 * f.median_tr(), 4 * tick)
        stop = round_down_to_tick(int(min(or_mid, noise_floor)), tick)
        risk = entry_max - stop
        if risk <= 0:
            return None
        target = round_up_to_tick(int(entry_max + self.target_r * risk), tick)
        rs = f.ret_since_open() - ctx.index.ret_since_open()
        evidence = {
            "rvol": round(rv, 2), "breakout_vol_ratio": round(vol_ratio, 2), "close_location": round(close_loc, 2),
            "rel_strength": round(rs, 4), "above_vwap_atr": round((bar.c - vwap) / atr, 2) if atr else 0.0,
            "efficiency": round(f.efficiency_ratio(), 2),
        }
        score = (0.30 * _ramp(rv, self.min_rvol, 5.0) + 0.20 * _ramp(vol_ratio, 1.5, 4.0)
                 + 0.15 * _ramp(close_loc, 0.6, 1.0) + 0.20 * _ramp(rs, 0.0, 0.02)
                 + 0.15 * _ramp(f.efficiency_ratio(), 0.2, 0.6))
        self._fired.add((inst.key, bar.ts.date()))
        reasons = [
            Reason("ORB_BREAK", f"close {bar.c/100:.2f} broke OR high {f.or_high/100:.2f}",
                   {"or_high": f.or_high, "or_low": f.or_low, "or_minutes": self.or_minutes}),
            Reason("IN_PLAY", f"relative volume {rv:.2f}x (min {self.min_rvol})", {"rvol": rv}),
            Reason("CONFIRM", f"breakout bar volume {vol_ratio:.1f}x avg, close in top {100-close_loc*100:.0f}%, above VWAP {vwap/100:.2f}", {}),
        ]
        return Candidate(key=inst.key, strategy=self.name, strategy_version=self.version, ts=bar.end,
                         entry_ideal=entry_ideal, entry_max=entry_max, stop=stop, target=target,
                         confidence=round(score, 3), prior_win_rate=self.prior_win_rate,
                         expected_hold_min=self.t_expect_min, evidence=evidence, reasons=reasons,
                         setup_id=f"{self.name}:{inst.symbol}:{bar.ts.date()}:{f.or_high}")

    def thesis_check(self, inst: Instrument, bar: Bar, f: FeatureState, entry: float, ctx: StrategyContext) -> ExitAdvice:
        # Invalidation must be robust to ordinary noise (same logic as stop placement, doc 07 §7):
        # two consecutive 1-min closes back inside the range by more than a noise buffer, or a close
        # below VWAP by 0.3 ATR. The protective stop (OR midpoint / noise floor) handles fast failures.
        buf = max(0.5 * f.median_tr(), 2 * inst.tick)
        closes = list(f.closes)
        inside = [c < f.or_high - buf for c in closes[-2:]]
        if len(inside) == 2 and all(inside):
            return ExitAdvice(True, Reason("THESIS_INVALID", f"two 1-min closes back inside opening range "
                                           f"(last {bar.c/100:.2f} < OR high {f.or_high/100:.2f} - noise {buf/100:.2f})"))
        atr = f.atr or 0.0
        if bar.c < f.vwap - 0.3 * atr:
            return ExitAdvice(True, Reason("THESIS_INVALID", f"1-min close {bar.c/100:.2f} below VWAP {f.vwap/100:.2f} - 0.3 ATR"))
        return ExitAdvice(False)
