"""Read-only push bridge (aureon_mt5/push_bridge.py): body shape, scrubbing, cursor on 2xx only, size trim, token hygiene."""
import ast
import gzip
import json
import os
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pandas as pd
import pytest

from aureon_mt5 import push_bridge as pb
from aureon_mt5 import status_export as sx

TOKEN = "k" * 20 + "SECRETTOKENVALUE" + "z" * 12          # 48 chars, easy to grep for
NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _imports(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            out |= {(node.module or "")} | {a.name for a in node.names}
    return out


def test_bridge_never_touches_mt5_or_orders():
    imps = _imports("aureon_mt5/push_bridge.py")
    assert not imps & {"broker", "MetaTrader5", "halt", "actions", "agent", "auto_trade", "manual"}, imps
    src = open("aureon_mt5/push_bridge.py", encoding="utf-8").read()
    for word in ("order_send", "order_check", "positions_get", "place_market", "copy_rates", ".close(p", "set_halt",
                 "_move_sl", "modify", "cancel_order"):
        assert word not in src, word


# ----------------------------------------------------------------------------- fake server
class Ingest:
    def __init__(self, code=202):
        self.code = code
        self.bodies, self.headers = [], []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                outer.headers.append(dict(self.headers))
                outer.bodies.append(json.loads(raw))
                body = json.dumps({"accepted": True}).encode()
                self.send_response(outer.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/trading/mt5/ingest"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def ingest():
    srv = Ingest()
    yield srv
    srv.close()


def _df(n=400, t0=1_762_000_000):
    rows = [{"time": t0 + i * 300, "open": 2650.0 + i, "high": 2651.0 + i, "low": 2649.0 + i, "close": 2650.5 + i,
             "volume": 100 + i, "ema_fast": 2650.2 + i, "ema_slow": 2650.1 + i} for i in range(n)]
    return pd.DataFrame(rows)


class FakeAgent:
    def __init__(self, sym, n_bars=400):
        self.S = SimpleNamespace(name="ema5080", fast=50, slow=80, ema_cols=("ema_fast", "ema_slow"))
        self.g = SimpleNamespace(secure_level=10.0)
        self._resolved_profile = "classic"
        self.exec_tf = "M5"
        self.last_session = "london"
        self.snapshot = {"at": time.time(), "market_open": True, "connected": True, "mt5": "CONNECTED", "tick_age": 1.4,
                         "positions": [{"ticket": 101, "symbol": sym, "direction": "long", "price_open": 2650.5,
                                        "time": 1_762_010_000, "sl": 2640.0, "tp": 0.0, "volume": 0.1, "current": 2655.25,
                                        "comment": "", "magic": 0, "points": 4.75}],
                         "state": {101: {"mode": "ema5080", "secured": 12.0, "peak": 15.5, "pre": False, "bars": 7,
                                         "alert_id": "a1", "webhook_url": "https://discord/xyz"}},
                         "today": {"signals": 1}, "health": {"status": "ok"}}
        self.health = {"status": "ok", "offset_h": 2.0, "auth_token": "nope"}
        self.latest_signal = {"bar_time": 1, "symbol": sym, "kind": "cross", "direction": "LONG"}
        self.last_df = _df(n_bars)
        self._htf_frames_cache = (1, {"M15": _df(50), "H1": None})

    def is_alive(self):
        return True


def _journal(logs, rows):
    with open(logs / "journal.jsonl", "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _setup(tmp_path, url, **extra):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    now = int(time.time())
    _journal(logs, [
        {"t": now - 50, "event": "signal", "symbol": "XAUUSD", "side": "LONG", "signal_kind": "P", "bar": 1_762_010_100,
         "nested": {"password": "x", "keep": 1, "deep": [{"account_login": 5, "ok": True}]}},
        {"t": now - 40, "event": "console_call", "symbol": "XAUUSD", "token": "secret"},          # not allowlisted
        {"t": now - 30, "event": "protected", "symbol": "XAUUSD", "ticket": 101},
        {"t": now - 20, "event": "agent_crash", "symbol": "XAUUSD", "exc": "KeyError: 'x'", "trace": "Traceback ...\nKeyError"},
    ])
    env = {"AUREON_PUSH_ENABLED": "1", "AUREON_PUSH_URL": url, "MT5_INGEST_TOKEN": TOKEN,
           "AUREON_STATUS_BACKTEST_DIR": str(tmp_path / "out"), **extra}
    cfg = SimpleNamespace(log_dir=str(logs), mode="ema5080", symbols=["XAUUSD"], exec_tf="m5", dry=False,
                          server_utc_offset=3.0, htf_align="on", htf_confirm_tfs=("M15",), poll_seconds=10,
                          webhook="https://discord.com/api/webhooks/1/abc")
    return cfg, env, logs


def _bridge(cfg, env, logs, log=None):
    st = pb.Settings(env, log_dir=str(logs))
    base = sx.Settings(env, log_dir=str(logs))
    base.max_events = 0
    agents = {"XAUUSD": FakeAgent("XAUUSD")}
    return pb.PushBridge(st, lambda: sx.build_status(cfg, agents, settings=base), cfg=cfg, agents=agents,
                         notify=SimpleNamespace(last_ok=True), log=log or (lambda m: None), opener=NOPROXY)


def _walk_keys(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_keys(v)


# ----------------------------------------------------------------------------- tests
def test_disabled_by_default(tmp_path, ingest):
    cfg, env, logs = _setup(tmp_path, ingest.url)
    assert pb.start(cfg, {}, env={}, log=lambda m: None) is None
    assert pb.start(cfg, {}, env={**env, "AUREON_PUSH_ENABLED": "0"}, log=lambda m: None) is None
    assert ingest.bodies == []
    assert not os.path.exists(logs / "push_cursor.json")


def test_short_or_missing_token_refuses_to_start(tmp_path, ingest):
    cfg, env, _ = _setup(tmp_path, ingest.url)
    msgs = []
    assert pb.start(cfg, {}, env={**env, "MT5_INGEST_TOKEN": "short"}, log=msgs.append) is None
    assert pb.start(cfg, {}, env={**env, "MT5_INGEST_TOKEN": ""}, log=msgs.append) is None
    assert all("32" in m for m in msgs) and ingest.bodies == []


def test_settings_defaults_and_bounds():
    st = pb.Settings({"AUREON_PUSH_ENABLED": "1"})
    assert st.url == pb.DEFAULT_URL and st.interval_s == 15 and st.gzip and st.candles == 200
    st = pb.Settings({"AUREON_STATUS_INTERVAL_S": "30", "AUREON_PUSH_CANDLES": "9999", "AUREON_PUSH_GZIP": "0"})
    assert st.interval_s == 30 and st.candles == 300 and not st.gzip
    st = pb.Settings({"AUREON_PUSH_INTERVAL_S": "1", "MT5_INGEST_TOKEN": TOKEN})
    assert st.interval_s == 5 and TOKEN not in repr(st) and "SECRETTOKENVALUE" not in repr(st)


def test_body_shape_headers_and_gzip(tmp_path, ingest):
    cfg, env, logs = _setup(tmp_path, ingest.url)
    b = _bridge(cfg, env, logs)
    assert b.tick() is True
    doc, h = ingest.bodies[0], ingest.headers[0]
    assert h["Authorization"] == f"Bearer {TOKEN}" and h["Content-Encoding"] == "gzip"
    assert h["X-Aureon-Push-Id"] == doc["push_id"] and h["Content-Type"] == "application/json"
    assert doc["schema"] == "aureon.mt5.push/v1" and doc["seq"] == 1
    for k in ("generated_at", "generated_ts", "bot", "agents", "halt", "console", "ema921e", "ema921e_trades",
              "backtests", "errors", "health", "guardian", "signals_latest", "events", "tracebacks", "strategy", "candles"):
        assert k in doc, k
    assert doc["health"]["status"] == "ok" and doc["health"]["mt5"]["connected"] is True
    assert doc["health"]["discord"]["ok"] is True and doc["health"]["market"]["XAUUSD"]["open"] is True
    g = doc["guardian"]["XAUUSD"][0]
    assert g["ticket"] == 101 and g["phase"] == "ride" and g["sl"] == 2640.0
    assert g["secured_level"] == pytest.approx(2662.5) and g["peak"] == pytest.approx(2666.0) and g["direction"] == "long"
    assert [e["event"] for e in doc["events"]] == ["signal", "protected", "agent_crash"]
    assert all(len(e["id"]) == 40 for e in doc["events"]) and "trace" not in doc["events"][2]
    assert doc["tracebacks"][0]["where"] == "agent:XAUUSD" and doc["tracebacks"][0]["type"] == "KeyError"
    assert set(doc["signals_latest"]["XAUUSD"]) >= {"P", "protected", "error"}
    s = doc["strategy"]
    assert s["mode"] == "ema5080" and s["timeframes"] == ["M5", "M15"] and s["ema"] == {"fast": 50, "slow": 80}
    c = doc["candles"]["XAUUSD"]
    assert len(c["M5"]) == 200 and len(c["M15"]) == 50 and "H1" not in c
    assert set(c["M5"][-1]) == {"t", "o", "h", "l", "c", "v", "ema"} and set(c["M5"][-1]["ema"]) == {"50", "80"}


def test_plain_json_when_gzip_off(tmp_path, ingest):
    cfg, env, logs = _setup(tmp_path, ingest.url, AUREON_PUSH_GZIP="0")
    assert _bridge(cfg, env, logs).tick()
    assert "Content-Encoding" not in ingest.headers[0]


def test_secret_keys_scrubbed_at_every_depth(tmp_path, ingest):
    cfg, env, logs = _setup(tmp_path, ingest.url)
    _bridge(cfg, env, logs).tick()
    doc = ingest.bodies[0]
    bad = [k for k in _walk_keys(doc) if any(p in str(k).lower() for p in
                                             ("token", "auth", "password", "secret", "webhook", "url", "login", "account"))]
    assert bad == []
    raw = json.dumps(doc)
    assert TOKEN not in raw and "discord/xyz" not in raw
    ev = doc["events"][0]
    assert ev["nested"] == {"keep": 1, "deep": [{"ok": True}]}


def test_cursor_advances_only_on_2xx(tmp_path, ingest):
    cfg, env, logs = _setup(tmp_path, ingest.url)
    b = _bridge(cfg, env, logs)
    ingest.code = 500
    assert b.tick() is False and b.cursor is None
    assert not os.path.exists(logs / "push_cursor.json")
    ingest.code = 401
    assert b.tick() is False
    first_ids = [e["id"] for e in ingest.bodies[0]["events"]]
    assert [e["id"] for e in ingest.bodies[1]["events"]] == first_ids          # same events, same stable ids
    assert ingest.bodies[0]["push_id"] != ingest.bodies[1]["push_id"] and ingest.bodies[1]["seq"] == 2
    ingest.code = 202
    assert b.tick() is True and [e["id"] for e in ingest.bodies[2]["events"]] == first_ids
    assert json.load(open(logs / "push_cursor.json"))["journal_offset"] == os.path.getsize(logs / "journal.jsonl")
    assert b.tick() is True and ingest.bodies[3]["events"] == [] and ingest.bodies[3]["tracebacks"] == []
    _journal(logs, [{"t": int(time.time()), "event": "exit", "symbol": "XAUUSD", "ticket": 101}])
    ingest.code = 200                                                          # {"duplicate": true} is also an ack
    assert b.tick() is True and [e["event"] for e in ingest.bodies[4]["events"]] == ["exit"]
    # a new process resumes from the persisted cursor
    b2 = _bridge(cfg, env, logs)
    assert b2.tick() is True and ingest.bodies[5]["events"] == []


def test_unreachable_server_never_raises(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg, env, logs = _setup(tmp_path, f"http://127.0.0.1:{port}/ingest")
    msgs = []
    b = _bridge(cfg, env, logs, log=msgs.append)
    assert b.tick() is False and b.tick() is False
    assert len(msgs) == 1 and "failed" in msgs[0]                                # once a minute, not every tick
    assert b.cursor is None


def test_token_never_in_logs_or_exceptions(tmp_path, ingest, capsys):
    cfg, env, logs = _setup(tmp_path, ingest.url)
    msgs = []
    b = _bridge(cfg, env, logs, log=msgs.append)

    def boom(req, timeout=None):
        raise RuntimeError(f"connection reset while sending {req.get_header('Authorization')}")
    b.opener = SimpleNamespace(open=boom)
    assert b.tick() is False
    ingest.code = 401
    b.opener = NOPROXY
    b._last_err_log = 0
    assert b.tick() is False
    out = capsys.readouterr()
    text = "\n".join(msgs) + out.out + out.err + repr(b.settings)
    assert msgs and "SECRETTOKENVALUE" not in text and TOKEN not in text and "***" in msgs[0]
    # the full start path logs the URL but never the token
    log2 = []
    br = pb.start(cfg, {"XAUUSD": FakeAgent("XAUUSD")}, env=env, log=log2.append, opener=NOPROXY)
    try:
        assert br is not None and log2 and all("SECRETTOKENVALUE" not in m for m in log2)
    finally:
        br.stop()


def test_size_trim_candles_first_then_events(tmp_path, ingest, monkeypatch):
    cfg, env, logs = _setup(tmp_path, ingest.url)
    b = _bridge(cfg, env, logs)
    doc, cut, _ = b.build()
    full = b._size(doc)
    # 1) a cap just under the full body: candles are trimmed, every event stays
    monkeypatch.setattr(pb, "MAX_BODY", full - 100)
    doc, cut, _ = b.build()
    assert len(doc["events"]) == 3 and len(doc["candles"]["XAUUSD"]["M5"]) < 200 and "candles" in doc["errors"]
    assert cut == os.path.getsize(logs / "journal.jsonl")
    # 2) many large events: candles gone, newest events held back, the cursor stops before them
    now = int(time.time())
    _journal(logs, [{"t": now, "event": "signal", "symbol": "XAUUSD", "signal_kind": "cross", "pad": "x" * 2000, "n": i}
                    for i in range(400)])
    monkeypatch.setattr(pb, "MAX_BODY", 200_000)
    assert b.tick() is True
    body = ingest.bodies[-1]
    sent = len(body["events"])
    assert body["candles"] == {} and 0 < sent < 403 and "events" in body["errors"]
    assert body["events"][0]["event"] == "signal" and body["events"][0].get("signal_kind") == "P"   # oldest first
    assert b.cursor < os.path.getsize(logs / "journal.jsonl")
    total = sent
    for _ in range(20):
        if b.cursor >= os.path.getsize(logs / "journal.jsonl"):
            break
        assert b.tick() is True
        total += len(ingest.bodies[-1]["events"])
    assert total == 403                                                          # nothing lost, nothing repeated
    ids = [e["id"] for bd in ingest.bodies for e in bd["events"]]
    assert len(ids) == len(set(ids))


def test_health_degrades_and_errors():
    doc = {"agents": {"XAUUSD": {"connected": True, "mt5": "CONNECTED", "market_open": True, "tick_age": 500.0,
                                 "alive": True, "offset_h": 2.0, "health": {"status": "running"}}}, "errors": {}}
    h = pb.health_section(doc, {}, SimpleNamespace(last_ok=True))
    assert h["status"] == "degraded" and h["market"]["XAUUSD"]["session"] in ("asia", "london", "ny", "off")
    doc["agents"]["XAUUSD"]["connected"] = False
    assert pb.health_section(doc, {}, None)["status"] == "error"
    doc["agents"]["XAUUSD"].update(connected=True, tick_age=1.0)
    h = pb.health_section(doc, {}, SimpleNamespace(last_ok=False))
    assert h["status"] == "degraded" and h["discord"]["ok"] is False and h["discord"]["last_error"]


def test_recent_failures_become_tracebacks(tmp_path, ingest, monkeypatch):
    from aureon_mt5 import telemetry
    cfg, env, logs = _setup(tmp_path, ingest.url)
    b = _bridge(cfg, env, logs)
    monkeypatch.setattr(telemetry, "RECENT", __import__("collections").deque([
        {"t": time.time() + 1, "title": "AUREON GUARDIAN FAILURE", "key": "ema5080:XAUUSD:ERROR:101", "action": "move SL",
         "exc_type": "ValueError", "exc": "ValueError('bad')", "tb": "Traceback\n" + "y" * 20000}]))
    assert b.tick()
    tbs = ingest.bodies[-1]["tracebacks"]
    g = [t for t in tbs if t["where"] == "guardian"][0]
    assert g["type"] == "ValueError" and len(g["traceback"]) <= pb.MAX_TB_TEXT
    assert b.tick() and ingest.bodies[-1]["tracebacks"] == []                     # acked → not sent again


def test_main_hook_is_guarded():
    src = open("main.py", encoding="utf-8").read()
    assert 'os.environ.get("AUREON_PUSH_ENABLED", "0").strip() == "1"' in src
    assert "push_bridge as _pb" in src
