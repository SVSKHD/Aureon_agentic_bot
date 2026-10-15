"""v2.11.0 Part A — event classes, immediate Discord mentions, the asks channel (fan-out + edits), registry entries."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests

from aureon_mt5 import config as C
from aureon_mt5 import features as F
from aureon_mt5.notify import EVENT_CLASS, Notifier, event_class, parse_key, title_event

MAIN = "https://discord.com/api/webhooks/1/main"
ASKS = "https://discord.com/api/webhooks/2/asks"


class Resp:
    def __init__(self, mid="900"):
        self.status_code = 200; self.text = ""; self._mid = mid

    def json(self):
        return {"id": self._mid}


@pytest.fixture
def http(monkeypatch):
    calls = []
    n = iter(range(1000, 2000))

    def post(url, json=None, data=None, files=None, timeout=None):
        calls.append({"m": "POST", "url": url, "json": json, "data": data}); return Resp(str(next(n)))

    def patch(url, json=None, timeout=None):
        calls.append({"m": "PATCH", "url": url, "json": json}); return Resp()

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests, "patch", patch)
    return calls


def cfg(**kw):
    base = dict(discord_user_id="", discord_asks_webhook="", discord_mention_events=("ASK", "FILLED", "CLOSED", "STOP"))
    base.update(kw)
    return SimpleNamespace(**base)


class FakePush:
    def __init__(self): self.calls = []

    def notify(self, **kw): self.calls.append(kw)


# ----------------------------------------------------------------------------- classification
def test_event_class_by_prefix_default_info():
    assert event_class("ASK") == "trade" and event_class("FILLED") == "trade" and event_class("STOP") == "trade"
    assert event_class("CLOSED_BY_YOU") == "trade"                 # prefix CLOSED
    assert event_class("COLD_START") == "ops" and event_class("MT5_NOT_UP") == "ops"
    assert event_class("TREND") == "info" and event_class("") == "info" and event_class(None) == "info"
    assert set(EVENT_CLASS.values()) == {"trade", "ops"}


def test_parse_key_formats():
    assert parse_key("ema5080:XAUUSD:FLIP_HOLD:12-34") == ("XAUUSD", "FLIP_HOLD")
    assert parse_key("alert:XAUUSD:A7:ORDER") == ("XAUUSD", "FILLED")          # ORDER is an alias of FILLED
    assert parse_key("nokey") == ("", "")
    assert title_event("AUREON · MT5 · COLD START") == "COLD_START"
    assert title_event("REPAIR AUTO-APPLY #3") == "REPAIR_AUTO"
    assert title_event("REPAIR #3 · AUTO-APPLY HELD") == ""


def test_classify_explicit_event_and_klass_win():
    n = Notifier(None)
    assert n.classify("m:XAUUSD:TREND:1", "t") == ("XAUUSD", "TREND", "info")
    assert n.classify("m:XAUUSD:TREND:1", "t", event="FILLED") == ("XAUUSD", "FILLED", "trade")
    assert n.classify("m:XAUUSD:TREND:1", "t", klass="trade")[2] == "trade"
    assert n.classify(None, "AUREON · MT5 · MT5 NOT UP") == ("", "MT5_NOT_UP", "ops")


# ----------------------------------------------------------------------------- defaults are byte-identical
def test_defaults_payload_unchanged(http):
    n = Notifier(MAIN)                                     # no configure() at all
    assert n.send("m:XAUUSD:ASK:1", "XAUUSD · ENTER LONG", ["x"])
    n2 = Notifier(MAIN).configure(C.Config())              # configured, v2.11 variables unset
    assert n2.send("m:XAUUSD:ASK:2", "XAUUSD · ENTER LONG", ["x"])
    for c in http:
        assert c["url"] == MAIN and set(c["json"]) == {"embeds"}


# ----------------------------------------------------------------------------- mentions
def test_mention_on_trade_events_only(http):
    n = Notifier(MAIN).configure(cfg(discord_user_id="123456789012345678"))
    n.ask("m:XAUUSD:P_LONG:1", "XAUUSD · ENTER LONG", ["take it?"])
    n.send("alert:XAUUSD:A1:ORDER", "XAUUSD · MT5 · ORDER PLACED · LONG", ["x"])
    n.send("m:XAUUSD:TREND:1", "XAUUSD · TREND · UP", ["x"])
    n.send("m:XAUUSD:CLOSE_X:1", "XAUUSD · CLOSED LOSS", ["x"], event="STOP", points=-4.0)
    n.send("m:XAUUSD:ASK:7", "SHOULD WE CLOSE?", ["x"], event="POSITION_ASK")
    ask, filled, trend, stop, pos = (c["json"] for c in http)
    for p in (ask, filled, stop):
        assert p["content"] == "<@123456789012345678>"
        assert p["allowed_mentions"] == {"users": ["123456789012345678"]}
        assert "embeds" in p
    for p in (trend, pos):
        assert "content" not in p and "allowed_mentions" not in p


def test_mention_events_are_hot_and_user_id_must_be_digits(http):
    c = cfg(discord_user_id="42")
    n = Notifier(MAIN).configure(c)
    c.discord_mention_events = ("FILLED",)                 # /feature-set mention_events FILLED (applied in place)
    n.ask("m:XAUUSD:P_LONG:1", "ENTER", ["x"])
    n.send("alert:XAUUSD:A1:ORDER", "ORDER PLACED", ["x"])
    assert "content" not in http[0]["json"] and http[1]["json"]["content"] == "<@42>"
    c.discord_user_id = "@me"
    n.send("alert:XAUUSD:A2:ORDER", "ORDER PLACED", ["x"])
    assert "content" not in http[2]["json"]


def test_bot_mode_ask_carries_the_mention():
    n = Notifier(None).configure(cfg(discord_user_id="42"))
    n.bot_ready = True
    assert n.ask("m:XAUUSD:P_LONG:1", "ENTER", ["x"], meta={"symbol": "XAUUSD", "side": "LONG"})
    item = n.ask_queue.get_nowait()
    assert item["mention"] == "42" and n.open_asks["m:XAUUSD:P_LONG:1"] is item


def test_bot_posts_mention_with_allowed_mentions_source():
    src = open("aureon_mt5/bot.py", encoding="utf-8").read()
    assert 'content": f"<@{item[\'mention\']}>"' in src
    assert "discord.AllowedMentions(everyone=False, roles=False" in src


# ----------------------------------------------------------------------------- asks channel
def test_trade_cards_fan_out_to_the_asks_channel(http):
    n = Notifier(MAIN).configure(cfg(discord_user_id="42", discord_asks_webhook=ASKS))
    n.send("alert:XAUUSD:A1:ORDER", "ORDER PLACED", ["x"])
    n.send("m:XAUUSD:TREND:1", "TREND", ["x"])
    urls = [c["url"] for c in http]
    assert urls == [MAIN + "?wait=true", ASKS + "?wait=true", MAIN]
    assert http[1]["json"]["content"] == "<@42>"
    assert [c[0] for c in n.copies["alert:XAUUSD:A1:ORDER"]] == [MAIN, ASKS]


def test_edit_card_updates_both_copies_in_webhook_mode(http):
    n = Notifier(MAIN).configure(cfg(discord_asks_webhook=ASKS))
    n.ask("m:XAUUSD:P_LONG:1", "ENTER", ["x"], fields=[{"name": "Side", "value": "LONG"}])
    assert n.edit_card("m:XAUUSD:P_LONG:1", {"Decision": "✅ TAKEN"}) is True
    patches = [c for c in http if c["m"] == "PATCH"]
    assert sorted(p["url"] for p in patches) == sorted([f"{MAIN}/messages/1000", f"{ASKS}/messages/1001"])
    for p in patches:
        names = [f["name"] for f in p["json"]["embeds"][0]["fields"]]
        assert names == ["Side", "Decision"]


def test_edit_card_without_asks_channel_keeps_v210_semantics(http):
    n = Notifier(MAIN).configure(cfg())
    n.ask("m:XAUUSD:P_LONG:1", "ENTER", ["x"])
    assert n.edit_card("m:XAUUSD:P_LONG:1", {"Decision": "x"}) is False        # caller posts a follow-up, as before
    assert not [c for c in http if c["m"] == "PATCH"]


def test_bot_mode_edit_patches_the_asks_copy_and_calls_the_hook(http):
    n = Notifier(MAIN).configure(cfg(discord_asks_webhook=ASKS))
    n.bot_ready = True
    seen = []
    n.card_edit_hook = lambda k, u: seen.append((k, u)) or True
    n.ask("m:XAUUSD:P_LONG:1", "ENTER", ["x"])
    assert [c["url"] for c in http] == [ASKS + "?wait=true"]
    assert n.edit_card("m:XAUUSD:P_LONG:1", {"Decision": "⏭"}) is True
    assert seen and http[-1]["url"] == f"{ASKS}/messages/1000"


def test_mirror_edit_never_raises(monkeypatch):
    n = Notifier(MAIN)
    n.copies["k"] = [[ASKS, "1", {"title": "t"}]]
    monkeypatch.setattr(requests, "patch", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert n.mirror_edit("k", {"a": "b"}) is False


# ----------------------------------------------------------------------------- push hook: one per dedupe key
def test_one_push_per_dedupe_key(http):
    p = FakePush()
    n = Notifier(MAIN).configure(cfg(), push=p)
    n.ask("m:XAUUSD:P_LONG:1", "ENTER", ["x"], meta={"symbol": "XAUUSD"})
    n.ask("m:XAUUSD:P_LONG:1", "ENTER", ["x"])
    n.send("m:XAUUSD:CLOSED_BY_YOU:9", "CLOSED", ["x"], event="STOP", points=-7.5)
    n.card("AUREON · MT5 · COLD START", "boot")
    assert [c["event"] for c in p.calls] == ["ASK", "STOP", "COLD_START"]
    assert p.calls[1]["points"] == -7.5 and p.calls[0]["symbol"] == "XAUUSD" and p.calls[2]["klass"] == "ops"


def test_discord_down_still_pushes_once(monkeypatch):
    class R(Resp):
        def __init__(self): super().__init__(); self.status_code = 500
    monkeypatch.setattr(requests, "post", lambda *a, **k: R())
    p = FakePush()
    n = Notifier(MAIN).configure(cfg(), push=p)
    assert not n.send("alert:XAUUSD:A1:ORDER", "ORDER", ["x"])
    assert not n.send("alert:XAUUSD:A1:ORDER", "ORDER", ["x"])          # Discord retry: no second push
    assert [c["event"] for c in p.calls] == ["FILLED"]


# ----------------------------------------------------------------------------- registry + validation
def test_registry_entries():
    push, imgs, me = F.get("push"), F.get("push_images"), F.get("mention_events")
    assert push.type == "enum-set" and push.hot and push.default == () and push.group == "notify"
    assert F.parse(push, "ntfy,telegram") == ("NTFY", "TELEGRAM") and F.parse(push, "off") == ()
    assert F.is_loud(push, ("NTFY",)) and not F.is_loud(push, ())
    assert imgs.type == "bool" and imgs.default is False
    assert me.default == ("ASK", "FILLED", "CLOSED", "STOP") and me.hot
    with pytest.raises(ValueError):
        F.parse(me, "ASK,NOPE")


def test_config_defaults():
    c = C.Config()
    assert c.push == () and c.discord_mention_events == ("ASK", "FILLED", "CLOSED", "STOP")
    assert c.console_bind == "127.0.0.1:8787" and c.console_allow_public is False and c.ntfy_url == "https://ntfy.sh"
    assert C._notify_problems(c, {}) == []


def test_validate_flags_console_and_push_problems():
    c = C.Config()
    c.discord_user_id = "me"; c.console_token = "short"; c.console_bind = "0.0.0.0:8787"; c.push = ("NTFY", "PUSHOVER")
    probs = {p["name"]: p for p in C._notify_problems(c, {})}
    assert "discord_user_id" in probs and "console_token" in probs and probs["console_bind"]["fallback"] == "127.0.0.1:8787"
    fixed = C.apply_fallbacks(c, list(probs.values()) + [p for p in C._notify_problems(c, {}) if p["name"] == "push"])
    assert c.console_bind == "127.0.0.1:8787" and c.console_token == "" and c.discord_user_id == ""
    assert "console_bind" in fixed


def test_public_bind_is_loud_when_allowed():
    c = C.Config(); c.console_bind = "0.0.0.0:8787"; c.console_allow_public = True; c.console_token = "x" * 40
    probs = C._notify_problems(c, {})
    assert any(p["name"] == "console_public" and "CONSOLE LISTENS ON 0.0.0.0" in p["issue"] for p in probs)
    assert not any(p["name"] == "console_bind" for p in probs)
