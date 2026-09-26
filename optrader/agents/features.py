"""Per-symbol feature snapshot that every strategy agent reads.

Built from data available *at time t only*, so the same code runs live and in backtests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np

from ..clock import ET
from ..data import indicators as ind
from ..models import Bar


@dataclass
class SymbolFeatures:
    symbol: str
    now: datetime
    price: float
    prev_close: float
    open: float
    session: list[Bar]                   # today's completed 1-minute bars
    avg_daily_volume: float
    prev_day_high: float | None = None
    prev_day_low: float | None = None
    earnings: bool = False
    orb_minutes: int = 15

    # derived
    vwap: float = 0.0
    vwap_series: np.ndarray = field(default_factory=lambda: np.array([]))
    ema9: float = 0.0
    ema21: float = 0.0
    ema9_series: np.ndarray = field(default_factory=lambda: np.array([]))
    ema9_5m: float = 0.0
    ema21_5m: float = 0.0
    rvol: float = 0.0
    orb_high: float | None = None
    orb_low: float | None = None
    atr_5m: float | None = None
    rsi: float | None = None
    day_high: float = 0.0
    day_low: float = 0.0
    minutes_since_open: float = 0.0

    @property
    def gap_pct(self) -> float:
        return (self.open / self.prev_close - 1) * 100 if self.prev_close and self.open else 0.0

    @property
    def change_pct(self) -> float:
        return (self.price / self.prev_close - 1) * 100 if self.prev_close else 0.0

    @property
    def last_bar(self) -> Bar | None:
        return self.session[-1] if self.session else None

    def avg_bar_volume(self, n: int = 20) -> float:
        vols = [b.volume for b in self.session[-n - 1:-1]]
        return float(np.mean(vols)) if vols else 0.0

    def summary(self) -> dict:
        return {
            "symbol": self.symbol, "price": round(self.price, 2), "change_pct": round(self.change_pct, 2),
            "gap_pct": round(self.gap_pct, 2), "rvol": round(self.rvol, 2), "vwap": round(self.vwap, 2),
            "above_vwap": self.price > self.vwap if self.vwap else None,
            "trend": "up" if self.ema9 > self.ema21 else "down",
            "orb_high": round(self.orb_high, 2) if self.orb_high else None,
            "orb_low": round(self.orb_low, 2) if self.orb_low else None,
            "rsi": round(self.rsi, 1) if self.rsi else None,
            "earnings": self.earnings,
        }


def build_features(symbol: str, now: datetime, bars_1m: list[Bar], prev_close: float, daily: list[Bar],
                   avg_daily_volume: float, price: float | None = None, earnings: bool = False,
                   orb_minutes: int = 15) -> SymbolFeatures | None:
    day: date = now.astimezone(ET).date()
    # only bars that have fully completed before `now`
    session = [b for b in ind.session_bars(bars_1m, day) if (now - b.ts).total_seconds() >= 60]
    if not session:
        return None
    last_px = price or session[-1].close
    f = SymbolFeatures(
        symbol=symbol, now=now, price=last_px, prev_close=prev_close or (daily[-1].close if daily else 0.0),
        open=session[0].open, session=session, avg_daily_volume=avg_daily_volume,
        prev_day_high=daily[-1].high if daily else None, prev_day_low=daily[-1].low if daily else None,
        earnings=earnings, orb_minutes=orb_minutes,
    )
    c = ind.closes(session)
    f.vwap_series = ind.vwap(session)
    f.vwap = float(f.vwap_series[-1])
    e9 = ind.ema(c, 9)
    f.ema9_series = e9
    f.ema9, f.ema21 = float(e9[-1]), float(ind.ema(c, 21)[-1])
    b5 = ind.bars_to_timeframe(session, 5)
    c5 = ind.closes(b5)
    f.ema9_5m, f.ema21_5m = float(ind.ema(c5, 9)[-1]), float(ind.ema(c5, 21)[-1])
    f.atr_5m = ind.atr(b5, 14)
    f.rsi = ind.rsi(c, 14)
    f.minutes_since_open = (now - session[0].ts).total_seconds() / 60
    f.rvol = ind.relative_volume(session, avg_daily_volume, f.minutes_since_open)
    orb = ind.opening_range(session, orb_minutes)
    if orb:
        f.orb_high, f.orb_low = orb
    f.day_high = max(b.high for b in session)
    f.day_low = min(b.low for b in session)
    return f
