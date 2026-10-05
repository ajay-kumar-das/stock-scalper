from datetime import datetime

import pytest

from scalper.core.clock import SimClock
from scalper.core.types import IST, Bar, Instrument

KEY = "NSE_EQ|TEST"


@pytest.fixture
def inst():
    return Instrument(KEY, "TEST", tick=5, sector="IT")


@pytest.fixture
def clock():
    return SimClock(datetime(2025, 3, 3, 9, 15, tzinfo=IST))


def mkbar(ts, o, h, l, c, v=100_000, key=KEY):
    return Bar(key, ts, o, h, l, c, v)


def t(h, m, s=0):
    return datetime(2025, 3, 3, h, m, s, tzinfo=IST)
