"""v2.2.1 fixes: CliResult usage fields, safe auto-update pull, rules file sections."""
from types import SimpleNamespace
from unittest.mock import Mock

import main
from aureon_mt5 import claude_advisor
from aureon_mt5.claude_usage import append
from aureon_mt5.common.claude_cli import CliResult, parse_output


# ------------------------------------------------------------------ A1) CliResult carries usage / cost
def test_parse_output_keeps_usage_and_cost(tmp_path):
    out = ('{"type": "result", "result": "{\\"decision\\": \\"SKIP\\", \\"side\\": \\"BUY\\", \\"confidence\\": \\"low\\", '
           '\\"reason\\": \\"chop\\"}", "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.01}')
    res = parse_output(out, "ENTER")
    assert res.usage == {"input_tokens": 10, "output_tokens": 5} and res.total_cost_usd == 0.01
    assert CliResult(False).usage is None and CliResult(False).total_cost_usd is None
    row = append(str(tmp_path), "entry", SimpleNamespace(ok=False, latency_s=0, error="", timed_out=False, verdict=None))
    assert row["total_cost_usd"] is None and row["input_tokens"] == 0 and row["model"] == ""


# ------------------------------------------------------------------ A2) auto-update never pulls a dirty checkout
def _git(status_out="", pull_rc=0, local="aaaa1111", remote="bbbb2222"):
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["git", "rev-parse"]:
            return SimpleNamespace(returncode=0, stdout=local if cmd[2] == "HEAD" else remote, stderr="")
        if cmd[:2] == ["git", "status"]:
            return SimpleNamespace(returncode=0, stdout=status_out, stderr="")
        if cmd[:2] == ["git", "pull"]:
            return SimpleNamespace(returncode=pull_rc, stdout="", stderr="not possible to fast-forward")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run, calls


def test_dirty_checkout_skips_once_per_day(monkeypatch):
    monkeypatch.setattr(main, "_update_skipped_day", None)
    run, calls = _git(status_out=" M main.py\n")
    notify, execv = Mock(), Mock()
    assert main.check_for_updates(notify, run=run, execv=execv) is False
    assert main.check_for_updates(notify, run=run, execv=execv) is False
    assert notify.card.call_count == 1 and "UPDATE SKIPPED · main.py" in notify.card.call_args[0][0]
    assert not any(c[:2] == ["git", "pull"] for c in calls) and not execv.called


def test_failed_pull_never_execs_and_uses_ff_only(monkeypatch):
    monkeypatch.setattr(main, "_time", SimpleNamespace(sleep=lambda s: None))
    run, calls = _git(pull_rc=1)
    notify, execv = Mock(), Mock()
    assert main.check_for_updates(notify, run=run, execv=execv) is False
    assert ["git", "pull", "--ff-only", "origin", "master"] in calls
    assert not execv.called and "UPDATE FAILED" in notify.card.call_args[0][0]


def test_clean_checkout_pulls_and_execs(monkeypatch):
    monkeypatch.setattr(main, "_time", SimpleNamespace(sleep=lambda s: None))
    run, calls = _git()
    notify, execv = Mock(), Mock()
    main.check_for_updates(notify, run=run, execv=execv)
    assert ["git", "pull", "--ff-only", "origin", "master"] in calls and execv.called


# ------------------------------------------------------------------ A3) rules file loads with both mode sections
def test_rules_file_loads_with_both_mode_sections():
    text = open(claude_advisor.RULES_PATH, encoding="utf-8").read()
    assert "== MODE ema2050" in text and "== MODE ema5080" in text
