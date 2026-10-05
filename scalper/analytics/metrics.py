"""Performance metrics (brief §23). Every figure is available before and after costs.
The headline metric is NET EXPECTANCY AFTER REALISTIC COSTS."""
from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from typing import Callable, Iterable


def _streaks(xs: list[float]) -> tuple[int, int]:
    best_w = best_l = cur_w = cur_l = 0
    for x in xs:
        if x > 0:
            cur_w += 1; cur_l = 0
        else:
            cur_l += 1; cur_w = 0
        best_w, best_l = max(best_w, cur_w), max(best_l, cur_l)
    return best_w, best_l


def _drawdown(pnls: list[float]) -> tuple[float, int]:
    """Max drawdown (rupees) and longest drawdown (number of trades under water)."""
    eq = peak = 0.0
    mdd = 0.0
    longest = cur = 0
    for p in pnls:
        eq += p
        if eq >= peak:
            peak = eq; cur = 0
        else:
            cur += 1
            longest = max(longest, cur)
        mdd = max(mdd, peak - eq)
    return mdd, longest


def block_bootstrap_ci(trades: list[dict], field: str = "net_pnl", n: int = 2000, alpha: float = 0.05,
                       seed: int = 11) -> tuple[float, float]:
    """Bootstrap CI of mean per-trade value, resampling whole DAYS (trades within a day are correlated)."""
    by_day: dict = defaultdict(list)
    for t in trades:
        by_day[str(t["entry_ts"])[:10]].append(t[field])
    days = list(by_day.values())
    if len(days) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    means = []
    for _ in range(n):
        sample = [x for _ in range(len(days)) for x in rng.choice(days)]
        means.append(sum(sample) / len(sample))
    means.sort()
    return means[int(alpha / 2 * n)], means[int((1 - alpha / 2) * n) - 1]


def summarize(trades: list[dict], pnl_field: str = "net_pnl") -> dict:
    n = len(trades)
    if n == 0:
        return {"total_trades": 0}
    pnls = [t[pnl_field] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp, gl = sum(wins), -sum(losses)
    avg_w = gp / len(wins) if wins else 0.0
    avg_l = gl / len(losses) if losses else 0.0
    mdd, longest = _drawdown(pnls)
    cw, cl = _streaks(pnls)
    holds = [t["holding_s"] for t in trades]
    risk = sum(t["risk_rupees"] for t in trades)
    r_field = "r_net" if pnl_field == "net_pnl" else "r_gross"
    rs = [t[r_field] for t in trades]
    return {
        "total_trades": n, "winning_trades": len(wins), "losing_trades": len(losses),
        "win_rate": len(wins) / n, "average_win": avg_w, "average_loss": avg_l,
        "payoff_ratio": avg_w / avg_l if avg_l else float("inf"),
        "expectancy": sum(pnls) / n, "expectancy_r": sum(rs) / n,
        "expectancy_r_std": statistics.pstdev(rs) if n > 1 else 0.0,
        "gross_profit": gp, "gross_loss": gl, "net_profit": sum(pnls),
        "profit_factor": gp / gl if gl else float("inf"),
        "max_drawdown": mdd, "longest_drawdown_trades": longest,
        "max_consecutive_wins": cw, "max_consecutive_losses": cl,
        "avg_holding_s": sum(holds) / n, "median_holding_s": statistics.median(holds),
        "avg_slippage_rupees": sum(t["slippage_rupees"] for t in trades) / n,
        "avg_entry_slippage_bps": sum(t["entry_slippage_bps"] for t in trades) / n,
        "avg_exit_slippage_bps": sum(t["exit_slippage_bps"] for t in trades) / n,
        "cost_per_trade": sum(t["costs"] for t in trades) / n,
        "total_costs": sum(t["costs"] for t in trades),
        "return_per_unit_risk": sum(pnls) / risk if risk else 0.0,
    }


def required_sample(expectancy_r: float, std_r: float, z: float = 2.5) -> float:
    """n such that expectancy is z standard errors from zero (doc 08 §2)."""
    if expectancy_r <= 0:
        return math.inf
    return (z * std_r / expectancy_r) ** 2


def group(trades: Iterable[dict], key: Callable[[dict], str]) -> dict[str, dict]:
    g: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        g[key(t)].append(t)
    return {k: {"net": summarize(v), "gross": summarize(v, "gross_pnl")} for k, v in sorted(g.items())}


def time_bucket(t: dict) -> str:
    ts = t["entry_ts"]
    m = ts.hour * 60 + ts.minute
    start = (m // 30) * 30
    return f"{start//60:02d}:{start%60:02d}"


def full_report(trades: list[dict]) -> dict:
    net = summarize(trades)
    gross = summarize(trades, "gross_pnl")
    rep = {
        "headline": {
            "net_expectancy_after_costs": net.get("expectancy"),
            "net_expectancy_r": net.get("expectancy_r"),
            "trades": net.get("total_trades"),
        },
        "after_costs": net, "before_costs": gross,
        "by_strategy": group(trades, lambda t: t["strategy"]),
        "by_stock": group(trades, lambda t: t["symbol"]),
        "by_time_of_day": group(trades, time_bucket),
        "by_regime": group(trades, lambda t: t["regime"]),
        "by_month": group(trades, lambda t: str(t["entry_ts"])[:7]),
    }
    if trades:
        lo, hi = block_bootstrap_ci(trades)
        rep["headline"]["net_expectancy_95ci"] = (lo, hi)
        rep["headline"]["required_sample_z2.5"] = required_sample(net["expectancy_r"], net["expectancy_r_std"])
        gp = gross.get("gross_profit", 0.0)
        rep["headline"]["fee_share_of_gross_profit"] = net["total_costs"] / gp if gp else float("inf")
    return rep
