"""AI analyst agent with a fake Anthropic client (no network, no API key needed)."""
import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from optrader.agents.ai_analyst import AIAnalyst
from optrader.clock import ET
from optrader.config import AISettings
from optrader.models import ExitPlan, OptionContract, Proposal, Signal


def _proposal():
    now = datetime(2026, 9, 24, 10, 30, tzinfo=ET)
    sig = Signal(ts=now, symbol="NVDA", strategy="momentum", direction="bullish", score=78, reasons=["ORB breakout"],
                 underlying_price=180, stop_underlying=178, target_underlying=184, meta={"setup": "orb"})
    c = OptionContract(symbol="NVDA261002C00182500", underlying="NVDA", expiry=date(2026, 10, 2), strike=182.5,
                       right="C", bid=2.4, ask=2.5, volume=5000, open_interest=20000, delta=0.42, iv=0.45)
    plan = ExitPlan(stop_price=1.7, target1_price=3.45, target1_qty=0, target2_price=4.65, trail_activation_price=3.0,
                    trail_pct=0.2, time_stop_minutes=60)
    return Proposal(created=now, expires=now + timedelta(minutes=2), signal=sig, contract=c, qty=1, limit_price=2.45,
                    est_cost=245, risk_dollars=74, exit_plan=plan)


def _resp(stop="end_turn", payload=None):
    text = json.dumps(payload or {"verdict": "take", "confidence": 72, "summary": "Clean breakout.",
                                  "risks": ["CPI tomorrow"], "catalysts": ["analyst upgrade"]})
    return SimpleNamespace(stop_reason=stop, model="claude-opus-5", content=[SimpleNamespace(type="text", text=text)])


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _analyst(monkeypatch, responses):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    a = AIAnalyst(AISettings(enabled=True))
    fake = FakeMessages(responses)
    a._client = SimpleNamespace(beta=SimpleNamespace(messages=fake), messages=fake)
    return a, fake


def test_disabled_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    r = asyncio.run(AIAnalyst(AISettings(enabled=True)).review(_proposal()))
    assert r.error == "disabled"


def test_structured_review(monkeypatch):
    a, fake = _analyst(monkeypatch, [_resp()])
    r = asyncio.run(a.review(_proposal()))
    assert r.verdict == "take" and r.confidence == 72 and r.risks == ["CPI tomorrow"]
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["tools"][0]["type"] == "web_search_20260209"
    assert "NVDA" in call["messages"][0]["content"]


def test_refusal_and_pause_turn(monkeypatch):
    a, _ = _analyst(monkeypatch, [_resp(stop="refusal")])
    assert asyncio.run(a.review(_proposal())).error == "refusal"
    a, fake = _analyst(monkeypatch, [_resp(stop="pause_turn"), _resp()])
    r = asyncio.run(a.review(_proposal()))
    assert r.verdict == "take" and len(fake.calls) == 2
    assert fake.calls[1]["messages"][-1]["role"] == "assistant"


def test_errors_never_raise(monkeypatch):
    a, _ = _analyst(monkeypatch, [RuntimeError("boom")])
    r = asyncio.run(a.review(_proposal()))
    assert r.verdict == "caution" and "boom" in r.error
