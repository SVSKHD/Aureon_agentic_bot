"""v2.5.1 — advisors as p_win rankers + calibration."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5.common.claude_cli import validate
from aureon_mt5.calibration import calibrate, save, load, is_useful, summary_line


# ================================================================== schema validation
def test_ranker_schema_entry_no_decision():
    """Entry event with p_win and rationale but no decision → valid (RANK)."""
    v = validate({"p_win": 0.62, "rationale": "first touch", "risks": ["chop day"]}, "ENTER")
    assert v["decision"] == "RANK" and v["p_win"] == 0.62 and v["rationale"] == "first touch"
    assert v["risks"] == ["chop day"]


def test_ranker_schema_entry_with_decision():
    """Entry event with both p_win and decision (TAKE) → still valid."""
    v = validate({"p_win": 0.7, "decision": "TAKE", "side": "BUY", "reason": "confirmed", "confidence": "high"}, "ENTER")
    assert v["decision"] == "TAKE" and v["p_win"] == 0.7


def test_ranker_schema_pullback_unchanged():
    """Pullback events still require HOLD/TIGHTEN/CLOSE."""
    v = validate({"decision": "HOLD", "reason": "trend intact", "confidence": "medium", "p_win": 0.55}, "pullback")
    assert v["decision"] == "HOLD"
    with pytest.raises(ValueError):
        validate({"p_win": 0.5}, "pullback")  # pullback MUST have decision


def test_no_code_path_lets_advisor_block():
    """No entry validation accepts a blocking decision from advisor."""
    # "SKIP" is still accepted for backwards compat, but ranker mode doesn't require it
    v = validate({"decision": "SKIP", "reason": "chop", "confidence": "low"}, "ENTER")
    assert v["decision"] == "SKIP"  # accepted but the advisor never blocks — the caller decides


def test_invalid_p_win_becomes_none():
    v = validate({"p_win": "garbage", "rationale": "test"}, "ENTER")
    assert v["p_win"] is None


def test_confidence_defaults():
    v = validate({"p_win": 0.5, "rationale": "test"}, "ENTER")
    assert v["confidence"] == "medium"


def test_old_verdict_still_loads():
    """Old-style verdict with decision+side+confidence → still validates."""
    v = validate({"decision": "TAKE", "side": "BUY", "confidence": "high", "reason": "good", "p_win": 0.68,
                   "evidence": ["crosses_today"]}, "P")
    assert v["decision"] == "TAKE" and v["p_win"] == 0.68 and v["evidence"] == ["crosses_today"]


# ================================================================== calibration math
def test_calibrate_useful():
    rows = [{"p_win": i / 100, "points": 5.0 if i > 50 else -3.0, "graded": True} for i in range(100)]
    result = calibrate(rows, "claude", min_n=30)
    assert result.useful is True
    assert result.top_half_avg > result.bottom_half_avg
    assert result.n == 100
    assert len(result.buckets) > 0
    assert result.brier is not None


def test_calibrate_not_useful():
    # All same result regardless of p_win → not useful
    rows = [{"p_win": i / 100, "points": 2.0, "graded": True} for i in range(50)]
    result = calibrate(rows, "codex", min_n=30)
    assert result.useful is False  # top half ≈ bottom half


def test_calibrate_below_min_n():
    rows = [{"p_win": 0.5, "points": 5.0, "graded": True} for _ in range(10)]
    result = calibrate(rows, "claude", min_n=30)
    assert result.useful is False and result.n == 10


def test_calibrate_skips_ungraded():
    rows = [{"p_win": 0.5, "points": None, "graded": False}] * 50
    result = calibrate(rows, "claude", min_n=30)
    assert result.n == 0


# ================================================================== save/load/is_useful
def test_save_load_useful(tmp_path):
    rows = [{"p_win": i / 100, "points": 5.0 if i > 50 else -3.0, "graded": True} for i in range(100)]
    result = calibrate(rows, "claude", min_n=30)
    save(str(tmp_path), result)
    loaded = load(str(tmp_path), "claude")
    assert loaded is not None and loaded["useful"] is True
    assert is_useful(str(tmp_path), "claude")
    assert not is_useful(str(tmp_path), "codex")  # not saved


# ================================================================== summary_line
def test_summary_line():
    rows = [{"p_win": i / 100, "points": 5.0 if i > 50 else -3.0, "graded": True} for i in range(100)]
    result = calibrate(rows, "claude")
    line = summary_line(result)
    assert "top half" in line and "useful" in line


# ================================================================== brier score
def test_brier_perfect():
    # Perfect calibration: p_win=1 for wins, p_win=0 for losses
    rows = [{"p_win": 1.0, "points": 5.0, "graded": True}] * 20 + [{"p_win": 0.0, "points": -3.0, "graded": True}] * 20
    result = calibrate(rows, "test", min_n=30)
    assert result.brier == 0.0  # perfect
