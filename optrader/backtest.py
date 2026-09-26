"""Backtester: replays historical 1-minute bars through the SAME strategy, risk and exit code.

Honest limitations (read these before trusting a number):
  * Historical option quotes aren't free, so option prices are MODELED with Black-Scholes using an
    implied vol estimated from the stock's realized volatility × ``iv_mult``, plus a bid/ask spread.
    Real fills, IV crush and skew will differ.
  * Unusual-options-activity signals can't be backtested (no historical options volume/OI).
  * Earnings flags are unknown historically, so catalyst signals get no earnings bonus.
Use it to compare settings and weed out bad ideas, then confirm in paper trading.
"""
from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from .agents.contract_selector import round_to_tick, tick_size
from .agents.features import build_features
from .agents.position_manager import decide_exit
from .agents.risk import RiskManager
from .agents.strategies import CatalystAgent, MomentumAgent, ZeroDTEAgent
from .analytics import journal_stats
from .clock import ET, ManualClock, is_trading_day
from .config import Settings
from .data.base import RISK_FREE
from .data.options_math import bs_greeks, bs_price
from .db import Database
from .orchestrator import SIGNAL_COOLDOWN_MIN
from .models import AccountSnapshot, Bar, OptionContract, Position, Signal, occ_symbol

INDEX = {"SPY", "QQQ", "IWM"}


@dataclass
class BacktestConfig:
    iv_mult: float = 1.15            # implied vol ≈ realized vol × this
    spread_pct: float = 0.05         # single names: (ask-bid)/mid
    index_spread_pct: float = 0.02   # SPY/QQQ/IWM
    fee_per_contract: float = 0.65


def _strike_step(sym: str, spot: float) -> float:
    if sym in INDEX:
        return 1.0
    return 0.5 if spot < 25 else 1.0 if spot < 100 else 2.5 if spot < 250 else 5.0 if spot < 700 else 10.0


def _expiries(sym: str, today: date, n: int = 10) -> list[date]:
    out, d = [], today
    while len(out) < n:
        if is_trading_day(d) and (sym in INDEX or d.weekday() == 4):
            out.append(d)
        d += timedelta(days=1)
    return out


def _years(now: datetime, expiry: date) -> float:
    close = datetime.combine(expiry, datetime.min.time(), tzinfo=ET).replace(hour=16)
    return max((close - now).total_seconds(), 60) / (365 * 24 * 3600)


