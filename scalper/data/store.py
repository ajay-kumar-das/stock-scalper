"""Parquet storage for bars and ticks, partitioned by date (ADR-4), and loaders for the backtester."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, time
from pathlib import Path
from typing import Iterable, Optional

import pyarrow as pa
import pyarrow.parquet as pq

from ..core.types import IST, Bar

BAR_SCHEMA = pa.schema([("key", pa.string()), ("ts", pa.timestamp("s", tz="Asia/Kolkata")), ("o", pa.int64()),
                        ("h", pa.int64()), ("l", pa.int64()), ("c", pa.int64()), ("v", pa.int64())])


class BarStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, d: date, key: str) -> Path:
        safe = key.replace("|", "_").replace(" ", "_")
        return self.root / "bars_1m" / f"date={d.isoformat()}" / f"{safe}.parquet"

    def write(self, bars: Iterable[Bar]) -> int:
        by: dict[tuple[date, str], list[Bar]] = defaultdict(list)
        for b in bars:
            by[(b.ts.date(), b.key)].append(b)
        for (d, key), bs in by.items():
            p = self._path(d, key)
            p.parent.mkdir(parents=True, exist_ok=True)
            tbl = pa.table({"key": [b.key for b in bs], "ts": [b.ts for b in bs], "o": [b.o for b in bs],
                            "h": [b.h for b in bs], "l": [b.l for b in bs], "c": [b.c for b in bs],
                            "v": [b.v for b in bs]}, schema=BAR_SCHEMA)
            pq.write_table(tbl, p)
        return sum(len(v) for v in by.values())

    def dates(self) -> list[date]:
        base = self.root / "bars_1m"
        if not base.exists():
            return []
        return sorted(date.fromisoformat(p.name.split("=")[1]) for p in base.iterdir() if p.name.startswith("date="))

    def load_day(self, d: date, keys: Optional[set[str]] = None,
                 session: tuple[time, time] = (time(9, 15), time(15, 29))) -> dict[str, list[Bar]]:
        out: dict[str, list[Bar]] = {}
        folder = self.root / "bars_1m" / f"date={d.isoformat()}"
        if not folder.exists():
            return out
        for f in sorted(folder.glob("*.parquet")):
            tbl = pq.read_table(f).to_pydict()
            if not tbl["key"]:
                continue
            key = tbl["key"][0]
            if keys is not None and key not in keys:
                continue
            bars = []
            for i in range(len(tbl["key"])):
                ts = tbl["ts"][i].astimezone(IST)
                if session[0] <= ts.time() <= session[1]:
                    bars.append(Bar(key, ts, tbl["o"][i], tbl["h"][i], tbl["l"][i], tbl["c"][i], tbl["v"][i]))
            out[key] = bars
        return out

    def load_range(self, start: date, end: date, keys: Optional[set[str]] = None) -> dict[date, dict[str, list[Bar]]]:
        return {d: self.load_day(d, keys) for d in self.dates() if start <= d <= end}


def align_day(day: dict[str, list[Bar]], index_key: str) -> dict[str, list[Bar]]:
    """Drop instruments with gaps the engine cannot handle safely; keep the index regardless.
    Missing minutes are NOT forward-filled (that would invent trades); the instrument is simply
    absent for that minute, which the engine treats as stale."""
    return {k: v for k, v in day.items() if v and (k == index_key or len(v) >= 300)}


class UniverseStoreLoader:
    """Picklable loader for walk-forward windows: loads each day's point-in-time universe from the
    bar store (plus the index). universe: {date: [symbols]}, by_symbol: {symbol: instrument_key}."""

    def __init__(self, root: str, universe: dict, by_symbol: dict, index_key: str) -> None:
        self.root, self.universe, self.by_symbol, self.index_key = root, universe, by_symbol, index_key

    def __call__(self, start: date, end: date) -> dict:
        store = BarStore(self.root)
        out = {}
        for d in store.dates():
            if not (start <= d <= end) or d.weekday() >= 5:
                continue
            keys = {self.by_symbol[s] for s in self.universe.get(d, []) if s in self.by_symbol} | {self.index_key}
            day = align_day(store.load_day(d, keys), self.index_key)
            if self.index_key in day and len(day) > 1:
                out[d] = day
        return out
