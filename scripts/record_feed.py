"""Market-hours tick/depth recorder (M2). Market data only — cannot place orders.

    python scripts/upstox_login.py          # once each morning
    python scripts/record_feed.py --symbols-file config/universe_seed.txt

Records `full` mode (LTP, 5-level depth, volume, ATP, total buy/sell qty) for up to 1,500 instruments,
builds 1-minute bars, validates data, and writes Parquet under data/ticks and data/bars_live.
Stops itself after 15:35 IST.
"""
import argparse
import signal
import threading
import time as _time
from datetime import datetime, time, timedelta
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))   # works from a checkout without installing

from scalper.core.clock import WallClock
from scalper.data.instruments import download_master, load_token, parse_master
from scalper.data.store import BarStore
from scalper.data.ticks import BarBuilder, StalenessMonitor, TickRecorder, TickValidator
from scalper.data.upstox_feed import FeedClient


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols-file", required=True)
    ap.add_argument("--data", default="data")
    a = ap.parse_args()
    token = load_token()
    if not token:
        raise SystemExit("No valid token for today. Run scripts/upstox_login.py first.")
    syms = {s.strip().upper() for s in Path(a.symbols_file).read_text().splitlines() if s.strip()}
    insts = parse_master(download_master(Path(a.data) / "instruments" / "latest.json"), syms)
    keys = list(insts)[:1500]
    clock = WallClock()
    rec, bb, val, stale = TickRecorder(a.data), BarBuilder(), TickValidator(), StalenessMonitor()
    bars_live = BarStore(Path(a.data) / "live")
    live_bars = []                               # BarStore.write rewrites a (date, key) file, so keep the full day
    stats = {"ticks": 0, "bad": 0, "bars": 0}
    lock = threading.Lock()                      # SDK callbacks run on the WebSocket thread

    def on_ticks(ticks):
        with lock:
            _on_ticks(ticks)

    def _on_ticks(ticks):
        for t in ticks:
            stale.on_tick(t)
            rec.on_tick(t)                       # raw data is always recorded, even if suspect
            if val.check(t):
                stats["bad"] += 1
                val.release(t.key)               # recorder keeps going; the engine would quarantine
                continue
            stats["ticks"] += 1
            b = bb.on_tick(t)
            if b:
                live_bars.append(b); stats["bars"] += 1

    feed = FeedClient(token, keys, "full", on_ticks, lambda info: print("market_info", info),
                      lambda s: print(datetime.now().strftime("%H:%M:%S"), "feed", s), clock.now)
    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *x: stop.update(flag=True))
    feed.connect()
    while not stop["flag"] and clock.now().time() < time(15, 35):
        _time.sleep(1)
        with lock:
            done = bb.flush(clock.now() - timedelta(seconds=2))      # 2 s grace for late ticks
            live_bars.extend(done); stats["bars"] += len(done)
            if clock.now().minute % 15 == 0 and clock.now().second == 30 and live_bars:
                bars_live.write(live_bars); rec.flush()
        if stale.feed_stale(clock.now()) and time(9, 15) <= clock.now().time() <= time(15, 30):
            print("WARNING: feed stale")
        if clock.now().second == 0:
            print(clock.now().strftime("%H:%M"), stats, "unrecognised", feed.unrecognised)
    feed.disconnect()
    rec.flush()
    live_bars.extend(bb.flush(clock.now() + timedelta(minutes=1)))
    if live_bars:
        bars_live.write(live_bars)
    print("done", stats)


if __name__ == "__main__":
    main()
