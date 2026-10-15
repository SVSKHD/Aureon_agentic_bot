"""v2.11.0 Part B — the action console: auth (console token + scoped action tokens), rate limit, journal, shared actions,
/console/state contract, charts, bind safety, never touches MT5."""
from __future__ import annotations

import ast
import os
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from aureon_mt5 import config as C  # noqa: E402
from aureon_mt5 import console as K  # noqa: E402
from aureon_mt5.actions import Actions  # noqa: E402
from aureon_mt5.notify import Notifier  # noqa: E402
from aureon_mt5.push import ActionTokens  # noqa: E402

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class J:
    def __init__(self): self.rows = []

    def log(self, ev, **kw): self.rows.append((ev, kw))

    def decision(self, ev, **kw): self.rows.append((ev, kw))


class Tracker:
    def __init__(self, state="TRENDING", reasons=()):
        self.state, self.score, self.reasons = state, 0.7, list(reasons)

    def blocks(self): return self.state in ("CHOPPY", "DEAD")


def make(tmp_path, *, execution=True, regime="TRENDING", ask_age=0.0, positions=None, png=None):
    cfg = SimpleNamespace(console_token=TOKEN, log_dir=str(tmp_path), ask_ttl_min=20, symbols=["XAUUSD"],
                          execution_enabled=execution, dry=False, session_ist="05:30-23:00", regime_gate="block",
                          push=("NTFY",), console_bind="127.0.0.1:8787", console_allow_public=False)
    ag = SimpleNamespace(regime_tracker=Tracker(regime, ["crosses 4"] if regime != "TRENDING" else []), closed_by_aureon=set(),
                         snapshot={"positions": positions or []}, health={"status": "ok", "last_bar": "12:35"},
                         decided=[], mark_ask_decided=lambda k, d, placed=False: ag.decided.append((k, d)))
    n = Notifier(None)
    n.ask("ema5080:XAUUSD:P_LONG:1", "XAUUSD · ENTER LONG", ["x"], png=png,
          meta={"symbol": "XAUUSD", "side": "LONG", "kind": "P", "price": 4210.5})
    n.open_asks["ema5080:XAUUSD:P_LONG:1"]["t"] = time.time() - ask_age
    j = J(); executed, closed, reads = [], [], []
    acts = Actions(cfg, {"XAUUSD": ag}, j, n,
                   executor=lambda item, d, user, lots: executed.append((item["key"], d, str(user), lots)) or
                   {"ok": True, "line": "TAKEN LONG 0.1 lots", "text": "placed", "placed": True, "ticket": 55},
                   closer=lambda syms: closed.append(syms) or [{"ticket": 9, "ok": True, "points": 3.2}],
                   reader=lambda a: reads.append(a) or {"verdict": {"decision": "HOLD", "confidence": 0.7}, "snapshot": {}})
    tokens = ActionTokens(TOKEN)
    app = K.create_app(cfg, acts, j, tokens=tokens, chart_dir=os.path.join(str(tmp_path), "charts"),
                       state_fn=lambda: K.console_state(cfg, {"XAUUSD": ag}, acts, n, started=time.time() - 60))
    return SimpleNamespace(cfg=cfg, ag=ag, n=n, j=j, acts=acts, tokens=tokens, client=TestClient(app), executed=executed,
                           closed=closed, reads=reads)


KEY = "ema5080:XAUUSD:P_LONG:1"


# ----------------------------------------------------------------------------- auth + journal
def test_action_requires_a_bearer(tmp_path):
    r = make(tmp_path)
    assert r.client.post("/console/action", json={"action": "take"}).status_code == 401
    assert r.client.post("/console/action", json={"action": "take"}, headers={"Authorization": "Bearer " + "x" * 40}).status_code == 401
    assert r.executed == []
    calls = [kw for ev, kw in r.j.rows if ev == "console_call"]
    assert calls and all(kw["source"] == "push" for kw in calls) and {kw["status"] for kw in calls} == {401}


def test_take_with_console_token_uses_the_shared_path(tmp_path):
    r = make(tmp_path)
    res = r.client.post("/console/action", json={"action": "take", "symbol": "XAUUSD", "ask_key": KEY}, headers=AUTH)
    assert res.status_code == 200 and res.json()["ok"] is True and res.json()["ticket"] == 55
    assert r.executed == [(KEY, "TAKE", "console:push", None)] and r.ag.decided == [(KEY, "TAKE")]
    assert [kw for ev, kw in r.j.rows if ev == "console_call"][-1]["ok"] is True


