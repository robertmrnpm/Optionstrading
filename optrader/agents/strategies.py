"""Price-action strategy agents. Pure functions of SymbolFeatures -> Signals (live == backtest).

Scores are 0-100. They combine the base quality of the setup with confirmations
(relative volume, multi-timeframe trend, VWAP side, breakout-bar volume). Treat them as a
ranking, not a probability — use the backtester and your paper-trading journal to calibrate.
"""
from __future__ import annotations

from ..config import StrategySettings
from ..models import Signal
from .features import SymbolFeatures


def _clamp(x: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, x))


def _vol_confirm(f: SymbolFeatures) -> float:
    """Breakout-bar volume relative to recent bars (1.0 = average)."""
    avg = f.avg_bar_volume(20)
    return f.last_bar.volume / avg if (f.last_bar and avg > 0) else 1.0


def _signal(f: SymbolFeatures, strategy: str, setup: str, direction: str, score: float, reasons: list[str],
            stop: float, target: float, **kw) -> Signal:
    return Signal(ts=f.now, symbol=f.symbol, strategy=strategy, direction=direction, score=round(_clamp(score), 1),
                  reasons=reasons, underlying_price=round(f.price, 2), stop_underlying=round(stop, 2),
                  target_underlying=round(target, 2), meta={"setup": setup, **kw.pop("meta", {})}, **kw)


