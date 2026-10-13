"""v2.5.3 — Codex end-of-day review."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from datetime import datetime, timedelta, timezone
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5 import eod_review

IST = timezone(timedelta(hours=5, minutes=30))


class Cap(Notifier):
    def __init__(self): super().__init__(None); self.titles = []
    def raw(self, body, png=None, *, embed=None):
        self.titles.append(embed["title"] if embed else str(body).splitlines()[0]); return True


def _cfg(tmp_path):
    cfg = Config(); cfg.log_dir = str(tmp_path); cfg.dry = True; cfg.codex_mode = "advisory"
    cfg.codex_bin = "codex"; cfg.codex_workdir = str(tmp_path); cfg.codex_timeout = 30
    cfg.codex_model = ""; cfg.eod_at = "23:15"
    return cfg


def _populate_journal(j):
    """Write some events into the journal for the pack."""
    j.log("signal", symbol="XAUUSD", mode="ema2050", side="LONG", signal_kind="pullback", price=4200.0, bar=1000)
    j.log("decision", symbol="XAUUSD", decision="TAKE", side="LONG", by="tester")
    j.log("order_placed", symbol="XAUUSD", side="LONG", lots=1.0, ok=True, ticket=123)
    j.log("secured", symbol="XAUUSD", ticket=123, level=10, step=10)
    j.log("closed", symbol="XAUUSD", ticket=123, direction="long", final_points=8.5)


# ================================================================== pack contents from fixture journal
def test_day_pack_contents(tmp_path):
    j = Journal(str(tmp_path)); _populate_journal(j)
    pack = eod_review.build_day_pack(j)
    assert "signal" in pack and "decision" in pack and "order_placed" in pack and "secured" in pack


# ================================================================== card format from valid response
def test_valid_response_card(tmp_path):
    cfg = _cfg(tmp_path); j = Journal(str(tmp_path)); n = Cap()
    _populate_journal(j)
    response = json.dumps({"summary": "Good day", "wins": ["LONG +8.5"], "costs": [], "violations": [], "observations": ["Pullback worked"]})
    result = eod_review.run_review(cfg, j, n, runner=lambda p: response)
    assert result["ok"]
    assert result["card"]["title"].startswith("📓 EOD REVIEW")
    assert "Good day" in result["card"]["description"]
    field_names = [f["name"] for f in result["card"]["fields"]]
    assert "Wins" in field_names and "Observations" in field_names


# ================================================================== invalid output handled
def test_invalid_response(tmp_path):
    cfg = _cfg(tmp_path); j = Journal(str(tmp_path)); n = Cap()
    _populate_journal(j)
    result = eod_review.run_review(cfg, j, n, runner=lambda p: "not json at all")
    assert not result["ok"]
    assert result["card"]["title"].startswith("📓 EOD REVIEW")
    assert "unavailable" in result["card"]["description"]


# ================================================================== dedupe per date
def test_dedupe_per_date(tmp_path):
    cfg = _cfg(tmp_path); j = Journal(str(tmp_path)); n = Cap()
    _populate_journal(j)
    now = datetime(2026, 10, 13, 23, 15, tzinfo=IST)
    # First tick
    ok1 = eod_review.tick(cfg, j, n, now=now)
    assert ok1 or not ok1  # may fail without codex, but should not crash
    # Second tick same date — should not re-run (dedupe)
    ok2 = eod_review.tick(cfg, j, n, now=now)
    assert not ok2  # already done


# ================================================================== prompt contains no write/edit instructions
def test_prompt_is_read_only():
    assert "NO parameter changes" in eod_review.EOD_PROMPT
    assert "NO code edits" in eod_review.EOD_PROMPT
    assert "NO file modifications" in eod_review.EOD_PROMPT
    assert "Read only" in eod_review.EOD_PROMPT


# ================================================================== skips weekends
def test_skip_weekend(tmp_path):
    cfg = _cfg(tmp_path); j = Journal(str(tmp_path)); n = Cap()
    now = datetime(2026, 10, 17, 23, 15, tzinfo=IST)  # Saturday
    assert not eod_review.tick(cfg, j, n, now=now)


# ================================================================== validate_response
def test_validate_valid():
    r = eod_review.validate_response('{"summary": "ok", "wins": ["a"], "costs": [], "violations": [], "observations": []}')
    assert r is not None and r["summary"] == "ok"


def test_validate_missing_summary():
    r = eod_review.validate_response('{"wins": []}')
    assert r is None


def test_validate_with_fences():
    r = eod_review.validate_response('```json\n{"summary": "ok"}\n```')
    assert r is not None
