import math

import numpy as np

from optrader.data import indicators as ind
from optrader.data.options_math import bs_greeks, bs_price, implied_vol

from .conftest import make_bars


def test_put_call_parity():
    s, k, t, r, v = 100.0, 105.0, 30 / 365, 0.04, 0.3
    c = bs_price(s, k, t, r, v, "C")
    p = bs_price(s, k, t, r, v, "P")
    assert math.isclose(c - p, s - k * math.exp(-r * t), abs_tol=1e-6)


def test_implied_vol_roundtrip():
    price = bs_price(250, 255, 5 / 365, 0.04, 0.42, "C")
    iv = implied_vol(price, 250, 255, 5 / 365, 0.04, "C")
    assert abs(iv - 0.42) < 1e-3


def test_delta_ranges():
    g_call = bs_greeks(100, 100, 10 / 365, 0.04, 0.3, "C")
    g_put = bs_greeks(100, 100, 10 / 365, 0.04, 0.3, "P")
    assert 0.45 < g_call["delta"] < 0.6
    assert -0.55 < g_put["delta"] < -0.4
    assert g_call["theta"] < 0


def test_vwap_and_ema():
    bars = make_bars([10, 11, 12, 13], volume=[100, 100, 100, 100])
    v = ind.vwap(bars)
    assert 10 < v[-1] < 13
    e = ind.ema(np.array([1.0] * 10 + [2.0] * 10), 5)
    assert 1.9 < e[-1] <= 2.0


def test_opening_range_only_after_period():
    assert ind.opening_range(make_bars([100 + i * 0.1 for i in range(10)]), 15) is None
    hi, lo = ind.opening_range(make_bars([100 + i * 0.1 for i in range(20)]), 15)
    assert hi >= lo


def test_relative_volume():
    bars = make_bars([100] * 60, volume=20_000)  # 1.2M in the first hour
    assert ind.relative_volume(bars, avg_daily_volume=3_000_000, minutes_elapsed=60) > 1.0


def test_bars_to_timeframe():
    bars = make_bars(list(range(100, 110)))
    five = ind.bars_to_timeframe(bars, 5)
    assert len(five) == 2
    assert five[0].open == bars[0].open and five[0].close == bars[4].close
