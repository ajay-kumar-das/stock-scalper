"""Normalised ticks, data validators, staleness monitor, tick->bar builder, tick recorder and the
bar-agreement check required by M2 (FR-DATA-2/3/4, doc 13 finding 3)."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..core.types import Bar


@dataclass(frozen=True)
class Tick:
    key: str
    ts: datetime               # exchange last-trade time
    recv_ts: datetime          # local receive time
    ltp: int                   # paise
    ltq: int = 0
    vtt: int = 0               # cumulative volume traded today
    atp: int = 0               # average traded price (VWAP), paise
    tbq: int = 0               # total buy quantity
    tsq: int = 0               # total sell quantity
    bids: tuple = ()           # ((price_paise, qty, orders), ...) best first
    asks: tuple = ()

    @property
    def best_bid(self) -> Optional[int]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[int]:
        return self.asks[0][0] if self.asks else None

    @property
    def spread_bps(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            mid = (self.best_bid + self.best_ask) / 2
            return (self.best_ask - self.best_bid) / mid * 1e4
        return None


# ---------------------------------------------------------------------- validation (FR-DATA-4)
@dataclass
class TickValidator:
    max_jump: float = 0.10            # > 10% tick-to-tick move = suspect (F&O stocks have dynamic bands)
    quarantined: dict = field(default_factory=dict)       # key -> reason
    _last: dict = field(default_factory=dict)
    _last_raw: dict = field(default_factory=dict)

    def check(self, t: Tick) -> Optional[str]:
        """Jumps are measured against the last RECEIVED tick, so a genuine gap flags one tick and then
        the new level is accepted (a lasting >10% move must not silently blind us for the day)."""
        reason = None
        prev = self._last.get(t.key)
        raw = self._last_raw.get(t.key)
        self._last_raw[t.key] = t
        if t.ltp <= 0:
            reason = "non-positive LTP"
        elif t.best_bid is not None and t.best_ask is not None and t.best_bid >= t.best_ask:
            reason = f"crossed book bid {t.best_bid} >= ask {t.best_ask}"
        elif prev is not None:
            if t.vtt < prev.vtt:
                reason = f"cumulative volume decreased {prev.vtt} -> {t.vtt}"
            elif t.ts < prev.ts:
                reason = "timestamp regression"
            elif raw is not None and raw.ltp > 0 and abs(t.ltp / raw.ltp - 1) > self.max_jump:
                reason = f"price jump {raw.ltp} -> {t.ltp}"
        if reason:
            self.quarantined[t.key] = reason
            return reason
        self._last[t.key] = t
        return None

    def release(self, key: str) -> None:
        self.quarantined.pop(key, None)


# ---------------------------------------------------------------------- staleness (FR-DATA-3)
@dataclass
class StalenessMonitor:
    stale_after: timedelta = timedelta(seconds=5)
    global_after: timedelta = timedelta(seconds=3)
    last_recv: dict = field(default_factory=dict)
    last_any: Optional[datetime] = None

    def on_tick(self, t: Tick) -> None:
        self.last_recv[t.key] = t.recv_ts
        self.last_any = t.recv_ts

    def stale_keys(self, now: datetime, keys) -> list[str]:
        return [k for k in keys if k not in self.last_recv or now - self.last_recv[k] > self.stale_after]

    def feed_stale(self, now: datetime) -> bool:
        return self.last_any is None or now - self.last_any > self.global_after


# ---------------------------------------------------------------------- bar builder (FR-DATA-2)
class BarBuilder:
    """Builds 1-minute bars from ticks. Minutes with no trade produce NO bar (never invent prices)."""

    def __init__(self) -> None:
        self._cur: dict[str, list] = {}           # key -> [minute_ts, o, h, l, c, vtt_start, vtt_last]
        self._vtt: dict[str, int] = {}
        self._emitted: dict[str, datetime] = {}   # last closed minute per key
        self.late_ticks = 0

    @staticmethod
    def _minute(ts: datetime) -> datetime:
        return ts.replace(second=0, microsecond=0)

    def on_tick(self, t: Tick) -> Optional[Bar]:
        m = self._minute(t.ts)
        cur = self._cur.get(t.key)
        if (t.key in self._emitted and m <= self._emitted[t.key]) or (cur is not None and m < cur[0]):
            # tick for a minute already closed (exchange/receive lag): never reopen it. Its volume is
            # carried into the next bar via the cumulative-volume baseline; its price is ignored.
            self.late_ticks += 1
            if cur is not None:
                cur[6] = max(cur[6], t.vtt)
            return None
        done = None
        if cur is not None and m > cur[0]:
            done = self._close(t.key)
            cur = None
        if cur is None:
            base = self._vtt.get(t.key, t.vtt - t.ltq)
            self._cur[t.key] = [m, t.ltp, t.ltp, t.ltp, t.ltp, base, t.vtt]
        else:
            cur[2] = max(cur[2], t.ltp); cur[3] = min(cur[3], t.ltp); cur[4] = t.ltp; cur[6] = t.vtt
        return done

    def _close(self, key: str) -> Optional[Bar]:
        cur = self._cur.pop(key, None)
        if cur is None:
            return None
        m, o, h, l, c, v0, v1 = cur
        self._vtt[key] = v1
        self._emitted[key] = m
        return Bar(key, m, o, h, l, c, max(0, v1 - v0))

    def flush(self, now: datetime) -> list[Bar]:
        """Close every bar whose minute has ended (call at each minute boundary + small grace)."""
        m = self._minute(now)
        return [b for k in [k for k, c in self._cur.items() if c[0] < m] if (b := self._close(k)) is not None]


# ---------------------------------------------------------------------- recorder
class TickRecorder:
    """Buffers ticks and writes Parquet files partitioned by date (data/ticks/date=YYYY-MM-DD/part-*.parquet)."""

    def __init__(self, root: str | Path, flush_every: int = 5000) -> None:
        self.root = Path(root)
        self.flush_every = flush_every
        self.buf: list[Tick] = []
        self.files_written = 0

    def on_tick(self, t: Tick) -> None:
        self.buf.append(t)
        if len(self.buf) >= self.flush_every:
            self.flush()

    def flush(self) -> None:
        if not self.buf:
            return
        import pyarrow as pa
        import pyarrow.parquet as pq
        by_date = defaultdict(list)
        for t in self.buf:
            by_date[t.recv_ts.date()].append(t)
        for d, ts in by_date.items():
            folder = self.root / "ticks" / f"date={d.isoformat()}"
            folder.mkdir(parents=True, exist_ok=True)
            cols = {
                "key": [t.key for t in ts], "ts": [t.ts for t in ts], "recv_ts": [t.recv_ts for t in ts],
                "ltp": [t.ltp for t in ts], "ltq": [t.ltq for t in ts], "vtt": [t.vtt for t in ts],
                "atp": [t.atp for t in ts], "tbq": [t.tbq for t in ts], "tsq": [t.tsq for t in ts],
                "bid_p": [[b[0] for b in t.bids] for t in ts], "bid_q": [[b[1] for b in t.bids] for t in ts],
                "ask_p": [[a[0] for a in t.asks] for t in ts], "ask_q": [[a[1] for a in t.asks] for t in ts],
            }
            pq.write_table(pa.table(cols), folder / f"part-{ts[0].recv_ts:%H%M%S%f}-{self.files_written}.parquet")
            self.files_written += 1
        self.buf.clear()


def read_ticks(root: str | Path, d) -> list[Tick]:
    import pyarrow.parquet as pq
    out: list[Tick] = []
    folder = Path(root) / "ticks" / f"date={d.isoformat()}"
    for f in sorted(folder.glob("*.parquet")):
        c = pq.read_table(f).to_pydict()
        for i in range(len(c["key"])):
            out.append(Tick(c["key"][i], c["ts"][i], c["recv_ts"][i], c["ltp"][i], c["ltq"][i], c["vtt"][i],
                            c["atp"][i], c["tbq"][i], c["tsq"][i],
                            tuple(zip(c["bid_p"][i], c["bid_q"][i], [0] * len(c["bid_p"][i]))),
                            tuple(zip(c["ask_p"][i], c["ask_q"][i], [0] * len(c["ask_p"][i])))))
    out.sort(key=lambda t: (t.recv_ts, t.key))
    return out


# ---------------------------------------------------------------------- M2 exit check (finding 3)
def bar_agreement(built: list[Bar], reference: list[Bar], tick: int = 5, vol_tol: float = 0.02) -> dict:
    """Compare bars built from our feed with Upstox historical candles for the same instrument/day."""
    ref = {b.ts: b for b in reference}
    n = ok_px = ok_vol = 0
    mismatches = []
    for b in built:
        r = ref.get(b.ts)
        if r is None:
            continue
        n += 1
        px_ok = all(abs(x - y) <= tick for x, y in ((b.o, r.o), (b.h, r.h), (b.l, r.l), (b.c, r.c)))
        vol_ok = abs(b.v - r.v) <= vol_tol * max(1, r.v)
        ok_px += px_ok
        ok_vol += vol_ok
        if not (px_ok and vol_ok) and len(mismatches) < 20:
            mismatches.append((b.ts.isoformat(), (b.o, b.h, b.l, b.c, b.v), (r.o, r.h, r.l, r.c, r.v)))
    return {"compared": n, "ohlc_agreement": ok_px / n if n else 0.0, "volume_agreement": ok_vol / n if n else 0.0,
            "passes": n > 0 and ok_px / n >= 0.99 and ok_vol / n >= 0.99, "examples": mismatches}
