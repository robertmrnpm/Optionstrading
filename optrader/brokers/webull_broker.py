"""Live Webull broker via the official OpenAPI (``TradeClient.order_v3``).

Webull constraints for options (per their docs):
  * MARKET and TRAILING_STOP_LOSS order types are not supported -> we use LIMIT only.
  * SELL orders only support time_in_force=DAY.
Stops/targets are therefore managed by the PositionManager agent, which sends marketable
limit orders when an exit triggers. Keep the app running while you have open positions.
"""
from __future__ import annotations

import asyncio
import logging

from ..clock import Clock
from ..models import AccountSnapshot, Order
from ..webull_client import fnum, find_dicts, pick, response_json
from .base import Broker

log = logging.getLogger(__name__)

STATUS_MAP = {
    "SUBMITTED": "submitted", "PENDING": "submitted", "WORKING": "submitted", "NEW": "submitted",
    "PARTIAL_FILLED": "partial", "PARTIAL FILLED": "partial", "PARTIALLY_FILLED": "partial",
    "FILLED": "filled", "CANCELLED": "cancelled", "CANCELED": "cancelled",
    "FAILED": "failed", "REJECTED": "rejected", "EXPIRED": "cancelled",
}


class WebullBroker(Broker):
    name = "webull"
    is_live = True

    def __init__(self, api_client, clock: Clock, account_id: str | None = None):
        from webull.trade.trade_client import TradeClient
        self.tc = TradeClient(api_client)
        self.clock = clock
        self.account_id = account_id

    async def _call(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)

    async def ensure_account(self) -> str:
        if self.account_id:
            return self.account_id
        data = response_json(await self._call(self.tc.account_v2.get_account_list), "account list")
        accounts = find_dicts(data, ["account_id"])
        if not accounts:
            raise RuntimeError("No Webull accounts returned for this App Key")
        self.account_id = str(accounts[0]["account_id"])
        log.info("Using Webull account %s", self.account_id)
        return self.account_id

    async def account(self) -> AccountSnapshot:
        acct = await self.ensure_account()
        data = response_json(await self._call(self.tc.account_v2.get_account_balance, acct), "account balance")
        flat = data if isinstance(data, dict) else {}
        # Some fields are nested per-currency; merge the first USD asset block if present.
        for block in find_dicts(data, ["currency"]):
            if str(block.get("currency")).upper() == "USD":
                flat = {**block, **{k: v for k, v in flat.items() if not isinstance(v, (list, dict))}}
                break
        equity = fnum(pick(flat, "total_asset", "net_liquidation_value", "net_liquidation", "total_net_asset",
                           "account_value"))
        cash = fnum(pick(flat, "cash_balance", "total_cash", "cash", "total_cash_value"))
        bp = fnum(pick(flat, "option_buying_power", "buying_power", "day_buying_power", "cash_buying_power",
                       default=cash))
        settled = pick(flat, "settled_cash", "cash_settled", "settled_funds")
        dt_used = pick(flat, "day_trades_used", "day_trade_count", "used_day_trades")
        return AccountSnapshot(equity=equity or cash, cash=cash, buying_power=bp,
                               settled_cash=fnum(settled) if settled is not None else None,
                               day_trades_used=int(fnum(dt_used)) if dt_used is not None else None,
                               source="webull")

    def _order_payload(self, order: Order) -> dict:
        c = order.contract
        side = "BUY" if order.side == "buy" else "SELL"
        return {
            "client_order_id": order.client_order_id,
            "combo_type": "NORMAL",
            "order_type": "LIMIT",
            "quantity": str(order.qty),
            "limit_price": f"{order.limit_price:.2f}",
            "option_strategy": "SINGLE",
            "side": side,
            "time_in_force": "DAY",
            "entrust_type": "QTY",
            "position_intent": "BUY_TO_OPEN" if order.side == "buy" else "SELL_TO_CLOSE",
            "legs": [{
                "side": side,
                "quantity": str(order.qty),
                "symbol": c.underlying,
                "strike_price": f"{c.strike:g}",
                "option_expire_date": c.expiry.isoformat(),
                "instrument_type": "OPTION",
                "option_type": "CALL" if c.right == "C" else "PUT",
                "market": "US",
            }],
        }

    async def place_order(self, order: Order) -> Order:
        acct = await self.ensure_account()
        order.updated = self.clock.now()
        try:
            data = response_json(await self._call(self.tc.order_v3.place_order, acct, [self._order_payload(order)]),
                                 "place option order")
        except Exception as e:
            order.status, order.message = "rejected", str(e)[:300]
            return order
        oid = pick(data, "order_id", "id") if isinstance(data, dict) else None
        if not oid:
            found = find_dicts(data, ["order_id"])
            oid = found[0]["order_id"] if found else None
        order.broker_order_id = str(oid) if oid else None
        order.status = "submitted"
        return order

    async def _detail(self, order: Order) -> dict:
        acct = await self.ensure_account()
        data = response_json(await self._call(self.tc.order_v3.get_order_detail, acct, order.client_order_id),
                             "order detail")
        rows = find_dicts(data, ["client_order_id"]) or ([data] if isinstance(data, dict) else [])
        for r in rows:
            if str(r.get("client_order_id")) == order.client_order_id:
                return r
        return rows[0] if rows else {}

    async def refresh_order(self, order: Order) -> Order:
        if order.status in ("filled", "cancelled", "rejected", "failed"):
            return order
        try:
            d = await self._detail(order)
        except Exception as e:
            log.warning("order refresh failed: %s", e)
            return order
        raw = str(pick(d, "status", "order_status", default="")).upper()
        order.status = STATUS_MAP.get(raw, order.status)  # type: ignore[assignment]
        filled = pick(d, "filled_quantity", "filled_qty", "total_filled_quantity")
        if filled is not None:
            order.filled_qty = int(fnum(filled))
        avg = pick(d, "filled_price", "avg_filled_price", "average_filled_price", "avg_price")
        if avg is not None and fnum(avg) > 0:
            order.avg_fill_price = fnum(avg)
        order.updated = self.clock.now()
        return order

    async def cancel_order(self, order: Order) -> Order:
        acct = await self.ensure_account()
        try:
            response_json(await self._call(self.tc.order_v3.cancel_order, acct, order.client_order_id), "cancel order")
        except Exception as e:
            log.warning("cancel failed (may already be filled): %s", e)
        for _ in range(5):
            order = await self.refresh_order(order)
            if order.status in ("cancelled", "filled", "rejected", "failed"):
                break
            await asyncio.sleep(1)
        if order.status in ("cancelled",) and order.filled_qty > 0:
            order.qty, order.status = order.filled_qty, "filled"
        return order

    async def replace_order(self, order: Order, new_limit: float) -> Order:
        """US replace needs the leg id from the order detail (see Webull SDK sample)."""
        acct = await self.ensure_account()
        try:
            d = await self._detail(order)
            legs = d.get("legs") or [{}]
            leg_id = legs[0].get("id")
            if not leg_id:
                raise RuntimeError("no leg id")
            remaining = order.qty - order.filled_qty
            payload = [{"client_order_id": order.client_order_id, "quantity": str(order.qty),
                        "limit_price": f"{new_limit:.2f}", "legs": [{"id": leg_id, "quantity": str(order.qty)}]}]
            response_json(await self._call(self.tc.order_v3.replace_order, acct, payload), "replace order")
            order.limit_price = new_limit
            order.updated = self.clock.now()
            log.info("replaced %s -> %.2f (remaining %d)", order.client_order_id, new_limit, remaining)
            return order
        except Exception as e:
            log.info("replace not available (%s); falling back to cancel/re-place", e)
            return await super().replace_order(order, new_limit)

    async def positions(self) -> list[dict]:
        acct = await self.ensure_account()
        data = response_json(await self._call(self.tc.account_v2.get_account_position, acct), "positions")
        out = []
        for r in find_dicts(data, ["symbol"]):
            qty = fnum(pick(r, "quantity", "qty", "position"))
            if qty:
                out.append({"symbol": str(r["symbol"]), "qty": qty,
                            "avg_price": fnum(pick(r, "cost_price", "avg_price", "average_cost")), "raw": r})
        return out
