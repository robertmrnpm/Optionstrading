"""End-to-end: full simulated trading day through the orchestrator, plus the dashboard API."""
import asyncio
from datetime import datetime

import pytest

from fastapi.testclient import TestClient

from optrader.agents.context import Context
from optrader.api.server import create_app
from optrader.brokers.paper import PaperBroker
from optrader.clock import ET, ManualClock
from optrader.data.simulated import SimulatedMarket
from optrader.db import Database
from optrader.events import EventBus
from optrader.models import Position
from optrader.notify import Notifier
from optrader.orchestrator import Orchestrator


def _orch(settings, tmp_path, start=datetime(2026, 9, 24, 9, 25, tzinfo=ET), seed=42):
    clock = ManualClock(start)
    data = SimulatedMarket(clock, settings.scanner.core_watchlist, seed)
    broker = PaperBroker(data, clock, settings.risk.starting_equity, tmp_path / "paper.json")
    db = Database(tmp_path / "j.db")
    ctx = Context(settings=settings, clock=clock, data=data, broker=broker, db=db, bus=EventBus(db, clock),
                  notifier=Notifier())
    return Orchestrator(ctx), clock


async def _run_day(orch, clock, until=(16, 5)):
    end = clock.now().replace(hour=until[0], minute=until[1])
    while clock.now() < end:
        await orch.step()
        for _ in range(5):
            await asyncio.sleep(0)
        clock.advance(30)
    # let exit/entry tasks finish
    for _ in range(50):
        await asyncio.sleep(0)


@pytest.mark.parametrize("seed", [42, 7, 2024])
def test_full_sim_day_auto_mode_respects_pdt_and_flattens(settings, tmp_path, seed):
    settings.execution.trade_mode = "auto"
    orch, clock = _orch(settings, tmp_path, seed=seed)
    asyncio.run(_run_day(orch, clock))
    positions = orch.ctx.db.list("positions", Position, limit=100)
    if seed == 42:
        assert positions, "expected the agents to take at least one trade in a full simulated day"
    assert all(p.status == "closed" for p in positions), "day trading: everything must be flat after the close"
    assert orch.ctx.db.day_trades_between(clock.today(), clock.today()) <= settings.risk.pdt_max_day_trades
    assert len(positions) <= settings.risk.max_trades_per_day
    for p in positions:
        assert p.exits and sum(x.qty for x in p.exits) == p.initial_qty


def test_alerts_mode_never_trades(settings, tmp_path):
    settings.execution.trade_mode = "alerts"
    orch, clock = _orch(settings, tmp_path)
    asyncio.run(_run_day(orch, clock, until=(12, 0)))
    assert orch.ctx.db.list("positions", Position) == []
    assert not orch.ctx.broker.holdings


def test_api_endpoints_and_auth(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "s3cret")
    orch, clock = _orch(settings, tmp_path, start=datetime(2026, 9, 24, 10, 30, tzinfo=ET))
    asyncio.run(orch.refresh_account(force=True))
    with TestClient(create_app(orch, start_agents=False)) as client:
        assert client.get("/api/health").json()["auth_required"] is True
        assert client.get("/api/status").status_code == 401
        h = {"Authorization": "Bearer s3cret"}
        st = client.get("/api/status", headers=h).json()
        assert st["mode"] == "sim" and st["account"]["equity"] == 10_000
        r = client.post("/api/trade-mode", json={"trade_mode": "auto"}, headers=h)
        assert r.json()["effective"] == "auto"
        assert client.post("/api/trade-mode", json={"trade_mode": "yolo"}, headers=h).status_code == 400
        r = client.patch("/api/settings", json={"risk": {"max_daily_loss": 150}}, headers=h)
        assert r.status_code == 200 and r.json()["risk"]["max_daily_loss"] == 150
        assert client.patch("/api/settings", json={"app": {"mode": "live"}}, headers=h).status_code == 400
        assert client.post("/api/kill-switch", json={"on": True}, headers=h).json()["kill_switch"] is True
        assert settings.risk.kill_switch is True
        assert client.post("/api/proposals/nope/approve", json={}, headers=h).status_code == 404
        j = client.get("/api/journal", headers=h).json()
        assert j["stats"]["overall"]["trades"] == 0
        assert client.get("/", headers=h).status_code == 200


def test_concurrent_approvals_cannot_exceed_pdt(settings, tmp_path):
    """Two proposals approved at once with one day trade left: only one may be entered."""
    from datetime import date

    settings.execution.trade_mode = "approval"
    settings.risk.max_open_positions = 5
    orch, clock = _orch(settings, tmp_path, start=datetime(2026, 9, 24, 10, 30, tzinfo=ET))
    db = orch.ctx.db
    db.record_day_trade(date(2026, 9, 23), "SPY", "a")
    db.record_day_trade(date(2026, 9, 23), "QQQ", "b")  # 1 day trade left

    async def run():
        await orch.refresh_account(force=True)
        from optrader.models import Signal
        props = []
        for sym in ("SPY", "QQQ"):
            spot = orch.ctx.data.spot(sym)
            sig = Signal(ts=clock.now(), symbol=sym, strategy="test", direction="bullish", score=90,
                         underlying_price=spot, stop_underlying=spot * 0.99, target_underlying=spot * 1.02,
                         min_dte=1, max_dte=7)
            c, note = await orch.selector.select(sig, clock.today(), 3.0)
            assert c, note
            props.append(await orch.execution.propose(sig, c, note))
        assert all(p.status == "pending" for p in props), [p.status_reason for p in props]
        for p in props:
            await orch.execution.approve(p.id)
        for _ in range(400):
            await asyncio.sleep(0)
            clock.advance(1)
        return props

    props = asyncio.run(run())
    statuses = sorted(p.status for p in props)
    assert statuses.count("filled") == 1, [(p.status, p.status_reason) for p in props]
    assert any("PDT" in p.status_reason or "working" in p.status_reason for p in props if p.status != "filled")
