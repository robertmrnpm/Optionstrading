"""Orchestrator: runs the agent team on a loop.

    ScannerAgent ──► features ──► Momentum / 0DTE / Catalyst agents ─┐
         └──────────────────────► Unusual Options agent ─────────────┤
                                                                     ▼
                       signals ─► ContractSelector ─► RiskManager ─► ExecutionAgent ─► (you / AI analyst)
                                                                                           │
                                                                     PositionManager ◄── fills
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from .agents.ai_analyst import AIAnalyst
from .agents.context import Context
from .agents.contract_selector import ContractSelector
from .agents.execution import ExecutionAgent
from .agents.position_manager import PositionManager
from .agents.risk import RiskManager
from .agents.scanner import ScannerAgent
from .agents.strategies import CatalystAgent, MomentumAgent, ZeroDTEAgent
from .agents.unusual_options import UnusualOptionsAgent
from .clock import ET, MARKET_OPEN, SimClock, is_trading_day, next_trading_day
from .models import AccountSnapshot, Proposal, Signal

log = logging.getLogger(__name__)

SIGNAL_COOLDOWN_MIN = 30


class Orchestrator:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        s = ctx.settings
        self.scanner = ScannerAgent(s, ctx.data)
        self.selector = ContractSelector(s.contracts, ctx.data)
        self.risk = RiskManager(s, ctx.db, ctx.clock)
        self.positions = PositionManager(ctx)
        self.ai = AIAnalyst(s.ai)
        self.execution = ExecutionAgent(ctx, self.risk, self.positions, self.ai)
        self.momentum = MomentumAgent(s.strategies)
        self.zero_dte = ZeroDTEAgent(s.strategies, s.scanner.zero_dte_symbols, s.contracts.zero_dte_target_delta)
        self.catalyst = CatalystAgent(s.strategies)
        self.uoa = UnusualOptionsAgent(s.strategies, ctx.data)
        self.account: AccountSnapshot | None = None
        self.recent_signals: list[Signal] = []
        self._cooldown: dict[tuple, datetime] = {}
        self._last_universe: datetime | None = None
        self._last_uoa: datetime | None = None
        self._last_eval_minute: datetime | None = None
        self._last_account: float = 0.0
        self._day: date | None = None
        self._running = False
        self._info_throttle: dict[str, datetime] = {}
        self._goal_notified: date | None = None
        self.last_error: str | None = None

    # ---- lifecycle -------------------------------------------------------------------------------------
    async def run(self) -> None:
        self._running = True
        ctx = self.ctx
        ctx.bus.publish("system", f"Agents started — mode={ctx.settings.app.mode}, data={ctx.data.name}, "
                                  f"broker={ctx.broker.name}, trade_mode={self.execution.effective_mode}")
        while self._running:
            try:
                await self.step()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("orchestrator step failed")
                self.last_error = str(e)[:300]
                ctx.bus.publish("error", f"Agent loop error: {e}", persist=False)
            await ctx.clock.sleep(ctx.settings.app.loop_interval_seconds)

    def stop(self) -> None:
        self._running = False

    async def refresh_account(self, force: bool = False) -> AccountSnapshot | None:
        import time
        if force or time.monotonic() - self._last_account > 15 or self.account is None:
            try:
                self.account = await self.ctx.broker.account()
                self.execution.set_account(self.account)
                self._last_account = time.monotonic()
            except Exception as e:
                log.warning("account refresh failed: %s", e)
        return self.account

    def _roll_sim_day(self) -> None:
        clock = self.ctx.clock
        if not isinstance(clock, SimClock):
            return
        now = clock.now()
        if not is_trading_day(now.date()) or now.time() >= datetime.strptime("16:05", "%H:%M").time():
            nxt = next_trading_day(now.date())
            target = datetime.combine(nxt, MARKET_OPEN, tzinfo=ET) - timedelta(minutes=5)
            clock.advance((target - now).total_seconds())
            self.ctx.bus.publish("system", f"Simulation rolled to {nxt:%a %b %d}")

    # ---- one iteration -------------------------------------------------------------------------------------
    async def step(self) -> None:
        ctx = self.ctx
        self._roll_sim_day()
        now = ctx.clock.now()
        if self._day != now.date():
            self._day = now.date()
            self._cooldown.clear()
            self.scanner.reset_day()
            self._last_universe = None
        await self.refresh_account()
        self.execution.expire_stale()
        await self.positions.tick()
        self._check_profit_goal()

        if not ctx.clock.is_market_open():
            return

        s = ctx.settings
        if self._last_universe is None or (now - self._last_universe).total_seconds() >= s.scanner.interval_seconds:
            await self.scanner.refresh_universe()
            self._last_universe = now

        minute = now.replace(second=0, microsecond=0)
        if self._last_eval_minute == minute:
            return
        self._last_eval_minute = minute

        features = await self.scanner.build_all_features()
        self.execution.features = features
        self.execution.market_context = {k: features[k].summary() for k in ("SPY", "QQQ") if k in features}

        signals: list[Signal] = []
        st = s.strategies
        for f in features.values():
            if st.momentum:
                signals += self.momentum.evaluate(f)
            if st.zero_dte:
                signals += self.zero_dte.evaluate(f)
            if st.catalyst:
                signals += self.catalyst.evaluate(f)
        if st.unusual_options and (self._last_uoa is None or
                                   (now - self._last_uoa).total_seconds() >= st.uoa_interval_seconds):
            self._last_uoa = now
            signals += await self.uoa.scan(features, now)

        await self.handle_signals(signals)

    async def handle_signals(self, signals: list[Signal]) -> list[Proposal]:
        ctx = self.ctx
        now = ctx.clock.now()
        fresh: list[Signal] = []
        for sig in sorted(signals, key=lambda x: x.score, reverse=True):
            key = (sig.symbol, sig.strategy, sig.meta.get("setup"), sig.direction)
            last = self._cooldown.get(key)
            if last and (now - last).total_seconds() < SIGNAL_COOLDOWN_MIN * 60:
                continue
            self._cooldown[key] = now
            fresh.append(sig)
            if sig.score >= 50:
                ctx.db.put("signals", sig, now, sig.strategy)
                ctx.bus.publish("signal", f"{sig.symbol} {sig.direction} [{sig.strategy}/{sig.meta.get('setup')}] "
                                          f"score {sig.score:.0f}: {sig.reasons[0]}",
                                {"signal": sig.model_dump(mode="json")}, persist=False)
        self.recent_signals = (fresh + self.recent_signals)[:100]

        proposals: list[Proposal] = []
        busy = ({p.signal.symbol for p in self.execution.pending()} | {p.underlying for p in self.positions.open_positions()}
                | set(self.execution.inflight.values()))
        min_score = ctx.settings.risk.min_signal_score
        acct = self.account or await self.refresh_account(force=True)
        slots = (ctx.settings.risk.max_open_positions - len(self.positions.open_positions())
                 - len(self.execution.pending()) - len(self.execution.inflight))
        for sig in fresh:
            if sig.score < min_score or sig.symbol in busy:
                continue
            if acct is None or len(proposals) >= max(slots, 0):
                break
            blocks = self.risk.global_blocks(sig.zero_dte, acct, self.positions.positions_today(),
                                             self.positions.open_positions(), inflight=len(self.execution.inflight))
            if blocks:
                self._throttled_info(f"Signal {sig.symbol} {sig.direction} ({sig.strategy}, score {sig.score:.0f}) "
                                     f"not traded: {'; '.join(blocks)}", key="; ".join(blocks))
                continue
            max_premium = self.risk.max_affordable_premium(sig.zero_dte, acct,
                                                           positions_today=self.positions.positions_today())
            try:
                contract, note = await self.selector.select(sig, now.date(), max_premium)
            except Exception as e:
                log.warning("contract selection failed for %s: %s", sig.symbol, e)
                continue
            if not contract:
                ctx.bus.publish("info", f"{sig.symbol} {sig.strategy}: skipped — {note}", persist=False)
                continue
            p = await self.execution.propose(sig, contract, note)
            proposals.append(p)
            busy.add(sig.symbol)
        return proposals

    def _check_profit_goal(self) -> None:
        target = self.ctx.settings.risk.daily_profit_target
        today = self.ctx.clock.today()
        if target <= 0 or self._goal_notified == today:
            return
        st = self.risk.day_stats(self.positions.positions_today(), self.positions.open_positions())
        if st.total >= target:
            self._goal_notified = today
            stop = self.ctx.settings.risk.stop_at_profit_target
            msg = (f"🎯 Daily goal reached: ${st.total:,.0f} (goal ${target:,.0f})."
                   + (" No new trades today — open positions keep their exits." if stop else ""))
            self.ctx.bus.publish("system", msg)
            asyncio.create_task(self.ctx.notify("Daily goal reached", msg, "high"))

    def _throttled_info(self, message: str, key: str, minutes: int = 15) -> None:
        now = self.ctx.clock.now()
        last = self._info_throttle.get(key)
        if last and (now - last).total_seconds() < minutes * 60:
            return
        self._info_throttle[key] = now
        self.ctx.bus.publish("risk", message)

    # ---- dashboard state ---------------------------------------------------------------------------------------
    def status(self) -> dict:
        ctx = self.ctx
        acct = self.account
        open_pos = self.positions.open_positions()
        stats = self.risk.day_stats(self.positions.positions_today(), open_pos)
        remaining = self.risk.day_trades_remaining(acct, open_pos) if acct else None
        now = ctx.clock.now()
        return {
            "now": now.isoformat(),
            "market_open": ctx.clock.is_market_open(),
            "mode": ctx.settings.app.mode,
            "trade_mode": ctx.settings.execution.trade_mode,
            "effective_trade_mode": self.execution.effective_mode,
            "live_auto_blocked": ctx.settings.execution.trade_mode == "auto" and self.execution.effective_mode != "auto",
            "data_source": ctx.data.name,
            "broker": ctx.broker.name,
            "broker_live": ctx.broker.is_live,
            "sim_speed": ctx.clock.speed,
            "account": acct.model_dump() if acct else None,
            "pdt": {
                "applies": self.risk.pdt_applies(acct) if acct else None,
                "used": self.risk.day_trades_used(acct) if acct else None,
                "max": ctx.settings.risk.pdt_max_day_trades,
                "remaining_for_new_entries": remaining,
            },
            "today": {
                "realized": round(stats.realized, 2), "unrealized": round(stats.unrealized, 2),
                "total": round(stats.total, 2), "entries": stats.entries, "wins": stats.wins,
                "losses": stats.losses, "consecutive_losses": stats.consecutive_losses,
                "daily_loss_limit": round(self.risk.daily_loss_limit(acct), 2) if acct else None,
                "profit_target": ctx.settings.risk.daily_profit_target or None,
                "goal_reached": self._goal_notified == now.date(),
            },
            "kill_switch": ctx.settings.risk.kill_switch,
            "account_type": ctx.settings.risk.account_type,
            "available_funds": round(self.risk.available_funds(acct, self.positions.positions_today()), 2) if acct else None,
            "ai_enabled": self.ai.available,
            "notifications": ctx.notifier.enabled,
            "hot_list": [self.scanner.features[s].summary() if s in self.scanner.features else {"symbol": s}
                         for s in self.scanner.hot_list],
            "uoa_hits": self.uoa.last_hits[:15],
            "last_error": self.last_error,
        }
