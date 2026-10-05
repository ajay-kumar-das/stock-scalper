"""Append-only decision journal (SQLite WAL) with explain queries (FR-JRN, doc 07 §11)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL, mode TEXT NOT NULL, ts TEXT NOT NULL,
  kind TEXT NOT NULL, key TEXT, strategy TEXT, ref TEXT, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_ref ON events(run_id, ref);
CREATE INDEX IF NOT EXISTS ix_events_kind ON events(run_id, kind);
CREATE TABLE IF NOT EXISTS trades (
  run_id TEXT NOT NULL, trade_id TEXT NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY (run_id, trade_id)
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, mode TEXT, created TEXT, config TEXT, code_version TEXT,
  data_range TEXT, cost_model TEXT, fill_model TEXT, metrics TEXT
);
"""


def _default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if hasattr(o, "as_dict"):
        return o.as_dict()
    if hasattr(o, "value"):
        return o.value
    return str(o)


class Journal:
    def __init__(self, path: str, run_id: str, mode: str, batch: int = 500) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.run_id, self.mode, self.batch = run_id, mode, batch
        self._buf: list[tuple] = []
        self._dedupe: dict[tuple, int] = {}

    def record(self, ts: datetime, kind: str, payload: dict, key: str = "", strategy: str = "", ref: str = "") -> None:
        self._buf.append((self.run_id, self.mode, ts.isoformat(), kind, key, strategy, ref,
                          json.dumps(payload, default=_default)))
        if len(self._buf) >= self.batch:
            self.flush()

    def record_rejection(self, ts: datetime, key: str, strategy: str, code: str, payload: dict) -> None:
        """Dedupe repeated rejections per (instrument, strategy, reason, minute) (doc 07 §1)."""
        k = (key, strategy, code, ts.strftime("%Y%m%d%H%M"))
        if k in self._dedupe:
            self._dedupe[k] += 1
            return
        self._dedupe[k] = 1
        self.record(ts, "candidate_rejected", payload, key, strategy, ref=payload.get("setup_id", ""))

    def record_trade(self, trade: dict) -> None:
        self.flush()
        self.conn.execute("INSERT OR REPLACE INTO trades VALUES (?,?,?)",
                          (self.run_id, trade["trade_id"], json.dumps(trade, default=_default)))
        self.conn.commit()

    def record_run(self, config: dict, code_version: str, data_range: str, cost_model: str, fill_model: str,
                   metrics: Optional[dict] = None) -> None:
        self.conn.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
                          (self.run_id, self.mode, datetime.now().isoformat(), json.dumps(config, default=_default),
                           code_version, data_range, cost_model, fill_model, json.dumps(metrics or {}, default=_default)))
        self.conn.commit()

    def flush(self) -> None:
        if self._buf:
            self.conn.executemany("INSERT INTO events (run_id,mode,ts,kind,key,strategy,ref,payload) VALUES (?,?,?,?,?,?,?,?)",
                                  self._buf)
            self.conn.commit()
            self._buf.clear()

    # ------------------------------------------------------------------ explain
    def events_for(self, ref: str) -> list[tuple[str, str, dict]]:
        self.flush()
        rows = self.conn.execute("SELECT ts, kind, payload FROM events WHERE run_id=? AND ref=? ORDER BY id",
                                 (self.run_id, ref)).fetchall()
        return [(ts, kind, json.loads(p)) for ts, kind, p in rows]

    def explain_trade(self, trade_id: str) -> str:
        row = self.conn.execute("SELECT payload FROM trades WHERE run_id=? AND trade_id=?", (self.run_id, trade_id)).fetchone()
        if not row:
            return f"No trade {trade_id} in run {self.run_id}"
        t = json.loads(row[0])
        lines = [f"Trade {trade_id}: {t['symbol']} via {t['strategy']} v{t['strategy_version']} [{self.mode}]",
                 f"Regime at entry: {t['regime']}",
                 "WHY BOUGHT:"]
        lines += [f"  - {r['text']}" for r in t["entry_reasons"]]
        lines.append(f"  confidence {t['confidence']:.2f}; evidence {t['evidence']}")
        for ts, kind, p in self.events_for(trade_id):
            if kind == "decision_accepted":
                s = p.get("sizing", {})
                lines.append(f"SIZING: qty {p['qty']} limited by {s.get('limiting')} | limits {s.get('limits')} | "
                             f"multipliers conf={s.get('m_conf')} regime={s.get('m_regime')} perf={s.get('m_perf')} dd={s.get('m_dd')}")
                lines.append(f"GATES: {p.get('gate_trace')}")
            elif kind == "stop_moved":
                lines.append(f"{ts} STOP MOVED {p['from']/100:.2f} -> {p['to']/100:.2f}: {p['reason']['text']}")
            elif kind == "exit_requested":
                lines.append(f"{ts} EXIT REQUESTED: {p['reason']['code']} - {p['reason']['text']}")
        lines.append(f"Entry {t['entry_price']:.2f} x {t['qty']} @ {t['entry_ts']} | exit {t['exit_price']:.2f} @ {t['exit_ts']}")
        lines.append(f"Gross {t['gross_pnl']:.2f} | costs {t['costs']:.2f} | NET {t['net_pnl']:.2f} ({t['r_net']:.2f}R) | "
                     f"slippage {t['slippage_rupees']:.2f}")
        return "\n".join(lines)

    def why_not(self, key: str, date_prefix: str = "") -> list[str]:
        self.flush()
        rows = self.conn.execute(
            "SELECT ts, strategy, payload FROM events WHERE run_id=? AND kind='candidate_rejected' AND key=? AND ts LIKE ? ORDER BY id",
            (self.run_id, key, f"{date_prefix}%")).fetchall()
        out = []
        for ts, strat, p in rows:
            d = json.loads(p)
            out.append(f"{ts} {strat}: rejected at {d['rejection']['code']} - {d['rejection']['text']}")
        return out
