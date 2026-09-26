"""Market clock. RealClock follows the wall clock; SimClock runs a compressed trading day."""
from __future__ import annotations

import asyncio
import time as _time
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# NYSE full-day holidays.
HOLIDAYS = {
    date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1), date(2025, 11, 27),
    date(2025, 12, 25),
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
    date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
}
# Early closes (13:00 ET).
HALF_DAYS = {
    date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
    date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 11, 26),
}


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def close_time(d: date) -> time:
    return time(13, 0) if d in HALF_DAYS else MARKET_CLOSE


def previous_trading_days(d: date, n: int) -> list[date]:
    """The n trading days ending at d (inclusive), oldest first."""
    out: list[date] = []
    cur = d
    while len(out) < n:
        if is_trading_day(cur):
            out.append(cur)
        cur -= timedelta(days=1)
    return list(reversed(out))


def next_trading_day(d: date) -> date:
    cur = d + timedelta(days=1)
    while not is_trading_day(cur):
        cur += timedelta(days=1)
    return cur


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


class Clock:
    def now(self) -> datetime:  # timezone-aware, ET
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError

    @property
    def speed(self) -> float:
        return 1.0

    # --- helpers -------------------------------------------------------------------
    def today(self) -> date:
        return self.now().date()

    def is_market_open(self) -> bool:
        n = self.now()
        return is_trading_day(n.date()) and MARKET_OPEN <= n.time() < close_time(n.date())

    def minutes_since_open(self) -> float:
        n = self.now()
        open_dt = datetime.combine(n.date(), MARKET_OPEN, tzinfo=ET)
        return (n - open_dt).total_seconds() / 60

    def minutes_to_close(self) -> float:
        n = self.now()
        close_dt = datetime.combine(n.date(), close_time(n.date()), tzinfo=ET)
        return (close_dt - n).total_seconds() / 60

    def at(self, hhmm: str) -> datetime:
        """Today's datetime at HH:MM ET (clamped to the early close on half days)."""
        n = self.now()
        t = parse_hhmm(hhmm)
        ct = close_time(n.date())
        if t > ct:
            # keep the same distance before the close on half days
            delta = datetime.combine(n.date(), MARKET_CLOSE) - datetime.combine(n.date(), t)
            return datetime.combine(n.date(), ct, tzinfo=ET) - delta
        return datetime.combine(n.date(), t, tzinfo=ET)


class RealClock(Clock):
    def now(self) -> datetime:
        return datetime.now(ET)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SimClock(Clock):
    """Starts at ``start`` and advances ``speed`` times faster than real time."""

    def __init__(self, start: datetime, speed: float = 30.0):
        self._start = start if start.tzinfo else start.replace(tzinfo=ET)
        self._speed = speed
        self._t0 = _time.monotonic()
        self._offset = timedelta(0)

    @property
    def speed(self) -> float:
        return self._speed

    def now(self) -> datetime:
        elapsed = (_time.monotonic() - self._t0) * self._speed
        return self._start + timedelta(seconds=elapsed) + self._offset

    def advance(self, seconds: float) -> None:
        self._offset += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(seconds / self._speed, 0.01))


class ManualClock(Clock):
    """Fully controlled clock for tests and backtests."""

    def __init__(self, start: datetime):
        self._now = start if start.tzinfo else start.replace(tzinfo=ET)

    def now(self) -> datetime:
        return self._now

    def set(self, dt: datetime) -> None:
        self._now = dt if dt.tzinfo else dt.replace(tzinfo=ET)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)
