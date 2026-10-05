"""Universe filters and opportunity ranking (doc 06). Uses only prior-session statistics for
eligibility and closed bars for intraday scores (no look-ahead)."""
from __future__ import annotations

from dataclasses import dataclass

from ..core.types import Instrument, Reason
from ..features.intraday import FeatureState


@dataclass
class UniverseConfig:
    min_adv_rupees: float = 150e7          # Rs 150 crore median daily traded value
    min_price: float = 100.0
    max_price: float = 10_000.0
    min_daily_atr_pct: float = 0.012
    max_daily_atr_pct: float = 0.06
    watchlist_size: int = 25
    exit_rank: int = 40
    min_efficiency: float = 0.15
    max_move_atr: float = 3.0


def eligibility(inst: Instrument, f: FeatureState, cfg: UniverseConfig) -> Reason | None:
    """Pre-market hard filters. Returns a rejection Reason or None if eligible."""
    if inst.is_index:
        return Reason("INDEX", "index is not tradable")
    if inst.cas_eligible:
        return Reason("CAS", "call-auction (illiquid) stock")
    if not inst.fo_eligible:
        return Reason("NOT_FO", "not F&O eligible (fixed circuit risk)")
    adv = f.adv_value()
    if adv is None:
        return Reason("WARMUP", "insufficient history")
    if adv < cfg.min_adv_rupees:
        return Reason("ADV", f"median traded value Rs {adv/1e7:.0f} cr < {cfg.min_adv_rupees/1e7:.0f} cr")
    px = (f.prev_close or 0) / 100.0
    if not (cfg.min_price <= px <= cfg.max_price):
        return Reason("PRICE", f"price {px:.2f} outside [{cfg.min_price}, {cfg.max_price}]")
    atr = f.daily_atr_pct()
    if atr is None or not (cfg.min_daily_atr_pct <= atr <= cfg.max_daily_atr_pct):
        return Reason("ATR", f"daily ATR {atr if atr is None else round(atr*100,2)}% outside range")
    return None


def _pct_rank(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    order = sorted(values, key=values.get)
    n = len(order)
    return {k: (i / (n - 1) if n > 1 else 1.0) for i, k in enumerate(order)}


def opportunity_scores(feats: dict[str, FeatureState], index: FeatureState, cost_bps: dict[str, float],
                       cfg: UniverseConfig) -> dict[str, tuple[float, dict]]:
    """Percentile-rank composite (doc 06 §2). Weights fixed from rationale; changed only via research."""
    rv, rs, er, tv = {}, {}, {}, {}
    vetoes: dict[str, str] = {}
    for k, f in feats.items():
        if f.last is None:
            continue
        r = f.rvol()
        rv[k] = r if r is not None else 0.0
        rs[k] = f.ret_since_open() - index.ret_since_open()
        er[k] = f.efficiency_ratio()
        rng15 = (max(list(f.highs)[-15:]) - min(list(f.lows)[-15:])) / f.last.c * 1e4 if f.highs else 0.0
        tv[k] = rng15 / max(1.0, cost_bps.get(k, 15.0))
        if er[k] < cfg.min_efficiency:
            vetoes[k] = "noisy (efficiency ratio)"
        datr = f.daily_atr_pct()
        if datr and abs(f.ret_since_open()) > cfg.max_move_atr * datr:
            vetoes[k] = "move exhausted (> 3 daily ATR)"
    prv, prs, per, ptv = _pct_rank(rv), _pct_rank(rs), _pct_rank(er), _pct_rank(tv)
    out = {}
    for k in rv:
        comps = {"rvol": prv[k], "rel_strength": prs[k], "efficiency": per[k], "tradable_vol": ptv[k]}
        score = 0.30 * comps["rvol"] + 0.25 * comps["rel_strength"] + 0.25 * comps["efficiency"] + 0.20 * comps["tradable_vol"]
        if k in vetoes:
            score = 0.0
            comps["veto"] = vetoes[k]
        out[k] = (score, comps)
    return out


class Watchlist:
    """Top-N with hysteresis (doc 06 §3)."""

    def __init__(self, cfg: UniverseConfig) -> None:
        self.cfg = cfg
        self.members: set[str] = set()
        self.strikes: dict[str, int] = {}

    def update(self, scores: dict[str, tuple[float, dict]], pinned: set[str]) -> set[str]:
        ranked = sorted(scores, key=lambda k: scores[k][0], reverse=True)
        rank = {k: i for i, k in enumerate(ranked)}
        for k in ranked[: self.cfg.watchlist_size]:
            if scores[k][0] > 0:
                self.members.add(k)
                self.strikes.pop(k, None)
        for k in list(self.members):
            if rank.get(k, 10**9) >= self.cfg.exit_rank or scores.get(k, (0,))[0] == 0:
                self.strikes[k] = self.strikes.get(k, 0) + 1
                if self.strikes[k] >= 3 and k not in pinned:
                    self.members.discard(k)
                    self.strikes.pop(k, None)
        return self.members | pinned
