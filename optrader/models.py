"""Shared domain models. Pydantic so everything serializes cleanly to the DB and dashboard."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Right = Literal["C", "P"]
Direction = Literal["bullish", "bearish"]


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


class Bar(BaseModel):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class Quote(BaseModel):
    symbol: str
    last: float
    bid: float = 0.0
    ask: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    prev_close: float = 0.0
    volume: float = 0.0
    ts: datetime | None = None

    @property
    def change_pct(self) -> float:
        return (self.last / self.prev_close - 1) * 100 if self.prev_close else 0.0

    @property
    def gap_pct(self) -> float:
        return (self.open / self.prev_close - 1) * 100 if self.prev_close and self.open else 0.0


def occ_symbol(underlying: str, expiry: date, right: str, strike: float) -> str:
    """OCC-style option symbol, e.g. AAPL260522C00300000 (the format Webull uses)."""
    return f"{underlying.upper()}{expiry:%y%m%d}{right}{int(round(strike * 1000)):08d}"


class OptionContract(BaseModel):
    symbol: str
    underlying: str
    expiry: date
    strike: float
    right: Right
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    volume: int = 0
    open_interest: int = 0
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return round((self.bid + self.ask) / 2, 4)
        return self.last or self.ask or self.bid

    @property
    def spread_pct(self) -> float:
        m = self.mid
        if m <= 0 or self.ask <= 0:
            return 1.0
        return (self.ask - self.bid) / m

    def dte(self, today: date) -> int:
        return (self.expiry - today).days

    def label(self) -> str:
        kind = "CALL" if self.right == "C" else "PUT"
        return f"{self.underlying} {self.expiry:%m/%d} {self.strike:g}{self.right} ({kind})"


class Signal(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sig_"))
    ts: datetime
    symbol: str
    strategy: str
    direction: Direction
    score: float                       # 0..100 conviction
    reasons: list[str] = Field(default_factory=list)
    underlying_price: float
    stop_underlying: float | None = None     # thesis invalidated if underlying crosses this
    target_underlying: float | None = None
    min_dte: int = 0
    max_dte: int = 7
    target_delta: float | None = None
    zero_dte: bool = False
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def right(self) -> Right:
        return "C" if self.direction == "bullish" else "P"


class ExitPlan(BaseModel):
    stop_price: float
    target1_price: float
    target1_qty: int
    target2_price: float
    trail_activation_price: float
    trail_pct: float
    time_stop_minutes: int
    stop_underlying: float | None = None
    flatten_at: str = "15:50"


class AIReview(BaseModel):
    verdict: Literal["take", "caution", "skip"]
    confidence: int
    summary: str
    risks: list[str] = Field(default_factory=list)
    catalysts: list[str] = Field(default_factory=list)
    model: str = ""
    error: str | None = None


ProposalStatus = Literal[
    "pending", "approved", "rejected", "expired", "blocked", "submitted", "filled", "cancelled", "failed", "alert"
]


class Proposal(BaseModel):
    id: str = Field(default_factory=lambda: new_id("prop_"))
    created: datetime
    expires: datetime
    signal: Signal
    contract: OptionContract
    qty: int
    limit_price: float
    est_cost: float
    risk_dollars: float
    exit_plan: ExitPlan
    status: ProposalStatus = "pending"
    status_reason: str = ""
    risk_notes: list[str] = Field(default_factory=list)
    ai_review: AIReview | None = None
    position_id: str | None = None


OrderStatus = Literal["new", "submitted", "partial", "filled", "cancelled", "rejected", "failed"]


class Order(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ord_"))
    client_order_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    broker_order_id: str | None = None
    contract: OptionContract
    side: Literal["buy", "sell"]
    qty: int
    limit_price: float
    status: OrderStatus = "new"
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    purpose: Literal["entry", "exit"] = "entry"
    reason: str = ""
    created: datetime
    updated: datetime
    proposal_id: str | None = None
    position_id: str | None = None
    message: str = ""


class ExitFill(BaseModel):
    ts: datetime
    qty: int
    price: float
    reason: str
    pnl: float


class Position(BaseModel):
    id: str = Field(default_factory=lambda: new_id("pos_"))
    proposal_id: str | None = None
    strategy: str
    underlying: str
    contract: OptionContract
    qty: int                      # remaining open contracts
    initial_qty: int
    entry_price: float
    entry_time: datetime
    exit_plan: ExitPlan
    high_water: float
    last_price: float
    stop_price: float
    target1_done: bool = False
    status: Literal["open", "closing", "closed"] = "open"
    exits: list[ExitFill] = Field(default_factory=list)
    realized_pnl: float = 0.0
    closed_time: datetime | None = None
    external: bool = False        # opened outside the app (found at the broker)

    @property
    def unrealized_pnl(self) -> float:
        return (self.last_price - self.entry_price) * self.qty * 100

    @property
    def pnl_pct(self) -> float:
        return (self.last_price / self.entry_price - 1) * 100 if self.entry_price else 0.0


class AccountSnapshot(BaseModel):
    equity: float
    cash: float
    buying_power: float
    settled_cash: float | None = None
    day_trades_used: int | None = None   # as reported by the broker, when available
    source: str = ""
