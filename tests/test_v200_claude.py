"""v2.0.0 — Claude add-on for both modes: advises ema2050 (ENTER bar + pullbacks only, never raw CROSS / MULTI), the quantified
snapshot fields, the evidence contract (journaled), the mode-aware prompt, the /alert add-on line (budget-safe), and the Saturday
CLAUDE RULE PROPOSALS card + /claude-rules-approve (nothing changes without approval). The CLI is always mocked."""
import json, os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import pytest
from aureon_mt5 import alerts as A, broker, compare
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.claude_advisor import ClaudeAdvisor, build_snapshot
from aureon_mt5.common import claude_cli as cc
from aureon_mt5.common.timeutil import now_server
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.strategies import get_strategy
from aureon_mt5.strategies.ema2050 import journeys as J
from tests.test_claude_advisor import Cap, FakeCLI, ok, frame, verdicts
from tests.test_v200_alerts import TickMT5
from tests.test_v200_ema2050 import _bull, OFF

S2050 = get_strategy("ema2050"); S5080 = get_strategy("ema5080")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    def make(mode="advisory", reply=None, strategy=S2050, price=4000.5, max_calls=30, position=False):
        fake = TickMT5(price=price); fake.closed = not position
        monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
        cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.claude_mode = mode; cfg.claude_workdir = str(tmp_path / "wd"); cfg.claude_max_calls = max_calls
        n = Cap(); j = Journal(str(tmp_path)); cli = FakeCLI(reply)
        adv = ClaudeAdvisor(cfg, n, j, caller=cli, start=False)
        store = A.AlertStore(str(tmp_path))
        ag = SymbolAgent("XAUUSD", cfg, strategy, n, j, (), claude=adv, alerts=store); ag.off = OFF
        return types.SimpleNamespace(fake=fake, cfg=cfg, n=n, j=j, cli=cli, adv=adv, ag=ag, store=store)
    return make


def ok2(decision, **kw):
    v = ok(decision, **kw); v["evidence"] = kw.get("evidence", ["gap", "dist_to_fast_ema_pts"]); return v


# ------------------------------------------------------------------ advises both modes; ema2050 triggers
def test_advises_both_modes():
    cfg = Config(); cfg.claude_mode = "advisory"
    adv = ClaudeAdvisor(cfg, Cap(), Journal(os.devnull.replace("null", "tmp_claude_x")) if False else Journal("/tmp/claude-0/jl"), caller=FakeCLI(), start=False)
    assert adv.advises(S2050) and adv.advises(S5080)
    adv.mode = "review"; assert not adv.advises(S2050) and not adv.advises(S5080)


def test_ema2050_asked_only_at_the_enter_bar(rig):
    r = rig(mode="advisory", reply=ok2("TAKE", side="BUY"))
    df = _bull(n=40, touch_at=(11,))
    for last in (5, 7, 11):                                                     # CROSS, CONFIRMED, ENTER
        d = df.iloc[: last + 1].reset_index(drop=True)
        res = J.build(d, J.rules_for("XAUUSD"), OFF); res["df"] = d
        r.ag._detect_2050(res, d, int(d["time"].iloc[-1]), float(d["close"].iloc[-1]), float(d["ema20"].iloc[-1]), float(d["ema50"].iloc[-1]))
    jobs = []
    while not r.adv.q.empty(): jobs.append(r.adv.q.get_nowait())
    assert [j["event"] for j in jobs] == ["ENTER"] and jobs[0]["bar_t"] == int(df["time"].iloc[11]) and jobs[0]["side"] == "LONG"
    # a multi cross never asks
    g = [-2] * 5 + [0.5, 1.0, -0.5, -1.0, -2.0, -2.5, -3.0, -3.0]
    from tests.test_v200_ema2050 import frame as f2050
    d = f2050(g, closes=[4000.5] * 13, lows=[4002.0] * 13, highs=[4000.6] * 13)
    res = J.build(d, J.rules_for("XAUUSD"), OFF); res["df"] = d
    r.ag._detect_2050(res, d, int(d["time"].iloc[7]), 4000.5, 4000.0, 4000.5)    # the MULTI bar (index 7 == last of a 8-bar frame?)
    assert r.adv.q.empty()
    r.adv.process(jobs[0])
    v = verdicts(r.j)[-1]
    assert v["claude_event"] == "ENTER" and v["decision"] == "TAKE" and v["evidence"] == ["gap", "dist_to_fast_ema_pts"]
    assert isinstance(v["snapshot_fields"], dict) and "gap" in v["snapshot_fields"]
    assert r.cli.calls[0]["prompt"].startswith("MODE: ema2050 — fast EMA = EMA 20, slow EMA = EMA 50")


