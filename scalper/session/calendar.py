"""NSE trading calendar and per-stock event exclusions (doc 03 §5, doc 13 findings 14-15).

Holidays 2026: NSE circular as listed by Upstox (verified 2026-10-05). For history, the bar data
itself defines trading days. Special sessions (Muhurat, weekend budget sessions) are NO-TRADE by
default: their liquidity and timing are atypical and they are excluded from research too.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

NSE_HOLIDAYS_2026 = {
    date(2026, 1, 26): "Republic Day", date(2026, 3, 3): "Holi", date(2026, 3, 26): "Shri Ram Navami",
    date(2026, 3, 31): "Shri Mahavir Jayanti", date(2026, 4, 3): "Good Friday",
    date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti", date(2026, 5, 1): "Maharashtra Day",
    date(2026, 5, 28): "Bakri Id", date(2026, 6, 26): "Muharram", date(2026, 9, 14): "Ganesh Chaturthi",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti", date(2026, 10, 20): "Dussehra",
    date(2026, 11, 10): "Diwali Balipratipada", date(2026, 11, 24): "Guru Nanak Jayanti", date(2026, 12, 25): "Christmas",
}
SPECIAL_SESSIONS = {
    date(2026, 2, 1): "Union Budget (Sunday session)",
    date(2026, 11, 8): "Muhurat trading (Sunday)",
    date(2025, 2, 1): "Union Budget (Saturday session)",
}


@dataclass
class TradingCalendar:
    holidays: dict = field(default_factory=lambda: dict(NSE_HOLIDAYS_2026))
    special: dict = field(default_factory=lambda: dict(SPECIAL_SESSIONS))

    def is_special(self, d: date) -> bool:
        return d in self.special or d.weekday() >= 5

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def trade_allowed(self, d: date) -> tuple[bool, str]:
        if d in self.holidays:
            return False, f"exchange holiday: {self.holidays[d]}"
        if self.is_special(d):
            return False, f"special session: {self.special.get(d, 'weekend session')} (no-trade by policy)"
        return True, "ok"


def load_event_exclusions(path: str | Path, purposes: tuple = ("result", "financial", "dividend", "split",
                                                                 "bonus", "fund raising", "merger")) -> dict[date, set[str]]:
    """Load an NSE board-meetings / corporate-actions CSV export into {date: {symbols}}.
    Accepts common column names (SYMBOL/Symbol, BM_DATE/Date/Meeting Date, PURPOSE/Purpose)."""
    out: dict[date, set[str]] = {}
    with open(path, newline="", encoding="utf-8", errors="ignore") as fh:
        rdr = csv.DictReader(fh)
        for r in rdr:
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            sym = r.get("symbol")
            ds = r.get("bm_date") or r.get("meeting date") or r.get("date") or r.get("ex-date") or r.get("ex date")
            purpose = (r.get("purpose") or r.get("bm_purpose") or "").lower()
            if not sym or not ds or (purposes and not any(p in purpose for p in purposes)):
                continue
            for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
                try:
                    from datetime import datetime
                    d = datetime.strptime(ds, fmt).date()
                    break
                except ValueError:
                    d = None
            if d:
                out.setdefault(d, set()).add(sym.upper())
    return out
