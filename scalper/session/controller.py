"""Session state machine (doc 03 §1, FR-SESS)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum


class SessionState(str, Enum):
    STARTING = "STARTING"
    RECOVERING = "RECOVERING"
    ARMED = "ARMED"
    TRADING = "TRADING"
    PAUSED = "PAUSED"
    DEFENSIVE = "DEFENSIVE"
    HALTED_FOR_DAY = "HALTED_FOR_DAY"
    NO_NEW_ENTRIES = "NO_NEW_ENTRIES"
    FLATTENING = "FLATTENING"
    EMERGENCY = "EMERGENCY"
    CLOSED = "CLOSED"


ENTRY_STATES = frozenset({SessionState.TRADING})
MANAGE_STATES = frozenset({SessionState.TRADING, SessionState.PAUSED, SessionState.NO_NEW_ENTRIES,
                           SessionState.FLATTENING, SessionState.HALTED_FOR_DAY, SessionState.DEFENSIVE,
                           SessionState.EMERGENCY, SessionState.ARMED, SessionState.RECOVERING})


@dataclass
class SessionTimes:
    market_open: time = time(9, 15)
    earliest_entry: time = time(9, 20)
    entry_cutoff: time = time(14, 45)
    flatten_start: time = time(15, 0)
    flatten_deadline: time = time(15, 10)
    verify_flat: time = time(15, 15)
    market_close: time = time(15, 30)


@dataclass
class SessionController:
    times: SessionTimes = field(default_factory=SessionTimes)
    state: SessionState = SessionState.STARTING
    paused: bool = False
    defensive_reasons: set = field(default_factory=set)
    halted: bool = False
    emergency: bool = False
    log: list = field(default_factory=list)

    def _set(self, s: SessionState, ts: datetime, why: str) -> None:
        if s != self.state:
            self.log.append((ts, self.state.value, s.value, why))
            self.state = s

    def start_day(self, ts: datetime, reconciled: bool = True, trade_allowed: tuple[bool, str] = (True, "ok")) -> None:
        self.halted = self.emergency = self.paused = False
        self.defensive_reasons.clear()
        self._set(SessionState.RECOVERING, ts, "start of day")
        if not trade_allowed[0]:
            self.paused = True                  # calendar policy: manage only, no entries
            self.log.append((ts, "CALENDAR", trade_allowed[1], ""))
        if reconciled:
            self._set(SessionState.ARMED, ts, "reconciliation ok")
        else:
            self.defensive_reasons.add("RECONCILIATION_PENDING")

    def tick(self, ts: datetime) -> SessionState:
        t = ts.time()
        if self.emergency:
            self._set(SessionState.EMERGENCY, ts, "emergency")
        elif t >= self.times.market_close:
            self._set(SessionState.CLOSED, ts, "market closed")
        elif t >= self.times.flatten_start:
            self._set(SessionState.FLATTENING, ts, "flatten window")
        elif self.halted:
            self._set(SessionState.HALTED_FOR_DAY, ts, "risk halt")
        elif self.defensive_reasons:
            self._set(SessionState.DEFENSIVE, ts, ",".join(sorted(self.defensive_reasons)))
        elif self.paused:
            self._set(SessionState.PAUSED, ts, "operator pause")
        elif t >= self.times.entry_cutoff:
            self._set(SessionState.NO_NEW_ENTRIES, ts, "entry cutoff")
        elif t >= self.times.market_open:
            self._set(SessionState.TRADING, ts, "market open")
        return self.state

    def entries_allowed(self, ts: datetime) -> tuple[bool, str]:
        s = self.tick(ts)
        if s not in ENTRY_STATES:
            return False, f"session state {s.value}"
        if ts.time() < self.times.earliest_entry:
            return False, f"before earliest entry {self.times.earliest_entry}"
        return True, "ok"

    # operator / system controls (L0/L1 precedence)
    def pause(self, ts):
        self.paused = True; self.tick(ts)

    def resume(self, ts):
        self.paused = False; self.tick(ts)

    def halt_for_day(self, ts, why):
        self.halted = True; self.log.append((ts, "HALT", why, "")); self.tick(ts)

    def enter_defensive(self, ts, reason):
        self.defensive_reasons.add(reason); self.tick(ts)

    def clear_defensive(self, ts, reason):
        self.defensive_reasons.discard(reason); self.tick(ts)

    def emergency_stop(self, ts):
        self.emergency = True; self.tick(ts)
