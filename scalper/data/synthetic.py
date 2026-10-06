"""Synthetic 1-minute market generator for tests and engine validation (NOT for strategy research).

Two uses:
  * null market (no exploitable structure): a correct backtester must LOSE roughly the costs
    there; any profit indicates look-ahead or fill-model bugs.
  * planted-edge market: momentum continuation is planted on 'in-play' days so we can check
    that the pipeline can detect a real effect end-to-end.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..core.prices import round_to_tick
from ..core.types import IST, Bar, Instrument

MINUTES = 375
INDEX_KEY = "NSE_INDEX|Nifty 50"


@dataclass
class SynthConfig:
    n_stocks: int = 12
    n_days: int = 40
    start: date = date(2025, 1, 6)
    seed: int = 1
    daily_vol: float = 0.018
    beta: float = 1.0
    market_daily_vol: float = 0.009
    in_play_prob: float = 0.08           # per stock-day
    plant_edge: bool = False
    edge_drift_daily: float = 0.020      # extra drift over the in-play window when planted
    in_play_volume_mult: float = 4.0
    base_volume_per_min: int = 20_000


def _u_shape(m: int) -> float:
    x = m / (MINUTES - 1)
    return 0.6 + 1.6 * (x - 0.5) ** 2 * 4 / 2 + (0.8 if m < 15 else 0.0)


def trading_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def generate(cfg: SynthConfig) -> tuple[dict[str, Instrument], dict[date, dict[str, list[Bar]]], set]:
    rng = random.Random(cfg.seed)
    sectors = ["BANK", "IT", "AUTO", "PHARMA", "ENERGY", "FMCG", "METAL", "INFRA"]
    insts = {INDEX_KEY: Instrument(INDEX_KEY, "NIFTY", tick=5, sector="INDEX", is_index=True)}
    prices = {INDEX_KEY: 2_400_000}
    for i in range(cfg.n_stocks):
        k = f"NSE_EQ|SYN{i:03d}"
        insts[k] = Instrument(k, f"SYN{i:03d}", tick=5, sector=sectors[i % len(sectors)])
        prices[k] = rng.randint(30_000, 300_000)   # Rs 300 - 3000
    sig_m = cfg.market_daily_vol / math.sqrt(MINUTES)
    sig_i = math.sqrt(max(cfg.daily_vol ** 2 - (cfg.beta * cfg.market_daily_vol) ** 2, 1e-8)) / math.sqrt(MINUTES)
    norm = sum(_u_shape(m) for m in range(MINUTES)) / MINUTES
    data: dict[date, dict[str, list[Bar]]] = {}
    in_play_log: set = set()
    for d in trading_days(cfg.start, cfg.n_days):
        day: dict[str, list[Bar]] = {k: [] for k in insts}
        # overnight gaps
        for k in prices:
            prices[k] = max(1000, int(prices[k] * math.exp(rng.gauss(0, 0.004))))
        in_play = {k for k in insts if k != INDEX_KEY and rng.random() < cfg.in_play_prob}
        in_play_log |= {(k, d) for k in in_play}
        mkt_rets = [rng.gauss(0, sig_m * math.sqrt(_u_shape(m) / norm)) for m in range(MINUTES)]
        for k, inst in insts.items():
            p = prices[k] / 100.0
            is_idx = k == INDEX_KEY
            vol_mult = cfg.in_play_volume_mult if k in in_play else 1.0
            for m in range(MINUTES):
                ts = datetime.combine(d, time(9, 15), IST) + timedelta(minutes=m)
                shape = _u_shape(m) / norm
                drift = 0.0
                if k in in_play and cfg.plant_edge and 15 <= m < 90:
                    drift = cfg.edge_drift_daily / 75.0
                r = mkt_rets[m] if is_idx else cfg.beta * mkt_rets[m] + rng.gauss(drift, sig_i * math.sqrt(shape) * (1.5 if k in in_play else 1.0))
                o = p
                path = [o]
                for _ in range(4):
                    path.append(path[-1] * math.exp(r / 4 + rng.gauss(0, abs(r) / 4 + sig_i * 0.3)))
                c = path[-1]
                h, l = max(path), min(path)
                tick = inst.tick
                oi, hi, li, ci = (round_to_tick(int(x * 100), tick) for x in (o, h, l, c))
                hi, li = max(hi, oi, ci), min(li, oi, ci)
                vm = vol_mult if (m < 120) else 1.0 + (vol_mult - 1) * 0.3
                v = 0 if is_idx else int(cfg.base_volume_per_min * shape * vm * math.exp(rng.gauss(0, 0.4)))
                if is_idx:
                    v = 1
                day[k].append(Bar(k, ts, oi, hi, li, ci, v))
                p = c
            prices[k] = int(p * 100)
        data[d] = day
    return insts, data, in_play_log


def ticks_from_bars(bars_by_key: dict[str, list[Bar]], seed: int = 3, per_bar: int = 30, depth_qty: int = 2_000):
    """Synthesise a tick stream (with a 5-level book around LTP) that reproduces each bar's OHLCV.
    For engine/fill-model tests only."""
    from .ticks import Tick
    rng = random.Random(seed)
    out = []
    for key, bars in bars_by_key.items():
        vtt = 0
        for b in bars:
            path = [b.o, b.h, b.l, b.c] if rng.random() < 0.5 else [b.o, b.l, b.h, b.c]
            n = per_bar
            pts = [path[min(3, int(i * 4 / n))] for i in range(n - 1)] + [b.c]
            vols = [b.v // n] * n
            vols[-1] += b.v - sum(vols)
            offsets = sorted(rng.randint(0, 59_000) for _ in range(n))       # monotonic exchange times
            for i, (p, v) in enumerate(zip(pts, vols)):
                ts = b.ts + timedelta(milliseconds=offsets[i])
                vtt += v
                tick = 5
                bids = tuple((p - tick * (j + 1), rng.randint(depth_qty // 2, depth_qty), 1) for j in range(5))
                asks = tuple((p + tick * (j + 1), rng.randint(depth_qty // 2, depth_qty), 1) for j in range(5))
                out.append(Tick(key, ts, ts + timedelta(milliseconds=20 + i), p, max(1, v), vtt, p,
                                0, 0, bids, asks))
    out.sort(key=lambda t: (t.recv_ts, t.key))
    return out