class MomentumAgent:
    """Opening-range breakouts, VWAP reclaims and trend pullbacks on liquid names (1-7 DTE)."""

    name = "momentum"

    def __init__(self, cfg: StrategySettings):
        self.cfg = cfg

    def evaluate(self, f: SymbolFeatures) -> list[Signal]:
        out: list[Signal] = []
        if len(f.session) < max(self.cfg.orb_minutes + 2, 22) or not f.vwap:
            return out
        for s in (self._orb(f), self._vwap_reclaim(f), self._trend_pullback(f)):
            if s:
                out.append(s)
        return out

    def _common_score(self, f: SymbolFeatures, bullish: bool) -> tuple[float, list[str]]:
        score, reasons = 0.0, []
        if f.rvol >= self.cfg.min_relative_volume:
            bonus = min(15.0, (f.rvol - 1) * 7)
            score += bonus
            reasons.append(f"relative volume {f.rvol:.1f}x")
        else:
            score -= 10
        mtf = (f.ema9_5m > f.ema21_5m) if bullish else (f.ema9_5m < f.ema21_5m)
        if mtf:
            score += 10
            reasons.append("5-min trend aligned")
        if f.rsi is not None:
            if (bullish and f.rsi > 80) or (not bullish and f.rsi < 20):
                score -= 8
                reasons.append(f"RSI stretched ({f.rsi:.0f})")
        return score, reasons

    def _orb(self, f: SymbolFeatures) -> Signal | None:
        if f.orb_high is None or f.orb_low is None or f.minutes_since_open > 150:
            return None
        bar = f.last_bar
        prev = f.session[-2]
        rng = f.orb_high - f.orb_low
        if rng <= 0:
            return None
        for bullish in (True, False):
            level = f.orb_high if bullish else f.orb_low
            crossed = (bar.close > level >= prev.close) if bullish else (bar.close < level <= prev.close)
            if not crossed:
                continue
            vwap_ok = f.price > f.vwap if bullish else f.price < f.vwap
            trend_ok = f.ema9 > f.ema21 if bullish else f.ema9 < f.ema21
            if not (vwap_ok and trend_ok):
                continue
            score, reasons = self._common_score(f, bullish)
            vc = _vol_confirm(f)
            score += 55 + min(10.0, (vc - 1) * 5)
            reasons.insert(0, f"{self.cfg.orb_minutes}-min opening range {'breakout' if bullish else 'breakdown'} "
                              f"through {level:.2f}")
            reasons.append(f"breakout bar volume {vc:.1f}x avg")
            reasons.append("price on the right side of VWAP and 1-min EMA9/21")
            if bullish:
                stop = max(f.orb_high - 0.5 * rng, f.vwap) if f.vwap < f.price else f.orb_high - 0.5 * rng
                target = f.price + max(rng, (f.price - stop) * 2)
            else:
                stop = min(f.orb_low + 0.5 * rng, f.vwap) if f.vwap > f.price else f.orb_low + 0.5 * rng
                target = f.price - max(rng, (stop - f.price) * 2)
            return _signal(f, self.name, "orb", "bullish" if bullish else "bearish", score, reasons, stop, target,
                           min_dte=1, max_dte=7)
        return None

    def _vwap_reclaim(self, f: SymbolFeatures) -> Signal | None:
        if f.minutes_since_open < 30 or len(f.vwap_series) < 12:
            return None
        closes = [b.close for b in f.session]
        vw = f.vwap_series
        for bullish in (True, False):
            before = [(c < v) if bullish else (c > v) for c, v in zip(closes[-11:-1], vw[-11:-1])]
            if sum(before) < 8:  # spent most of the last 10 minutes on the other side
                continue
            now_ok = closes[-1] > vw[-1] if bullish else closes[-1] < vw[-1]
            slope_ok = (f.ema9_series[-1] > f.ema9_series[-4]) if bullish else (f.ema9_series[-1] < f.ema9_series[-4])
            vc = _vol_confirm(f)
            if not (now_ok and slope_ok and vc >= 1.5):
                continue
            score, reasons = self._common_score(f, bullish)
            score += 48 + min(10.0, (vc - 1.5) * 5)
            reasons.insert(0, f"VWAP {'reclaim' if bullish else 'rejection'} at {vw[-1]:.2f} on {vc:.1f}x volume")
            recent = f.session[-10:]
            if bullish:
                stop = min(b.low for b in recent)
                target = f.price + 2 * (f.price - stop)
            else:
                stop = max(b.high for b in recent)
                target = f.price - 2 * (stop - f.price)
            return _signal(f, self.name, "vwap_reclaim", "bullish" if bullish else "bearish", score, reasons,
                           stop, target, min_dte=1, max_dte=7)
        return None

    def _trend_pullback(self, f: SymbolFeatures) -> Signal | None:
        if f.minutes_since_open < 45 or len(f.session) < 30:
            return None
        closes = [b.close for b in f.session]
        vw = f.vwap_series
        bar = f.last_bar
        for bullish in (True, False):
            side = [(c > v) if bullish else (c < v) for c, v in zip(closes[-30:], vw[-30:])]
            if sum(side) < 27:  # strong trend day: held one side of VWAP for 30 minutes
                continue
            trend = f.ema9 > f.ema21 and f.ema9_5m > f.ema21_5m if bullish else \
                f.ema9 < f.ema21 and f.ema9_5m < f.ema21_5m
            touched = bar.low <= f.ema21 * 1.001 if bullish else bar.high >= f.ema21 * 0.999
            bounced = bar.close > f.ema9 and bar.close > bar.open if bullish else \
                bar.close < f.ema9 and bar.close < bar.open
            if not (trend and touched and bounced):
                continue
            score, reasons = self._common_score(f, bullish)
            score += 50
            reasons.insert(0, f"trend-day pullback to EMA21 ({f.ema21:.2f}) and bounce")
            if bullish:
                stop = min(bar.low, f.vwap) - 0.1 * (f.atr_5m or 0)
                target = max(f.day_high, f.price + 2 * (f.price - stop))
            else:
                stop = max(bar.high, f.vwap) + 0.1 * (f.atr_5m or 0)
                target = min(f.day_low, f.price - 2 * (stop - f.price))
            return _signal(f, self.name, "trend_pullback", "bullish" if bullish else "bearish", score, reasons,
                           stop, target, min_dte=1, max_dte=7)
        return None


