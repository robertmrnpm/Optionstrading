"""AI analyst agent: a second opinion on every trade proposal using Claude.

It sees the signal, the chosen contract, the exit plan and live features, can search the
web for news/catalysts, and returns a structured verdict (take / caution / skip). In auto
mode you can require its OK before an order is sent (``ai.required_for_auto``).

Requires ``ANTHROPIC_API_KEY`` and ``ai.enabled: true``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from ..config import AISettings
from ..models import AIReview, Proposal
from .features import SymbolFeatures

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a disciplined options day-trading risk analyst reviewing a trade proposed by \
automated scanners for a small (<$25k) account. Your job is to catch bad trades, not to cheerlead.

Evaluate: whether the technical thesis is coherent with the data given; whether there is a news/catalyst \
reason for the move (search the web for today's news on the ticker when useful); scheduled events that could \
blow through the stop (earnings, FOMC, CPI, company events); liquidity and spread cost; whether the contract \
(expiry/delta) fits the thesis and holding time; and whether the reward justifies the risk.

Verdicts: "take" = thesis is sound and nothing material argues against it; "caution" = tradable but with a \
specific concern the trader should weigh; "skip" = a material reason not to take it. Confidence is 0-100 in \
your verdict. Keep the summary to 2-3 sentences. Be concrete and brief."""

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["take", "caution", "skip"]},
        "confidence": {"type": "integer"},
        "summary": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}},
        "catalysts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "confidence", "summary", "risks", "catalysts"],
    "additionalProperties": False,
}


class AIAnalyst:
    def __init__(self, cfg: AISettings):
        self.cfg = cfg
        self._client = None

    @property
    def available(self) -> bool:
        return self.cfg.enabled and bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.AsyncAnthropic()
        return self._client

    def _prompt(self, p: Proposal, f: SymbolFeatures | None, market: dict) -> str:
        s, c, x = p.signal, p.contract, p.exit_plan
        lines = [
            f"Time (ET): {s.ts:%Y-%m-%d %H:%M}",
            f"Proposed: BUY {p.qty}x {c.label()} [{c.symbol}] limit ${p.limit_price:.2f} "
            f"(est. cost ${p.est_cost:,.0f}, max planned loss ${p.risk_dollars:,.0f})",
            f"Strategy: {s.strategy} / {s.meta.get('setup')} — {s.direction}, scanner score {s.score:.0f}/100",
            "Scanner reasons: " + "; ".join(s.reasons),
            f"Underlying {s.symbol}: ${s.underlying_price:.2f}; thesis invalid beyond ${s.stop_underlying}; "
            f"target ${s.target_underlying}",
            f"Contract: bid {c.bid:.2f} / ask {c.ask:.2f} (spread {c.spread_pct*100:.1f}%), delta "
            f"{(c.delta or 0):.2f}, IV {(c.iv or 0)*100:.0f}%, theta {(c.theta or 0):.3f}/day, "
            f"volume {c.volume:,}, OI {c.open_interest:,}",
            f"Exit plan: stop ${x.stop_price:.2f}, target1 ${x.target1_price:.2f} ({x.target1_qty} contracts), "
            f"target2 ${x.target2_price:.2f}, trail {x.trail_pct*100:.0f}% after ${x.trail_activation_price:.2f}, "
            f"time stop {x.time_stop_minutes} min, flat by {x.flatten_at} ET",
        ]
        if f:
            lines.append("Live features: " + json.dumps(f.summary()))
        if market:
            lines.append("Market context: " + json.dumps(market))
        if p.risk_notes:
            lines.append("Risk notes: " + "; ".join(p.risk_notes))
        lines.append("Review this trade and return your verdict.")
        return "\n".join(lines)

    async def review(self, p: Proposal, f: SymbolFeatures | None = None, market: dict | None = None) -> AIReview:
        if not self.available:
            return AIReview(verdict="caution", confidence=0, summary="AI analyst disabled", error="disabled")
        try:
            return await asyncio.wait_for(self._review(p, f, market or {}), timeout=self.cfg.timeout_seconds)
        except asyncio.TimeoutError:
            return AIReview(verdict="caution", confidence=0, summary="AI review timed out", error="timeout",
                            model=self.cfg.model)
        except Exception as e:  # never let the analyst crash the trading loop
            log.exception("AI review failed")
            return AIReview(verdict="caution", confidence=0, summary=f"AI review failed: {e}", error=str(e)[:300],
                            model=self.cfg.model)

    async def _review(self, p: Proposal, f: SymbolFeatures | None, market: dict) -> AIReview:
        import anthropic
        client = self._get_client()
        messages: list[dict] = [{"role": "user", "content": self._prompt(p, f, market)}]
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}] if self.cfg.web_search else []
        use_fallbacks = True
        for _attempt in range(4):
            kwargs = dict(
                model=self.cfg.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                thinking={"type": "adaptive"},
                output_config={"effort": self.cfg.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
                messages=messages,
            )
            if tools:
                kwargs["tools"] = tools
            try:
                if use_fallbacks:
                    resp = await client.beta.messages.create(
                        betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
                else:
                    resp = await client.messages.create(**kwargs)
            except anthropic.BadRequestError as e:
                # Degrade gracefully (e.g. an org without web search or fallbacks enabled).
                if use_fallbacks:
                    log.info("retrying AI review without server-side fallbacks: %s", e)
                    use_fallbacks = False
                    continue
                if tools:
                    log.info("retrying AI review without web search: %s", e)
                    tools = []
                    continue
                raise
            if resp.stop_reason == "refusal":
                return AIReview(verdict="caution", confidence=0, summary="AI analyst declined to review this trade",
                                error="refusal", model=resp.model)
            if resp.stop_reason == "pause_turn":
                # long server-side web search: continue the same turn
                messages = messages[:1] + [{"role": "assistant", "content": resp.content}]
                continue
            text = next((b.text for b in reversed(resp.content) if b.type == "text"), "")
            data = json.loads(text)
            return AIReview(verdict=data["verdict"], confidence=int(data["confidence"]), summary=data["summary"],
                            risks=data.get("risks", []), catalysts=data.get("catalysts", []), model=resp.model)
        return AIReview(verdict="caution", confidence=0, summary="AI review did not complete", error="incomplete",
                        model=self.cfg.model)
