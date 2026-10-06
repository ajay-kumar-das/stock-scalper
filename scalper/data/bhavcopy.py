"""NSE daily bhavcopy loader -> point-in-time daily stats and eligible universe (doc 06 §1, finding 24).

Supports both formats:
  * legacy (until Jul 2024): SYMBOL, SERIES, OPEN, HIGH, LOW, CLOSE, LAST, PREVCLOSE, TOTTRDQTY, TOTTRDVAL, TIMESTAMP, ..., ISIN
  * UDiFF (from Jul 2024): TradDt, ..., ISIN, TckrSymb, SctySrs, OpnPric, HghPric, LwPric, ClsPric, ..., PrvsClsgPric, ..., TtlTradgVol, TtlTrfVal
Bhavcopies include symbols that are later delisted, so universes built from them are free of
survivorship bias by construction.
"""
from __future__ import annotations

import csv
import io
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class DailyRow:
    d: date
    symbol: str
    isin: str
    series: str
    open: float
    high: float
    low: float
    close: float
    prev_close: float
    volume: int
    turnover: float     # rupees


def _date(s: str) -> date:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unrecognised date {s!r}")


def parse_bhavcopy(text: str) -> list[DailyRow]:
    rdr = csv.DictReader(io.StringIO(text))
    rdr.fieldnames = [f.strip() for f in rdr.fieldnames or []]
    rows = []
    udiff = "TckrSymb" in rdr.fieldnames
    for r in rdr:
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        try:
            if udiff:
                rows.append(DailyRow(_date(r["TradDt"]), r["TckrSymb"], r.get("ISIN", ""), r.get("SctySrs", ""),
                                     float(r["OpnPric"]), float(r["HghPric"]), float(r["LwPric"]), float(r["ClsPric"]),
                                     float(r.get("PrvsClsgPric") or r["ClsPric"]), int(float(r["TtlTradgVol"])),
                                     float(r["TtlTrfVal"])))
            else:
                rows.append(DailyRow(_date(r["TIMESTAMP"]), r["SYMBOL"], r.get("ISIN", ""), r["SERIES"],
                                     float(r["OPEN"]), float(r["HIGH"]), float(r["LOW"]), float(r["CLOSE"]),
                                     float(r.get("PREVCLOSE") or r["CLOSE"]), int(float(r["TOTTRDQTY"])),
                                     float(r["TOTTRDVAL"])))
        except (KeyError, ValueError):
            continue
    return rows


@dataclass
class UniverseRules:
    series: tuple = ("EQ",)
    min_median_turnover: float = 150e7     # Rs 150 crore
    min_price: float = 100.0
    max_price: float = 10_000.0
    min_atr_pct: float = 0.012
    max_atr_pct: float = 0.06
    lookback: int = 20
    min_history: int = 15


def point_in_time_universe(rows: Iterable[DailyRow], rules: UniverseRules | None = None,
                           fo_lists: dict[date, set[str]] | None = None) -> dict[date, list[str]]:
    """For each trading date d, the symbols eligible on d using ONLY data from sessions before d."""
    rules = rules or UniverseRules()
    by_date: dict[date, list[DailyRow]] = defaultdict(list)
    for r in rows:
        if r.series in rules.series:
            by_date[r.d].append(r)
    hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=rules.lookback))
    out: dict[date, list[str]] = {}
    for d in sorted(by_date):
        eligible = []
        fo = None
        if fo_lists:
            prior = [k for k in fo_lists if k < d]
            fo = fo_lists[max(prior)] if prior else None
        for sym, h in hist.items():
            if len(h) < rules.min_history:
                continue
            last = h[-1]
            med_to = statistics.median(x.turnover for x in h)
            atr = statistics.mean((max(x.high, x.prev_close) - min(x.low, x.prev_close)) / x.prev_close
                                  for x in h if x.prev_close > 0)
            if (med_to >= rules.min_median_turnover and rules.min_price <= last.close <= rules.max_price
                    and rules.min_atr_pct <= atr <= rules.max_atr_pct and (fo is None or sym in fo)):
                eligible.append(sym)
        out[d] = sorted(eligible)
        for r in by_date[d]:          # update history AFTER deciding d's universe (no look-ahead)
            hist[r.symbol].append(r)
    return out


def load_dir(folder: str | Path) -> list[DailyRow]:
    rows: list[DailyRow] = []
    for p in sorted(Path(folder).glob("*.csv")):
        rows.extend(parse_bhavcopy(p.read_text(errors="ignore")))
    return rows
