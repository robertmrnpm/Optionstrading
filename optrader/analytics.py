"""Performance statistics for the trade journal and backtests."""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .models import Position


def trade_stats(pnls: list[float]) -> dict:
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "trades": n,
        "net_pnl": round(sum(pnls), 2),
        "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (None if not wins else float("inf")),
        "expectancy": round(sum(pnls) / n, 2) if n else 0.0,
        "max_drawdown": round(max_dd, 2),
        "largest_win": round(max(wins), 2) if wins else 0.0,
        "largest_loss": round(min(losses), 2) if losses else 0.0,
    }


def journal_stats(positions: Iterable[Position]) -> dict:
    closed = sorted([p for p in positions if p.status == "closed"], key=lambda p: p.closed_time or p.entry_time)
    overall = trade_stats([p.realized_pnl for p in closed])
    by_strategy: dict[str, list[float]] = defaultdict(list)
    by_day: dict[str, float] = defaultdict(float)
    by_exit: dict[str, list[float]] = defaultdict(list)
    for p in closed:
        by_strategy[p.strategy].append(p.realized_pnl)
        by_day[(p.closed_time or p.entry_time).date().isoformat()] += p.realized_pnl
        if p.exits:
            by_exit[p.exits[-1].reason.split(" (")[0]].append(p.realized_pnl)
    equity_curve, running = [], 0.0
    for p in closed:
        running += p.realized_pnl
        equity_curve.append({"ts": (p.closed_time or p.entry_time).isoformat(), "pnl": round(running, 2)})
    return {
        "overall": overall,
        "by_strategy": {k: trade_stats(v) for k, v in by_strategy.items()},
        "by_exit_reason": {k: trade_stats(v) for k, v in by_exit.items()},
        "by_day": [{"day": d, "pnl": round(v, 2)} for d, v in sorted(by_day.items())],
        "equity_curve": equity_curve,
    }
