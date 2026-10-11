"""Codex bridge contract tests: no broker access, no action on bad replies."""
import json
import subprocess
from unittest.mock import patch

from aureon_mt5.common import codex_cli


def test_codex_schema_is_closed_and_complete():
    schema = codex_cli.VERDICT_SCHEMA
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_codex_success(tmp_path):
    def fake_run(cmd, **kw):
        assert "--sandbox" in cmd and "read-only" in cmd
        assert "--skip-git-repo-check" in cmd
        assert "order_send" not in kw["input"]
        out = cmd[cmd.index("--output-last-message") + 1]
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"decision": "SKIP", "side": None, "tighten_to": None,
                       "confidence": "low", "reason": "No entry", "evidence": []}, f)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    with patch.object(codex_cli.subprocess, "run", side_effect=fake_run):
        res = codex_cli.call("No market data", workdir=tmp_path)
    assert res.ok
    assert res.verdict["decision"] == "SKIP"


def test_codex_error_is_not_actionable(tmp_path):
    with patch.object(codex_cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "failed")):
        res = codex_cli.call("Snapshot", workdir=tmp_path)
    assert not res.ok and res.verdict is None


def test_codex_timeout_is_not_actionable(tmp_path):
    with patch.object(codex_cli.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 1)):
        res = codex_cli.call("Snapshot", workdir=tmp_path, timeout=1)
    assert not res.ok and res.timed_out
