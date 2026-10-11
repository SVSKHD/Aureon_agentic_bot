"""v2.3.0 — Codex parity with Claude. Codex is shown, never applied; two verdicts on one card; compare's fourth column;
/advisor-report; run.py --codex cache; usage rows. Every CLI is mocked."""
import ast, json, os, subprocess, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5 import advisor_board, broker, compare
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.claude_advisor import ClaudeAdvisor
from aureon_mt5.codex_advisor import CodexAdvisor
from aureon_mt5.common import claude_cli as cc, codex_cli, rules
from aureon_mt5.common.timeutil import now_server
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.strategies import get_strategy
from tests.test_claude_advisor import Cap, FakeCLI, frame, ok
from tests.test_v181 import FakeMT5, guard

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeCodex:
    """Stands in for codex_cli.call (its keyword signature)."""
    def __init__(self, reply=None):
        self.reply, self.calls = reply, []
    def __call__(self, prompt, *, event, binary, workdir, timeout, model=None, free_text=False):
        self.calls.append({"prompt": prompt, "event": event, "model": model, "free_text": free_text})
        r = self.reply(event) if callable(self.reply) else self.reply
        if isinstance(r, dict):
            return cc.CliResult(True, verdict=cc.validate(r, event), latency_s=0.8)
        return r


@pytest.fixture(autouse=True)
def _board():
    advisor_board.clear(); yield; advisor_board.clear()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    def make(codex_mode="advisory", claude_mode=None, reply=None, claude_reply=None, direction="long", entry=4200.0, sl=4194.0, price=4208.0,
             strategy="ema5080"):
        fake = FakeMT5(direction, entry, sl, price)
        monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", 3.0)
        cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.codex_mode = codex_mode; cfg.codex_workdir = str(tmp_path / "cx")
        cfg.claude_mode = claude_mode or "off"; cfg.claude_workdir = str(tmp_path / "cl")
        n = Cap(); j = Journal(str(tmp_path)); cli = FakeCodex(reply)
        cx = CodexAdvisor(cfg, n, j, caller=cli, start=False)
        cl = ClaudeAdvisor(cfg, n, j, caller=FakeCLI(claude_reply), start=False) if claude_mode else None
        ag = SymbolAgent("XAUUSD", cfg, get_strategy(strategy), n, j, (), claude=cl, codex=cx)
        ag.off = 3.0
        return types.SimpleNamespace(fake=fake, cfg=cfg, n=n, j=j, cli=cli, cx=cx, cl=cl, ag=ag)
    return make


def rows(j, event="codex_verdict"):
    return [r for r in j.read(0) if r["event"] == event]


def pullback_job(r, adv, peak=12.0, bar_t=None):
    pos = broker.positions("XAUUSD")
    guard(r.ag, r.fake, frame(price=r.fake.price), sgn=1, new_bar=False)
    st = r.ag.state[1]; st["peak"] = peak
    bar_t = bar_t if bar_t is not None else now_server(3.0) - 300
    assert adv.maybe_pullback(r.ag, pos[0], st, bar_t, frame(price=r.fake.price), ["giveback"])
    return adv.q.get_nowait()


# ------------------------------------------------------------------ Codex never applies (property test over every verdict kind)
VERDICTS = [ok("HOLD"), ok("TIGHTEN", tighten_to=4204.0), ok("TIGHTEN", tighten_to=4199.0), ok("CLOSE"), ok("CLOSE", confidence="high"),
            ok("TIGHTEN", tighten_to=4207.5, confidence="low")]


@pytest.mark.parametrize("v", VERDICTS, ids=lambda v: f"{v['decision']}-{v['tighten_to']}-{v['confidence']}")
@pytest.mark.parametrize("price", [4208.0, 4195.0])               # in profit / in loss
@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("closed", [False, True])
def test_codex_never_moves_sl_or_closes(rig, v, price, stale, closed):
    r = rig(reply=v, price=price)
    def boom(*a, **k):
        raise AssertionError("Codex reached the guardian")
    r.ag._move_sl = boom; r.ag._close = boom
    job = pullback_job(r, r.cx)
    if stale:
        job["bar_t"] -= 10 * 300
    r.cx.process(job)
    sl_before = r.fake.pos.sl
    positions = [] if closed else broker.positions("XAUUSD")
    r.cx.drain(r.ag, positions)
    assert r.fake.pos.sl == sl_before
    row = rows(r.j)[-1]
    assert row["acted"] is False and row["source"] == "codex" and row["codex_event"] == "pullback"
    assert any("CODEX PULLBACK VERDICT" in t for t in r.n.titles)


