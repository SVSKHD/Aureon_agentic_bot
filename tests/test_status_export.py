"""Read-only status export (aureon_mt5/status_export.py): no MT5, no writes besides logs/status.json, GET-only HTTP."""
import ast
import json
import os
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import socket

import pytest

from aureon_mt5 import status_export as sx

TOKEN = "t" * 40
NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # loopback never goes through a proxy


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _imports(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            out |= {(node.module or "")} | {a.name for a in node.names}
    return out


def test_export_never_touches_mt5_or_orders():
    imps = _imports("aureon_mt5/status_export.py")
    assert not imps & {"broker", "MetaTrader5", "halt", "actions", "agent"}, imps
    src = open("aureon_mt5/status_export.py", encoding="utf-8").read()
    for word in ("order_send", "positions_get", "place_market", ".close(p", "set_halt"):
        assert word not in src


class FakeAgent:
    def __init__(self, sym):
        self.S = SimpleNamespace(name="ema5080")
        self.exec_tf = "M5"
        self.snapshot = {"at": 1_762_000_000.0, "market_open": True, "connected": True, "mt5": "CONNECTED", "tick_age": 2.0,
                         "positions": [{"ticket": 101, "symbol": sym, "direction": "long", "price_open": 2650.5, "time": 1_762_010_000,
                                        "sl": 2640.0, "tp": 0.0, "volume": 0.1, "current": 2655.25, "comment": "", "magic": 0,
                                        "points": 4.75}],
                         "state": {101: {"mode": "ema5080"}}, "today": {"signals": 1, "secured": 0.0, "errors": 0, "closed": 0},
                         "health": {"status": "ok"}}
        self.health = {"status": "ok", "last_bar": 1_762_010_100, "offset_h": 2.0, "errors": 0}
        self.latest_signal = {"bar_time": 1_762_010_100, "symbol": sym, "strategy": "ema5080", "kind": "cross",
                              "direction": "LONG", "consumed": False}

    def is_alive(self):
        return True


def _setup(tmp_path):
    logs = tmp_path / "logs"; logs.mkdir()
    now = int(time.time())
    events = [
        {"t": now - 100, "event": "signal", "symbol": "XAUUSD", "mode": "ema5080", "side": "LONG", "signal_kind": "cross",
         "price": 2650.1, "bar": 1_762_010_100},
        {"t": now - 90, "event": "console_call", "symbol": "XAUUSD", "token": "secret"},       # not allowlisted
        {"t": now - 80, "event": "exit", "symbol": "XAUUSD", "mode": "ema5080", "ticket": 101, "points": 12.5,
         "reason": "TP", "webhook": "https://discord/xyz"},
        {"t": now - 99999999, "event": "signal", "symbol": "XAUUSD"},                            # too old
    ]
    with open(logs / "journal.jsonl", "w", encoding="utf-8") as f:
        f.write("garbage line\n")
        for e in events:
            f.write(json.dumps(e) + "\n")
    (logs / "halt.json").write_text(json.dumps({"on": True, "by": "admin", "reason": "news", "t": now, "ist": "12:00"}))
    (logs / "ema921e_trades.csv").write_text("symbol,time,dir,entry,pnl_usd\nXAUUSD,1,LONG,2600.5,12.30\nXAUUSD,2,SHORT,2610.0,-3.10\n")
    out = tmp_path / "out"; out.mkdir()
    (out / "XAUUSD_jan_ema5080.json").write_text(json.dumps({"mode": "ema5080", "symbol": "XAUUSD",
                                                             "summary": {"trades": 10, "wins": 6, "win_rate": 0.6, "net": 55.5, "avg": 5.55},
                                                             "journeys": [{"big": "x" * 1000}]}))
    (out / "broken.json").write_text("{nope")
    env = {"AUREON_STATUS_EXPORT": "1", "AUREON_STATUS_BACKTEST_DIR": str(out), "AUREON_STATUS_TOKEN": TOKEN,
           "AUREON_STATUS_BIND": f"127.0.0.1:{_free_port()}"}
    cfg = SimpleNamespace(log_dir=str(logs), mode="ema5080", symbols=["XAUUSD"], exec_tf="m5", dry=False, server_utc_offset=3.0)
    return cfg, env


def test_build_status_document(tmp_path):
    cfg, env = _setup(tmp_path)
    st = sx.Settings(env, log_dir=cfg.log_dir)
    doc = sx.build_status(cfg, {"XAUUSD": FakeAgent("XAUUSD")}, settings=st, backtests=sx.BacktestCache(st.backtest_dir),
                          state_fn=lambda: {"v": 1, "regime": {"XAUUSD": {"state": "TREND"}}, "pending_ask": {"key": "k"},
                                            "session": {"in_session": True}},
                          started=time.time() - 60)
    assert doc["schema"] == "aureon.mt5.status/v1" and doc["errors"] == {}
    a = doc["agents"]["XAUUSD"]
    assert a["positions"][0]["ticket"] == 101 and a["offset_h"] == 2.0 and a["latest_signal"]["direction"] == "LONG"
    assert [e["event"] for e in doc["events"]] == ["signal", "exit"]
    assert "webhook" not in doc["events"][1]
    assert doc["halt"]["reason"] == "news"
    assert "pending_ask" not in doc["console"] and doc["console"]["regime"]["XAUUSD"]["state"] == "TREND"
    assert [r["pnl_usd"] for r in doc["ema921e_trades"]] == ["12.30", "-3.10"]
    assert doc["backtests"] == [{"file": "XAUUSD_jan_ema5080.json", "mode": "ema5080", "symbol": "XAUUSD",
                                 "summary": {"trades": 10, "wins": 6, "win_rate": 0.6, "net": 55.5, "avg": 5.55},
                                 "modified_at": doc["backtests"][0]["modified_at"]}]
    json.dumps(doc, default=str)


def test_a_failing_section_is_reported_not_raised(tmp_path):
    cfg, env = _setup(tmp_path)
    st = sx.Settings(env, log_dir=cfg.log_dir)

    def boom():
        raise RuntimeError("console broke")
    doc = sx.build_status(cfg, {}, settings=st, state_fn=boom)
    assert doc["console"] is None and "console broke" in doc["errors"]["console"]


def test_disabled_by_default(tmp_path):
    cfg, _ = _setup(tmp_path)
    assert sx.start(cfg, {}, env={}, log=lambda m: None) == (None, None)
    assert not os.path.exists(os.path.join(cfg.log_dir, "status.json"))


def test_non_loopback_bind_refused_without_allow_public():
    st = sx.Settings({"AUREON_STATUS_TOKEN": TOKEN, "AUREON_STATUS_BIND": "0.0.0.0:8788"})
    assert "not loopback" in sx.http_bind(st)
    st = sx.Settings({"AUREON_STATUS_TOKEN": "short", "AUREON_STATUS_BIND": "127.0.0.1:8788"})
    assert "32+" in sx.http_bind(st)


def test_settings_tolerate_garbage():
    st = sx.Settings({"AUREON_STATUS_INTERVAL_S": "abc", "AUREON_STATUS_EVENTS": "999999"})
    assert st.interval_s == 15 and st.max_events == 5000


def test_file_and_http(tmp_path):
    cfg, env = _setup(tmp_path)
    exp, srv = sx.start(cfg, {"XAUUSD": FakeAgent("XAUUSD")}, env=env, log=lambda m: None)
    try:
        assert srv is not None
        with open(os.path.join(cfg.log_dir, "status.json"), encoding="utf-8") as f:
            assert json.load(f)["schema"] == sx.SCHEMA
        port = srv.httpd.server_address[1]
        url = f"http://127.0.0.1:{port}/status"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
        with NOPROXY.open(req, timeout=5) as r:
            assert json.loads(r.read())["agents"]["XAUUSD"]["mt5"] == "CONNECTED"
        for headers, method, code in (({}, "GET", 401), ({"Authorization": "Bearer " + "x" * 40}, "GET", 401),
                                      ({"Authorization": f"Bearer {TOKEN}"}, "POST", 405),
                                      ({"Authorization": f"Bearer {TOKEN}"}, "DELETE", 405)):
            with pytest.raises(urllib.error.HTTPError) as ei:
                NOPROXY.open(urllib.request.Request(url, headers=headers, method=method, data=b"" if method == "POST" else None), timeout=5)
            assert ei.value.code == code
        with pytest.raises(urllib.error.HTTPError) as ei:
            NOPROXY.open(urllib.request.Request(f"http://127.0.0.1:{port}/console/action",
                                                          headers={"Authorization": f"Bearer {TOKEN}"}), timeout=5)
        assert ei.value.code == 404
    finally:
        exp.stop()
        if srv is not None:
            srv.stop()


def test_main_hook_is_guarded():
    src = open("main.py", encoding="utf-8").read()
    assert 'os.environ.get("AUREON_STATUS_EXPORT", "0").strip() == "1"' in src
    assert "status_export as _sx" in src
