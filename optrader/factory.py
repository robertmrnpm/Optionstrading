"""Builds the clock / data provider / broker combination for the configured mode."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .agents.context import Context
from .brokers.paper import PaperBroker
from .clock import ET, MARKET_OPEN, RealClock, SimClock, is_trading_day
from .config import Settings
from .db import Database
from .events import EventBus
from .notify import Notifier
from .orchestrator import Orchestrator

log = logging.getLogger(__name__)


def _sim_start() -> datetime:
    d = datetime.now(ET).date()
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return datetime.combine(d, MARKET_OPEN, tzinfo=ET) - timedelta(minutes=5)


def _real_data(settings: Settings, clock):
    if settings.app.data_source == "webull":
        from .data.webull_data import WebullData
        from .webull_client import make_api_client
        api = make_api_client(settings.webull_app_key, settings.webull_app_secret, settings.webull_region,
                              settings.data_path / "webull_token")
        return WebullData(clock, api), api
    from .data.yahoo_data import YahooData
    return YahooData(clock), None


def build(settings: Settings) -> Orchestrator:
    mode = settings.app.mode
    db = Database(settings.data_path / f"journal_{mode}.db")
    if mode == "sim":
        from .data.simulated import SimulatedMarket
        clock = SimClock(_sim_start(), settings.app.sim_speed)
        data = SimulatedMarket(clock, settings.scanner.core_watchlist, settings.app.sim_seed)
        broker = PaperBroker(data, clock, settings.risk.starting_equity, settings.data_path / "paper_sim.json",
                             cash_account=settings.risk.account_type == "cash")
    else:
        clock = RealClock()
        data, api = _real_data(settings, clock)
        if mode == "live":
            from .brokers.webull_broker import WebullBroker
            from .webull_client import make_api_client
            api = api or make_api_client(settings.webull_app_key, settings.webull_app_secret,
                                         settings.webull_region, settings.data_path / "webull_token")
            broker = WebullBroker(api, clock, settings.webull_account_id)
        else:
            broker = PaperBroker(data, clock, settings.risk.starting_equity, settings.data_path / "paper_account.json",
                                 cash_account=settings.risk.account_type == "cash")
    ctx = Context(settings=settings, clock=clock, data=data, broker=broker, db=db, bus=EventBus(db, clock),
                  notifier=Notifier())
    return Orchestrator(ctx)