def test_codex_has_no_path_to_the_guardian():
    src = open(os.path.join(ROOT, "aureon_mt5", "codex_advisor.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not called & {"_move_sl", "_close", "modify_sl", "close_position", "place_market", "order_send"}
    imports = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    assert "broker" not in imports and not any("broker" in (n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom))
    assert CodexAdvisor.MODES == ("off", "review", "advisory")


@pytest.mark.parametrize("mode", ["review", "advisory"])
def test_manage_never_reachable(rig, mode):
    r = rig(codex_mode=mode)
    assert r.cx.manage is False
    r.cfg.codex_mode = "manage"
    assert CodexAdvisor.mode_of(r.cfg) == "off"                    # unknown → off (fail closed)


def test_codex_apply_returns_false_for_every_kind(rig):
    r = rig()
    pos = broker.positions("XAUUSD")[0]
    for v in VERDICTS:
        job = {"verdict": cc.validate(v, "pullback"), "bar_t": now_server(3.0) - 300, "ticket": pos["ticket"], "side": "LONG",
               "event": "pullback", "symbol": "XAUUSD", "trigger": "t"}
        assert r.cx.apply_pullback(r.ag, job, pos, {}) is False


# ------------------------------------------------------------------ same triggers, same snapshot, same contract
@pytest.mark.parametrize("strategy", ["ema2050", "ema5080"])
def test_same_triggers_as_claude(rig, strategy):
    r = rig(strategy=strategy, claude_mode="advisory")
    df = frame(); cf, cs = r.ag.S.ema_cols; df[cf] = df["ema_fast"]; df[cs] = df["ema_slow"]
    bar_t = int(df["time"].iloc[-1])
    for kind in ("enter", "pullback", "P", "cross", "reentry", "late", "multi"):
        assert r.cx.request_entry(r.ag, kind, "LONG", bar_t, df) == r.cl.request_entry(r.ag, kind, "LONG", bar_t, df), kind
    a, b = [], []
    while not r.cx.q.empty(): a.append(r.cx.q.get_nowait())
    while not r.cl.q.empty(): b.append(r.cl.q.get_nowait())
    assert [j["event"] for j in a] == [j["event"] for j in b] and a
    strip = lambda snap: {k: v for k, v in snap.items() if k != "minutes_to_news"}
    assert [strip(j["snapshot"]) for j in a] == [strip(j["snapshot"]) for j in b]          # same build_snapshot
    assert r.ag.advisors() == [r.cl, r.cx]
    if strategy == "ema5080":
        return
    r.cx.request_entry(r.ag, "enter", "LONG", bar_t, df, card_key="k")
    job = r.cx.q.get_nowait()
    assert job["event"] == "ENTER" and "account" not in json.dumps(job["snapshot"], default=str).lower()
    assert r.cx.prompt_for(job["snapshot"]).startswith("MODE: ema2050")
    assert rules.read_rules().strip() in r.cx.prompt_for(job["snapshot"])


def test_review_mode_is_not_attached(rig):
    r = rig(codex_mode="review")
    assert r.ag.codex is None and r.ag.codex_any is r.cx
    assert r.cx.attachment(r.ag)["state"] == "REVIEW"


def test_once_per_pullback(rig):
    r = rig(reply=ok("HOLD"))
    pullback_job(r, r.cx)
    pos = broker.positions("XAUUSD")[0]; st = r.ag.state[1]
    assert not r.cx.maybe_pullback(r.ag, pos, st, now_server(3.0) - 300, frame(price=r.fake.price), ["giveback"])


def test_fail_closed_and_stale(rig):
    r = rig(reply=cc.CliResult(False, error="invalid verdict: decision MAYBE"))
    df = frame(); bar_t = int(df["time"].iloc[-1])
    r.cx.request_entry(r.ag, "P", "LONG", bar_t, df, card_key="k"); r.cx.process(r.cx.q.get_nowait())
    assert rows(r.j)[-1]["decision"] is None and not any("CODEX ·" in t for t in r.n.titles)
    r2 = rig(reply=ok("TAKE"))
    r2.cx.request_entry(r2.ag, "P", "LONG", bar_t - 10 * 300, df, card_key="k2"); r2.cx.process(r2.cx.q.get_nowait())
    assert rows(r2.j)[-1]["stale"] is True and any("⌛ STALE" in b for b in r2.n.bodies)


# ------------------------------------------------------------------ two verdicts on one card
def test_verdict_line_format():
    v = {"decision": "TAKE", "confidence": "medium", "reason": "pullback to EMA 20", "evidence": ["gap", "atr20"]}
    assert advisor_board.verdict_line("claude", v) == 'CLAUDE: TAKE · medium · "pullback to EMA 20" · evidence gap, atr20'
    assert advisor_board.verdict_line("codex", dict(v, decision="SKIP", confidence="low")).startswith('CODEX : SKIP · low · "')


@pytest.mark.parametrize("cx_dec,tag", [("SKIP", "SPLIT"), ("TAKE", "AGREE")])
def test_entry_card_gets_both_lines_and_tag(rig, cx_dec, tag):
    r = rig(claude_mode="advisory", reply=ok(cx_dec, side="BUY"), claude_reply=ok("TAKE", side="BUY"))
    added, upserts = [], []
    r.n.claude_edit_hook = lambda key, fld: added.append((key, fld)) or True
    r.n.card_edit_hook = lambda key, upd: upserts.append((key, upd)) or True
    df = frame(); bar_t = int(df["time"].iloc[-1])
    for adv in r.ag.advisors():
        adv.request_entry(r.ag, "P", "LONG", bar_t, df, card_key="ask-1"); adv.process(adv.q.get_nowait())
    assert [f["name"] for _, f in added] == ["Claude", "Codex"]
    assert added[0][1]["value"].startswith("CLAUDE: TAKE") and added[1][1]["value"].startswith(f"CODEX : {cx_dec}")
    assert upserts == [("ask-1", {"Advisors": advisor_board.tag_text(tag)})]


def test_entry_card_webhook_fallback_carries_both_lines(rig):
    r = rig(claude_mode="advisory", reply=ok("SKIP", reason="chop"), claude_reply=ok("TAKE", reason="clean"))
    df = frame(); bar_t = int(df["time"].iloc[-1])
    for adv in r.ag.advisors():
        adv.request_entry(r.ag, "P", "LONG", bar_t, df, card_key="ask-2"); adv.process(adv.q.get_nowait())
    codex_card = next(b for t, b in zip(r.n.titles, r.n.bodies) if "CODEX · P PRE-CROSS" in t)
    assert "CLAUDE: TAKE" in codex_card and "CODEX : SKIP" in codex_card and "SPLIT" in codex_card


def test_pullback_card_two_lines(rig):
    r = rig(claude_mode="advisory", reply=ok("TIGHTEN", tighten_to=4204.0), claude_reply=ok("HOLD"))
    bar_t = now_server(3.0) - 300                                              # both advisors are asked on the same closed bar
    jobs = [pullback_job(r, adv, bar_t=bar_t) for adv in r.ag.advisors()]
    for adv, job in zip(r.ag.advisors(), jobs):
        adv.process(job)
    for adv in r.ag.advisors():
        adv.drain(r.ag, broker.positions("XAUUSD"))
    body = next(b for t, b in zip(r.n.titles, r.n.bodies) if "CODEX PULLBACK VERDICT" in t)
    assert "CLAUDE: HOLD" in body and "CODEX : TIGHTEN" in body and "SPLIT" in body and "never applied" in body
    assert r.fake.pos.sl == 4194.0                                             # advisory Claude + Codex: nothing applied


def test_alert_card_codex_block_and_tag(rig):
    r = rig(claude_mode="review", reply=ok("TAKE", side="BUY"), claude_reply=ok("TAKE", side="BUY"))
    edits = []
    r.n.card_edit_hook = lambda key, upd: edits.append(upd) or True
    for adv, name in ((r.cl, "CLAUDE"), (r.cx, "CODEX")):
        job = {"type": "alert", "event": "ALERT", "symbol": "XAUUSD", "side": "LONG", "bar_t": now_server(3.0) - 300, "off": 3.0,
               "snapshot": {"mode": "ema5080"}, "card_key": "alert-card", "agent": r.ag, "alert_id": "A7", "agent_block": "AGENT: LONG",
               "agent_side": "LONG", "title": "", "model": adv.entry_model, "mode_name": "ema5080", "display": "EMA 50/80"}
        adv.process(job)
    assert "CLAUDE VERDICT" in edits[0] and "CODEX VERDICT" in edits[1] and edits[1]["CODEX VERDICT"].startswith("**CODEX: LONG · TAKE")
    assert edits[1]["ADVISORS"] == advisor_board.tag_text("AGREE")


# ------------------------------------------------------------------ usage rows
def test_usage_rows_codex_ledger(rig, tmp_path):
    r = rig(reply=lambda e: cc.CliResult(True, verdict=cc.validate(ok("SKIP"), e), latency_s=1.0,
                                         usage={"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 50}))
    df = frame(); bar_t = int(df["time"].iloc[-1])
    r.cx.request_entry(r.ag, "P", "LONG", bar_t, df); r.cx.process(r.cx.q.get_nowait())
    r.cli.reply = ok("SKIP")                                                   # a CLI build that exposes no usage
    r.cx.request_entry(r.ag, "P", "LONG", bar_t + 300, df); r.cx.process(r.cx.q.get_nowait())
    lines = [json.loads(x) for x in open(tmp_path / "codex_usage.jsonl", encoding="utf-8")]
    assert [x["source"] for x in lines] == ["codex", "codex"] and lines[0]["input_tokens"] == 120 and lines[0]["kind"] == "entry"
    assert lines[1]["input_tokens"] is None and lines[1]["total_cost_usd"] is None
    assert not os.path.exists(tmp_path / "claude_usage.jsonl")
    from aureon_mt5.claude_usage import summarize
    s = summarize(str(tmp_path), "today", source="codex")
    assert s["calls"] == 2 and s["input_tokens"] == 120 and s["verdicts"] == {"SKIP": 2}


def test_codex_budget_is_its_own(rig, tmp_path):
    r = rig(reply=ok("SKIP")); r.cfg.codex_max_calls = 1; r.cx.budget.limit = 1
    df = frame(); bar_t = int(df["time"].iloc[-1])
    for i in range(2):
        r.cx.request_entry(r.ag, "P", "LONG", bar_t + i * 300, df); r.cx.process(r.cx.q.get_nowait())
    assert len(r.cli.calls) == 1 and os.path.exists(tmp_path / "codex_budget.json") and not os.path.exists(tmp_path / "claude_budget.json")
    assert any("CODEX BUDGET USED" in t for t in r.n.titles)


def test_parse_usage_from_codex_json_events():
    out = "\n".join([json.dumps({"type": "thread.started"}), "not json",
                     json.dumps({"type": "turn.completed", "usage": {"input_tokens": 900, "cached_input_tokens": 400, "output_tokens": 60}})])
    usage, cost, model = codex_cli.parse_usage(out)
    assert usage == {"input_tokens": 900, "cache_read_input_tokens": 400, "output_tokens": 60} and cost is None
    assert codex_cli.parse_usage("")[0] is None


# ------------------------------------------------------------------ CLI: same contract, CLI login only
def test_codex_cli_pullback_schema_and_no_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test"); monkeypatch.setenv("CODEX_API_KEY", "x")
    seen = {}
    def fake_run(cmd, **kw):
        seen["schema"] = json.load(open(cmd[cmd.index("--output-schema") + 1], encoding="utf-8"))
        seen["env"] = kw["env"]
        with open(cmd[cmd.index("--output-last-message") + 1], "w", encoding="utf-8") as f:
            json.dump({"decision": "TIGHTEN", "side": None, "tighten_to": 4204.5, "confidence": "medium", "reason": "tiring",
                       "evidence": ["retrace_pct"], "p_win": None}, f)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"usage": {"input_tokens": 5, "output_tokens": 2}}), "")
    res = codex_cli.call("snap", event="pullback", workdir=tmp_path, run=fake_run)
    assert res.ok and res.verdict["decision"] == "TIGHTEN" and res.verdict["tighten_to"] == 4204.5 and res.usage["input_tokens"] == 5
    assert seen["schema"]["properties"]["decision"]["enum"] == ["HOLD", "TIGHTEN", "CLOSE"]
    assert "OPENAI_API_KEY" not in seen["env"] and "CODEX_API_KEY" not in seen["env"]


def test_codex_cli_free_text_and_json_flag_fallback(tmp_path):
    calls = []
    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if "--json" in cmd:
            return subprocess.CompletedProcess(cmd, 2, "", "error: unexpected argument '--json' found")
        with open(cmd[cmd.index("--output-last-message") + 1], "w", encoding="utf-8") as f:
            f.write("1) worked\n2) cost\n3) watch")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    res = codex_cli.call("events", event="review", workdir=tmp_path, free_text=True, run=fake_run)
    assert res.ok and res.text.startswith("1) worked") and res.usage is None
    assert len(calls) == 2 and "--output-schema" not in calls[0] and "--json" not in calls[1]


def test_codex_cli_invalid_verdict_fails_closed(tmp_path):
    def fake_run(cmd, **kw):
        with open(cmd[cmd.index("--output-last-message") + 1], "w", encoding="utf-8") as f:
            json.dump({"decision": "TIGHTEN", "side": None, "tighten_to": None, "confidence": "low", "reason": "x", "evidence": []}, f)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    res = codex_cli.call("snap", event="pullback", workdir=tmp_path, run=fake_run)
    assert not res.ok and res.verdict is None and "TIGHTEN without tighten_to" in res.error


# ------------------------------------------------------------------ shared rules file
def test_shared_rules_file_and_alias():
    assert os.path.basename(rules.rules_path()) == "advisor_rules.md"
    text = rules.read_rules()
    assert "== MODE ema2050" in text and "== MODE ema5080" in text
    legacy = os.path.join(ROOT, "prompts", "claude_rules.md")
    assert os.path.lexists(legacy)
    if os.path.islink(legacy):
        assert os.readlink(legacy) == "advisor_rules.md" and open(legacy, encoding="utf-8").read() == text


def test_codex_proposal_approval_tags_source(tmp_path):
    shared = tmp_path / "advisor_rules.md"; shared.write_text("rules\n", encoding="utf-8")
    compare.save_proposals(str(tmp_path), "2026-W41", [{"n": 1, "line": "prefer SKIP when crosses_today >= 6 (EMA 20/50)",
                                                        "support": "3 wrong TAKE calls vs 0 right"}], "codex")
    line = compare.approve_proposal(1, str(tmp_path), rules_path=str(shared), source="codex")
    assert line.startswith("- [codex] [approved 20") and shared.read_text(encoding="utf-8").endswith(line + "\n")
    assert compare.load_proposals(str(tmp_path), "claude") == {}               # the Claude proposals file is separate
    c = compare.proposals_card("2026-W41", [], 0, "abcd", "codex")
    assert c["title"].startswith("AUREON · MT5 · CODEX RULE PROPOSALS")


# ------------------------------------------------------------------ compare: fourth column + split attribution
# i → codex verdict, on top of tests.test_compare's fixture (claude: 0 TAKE, 1 TAKE, 2 TAKE, 3 SKIP, 4 SKIP, 6 TAKE (OPEN), 7 SKIP (UNGRADED))
CODEX = {0: "SKIP", 1: "TAKE", 2: "SKIP", 3: "SKIP", 4: "TAKE", 6: "SKIP", 7: "SKIP"}


def compare_res():
    from tests import test_compare as tc
    recs = tc.journal_records()
    for i, k, side, *_ in tc.TABLE:
        if i in CODEX:
            recs.append({"t": tc.T0 + i * 3600 + 340, "event": "codex_verdict", "source": "codex", "codex_event": tc.EV[k], "symbol": "XAUUSD",
                         "bar": tc.bar(i), "decision": CODEX[i], "side": "BUY" if side == "LONG" else "SELL", "stale": False, "acted": False,
                         "model": "", "confidence": "high" if i in (1, 3) else "low"})
    return compare.compute(recs, tc.graded(), tc.SINCE, tc.UNTIL, tc.OFF), recs


def test_compare_fourth_column_metrics():
    res, _ = compare_res()
    m = res.metrics
    assert m["codex_take"].n == 2 and m["codex_take"].total == 22.0           # i1 +20, i4 +2
    assert m["codex_skip"].n == 3 and m["codex_skip"].total == -2.0           # i0 +10, i2 −6, i3 −6 (i6 OPEN, i7 UNGRADED excluded)
    assert m["claude_take"].n == 3 and m["detector"].n == 7                    # Claude column unchanged by the Codex rows
    card = compare.card(res)
    assert "Codex · TAKE" in card["description"] and any(f["name"] == "Claude vs Codex" for f in card["fields"])
    bd = compare.breakdown_card(res)["description"]
    assert "Cx n" in bd


def test_split_attribution():
    res, _ = compare_res()
    assert res.h2h == {"both": 7, "agree": 3, "agree_pct": 42.9, "split": 4, "claude_right": 1, "claude_pts": 10.0,
                       "codex_right": 2, "codex_pts": 8.0, "tie": 0, "ungraded": 1}
    v = next(f["value"] for f in compare.card(res)["fields"] if f["name"] == "Claude vs Codex")
    assert "agree 43%" in v and "Claude right 1 (+10.0 pts)" in v and "Codex right 2 (+8.0 pts)" in v


def test_claude_only_install_keeps_old_table():
    from tests import test_compare as tc
    res = tc.res()
    assert "Codex" not in compare.table(res) and not any(f["name"] == "Claude vs Codex" for f in compare.card(res)["fields"])


def test_advisor_report_sections_and_head_to_head(tmp_path):
    from aureon_mt5 import claude_report
    res, recs = compare_res()
    j = types.SimpleNamespace(read=lambda since, until=None: recs)
    cfg = Config(); cfg.log_dir = str(tmp_path)
    r = claude_report.advisor_report(cfg, {}, j, "week", gather=lambda *a, **k: res)
    names = [f["name"] for f in r["fields"]]
    for label in ("Claude", "Codex"):
        for part in ("verdicts", "agreement", "SKIP precision · TAKE expectancy", "calibration by confidence", "usage"):
            assert f"{label} · {part}" in names, (label, part)
    assert "Head-to-head · Claude vs Codex" in names and r["h2h"]["split"] == 4
    assert r["stats"]["codex"]["skip_precision"] == round(2 / 3, 3)            # i2, i3 lost of 3 graded SKIPs
    assert r["stats"]["codex"]["confidence"]["high"] == {"n": 2, "correct": 2} and "never applied" in " ".join(f["value"] for f in r["fields"])
    assert claude_report.report is not None and r["title"] == "AUREON · ADVISOR REPORT · WEEK"


# ------------------------------------------------------------------ run.py --codex: own cache, zero calls on a cache hit
@pytest.mark.parametrize("advisor", ["claude", "codex"])
def test_replay_cache_hit_zero_calls(tmp_path, advisor):
    from aureon_mt5 import claude_replay as CR
    from tests.test_v210_claude_replay import CountingCLI as ClaudeReplayCLI, day_frame, J, RULES, OFF, S2050
    cfg = Config(); cfg.claude_workdir = str(tmp_path / "w"); cfg.codex_workdir = str(tmp_path / "cx")
    df = day_frame(); res = J.build(df, RULES, OFF)
    def make():
        cli = FakeCodex(ok("TAKE", side=None)) if advisor == "codex" else ClaudeReplayCLI()
        sr = CR.ShadowReplay(cfg, S2050, "XAUUSD", OFF, (), out_dir=str(tmp_path / "out"), caller=cli, sleep=lambda s: None, advisor=advisor)
        sr.score_day("2026-10-07", df, res["journeys"])
        return sr, cli
    sr, cli = make()
    n = len(res["journeys"])
    assert len(cli.calls) == n and sr.path.endswith(f"{advisor}_replay_ema2050_XAUUSD.jsonl")
    sr2, cli2 = make()
    assert cli2.calls == [] and sr2.res.cache_hits == n
    text, path = CR.write_report(sr2, sr2.score(), "t")
    assert path.endswith(f"{advisor}_replay_ema2050_XAUUSD.md") and f"Detector-filtered-by-{sr2.label}" in text


def test_replay_combined_both_agree_and_split_list(tmp_path):
    from aureon_mt5 import claude_replay as CR
    from tests.test_v210_claude_replay import CountingCLI as ClaudeReplayCLI, day_frame, J, RULES, OFF, S2050
    cfg = Config(); cfg.claude_workdir = str(tmp_path / "w"); cfg.codex_workdir = str(tmp_path / "cx")
    df = day_frame(); res = J.build(df, RULES, OFF)
    js = sorted(res["journeys"], key=lambda j: j.entry_time)
    first = js[0].entry_time
    codex_cli_ = FakeCodex(lambda e: ok("SKIP") if len(codex_cli_.calls) == 1 else ok("TAKE"))
    runs = {}
    for adv, cli in (("claude", ClaudeReplayCLI()), ("codex", codex_cli_)):
        sr = CR.ShadowReplay(cfg, S2050, "XAUUSD", OFF, (), out_dir=str(tmp_path / "out"), caller=cli, sleep=lambda s: None, advisor=adv)
        sr.score_day("2026-10-07", df, res["journeys"]); runs[adv] = sr
    c = CR.combined(runs["claude"], runs["codex"])
    closed = [j for j in js if j.exit_reason != "open"]
    assert c["metrics"]["detector"].n == len(closed) and c["metrics"]["claude"].n == len(closed)
    assert c["metrics"]["codex"].n == len(closed) - 1 and c["metrics"]["both"].n == len(closed) - 1
    assert [d["bar_t"] for d in c["splits"]] == [first] and c["splits"][0]["claude"] == "TAKE" and c["splits"][0]["codex"] == "SKIP"
    text = CR.combined_text(c, "t")
    for key in ("Detector · all", "Detector-filtered-by-Claude", "Detector-filtered-by-Codex", "Detector-filtered-by-BOTH-agree",
                "SKIP precision Claude", "SKIP precision Codex", "SPLIT (1 of"):
        assert key in text, key


# ------------------------------------------------------------------ /codex · /codex-status · /status
def test_codex_card_per_agent_and_usage_block(rig, tmp_path):
    from aureon_mt5 import bot
    r = rig(claude_mode="advisory")
    card = bot.advisor_card(r.cx, r.cfg, {"XAUUSD": r.ag}, "codex", login_line="`codex login status`: authenticated")
    vals = {f["name"]: f["value"] for f in card["fields"]}
    assert card["title"] == "AUREON · MT5 · CODEX" and "never applied" in card["description"]
    assert vals["Per agent"].startswith("XAUUSD · EMA 50/80 · ATTACHED (advisory)") and "authenticated" in vals["Header"]
    off = bot.advisor_card(None, r.cfg, {"XAUUSD": r.ag}, "codex")
    assert "AUREON_CODEX=off" in off["description"] and "DETACHED (mode=off)" in off["fields"][1]["value"]
    assert [f["name"] for f in bot.usage_fields(str(tmp_path), r.j, "codex")] == ["Today", "Week", "Month"]
    st = {f["name"]: f["value"] for f in bot.status_card("XAUUSD", r.ag, r.cfg, claude=r.cl, codex=r.cx)["fields"]}
    assert st["Claude"] == "ATTACHED (advisory)" and st["Codex"] == "ATTACHED (advisory)"


def test_codex_review_card(rig):
    r = rig(reply=lambda e: cc.CliResult(True, text="1) fine\n2) cost\n3) watch", latency_s=2.0))
    r.j.log("signal", symbol="XAUUSD", side="LONG", signal_kind="P")
    text = r.cx.daily_review(force=True)
    assert text.startswith("1) fine") and r.cli.calls[-1]["free_text"] is True and r.cli.calls[-1]["model"] is None
    assert any(t.startswith("AUREON · MT5 · CODEX REVIEW") for t in r.n.titles)
    assert rows(r.j, "codex_review")[-1]["model"] == "CLI default"