def test_ema2050_pullback_in_trade_asks_once(rig):
    r = rig(mode="advisory", reply=ok2("HOLD"), position=True, price=4206.0)
    r.fake.pos.price_open = 4200.0; r.fake.bid = 4206.0; r.fake.ask = 4206.3
    from tests.test_v200_ema2050 import guard
    guard(r.ag, r.fake, sgn=1)
    st = r.ag.state[1]; st["peak"] = 12.0
    pos = broker.positions("XAUUSD")[0]
    d = frame(price=4206.0); d = d.rename(columns={"ema_fast": "ema20", "ema_slow": "ema50"})
    assert r.adv.maybe_pullback(r.ag, pos, st, now_server(OFF) - 300, d, ["giveback"])
    assert not r.adv.maybe_pullback(r.ag, pos, st, now_server(OFF) - 300, d, ["giveback"])           # once per pullback
    job = r.adv.q.get_nowait()
    snap = job["snapshot"]
    for k in ("mae_so_far", "retrace_from_peak_pts", "retrace_pct", "bars_since_peak", "closed_through_slow_ema", "dist_to_sl"):
        assert k in snap, k
    assert snap["retrace_from_peak_pts"] == 6.0 and snap["retrace_pct"] == 50.0 and snap["mode"] == "ema2050"


# ------------------------------------------------------------------ snapshot fields, no account data
def test_snapshot_quantified_fields_no_secrets(rig):
    r = rig(mode="advisory", reply=ok2("TAKE"))
    df = _bull(n=40, touch_at=(11,)); res = J.build(df, J.rules_for("XAUUSD"), OFF); res["df"] = df; r.ag.last_res = res
    s = build_snapshot(r.ag, "ENTER", "LONG", int(df["time"].iloc[-1]), df)
    json.dumps(s)
    for k in ("crosses_today", "bars_since_cross", "confirm_state", "dist_to_fast_ema_pts", "swing_against_last6_pts", "atr20", "day_pnl_pts",
              "trades_today", "ema_fast", "ema_slow", "ema_names", "ema20", "ema50"):
        assert k in s, k
    assert s["bars_since_cross"] == 34 and s["confirm_state"].startswith("confirmed") and s["ema_names"] == {"fast": "EMA 20", "slow": "EMA 50"}
    flat = json.dumps(s).lower()
    for bad in ("balance", "login", "account", "webhook", "token", "password", "equity", "ticket"):
        assert bad not in flat
    r5 = rig(mode="advisory", reply=ok2("TAKE"), strategy=S5080)
    s5 = build_snapshot(r5.ag, "P", "LONG", now_server(OFF) - 300, frame())
    assert s5["ema50"] == s5["ema_fast"] and s5["ema80"] == s5["ema_slow"] and "crosses_today" in s5 and "ema20" not in s5


