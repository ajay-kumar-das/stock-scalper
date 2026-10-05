"""Decision gates G1–G7 in fixed order (doc 07 §1). The first failing gate is the rejection
reason; later gates are still evaluated (where cheap) and recorded for research."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from ..core.types import Candidate, Instrument, Reason
from ..costs.upstox import UpstoxCostModel
from ..regime.model import Regime
from ..risk.engine import OpenRisk, RiskEngine, SizingResult


@dataclass
class DecisionConfig:
    min_confidence: float = 0.0            # uncalibrated scores are not used to gate until R4 (doc 07 §4)
    min_edge_r: float = 0.10               # expected net >= 0.10R  (see doc 13 / review note)
    k_cost: float = 2.0                    # p*W / cost >= k
    min_reward_risk: float = 1.5
    enforce_edge_gate: bool = True         # research backtests set False to *measure* p (gate still journalled)
    enforce_regime_gate: bool = True       # research backtests set False to measure expectancy per regime (doc 05 R6.3)
    regime_mult_weak: float = 0.5


@dataclass
class EdgeEstimate:
    p: float
    win_per_share: float
    loss_per_share: float
    cost_per_share: float
    e_net_per_share: float
    e_net_r: float
    gross_to_cost: float
    reward_risk: float
    est_qty: int


@dataclass
class Decision:
    candidate: Candidate
    accepted: bool
    qty: int = 0
    limit_price: int = 0
    rejection: Optional[Reason] = None
    also_failed: list[Reason] = field(default_factory=list)
    edge: Optional[EdgeEstimate] = None
    sizing: Optional[SizingResult] = None
    gate_trace: list[tuple[str, str]] = field(default_factory=list)


def research_config(**overrides) -> DecisionConfig:
    """Research (R1-R4) runs measure, rather than enforce, gates whose parameters must come from
    evidence (edge estimate, regime permissions). Risk, session and cost-floor gates stay enforced."""
    base = dict(enforce_edge_gate=False, enforce_regime_gate=False)
    base.update(overrides)
    return DecisionConfig(**base)


class DecisionPipeline:
    def __init__(self, cfg: DecisionConfig, risk: RiskEngine, cost_model: UpstoxCostModel,
                 half_spread: Callable[[str, int], int]) -> None:
        self.cfg, self.risk, self.cost_model, self.half_spread = cfg, risk, cost_model, half_spread

    def edge(self, c: Candidate, inst: Instrument, est_qty: int) -> EdgeEstimate:
        hs = self.half_spread(c.key, c.entry_max)
        win = (c.target - c.entry_max) - hs
        loss = (c.entry_max - c.stop) + hs + inst.tick
        q = max(1, est_qty)
        fees = self.cost_model.round_trip(q, c.entry_max / 100.0, c.target / 100.0) * 100.0 / q   # paise/share
        cost = fees + hs
        p = c.prior_win_rate
        e = p * win - (1 - p) * loss - cost
        return EdgeEstimate(p, win, loss, cost, e, e / loss if loss else 0.0,
                            (p * win) / cost if cost else float("inf"), win / loss if loss else 0.0, q)

    def evaluate(self, c: Candidate, inst: Instrument, *, now: datetime, session_ok: tuple[bool, str],
                 regime_ok: tuple[bool, str], regime: Regime, strategy_max_trades: int,
                 instrument_ok: tuple[bool, str], avg_1min_volume: float, open_risks: list[OpenRisk],
                 duplicate: Optional[str], available_margin: Optional[float] = None,
                 regime_best: bool = True) -> Decision:
        d = Decision(c, accepted=False)
        failures: list[Reason] = []

        def fail(gate: str, r: Reason) -> None:
            d.gate_trace.append((gate, f"FAIL {r.code}"))
            failures.append(r)

        def ok(gate: str) -> None:
            d.gate_trace.append((gate, "PASS"))

        # G1 session
        (ok("G1_session") if session_ok[0] else fail("G1_session", Reason("SESSION", session_ok[1])))
        # G2 regime
        if regime_ok[0]:
            ok("G2_regime")
        elif self.cfg.enforce_regime_gate:
            fail("G2_regime", Reason("REGIME", f"{regime_ok[1]} ({regime.label})"))
        else:
            d.gate_trace.append(("G2_regime", f"WOULD_FAIL REGIME {regime_ok[1]} (not enforced: research mode)"))
        # G3 instrument health
        (ok("G3_instrument") if instrument_ok[0] else fail("G3_instrument", Reason("INSTRUMENT", instrument_ok[1])))
        # G4 setup quality
        min_conf = self.cfg.min_confidence + self.risk.min_confidence_bump()
        if c.confidence >= min_conf:
            ok("G4_quality")
        else:
            fail("G4_quality", Reason("LOW_CONFIDENCE", f"confidence {c.confidence:.2f} < {min_conf:.2f}"))
        # indicative size for cost estimation
        prelim = self.risk.size(strategy=c.strategy, sector=inst.sector, entry=c.entry_max, stop=c.stop,
                                stop_slippage=inst.tick, est_round_trip_cost=0.0, confidence_mult=1.0,
                                regime_mult=1.0, avg_1min_volume=avg_1min_volume, open_risks=open_risks,
                                margin_pct=inst.mis_margin_pct, available_margin=available_margin)
        est_qty = prelim.qty if prelim.qty > 0 else max(1, int(self.risk.cfg.min_order_notional * 100 // c.entry_max))
        # G5 edge vs cost
        e = self.edge(c, inst, est_qty)
        d.edge = e
        edge_fail = None
        if e.reward_risk < self.cfg.min_reward_risk:
            edge_fail = Reason("REWARD_RISK", f"reward:risk {e.reward_risk:.2f} < {self.cfg.min_reward_risk}")
        elif e.gross_to_cost < self.cfg.k_cost:
            edge_fail = Reason("COST_DOMINATES", f"expected gross/cost {e.gross_to_cost:.2f} < {self.cfg.k_cost}")
        elif e.e_net_r < self.cfg.min_edge_r:
            edge_fail = Reason("EDGE_TOO_SMALL", f"expected net {e.e_net_r:.2f}R < {self.cfg.min_edge_r}R "
                                                 f"(p={e.p:.2f}, cost {e.cost_per_share/100:.2f}/sh)")
        if edge_fail is None:
            ok("G5_edge")
        elif self.cfg.enforce_edge_gate:
            fail("G5_edge", edge_fail)
        else:
            d.gate_trace.append(("G5_edge", f"WOULD_FAIL {edge_fail.code} (not enforced: research mode)"))
        # G6 risk & sizing
        blocked = self.risk.entry_allowed(c.strategy, c.key, strategy_max_trades, now)
        if blocked:
            fail("G6_risk", blocked)
        else:
            conf_mult = 0.75   # uncalibrated confidence -> fixed multiplier (doc 04 §3)
            reg_mult = 1.0 if regime_best else self.cfg.regime_mult_weak
            est_cost = self.cost_model.round_trip(est_qty, c.entry_max / 100.0, c.entry_max / 100.0)
            s = self.risk.size(strategy=c.strategy, sector=inst.sector, entry=c.entry_max, stop=c.stop,
                               stop_slippage=inst.tick + self.half_spread(c.key, c.entry_max),
                               est_round_trip_cost=est_cost, confidence_mult=conf_mult, regime_mult=reg_mult,
                               avg_1min_volume=avg_1min_volume, open_risks=open_risks,
                               margin_pct=inst.mis_margin_pct, available_margin=available_margin)
            d.sizing = s
            if s.rejection:
                fail("G6_risk", s.rejection)
            else:
                ok("G6_risk")
                d.qty = s.qty
        # G7 OMS duplicate / state
        (fail("G7_oms", Reason("DUPLICATE", duplicate)) if duplicate else ok("G7_oms"))

        if failures:
            d.rejection = failures[0]
            d.also_failed = failures[1:]
            d.qty = 0
            return d
        d.accepted = True
        d.limit_price = c.entry_max
        return d
