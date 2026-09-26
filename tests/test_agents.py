import asyncio
from datetime import datetime, timedelta

from optrader.agents.contract_selector import ContractSelector, round_to_tick, tick_size
from optrader.agents.features import build_features
from optrader.agents.strategies import MomentumAgent, ZeroDTEAgent
from optrader.brokers.paper import PaperBroker
from optrader.clock import ET
from optrader.data.simulated import SimulatedMarket
from optrader.models import Bar, Order, Signal

from .conftest import make_bars


def _features(prices, volume, now_min, daily=None, avg_vol=400_000):
    bars = make_bars(prices, volume=volume)
    now = bars[0].ts + timedelta(minutes=now_min)
    return build_features("TEST", now, bars, prev_close=prices[0], daily=daily or [], avg_daily_volume=avg_vol)


def test_orb_breakout_signal(settings):
    # quiet 15-minute range 100-101, steady uptrend, then a high-volume breakout
    prices = [100 + (i % 5) * 0.2 for i in range(15)] + [100.4 + i * 0.04 for i in range(12)] + [101.9]
    vols = [3000] * 27 + [9000]
    f = _features(prices, vols, len(prices))
    sigs = MomentumAgent(settings.strategies).evaluate(f)
    orb = [s for s in sigs if s.meta["setup"] == "orb"]
    assert orb, [s.meta for s in sigs]
    s = orb[0]
    assert s.direction == "bullish" and s.stop_underlying < s.underlying_price < s.target_underlying
    assert 0 <= s.score <= 100


def test_no_signal_in_flat_market(settings):
    prices = [100 + (0.05 if i % 2 else -0.05) for i in range(60)]
    f = _features(prices, 1000, 60)
    assert MomentumAgent(settings.strategies).evaluate(f) == []


def test_features_ignore_incomplete_bar():
    bars = make_bars([100, 101, 102])
    # at 09:32:30 only the 09:30 and 09:31 bars are complete
    f = build_features("X", bars[0].ts + timedelta(minutes=2, seconds=30), bars, 100, [], 1e6)
    assert len(f.session) == 2


def test_zero_dte_only_on_configured_symbols(settings):
    agent = ZeroDTEAgent(settings.strategies, ["SPY"], 0.35)
    prices = [100 + i * 0.02 for i in range(40)] + [101.5]
    f = _features(prices, [1000] * 40 + [5000], len(prices))
    assert agent.evaluate(f) == []  # symbol is TEST
    f.symbol = "SPY"
    sigs = agent.evaluate(f)
    assert sigs and all(s.zero_dte and s.max_dte == 0 for s in sigs)


def test_tick_rounding():
    assert tick_size("TSLA", 2.5) == 0.01
    assert tick_size("TSLA", 3.2) == 0.05
    assert tick_size("SPY", 7.0) == 0.01
    assert round_to_tick(3.21, 0.05, "up") == 3.25
    assert round_to_tick(3.24, 0.05, "down") == 3.20


def test_contract_selector_picks_liquid_target_delta(settings, clock):
    sim = SimulatedMarket(clock, ["SPY"], seed=1)

    async def run():
        spot = sim.spot("SPY")
        sig = Signal(ts=clock.now(), symbol="SPY", strategy="t", direction="bullish", score=80, underlying_price=spot,
                     min_dte=1, max_dte=7, target_delta=0.45)
        c, note = await ContractSelector(settings.contracts, sim).select(sig, clock.today())
        assert c is not None, note
        assert c.right == "C" and 0.3 < abs(c.delta) < 0.6 and c.spread_pct <= settings.contracts.max_spread_pct
        cheap, _ = await ContractSelector(settings.contracts, sim).select(sig, clock.today(), max_premium=1.0)
        assert cheap is None or cheap.mid <= 1.0
    asyncio.run(run())


def test_paper_broker_fill_model(settings, clock, tmp_path):
    sim = SimulatedMarket(clock, ["SPY"], seed=1)

    async def run():
        broker = PaperBroker(sim, clock, 10_000, tmp_path / "paper.json")
        exp = (await sim.expirations("SPY"))[1]
        c = (await sim.option_chain("SPY", exp, "C", sim.spot("SPY"), sim.spot("SPY") + 3))[0]
        now = clock.now()
        low = await broker.place_order(Order(contract=c, side="buy", qty=1, limit_price=round(c.bid, 2),
                                             created=now, updated=now))
        assert low.status == "submitted"  # bidding at the bid doesn't fill
        await broker.cancel_order(low)
        buy = await broker.place_order(Order(contract=c, side="buy", qty=2, limit_price=c.ask, created=now, updated=now))
        assert buy.status == "filled" and buy.avg_fill_price <= c.ask
        acct = await broker.account()
        assert acct.cash < 10_000
        too_many = await broker.place_order(Order(contract=c, side="sell", qty=5, limit_price=c.bid, created=now,
                                                  updated=now))
        assert too_many.status == "rejected"
        sell = await broker.place_order(Order(contract=c, side="sell", qty=2, limit_price=c.bid, created=now,
                                              updated=now))
        assert sell.status == "filled"
        assert not broker.holdings
        # persistence
        again = PaperBroker(sim, clock, 10_000, tmp_path / "paper.json")
        assert abs(again.cash - broker.cash) < 1e-9
        big = await broker.place_order(Order(contract=c, side="buy", qty=10_000, limit_price=c.ask, created=now,
                                             updated=now))
        assert big.status == "rejected" and "buying power" in big.message
    asyncio.run(run())


def test_simulated_market_is_deterministic(clock):
    a = SimulatedMarket(clock, ["TSLA"], seed=5)
    b = SimulatedMarket(clock, ["TSLA"], seed=5)
    assert a.spot("TSLA") == b.spot("TSLA")
    assert isinstance(a._sd("TSLA").bars[0], Bar)
    assert a._sd("TSLA").bars[0].ts == datetime(2026, 9, 24, 9, 30, tzinfo=ET)
