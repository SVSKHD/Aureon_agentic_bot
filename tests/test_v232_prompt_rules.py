"""v2.3.2 — ema2050 advisor rules: the detector already validated confirmation / touch / hour / news; default TAKE; SKIP only for a
listed reason cited verbatim (|dist| > 5 · swing >= 8 · crosses_today >= 5 in Asia / London before 11:00 server); no ATR rule.

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
    assert "default is TAKE" in s and "VERBATIM" in s and "80%" in s and "30%" in s
    assert "Do NOT re-test" in s and "not yours" in s
    assert "atr20" in s and "never compare it to atr20" in s                    # mentioned only to forbid it
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


@needs_fixture
@pytest.mark.skipif(os.environ.get("AUREON_PROMPT_TEST") != "1", reason="live prompt test: set AUREON_PROMPT_TEST=1 (calls `claude -p`)")
def test_july_snapshots_through_the_live_prompt():
    from aureon_mt5.claude_advisor import ClaudeAdvisor
    from aureon_mt5.claude_replay import _NoJournal, _Quiet
    from aureon_mt5.config import Config
    cfg = Config()
    adv = ClaudeAdvisor(cfg, _Quiet(), _NoJournal(), start=False)
    verdicts = []
    for snap in load_fixture():
        r = adv._invoke(adv.prompt_for(snap), cfg.claude_entry_model, "ENTER")
        assert r.ok, r.error
        verdicts.append(r.verdict)
    skips = [v for v in verdicts if v["decision"] == "SKIP"]
    uncited = [v["reason"] for v in skips if not cites_listed_reason(v["reason"])]
    assert len(skips) / len(verdicts) <= MAX_SKIP_RATE, f"{len(skips)}/{len(verdicts)} SKIP"
    assert not uncited, f"SKIPs without a listed reason: {uncited}"
