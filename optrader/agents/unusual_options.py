"""Unusual options activity (UOA) agent.

Flags contracts trading far more volume than their open interest with large premium, which
often means fresh (opening) positioning. Caveat shown to the user: snapshot data can't tell
whether prints were buys or sells, so we require the underlying's price action to agree.
"""
from __future__ import annotations

import logging
from datetime import datetime

from ..config import StrategySettings
from ..data.base import MarketDataProvider
from ..models import OptionContract, Signal
from .features import SymbolFeatures

log = logging.getLogger(__name__)


class UnusualOptionsAgent:
    name = "unusual_options"

    def __init__(self, cfg: StrategySettings, data: MarketDataProvider, max_symbols: int = 8, max_dte: int = 14):
        self.cfg = cfg
        self.data = data
        self.max_symbols = max_symbols
        self.max_dte = max_dte
        self.last_hits: list[dict] = []   # shown on the dashboard

    def _is_unusual(self, c: OptionContract) -> bool:
        if c.volume < 500 or c.mid <= 0:
            return False
        ratio = c.volume / max(c.open_interest, 1)
        premium = c.volume * c.mid * 100
        return ratio >= self.cfg.uoa_min_vol_oi_ratio and premium >= self.cfg.uoa_min_premium

    async def scan(self, features: dict[str, SymbolFeatures], now: datetime) -> list[Signal]:
        # prioritise the most active / moving names to stay inside API rate limits
        ranked = sorted(features.values(), key=lambda f: (f.rvol, abs(f.change_pct)), reverse=True)
        signals: list[Signal] = []
        hits: list[dict] = []
        for f in ranked[: self.max_symbols]:
            try:
                exps = [e for e in await self.data.expirations(f.symbol) if (e - now.date()).days <= self.max_dte][:3]
                unusual: list[OptionContract] = []
                call_prem = put_prem = 0.0
                for e in exps:
                    chain = await self.data.option_chain(f.symbol, e, None, f.price * 0.92, f.price * 1.08)
                    for c in chain:
                        prem = c.volume * c.mid * 100
                        if c.right == "C":
                            call_prem += prem
                        else:
                            put_prem += prem
                        if self._is_unusual(c):
                            unusual.append(c)
            except Exception as e:
                log.warning("UOA scan %s failed: %s", f.symbol, e)
                continue
            if not unusual:
                continue
            best = max(unusual, key=lambda c: c.volume * c.mid)
            bullish = best.right == "C"
            ratio = best.volume / max(best.open_interest, 1)
            premium = best.volume * best.mid * 100
            hits.append({"symbol": f.symbol, "contract": best.symbol, "label": best.label(), "volume": best.volume,
                         "open_interest": best.open_interest, "vol_oi": round(ratio, 1), "premium": round(premium),
                         "ts": now.isoformat()})
            flow_bias = call_prem / (call_prem + put_prem) if (call_prem + put_prem) else 0.5
            price_agrees = (f.price > f.vwap and f.ema9 > f.ema21) if bullish else (f.price < f.vwap and f.ema9 < f.ema21)
            score = 50 + min(15.0, ratio * 1.5) + min(10.0, premium / 250_000 * 2)
            reasons = [
                f"unusual {'call' if bullish else 'put'} volume: {best.label()} traded {best.volume:,} vs OI "
                f"{best.open_interest:,} ({ratio:.1f}x), ~${premium/1e6:.2f}M premium",
                f"session flow {flow_bias*100:.0f}% calls / {(1-flow_bias)*100:.0f}% puts by premium",
                "note: trade side (bought vs sold) unknown from snapshot data",
            ]
            if price_agrees:
                score += 10
                reasons.append("underlying price action agrees (VWAP + EMA trend)")
            else:
                score -= 20
                reasons.append("underlying price action does NOT agree yet")
            if (bullish and flow_bias > 0.65) or (not bullish and flow_bias < 0.35):
                score += 5
            dte = (best.expiry - now.date()).days
            risk = (f.atr_5m or f.price * 0.003) * 2
            signals.append(Signal(
                ts=now, symbol=f.symbol, strategy=self.name, direction="bullish" if bullish else "bearish",
                score=round(max(0.0, min(100.0, score)), 1), reasons=reasons, underlying_price=round(f.price, 2),
                stop_underlying=round(f.price - risk if bullish else f.price + risk, 2),
                target_underlying=round(f.price + 2 * risk if bullish else f.price - 2 * risk, 2),
                min_dte=max(1, min(dte, 3)), max_dte=max(dte, 3), target_delta=0.40,
                meta={"setup": "uoa", "flow_contract": best.symbol, "vol_oi": round(ratio, 1)},
            ))
        if hits:
            self.last_hits = (hits + self.last_hits)[:30]
        return signals
