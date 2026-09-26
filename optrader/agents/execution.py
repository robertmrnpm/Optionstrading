"""Execution agent: turns signals into proposals and proposals into orders.

Modes (``execution.trade_mode``):
  alerts   -> proposals are published + pushed to your phone, never executed.
  approval -> proposals wait for you to click Approve in the dashboard (expire after a TTL).
  auto     -> proposals execute on their own (optionally only if the AI analyst agrees).
             Live auto-trading additionally requires ALLOW_LIVE_AUTO_TRADING=true.

Entry orders are LIMIT orders starting at the mid price and stepping toward the ask a few
times before giving up — you never chase a runaway contract.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from ..models import AccountSnapshot, OptionContract, Order, Proposal, Signal
from .ai_analyst import AIAnalyst
from .context import Context
from .contract_selector import round_to_tick, tick_size
from .features import SymbolFeatures
from .position_manager import PositionManager
from .risk import RiskManager

log = logging.getLogger(__name__)


class ExecutionAgent:
    def __init__(self, ctx: Context, risk: RiskManager, positions: PositionManager, ai: AIAnalyst):
        self.ctx = ctx
        self.risk = risk
        self.positions = positions
        self.ai = ai
        self.proposals: dict[str, Proposal] = {}
        self._tasks: set[asyncio.Task] = set()
        self.features: dict[str, SymbolFeatures] = {}   # latest, set by the orchestrator
        self.market_context: dict = {}
        self._account: AccountSnapshot | None = None
        # Entries being worked right now. Risk check + reservation happen under a lock so two
        # concurrent entries can't both squeeze through the last PDT day trade / position slot.
        self.inflight: dict[str, str] = {}   # proposal id -> symbol
        self._entry_lock = asyncio.Lock()

    def set_account(self, account: AccountSnapshot) -> None:
        self._account = account

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _save(self, p: Proposal) -> None:
        self.ctx.db.put("proposals", p, p.created, p.status)

    @property
    def effective_mode(self) -> str:
        s = self.ctx.settings
        mode = s.execution.trade_mode
        if mode == "auto" and self.ctx.broker.is_live and not s.allow_live_auto:
            return "approval"
        return mode

    def pending(self) -> list[Proposal]:
        return [p for p in self.proposals.values() if p.status == "pending"]

    # ---- proposal creation ---------------------------------------------------------------------------
    async def propose(self, sig: Signal, contract: OptionContract, selection_note: str) -> Proposal:
        ctx = self.ctx
        now = ctx.clock.now()
        account = self._account or await ctx.broker.account()
        tick = tick_size(contract.underlying, contract.mid)
        limit = round_to_tick(contract.mid, tick, "up")
        decision = self.risk.check_entry(sig, contract, limit, account, self.positions.positions_today(),
                                         self.positions.open_positions())
        qty = max(decision.qty, 1)
        plan = self.risk.exit_plan(sig, contract, limit, qty)
        mode = self.effective_mode
        p = Proposal(
            # TTL is in real seconds (you need real time to click Approve, even in fast sim mode)
            created=now, expires=now + timedelta(seconds=ctx.settings.execution.proposal_ttl_seconds * ctx.clock.speed),
            signal=sig, contract=contract, qty=qty, limit_price=limit, est_cost=round(limit * qty * 100, 2),
            risk_dollars=decision.risk_dollars, exit_plan=plan,
            risk_notes=[selection_note] + decision.notes,
        )
        if not decision.ok:
            p.status, p.status_reason = "blocked", "; ".join(decision.blocks)
        elif mode == "alerts":
            p.status, p.status_reason = "alert", "alerts-only mode"
        self.proposals[p.id] = p
        self._save(p)
        verb = {"blocked": "BLOCKED", "alert": "ALERT"}.get(p.status, "PROPOSED")
        msg = (f"{verb}: BUY {p.qty}x {contract.label()} @ ${limit:.2f} — {sig.strategy}/{sig.meta.get('setup')} "
               f"score {sig.score:.0f}")
        if p.status == "blocked":
            msg += f" ({p.status_reason})"
        ctx.bus.publish("proposal", msg, {"proposal": p.model_dump(mode="json")})

        if p.status in ("pending", "alert"):
            n = ctx.settings.notify
            if n.on_proposal:
                body = (f"{sig.symbol} {sig.direction} • {', '.join(sig.reasons[:2])}\n"
                        f"Cost ~${p.est_cost:,.0f}, max planned loss ${p.risk_dollars:,.0f}, "
                        f"stop ${plan.stop_price:.2f} / T1 ${plan.target1_price:.2f}")
                title = f"{'🔔 Approve?' if mode == 'approval' else '📈'} {contract.label()}"
                self._spawn(ctx.notify(title, body, "high" if mode == "approval" else "default"))
            if self.ai.available:
                self._spawn(self._review_then_maybe_execute(p))
            elif mode == "auto" and p.status == "pending":
                self._spawn(self._execute(p))
        elif p.status == "blocked" and ctx.settings.notify.on_risk_block and sig.score >= 75:
            self._spawn(ctx.notify("⛔ Trade blocked by risk manager", f"{contract.label()}: {p.status_reason}"))
        return p

    async def _review_then_maybe_execute(self, p: Proposal) -> None:
        review = await self.ai.review(p, self.features.get(p.signal.symbol), self.market_context)
        p.ai_review = review
        self._save(p)
        self.ctx.bus.publish("ai_review", f"AI analyst on {p.contract.label()}: {review.verdict.upper()} "
                                          f"({review.confidence}%) — {review.summary}",
                             {"proposal": p.model_dump(mode="json")})
        if self.effective_mode != "auto" or p.status != "pending":
            return
        cfg = self.ctx.settings.ai
        if cfg.required_for_auto and (review.verdict != "take" or review.confidence < cfg.min_confidence):
            p.status, p.status_reason = "rejected", f"AI analyst: {review.verdict} ({review.confidence}%)"
            self._save(p)
            self.ctx.bus.publish("proposal", f"Auto-skipped {p.contract.label()}: {p.status_reason}",
                                 {"proposal": p.model_dump(mode="json")})
            return
        await self._execute(p)

    # ---- user actions ------------------------------------------------------------------------------------
    async def approve(self, proposal_id: str, qty: int | None = None) -> Proposal:
        p = self.proposals.get(proposal_id)
        if not p:
            raise KeyError("proposal not found")
        if p.status != "pending":
            raise ValueError(f"proposal is {p.status}")
        if self.ctx.clock.now() > p.expires:
            p.status, p.status_reason = "expired", "approval window passed"
            self._save(p)
            raise ValueError("proposal expired — prices have moved; wait for a fresh signal")
        if qty is not None:
            if qty < 1 or qty > p.qty:
                raise ValueError(f"quantity must be between 1 and {p.qty} (risk-sized maximum)")
            p.qty = qty
        p.status = "approved"
        self._save(p)
        self._spawn(self._execute(p))
        return p

    def reject(self, proposal_id: str, reason: str = "rejected by user") -> Proposal:
        p = self.proposals.get(proposal_id)
        if not p:
            raise KeyError("proposal not found")
        if p.status == "pending":
            p.status, p.status_reason = "rejected", reason
            self._save(p)
            self.ctx.bus.publish("proposal", f"Rejected {p.contract.label()}", {"proposal": p.model_dump(mode="json")})
        return p

    def expire_stale(self) -> None:
        now = self.ctx.clock.now()
        for p in self.pending():
            if now > p.expires:
                p.status, p.status_reason = "expired", "not approved in time"
                self._save(p)
                self.ctx.bus.publish("proposal", f"Expired {p.contract.label()}", {"proposal": p.model_dump(mode="json")})

    # ---- order execution ------------------------------------------------------------------------------------
    async def _execute(self, p: Proposal) -> None:
        ctx = self.ctx
        try:
            q = (await ctx.data.option_quotes([p.contract])).get(p.contract.symbol)
            if not q or q.ask <= 0:
                raise RuntimeError("no live quote for contract")
            drift = abs(q.mid / p.limit_price - 1)
            if drift > ctx.settings.execution.max_price_drift_pct:
                p.status, p.status_reason = "cancelled", f"price moved {drift*100:.0f}% since proposal"
                self._save(p)
                ctx.bus.publish("proposal", f"Cancelled {p.contract.label()}: {p.status_reason}",
                                {"proposal": p.model_dump(mode="json")})
                return
            tick = tick_size(p.contract.underlying, q.mid)
            start = round_to_tick(q.mid, tick, "up")
            async with self._entry_lock:
                account = await ctx.broker.account()
                self._account = account
                decision = self.risk.check_entry(p.signal, q, start, account, self.positions.positions_today(),
                                                 self.positions.open_positions(), inflight=len(self.inflight))
                if any(sym == p.signal.symbol for sym in self.inflight.values()):
                    decision.ok = False
                    decision.blocks.append(f"another {p.signal.symbol} entry is already working")
                if not decision.ok:
                    p.status, p.status_reason = "blocked", "; ".join(decision.blocks)
                    self._save(p)
                    ctx.bus.publish("proposal", f"Blocked at execution: {p.contract.label()} ({p.status_reason})",
                                    {"proposal": p.model_dump(mode="json")})
                    return
                self.inflight[p.id] = p.signal.symbol
            qty = min(p.qty, decision.qty)
            max_price = round_to_tick(q.mid + (q.ask - q.mid) * ctx.settings.execution.max_chase_pct_of_spread,
                                      tick, "up")
            order = await self._work_entry(p, q, qty, start, max_price, tick)
            if order.filled_qty > 0:
                p.status = "filled"
                p.limit_price = order.avg_fill_price
                pos = self.positions.open_from_fill(p, order)  # now counted as an open position...
                self.inflight.pop(p.id, None)                   # ...so release the reservation
                p.position_id = pos.id
                self._save(p)
                msg = f"🟢 Bought {order.filled_qty}x {p.contract.label()} @ ${order.avg_fill_price:.2f}"
                ctx.bus.publish("fill", msg, {"proposal": p.model_dump(mode="json")})
                if ctx.settings.notify.on_fill:
                    await ctx.notify("Filled", msg + f"\nStop ${pos.stop_price:.2f} • T1 ${pos.exit_plan.target1_price:.2f}")
            else:
                self.inflight.pop(p.id, None)
                p.status, p.status_reason = "cancelled", order.message or "entry not filled within limits"
                self._save(p)
                ctx.bus.publish("proposal", f"Entry not filled: {p.contract.label()} ({p.status_reason})",
                                {"proposal": p.model_dump(mode="json")})
        except Exception as e:
            self.inflight.pop(p.id, None)
            log.exception("execution failed")
            p.status, p.status_reason = "failed", str(e)[:300]
            self._save(p)
            ctx.bus.publish("error", f"Execution failed for {p.contract.label()}: {e}",
                            {"proposal": p.model_dump(mode="json")})

    async def _work_entry(self, p: Proposal, q: OptionContract, qty: int, start: float, max_price: float,
                          tick: float) -> Order:
        ctx = self.ctx
        ex = ctx.settings.execution
        steps = max(ex.entry_chase_steps, 1)
        per_step = max(ex.entry_timeout_seconds / steps, 1)
        price = start
        now = ctx.clock.now()
        order = Order(contract=p.contract, side="buy", qty=qty, limit_price=price, purpose="entry",
                      reason=f"{p.signal.strategy}/{p.signal.meta.get('setup')}", created=now, updated=now,
                      proposal_id=p.id)
        p.status = "submitted"
        self._save(p)
        order = await ctx.broker.place_order(order)
        ctx.db.put("orders", order, now, order.status)
        ctx.bus.publish("order", f"Entry order: BUY {qty}x {p.contract.label()} @ ${price:.2f} ({order.status})",
                        {"order": order.model_dump(mode="json")})
        if order.status in ("rejected", "failed"):
            return order
        for step in range(steps):
            waited = 0.0
            while waited < per_step:
                order = await ctx.broker.refresh_order(order)
                if order.status == "filled":
                    ctx.db.put("orders", order, now, order.status)
                    return order
                await ctx.clock.sleep(1)
                waited += 1
            if step == steps - 1:
                break
            new_price = round_to_tick(min(price + tick * max(1, round((max_price - start) / tick / steps)), max_price),
                                      tick, "up")
            if new_price <= price:
                continue
            price = new_price
            order = await ctx.broker.replace_order(order, price)
            ctx.db.put("orders", order, now, order.status)
            if order.status == "filled":
                return order
        order = await ctx.broker.cancel_order(order)
        if order.status != "filled":
            order.message = order.message or f"not filled up to ${price:.2f} (max ${max_price:.2f})"
        ctx.db.put("orders", order, now, order.status)
        return order
