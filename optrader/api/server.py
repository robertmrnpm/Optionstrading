"""Dashboard + REST API (FastAPI). Live updates stream over Server-Sent Events."""
from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..analytics import journal_stats
from ..models import Position, Proposal, Signal
from ..orchestrator import Orchestrator

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class ApproveBody(BaseModel):
    qty: int | None = None


class RejectBody(BaseModel):
    reason: str = "rejected by user"


class KillBody(BaseModel):
    on: bool
    flatten: bool = False


class ModeBody(BaseModel):
    trade_mode: str


def create_app(orch: Orchestrator, start_agents: bool = True) -> FastAPI:
    ctx = orch.ctx
    token = ctx.settings.dashboard_token

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(orch.run()) if start_agents else None
        yield
        orch.stop()
        if task:
            task.cancel()

    app = FastAPI(title="Options Day-Trading Agents", lifespan=lifespan)

    def auth(request: Request) -> None:
        if not token:
            return
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip() \
            or request.query_params.get("token", "")
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(401, "invalid or missing dashboard token")

    guarded = [Depends(auth)]

    # ---- pages ------------------------------------------------------------------------------------------
    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/api/health")
    async def health():
        return {"ok": True, "auth_required": bool(token)}

    # ---- state -------------------------------------------------------------------------------------------
    @app.get("/api/status", dependencies=guarded)
    async def status():
        return orch.status()

    @app.get("/api/proposals", dependencies=guarded)
    async def proposals(limit: int = 60):
        live = {p.id: p for p in orch.execution.proposals.values()}
        stored = ctx.db.list("proposals", Proposal, limit=limit)
        merged = {p.id: p for p in stored}
        merged.update(live)
        items = sorted(merged.values(), key=lambda p: p.created, reverse=True)[:limit]
        return [p.model_dump(mode="json") for p in items]

    @app.post("/api/proposals/{pid}/approve", dependencies=guarded)
    async def approve(pid: str, body: ApproveBody | None = None):
        try:
            p = await orch.execution.approve(pid, body.qty if body else None)
        except KeyError:
            raise HTTPException(404, "proposal not found")
        except ValueError as e:
            raise HTTPException(409, str(e))
        ctx.bus.publish("proposal", f"Approved {p.contract.label()} x{p.qty}", {"proposal": p.model_dump(mode="json")})
        return p.model_dump(mode="json")

    @app.post("/api/proposals/{pid}/reject", dependencies=guarded)
    async def reject(pid: str, body: RejectBody | None = None):
        try:
            return orch.execution.reject(pid, body.reason if body else "rejected by user").model_dump(mode="json")
        except KeyError:
            raise HTTPException(404, "proposal not found")

    @app.get("/api/positions", dependencies=guarded)
    async def positions():
        today = ctx.clock.today()
        open_pos = orch.positions.open_positions()
        closed = [p for p in ctx.db.list("positions", Position, day=today) if p.status == "closed"]
        return {
            "open": [p.model_dump(mode="json") | {"unrealized_pnl": round(p.unrealized_pnl, 2),
                                                  "pnl_pct": round(p.pnl_pct, 1)} for p in open_pos],
            "closed_today": [p.model_dump(mode="json") for p in closed],
        }

    @app.post("/api/positions/{pos_id}/close", dependencies=guarded)
    async def close_position(pos_id: str):
        ok = await orch.positions.close(pos_id, "manual close")
        if not ok:
            raise HTTPException(409, "position not found or already closing")
        return {"ok": True}

    @app.post("/api/flatten", dependencies=guarded)
    async def flatten():
        n = await orch.positions.flatten_all("manual flatten all")
        ctx.bus.publish("system", f"Flatten all requested ({n} positions)")
        return {"closing": n}

    @app.post("/api/kill-switch", dependencies=guarded)
    async def kill_switch(body: KillBody):
        ctx.settings.risk.kill_switch = body.on
        ctx.settings.save_overrides()
        for p in orch.execution.pending():
            if body.on:
                orch.execution.reject(p.id, "kill switch")
        n = await orch.positions.flatten_all("kill switch") if (body.on and body.flatten) else 0
        ctx.bus.publish("system", f"Kill switch {'ON' if body.on else 'OFF'}" + (f", flattening {n}" if n else ""))
        return {"kill_switch": body.on, "flattening": n}

    @app.post("/api/trade-mode", dependencies=guarded)
    async def trade_mode(body: ModeBody):
        try:
            ctx.settings.apply_patch({"execution": {"trade_mode": body.trade_mode}})
        except Exception as e:
            raise HTTPException(400, str(e))
        ctx.settings.save_overrides()
        eff = orch.execution.effective_mode
        msg = f"Trade mode set to {body.trade_mode}"
        if eff != body.trade_mode:
            msg += f" (running as {eff}: set ALLOW_LIVE_AUTO_TRADING=true to auto-trade real money)"
        ctx.bus.publish("system", msg)
        return {"trade_mode": body.trade_mode, "effective": eff}

    @app.get("/api/signals", dependencies=guarded)
    async def signals(limit: int = 100):
        return [s.model_dump(mode="json") for s in orch.recent_signals[:limit]] or \
            [s.model_dump(mode="json") for s in ctx.db.list("signals", Signal, limit=limit)]

    @app.get("/api/events", dependencies=guarded)
    async def events(limit: int = 150):
        return ctx.db.events(limit)

    @app.get("/api/journal", dependencies=guarded)
    async def journal(days: int = 30):
        since = ctx.clock.today() - timedelta(days=days)
        pos = ctx.db.list("positions", Position, since=since, limit=5000)
        closed = [p for p in pos if p.status == "closed"]
        return {"stats": journal_stats(closed),
                "trades": [p.model_dump(mode="json") for p in sorted(closed, key=lambda p: p.entry_time, reverse=True)]}

    @app.get("/api/settings", dependencies=guarded)
    async def get_settings():
        return {s: getattr(ctx.settings, s).model_dump() for s in ctx.settings.EDITABLE_SECTIONS} | {
            "app": ctx.settings.app.model_dump(), "scanner": ctx.settings.scanner.model_dump()}

    @app.patch("/api/settings", dependencies=guarded)
    async def patch_settings(request: Request):
        patch = await request.json()
        try:
            ctx.settings.apply_patch(patch)
        except Exception as e:
            raise HTTPException(400, str(e))
        ctx.settings.save_overrides()
        ctx.bus.publish("system", "Settings updated: " + ", ".join(
            f"{sec}.{k}={v}" for sec, vals in patch.items() for k, v in vals.items()))
        return await get_settings()

    @app.get("/api/stream", dependencies=guarded)
    async def stream(request: Request):
        q = ctx.bus.subscribe()

        async def gen():
            try:
                yield "retry: 3000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(ev, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                ctx.bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