class ZeroDTEAgent:
    """Same-day-expiry scalps on SPY/QQQ/IWM: micro-breakouts with trend + VWAP, and prior-day level breaks."""

    name = "zero_dte"

    def __init__(self, cfg: StrategySettings, symbols: list[str], target_delta: float):
        self.cfg = cfg
        self.symbols = set(symbols)
        self.target_delta = target_delta

    def evaluate(self, f: SymbolFeatures) -> list[Signal]:
        if f.symbol not in self.symbols or len(f.session) < 20 or f.minutes_since_open < 15:
            return []
        out = []
        bar, prev = f.last_bar, f.session[-2]
        window = f.session[-16:-1]
        hi15, lo15 = max(b.high for b in window), min(b.low for b in window)
        vc = _vol_confirm(f)
        for bullish in (True, False):
            trend = (f.price > f.vwap and f.ema9 > f.ema21 and f.ema9_5m > f.ema21_5m) if bullish else \
                (f.price < f.vwap and f.ema9 < f.ema21 and f.ema9_5m < f.ema21_5m)
            if not trend:
                continue
            micro = bar.close > hi15 >= prev.close if bullish else bar.close < lo15 <= prev.close
            level = f.prev_day_high if bullish else f.prev_day_low
            level_break = level is not None and (bar.close > level >= prev.close if bullish
                                                 else bar.close < level <= prev.close)
            if not (micro or level_break):
                continue
            score = 52 + min(10.0, (vc - 1) * 6)
            reasons = []
            if level_break:
                score += 10
                reasons.append(f"broke prior-day {'high' if bullish else 'low'} {level:.2f}")
            if micro:
                reasons.append(f"15-min {'high' if bullish else 'low'} break ({hi15 if bullish else lo15:.2f})")
            reasons += ["VWAP + 1m/5m EMA trend aligned", f"breakout volume {vc:.1f}x"]
            if f.rvol >= 1.2:
                score += 5
            if f.rsi is not None and ((bullish and f.rsi > 78) or (not bullish and f.rsi < 22)):
                score -= 10
                reasons.append(f"RSI stretched ({f.rsi:.0f})")
            stop = (min(b.low for b in f.session[-5:]) if bullish else max(b.high for b in f.session[-5:]))
            risk = abs(f.price - stop)
            target = f.price + 2 * risk if bullish else f.price - 2 * risk
            out.append(_signal(f, self.name, "level_break" if level_break else "micro_breakout",
                               "bullish" if bullish else "bearish", score, reasons, stop, target,
                               min_dte=0, max_dte=0, zero_dte=True, target_delta=self.target_delta))
        return out


class CatalystAgent:
    """Gappers (earnings/news): gap-and-go continuation or gap-fade reversal after the open."""

    name = "catalyst"

    def __init__(self, cfg: StrategySettings):
        self.cfg = cfg

    def evaluate(self, f: SymbolFeatures) -> list[Signal]:
        gap = f.gap_pct
        if abs(gap) < self.cfg.catalyst_min_gap_pct or f.minutes_since_open < 5 or f.minutes_since_open > 180:
            return []
        if f.rvol < 2.0 or len(f.session) < 8:
            return []
        up_gap = gap > 0
        bar, prev = f.last_bar, f.session[-2]
        first5 = f.session[:5]
        hi5, lo5 = max(b.high for b in first5), min(b.low for b in first5)
        out = []
        base_reasons = [f"gapped {gap:+.1f}%", f"relative volume {f.rvol:.1f}x"]
        if f.earnings:
            base_reasons.append("earnings catalyst")
        # continuation: break of the first-5-minute extreme in the gap direction, holding VWAP
        go = (bar.close > hi5 >= prev.close and f.price > f.vwap) if up_gap else \
            (bar.close < lo5 <= prev.close and f.price < f.vwap)
        if go:
            score = 58 + min(12.0, (f.rvol - 2) * 4) + (5 if f.earnings else 0)
            stop = max(f.vwap, lo5) if up_gap else min(f.vwap, hi5)
            risk = abs(f.price - stop) or f.price * 0.01
            target = f.price + 2 * risk if up_gap else f.price - 2 * risk
            out.append(_signal(f, "catalyst", "gap_and_go", "bullish" if up_gap else "bearish", score,
                               ["gap-and-go: broke the opening 5-min " + ("high" if up_gap else "low")] + base_reasons,
                               stop, target, min_dte=1, max_dte=10,
                               meta={"iv_note": "post-earnings IV may still be elevated — size down"}))
        # fade: gap fails, loses VWAP and the opening range the other way
        if f.minutes_since_open >= 20 and f.orb_high and f.orb_low:
            fade = (bar.close < f.vwap and bar.close < f.orb_low <= prev.close) if up_gap else \
                (bar.close > f.vwap and bar.close > f.orb_high >= prev.close)
            if fade:
                score = 55 + min(10.0, (f.rvol - 2) * 3)
                stop = f.vwap + 0.5 * (f.orb_high - f.orb_low) if up_gap else f.vwap - 0.5 * (f.orb_high - f.orb_low)
                risk = abs(stop - f.price) or f.price * 0.01
                target = f.price - 2 * risk if up_gap else f.price + 2 * risk
                out.append(_signal(f, "catalyst", "gap_fade", "bearish" if up_gap else "bullish", score,
                                   ["gap fade: lost VWAP and the opening range"] + base_reasons,
                                   stop, target, min_dte=1, max_dte=10))
        return out
