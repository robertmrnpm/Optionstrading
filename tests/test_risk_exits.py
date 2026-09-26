from datetime import date, datetime, timedelta

from optrader.agents.position_manager import decide_exit
from optrader.agents.risk import RiskManager
from optrader.clock import ET
from optrader.db import Database
from optrader.models import AccountSnapshot, ExitPlan, OptionContract, Position, Signal


def _sig(now, zero_dte=False, score=80):
    return Signal(ts=now, symbol="TSLA", strategy="momentum", direction="bullish", score=score,
                  underlying_price=400, stop_underlying=395, target_underlying=410, zero_dte=zero_dte)


def _contract(mid=2.0):
    return OptionContract(symbol="TSLA261002C00400000", underlying="TSLA", expiry=date(2026, 10, 2), strike=400,
                          right="C", bid=mid - 0.05, ask=mid + 0.05, volume=1000, open_interest=5000, delta=0.45)


def _acct(eq=10_000):
    return AccountSnapshot(equity=eq, cash=eq, buying_power=eq)


def _open_pos(rm, now, symbol="NVDA"):
    plan = rm.exit_plan(_sig(now), _contract(), 2.0, 1)
    return Position(strategy="x", underlying=symbol, contract=_contract(), qty=1, initial_qty=1, entry_price=2,
                    entry_time=now, exit_plan=plan, high_water=2, last_price=2, stop_price=1.4)


def test_sizing_uses_fixed_fractional_risk(settings, clock, tmp_path):
    rm = RiskManager(settings, Database(tmp_path / "t.db"), clock)
    d = rm.check_entry(_sig(clock.now()), _contract(2.0), 2.0, _acct(), [], [])
    assert d.ok, d.blocks
    # 1% of $10k = $100 risk budget; stop is 30% of $200/contract = $60 -> 1 contract
    assert d.qty == 1
    assert d.risk_dollars == 60


def test_blocks_when_too_expensive(settings, clock, tmp_path):
    rm = RiskManager(settings, Database(tmp_path / "t.db"), clock)
    d = rm.check_entry(_sig(clock.now()), _contract(9.0), 9.0, _acct(), [], [])
    assert not d.ok
    assert any("position size" in b for b in d.blocks)
    assert rm.max_affordable_premium(False, _acct()) < 3.4


def test_pdt_blocks_fourth_day_trade(settings, clock, tmp_path):
    db = Database(tmp_path / "t.db")
    rm = RiskManager(settings, db, clock)
    for i, d in enumerate([date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)]):
        db.record_day_trade(d, "SPY", f"p{i}")
    d = rm.check_entry(_sig(clock.now()), _contract(), 2.0, _acct(), [], [])
    assert not d.ok
    assert any("PDT" in b for b in d.blocks)
    # PDT doesn't apply at/above $25k, or in a cash account
    assert not any("PDT" in b for b in rm.check_entry(_sig(clock.now()), _contract(), 2.0, _acct(30_000), [], []).blocks)
    settings.risk.account_type = "cash"
    assert not any("PDT" in b for b in rm.check_entry(_sig(clock.now()), _contract(), 2.0, _acct(), [], []).blocks)


def test_pdt_window_is_five_trading_days(settings, clock, tmp_path):
    db = Database(tmp_path / "t.db")
    rm = RiskManager(settings, db, clock)
    # 2026-09-17 is 6 trading days before 2026-09-24 -> outside the window
    for i in range(3):
        db.record_day_trade(date(2026, 9, 17), "SPY", f"old{i}")
    assert rm.day_trades_used(_acct()) == 0
    db.record_day_trade(date(2026, 9, 18), "SPY", "in")
    assert rm.day_trades_used(_acct()) == 1


def test_pdt_counts_open_positions_opened_today(settings, clock, tmp_path):
    db = Database(tmp_path / "t.db")
    rm = RiskManager(settings, db, clock)
    db.record_day_trade(clock.today(), "SPY", "p0")
    db.record_day_trade(clock.today(), "QQQ", "p1")
    assert rm.day_trades_remaining(_acct(), [_open_pos(rm, clock.now())]) == 0


def test_time_windows_and_kill_switch(settings, clock, tmp_path):
    rm = RiskManager(settings, Database(tmp_path / "t.db"), clock)
    clock.set(datetime(2026, 9, 24, 9, 32, tzinfo=ET))
    assert any("first" in b for b in rm.check_entry(_sig(clock.now()), _contract(), 2.0, _acct(), [], []).blocks)
    clock.set(datetime(2026, 9, 24, 15, 5, tzinfo=ET))
    blocks = rm.check_entry(_sig(clock.now(), zero_dte=True), _contract(), 2.0, _acct(), [], []).blocks
    assert any("last entry" in b for b in blocks)
    clock.set(datetime(2026, 9, 24, 11, 0, tzinfo=ET))
    settings.risk.kill_switch = True
    assert any("kill switch" in b for b in rm.check_entry(_sig(clock.now()), _contract(), 2.0, _acct(), [], []).blocks)


def test_market_closed_on_holiday(settings, tmp_path):
    from optrader.clock import ManualClock
    c = ManualClock(datetime(2026, 11, 26, 11, 0, tzinfo=ET))  # Thanksgiving
    rm = RiskManager(settings, Database(tmp_path / "t.db"), c)
    assert any("closed" in b for b in rm.check_entry(_sig(c.now()), _contract(), 2.0, _acct(), [], []).blocks)


