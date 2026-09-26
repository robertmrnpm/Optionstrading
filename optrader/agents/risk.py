"""Risk manager agent: the gatekeeper every trade must pass, in every mode.

Enforces: kill switch, trading windows, daily loss limit, loss streak, trade count,
concurrent positions, PDT (3 day trades / 5 business days for <$25k margin accounts),
settled cash (cash accounts), and position sizing from a fixed % risk per trade.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..clock import Clock, previous_trading_days
from ..config import Settings
from ..db import Database
from ..models import AccountSnapshot, ExitPlan, OptionContract, Position, Signal
from .contract_selector import round_to_tick, tick_size


@dataclass
class RiskDecision:
    ok: bool
    qty: int = 0
    risk_dollars: float = 0.0
    blocks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class DayStats:
    realized: float = 0.0
    unrealized: float = 0.0
    entries: int = 0
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    open_positions: int = 0

    @property
    def total(self) -> float:
        return self.realized + self.unrealized


class RiskManager:
    def __init__(self, settings: Settings, db: Database, clock: Clock):
        self.s = settings
        self.db = db
        self.clock = clock

    # ---- state ----------------------------------------------------------------------------------
    def day_stats(self, positions_today: list[Position], open_positions: list[Position]) -> DayStats:
        st = DayStats()
        today = self.clock.today()
        closed = sorted([p for p in positions_today if p.status == "closed" and p.closed_time],
                        key=lambda p: p.closed_time)
        for p in closed:
            st.realized += p.realized_pnl
            if p.realized_pnl > 0:
                st.wins += 1
                st.consecutive_losses = 0
            else:
                st.losses += 1
                st.consecutive_losses += 1
        for p in open_positions:
            st.realized += p.realized_pnl  # partial exits already booked
            st.unrealized += p.unrealized_pnl
        st.entries = len([p for p in positions_today if not p.external and p.entry_time.date() == today])
        st.open_positions = len(open_positions)
        return st

    def pdt_applies(self, account: AccountSnapshot) -> bool:
        r = self.s.risk
        return r.pdt_enforce and r.account_type == "margin" and account.equity < r.pdt_threshold_equity

    def day_trades_used(self, account: AccountSnapshot) -> int:
        if account.day_trades_used is not None:
            return account.day_trades_used
        days = previous_trading_days(self.clock.today(), 5)
        return self.db.day_trades_between(days[0], days[-1])

    def day_trades_remaining(self, account: AccountSnapshot, open_positions: list[Position]) -> int | None:
        if not self.pdt_applies(account):
            return None
        today = self.clock.today()
        # positions opened today will almost certainly be closed today (we flatten EOD)
        pending = len([p for p in open_positions if p.entry_time.date() == today and not p.external])
        return self.s.risk.pdt_max_day_trades - self.day_trades_used(account) - pending - self.s.risk.pdt_reserve

    def available_funds(self, account: AccountSnapshot, positions_today: list[Position] | None = None) -> float:
        """Money that can be spent on a new entry without breaking account rules.

        Cash accounts may only buy with SETTLED funds (options settle T+1). Buying with unsettled
        sale proceeds and selling before they settle is a good-faith violation (3 in 12 months =
        90-day restriction). If the broker reports settled cash we use it; otherwise we subtract
        today's sale proceeds from buying power, since they won't settle until tomorrow.
        """
        bp = max(account.buying_power, 0.0)
        if self.s.risk.account_type != "cash":
            return bp
        if account.settled_cash is not None:
            return max(min(bp, account.settled_cash), 0.0)
        today = self.clock.today()
        unsettled = sum(x.price * x.qty * 100 for p in (positions_today or []) for x in p.exits
                        if x.ts.date() == today)
        return max(bp - unsettled, 0.0)

    def daily_loss_limit(self, account: AccountSnapshot) -> float:
        r = self.s.risk
        return min(r.max_daily_loss, account.equity * r.max_daily_loss_pct / 100)

    # ---- entry checks -------------------------------------------------------------------------------
    def global_blocks(self, zero_dte: bool, account: AccountSnapshot, positions_today: list[Position],
                      open_positions: list[Position], inflight: int = 0) -> list[str]:
        """Reasons no new trade of this kind can be opened right now, regardless of symbol/contract.

        ``inflight`` = entry orders currently being worked (they count as positions/day trades already).
        """
        r = self.s.risk
        blocks: list[str] = []
        now = self.clock.now()
        if r.kill_switch:
            blocks.append("kill switch is ON")
        if not self.clock.is_market_open():
            blocks.append("market is closed")
        elif self.clock.minutes_since_open() < r.no_entry_first_minutes:
            blocks.append(f"no entries in the first {r.no_entry_first_minutes} minutes")
        last_entry = self.clock.at(r.zero_dte_last_entry_time if zero_dte else r.last_entry_time)
        if now >= last_entry:
            blocks.append(f"past last entry time ({last_entry:%H:%M} ET)")
        st = self.day_stats(positions_today, open_positions)
        limit = self.daily_loss_limit(account)
        if st.total <= -limit:
            blocks.append(f"daily loss limit hit (${st.total:,.0f} / -${limit:,.0f})")
        if r.daily_profit_target > 0 and r.stop_at_profit_target and st.total >= r.daily_profit_target:
            blocks.append(f"daily profit goal reached (${st.total:,.0f} ≥ ${r.daily_profit_target:,.0f}) — "
                          "locking in the day")
        if st.consecutive_losses >= r.max_consecutive_losses:
            blocks.append(f"{st.consecutive_losses} losses in a row — done for the day")
        if st.entries + inflight >= r.max_trades_per_day:
            blocks.append(f"max trades per day reached ({r.max_trades_per_day})")
        if st.open_positions + inflight >= r.max_open_positions:
            blocks.append(f"max open positions reached ({r.max_open_positions})")
        remaining = self.day_trades_remaining(account, open_positions)
        if remaining is not None:
            remaining -= inflight
        if remaining is not None and remaining <= 0:
            today = self.clock.today()
            pending = len([p for p in open_positions if p.entry_time.date() == today and not p.external])
            detail = f"used {self.day_trades_used(account)}/{r.pdt_max_day_trades}"
            if pending or inflight:
                detail += f", {pending + inflight} open/working position(s) will use more"
            if r.pdt_reserve:
                detail += f", {r.pdt_reserve} held in reserve"
            blocks.append(f"PDT: no day trades left in the rolling 5-day window ({detail})")
        return blocks

    def max_affordable_premium(self, zero_dte: bool, account: AccountSnapshot, headroom: float = 0.85,
                               positions_today: list[Position] | None = None) -> float:
        """Highest option price (per share) that still sizes to >= 1 contract, with headroom for price drift."""
        r, x = self.s.risk, self.s.exits
        stop_pct = x.zero_dte_stop_loss_pct if zero_dte else x.stop_loss_pct
        by_risk = account.equity * r.risk_per_trade_pct / 100 / (stop_pct * 100)
        by_cap = r.max_premium_per_trade / 100
        by_pct = account.equity * r.max_position_pct / 100 / 100
        by_bp = self.available_funds(account, positions_today) / 100
        return round(min(by_risk, by_cap, by_pct, by_bp) * headroom, 2)

    def check_entry(self, sig: Signal, contract: OptionContract, entry_price: float, account: AccountSnapshot,
                    positions_today: list[Position], open_positions: list[Position], inflight: int = 0) -> RiskDecision:
        r, x = self.s.risk, self.s.exits
        d = RiskDecision(ok=True)
        d.blocks.extend(self.global_blocks(sig.zero_dte, account, positions_today, open_positions, inflight))
        if sig.score < r.min_signal_score:
            d.blocks.append(f"signal score {sig.score:.0f} < minimum {r.min_signal_score:.0f}")
        if any(p.underlying == sig.symbol for p in open_positions):
            d.blocks.append(f"already holding a {sig.symbol} position")
        remaining = self.day_trades_remaining(account, open_positions)
        if remaining is not None:
            remaining -= inflight
        if remaining is not None and remaining > 0:
            d.notes.append(f"PDT: {remaining - 1} day trade(s) left after this one is closed" if remaining > 1
                           else "PDT: this uses your LAST available day trade")

        # ---- sizing ----
        stop_pct = x.zero_dte_stop_loss_pct if sig.zero_dte else x.stop_loss_pct
        per_contract_risk = entry_price * stop_pct * 100
        per_contract_cost = entry_price * 100
        risk_budget = account.equity * r.risk_per_trade_pct / 100
        qty = math.floor(risk_budget / per_contract_risk) if per_contract_risk > 0 else 0
        caps = {
            "risk budget": qty,
            "max premium per trade": math.floor(r.max_premium_per_trade / per_contract_cost),
            "max position % of equity": math.floor(account.equity * r.max_position_pct / 100 / per_contract_cost),
            ("settled cash" if r.account_type == "cash" else "buying power"):
                math.floor(self.available_funds(account, positions_today) / (per_contract_cost + 1)),
        }
        qty = min(caps.values())
        if qty < 1:
            binding = min(caps, key=caps.get)
            d.blocks.append(f"position size < 1 contract (limited by {binding}; premium ${per_contract_cost:,.0f})")
        d.qty = max(qty, 0)
        d.risk_dollars = round(d.qty * per_contract_risk, 2)
        if sig.zero_dte:
            d.notes.append("0DTE: premium can go to zero fast; hard time stop applies")
        d.ok = not d.blocks
        return d

    def exit_plan(self, sig: Signal, contract: OptionContract, entry_price: float, qty: int) -> ExitPlan:
        x = self.s.exits
        tick = tick_size(contract.underlying, entry_price)
        stop_pct = x.zero_dte_stop_loss_pct if sig.zero_dte else x.stop_loss_pct
        t1_qty = int(math.floor(qty * x.target1_fraction)) if qty > 1 else 0
        return ExitPlan(
            stop_price=round_to_tick(entry_price * (1 - stop_pct), tick, "down"),
            target1_price=round_to_tick(entry_price * (1 + x.target1_pct), tick, "up"),
            target1_qty=t1_qty,
            target2_price=round_to_tick(entry_price * (1 + x.target2_pct), tick, "up"),
            trail_activation_price=round(entry_price * (1 + x.trail_activation_pct), 2),
            trail_pct=x.trail_pct,
            time_stop_minutes=x.zero_dte_time_stop_minutes if sig.zero_dte else x.time_stop_minutes,
            stop_underlying=sig.stop_underlying,
            flatten_at=self.s.risk.flatten_time,
        )
