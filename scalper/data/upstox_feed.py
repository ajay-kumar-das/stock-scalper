"""Upstox Market Data Feed v3 -> normalised Ticks (M2).

We use the official SDK's MarketDataStreamerV3 for the WebSocket/protobuf plumbing (auth redirect,
protobuf decoding, auto-reconnect) and keep our own normaliser, validator, bar builder and recorder.
The decoded message shape (v3, `full` mode) is:
  {"type": "live_feed", "feeds": {KEY: {"fullFeed": {"marketFF": {
        "ltpc": {"ltp", "ltt", "ltq", "cp"},
        "marketLevel": {"bidAskQuote": [{"bidQ","bidP","askQ","askP"}, ...]},
        "atp", "vtt", "tbq", "tsq", "marketOHLC": {...}}}}}, "currentTs": ...}
Index instruments arrive as fullFeed.indexFF with ltpc only. The first message is market_info
(segment status), which drives market-open / circuit-halt state (F19).
The normaliser is defensive: unknown shapes return None and are counted, never guessed.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from ..core.types import IST
from .ticks import Tick


def _paise(x) -> int:
    return int(round(float(x) * 100))


def _ms_to_dt(ms) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, IST)


def normalise(message: dict, recv_ts: datetime) -> tuple[list[Tick], Optional[dict]]:
    """Returns (ticks, market_info). Unrecognised entries are skipped."""
    if message.get("type") == "market_info":
        return [], message.get("marketInfo", {})
    out: list[Tick] = []
    for key, feed in (message.get("feeds") or {}).items():
        ff = (feed.get("fullFeed") or {})
        node = ff.get("marketFF") or ff.get("indexFF")
        ltpc = (node or {}).get("ltpc") or feed.get("ltpc")
        if not ltpc or "ltp" not in ltpc:
            continue
        bids, asks = [], []
        if node and node.get("marketLevel"):
            for q in node["marketLevel"].get("bidAskQuote", []):
                if float(q.get("bidP", 0)) > 0:
                    bids.append((_paise(q["bidP"]), int(float(q.get("bidQ", 0))), 0))
                if float(q.get("askP", 0)) > 0:
                    asks.append((_paise(q["askP"]), int(float(q.get("askQ", 0))), 0))
        ts = _ms_to_dt(ltpc["ltt"]) if ltpc.get("ltt") else recv_ts
        out.append(Tick(key=key, ts=ts, recv_ts=recv_ts, ltp=_paise(ltpc["ltp"]), ltq=int(float(ltpc.get("ltq", 0) or 0)),
                        vtt=int(float((node or {}).get("vtt", 0) or 0)), atp=_paise((node or {}).get("atp", 0) or 0),
                        tbq=int(float((node or {}).get("tbq", 0) or 0)), tsq=int(float((node or {}).get("tsq", 0) or 0)),
                        bids=tuple(bids), asks=tuple(asks)))
    return out, None


class FeedClient:
    """Thin wrapper over upstox_client.MarketDataStreamerV3 (installed via `pip install .[live]`).
    Market data only — this class can never place orders."""

    def __init__(self, access_token: str, keys: list[str], mode: str, on_ticks: Callable[[list[Tick]], None],
                 on_market_info: Callable[[dict], None], on_status: Callable[[str], None],
                 clock_now: Callable[[], datetime]) -> None:
        import upstox_client  # imported lazily so research environments don't need the SDK
        cfg = upstox_client.Configuration()
        cfg.access_token = access_token
        self._streamer = upstox_client.MarketDataStreamerV3(upstox_client.ApiClient(cfg), keys, mode)
        self._streamer.auto_reconnect(True, 3, 20)
        self._streamer.on("message", self._on_message)
        self._streamer.on("open", lambda: on_status("open"))
        self._streamer.on("close", lambda *a: on_status("close"))
        self._streamer.on("error", lambda e: on_status(f"error: {e}"))
        self.on_ticks, self.on_market_info, self.now = on_ticks, on_market_info, clock_now
        self.unrecognised = 0

    def _on_message(self, message: dict) -> None:
        ticks, info = normalise(message, self.now())
        if info is not None:
            self.on_market_info(info)
        elif not ticks and message.get("feeds"):
            self.unrecognised += 1
        if ticks:
            self.on_ticks(ticks)

    def connect(self) -> None:
        self._streamer.connect()

    def subscribe(self, keys: list[str], mode: str) -> None:
        self._streamer.subscribe(keys, mode)

    def change_mode(self, keys: list[str], mode: str) -> None:
        self._streamer.change_mode(keys, mode)

    def disconnect(self) -> None:
        self._streamer.disconnect()
