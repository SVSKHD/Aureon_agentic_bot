"""v2.3.3 — the July 2026 ema2050 ENTER snapshots through the CURRENT rules prompt, answered by the real `claude -p` once and cached.

  tests/fixtures/ema2050_jul2026_snapshots.jsonl     the snapshots (python -m aureon_mt5.claude_replay export … 2026-07)
  tests/fixtures/claude_rules_replay_jul2026.jsonl   the live verdicts, one row per (bar_t, rules hash, model) — committed

Normal runs read the cache only. When the rules file changes (new hash) the cached verdicts no longer apply and the test FAILS
until it is refreshed with one live run:  AUREON_PROMPT_TEST=1 python -m pytest tests/test_claude_rules_replay.py
(then commit the updated cache). A mock CLI cannot stand in: the point is what the model does with these rules."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5.common import claude_cli
from aureon_mt5.common.rules import cites_listed_reason, rules_path
from aureon_mt5.compare import rules_hash

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOTS = os.path.join(ROOT, "tests", "fixtures", "ema2050_jul2026_snapshots.jsonl")
CACHE = os.path.join(ROOT, "tests", "fixtures", "claude_rules_replay_jul2026.jsonl")
MAX_SKIP_RATE = 0.35
FORBIDDEN_IN_REASON = ("1.5", "atr")
LIVE = os.environ.get("AUREON_PROMPT_TEST") == "1"


def load_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def cached_verdicts(snaps: list[dict], cache_path: str, rules_h: str, model: str, ask=None) -> tuple[list[dict], list[int]]:
    """→ (verdict per snapshot or None, bars still missing). With `ask(snapshot) -> CliResult`, missing bars are asked and appended."""
    have = {(r["bar_t"], r["rules_hash"], r["model"]): r["verdict"] for r in load_jsonl(cache_path)}
    out, missing = [], []
    for row in snaps:
        key = (row["bar_t"], rules_h, model)
        if key not in have and ask is not None:
            res = ask(row["snapshot"])
            assert res.ok, f"live call failed for bar {row['bar_t']}: {res.error}"
            have[key] = res.verdict
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"bar_t": row["bar_t"], "rules_hash": rules_h, "model": model, "verdict": res.verdict}) + "\n")
        out.append(have.get(key))
        if key not in have:
            missing.append(row["bar_t"])
    return out, missing


def check(verdicts: list[dict]) -> list[str]:
    """Problems with a set of verdicts under the v2.3.3 rules ([] = fine)."""
    problems = []
    skips = [v for v in verdicts if v["decision"] == "SKIP"]
    if verdicts and len(skips) / len(verdicts) > MAX_SKIP_RATE:
        problems.append(f"SKIP rate {len(skips)}/{len(verdicts)} > {MAX_SKIP_RATE:.0%}")
    for v in verdicts:
        if any(bad in (v.get("reason") or "").lower() for bad in FORBIDDEN_IN_REASON):
            problems.append(f"reason leaks a detector threshold: {v['reason']!r}")
    problems += [f"SKIP without a listed reason: {v['reason']!r}" for v in skips if not cites_listed_reason(v["reason"])]
    problems += [f"no p_win: {v['reason']!r}" for v in verdicts if v.get("p_win") is None]
    return problems


# ------------------------------------------------------------------ the helpers themselves (always run)
def _v(dec, reason, p=0.7):
    return {"decision": dec, "side": "BUY", "tighten_to": None, "confidence": "medium", "reason": reason, "evidence": [], "p_win": p}


def test_check_flags_each_problem():
    ok = [_v("TAKE", "no listed reason applies")] * 7 + [_v("SKIP", "|dist_to_fast_ema_pts| > 5")] * 3
    assert check(ok) == []
    assert any("SKIP rate" in p for p in check([_v("TAKE", "x")] * 6 + [_v("SKIP", "swing_against_last6_pts >= 8")] * 4))
    assert any("leaks" in p for p in check([_v("TAKE", "touch within 1.5 of EMA20")]))
    assert any("leaks" in p for p in check([_v("TAKE", "swing below 2x ATR")]))
    assert any("listed reason" in p for p in check([_v("TAKE", "x")] * 9 + [_v("SKIP", "gap not growing")]))
    assert any("p_win" in p for p in check([_v("TAKE", "x", p=None)]))


def test_cache_asks_once_then_reads(tmp_path):
    snaps = [{"bar_t": t, "snapshot": {"event": "ENTER", "bar": t}} for t in (100, 200)]
    calls = []
    def ask(snap):
        calls.append(snap["bar"]); return claude_cli.CliResult(True, verdict=_v("TAKE", "ok"))
    cache = str(tmp_path / "c.jsonl")
    v1, miss1 = cached_verdicts(snaps, cache, "h1", "opus", ask)
    v2, miss2 = cached_verdicts(snaps, cache, "h1", "opus")                    # no ask: read only
    assert calls == [100, 200] and v1 == v2 and miss1 == miss2 == []
    v3, miss3 = cached_verdicts(snaps, cache, "h2", "opus")                    # rules changed → stale
    assert v3 == [None, None] and miss3 == [100, 200]


# ------------------------------------------------------------------ the July snapshots through the current rules (live once, then cached)
@pytest.mark.skipif(not os.path.exists(SNAPSHOTS), reason="tests/fixtures/ema2050_jul2026_snapshots.jsonl not in the repo yet")
def test_july_snapshots_through_current_rules():
    from aureon_mt5.claude_advisor import ClaudeAdvisor
    from aureon_mt5.claude_replay import _NoJournal, _Quiet
    from aureon_mt5.config import Config
    cfg = Config()
    adv = ClaudeAdvisor(cfg, _Quiet(), _NoJournal(), start=False)
    snaps = load_jsonl(SNAPSHOTS)
    rh = rules_hash(rules_path())
    ask = (lambda snap: adv._invoke(adv.prompt_for(snap), cfg.claude_entry_model, "ENTER")) if LIVE else None
    verdicts, missing = cached_verdicts(snaps, CACHE, rh, cfg.claude_entry_model, ask)
    assert not missing, (f"{len(missing)} of {len(snaps)} snapshots have no cached verdict for rules {rh} / {cfg.claude_entry_model} — "
                         "run once with AUREON_PROMPT_TEST=1 and commit tests/fixtures/claude_rules_replay_jul2026.jsonl")
    assert check(verdicts) == []
