"""Fixed data partitions and the holdout lock (doc 05 §2), plus the experiment registry (doc 05 §8).

The holdout can be touched once per strategy version, only after a registered R4 (validation)
pass. Every run is counted so reported statistics can be deflated for the number of trials.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Callable

PARTITIONS = {
    "development": (date(2022, 1, 1), date(2024, 12, 31)),
    "validation": (date(2025, 1, 1), date(2025, 12, 31)),
    "holdout": (date(2026, 1, 1), date(2026, 8, 31)),
    "live_forward": (date(2026, 9, 1), date(2100, 1, 1)),
}


class HoldoutLocked(PermissionError):
    pass


def partition_of(d: date) -> str:
    for name, (a, b) in PARTITIONS.items():
        if a <= d <= b:
            return name
    return "pre-history"


class ExperimentRegistry:
    def __init__(self, path: str = "experiments.sqlite") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, ts TEXT, hypothesis TEXT, strategy TEXT,
            version TEXT, stage TEXT, partition TEXT, metrics TEXT, passed INTEGER);
        """)

    def trials(self, hypothesis: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM runs WHERE hypothesis=?", (hypothesis,)).fetchone()[0]

    def has_pass(self, strategy: str, version: str, stage: str) -> bool:
        return self.conn.execute("SELECT 1 FROM runs WHERE strategy=? AND version=? AND stage=? AND passed=1",
                                 (strategy, version, stage)).fetchone() is not None

    def holdout_used(self, strategy: str, version: str) -> bool:
        return self.conn.execute("SELECT 1 FROM runs WHERE strategy=? AND version=? AND partition='holdout'",
                                 (strategy, version)).fetchone() is not None

    def record(self, run_id: str, hypothesis: str, strategy: str, version: str, stage: str, partition: str,
               metrics: dict, passed: bool) -> None:
        self.conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
                          (run_id, datetime.now().isoformat(), hypothesis, strategy, version, stage, partition,
                           json.dumps(metrics, default=str), int(passed)))
        self.conn.commit()


def date_filter(partition: str, *, strategy: str = "", version: str = "",
                registry: ExperimentRegistry | None = None) -> Callable[[date], bool]:
    """Returns an `allowed_dates` predicate for run_backtest; enforces the holdout lock."""
    if partition not in PARTITIONS:
        raise ValueError(partition)
    if partition == "holdout":
        if registry is None or not strategy or not version:
            raise HoldoutLocked("holdout access requires a registry, strategy and version")
        if not registry.has_pass(strategy, version, "R4"):
            raise HoldoutLocked(f"{strategy} v{version} has no registered R4 validation pass")
        if registry.holdout_used(strategy, version):
            raise HoldoutLocked(f"holdout already used for {strategy} v{version}; never re-test on holdout")
    a, b = PARTITIONS[partition]
    return lambda d: a <= d <= b
