"""Shared dependencies handed to every agent."""
from __future__ import annotations

from dataclasses import dataclass

from ..brokers.base import Broker
from ..clock import Clock
from ..config import Settings
from ..data.base import MarketDataProvider
from ..db import Database
from ..events import EventBus
from ..notify import Notifier


@dataclass
class Context:
    settings: Settings
    clock: Clock
    data: MarketDataProvider
    broker: Broker
    db: Database
    bus: EventBus
    notifier: Notifier

    async def notify(self, title: str, body: str, priority: str = "default") -> None:
        try:
            await self.notifier.send(title, body, priority)
        except Exception:
            pass
