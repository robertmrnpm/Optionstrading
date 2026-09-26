"""Webull adapter tests with canned payloads (no network)."""
from datetime import date

from optrader.brokers.webull_broker import WebullBroker
from optrader.data.webull_data import WebullData
from optrader.models import OptionContract, Order
from optrader.webull_client import find_dicts, parse_date, parse_time, pick


def test_helpers():
    assert pick({"a": None, "b": "", "c": 3}, "a", "b", "c") == 3
    assert parse_time(1758720600000).year == 2025
    assert parse_time("2026-09-24T13:30:00Z").hour == 13
    assert parse_date("2026-10-02") == date(2026, 10, 2)
    assert parse_date("20261002") == date(2026, 10, 2)
    assert len(find_dicts({"x": [{"symbol": "A"}, {"y": {"symbol": "B"}}]}, ["symbol"])) == 2


def test_parse_batch_bars():
    wd = WebullData.__new__(WebullData)
    payload = [{"symbol": "SPY", "result": [
        {"time": "2026-09-24T13:31:00Z", "open": "660.1", "high": "660.5", "low": "659.9", "close": "660.4", "volume": "1000"},
        {"time": "2026-09-24T13:30:00Z", "open": "660.0", "high": "660.2", "low": "659.8", "close": "660.1", "volume": "900"},
    ]}]
    groups = wd._parse_bars(payload)
    bars = groups["SPY"]
    assert len(bars) == 2 and bars[0].ts < bars[1].ts
    assert bars[0].ts.hour == 9 and bars[0].ts.minute == 30  # converted to ET
    assert bars[1].close == 660.4


def test_contract_row_parsing():
    wd = WebullData.__new__(WebullData)
    c = wd._row_to_contract("AAPL", {"symbol": "AAPL261002C00250000", "strike_price": "250",
                                     "option_type": "CALL", "expiration_date": "2026-10-02"})
    assert c.right == "C" and c.strike == 250 and c.expiry == date(2026, 10, 2)
    c2 = wd._row_to_contract("AAPL", {"symbol": "AAPL", "strike_price": "245.5", "option_type": "PUT",
                                      "option_expire_date": "2026-10-02"})
    assert c2.symbol == "AAPL261002P00245500"


def test_option_order_payload_matches_sdk_sample():
    wb = WebullBroker.__new__(WebullBroker)
    c = OptionContract(symbol="TSLA261002C00400000", underlying="TSLA", expiry=date(2026, 10, 2), strike=400,
                       right="C")
    from datetime import datetime
    now = datetime(2026, 9, 24, 10, 0)
    buy = wb._order_payload(Order(contract=c, side="buy", qty=2, limit_price=3.25, created=now, updated=now))
    assert buy["order_type"] == "LIMIT" and buy["option_strategy"] == "SINGLE" and buy["combo_type"] == "NORMAL"
    assert buy["position_intent"] == "BUY_TO_OPEN" and buy["time_in_force"] == "DAY"
    leg = buy["legs"][0]
    assert leg == {"side": "BUY", "quantity": "2", "symbol": "TSLA", "strike_price": "400",
                   "option_expire_date": "2026-10-02", "instrument_type": "OPTION", "option_type": "CALL",
                   "market": "US"}
    sell = wb._order_payload(Order(contract=c, side="sell", qty=1, limit_price=4.1, created=now, updated=now))
    assert sell["side"] == "SELL" and sell["position_intent"] == "SELL_TO_CLOSE" and sell["time_in_force"] == "DAY"
    assert sell["limit_price"] == "4.10"
