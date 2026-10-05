"""Account-level risk engine and position sizing (doc 04).

Design rule: no multiplier can exceed 1.0, and multipliers recover only at session
boundaries one step at a time (anti-revenge, FR-RISK-4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ..core.types import Reason


class RiskState(str, Enum):
    NORMAL = "NORMAL"
    SOFT_LOSS = "SOFT_LOSS"
    COOLDOWN = "COOLDOWN"
    HALTED_FOR_DAY = "HALTED_FOR_DAY"


@dataclass
class RiskConfig:
    capital: float = 300_000.0
    risk_per_trade_pct: float = 0.0025
    leverage_cap: float = 2.0
    max_position_frac: float = 0.25          # of capital x leverage cap
    max_positions: int = 3
    max_per_sector: int = 1
    max_open_risk_pct: float = 0.0075
    daily_loss_hard_pct: float = 0.010
    daily_loss_soft_pct: float = 0.006
    max_consec_losses_cooldown: int = 4
    max_consec_losses_halt: int = 6
    cooldown_minutes: int = 30
    strategy_consec_losses: int = 3
    strategy_cooldown_minutes: int = 60
    max_trades_day: int = 20
    max_trades_per_instrument: int = 2
    min_order_notional: float = 40_000.0
    liquidity_frac_1min_vol: float = 0.01
    margin_buffer: float = 0.8
    soft_loss_conf_bump: float = 0.10

    def validate(self) -> None:
        assert 0 < self.risk_per_trade_pct <= 0.01, "risk per trade must be in (0, 1%]"
        assert 0 < self.daily_loss_hard_pct <= 0.03, "daily hard loss must be in (0, 3%]"
        assert self.daily_loss_soft_pct < self.daily_loss_hard_pct
        assert 1.0 <= self.leverage_cap <= 5.0
        assert self.max_positions >= 1 and self.capital > 0


@dataclass
class SizingResult:
    qty: int
    limiting: str
    detail: dict
    rejection: Optional[Reason] = None


@dataclass
class OpenRisk:
    key: str
    sector: str
    notional: float
    risk_rupees: float
    strategy: str


@dataclass
class RiskEngine:
    cfg: RiskConfig
    state: RiskState = RiskState.NORMAL
    realized_net: float = 0.0
    unrealized: float = 0.0
    consec_losses: int = 0
    strat_consec: dict = field(default_factory=dict)
    strat_cooldown_until: dict = field(default_factory=dict)
    strat_mult: dict = field(default_factory=dict)
    cooldown_until: object = None
    trades_today: int = 0
    trades_by_key: dict = field(default_factory=dict)
    trades_by_strategy: dict = field(default_factory=dict)
    m_dd: float = 1.0
    m_perf_carry: dict = field(default_factory=dict)
    halt_reason: str = ""
    events: list = field(default_factory=list)
    equity_peak: float = 0.0
    cum_net: float = 0.0
    soft_hit_today: bool = False
    max_drawdown_stop_pct: float = 0.08
    drawdown_half_size_pct: float = 0.05

    def __post_init__(self) -> None:
        self.cfg.validate()

    # ------------------------------------------------------------------ state
    @property
    def day_pnl(self) -> float:
        return self.realized_net + self.unrealized

    def _set_state(self, s: RiskState, why: str) -> None:
        if s in (RiskState.SOFT_LOSS, RiskState.HALTED_FOR_DAY):
            self.soft_hit_today = True
        if s != self.state:
            self.events.append((s.value, why))
            self.state = s

    def update_marks(self, unrealized: float, now) -> None:
        self.unrealized = unrealized
        self._evaluate(now)

    def _evaluate(self, now) -> None:
        c = self.cfg
        if self.state == RiskState.HALTED_FOR_DAY:
            return
        if self.day_pnl <= -c.daily_loss_hard_pct * c.capital:
            self.halt_reason = f"daily loss {self.day_pnl:.0f} breached hard limit {-c.daily_loss_hard_pct*c.capital:.0f}"
            self._set_state(RiskState.HALTED_FOR_DAY, self.halt_reason)
            return
        if self.consec_losses >= c.max_consec_losses_halt:
            self.halt_reason = f"{self.consec_losses} consecutive losses"
            self._set_state(RiskState.HALTED_FOR_DAY, self.halt_reason)
            return
        if self.cooldown_until is not None and now < self.cooldown_until:
            self._set_state(RiskState.COOLDOWN, "consecutive-loss cooldown")
            return
        if self.day_pnl <= -c.daily_loss_soft_pct * c.capital:
            self._set_state(RiskState.SOFT_LOSS, f"day P&L {self.day_pnl:.0f} below soft limit")
            return
        self._set_state(RiskState.NORMAL, "limits ok")

    def on_trade_closed(self, strategy: str, key: str, net: float, now) -> None:
        from datetime import timedelta
        c = self.cfg
        self.realized_net += net
        self.cum_net += net
        self.equity_peak = max(self.equity_peak, self.cum_net)
        if net < 0:
            self.consec_losses += 1
            self.strat_consec[strategy] = self.strat_consec.get(strategy, 0) + 1
            if self.consec_losses == c.max_consec_losses_cooldown:
                self.cooldown_until = now + timedelta(minutes=c.cooldown_minutes)
            if self.strat_consec[strategy] >= c.strategy_consec_losses:
                self.strat_cooldown_until[strategy] = now + timedelta(minutes=c.strategy_cooldown_minutes)
                self.strat_mult[strategy] = 0.5
                self.strat_consec[strategy] = 0
        else:
            self.consec_losses = 0
            self.strat_consec[strategy] = 0
        self._evaluate(now)

    def record_entry(self, strategy: str, key: str) -> None:
        self.trades_today += 1
        self.trades_by_key[key] = self.trades_by_key.get(key, 0) + 1
        self.trades_by_strategy[strategy] = self.trades_by_strategy.get(strategy, 0) + 1

    def new_session(self) -> None:
        """Multipliers recover one step per session, never above 1.0 (anti-revenge)."""
        self.m_dd = 0.5 if self.soft_hit_today else min(1.0, self.m_dd + 0.25)
        self.soft_hit_today = False
        for s, m in list(self.strat_mult.items()):
            self.strat_mult[s] = min(1.0, m + 0.25)
        self.state = RiskState.NORMAL
        self.realized_net = self.unrealized = 0.0
        self.consec_losses = 0
        self.strat_consec.clear()
        self.strat_cooldown_until.clear()
        self.cooldown_until = None
        self.trades_today = 0
        self.trades_by_key.clear()
        self.trades_by_strategy.clear()
        self.halt_reason = ""

    # ------------------------------------------------------------------ gates
    def entry_allowed(self, strategy: str, key: str, max_strategy_trades: int, now) -> Optional[Reason]:
        c = self.cfg
        self._evaluate(now)
        if self.state == RiskState.HALTED_FOR_DAY:
            return Reason("RISK_HALTED", f"trading halted for the day: {self.halt_reason}")
        if self.state == RiskState.COOLDOWN:
            return Reason("RISK_COOLDOWN", f"cooldown after {self.consec_losses} consecutive losses until {self.cooldown_until:%H:%M}")
        cd = self.strat_cooldown_until.get(strategy)
        if cd is not None and now < cd:
            return Reason("STRATEGY_COOLDOWN", f"{strategy} cooling down until {cd:%H:%M}")
        if self.trades_today >= c.max_trades_day:
            return Reason("MAX_TRADES_DAY", f"{self.trades_today} trades today (max {c.max_trades_day})")
        if self.trades_by_key.get(key, 0) >= c.max_trades_per_instrument:
            return Reason("MAX_TRADES_INSTRUMENT", f"already traded {key} {self.trades_by_key[key]}x today")
        if self.trades_by_strategy.get(strategy, 0) >= max_strategy_trades:
            return Reason("MAX_TRADES_STRATEGY", f"{strategy} reached {max_strategy_trades} trades today")
        return None

    def min_confidence_bump(self) -> float:
        return self.cfg.soft_loss_conf_bump if self.state == RiskState.SOFT_LOSS else 0.0

    def size(self, *, strategy: str, sector: str, entry: int, stop: int, stop_slippage: int,
             est_round_trip_cost: float, confidence_mult: float, regime_mult: float,
             avg_1min_volume: float, open_risks: list[OpenRisk], margin_pct: float,
             available_margin: Optional[float] = None) -> SizingResult:
        """Doc 04 §3. Prices in paise; money in rupees."""
        c = self.cfg
        per_share_risk = (entry - stop + stop_slippage) / 100.0
        if per_share_risk <= 0:
            return SizingResult(0, "invalid", {}, Reason("INVALID_STOP", "stop at or above entry"))
        m_conf = min(1.0, max(0.0, confidence_mult))
        m_regime = min(1.0, max(0.0, regime_mult))
        m_perf = min(1.0, self.strat_mult.get(strategy, 1.0))
        m_dd = min(self.m_dd, 0.5 if self.state == RiskState.SOFT_LOSS else 1.0)
        drawdown = self.equity_peak - (self.cum_net + self.unrealized)
        if drawdown > self.max_drawdown_stop_pct * c.capital:
            return SizingResult(0, "drawdown", {"drawdown": drawdown},
                                Reason("MAX_DRAWDOWN", f"drawdown {drawdown:,.0f} exceeds {self.max_drawdown_stop_pct:.0%} of capital: back to paper"))
        if drawdown > self.drawdown_half_size_pct * c.capital:
            m_dd = min(m_dd, 0.5)
        budget = c.capital * c.risk_per_trade_pct * m_conf * m_regime * m_perf * m_dd
        px = entry / 100.0
        gross_cap = c.capital * c.leverage_cap
        used_notional = sum(r.notional for r in open_risks)
        used_risk = sum(r.risk_rupees for r in open_risks)
        if len(open_risks) >= c.max_positions:
            return SizingResult(0, "max_positions", {}, Reason("MAX_POSITIONS", f"{len(open_risks)} positions open"))
        if sum(1 for r in open_risks if r.sector == sector and sector != "UNKNOWN") >= c.max_per_sector:
            return SizingResult(0, "sector", {}, Reason("SECTOR_LIMIT", f"already holding a {sector} stock"))
        open_risk_room = c.max_open_risk_pct * c.capital - used_risk
        # daily-loss projection: current loss + open risk + new trade must stay within the hard limit
        projection_room = c.daily_loss_hard_pct * c.capital + min(0.0, self.day_pnl) - used_risk
        budget_eff = min(budget, open_risk_room, projection_room) - est_round_trip_cost
        if budget_eff <= 0:
            return SizingResult(0, "risk_room", {"budget": budget, "open_risk_room": open_risk_room,
                                                 "projection_room": projection_room},
                                Reason("NO_RISK_ROOM", "open risk / daily-loss projection leaves no room"))
        limits = {
            "risk": int(budget_eff // per_share_risk),
            "notional": int(c.max_position_frac * gross_cap // px),
            "exposure": int(max(0.0, gross_cap - used_notional) // px),
            "liquidity": int(c.liquidity_frac_1min_vol * avg_1min_volume),
        }
        if available_margin is not None:
            limits["margin"] = int(available_margin * c.margin_buffer // (px * margin_pct))
        limiting = min(limits, key=limits.get)
        qty = max(0, limits[limiting])
        detail = {"limits": limits, "budget": round(budget, 2), "per_share_risk": per_share_risk,
                  "m_conf": m_conf, "m_regime": m_regime, "m_perf": m_perf, "m_dd": m_dd}
        if qty * px < c.min_order_notional:
            return SizingResult(qty, limiting, detail,
                                Reason("BELOW_COST_FLOOR", f"size {qty} x {px:.2f} = {qty*px:,.0f} < min notional "
                                       f"{c.min_order_notional:,.0f} (limited by {limiting})", detail))
        return SizingResult(qty, limiting, detail)
