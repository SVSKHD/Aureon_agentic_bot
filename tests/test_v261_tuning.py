"""v2.6.1 — bounded threshold tuning + /approve."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5 import tuning
from aureon_mt5.journal import Journal


# ================================================================== bounds enforced
def test_within_bounds(tmp_path):
    result = tuning.propose(str(tmp_path), "pre_stop", 6.0, 7.0, "stop hit rate high")
    assert isinstance(result, dict) and result["status"] == "pending"


def test_outside_bounds(tmp_path):
    result = tuning.propose(str(tmp_path), "pre_stop", 6.0, 20.0, "too far")
    assert isinstance(result, str) and "outside bounds" in result


# ================================================================== step limit
def test_step_limit(tmp_path):
    result = tuning.propose(str(tmp_path), "pre_stop", 6.0, 9.0, "big jump")
    assert isinstance(result, str) and "step" in result


def test_step_at_limit(tmp_path):
    result = tuning.propose(str(tmp_path), "pre_stop", 6.0, 7.0, "one step")
    assert isinstance(result, dict)


# ================================================================== entry-rule params can't be proposed
def test_entry_rule_not_tunable(tmp_path):
    result = tuning.propose(str(tmp_path), "confirm_bars", 3, 4, "test")
    assert isinstance(result, str) and "not tunable" in result


def test_confirm_gap_not_tunable(tmp_path):
    result = tuning.propose(str(tmp_path), "confirm_gap", 1.5, 2.0, "test")
    assert isinstance(result, str) and "not tunable" in result


# ================================================================== approve applies + persists
def test_approve_persists(tmp_path):
    j = Journal(str(tmp_path))
    tuning.propose(str(tmp_path), "ride_step", 5.0, 6.0, "early close left pts")
    props = tuning.load_proposals(str(tmp_path))
    pid = props[-1]["id"]
    result = tuning.approve(str(tmp_path), pid, j)
    assert isinstance(result, dict) and result["status"] == "approved"
    overrides = tuning.load_overrides(str(tmp_path))
    assert overrides["ride_step"] == 6.0
    applied = [r for r in j.read(0) if r["event"] == "tuning_applied"]
    assert len(applied) == 1


# ================================================================== reject discards
def test_reject(tmp_path):
    tuning.propose(str(tmp_path), "ride_step", 5.0, 6.0, "test")
    props = tuning.load_proposals(str(tmp_path))
    result = tuning.reject(str(tmp_path), props[-1]["id"])
    assert isinstance(result, dict) and result["status"] == "rejected"


# ================================================================== expiry discards
def test_expiry(tmp_path):
    tuning.propose(str(tmp_path), "ride_step", 5.0, 6.0, "test")
    props = tuning.load_proposals(str(tmp_path))
    props[-1]["expires"] = int(time.time()) - 1  # already expired
    tuning.save_proposals(str(tmp_path), props)
    tuning.expire_stale(str(tmp_path))
    props = tuning.load_proposals(str(tmp_path))
    assert props[-1]["status"] == "expired"


# ================================================================== revert
def test_revert(tmp_path):
    j = Journal(str(tmp_path))
    tuning.propose(str(tmp_path), "ride_step", 5.0, 6.0, "test")
    tuning.approve(str(tmp_path), 1, j)
    assert tuning.load_overrides(str(tmp_path)).get("ride_step") == 6.0
    assert tuning.revert(str(tmp_path), "ride_step", j)
    assert "ride_step" not in tuning.load_overrides(str(tmp_path))


# ================================================================== weekly limit
def test_weekly_limit(tmp_path):
    tuning.propose(str(tmp_path), "pre_stop", 6.0, 7.0, "test1")
    tuning.propose(str(tmp_path), "ride_step", 5.0, 6.0, "test2")
    result = tuning.propose(str(tmp_path), "chop_crosses", 5, 6, "test3")
    assert isinstance(result, str) and "limit" in result


# ================================================================== format
def test_format_proposal(tmp_path):
    tuning.propose(str(tmp_path), "pre_stop", 6.0, 7.0, "evidence here")
    p = tuning.load_proposals(str(tmp_path))[-1]
    text = tuning.format_proposal(p)
    assert "pre_stop" in text and "6.0" in text and "7.0" in text