class Backtester:
    def __init__(self, settings: Settings, bars: dict[str, list[Bar]], cfg: BacktestConfig | None = None):
        self.s = settings
        self.bars = {s: sorted(b, key=lambda x: x.ts) for s, b in bars.items() if b}
        self.cfg = cfg or BacktestConfig()
        st = settings.strategies
        self.agents = []
        if st.momentum:
            self.agents.append(MomentumAgent(st))
        if st.zero_dte:
            self.agents.append(ZeroDTEAgent(st, settings.scanner.zero_dte_symbols, settings.contracts.zero_dte_target_delta))
        if st.catalyst:
            self.agents.append(CatalystAgent(st))
        self.clock = ManualClock(datetime(2000, 1, 3, 9, 30, tzinfo=ET))
        self.db = Database(":memory:")
        self.risk = RiskManager(settings, self.db, self.clock)
        self.cash = settings.risk.starting_equity
        self.closed: list[Position] = []
        self.open: list[Position] = []
        self.signals = 0
        self.blocked: dict[str, int] = {}
        self._cooldown: dict[tuple, datetime] = {}

    # ---- pricing ---------------------------------------------------------------------------------------
    def _iv(self, sym: str, day: date) -> float:
        prior = [b for b in self.bars[sym] if b.ts.date() < day][-390 * 5:]
        if len(prior) < 60:
            return 0.35
        c = np.array([b.close for b in prior])
        r = np.diff(np.log(c))
        minutes_per_bar = max((prior[-1].ts - prior[-2].ts).total_seconds() / 60, 1)
        rv = float(np.std(r) * math.sqrt(252 * 390 / minutes_per_bar))
        return max(0.12, rv * self.cfg.iv_mult)

    def _quote(self, sym: str, expiry: date, strike: float, right: str, spot: float, iv: float,
               now: datetime) -> OptionContract:
        t = _years(now, expiry)
        mid = max(bs_price(spot, strike, t, RISK_FREE, iv, right), 0.01)
        g = bs_greeks(spot, strike, t, RISK_FREE, iv, right)
        sp = self.cfg.index_spread_pct if sym in INDEX else self.cfg.spread_pct
        half = max(mid * sp / 2, 0.005)
        return OptionContract(symbol=occ_symbol(sym, expiry, right, strike), underlying=sym, expiry=expiry,
                              strike=strike, right=right, bid=round(max(mid - half, 0.0), 2),
                              ask=round(mid + half, 2), last=round(mid, 2), volume=10_000, open_interest=10_000,
                              iv=iv, delta=g["delta"], gamma=g["gamma"], theta=g["theta"], vega=g["vega"])

    def _select(self, sig: Signal, spot: float, iv: float, now: datetime, max_prem: float) -> OptionContract | None:
        today = now.date()
        exps = [e for e in _expiries(sig.symbol, today) if sig.min_dte <= (e - today).days <= sig.max_dte]
        if not exps:
            return None
        expiry = exps[0]
        target = sig.target_delta or (self.s.contracts.zero_dte_target_delta if sig.zero_dte else self.s.contracts.target_delta)
        step = _strike_step(sig.symbol, spot)
        best = None
        k = math.floor(spot * 0.9 / step) * step
        while k <= spot * 1.1:
            c = self._quote(sig.symbol, expiry, round(k, 2), sig.right, spot, iv, now)
            ok = self.s.contracts.min_premium <= c.mid <= min(self.s.contracts.max_premium, max_prem) and \
                0.15 <= abs(c.delta or 0) <= 0.75
            if ok and (best is None or abs(abs(c.delta) - target) < abs(abs(best.delta) - target)):
                best = c
            k += step
        return best

    # ---- account ---------------------------------------------------------------------------------------------
    def _account(self) -> AccountSnapshot:
        eq = self.cash + sum(p.last_price * p.qty * 100 for p in self.open)
        return AccountSnapshot(equity=eq, cash=self.cash, buying_power=self.cash, source="backtest")

    def _positions_today(self, day: date) -> list[Position]:
        return [p for p in self.closed if p.entry_time.date() == day] + self.open

    # ---- main loop ---------------------------------------------------------------------------------------------
    def run(self) -> dict:
        days = sorted({b.ts.astimezone(ET).date() for bars in self.bars.values() for b in bars})
        for i, day in enumerate(days):
            if i == 0:
                continue  # first day only seeds prev close / IV
            self._run_day(day, days[i - 1])
        trades = sorted(self.closed, key=lambda p: p.entry_time)
        return {
            "days": len(days) - 1, "symbols": list(self.bars), "signals": self.signals,
            "blocked_by": dict(sorted(self.blocked.items(), key=lambda kv: -kv[1])[:8]),
            "starting_equity": self.s.risk.starting_equity, "ending_equity": round(self.cash, 2),
            "stats": journal_stats(trades), "trades": trades,
        }

    def _run_day(self, day: date, prev_day: date) -> None:
        day_bars = {s: [b for b in bars if b.ts.astimezone(ET).date() == day] for s, bars in self.bars.items()}
        day_bars = {s: b for s, b in day_bars.items() if b}
        if not day_bars:
            return
        prev_close, avg_vol, iv, hist = {}, {}, {}, {}
        for s in day_bars:
            prior = [b for b in self.bars[s] if b.ts.astimezone(ET).date() < day]
            if not prior:
                continue
            prev_close[s] = prior[-1].close
            by_day: dict[date, float] = {}
            for b in prior:
                by_day[b.ts.date()] = by_day.get(b.ts.date(), 0) + b.volume
            avg_vol[s] = float(np.mean(list(by_day.values())[-20:]))
            iv[s] = self._iv(s, day)
            prev_bars = [b for b in prior if b.ts.astimezone(ET).date() == prior[-1].ts.astimezone(ET).date()]
            hist[s] = [Bar(ts=datetime.combine(prior[-1].ts.date(), datetime.min.time(), tzinfo=ET),
                           open=prev_bars[0].open, high=max(b.high for b in prev_bars),
                           low=min(b.low for b in prev_bars), close=prev_bars[-1].close,
                           volume=sum(b.volume for b in prev_bars))]
        times = sorted({b.ts for bars in day_bars.values() for b in bars})
        idx = {s: 0 for s in day_bars}
        for t in times:
            now = t + timedelta(minutes=1)  # bar t has closed
            self.clock.set(now)
            spots = {}
            for s, bars in day_bars.items():
                while idx[s] < len(bars) and bars[idx[s]].ts <= t:
                    idx[s] += 1
                if idx[s]:
                    spots[s] = bars[idx[s] - 1].close
            self._manage_exits(now, spots, iv)
            if not self.clock.is_market_open():
                continue
            for s, bars in day_bars.items():
                if s not in prev_close or not idx[s]:
                    continue
                f = build_features(s, now, bars[: idx[s]], prev_close[s], hist.get(s, []), avg_vol[s],
                                   orb_minutes=self.s.strategies.orb_minutes)
                if not f:
                    continue
                for agent in self.agents:
                    for sig in agent.evaluate(f):
                        key = (sig.symbol, sig.strategy, sig.meta.get("setup"), sig.direction)
                        last = self._cooldown.get(key)
                        if last and (now - last).total_seconds() < SIGNAL_COOLDOWN_MIN * 60:
                            continue
                        self._cooldown[key] = now
                        self.signals += 1
                        self._try_enter(sig, spots[s], iv[s], now)
        # safety: anything still open at the end of the data is closed at the last price
        for p in list(self.open):
            self._book(p, p.qty, p.contract.bid or p.last_price, "end of data", self.clock.now())

    def _try_enter(self, sig: Signal, spot: float, iv: float, now: datetime) -> None:
        if sig.score < self.s.risk.min_signal_score:
            return
        acct = self._account()
        blocks = self.risk.global_blocks(sig.zero_dte, acct, self._positions_today(now.date()), self.open)
        if any(p.underlying == sig.symbol for p in self.open):
            blocks.append("already holding symbol")
        if blocks:
            key = blocks[0].split(" (")[0]
            self.blocked[key] = self.blocked.get(key, 0) + 1
            return
        c = self._select(sig, spot, iv, now, self.risk.max_affordable_premium(sig.zero_dte, acct))
        if not c:
            self.blocked["no affordable contract"] = self.blocked.get("no affordable contract", 0) + 1
            return
        tick = tick_size(c.underlying, c.mid)
        entry = round_to_tick(c.mid + (c.ask - c.mid) * 0.5, tick, "up")
        d = self.risk.check_entry(sig, c, entry, acct, self._positions_today(now.date()), self.open)
        if not d.ok:
            key = d.blocks[0].split(" (")[0]
            self.blocked[key] = self.blocked.get(key, 0) + 1
            return
        qty = d.qty
        self.cash -= entry * qty * 100 + self.cfg.fee_per_contract * qty
        plan = self.risk.exit_plan(sig, c, entry, qty)
        self.open.append(Position(strategy=sig.strategy, underlying=sig.symbol, contract=c, qty=qty, initial_qty=qty,
                                  entry_price=entry, entry_time=now, exit_plan=plan, high_water=entry,
                                  last_price=entry, stop_price=plan.stop_price))

    def _manage_exits(self, now: datetime, spots: dict[str, float], iv: dict[str, float]) -> None:
        for p in list(self.open):
            spot = spots.get(p.underlying)
            if spot is None:
                continue
            q = self._quote(p.underlying, p.contract.expiry, p.contract.strike, p.contract.right, spot,
                            iv.get(p.underlying, 0.3), now)
            p.last_price = q.mid
            p.contract.bid, p.contract.ask = q.bid, q.ask
            p.high_water = max(p.high_water, q.mid)
            reason, qty = decide_exit(p, now, spot, self.clock.at(p.exit_plan.flatten_at))
            if reason:
                urgent = not reason.startswith("target")
                px = q.bid if urgent else round(q.mid - (q.mid - q.bid) * 0.5, 2)
                self._book(p, qty, max(px, 0.01), reason, now)

    def _book(self, p: Position, qty: int, px: float, reason: str, now: datetime) -> None:
        from .models import ExitFill
        if not p.exits and p.entry_time.date() == now.date():
            self.db.record_day_trade(now.date(), p.underlying, p.id)
        pnl = (px - p.entry_price) * qty * 100 - self.cfg.fee_per_contract * qty * 2
        self.cash += px * qty * 100 - self.cfg.fee_per_contract * qty
        p.exits.append(ExitFill(ts=now, qty=qty, price=px, reason=reason, pnl=round(pnl, 2)))
        p.realized_pnl = round(p.realized_pnl + pnl, 2)
        p.qty -= qty
        if reason.startswith("target 1"):
            p.target1_done = True
            if self.s.exits.breakeven_after_target1:
                p.stop_price = max(p.stop_price, p.entry_price)
        if p.qty <= 0:
            p.status, p.closed_time = "closed", now
            self.open.remove(p)
            self.closed.append(p)


