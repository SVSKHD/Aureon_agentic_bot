"""v2.11.0 Part B — push layer: mapping, ntfy / pushover / telegram requests, scoped action tokens, never raises."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aureon_mt5 import push as P

TOKEN = "c" * 40


def cfg(**kw):
    base = dict(push=("NTFY",), push_images=False, push_loss_alert_pts=5.0, ntfy_url="https://ntfy.sh", ntfy_topic="aureon-x1",
                ntfy_token="", pushover_token="pt", pushover_user="pu", telegram_token="tt", telegram_chat="777",
                console_token=TOKEN, console_url="https://box.tail1234.ts.net", ask_ttl_min=20)
    base.update(kw)
    return SimpleNamespace(**base)


class R:
    def __init__(self, code=200, body=None):
        self.status_code = code; self._b = body or {}

    def json(self):
        return self._b


class Http:
    def __init__(self, code=200, raise_=None):
        self.calls = []; self.code = code; self.raise_ = raise_

    def _do(self, m, url, **kw):
        self.calls.append({"m": m, "url": url, **kw})
        if self.raise_:
            raise self.raise_
        return R(self.code)

    def post(self, url, **kw): return self._do("POST", url, **kw)

    def put(self, url, **kw): return self._do("PUT", url, **kw)


class J:
    def __init__(self): self.rows = []

    def log(self, ev, **kw): self.rows.append((ev, kw))


def ask_event(c=None):
    return P.map_event(c or cfg(), key="ema5080:XAUUSD:P_LONG:1", title="XAUUSD · **ENTER LONG**", lines=["P above EMA 50", ""],
                       fields=[{"name": "Price", "value": "4210.50"}], symbol="XAUUSD", event="ASK", klass="trade")


# ----------------------------------------------------------------------------- mapping
def test_mapping_priorities_and_actions():
    c = cfg()
    a = ask_event(c)
    assert a.priority == "urgent" and [x.label for x in a.actions] == ["TAKE", "SKIP", "READ"]
    assert a.title == "XAUUSD · ENTER LONG" and a.lines == ["P above EMA 50", "Price 4210.50"]
    m = lambda ev, **kw: P.map_event(c, key="k", title="t", event=ev, klass="trade", **kw)
    assert m("FILLED").priority == "high" and m("FILLED").actions == []
    assert m("CLOSED", points=8.0).priority == "normal"
    assert m("STOP", points=-3.0).priority == "normal"
    assert m("STOP", points=-6.5).priority == "urgent"                 # loss > AUREON_PUSH_LOSS_ALERT_PTS
    assert m("FLIP_HOLD").priority == "high" and [x.action for x in m("FLIP_HOLD").actions] == ["read"]
    assert [x.action for x in m("POSITION_ASK").actions] == ["close", "read"]
    for ev in ("MAINTENANCE", "COLD_START", "MT5_NOT_UP", "REPAIR_AUTO"):
        assert m(ev).priority == "high"
    assert m("TREND") is None and m("") is None


# ----------------------------------------------------------------------------- ntfy
def test_ntfy_json_publish_with_scoped_actions():
    tokens = P.ActionTokens(TOKEN)
    req = P.ntfy_request(cfg(), ask_event(), tokens)
    assert req["method"] == "POST" and req["url"] == "https://ntfy.sh"
    assert req["headers"] == {"Content-Type": "application/json"}
    b = req["json"]
    assert b["topic"] == "aureon-x1" and b["priority"] == 5 and b["tags"] == ["rotating_light", "XAUUSD"]
    assert b["click"] == "https://box.tail1234.ts.net/console/state"
    assert [a["label"] for a in b["actions"]] == ["TAKE", "SKIP", "READ"]
    for a in b["actions"]:
        assert a["action"] == "http" and a["method"] == "POST" and a["clear"] is True
        assert a["url"] == "https://box.tail1234.ts.net/console/action"
        bearer = a["headers"]["Authorization"]
        assert bearer.startswith("Bearer act.") and TOKEN not in json.dumps(b)          # the console token never leaves the box
        body = json.loads(a["body"])
        assert tokens.verify(bearer[7:], body["action"], body["symbol"], body["ask_key"]) is None


def test_ntfy_without_console_has_no_actions_and_token_header():
    req = P.ntfy_request(cfg(console_url="", ntfy_token="tk_abc"), ask_event(), P.ActionTokens(TOKEN))
    assert "actions" not in req["json"] and "click" not in req["json"]
    assert req["headers"]["Authorization"] == "Bearer tk_abc"


def test_ntfy_image_upload_put(tmp_path):
    png = tmp_path / "c.png"; png.write_bytes(b"\x89PNG fake")
    pe = ask_event(); pe.png = str(png); pe.title = "XAUUSD · ENTER 🚀"
    req = P.ntfy_request(cfg(push_images=True), pe, P.ActionTokens(TOKEN))
    assert req["method"] == "PUT" and req["url"] == "https://ntfy.sh/aureon-x1" and req["data"] == b"\x89PNG fake"
    h = req["headers"]
    assert h["X-Priority"] == "5" and h["X-Filename"] == "c.png" and h["X-Title"].startswith("=?UTF-8?B?")
    assert len(json.loads(h["X-Actions"])) == 3
    for v in h.values():
        v.encode("latin-1")                                                  # every header is sendable


# ----------------------------------------------------------------------------- pushover
def test_pushover_priorities():
    c = cfg()
    u = P.pushover_request(c, ask_event(c))["data"]
    assert u["priority"] == 2 and u["retry"] == 30 and u["expire"] == 600
    assert u["url"] == "https://box.tail1234.ts.net/console/state" and u["url_title"] == "Aureon console"
    hi = P.pushover_request(c, P.map_event(c, key="k", title="t", event="FILLED", klass="trade"))["data"]
    lo = P.pushover_request(c, P.map_event(c, key="k", title="t", event="CLOSED", klass="trade"))["data"]
    assert hi["priority"] == 1 and "retry" not in hi and lo["priority"] == 0


# ----------------------------------------------------------------------------- tokens
def test_action_tokens_single_use_scoped_expiring():
    now = [1000.0]
    t = P.ActionTokens(TOKEN, clock=lambda: now[0])
    tok = t.issue("take", "XAUUSD", "k1", ttl_s=60)
    assert t.verify(tok, "skip", "XAUUSD", "k1") == "action token is for a different action"
    assert t.verify(tok, "take", "XAUUSD", "k2") == "action token is for a different action"
    assert t.verify(tok, "take", "XAUUSD", "k1") is None
    assert t.verify(tok, "take", "XAUUSD", "k1") == "action token already used"
    old = t.issue("read", "XAUUSD", "k1", ttl_s=60); now[0] += 61
    assert t.verify(old, "read", "XAUUSD", "k1") == "action token expired"
    forged = t.issue("take", "XAUUSD", "k1")
    pre, body, sig = forged.split(".")
    assert t.verify(f"{pre}.{body}.{sig[:-2]}AA", "take", "XAUUSD", "k1") == "bad action token"
    assert P.ActionTokens("other" * 8).verify(forged, "take", "XAUUSD", "k1") == "bad action token"
    assert P.ActionTokens("").verify(forged, "take", "XAUUSD", "k1") == "console token not set"


# ----------------------------------------------------------------------------- the layer
def test_push_send_dedupes_and_uses_10s_timeout():
    http, j = Http(), J()
    p = P.Push(cfg(), j, http=http)
    futs = p.send(ask_event()) + p.send(ask_event())
    assert len(futs) == 1 and futs[0].result(5) is True
    assert http.calls[0]["timeout"] == 10.0 and http.calls[0]["m"] == "POST"
    assert j.rows[0][0] == "push_sent" and j.rows[0][1]["backend"] == "ntfy"
    p.stop()


def test_push_failures_are_logged_never_raised():
    j = J()
    p = P.Push(cfg(push=("NTFY", "PUSHOVER")), j, http=Http(raise_=TimeoutError("slow")))
    assert [f.result(5) for f in p.send(ask_event())] == [False, False]
    assert {r[0] for r in j.rows} == {"push_failed"} and "TimeoutError" in j.rows[0][1]["error"]
    p2 = P.Push(cfg(), j, http=Http(code=500))
    assert p2.send(ask_event())[0].result(5) is False
    p.stop(); p2.stop()


def test_push_off_does_nothing():
    http = Http()
    p = P.Push(cfg(push=()), None, http=http)
    assert p.notify(key="k", title="t", event="ASK", klass="trade") == [] and http.calls == []
    p.stop()


def test_notify_entry_maps_and_skips_info():
    http = Http()
    p = P.Push(cfg(), None, http=http)
    assert p.notify(key="m:XAUUSD:TREND:1", title="TREND", event="TREND", klass="info") == []
    [f] = p.notify(key="alert:XAUUSD:A:ORDER", title="ORDER PLACED", event="FILLED", klass="trade", symbol="XAUUSD")
    assert f.result(5) and http.calls[0]["json"]["priority"] == 4
    p.stop()


# ----------------------------------------------------------------------------- telegram
def test_telegram_keyboard_and_callbacks():
    sent = []
    p = P.Push(cfg(push=("TELEGRAM",)), J(), tg_send=lambda tok, chat, text, timeout, reply_markup: sent.append(
        (tok, chat, text, timeout, reply_markup)) or True)
    got = []
    p.handler = lambda action, symbol, ask_key, user: got.append((action, symbol, ask_key, user)) or {"ok": True, "text": "taken"}
    assert p.send(ask_event())[0].result(5) is True
    tok, chat, text, timeout, kb = sent[0]
    assert (tok, chat, timeout) == ("tt", "777", 10.0) and text.startswith("XAUUSD · ENTER LONG")
    btns = kb["inline_keyboard"][0]
    assert [b["text"] for b in btns] == ["TAKE", "SKIP", "READ"] and all(len(b["callback_data"]) <= 64 for b in btns)
    take = btns[0]["callback_data"]
    assert p.on_callback({"data": take, "message": {"chat": {"id": 999}}, "from": {"username": "x"}}) == "not this chat"
    assert p.on_callback({"data": take, "message": {"chat": {"id": 777}}, "from": {"username": "me"}}) == "taken"
    assert got == [("take", "XAUUSD", "ema5080:XAUUSD:P_LONG:1", "telegram:me")]
    assert p.on_callback({"data": take, "message": {"chat": {"id": 777}}}) == "expired or already used"
    p.stop()


def test_telegram_poll_once_answers_callbacks():
    class TgHttp(Http):
        def get(self, url, params=None, timeout=None):
            self.calls.append({"m": "GET", "url": url, "params": params, "timeout": timeout})
            return R(body={"result": [{"update_id": 5, "callback_query": {"id": "cq1", "data": "a:nope",
                                                                          "message": {"chat": {"id": 777}}}}]})
    http = TgHttp()
    p = P.Push(cfg(push=("TELEGRAM",)), None, http=http)
    assert p.poll_once(0) == 6
    assert http.calls[0]["params"]["timeout"] == 25
    assert http.calls[1]["url"].endswith("/answerCallbackQuery") and http.calls[1]["data"]["text"] == "expired or already used"
    p.stop()


def test_reuses_deadman_telegram_send():
    fn = P._telegram_send()
    assert fn.__name__ == "telegram_send" and "reply_markup" in fn.__code__.co_varnames
