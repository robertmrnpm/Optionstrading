"""Position manager agent: watches every open position and runs the exit plan.

Exit rules, checked every loop (first match wins):
  1. Flatten time (default 15:50 ET) — this is a day-trading system, nothing is held overnight.
  2. Premium stop (e.g. -30%), raised to breakeven after target 1 and by the trailing stop.
  3. Underlying invalidation (thesis level from the signal).
  4. Target 2 (close all) / Target 1 (scale out part).
  5. Time stop (trade isn't working after N minutes).
Exits are marketable LIMIT orders at the bid (Webull doesn't accept market orders on options),
stepping down a tick at a time if not filled.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from ..models import ExitFill, Order, Position, Proposal
from .context import Context
from .contract_selector import round_to_tick, tick_size

log = logging.getLogger(__name__)


class PositionManager:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.positions: dict[str, Position] = {}
        self._exit_tasks: dict[str, asyncio.Task] = {}
        for p in ctx.db.list("positions", Position, status=["open", "closing"]):
            p.status = "open"
            self.positions[p.id] = p

    # ---- queries -------------------------------------------------------------------------------------
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.status in ("open", "closing")]

    def positions_today(self) -> list[Position]:
        today = self.ctx.clock.today()
        closed = self.ctx.db.list("positions", Position, day=today)
        merged = {p.id: p for p in closed}
        merged.update({p.id: p for p in self.open_positions()})
        return list(merged.values())

    def _save(self, p: Position) -> None:
        self.ctx.db.put("positions", p, p.entry_time, p.status)

    # ---- opening ------------------------------------------------------------------------------------
    def open_from_fill(self, proposal: Proposal, order: Order) -> Position:
        px = order.avg_fill_price or order.limit_price
        risk = self._risk_manager_exit_plan(proposal, px, order.filled_qty)
        pos = Position(
            proposal_id=proposal.id, strategy=proposal.signal.strategy, underlying=proposal.contract.underlying,
            contract=proposal.contract, qty=order.filled_qty, initial_qty=order.filled_qty, entry_price=px,
            entry_time=self.ctx.clock.now(), exit_plan=risk, high_water=px, last_price=px, stop_price=risk.stop_price,
        )
        self.positions[pos.id] = pos
        self._save(pos)
        self.ctx.bus.publish("position_opened", f"Opened {pos.qty}x {pos.contract.label()} @ ${px:.2f}",
                             {"position": pos.model_dump(mode="json")})
        return pos

    def _risk_manager_exit_plan(self, proposal: Proposal, fill_px: float, qty: int):
        # Re-anchor the exit plan on the actual fill price (keeps the same percentages).
        from .risk import RiskManager
        rm = RiskManager(self.ctx.settings, self.ctx.db, self.ctx.clock)
        return rm.exit_plan(proposal.signal, proposal.contract, fill_px, qty)

    # ---- monitoring --------------------------------------------------------------------------------
    async def tick(self) -> None:
        open_pos = [p for p in self.open_positions() if p.status == "open"]
        if not open_pos:
            return
        try:
            quotes = await self.ctx.data.option_quotes([p.contract for p in open_pos])
            unders = await self.ctx.data.quotes(list({p.underlying for p in open_pos}))
        except Exception as e:
            log.warning("position quote refresh failed: %s", e)
            return
        now = self.ctx.clock.now()
        for p in open_pos:
            q = quotes.get(p.contract.symbol)
            if not q or q.mid <= 0:
                continue
            p.last_price = q.mid
            p.contract.bid, p.contract.ask = q.bid, q.ask
            p.high_water = max(p.high_water, q.mid)
            reason, qty = self._exit_decision(p, now, unders.get(p.underlying).last if p.underlying in unders else None)
            self._save(p)
            if reason:
                self.start_exit(p, qty, reason)

    def _exit_decision(self, p: Position, now: datetime, underlying_px: float | None) -> tuple[str | None, int]:
        return decide_exit(p, now, underlying_px, self.ctx.clock.at(p.exit_plan.flatten_at))

    # ---- exiting --------------------------------------------------------------------------------------
    def start_exit(self, p: Position, qty: int, reason: str) -> bool:
        if p.status != "open" or p.id in self._exit_tasks:
            return False
        p.status = "closing"
        self._save(p)
        self._exit_tasks[p.id] = asyncio.create_task(self._run_exit(p, min(qty, p.qty), reason))
        return True

    async def close(self, position_id: str, reason: str = "manual close") -> bool:
        p = self.positions.get(position_id)
        if not p:
            return False
        return self.start_exit(p, p.qty, reason)

    async def flatten_all(self, reason: str = "flatten all") -> int:
        n = 0
        for p in self.open_positions():
            if self.start_exit(p, p.qty, reason):
                n += 1
        return n

    async def _run_exit(self, p: Position, qty: int, reason: str) -> None:
        try:
            await self._exit_order_loop(p, qty, reason)
        except Exception as e:
            log.exception("exit failed for %s", p.id)
            self.ctx.bus.publish("error", f"Exit failed for {p.contract.label()}: {e}")
        finally:
            self._exit_tasks.pop(p.id, None)
            if p.qty > 0 and p.status == "closing":
                p.status = "open"  # retry on the next tick
                self._save(p)

    async def _exit_order_loop(self, p: Position, qty: int, reason: str) -> None:
        ctx = self.ctx
        urgent = not reason.startswith("target")
        q = (await ctx.data.option_quotes([p.contract])).get(p.contract.symbol)
        bid = q.bid if q else p.contract.bid
        mid = q.mid if q else p.last_price
        tick = tick_size(p.underlying, mid)
        price = round_to_tick(bid if urgent else mid, tick, "down" if urgent else "nearest")
        price = max(price, 0.01)
        remaining = qty
        for step in range(6):
            now = ctx.clock.now()
            order = Order(contract=p.contract, side="sell", qty=remaining, limit_price=price, purpose="exit",
                          reason=reason, created=now, updated=now, position_id=p.id, proposal_id=p.proposal_id)
            order = await ctx.broker.place_order(order)
            ctx.db.put("orders", order, now, order.status)
            if order.status in ("rejected", "failed"):
                ctx.bus.publish("error", f"Exit order rejected for {p.contract.label()}: {order.message}")
                return
            for _ in range(5 if urgent else 10):
                order = await ctx.broker.refresh_order(order)
                if order.status == "filled":
                    break
                await ctx.clock.sleep(1)
            if order.status != "filled":
                order = await ctx.broker.cancel_order(order)
            ctx.db.put("orders", order, now, order.status)
            if order.filled_qty:
                self._book_exit(p, order.filled_qty, order.avg_fill_price, reason)
                remaining -= order.filled_qty
            if remaining <= 0:
                return
            # step down: toward/through the bid
            q = (await ctx.data.option_quotes([p.contract])).get(p.contract.symbol)
            bid = q.bid if q else price
            price = max(0.01, round_to_tick(min(price - tick, bid) if step >= 1 else bid, tick, "down"))
        ctx.bus.publish("error", f"Could not fully exit {p.contract.label()} ({remaining} left) — will retry",
                        {"position_id": p.id})
        await ctx.notify("⚠️ Exit not filled", f"{p.contract.label()}: {remaining} contracts still open ({reason})",
                         "high")

    def _book_exit(self, p: Position, qty: int, price: float, reason: str) -> None:
        now = self.ctx.clock.now()
        if not p.exits and p.entry_time.date() == now.date() and not p.external:
            self.ctx.db.record_day_trade(now.date(), p.underlying, p.id)
        pnl = (price - p.entry_price) * qty * 100
        p.exits.append(ExitFill(ts=now, qty=qty, price=price, reason=reason, pnl=round(pnl, 2)))
        p.realized_pnl = round(p.realized_pnl + pnl, 2)
        p.qty -= qty
        if reason.startswith("target 1"):
            p.target1_done = True
            if self.ctx.settings.exits.breakeven_after_target1:
                p.stop_price = max(p.stop_price, p.entry_price)
        if p.qty <= 0:
            p.status = "closed"
            p.closed_time = now
        self._save(p)
        emoji = "✅" if pnl >= 0 else "🔻"
        msg = f"{emoji} Sold {qty}x {p.contract.label()} @ ${price:.2f} ({reason}) P&L ${pnl:+,.0f}"
        self.ctx.bus.publish("position_exit", msg, {"position": p.model_dump(mode="json")})
        if self.ctx.settings.notify.on_exit:
            asyncio.create_task(self.ctx.notify("Exit" if pnl >= 0 else "Exit (loss)", msg))


def decide_exit(p: Position, now: datetime, underlying_px: float | None, flatten_dt: datetime) -> tuple[str | None, int]:
    """Pure exit-rule evaluation shared by live trading and the backtester. May raise p.stop_price (trailing)."""
    x = p.exit_plan
    if p.high_water >= x.trail_activation_price:
        trail = p.high_water * (1 - x.trail_pct)
        if trail > p.stop_price:
            p.stop_price = round(trail, 2)
    if now >= flatten_dt:
        return "end-of-day flatten", p.qty
    if p.contract.expiry < now.date():
        return "expired", p.qty
    if p.last_price <= p.stop_price:
        label = "trailing stop" if p.stop_price > x.stop_price else "stop loss"
        if p.target1_done and abs(p.stop_price - p.entry_price) < 0.011:
            label = "breakeven stop"
        return label, p.qty
    if underlying_px is not None and x.stop_underlying is not None:
        if (p.contract.right == "C" and underlying_px < x.stop_underlying) or \
           (p.contract.right == "P" and underlying_px > x.stop_underlying):
            return f"thesis invalidated (underlying {underlying_px:.2f} crossed {x.stop_underlying:.2f})", p.qty
    if p.last_price >= x.target2_price:
        return "target 2", p.qty
    if not p.target1_done and x.target1_qty > 0 and p.last_price >= x.target1_price and p.qty > x.target1_qty:
        return "target 1 (scale out)", x.target1_qty
    held_min = (now - p.entry_time).total_seconds() / 60
    if held_min >= x.time_stop_minutes and p.pnl_pct < 10:
        return f"time stop ({x.time_stop_minutes} min, not working)", p.qty
    return None, 0
