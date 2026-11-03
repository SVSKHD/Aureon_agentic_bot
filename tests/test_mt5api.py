"""aureon_mt5.mt5api: the native / mt5linux switch, against a real rpyc classic server in-process
(the same server mt5linux runs under Wine) with a stand-in MetaTrader5 module."""

from __future__ import annotations

import collections
import datetime as dt
import sys
import threading
import types

import numpy as np
import pytest

rpyc = pytest.importorskip("rpyc")
from rpyc.core import SlaveService  # noqa: E402
from rpyc.utils.server import ThreadedServer  # noqa: E402

from aureon_mt5 import mt5api  # noqa: E402

TradePosition = collections.namedtuple("TradePosition", "ticket symbol type volume price_open sl tp")
TradeRequest = collections.namedtuple("TradeRequest", "action symbol volume price sl tp")
OrderSendResult = collections.namedtuple("OrderSendResult", "retcode order comment request")


@pytest.fixture()
def fake_mt5(monkeypatch):
    m = types.ModuleType("MetaTrader5")
    m.TIMEFRAME_M5, m.ORDER_TYPE_BUY, m.TRADE_RETCODE_DONE, m.__version__ = 5, 0, 10009, "fake"
    m.sent = []
    m.seen_args = []

    def positions_get(symbol=None):
        if symbol == "NONE":
            return None
        return (TradePosition(101, "XAUUSD", 0, 0.1, 2650.5, 2640.0, 0.0),)

    def order_send(request):
        m.sent.append(request)
        req = TradeRequest(request["action"], request["symbol"], request["volume"], request["price"],
                           request["sl"], request["tp"])
        return OrderSendResult(10009, 555, "done", req)

    def copy_rates_from_pos(symbol, tf, start, count):
        a = np.zeros(count, dtype=[("time", "<i8"), ("close", "<f8")])
        a["time"] = np.arange(count) * 300
        a["close"] = 2650.0 + np.arange(count)
        return a

    def copy_rates_range(symbol, tf, date_from, date_to):
        m.seen_args.append((type(date_from).__name__, date_from.isoformat()))
        return copy_rates_from_pos(symbol, tf, 0, 2)

    m.positions_get, m.order_send = positions_get, order_send
    m.copy_rates_from_pos, m.copy_rates_range = copy_rates_from_pos, copy_rates_range
    m.initialize = lambda *a, **k: True
    m.last_error = lambda: (1, "Success")
    monkeypatch.setitem(sys.modules, "MetaTrader5", m)
    return m


@pytest.fixture()
def server(fake_mt5):
    srv = ThreadedServer(SlaveService, hostname="127.0.0.1", port=0,
                         protocol_config={"allow_all_attrs": True, "allow_pickle": True})
    t = threading.Thread(target=srv.start, daemon=True)
    t.start()
    yield srv
    srv.close()


def remote(server) -> mt5api.Mt5Remote:
    return mt5api.Mt5Remote("127.0.0.1", server.port)


def test_constants_and_named_tuples(server):
    mt5 = remote(server)
    assert mt5.TIMEFRAME_M5 == 5 and mt5.ORDER_TYPE_BUY == 0
    assert mt5.initialize() is True and mt5.last_error() == (1, "Success")
    (pos,) = mt5.positions_get(symbol="XAUUSD")
    assert isinstance(pos, tuple) and type(pos).__name__ == "TradePosition"
    assert pos.ticket == 101 and pos.sl == 2640.0
    assert pos._asdict()["symbol"] == "XAUUSD"
    assert mt5.positions_get(symbol="NONE") is None


def test_results_are_local_not_remote_references(server):
    import json

    mt5 = remote(server)
    (pos,) = mt5.positions_get()
    assert not isinstance(pos, rpyc.core.netref.BaseNetref)
    json.dumps(pos._asdict())  # state files and the status export can serialise it


def test_numpy_prices_and_nested_results(server, fake_mt5):
    mt5 = remote(server)
    req = {"action": 1, "symbol": "XAUUSD", "volume": np.float64(0.1), "price": np.float64(2650.5),
           "sl": np.float32(2640.0), "tp": 0.0}
    r = mt5.order_send(req)
    assert r.retcode == 10009 and r.request.price == 2650.5 and type(r.request).__name__ == "TradeRequest"
    assert len(fake_mt5.sent) == 1
    sent = fake_mt5.sent[0]
    assert all(type(sent[k]) is float for k in ("volume", "price", "sl", "tp")), sent


def test_arrays_and_datetimes(server, fake_mt5):
    mt5 = remote(server)
    a = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M5, 0, 3)
    assert isinstance(a, np.ndarray) and a["close"].tolist() == [2650.0, 2651.0, 2652.0]
    t0 = dt.datetime(2026, 11, 3, 9, 0, tzinfo=dt.timezone.utc)
    b = mt5.copy_rates_range("XAUUSD", 5, t0, t0 + dt.timedelta(hours=1))
    assert len(b) == 2
    assert fake_mt5.seen_args[-1] == ("datetime", "2026-11-03T09:00:00+00:00")


def test_dropped_connection_raises_once_never_retries_then_reconnects(server, fake_mt5):
    mt5 = remote(server)
    mt5._conn.close()  # the server restarted / the socket died
    req = {"action": 1, "symbol": "XAUUSD", "volume": 0.1, "price": 1.0, "sl": 0.0, "tp": 0.0}
    with pytest.raises(ConnectionError):
        mt5.order_send(req)
    assert fake_mt5.sent == []  # not retried behind the caller's back
    assert mt5.order_send(req).retcode == 10009  # the next call reconnects
    assert len(fake_mt5.sent) == 1


def test_unreachable_server_is_a_connection_error(server):
    port = server.port
    server.close()
    with pytest.raises(OSError):  # the constructor: broker.connect() catches it and retries later
        mt5api.Mt5Remote("127.0.0.1", port)


def test_backend_selection(monkeypatch, server, fake_mt5):
    mt5api.reset()
    monkeypatch.delenv("AUREON_MT5_BACKEND", raising=False)
    assert mt5api.load() is fake_mt5  # native: the module itself
    mt5api.reset()
    monkeypatch.setenv("AUREON_MT5_BACKEND", "mt5linux")
    monkeypatch.setenv("AUREON_MT5LINUX_PORT", str(server.port))
    api = mt5api.load()
    assert isinstance(api, mt5api.Mt5Remote) and api is mt5api.load()
    mt5api.reset()
    monkeypatch.setenv("AUREON_MT5_BACKEND", "wine-magic")
    with pytest.raises(ValueError):
        mt5api.load()
    mt5api.reset()


def test_broker_connect_uses_the_backend(monkeypatch, server):
    from aureon_mt5 import broker

    mt5api.reset()
    monkeypatch.setenv("AUREON_MT5_BACKEND", "mt5linux")
    monkeypatch.setenv("AUREON_MT5LINUX_PORT", str(server.port))
    old = broker._m
    broker._m = None
    try:
        assert broker.connect() is True
        assert isinstance(broker.mt5(), mt5api.Mt5Remote)
    finally:
        broker._m = old
        mt5api.reset()
