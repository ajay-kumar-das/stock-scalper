"""Incremental per-instrument features. Updated only with bars that have closed (look-ahead safe).

Every feature has a stated hypothesis in the strategy/selection docs; nothing here is used
just because it is popular.
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Optional

from ..core.types import Bar

SESSION_OPEN = time(9, 15)
SESSION_MINUTES = 375


def minute_of_session(ts: datetime) -> int:
    return (ts.hour * 60 + ts.minute) - (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute)


@dataclass
class FeatureState:
    key: str
    or_minutes: int = 15
    atr_n: int = 14
    history_days: int = 20
    # --- cross-session state (prior sessions only) ---
    prior_cum_vol: deque = field(default_factory=lambda: deque(maxlen=20))     # list[list[int]] per session
    prior_daily_tr_pct: deque = field(default_factory=lambda: deque(maxlen=20))
    prior_traded_value: deque = field(default_factory=lambda: deque(maxlen=20))
    prior_session_atr: deque = field(default_factory=lambda: deque(maxlen=20))
    prior_abs_oc: deque = field(default_factory=lambda: deque(maxlen=20))      # |close/open - 1|
    prev_close: Optional[int] = None
    atr: Optional[float] = None
    # --- session state ---
    session: Optional[date] = None
    open: int = 0
    day_high: int = 0
    day_low: int = 0
    last: Optional[Bar] = None
    cum_vol: int = 0
    cum_pv: float = 0.0
    cum_value: float = 0.0
    or_high: int = 0
    or_low: int = 0
    or_complete: bool = False
    cum_vol_by_min: list = field(default_factory=list)
    closes: deque = field(default_factory=lambda: deque(maxlen=61))
    highs: deque = field(default_factory=lambda: deque(maxlen=31))
    lows: deque = field(default_factory=lambda: deque(maxlen=31))
    vols: deque = field(default_factory=lambda: deque(maxlen=21))
    trs: deque = field(default_factory=lambda: deque(maxlen=30))
    atr_sum_session: float = 0.0
    n_bars: int = 0
    absdiffs: deque = field(default_factory=lambda: deque(maxlen=30))
    absdiff_sum: int = 0
    rvol_ref: list = field(default_factory=list)       # mean prior cumulative volume per minute (prior sessions)

    # ------------------------------------------------------------------ update
    def update(self, bar: Bar) -> None:
        d = bar.ts.date()
        if self.session != d:
            self._roll_session(d, bar)
        prev_c = self.last.c if self.last else (self.prev_close or bar.o)
        tr = max(bar.h - bar.l, abs(bar.h - prev_c), abs(bar.l - prev_c))
        self.atr = float(tr) if self.atr is None else (self.atr * (self.atr_n - 1) + tr) / self.atr_n
        self.trs.append(tr)
        self.atr_sum_session += self.atr
        self.day_high = max(self.day_high, bar.h)
        self.day_low = min(self.day_low, bar.l)
        typical = (bar.h + bar.l + bar.c) / 3.0
        self.cum_vol += bar.v
        self.cum_pv += typical * bar.v
        self.cum_value += typical * bar.v / 100.0
        m = minute_of_session(bar.ts)
        while len(self.cum_vol_by_min) < m:
            self.cum_vol_by_min.append(self.cum_vol - bar.v)
        if len(self.cum_vol_by_min) == m:
            self.cum_vol_by_min.append(self.cum_vol)
        if m < self.or_minutes:
            self.or_high = max(self.or_high, bar.h) if self.or_high else bar.h
            self.or_low = min(self.or_low, bar.l) if self.or_low else bar.l
        if m >= self.or_minutes - 1:
            self.or_complete = True
        if self.closes:
            if len(self.absdiffs) == self.absdiffs.maxlen:
                self.absdiff_sum -= self.absdiffs[0]
            dlt = abs(bar.c - self.closes[-1])
            self.absdiffs.append(dlt)
            self.absdiff_sum += dlt
        self.closes.append(bar.c)
        self.highs.append(bar.h)
        self.lows.append(bar.l)
        self.vols.append(bar.v)
        self.last = bar
        self.n_bars += 1

    def _roll_session(self, d: date, bar: Bar) -> None:
        if self.session is not None and self.last is not None:
            self.prior_cum_vol.append(list(self.cum_vol_by_min))
            if self.prev_close:
                tr = max(self.day_high, self.prev_close) - min(self.day_low, self.prev_close)
                self.prior_daily_tr_pct.append(tr / self.prev_close)
            self.prior_traded_value.append(self.cum_value)
            if self.n_bars:
                self.prior_session_atr.append(self.atr_sum_session / self.n_bars)
            if self.open:
                self.prior_abs_oc.append(abs(self.last.c / self.open - 1))
            self.prev_close = self.last.c
        self.session = d
        self.open = bar.o
        self.day_high = bar.h
        self.day_low = bar.l
        self.cum_vol = 0
        self.cum_pv = 0.0
        self.cum_value = 0.0
        self.or_high = self.or_low = 0
        self.or_complete = False
        self.cum_vol_by_min = []
        self.closes.clear(); self.highs.clear(); self.lows.clear(); self.vols.clear(); self.trs.clear()
        self.absdiffs.clear(); self.absdiff_sum = 0
        self.rvol_ref = []
        if len(self.prior_cum_vol) >= 5:
            for m in range(max(len(x) for x in self.prior_cum_vol)):
                ref = [x[m] for x in self.prior_cum_vol if len(x) > m and x[m] > 0]
                self.rvol_ref.append(sum(ref) / len(ref) if len(ref) >= 5 else 0.0)
        self.atr_sum_session = 0.0
        self.n_bars = 0
        self.last = None

    # ------------------------------------------------------------------ derived
    @property
    def vwap(self) -> float:
        return self.cum_pv / self.cum_vol if self.cum_vol else float(self.last.c if self.last else 0)

    @property
    def vol_avg20(self) -> float:
        v = list(self.vols)[:-1]
        return sum(v) / len(v) if v else 0.0

    def rvol(self) -> Optional[float]:
        """Cumulative volume today vs mean cumulative volume at the same minute (prior sessions)."""
        if not self.rvol_ref or not self.cum_vol_by_min:
            return None
        m = len(self.cum_vol_by_min) - 1
        ref = self.rvol_ref[m] if m < len(self.rvol_ref) else 0.0
        return self.cum_vol / ref if ref > 0 else None

    def efficiency_ratio(self, n: int = 30) -> float:
        if n == 30 and len(self.closes) >= 3:
            first = self.closes[-(len(self.absdiffs) + 1)]
            return abs(self.closes[-1] - first) / self.absdiff_sum if self.absdiff_sum else 0.0
        c = list(self.closes)[-(n + 1):]
        if len(c) < 3:
            return 0.0
        path = sum(abs(c[i] - c[i - 1]) for i in range(1, len(c)))
        return abs(c[-1] - c[0]) / path if path else 0.0

    def median_tr(self) -> float:
        return statistics.median(self.trs) if self.trs else 0.0

    def daily_atr_pct(self) -> Optional[float]:
        if len(self.prior_daily_tr_pct) < 5:
            return None
        return sum(self.prior_daily_tr_pct) / len(self.prior_daily_tr_pct)

    def adv_value(self) -> Optional[float]:
        if len(self.prior_traded_value) < 5:
            return None
        return statistics.median(self.prior_traded_value)

    def ret_since_open(self) -> float:
        return (self.last.c / self.open - 1) if (self.last and self.open) else 0.0

    def typical_abs_oc(self) -> Optional[float]:
        if len(self.prior_abs_oc) < 5:
            return None
        return max(1e-4, sum(self.prior_abs_oc) / len(self.prior_abs_oc))

    def vol_ratio(self) -> Optional[float]:
        if len(self.prior_session_atr) < 5 or self.atr is None:
            return None
        base = sum(self.prior_session_atr) / len(self.prior_session_atr)
        return self.atr / base if base else None

    def move_over(self, n: int) -> float:
        c = list(self.closes)
        if len(c) <= n:
            return 0.0
        return c[-1] / c[-1 - n] - 1

    def variance_ratio(self, q: int = 5, n: int = 60) -> Optional[float]:
        c = list(self.closes)[-(n + 1):]
        if len(c) < 3 * q:
            return None
        r1 = [c[i] / c[i - 1] - 1 for i in range(1, len(c))]
        rq = [c[i] / c[i - q] - 1 for i in range(q, len(c))]
        def _var(x):
            mu = sum(x) / len(x)
            return sum((y - mu) ** 2 for y in x) / len(x)
        v1 = _var(r1)
        vq = _var(rq)
        return vq / (q * v1) if v1 > 0 else None