# ---- data loading + reporting ------------------------------------------------------------------------------------
async def load_bars(source: str, symbols: list[str], days: int, interval: str = "1m", seed: int = 7) -> dict[str, list[Bar]]:
    if source == "sim":
        from .clock import previous_trading_days
        from .data.simulated import SimulatedMarket
        clock = ManualClock(datetime.now(ET))
        sim = SimulatedMarket(clock, symbols, seed)
        out: dict[str, list[Bar]] = {s: [] for s in symbols}
        for d in previous_trading_days(datetime.now(ET).date() - timedelta(days=1), days + 1):
            for s in symbols:
                out[s].extend(sim._sd(s, d).bars)
        return out
    if source == "yahoo":
        from .data.yahoo_data import YahooData
        yd = YahooData(ManualClock(datetime.now(ET)))
        out = {}
        for s in symbols:
            try:
                out[s] = await (yd.history_1m(s, days + 1) if interval == "1m" else yd.history_5m(s, days + 1))
            except Exception as e:
                print(f"warning: could not download {s} from Yahoo: {str(e).splitlines()[0][:150]}")
                out[s] = []
        return out
    raise ValueError(f"unknown source {source}")


def save_report(result: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"backtest_{stamp}"
    with open(f"{path}_trades.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["entry_time", "symbol", "strategy", "contract", "qty", "entry", "exits", "pnl"])
        for p in result["trades"]:
            w.writerow([p.entry_time.isoformat(), p.underlying, p.strategy, p.contract.symbol, p.initial_qty,
                        p.entry_price, " | ".join(f"{x.qty}@{x.price} {x.reason}" for x in p.exits), p.realized_pnl])
    summary = {k: v for k, v in result.items() if k != "trades"}
    Path(f"{path}_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return path


def format_report(result: dict) -> str:
    o = result["stats"]["overall"]
    lines = [
        f"Backtest: {result['days']} day(s), {len(result['symbols'])} symbols, {result['signals']} raw signals",
        f"Equity ${result['starting_equity']:,.0f} -> ${result['ending_equity']:,.0f}",
        f"Trades {o['trades']} | win rate {o['win_rate']}% | net ${o['net_pnl']:,.2f} | PF {o['profit_factor']} | "
        f"expectancy ${o['expectancy']:,.2f} | max DD ${o['max_drawdown']:,.2f}",
        "",
        f"{'strategy':<18}{'trades':>7}{'win%':>7}{'net $':>11}{'PF':>7}",
    ]
    for k, s in result["stats"]["by_strategy"].items():
        lines.append(f"{k:<18}{s['trades']:>7}{s['win_rate']:>7}{s['net_pnl']:>11,.2f}{str(s['profit_factor']):>7}")
    lines.append("")
    lines.append(f"{'exit reason':<30}{'trades':>7}{'net $':>11}")
    for k, s in result["stats"]["by_exit_reason"].items():
        lines.append(f"{k:<30}{s['trades']:>7}{s['net_pnl']:>11,.2f}")
    if result["blocked_by"]:
        lines.append("")
        lines.append("Signals not taken (top reasons): " + ", ".join(f"{k}: {v}" for k, v in result["blocked_by"].items()))
    lines.append("")
    lines.append("NOTE: option prices are modeled (Black-Scholes + spread), not real fills. Paper trade before going live.")
    return "\n".join(lines)
