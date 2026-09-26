"""In-process event bus: agents publish, the dashboard (SSE) and notifier subscribe."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .clock import Clock
from .db import Database

log = logging.getLogger("optrader")


class EventBus:
    def __init__(self, db: Database, clock: Clock):
        self.db = db
        self.clock = clock
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, kind: str, message: str, data: dict[str, Any] | None = None, persist: bool = True) -> dict:
        if persist:
            ev = self.db.log_event(self.clock.now(), kind, message, data)
        else:
            ev = {"id": "", "ts": self.clock.now().isoformat(), "kind": kind, "message": message, "data": data or {}}
        log.info("[%s] %s", kind, message)
        for q in list(self._subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass
        return ev
