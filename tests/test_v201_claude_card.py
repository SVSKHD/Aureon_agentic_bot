"""v2.0.1 — /claude attachment card: ATTACHED for an advisory ema2050 agent, DETACHED (mode=off) when the add-on is off, DETACHED after
/claude-detach with no Claude job enqueued afterwards, re-attach, /status line, red header when the binary is missing or the login failed."""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5 import bot, broker
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.claude_advisor import ClaudeAdvisor
from aureon_mt5.common import claude_cli as cc
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.strategies import get_strategy
from aureon_mt5.strategies.ema2050 import journeys as J
from tests.test_claude_advisor import Cap, FakeCLI, ok
from tests.test_v200_alerts import TickMT5
from tests.test_v200_ema2050 import _bull, OFF

S2050 = get_strategy("ema2050"); S5080 = get_strategy("ema5080")


def rig(tmp_path, monkeypatch, mode="advisory", strategy=S2050):
    fake = TickMT5(price=4000.5); monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.claude_mode = mode; cfg.claude_workdir = str(tmp_path / "wd")
    j = Journal(str(tmp_path)); n = Cap()
    adv = ClaudeAdvisor.create(cfg, n, j, caller=FakeCLI(ok("TAKE", side="BUY")), start=False)
    ag = SymbolAgent("XAUUSD", cfg, strategy, n, j, (), claude=adv); ag.off = OFF
    ag._refresh_snapshot(True)
    return types.SimpleNamespace(cfg=cfg, j=j, n=n, adv=adv, ag=ag, agents={"XAUUSD": ag})


def per_agent(card):
    return next(f["value"] for f in card["fields"] if f["name"] == "Per agent")


def enter_bar(ag):
    df = _bull(n=12, touch_at=(11,)); res = J.build(df, J.rules_for("XAUUSD"), OFF); res["df"] = df
    ag._detect_2050(res, df, int(df["time"].iloc[-1]), float(df["close"].iloc[-1]), float(df["ema20"].iloc[-1]), float(df["ema50"].iloc[-1]))


def test_card_attached_for_advisory_ema2050(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    card = bot.claude_card(r.adv, r.cfg, r.agents)
    line = per_agent(card)
    assert line.startswith("XAUUSD · EMA 20/50 · ATTACHED (advisory) · triggers: ENTER bar, pullback-in-trade")
    hdr = next(f["value"] for f in card["fields"] if f["name"] == "Header")
    assert "mode **ADVISORY**" in hdr and "bin `" in hdr and card["color"] == bot.BLURPLE
    models = next(f["value"] for f in card["fields"] if f["name"] == "Models")
    assert f"entry `{r.cfg.claude_entry_model}`" in models
    budget = next(f["value"] for f in card["fields"] if f["name"] == "Budget")
    assert "calls **0 / 30** today · entry 0 · pullback 0 · alert 0 · fast-exit cancellations 0" in budget
    assert bot.claude_status_word(r.ag, r.adv) == "ATTACHED (advisory)"
    assert any(f["name"] == "Claude" and f["value"] == "ATTACHED (advisory)" for f in bot.status_card("XAUUSD", r.ag, r.cfg, claude=r.adv)["fields"])


def test_card_detached_when_mode_off(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch, mode="off")
    assert r.adv is None and r.ag.claude is None
    card = bot.claude_card(None, r.cfg, r.agents)
    assert "XAUUSD · EMA 20/50 · DETACHED (mode=off)" in next(f["value"] for f in card["fields"] if f["name"] == "Attachment")
    assert bot.claude_status_word(r.ag, None) == "OFF"
    r5 = rig(tmp_path, monkeypatch, mode="review", strategy=S5080)
    assert "REVIEW (cards only, nothing applied)" in per_agent(bot.claude_card(r5.adv, r5.cfg, r5.agents)) and bot.claude_status_word(r5.ag, r5.adv) == "REVIEW (cards only)"


def test_detach_then_no_job_enqueued_then_attach(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    enter_bar(r.ag); assert r.adv.q.qsize() == 1                             # attached: the ENTER bar asks
    n = r.adv.detach(r.ag, "tester")
    assert n == 1 and r.ag.claude is None and r.adv.q.qsize() == 0
    assert "XAUUSD · EMA 20/50 · DETACHED (/claude-detach)" in per_agent(bot.claude_card(r.adv, r.cfg, r.agents))
    assert bot.claude_status_word(r.ag, r.adv) == "DETACHED"
    r.n.sent.clear(); enter_bar(r.ag)
    assert r.adv.q.qsize() == 0                                              # detached: no Claude job
    assert any(x["event"] == "claude_detach" and x["cancelled"] == 1 for x in r.j.read(0))
    okk, why = r.adv.attach(r.ag, "tester")
    assert okk and r.ag.claude is r.adv and why == "ATTACHED (advisory)" and any(x["event"] == "claude_attach" for x in r.j.read(0))
    r.n.sent.clear(); enter_bar(r.ag); assert r.adv.q.qsize() == 1


def test_attach_refused_in_review_mode(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch, mode="review")
    okk, why = r.adv.attach(r.ag, "tester")
    assert not okk and "review" in why and r.ag.claude is None


def test_header_red_when_binary_missing_or_auth_failed(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    r.adv.stats["login"] = "NOT FOUND"; r.adv.stats["version_error"] = "'claude' not found — install Claude Code or set AUREON_CLAUDE_BIN"
    card = bot.claude_card(r.adv, r.cfg, r.agents)
    assert card["color"] == bot.RED and "AUREON_CLAUDE_BIN" in next(f["value"] for f in card["fields"] if f["name"] == "Header")
    r.adv.stats["login"] = "FAILED"; r.adv.stats["version_error"] = ""; r.adv.stats["version"] = "2.1.0 (Claude Code)"
    card = bot.claude_card(r.adv, r.cfg, r.agents)
    assert card["color"] == bot.RED and "log in" in next(f["value"] for f in card["fields"] if f["name"] == "Header")


def test_version_and_reported_model_captured(tmp_path, monkeypatch):
    v = cc.version("claude-definitely-not-installed-xyz")
    assert not v["ok"] and "not found" in v["error"]
    r = rig(tmp_path, monkeypatch)
    out = {"type": "result", "subtype": "success", "is_error": False, "result": '{"decision":"TAKE","side":"BUY","confidence":"high","reason":"x"}',
           "modelUsage": {"claude-opus-4-1-20250805": {"inputTokens": 1}}}
    import json
    res = cc.parse_output(json.dumps(out), "ENTER")
    assert res.ok and res.model == "claude-opus-4-1-20250805"
    r.adv.caller = lambda *a, **k: res
    r.adv._call("p", "opus", "ENTER")
    st = r.adv.status()
    assert st["calls_by_event"]["entry"] == 1 and st["reported_models"]["entry"] == "claude-opus-4-1-20250805" and st["last_latency"] is not None
    assert "→ reported `claude-opus-4-1-20250805`" in next(f["value"] for f in bot.claude_card(r.adv, r.cfg, r.agents)["fields"] if f["name"] == "Models")


def test_commands_go_through_run():
    import ast
    tree = ast.parse(open(bot.__file__, encoding="utf-8").read())
    names = {f.name for f in ast.walk(tree) if isinstance(f, ast.AsyncFunctionDef)
             and any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "command" for d in f.decorator_list)}
    assert {"claude_cmd", "claude_attach_cmd", "claude_detach_cmd"} <= names
