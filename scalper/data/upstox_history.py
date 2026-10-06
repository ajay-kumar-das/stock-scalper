"""Upstox v3 historical/intraday candle client (M2).

Endpoint (verified 2026-10-05):
  GET https://api.upstox.com/v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}
  unit=minutes, interval=1..300; 1-minute data available since Jan 2022; max 1 month per request for 1-15 min.
Response: {"status": "success", "data": {"candles": [[ts_iso, o, h, l, c, volume, oi], ...]}} (newest first).

Runs only where upstox.com is reachable (operator machine / VPS), never inside the research sandbox.
"""
from __future__ import annotations

import json
import time as _time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Iterable, Optional

from ..core.types import Bar
from ..core.prices import round_to_tick

BASE = "https://api.upstox.com/v3/historical-candle"


def parse_candles(key: str, payload: dict, tick: int = 5) -> list[Bar]:
    """Parse an Upstox candle response into chronologically ordered Bars (prices -> paise)."""
    if payload.get("status") != "success":
        raise ValueError(f"Upstox error: {payload.get('errors') or payload}")
    out = []
    for row in payload.get("data", {}).get("candles", []):
        ts = datetime.fromisoformat(row[0])
        o, h, l, c = (round_to_tick(int(round(float(x) * 100)), 1) for x in row[1:5])
        v = int(row[5])
        h, l = max(h, o, c), min(l, o, c)
        out.append(Bar(key, ts, o, h, l, c, v))
    out.sort(key=lambda b: b.ts)
    return out


def month_windows(start: date, end: date) -> Iterable[tuple[date, date]]:
    """Split [start, end] into <= 1-month windows (API limit for 1-minute candles)."""
    a = start
    while a <= end:
        b = min(end, (a.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1))
        yield a, b
        a = b + timedelta(days=1)


@dataclass
class HistoryClient:
    access_token: Optional[str] = None          # some history endpoints work without auth; pass if you have one
    min_interval_s: float = 0.05                 # stay far below 25 req/s
    opener: Callable = urllib.request.urlopen

    def _get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        if self.access_token:
            req.add_header("Authorization", f"Bearer {self.access_token}")
        for attempt in range(4):
            try:
                with self.opener(req, timeout=30) as r:
                    return json.loads(r.read().decode())
            except Exception as e:  # noqa: BLE001
                if attempt == 3:
                    raise
                _time.sleep(1.5 * (attempt + 1))
                last = e
        raise RuntimeError(last)

    def minute_candles(self, key: str, start: date, end: date, interval: int = 1, tick: int = 5) -> list[Bar]:
        bars: list[Bar] = []
        for a, b in month_windows(start, end):
            url = f"{BASE}/{urllib.parse.quote(key, safe='')}/minutes/{interval}/{b.isoformat()}/{a.isoformat()}"
            bars.extend(parse_candles(key, self._get(url), tick))
            _time.sleep(self.min_interval_s)
        dedup = {b.ts: b for b in bars}
        return [dedup[t] for t in sorted(dedup)]
