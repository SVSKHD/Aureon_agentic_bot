"""v2.3.2 — ema2050 advisor rules: the detector already validated confirmation / touch / hour / news; default TAKE; SKIP only for a
listed reason cited verbatim (|dist| > 5 · swing >= 8 · crosses_today >= 5 before 11:00 server); no ATR rule.

Fixture: tests/fixtures/ema2050_jul2026_snapshots.jsonl — the July 2026 ENTER snapshots, made from a shadow-replay cache with
  python run.py --mode ema2050 --source csv --csv xauusd_m5.csv --no-nfp --from 2026-07-01 --to 2026-07-31 --claude entry
  python -m aureon_mt5.claude_replay export out/claude_replay_ema2050_XAUUSD.jsonl tests/fixtures/ema2050_jul2026_snapshots.jsonl 2026-07
The live prompt test calls the real `claude -p` (your CLI login) and runs only with AUREON_PROMPT_TEST=1."""
import json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5.common import rules
from aureon_mt5.common.rules import SKIP_REASONS_2050, cites_listed_reason, ema2050_skip_reasons

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "ema2050_jul2026_snapshots.jsonl")
MAX_SKIP_RATE = 0.35


def section_2050() -> str:
    text = rules.read_rules()
    return text[text.index("== MODE ema2050"):text.index("== MODE ema5080")]


def load_fixture() -> list[dict]:
    with open(FIXTURE, encoding="utf-8") as f:
        return [json.loads(line)["snapshot"] for line in f if line.strip()]


# ------------------------------------------------------------------ the rules file says exactly the corrected rules
def test_rules_file_ema2050_section():
    s = section_2050()
    for reason in SKIP_REASONS_2050:
        assert f'"{reason}"' in s, reason
    assert "Default TAKE. A SKIP must cite one listed reason. Produce p_win (0-1) on every verdict." in s
    assert "Never re-test the pullback; the 1.5-pt\ntouch tolerance is the detector's." in s
    assert "There is no ATR rule. Never compare swing_against_last6_pts to atr20; it matters only if >= 8." in s
    assert "crosses_today >= 5 is a SKIP reason only when bar_time is before 11:00 server." in s
    assert "80%" in s and "30%" in s and '"p_win": 0-1' in rules.read_rules()
    assert s.count("1.5") == 1                                                    # only in the detector sentence
    for gone in ("good-entry limit", "Good ENTER", "gap growth", "gap growing", "2 × atr20", "2x ATR", "Be sceptical"):
        assert gone.lower() not in s.lower(), gone
    assert not re.search(r"atr20\s*[<>]|2\s*×\s*atr20", s)                        # no ATR threshold left
    assert "Good ENTER" not in s and "Be sceptical" not in s                      # the old extra criteria are gone
    assert "When unsure: ema2050 entries → TAKE" in rules.read_rules()


# ------------------------------------------------------------------ the listed reasons, at their boundaries
BASE = {"dist_to_fast_ema_pts": 1.0, "swing_against_last6_pts": 2.0, "crosses_today": 1, "session": "ny", "bar_time_server": "15:00",
        "atr20": 0.4}


@pytest.mark.parametrize("over,reasons", [
    ({}, []),
    ({"dist_to_fast_ema_pts": 5.0}, []), ({"dist_to_fast_ema_pts": 5.01}, [SKIP_REASONS_2050[0]]),
    ({"dist_to_fast_ema_pts": -6.0}, [SKIP_REASONS_2050[0]]),
    ({"swing_against_last6_pts": 7.99}, []), ({"swing_against_last6_pts": 8.0}, [SKIP_REASONS_2050[1]]),
    ({"swing_against_last6_pts": 3.0, "atr20": 0.5}, []),                        # 6 × atr20 — no ATR rule
    ({"crosses_today": 7, "session": "ny"}, []),
    ({"crosses_today": 5, "session": "asia", "bar_time_server": "03:00"}, [SKIP_REASONS_2050[2]]),
    ({"crosses_today": 5, "session": "london", "bar_time_server": "10:55"}, [SKIP_REASONS_2050[2]]),
    ({"crosses_today": 5, "session": "london", "bar_time_server": "11:00"}, []),
    ({"crosses_today": 6, "session": "ny", "bar_time_server": "09:30"}, [SKIP_REASONS_2050[2]]),   # v2.3.3: the hour decides, not the session
    ({"crosses_today": 4, "session": "asia"}, []),
])
def test_listed_reasons(over, reasons):
    assert ema2050_skip_reasons({**BASE, **over}) == reasons


def test_citation_check():
    assert cites_listed_reason("SKIP: swing_against_last6_pts >= 8 (9.1)")
    assert not cites_listed_reason("chop, gap not growing")


def test_snapshot_has_server_time():
    from tests.test_claude_advisor import frame
    from aureon_mt5.claude_advisor import build_snapshot
    from aureon_mt5.common.timeutil import server_str
    import types
    from aureon_mt5.strategies import get_strategy
    S = get_strategy("ema5080")
    ag = types.SimpleNamespace(S=S, off=3.0, symbol="XAUUSD", news=(), journal=types.SimpleNamespace(read=lambda *a: []))
    df = frame(); bar_t = int(df["time"].iloc[-1])
    assert build_snapshot(ag, "ENTER", "LONG", bar_t, df)["bar_time_server"] == server_str(bar_t, "%H:%M")


def test_replay_cache_keeps_snapshots_for_export(tmp_path):
    from aureon_mt5 import claude_replay as CR
    from aureon_mt5.config import Config
    from tests.test_v210_claude_replay import CountingCLI, day_frame, J, RULES, OFF, S2050
    cfg = Config(); cfg.claude_workdir = str(tmp_path / "w")
    df = day_frame(); res = J.build(df, RULES, OFF)
    sr = CR.ShadowReplay(cfg, S2050, "XAUUSD", OFF, (), out_dir=str(tmp_path), caller=CountingCLI(), sleep=lambda s: None)
    sr.score_day("2026-10-07", df, res["journeys"])
    n = CR.export_snapshots(sr.path, str(tmp_path / "snaps.jsonl"))
    rows = [json.loads(x) for x in open(tmp_path / "snaps.jsonl", encoding="utf-8")]
    assert n == len(res["journeys"]) == len(rows) and all(r["snapshot"]["event"] == "ENTER" and "bar_time_server" in r["snapshot"] for r in rows)


# ------------------------------------------------------------------ the July snapshots through the new rules
needs_fixture = pytest.mark.skipif(not os.path.exists(FIXTURE), reason="tests/fixtures/ema2050_jul2026_snapshots.jsonl not in the repo yet")


@needs_fixture
def test_july_snapshots_listed_reasons_skip_rate():
    snaps = load_fixture()
    skips = [s for s in snaps if ema2050_skip_reasons(s)]
    assert snaps and len(skips) / len(snaps) <= MAX_SKIP_RATE, f"{len(skips)}/{len(snaps)} SKIP by the listed reasons"


# The live prompt check lives in tests/test_claude_rules_replay.py (live once, cached per rules hash).
