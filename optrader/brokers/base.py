"""Broker interface. Orders are single-leg option LIMIT orders (Webull doesn't allow market orders on options)."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import AccountSnapshot, Order


class Broker(ABC):
    name = "base"
    is_live = False

    @abstractmethod
    async def account(self) -> AccountSnapshot: ...

    @abstractmethod
    async def place_order(self, order: Order) -> Order:
        """Submit a limit order. Returns the order with status/broker ids updated."""

    @abstractmethod
    async def refresh_order(self, order: Order) -> Order:
        """Poll the broker for fills/status."""

    @abstractmethod
    async def cancel_order(self, order: Order) -> Order: ...

    async def replace_order(self, order: Order, new_limit: float) -> Order:
        """Change the limit price. Default: cancel and resubmit (keeps any partial fill)."""
        order = await self.cancel_order(order)
        if order.status == "filled":
            return order
        remaining = order.qty - order.filled_qty
        replacement = order.model_copy(update={"limit_price": new_limit, "qty": remaining, "status": "new",
                                               "filled_qty": 0, "avg_fill_price": 0.0})
        replacement.client_order_id = __import__("uuid").uuid4().hex
        return await self.place_order(replacement)

    async def positions(self) -> list[dict]:
        """Raw broker positions (for reconciliation). Optional."""
        return []

    async def close(self) -> None:
        return None
