"""v1.10.0 — claude_cli: subprocess call, parsing, validation, environment scrubbing, daily budget. Never calls real Claude."""
import json, os, subprocess, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5.common import claude_cli as cc


def out(result, **kw):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": result, **kw})


V = {"decision": "TAKE", "side": "BUY", "tighten_to": None, "confidence": "high", "reason": "lines separating, price near EMA 50"}


def test_parse_valid_plain_fenced_and_wrapped():
    for text in (json.dumps(V), "```json\n" + json.dumps(V) + "\n```", "Here you go: " + json.dumps(V) + " done"):
        r = cc.parse_output(out(text), "P")
        assert r.ok and r.verdict["decision"] == "TAKE" and r.verdict["side"] == "BUY"


@pytest.mark.parametrize("stdout,event", [
    ("not json at all", "P"),                                                     # CLI output not JSON
    (out("I think you should buy"), "CROSS"),                                    # result not JSON
    (out(json.dumps({**V, "decision": "MAYBE"})), "P"),                          # unknown decision
    (out(json.dumps({**V, "decision": "HOLD"})), "RE"),                          # pullback decision on an entry
    (out(json.dumps({**V, "decision": "TAKE"})), "pullback"),                    # entry decision on a pullback
    (out(json.dumps({**V, "decision": "TIGHTEN", "tighten_to": None})), "pullback"),
    (out(json.dumps({**V, "confidence": "certain"})), "P"),
    (out(json.dumps({**V, "side": "LONGISH"})), "P"),
    (json.dumps({"type": "result", "is_error": True, "result": "boom"}), "P"),
])
def test_invalid_outputs_give_no_verdict(stdout, event):
    r = cc.parse_output(stdout, event)
    assert not r.ok and r.verdict is None


def test_reason_truncated_to_20_words():
    r = cc.parse_output(out(json.dumps({**V, "reason": " ".join(["w"] * 40)})), "P")
    assert r.ok and len(r.verdict["reason"].split()) == 20


def test_call_builds_command_stdin_cwd_and_clean_env(tmp_path, monkeypatch):
    seen = {}
    def run(cmd, **kw):
        seen.update(cmd=cmd, **kw); return types.SimpleNamespace(returncode=0, stdout=out(json.dumps(V)), stderr="")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-pass"); monkeypatch.setenv("DISCORD_WEBHOOK", "https://x")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k"); monkeypatch.setenv("AUREON_CLAUDE", "manage"); monkeypatch.setenv("MT5_PASSWORD", "p")
    r = cc.call("PROMPT", "opus", event="P", bin="claude", workdir=str(tmp_path / "wd"), timeout=5, run=run)
    assert r.ok and r.verdict["decision"] == "TAKE"
    assert seen["cmd"][1:] == ["-p", "--output-format", "json", "--model", "opus"]
    assert seen["input"] == "PROMPT" and seen["cwd"] == str(tmp_path / "wd") and seen["timeout"] == 5
    env = seen["env"]
    for k in ("ANTHROPIC_API_KEY", "DISCORD_WEBHOOK", "SUPABASE_SERVICE_KEY", "AUREON_CLAUDE", "MT5_PASSWORD"):
        assert k not in env
    assert "PATH" in env


def test_call_timeout_fails_closed(tmp_path):
    def run(cmd, **kw): raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    r = cc.call("x", "opus", event="P", workdir=str(tmp_path), timeout=1, run=run)
    assert not r.ok and r.timed_out and r.verdict is None


def test_call_auth_error_detected(tmp_path):
    def run(cmd, **kw): return types.SimpleNamespace(returncode=1, stdout="", stderr="Invalid API key · Please run /login")
    r = cc.call("x", "opus", event="P", workdir=str(tmp_path), run=run)
    assert not r.ok and r.auth_error
    def run2(cmd, **kw): raise FileNotFoundError()
    assert cc.call("x", "opus", event="P", workdir=str(tmp_path), run=run2).auth_error


def test_review_and_test_return_text():
    r = cc.parse_output(out("OK"), "test"); assert r.ok and r.text == "OK"


def test_budget_limit_persists_and_resets_next_ist_day(tmp_path):
    now = [1_760_000_000.0]
    path = str(tmp_path / "b.json")
    b = cc.Budget(path, 2, clock=lambda: now[0])
    assert b.take() and b.take() and not b.take() and b.used() == 2
    assert not cc.Budget(path, 2, clock=lambda: now[0]).take()          # survives a restart
    now[0] += 86400
    assert b.used() == 0 and b.take()
