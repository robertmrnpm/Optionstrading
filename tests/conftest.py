import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from optrader.clock import ET, ManualClock  # noqa: E402
from optrader.config import Settings  # noqa: E402
from optrader.models import Bar  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.app.data_dir = str(tmp_path)
    return s


@pytest.fixture
def clock():
    # Thursday 2026-09-24, a normal trading day
    return ManualClock(datetime(2026, 9, 24, 10, 30, tzinfo=ET))


def make_bars(prices, start=None, volume=1000.0, spread=0.05):
    """1-minute bars from a list of closes starting at 09:30 ET."""
    start = start or datetime(2026, 9, 24, 9, 30, tzinfo=ET)
    bars, prev = [], prices[0]
    for i, p in enumerate(prices):
        o = prev
        v = volume[i] if isinstance(volume, list) else volume
        bars.append(Bar(ts=start + timedelta(minutes=i), open=o, high=max(o, p) + spread, low=min(o, p) - spread,
                        close=p, volume=v))
        prev = p
    return bars
