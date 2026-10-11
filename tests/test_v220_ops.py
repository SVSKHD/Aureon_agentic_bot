"""v2.2.0 operational helpers; no live MT5 or Discord connectivity required."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from aureon_mt5 import ops, crashes
from aureon_mt5.claude_usage import append, summarize
from aureon_mt5.common.claude_cli import CliResult
from aureon_mt5.claude_report import summarize_rows
from aureon_mt5.compare import Row


def test_admin_role_rejects_unconfigured_and_accepts_matching():
    user = SimpleNamespace(roles=[SimpleNamespace(id=27)])
    assert not ops.admin_allowed(user, "")
    assert not ops.admin_allowed(user, "28")
    assert ops.admin_allowed(user, "27")


def test_mode_rejects_open_positions_and_unverified_snapshots(tmp_path):
    ag = SimpleNamespace(snapshot={"at": 100, "positions": [{"ticket": 7}], "connected": True})
    assert ops.positions_block_mode_change({"XAUUSD": ag})
    ag.snapshot = {"at": 100, "positions": [], "connected": True}
    assert not ops.positions_block_mode_change({"XAUUSD": ag})
    ag.snapshot = {"at": None}
    assert ops.positions_block_mode_change({"XAUUSD": ag})
    env = tmp_path / ".env"
    env.write_text("AUREON_MODE=ema5080\n", encoding="utf-8")
    assert ops.write_mode(env, "ema2050", "XAUUSD,XAGUSD") == ["XAUUSD", "XAGUSD"]
    assert "AUREON_MODE=ema2050" in env.read_text(encoding="utf-8")
    assert "AUREON_SYMBOLS=XAUUSD,XAGUSD" in env.read_text(encoding="utf-8")


def test_restart_stops_joins_posts_and_execs(monkeypatch):
    monkeypatch.delenv("AUREON_RESTART_REASON", raising=False)
    ag = SimpleNamespace(stop=Mock(), join=Mock())
    journal, notify, execv = Mock(), Mock(), Mock()
    ops.restart_process({"XAUUSD": ag}, journal, notify, "operator",
                        "test", argv=["main.py", "--mode", "ema2050"],
                        executor=execv)
    notify.card.assert_called_once()
    journal.log.assert_called_once()
    ag.stop.assert_called_once()
    ag.join.assert_called_once_with(timeout=5)
    assert execv.called
    assert "AUREON_RESTART_REASON" in __import__("os").environ
    monkeypatch.delenv("AUREON_RESTART_REASON", raising=False)


def test_crash_three_repeats_pauses_and_logs():
    crashes.paused_symbols.clear()
    crashes._repeats.clear()
    ag = SimpleNamespace(
        symbol="XAUUSD", health={"status": "running", "last_bar": "10:00"},
        snapshot={"positions": []}, journal=Mock(), notify=Mock())
    try:
        for i in range(3):
            try: raise ValueError("synthetic crash")
            except ValueError as exc: paused = crashes.report(ag, exc)
        assert paused
        assert not crashes.should_restart("XAUUSD")
        assert ag.journal.log.call_count == 3
        assert ag.notify.card.call_count >= 4
    finally:
        crashes.paused_symbols.clear()
        crashes._repeats.clear()


def test_usage_persists_and_sums(tmp_path):
    v = CliResult(True, verdict={"decision": "TAKE"}, latency_s=2.5,
                  model="example-model", usage={
                      "input_tokens": 100, "output_tokens": 20,
                      "cache_read_input_tokens": 10,
                      "cache_creation_input_tokens": 5}, total_cost_usd=0.031)
    append(str(tmp_path), "entry", v)
    append(str(tmp_path), "entry", v)
    data = summarize(str(tmp_path), "today")
    assert data["calls"] == 2
    assert data["input_tokens"] == 200
    assert data["output_tokens"] == 40
    assert data["verdicts"]["TAKE"] == 2
    assert data["cost_usd"] == pytest.approx(.062)


def test_report_graded_math():
    rows = [
        Row("XAUUSD", "P", "LONG", 1, "London", status="GRADED", points=8, claude="TAKE", me="TAKEN"),
        Row("XAUUSD", "P", "LONG", 2, "London", status="GRADED", points=-5, claude="SKIP", me="SKIPPED"),
        Row("XAUUSD", "P", "LONG", 3, "London", status="GRADED", points=4, claude="SKIP", me="TAKEN"),
    ]
    x = summarize_rows(rows)
    assert x["verdicts"] == {"TAKE": 1, "SKIP": 2}
    assert x["skip_precision"] == .5
    assert x["take_expectancy"] == 8
    assert x["detector_expectancy"] == pytest.approx(7 / 3, rel=0.01)
    assert x["agreement_trader_pct"] == pytest.approx(66.7)
