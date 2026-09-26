"""Picks the option contract for a signal: right expiry, liquid, tight spread, target delta."""
from __future__ import annotations

import logging
from datetime import date

from ..config import ContractSettings
from ..data.base import MarketDataProvider
from ..models import OptionContract, Signal

log = logging.getLogger(__name__)

PENNY_ALL_PRICES = {"SPY", "QQQ", "IWM"}


def tick_size(underlying: str, price: float) -> float:
    """Penny Interval Program: $0.01 under $3, $0.05 at/above $3 (SPY/QQQ/IWM: pennies at all prices)."""
    if underlying in PENNY_ALL_PRICES or price < 3:
        return 0.01
    return 0.05


def round_to_tick(price: float, tick: float, direction: str = "nearest") -> float:
    import math
    n = price / tick
    if direction == "up":
        n = math.ceil(n - 1e-9)
    elif direction == "down":
        n = math.floor(n + 1e-9)
    else:
        n = round(n)
    return round(max(n * tick, tick), 2)


class ContractSelector:
    def __init__(self, cfg: ContractSettings, data: MarketDataProvider):
        self.cfg = cfg
        self.data = data

    async def select(self, sig: Signal, today: date,
                     max_premium: float | None = None) -> tuple[OptionContract | None, str]:
        exps = await self.data.expirations(sig.symbol)
        lo = max(sig.min_dte, self.cfg.min_dte if not sig.zero_dte else 0)
        hi = min(sig.max_dte, self.cfg.max_dte)
        candidates = [e for e in exps if lo <= (e - today).days <= hi]
        if not candidates:
            return None, f"no expiration between {lo} and {hi} DTE"
        target_delta = sig.target_delta or (self.cfg.zero_dte_target_delta if sig.zero_dte else self.cfg.target_delta)
        rejects: dict[str, int] = {}
        for expiry in candidates[:2]:  # nearest first; fall back to the next one
            chain = await self.data.option_chain(sig.symbol, expiry, sig.right,
                                                 sig.underlying_price * 0.85, sig.underlying_price * 1.15)
            good: list[OptionContract] = []
            for c in chain:
                reason = self._reject_reason(c, max_premium)
                if reason:
                    rejects[reason] = rejects.get(reason, 0) + 1
                else:
                    good.append(c)
            if good:
                best = min(good, key=lambda c: (abs(abs(c.delta or 0) - target_delta), c.spread_pct))
                return best, f"delta {abs(best.delta or 0):.2f}, spread {best.spread_pct*100:.1f}%, OI {best.open_interest:,}"
        detail = ", ".join(f"{k}: {v}" for k, v in sorted(rejects.items(), key=lambda kv: -kv[1])[:3])
        return None, f"no liquid contract ({detail or 'empty chain'})"

    def _reject_reason(self, c: OptionContract, max_premium: float | None = None) -> str | None:
        if c.delta is None:
            return "no greeks"
        if c.bid <= 0 or c.ask <= 0:
            return "no bid/ask"
        if c.spread_pct > self.cfg.max_spread_pct:
            return "wide spread"
        if c.open_interest < self.cfg.min_open_interest:
            return "low open interest"
        if c.volume < self.cfg.min_volume:
            return "low volume"
        if not (self.cfg.min_premium <= c.mid <= self.cfg.max_premium):
            return "premium out of range"
        if max_premium is not None and c.mid > max_premium:
            return "too expensive for risk budget"
        if not (0.15 <= abs(c.delta) <= 0.75):
            return "delta out of range"
        return None
