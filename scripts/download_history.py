"""Download Upstox 1-minute candles into the local bar store (M2). Run on a machine that can reach Upstox.

    python scripts/download_history.py --symbols-file config/universe_seed.txt --start 2022-01-01 --end 2026-09-30

`universe_seed.txt` = one NSE symbol per line (e.g. today's F&O stock list). Survivorship: also include
symbols that were in the F&O list historically but have since left it (see doc 06 §1); the
point-in-time eligibility itself comes from bhavcopies, not from this list.
"""
import argparse
import json
from datetime import date
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))   # works from a checkout without installing

from scalper.data.instruments import download_master, load_token, parse_master
from scalper.data.store import BarStore
from scalper.data.upstox_history import HistoryClient


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols-file", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--data", default="data")
    a = ap.parse_args()
    syms = {s.strip().upper() for s in Path(a.symbols_file).read_text().splitlines() if s.strip()}
    master = download_master(Path(a.data) / "instruments" / f"NSE-{date.today()}.json")
    insts = parse_master(master, syms)
    missing = syms - {i.symbol for i in insts.values()}
    if missing:
        print(f"not in today's master (delisted/renamed? handle manually): {sorted(missing)}")
    client = HistoryClient(access_token=load_token())
    store = BarStore(a.data)
    s, e = date.fromisoformat(a.start), date.fromisoformat(a.end)
    log = {}
    for key, inst in sorted(insts.items(), key=lambda kv: kv[1].symbol):
        try:
            bars = client.minute_candles(key, s, e, tick=inst.tick)
            n = store.write(bars)
            log[inst.symbol] = n
            print(f"{inst.symbol:>12}: {n} bars")
        except Exception as ex:  # noqa: BLE001
            log[inst.symbol] = f"ERROR {ex}"
            print(f"{inst.symbol:>12}: ERROR {ex}")
    (Path(a.data) / "download_log.json").write_text(json.dumps(log, indent=1))


if __name__ == "__main__":
    main()