def test_evidence_contract_validated_and_capped():
    base = {"decision": "TAKE", "side": "BUY", "confidence": "high", "reason": "ok"}
    assert cc.validate({**base, "evidence": ["gap", " atr20 ", ""]}, "ENTER")["evidence"] == ["gap", " atr20 "]
    assert cc.validate({**base, "evidence": "gap, atr20"}, "ALERT")["evidence"] == ["gap", "atr20"]
    assert cc.validate(base, "P")["evidence"] == []
    assert len(cc.validate({**base, "evidence": [f"f{i}" for i in range(30)]}, "CROSS")["evidence"]) == cc.MAX_EVIDENCE
    r = cc.parse_output(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps({**base, "evidence": ["gap"]})}), "ENTER")
    assert r.ok and r.verdict["evidence"] == ["gap"]


# ------------------------------------------------------------------ /alert add-on line
def _fire_alert(r, df):
    res = J.build(df, J.rules_for("XAUUSD"), OFF); res["df"] = df; r.ag.last_df = df; r.ag.last_res = res; r.ag.last_bar = int(df["time"].iloc[-1])
    r.ag._refresh_snapshot(True)
    r.store.arm("XAUUSD", 4000.0, 4010.0, "lvl")
    r.fake.bid, r.fake.ask = 3999.5, 3999.8; r.fake.tick_time = now_server(OFF)
    r.ag._check_alerts(df, df, int(df["time"].iloc[-1]))


def test_alert_add_on_line_in_review_mode_edits_card(rig):
    r = rig(mode="review", reply=ok2("TAKE", side="BUY", reason="pullback at EMA 20", evidence=["confirm_state", "dist_to_fast_ema_pts"]))
    assert r.ag.claude is None and r.ag.claude_any is r.adv                   # review: no trade hooks, but the alert line
    edits = []
    r.n.card_edit_hook = lambda key, upd: edits.append((key, upd)) or True
    _fire_alert(r, _bull(n=12, touch_at=(11,)))
    job = r.adv.q.get_nowait()
    assert job["type"] == "alert" and job["event"] == "ALERT" and job["card_key"] == "alert:XAUUSD:A1" and job["snapshot"]["alert"]["id"] == "A1"
    assert job["snapshot"]["agent_verdict"]["side"] == "LONG" and job["snapshot"]["event"] == "alert" and job["agent_side"] == "LONG"
    r.adv.process(job)
    assert r.cli.calls[0]["model"] == r.cfg.claude_entry_model and r.cli.calls[0]["event"] == "ALERT"
    assert edits and edits[0][0] == "alert:XAUUSD:A1"
    blk = edits[0][1]["CLAUDE VERDICT"]
    assert "CLAUDE: LONG · TAKE" in blk and "evidence: confirm_state" in blk and "AGREES with agent ✅" in blk
    assert r.store.get("A1")["claude_side"] == "LONG" and r.store.get("A1")["claude_agreed"] is True
    v = verdicts(r.j)[-1]
    assert v["claude_event"] == "ALERT" and v["alert_id"] == "A1" and v["evidence"] == ["confirm_state", "dist_to_fast_ema_pts"]


def test_alert_add_on_skips_silently_when_budget_used(rig):
    r = rig(mode="advisory", reply=ok2("TAKE"), max_calls=0)
    _fire_alert(r, _bull(n=12, touch_at=(11,)))
    assert any("ALERT REACHED" in t for t in r.n.titles) and "— budget used" in " ".join(r.n.bodies)
    assert r.adv.q.empty() and r.cli.calls == [] and not any("ALERT VERDICT" in t for t in r.n.titles)      # no job, no call, no card


def test_alert_add_on_off_mode_makes_no_call(tmp_path, monkeypatch):
    fake = TickMT5(price=4000.5); monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.claude_mode = "off"
    adv = ClaudeAdvisor.create(cfg, Cap(), Journal(str(tmp_path)))
    assert adv is None
    ag = SymbolAgent("XAUUSD", cfg, S2050, Cap(), Journal(str(tmp_path)), (), claude=adv, alerts=A.AlertStore(str(tmp_path))); ag.off = OFF
    r = types.SimpleNamespace(ag=ag, store=ag.alerts, fake=fake, n=ag.notify)
    _fire_alert(r, _bull(n=12, touch_at=(11,)))
    assert ag.alerts.get("A1")["status"] == "fired"                            # the card still fires; no Claude anywhere