def test_scoped_action_token_works_once(tmp_path):
    r = make(tmp_path)
    tok = r.tokens.issue("skip", "XAUUSD", KEY)
    h = {"Authorization": f"Bearer {tok}"}
    body = {"action": "skip", "symbol": "XAUUSD", "ask_key": KEY}
    assert r.client.post("/console/action", json={**body, "action": "take"}, headers=h).status_code == 401   # wrong action
    assert r.client.post("/console/action", json=body, headers=h).status_code == 200
    assert r.n.open_asks[KEY]["decided"] == "SKIP"
    assert r.client.post("/console/action", json=body, headers=h).status_code == 401                          # burnt
    assert r.client.get("/console/state", headers=h).status_code == 401                                       # actions only


def test_rate_limit_20_per_minute(tmp_path):
    r = make(tmp_path)
    codes = [r.client.get("/console/state", headers=AUTH).status_code for _ in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429


# ----------------------------------------------------------------------------- refusals
def test_take_refused_when_stale(tmp_path):
    r = make(tmp_path, ask_age=21 * 60)
    res = r.client.post("/console/action", json={"action": "take", "ask_key": KEY}, headers=AUTH)
    assert res.status_code == 409 and res.json()["reasons"][0].startswith("stale: asked 21 min ago")
    assert r.executed == []


def test_take_refused_when_regime_blocks_with_reasons(tmp_path):
    r = make(tmp_path, regime="CHOPPY")
    res = r.client.post("/console/action", json={"action": "take", "ask_key": KEY}, headers=AUTH).json()
    assert res["ok"] is False and res["reasons"] == ["regime CHOPPY", "crosses 4"] and r.executed == []


def test_take_refused_when_already_answered(tmp_path):
    r = make(tmp_path)
    r.n.open_asks[KEY]["decided"] = "SKIP"
    res = r.client.post("/console/action", json={"action": "take", "ask_key": KEY}, headers=AUTH).json()
    assert res["reasons"] == ["already answered: SKIP"]


def test_bad_bodies(tmp_path):
    r = make(tmp_path)
    assert r.client.post("/console/action", content=b"[1]", headers={**AUTH, "Content-Type": "application/json"}).status_code == 400
    assert r.client.post("/console/action", json={"action": "nuke"}, headers=AUTH).json()["reasons"] == ["one of take, skip, close, read"]


# ----------------------------------------------------------------------------- close + read
def test_close_goes_through_the_close_path(tmp_path):
    pos = [{"ticket": 9, "direction": "long", "points": 3.2, "price_open": 4200.0, "current": 4203.2}]
    r = make(tmp_path, positions=pos)
    res = r.client.post("/console/action", json={"action": "close", "symbol": "XAUUSD"}, headers=AUTH).json()
    assert res["ok"] and r.closed == [["XAUUSD"]] and 9 in r.ag.closed_by_aureon
    assert any(ev == "manual_close" for ev, _ in r.j.rows)


def test_close_refused_with_execution_off_or_flat(tmp_path):
    pos = [{"ticket": 9, "direction": "long", "points": 3.2}]
    r = make(tmp_path, execution=False, positions=pos)
    assert r.client.post("/console/action", json={"action": "close"}, headers=AUTH).json()["reasons"] == ["execution off (AUREON_EXECUTION=0)"]
    r2 = make(tmp_path)
    assert r2.client.post("/console/action", json={"action": "close"}, headers=AUTH).json()["reasons"] == ["flat"]
    assert r.closed == [] and r2.closed == []


def test_read_pushes_the_verdict_as_a_high_event(tmp_path):
    r = make(tmp_path)
    pushed = []
    r.n.push = SimpleNamespace(notify=lambda **kw: pushed.append(kw))
    res = r.acts.read("XAUUSD", wait=True)
    assert res["read"]["decision"] == "HOLD" and r.acts.last_reads[-1]["symbol"] == "XAUUSD"
    assert pushed[0]["event"] == "READ_VERDICT" and pushed[0]["title"] == "XAUUSD · CLAUDE READ · HOLD"
    from aureon_mt5.push import map_event
    assert map_event(r.cfg, **{k: pushed[0][k] for k in ("key", "title", "lines", "symbol", "event", "klass")}).priority == "high"
    via = r.client.post("/console/action", json={"action": "read", "symbol": "XAUUSD"}, headers=AUTH).json()
    assert via["ok"] and via["pending"] is True


# ----------------------------------------------------------------------------- state + chart
def test_state_contract_v1(tmp_path):
    pos = [{"ticket": 9, "direction": "long", "points": 3.2, "price_open": 4200.0, "current": 4203.2, "sl": 4194.0}]
    r = make(tmp_path, positions=pos)
    s = r.client.get("/console/state", headers=AUTH).json()
    assert s["v"] == 1 and s["version"] == C.VERSION
    assert set(s) >= {"regime", "session", "positions", "pending_ask", "last_reads", "health"}
    assert s["regime"]["XAUUSD"]["state"] == "TRENDING" and s["positions"][0]["ticket"] == 9
    pa = s["pending_ask"]
    assert pa["key"] == KEY and pa["side"] == "LONG" and pa["actions"] == ["take", "skip", "read"] and pa["take_refused"] == []
    assert 1190 <= pa["expires_in_s"] <= 1200
    assert s["health"]["push"] == ["ntfy"] and s["health"]["agents"]["XAUUSD"]["status"] == "ok"


def test_chart_serves_the_ask_png_and_refuses_traversal(tmp_path):
    d = tmp_path / "charts"; d.mkdir()
    png = d / "ask.png"; png.write_bytes(b"\x89PNGx")
    (tmp_path / "secret.png").write_bytes(b"no")
    r = make(tmp_path, png=str(png))
    ok = r.client.get(f"/console/chart/{KEY}.png", headers=AUTH)
    assert ok.status_code == 200 and ok.content == b"\x89PNGx" and ok.headers["content-type"] == "image/png"
    assert r.client.get("/console/chart/ask.png", headers=AUTH).status_code == 200
    assert r.client.get("/console/chart/../secret.png", headers=AUTH).status_code == 404
    assert r.client.get("/console/chart/ask.png").status_code == 401


# ----------------------------------------------------------------------------- bind safety + architecture
def test_server_refuses_public_bind_and_short_token():
    c = SimpleNamespace(console_token=TOKEN, console_bind="0.0.0.0:8787", console_allow_public=False)
    assert "not loopback" in K.ConsoleServer(c, None).bind()
    c.console_allow_public = True
    assert K.ConsoleServer(c, None).bind() == ("0.0.0.0", 8787)
    assert "32+" in K.ConsoleServer(SimpleNamespace(console_token="short", console_bind="127.0.0.1:1", console_allow_public=False), None).bind()
    assert K.ConsoleServer(SimpleNamespace(console_token=TOKEN, console_bind="127.0.0.1:8787", console_allow_public=False), None).bind() \
        == ("127.0.0.1", 8787)


def test_server_starts_and_stops_on_its_own_thread(tmp_path):
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    r = make(tmp_path)
    r.cfg.console_bind = f"127.0.0.1:{port}"
    app = K.create_app(r.cfg, r.acts, r.j, tokens=r.tokens, state_fn=lambda: {"v": 1})
    srv = K.ConsoleServer(r.cfg, app)
    assert srv.start() == "" and srv.thread.name == "aureon-console"
    import requests
    for _ in range(50):
        try:
            got = requests.get(f"http://127.0.0.1:{port}/console/state", headers=AUTH, timeout=1); break
        except Exception:
            time.sleep(0.1)
    assert got.json() == {"v": 1}
    srv.stop()
    assert not srv.thread.is_alive()


def _imports(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            out |= {(node.module or "")} | {a.name for a in node.names}
    return out


def test_console_and_push_never_touch_mt5():
    for f in ("aureon_mt5/console.py", "aureon_mt5/push.py"):
        imps = _imports(f)
        assert "broker" not in imps and "MetaTrader5" not in imps, f
    assert "broker" not in _imports("aureon_mt5/claude_advisor.py")


def test_discord_take_skip_close_share_the_actions_path():
    src = open("aureon_mt5/bot.py", encoding="utf-8").read()
    assert "actions.take(" in src and "actions.skip(" in src and "actions.close(" in src
    assert '@tree.command(name="close"' in src
