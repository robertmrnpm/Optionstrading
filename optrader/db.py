"""SQLite trade journal. Every entity is stored as JSON with a few indexed columns."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

TABLES = ("signals", "proposals", "orders", "positions", "events")

SCHEMA = """
CREATE TABLE IF NOT EXISTS {t} (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    status TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_{t}_day ON {t}(day);
"""

DAY_TRADES = """
CREATE TABLE IF NOT EXISTS day_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    symbol TEXT NOT NULL,
    position_id TEXT
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            for t in TABLES:
                self.conn.executescript(SCHEMA.format(t=t))
            self.conn.executescript(DAY_TRADES)
            self.conn.commit()

    # ---- generic JSON entity storage -------------------------------------------------
    def put(self, table: str, obj: BaseModel, ts: datetime, status: str | None = None) -> None:
        assert table in TABLES
        with self._lock:
            self.conn.execute(
                f"INSERT INTO {table}(id, ts, day, status, data) VALUES (?,?,?,?,?) "
                f"ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data",
                (getattr(obj, "id"), ts.isoformat(), ts.date().isoformat(), status, obj.model_dump_json()),
            )
            self.conn.commit()

    def get(self, table: str, id_: str, model: type[T]) -> T | None:
        with self._lock:
            row = self.conn.execute(f"SELECT data FROM {table} WHERE id=?", (id_,)).fetchone()
        return model.model_validate_json(row[0]) if row else None

    def list(self, table: str, model: type[T], day: date | None = None, status: str | Iterable[str] | None = None,
             limit: int = 500, since: date | None = None) -> list[T]:
        q = f"SELECT data FROM {table} WHERE 1=1"
        args: list[Any] = []
        if day:
            q += " AND day=?"
            args.append(day.isoformat())
        if since:
            q += " AND day>=?"
            args.append(since.isoformat())
        if status:
            statuses = [status] if isinstance(status, str) else list(status)
            q += f" AND status IN ({','.join('?' * len(statuses))})"
            args.extend(statuses)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self.conn.execute(q, args).fetchall()
        return [model.model_validate_json(r[0]) for r in rows]

    # ---- event log ---------------------------------------------------------------------
    def log_event(self, ts: datetime, kind: str, message: str, data: dict | None = None) -> dict:
        import uuid
        ev = {"id": uuid.uuid4().hex[:12], "ts": ts.isoformat(), "kind": kind, "message": message, "data": data or {}}
        with self._lock:
            self.conn.execute("INSERT INTO events(id, ts, day, status, data) VALUES (?,?,?,?,?)",
                              (ev["id"], ev["ts"], ts.date().isoformat(), kind, json.dumps(ev, default=str)))
            self.conn.commit()
        return ev

    def events(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT data FROM events ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    # ---- PDT day-trade ledger ------------------------------------------------------------
    def record_day_trade(self, day: date, symbol: str, position_id: str | None) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO day_trades(day, symbol, position_id) VALUES (?,?,?)",
                              (day.isoformat(), symbol, position_id))
            self.conn.commit()

    def day_trades_between(self, start: date, end: date) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) FROM day_trades WHERE day>=? AND day<=?",
                                    (start.isoformat(), end.isoformat())).fetchone()
        return int(row[0])

    def close(self) -> None:
        with self._lock:
            self.conn.close()