# ------------------------------------------------------------------ Saturday rule proposals + approval
def _res_with_rows(points_by_bar):
    rows = [compare.Row("XAUUSD", "ENTER", "LONG", b, "London", status="GRADED", grade="WIN10" if p > 0 else "STOP", points=p) for b, p in points_by_bar.items()]
    return types.SimpleNamespace(rows=rows, since=0, until=10**9, rules_hash="abcd1234")


def _verdict(bar, dec, **fields):
    return {"t": bar, "event": "claude_verdict", "claude_event": "ENTER", "symbol": "XAUUSD", "bar": bar, "decision": dec, "stale": False,
            "snapshot_fields": fields, "evidence": list(fields)}


def test_rule_proposals_from_wrong_calls_and_approval(tmp_path):
    # wrong TAKEs (lost) all had crosses_today >= 6; the right TAKEs had 2 → one proposal with support 3 vs 0
    recs = [_verdict(1, "TAKE", crosses_today=6, gap=1.0), _verdict(2, "TAKE", crosses_today=7, gap=1.2), _verdict(3, "TAKE", crosses_today=8, gap=0.9),
            _verdict(4, "TAKE", crosses_today=2, gap=2.0), _verdict(5, "TAKE", crosses_today=2, gap=2.5)]
    res = _res_with_rows({1: -12.0, 2: -12.0, 3: -6.0, 4: 10.0, 5: 15.0})
    props = compare.rule_proposals(recs, res, "EMA 20/50")
    assert props and props[0]["field"] == "crosses_today" and props[0]["op"] == ">=" and props[0]["n_wrong"] == 3 and props[0]["n_right"] == 0
    assert props[0]["line"] == "prefer SKIP when crosses_today >= 6 (EMA 20/50)" and props[0]["n"] == 1 and len(props) <= 3
    assert compare.rule_proposals(recs[:1], res, "EMA 20/50") == []            # < MIN_SUPPORT wrong calls -> nothing
    rules = tmp_path / "claude_rules.md"; rules.write_text("rules\n", encoding="utf-8")
    compare.save_proposals(str(tmp_path), "2026-W41", props)
    card = compare.proposals_card("2026-W41", props, 3, "abcd1234")
    assert "1." in card["description"] and "/claude-rules-approve" in card["description"] and "unchanged" in card["fields"][1]["value"]
    assert rules.read_text(encoding="utf-8") == "rules\n"                      # nothing changes without approval
    line = compare.approve_proposal(1, str(tmp_path), rules_path=str(rules))
    text = rules.read_text(encoding="utf-8")
    assert text.endswith(line + "\n") and line.startswith("- [approved 20") and "prefer SKIP when crosses_today >= 6" in line
    with pytest.raises(ValueError):
        compare.approve_proposal(1, str(tmp_path), rules_path=str(rules))      # not twice
    with pytest.raises(ValueError):
        compare.approve_proposal(9, str(tmp_path), rules_path=str(rules))
    assert compare.load_proposals(str(tmp_path))["proposals"][0]["approved"]


def test_saturday_posts_proposals_card_without_claude_call(tmp_path):
    cfg = Config(); cfg.log_dir = str(tmp_path); cfg.mode = "ema2050"
    j = Journal(str(tmp_path)); n = Cap()
    res = types.SimpleNamespace(rows=[], since=0, until=100, rules_hash="x")
    props = compare.rule_proposals_tick(cfg, j, n, res, "2026-W41", "EMA 20/50")
    assert props == [] and any("CLAUDE RULE PROPOSALS" in t for t in n.titles) and compare.load_proposals(str(tmp_path))["week"] == "2026-W41"
