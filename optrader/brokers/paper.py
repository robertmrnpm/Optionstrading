"""Paper broker: realistic simulated fills against live (or simulated) option quotes.

Fill model (deliberately conservative so paper results don't flatter you):
  * BUY limit fills only if limit >= ask - 25% of the spread; fill price = min(limit, ask).
  * SELL limit fills only if limit <= bid + 25% of the spread; fill price = max(limit, bid).
  * Fees of ``fee_per_contract`` per contract per side (regulatory/exchange fees).
State (cash, positions) persists in ``paper_account.json`` so restarts keep your P&L history.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ..clock import Clock, next_trading_day
from ..data.base import MarketDataProvider
from ..models import AccountSnapshot, Order
from .base import Broker


class PaperBroker(Broker):
    name = "paper"
    is_live = False

    def __init__(self, data: MarketDataProvider, clock: Clock, starting_equity: float,
                 state_path: Path | None = None, fee_per_contract: float = 0.65, cash_account: bool = False):
        self.data = data
        self.clock = clock
        self.fee = fee_per_contract
        self.cash_account = cash_account
        self.state_path = state_path
        self.cash = starting_equity
        self.unsettled: list[tuple[str, float]] = []   # (settle_date iso, amount)
        self.holdings: dict[str, dict] = {}            # option symbol -> {qty, avg, contract}
        self.open_orders: dict[str, Order] = {}
        self._load()

    # ---- persistence ------------------------------------------------------------------------
    def _load(self) -> None:
        if self.state_path and self.state_path.exists():
            s = json.loads(self.state_path.read_text())
            self.cash = s["cash"]
            self.unsettled = [tuple(x) for x in s.get("unsettled", [])]
            self.holdings = s.get("holdings", {})

    def _save(self) -> None:
        if self.state_path:
            self.state_path.write_text(json.dumps(
                {"cash": self.cash, "unsettled": self.unsettled, "holdings": self.holdings}, indent=2, default=str))

    def reset(self, equity: float) -> None:
        self.cash, self.unsettled, self.holdings, self.open_orders = equity, [], {}, {}
        self._save()

    # ---- account --------------------------------------------------------------------------------
    def _settle(self) -> None:
        today = self.clock.today().isoformat()
        self.unsettled = [(d, a) for d, a in self.unsettled if d > today]

    async def account(self) -> AccountSnapshot:
        self._settle()
        mark_value = 0.0
        if self.holdings:
            from ..models import OptionContract
            contracts = [OptionContract.model_validate(h["contract"]) for h in self.holdings.values()]
            quotes = await self.data.option_quotes(contracts)
            for sym, h in self.holdings.items():
                q = quotes.get(sym)
                px = q.mid if q else h["avg"]
                mark_value += px * h["qty"] * 100
        unsettled = sum(a for _, a in self.unsettled)
        equity = self.cash + mark_value
        settled = self.cash - unsettled
        return AccountSnapshot(equity=round(equity, 2), cash=round(self.cash, 2),
                               buying_power=round(settled if self.cash_account else self.cash, 2),
                               settled_cash=round(settled, 2), source="paper")

    # ---- orders -----------------------------------------------------------------------------------
    async def place_order(self, order: Order) -> Order:
        now = self.clock.now()
        if order.side == "buy":
            cost = order.limit_price * order.qty * 100 + self.fee * order.qty
            acct = await self.account()
            if cost > acct.buying_power + 1e-6:
                order.status, order.message = "rejected", f"insufficient buying power (${acct.buying_power:,.2f})"
                order.updated = now
                return order
        else:
            held = self.holdings.get(order.contract.symbol, {}).get("qty", 0)
            if held < order.qty:
                order.status, order.message = "rejected", f"cannot sell {order.qty}, holding {held}"
                order.updated = now
                return order
        order.status = "submitted"
        order.broker_order_id = f"paper-{order.client_order_id[:8]}"
        order.updated = now
        self.open_orders[order.client_order_id] = order
        return await self.refresh_order(order)

    async def refresh_order(self, order: Order) -> Order:
        if order.status not in ("submitted", "partial"):
            return order
        q = (await self.data.option_quotes([order.contract])).get(order.contract.symbol)
        if not q or q.ask <= 0:
            return order
        spread = max(q.ask - q.bid, 0.01)
        remaining = order.qty - order.filled_qty
        fill_px = None
        if order.side == "buy" and order.limit_price >= q.ask - 0.25 * spread:
            fill_px = min(order.limit_price, q.ask)
        elif order.side == "sell" and order.limit_price <= q.bid + 0.25 * spread and q.bid > 0:
            fill_px = max(order.limit_price, q.bid)
        if fill_px is None:
            return order
        self._apply_fill(order, remaining, round(fill_px, 2))
        return order

    def _apply_fill(self, order: Order, qty: int, px: float) -> None:
        sym = order.contract.symbol
        fee = self.fee * qty
        if order.side == "buy":
            self.cash -= px * qty * 100 + fee
            h = self.holdings.get(sym, {"qty": 0, "avg": 0.0, "contract": order.contract.model_dump(mode="json")})
            h["avg"] = (h["avg"] * h["qty"] + px * qty) / (h["qty"] + qty)
            h["qty"] += qty
            self.holdings[sym] = h
        else:
            proceeds = px * qty * 100 - fee
            self.cash += proceeds
            if self.cash_account:  # options settle T+1
                self.unsettled.append((next_trading_day(self.clock.today()).isoformat(), proceeds))
            h = self.holdings[sym]
            h["qty"] -= qty
            if h["qty"] <= 0:
                del self.holdings[sym]
        total = order.avg_fill_price * order.filled_qty + px * qty
        order.filled_qty += qty
        order.avg_fill_price = round(total / order.filled_qty, 4)
        order.status = "filled" if order.filled_qty >= order.qty else "partial"
        order.updated = self.clock.now()
        if order.status == "filled":
            self.open_orders.pop(order.client_order_id, None)
        self._save()

    async def cancel_order(self, order: Order) -> Order:
        order = await self.refresh_order(order)  # a last-moment fill wins, like a real exchange
        if order.status in ("submitted", "partial"):
            order.status = "cancelled" if order.filled_qty == 0 else "filled"
            if order.filled_qty:
                order.qty = order.filled_qty
            order.updated = self.clock.now()
            self.open_orders.pop(order.client_order_id, None)
        return order

    async def positions(self) -> list[dict]:
        return [{"symbol": s, "qty": h["qty"], "avg_price": h["avg"]} for s, h in self.holdings.items()]
