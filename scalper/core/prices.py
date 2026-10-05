"""Tick-size utilities. Tick size comes from the instrument, never hard-coded."""
from __future__ import annotations


def round_down_to_tick(p: int, tick: int) -> int:
    return (p // tick) * tick


def round_up_to_tick(p: int, tick: int) -> int:
    return -((-p) // tick) * tick


def round_to_tick(p: int, tick: int) -> int:
    return int(round(p / tick)) * tick


def bps(part: float, whole: float) -> float:
    return 0.0 if whole == 0 else part / whole * 1e4
