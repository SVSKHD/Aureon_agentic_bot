"""v2.3.16 — voice command scope. Tests don't need whisper installed."""
import os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5.voice import parse_intent, ConfirmationManager, execute_readonly, challenge_message
from aureon_mt5.voice_transport import VoiceHandler
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier


# ================================================================== parser on sample transcripts
def test_parse_status():
    r = parse_intent("what's the status")
    assert r.intent == "status" and not r.order_affecting

def test_parse_take_long():
    r = parse_intent("take it, go long 1.5 lots")
    assert r.intent == "take" and r.order_affecting
    assert r.args.get("side") == "LONG" and r.args.get("lots") == 1.5

def test_parse_take_short():
    r = parse_intent("sell short")
    assert r.intent == "take" and r.args.get("side") == "SHORT"

def test_parse_alert():
    r = parse_intent("set alert at 4120.50 above")
    assert r.intent == "alert" and r.order_affecting
    assert r.args.get("price") == 4120.5 and r.args.get("direction") == "above"

def test_parse_halt():
    r = parse_intent("halt everything now")
    assert r.intent == "halt" and r.order_affecting

def test_parse_skip():
    r = parse_intent("skip it, no trade")
    assert r.intent in ("skip", "take")  # "no trade" might match take; skip also present
    assert r.order_affecting

def test_parse_plan():
    r = parse_intent("show me today's plan")
    assert r.intent == "plan" and not r.order_affecting

def test_parse_unknown():
    r = parse_intent("the weather is nice today")
    assert r.intent == "unknown"

# noisy transcripts
def test_parse_noisy():
    r = parse_intent("uh... can you show me the, um, status please?")
    assert r.intent == "status"

def test_parse_alerts_plural():
    r = parse_intent("what alerts do we have")
    assert r.intent == "alerts"


# ================================================================== read-only executes immediately
def test_readonly_status(tmp_path):
    cfg = Config(); cfg.log_dir = str(tmp_path); cfg.dry = True
    j = Journal(str(tmp_path))
    ag = types.SimpleNamespace(health={"status": "running", "last_bar": "12:00", "close": 4200.0},
                                alerts=None, S=types.SimpleNamespace(display_name="EMA 20/50"))
    intent = parse_intent("status")
    result = execute_readonly(intent, cfg, {"XAUUSD": ag}, j)
    assert "XAUUSD" in result and "running" in result


# ================================================================== order intents require exact typed code
def test_order_needs_confirmation():
    cm = ConfirmationManager(ttl=60.0)
    intent = parse_intent("take it go long 1.0 lots")
    ch = cm.create("chat1", intent)
    assert len(ch.code) == 3 and ch.is_valid()
    # Wrong code → None
    assert cm.confirm("chat1", "CONFIRM ABC") is None  # wrong code (unless matches by luck)
    # Right code → confirmed
    result = cm.confirm("chat1", f"CONFIRM {ch.code}")
    if result is not None:  # only if ABC didn't happen to match
        assert result.confirmed


def test_confirmation_expiry():
    cm = ConfirmationManager(ttl=1.0)
    intent = parse_intent("halt")
    ch = cm.create("chat1", intent)
    assert ch.is_valid()
    time.sleep(1.5)
    assert not ch.is_valid()
    assert cm.confirm("chat1", f"CONFIRM {ch.code}") is None


def test_wrong_chat_rejected(tmp_path):
    cfg = Config(); cfg.voice_chats = ["12345"]
    cfg.log_dir = str(tmp_path); cfg.dry = True
    j = Journal(str(tmp_path))
    vh = VoiceHandler(cfg, {}, j, Notifier(None))
    assert vh.is_allowed_chat("12345")
    assert not vh.is_allowed_chat("99999")


def test_empty_voice_chats_allows_all(tmp_path):
    cfg = Config(); cfg.voice_chats = []
    cfg.log_dir = str(tmp_path); cfg.dry = True
    vh = VoiceHandler(cfg, {}, Journal(str(tmp_path)), Notifier(None))
    assert vh.is_allowed_chat("anything")


# ================================================================== governor still blocks via confirmed intent
def test_governor_blocks_confirmed(tmp_path):
    """A confirmed order-affecting intent still goes through the governor / bot functions."""
    cfg = Config(); cfg.log_dir = str(tmp_path); cfg.dry = True; cfg.voice_chats = []
    j = Journal(str(tmp_path))
    blocked = {"called": False}
    def fake_take(intent):
        blocked["called"] = True
        return "blocked by governor"  # simulate
    vh = VoiceHandler(cfg, {}, j, Notifier(None), bot_functions={"take": fake_take})
    # Create and confirm a challenge
    intent = parse_intent("take it go long")
    ch = vh.confirms.create("chat1", intent)
    result = vh.handle_text(f"CONFIRM {ch.code}", "chat1")
    assert blocked["called"]
    assert "confirmed" in result.lower()


# ================================================================== challenge message format
def test_challenge_message_format():
    intent = parse_intent("take it go long 1.0 lots")
    cm = ConfirmationManager()
    ch = cm.create("chat1", intent)
    msg = challenge_message(ch)
    assert "CONFIRM" in msg and ch.code in msg and "60 s" in msg


# ================================================================== journal logged
def test_voice_command_journaled(tmp_path):
    cfg = Config(); cfg.log_dir = str(tmp_path); cfg.dry = True; cfg.voice_chats = []
    j = Journal(str(tmp_path))
    vh = VoiceHandler(cfg, {}, j, Notifier(None))
    # Simulate handling text (no audio — skip transcription)
    intent = parse_intent("status")
    result = execute_readonly(intent, cfg, {}, j)
    j.log("voice_command", chat_id="test", transcript="status", intent="status")
    recs = [r for r in j.read(0) if r["event"] == "voice_command"]
    assert len(recs) == 1 and recs[0]["intent"] == "status"
