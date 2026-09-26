"""Market scanner agent: decides *what* to watch.

Builds a "hot list" from your core watchlist plus the day's movers (top gainers/losers and
unusual relative volume from the data source's screeners), filtered for liquidity, then
computes fresh features for each symbol for the strategy agents.
"""
from __future__ import annotations

import asyncio
import logging

from ..config import Settings
from ..data.base import MarketDataProvider
from ..models import Quote
from .features import SymbolFeatures, build_features

log = logging.getLogger(__name__)


class ScannerAgent:
    def __init__(self, settings: Settings, data: MarketDataProvider):
        self.s = settings
        self.data = data
        self.hot_list: list[str] = list(settings.scanner.core_watchlist)
        self.movers: list[str] = []
        self.earnings: set[str] = set()
        self.quotes: dict[str, Quote] = {}
        self.features: dict[str, SymbolFeatures] = {}
        self._avg_vol: dict[str, float] = {}

    async def refresh_universe(self) -> list[str]:
        sc = self.s.scanner
        core = list(dict.fromkeys(sc.core_watchlist + sc.zero_dte_symbols))
        movers: list[str] = []
        if sc.use_market_movers:
            try:
                movers = await self.data.movers()
            except Exception as e:
                log.warning("movers scan failed: %s", e)
        candidates = list(dict.fromkeys(core + movers))
        try:
            self.quotes = await self.data.quotes(candidates)
        except Exception as e:
            log.warning("quote scan failed: %s", e)
            return self.hot_list
        liquid_movers = [
            s for s in movers if (q := self.quotes.get(s)) and q.last >= sc.min_price and q.volume >= sc.min_day_volume
        ]
        liquid_movers.sort(key=lambda s: abs(self.quotes[s].change_pct), reverse=True)
        self.movers = liquid_movers
        room = max(sc.max_hot_list - len(core), 0)
        self.hot_list = list(dict.fromkeys(core + liquid_movers[:room]))
        try:
            self.earnings = await self.data.earnings_symbols(self.hot_list)
        except Exception:
            pass
        return self.hot_list

    async def _avg_volume(self, symbol: str) -> float:
        if symbol not in self._avg_vol:
            fn = getattr(self.data, "avg_daily_volume", None)
            if fn:
                self._avg_vol[symbol] = await fn(symbol)
            else:
                daily = await self.data.daily_bars(symbol, 20)
                self._avg_vol[symbol] = sum(b.volume for b in daily) / len(daily) if daily else 0.0
        return self._avg_vol[symbol]

    async def build_all_features(self) -> dict[str, SymbolFeatures]:
        now = self.data.clock.now()
        try:
            self.quotes.update(await self.data.quotes(self.hot_list))
        except Exception as e:
            log.warning("quote refresh failed: %s", e)

        async def one(sym: str) -> tuple[str, SymbolFeatures | None]:
            try:
                bars = await self.data.intraday_bars(sym, 400)
                daily = await self.data.daily_bars(sym, 20)
                q = self.quotes.get(sym)
                f = build_features(sym, now, bars, q.prev_close if q else 0.0, daily, await self._avg_volume(sym),
                                   price=q.last if q else None, earnings=sym in self.earnings,
                                   orb_minutes=self.s.strategies.orb_minutes)
                return sym, f
            except Exception as e:
                log.warning("features %s failed: %s", sym, e)
                return sym, None

        results = await asyncio.gather(*(one(s) for s in self.hot_list))
        self.features = {s: f for s, f in results if f is not None}
        return self.features

    def reset_day(self) -> None:
        self._avg_vol.clear()
        self.features.clear()
        self.earnings.clear()
