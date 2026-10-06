"""Upstox instrument master + daily token loader (M2)."""
from __future__ import annotations

import gzip
import json
import urllib.request
from datetime import date
from pathlib import Path
from typing import Optional

from ..core.types import Instrument

MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
NIFTY_KEY = "NSE_INDEX|Nifty 50"


def load_token(path: Optional[Path] = None) -> Optional[str]:
    """Return today's token, or None if missing/stale (stale token => not valid for the mode guard)."""
    p = path or Path.home() / ".scalper" / "token.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    return d.get("access_token") if d.get("date") == date.today().isoformat() else None


def tick_to_paise(tick_size: float) -> int:
    """Upstox reports tick_size; values < 1 are treated as rupees, otherwise paise.
    VERIFY in M2 against a known stock (e.g. RELIANCE) before trusting it."""
    return int(round(tick_size * 100)) if tick_size < 1 else int(round(tick_size))


def parse_master(rows: list[dict], symbols: Optional[set[str]] = None,
                 sectors: Optional[dict[str, str]] = None) -> dict[str, Instrument]:
    out: dict[str, Instrument] = {}
    for r in rows:
        if r.get("segment") != "NSE_EQ" or r.get("instrument_type") not in ("EQ", None):
            continue
        sym = r.get("trading_symbol") or r.get("tradingsymbol")
        if symbols is not None and sym not in symbols:
            continue
        key = r["instrument_key"]
        out[key] = Instrument(key, sym, tick=tick_to_paise(float(r.get("tick_size", 5))),
                              sector=(sectors or {}).get(sym, "UNKNOWN"))
    out[NIFTY_KEY] = Instrument(NIFTY_KEY, "NIFTY", tick=5, sector="INDEX", is_index=True)
    return out


def download_master(dest: Path) -> list[dict]:
    with urllib.request.urlopen(MASTER_URL, timeout=60) as r:
        raw = gzip.decompress(r.read())
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return json.loads(raw)