def test_daily_loss_limit_and_streak(settings, clock, tmp_path):
    rm = RiskManager(settings, Database(tmp_path / "t.db"), clock)
    losers = []
    for i in range(3):
        p = _open_pos(rm, clock.now(), f"S{i}")
        p.status, p.qty, p.realized_pnl, p.closed_time = "closed", 0, -40, clock.now() + timedelta(minutes=i)
        losers.append(p)
    d = rm.check_entry(_sig(clock.now()), _contract(), 2.0, _acct(), losers, [])
    assert any("in a row" in b for b in d.blocks)
    losers[0].realized_pnl = -300
    d = rm.check_entry(_sig(clock.now()), _contract(), 2.0, _acct(), losers, [])
    assert any("daily loss" in b for b in d.blocks)


def test_one_position_per_symbol_and_max_open(settings, clock, tmp_path):
    rm = RiskManager(settings, Database(tmp_path / "t.db"), clock)
    d = rm.check_entry(_sig(clock.now()), _contract(), 2.0, _acct(), [], [_open_pos(rm, clock.now(), "TSLA")])
    assert any("already holding" in b for b in d.blocks)
    two = [_open_pos(rm, clock.now(), "A"), _open_pos(rm, clock.now(), "B")]
    d = rm.check_entry(_sig(clock.now()), _contract(), 2.0, _acct(), [], two)
    assert any("max open positions" in b for b in d.blocks)


def _pos(now, entry=2.0, qty=2):
    plan = ExitPlan(stop_price=1.4, target1_price=2.8, target1_qty=1, target2_price=3.8, trail_activation_price=2.5,
                    trail_pct=0.2, time_stop_minutes=60, stop_underlying=395)
    return Position(strategy="m", underlying="TSLA", contract=_contract(entry), qty=qty, initial_qty=qty,
                    entry_price=entry, entry_time=now, exit_plan=plan, high_water=entry, last_price=entry,
                    stop_price=1.4)


def test_exit_rules(clock):
    now = clock.now()
    flat = datetime(2026, 9, 24, 15, 50, tzinfo=ET)
    p = _pos(now)
    p.last_price = 1.35
    assert decide_exit(p, now, 401, flat)[0] == "stop loss"
    p = _pos(now)
    p.last_price = p.high_water = 2.85
    assert decide_exit(p, now, 401, flat) == ("target 1 (scale out)", 1)
    p = _pos(now)
    p.last_price = 3.9
    assert decide_exit(p, now, 401, flat) == ("target 2", 2)
    p = _pos(now)
    assert decide_exit(p, now, 394, flat)[0].startswith("thesis invalidated")
    p = _pos(now)
    p.high_water, p.last_price = 3.0, 2.35  # trailing stop = 2.40
    assert decide_exit(p, now, 401, flat)[0] == "trailing stop"
    p = _pos(now)
    assert decide_exit(p, now + timedelta(minutes=61), 401, flat)[0].startswith("time stop")
    p = _pos(now)
    assert decide_exit(p, flat, 401, flat)[0] == "end-of-day flatten"
    p = _pos(now)
    assert decide_exit(p, now + timedelta(minutes=5), 401, flat) == (None, 0)


def test_exit_plan_prices_on_valid_ticks(settings, clock, tmp_path):
    rm = RiskManager(settings, Database(tmp_path / "t.db"), clock)
    plan = rm.exit_plan(_sig(clock.now()), _contract(4.1), 4.1, 3)  # >= $3 on a single name -> nickel ticks
    for px in (plan.stop_price, plan.target1_price, plan.target2_price):
        assert abs(px * 20 - round(px * 20)) < 1e-6
    assert plan.target1_qty == 1


def test_cash_account_only_spends_settled_funds(settings, clock, tmp_path):
    from optrader.models import ExitFill
    settings.risk.account_type = "cash"
    rm = RiskManager(settings, Database(tmp_path / "t.db"), clock)
    # broker reports settled cash -> it caps spending
    acct = AccountSnapshot(equity=10_000, cash=10_000, buying_power=10_000, settled_cash=150)
    assert rm.available_funds(acct) == 150
    d = rm.check_entry(_sig(clock.now()), _contract(2.0), 2.0, acct, [], [])
    assert not d.ok and any("settled cash" in b for b in d.blocks)
    # broker doesn't report it -> today's sale proceeds are treated as unsettled
    sold = _open_pos(rm, clock.now(), "AMD")
    sold.exits.append(ExitFill(ts=clock.now(), qty=1, price=2.5, reason="target 2", pnl=50))
    acct2 = AccountSnapshot(equity=10_000, cash=10_000, buying_power=400)
    assert rm.available_funds(acct2, [sold]) == 150
    # margin accounts use plain buying power
    settings.risk.account_type = "margin"
    assert rm.available_funds(acct2, [sold]) == 400


def test_daily_profit_goal_stops_new_trades(settings, clock, tmp_path):
    rm = RiskManager(settings, Database(tmp_path / "t.db"), clock)
    winner = _open_pos(rm, clock.now(), "META")
    winner.status, winner.qty, winner.realized_pnl, winner.closed_time = "closed", 0, 1200, clock.now()
    d = rm.check_entry(_sig(clock.now()), _contract(), 2.0, _acct(30_000), [winner], [])
    assert d.ok  # goal off by default
    settings.risk.daily_profit_target = 1000
    d = rm.check_entry(_sig(clock.now()), _contract(), 2.0, _acct(30_000), [winner], [])
    assert any("profit goal" in b for b in d.blocks)
    settings.risk.stop_at_profit_target = False
    assert rm.check_entry(_sig(clock.now()), _contract(), 2.0, _acct(30_000), [winner], []).ok
