"""v2.8.0 Part 1 — Claude reads the chart; /claude-read. The CLI is always mocked; nothing reaches MT5."""
import ast, json, os, subprocess, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5 import broker
from aureon_mt5.codex_advisor import CodexAdvisor
from aureon_mt5.common import claude_cli as cc
from aureon_mt5.common.timeutil import now_server
from tests.test_claude_advisor import FakeCLI, frame, ok, rig, verdicts   # noqa: F401  (rig is a fixture)
from tests.test_claude_cli import V, out
from tests.test_v181 import guard

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ImageCLI(FakeCLI):
    """Records the image each call carried; marks the result like the real CLI does."""
    def __init__(self, reply=None):
        super().__init__(reply); self.images = []
    def __call__(self, prompt, model, *, event, **kw):
        self.calls.append({"prompt": prompt, "model": model, "event": event}); self.images.append(kw.get("image"))
        r = self.reply(event) if callable(self.reply) else self.reply
        if isinstance(r, dict):
            return cc.CliResult(True, verdict=cc.validate(r, event), latency_s=1.2, image=bool(kw.get("image")))
        return r


def read_v(decision, side=None, **kw):
    return {"decision": decision, "side": side, "confidence": "medium", "rationale": kw.get("reason", "structure intact"),
            "risks": kw.get("risks", ["news in 40 min"]), "evidence": ["ema order"]}


def png_file(tmp_path, name="chart.png"):
    p = tmp_path / name; p.write_bytes(b"\x89PNG fake"); return str(p)


def fake_run(stdout):
    seen = {}
    def run(cmd, input=None, **kw):
        seen.update(cmd=cmd, input=input, env=kw.get("env"), cwd=kw.get("cwd"))
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    return run, seen


# ------------------------------------------------------------------ (a)-(c) claude_cli.call with / without an image
def test_call_with_image_stages_file_and_allows_read(tmp_path, monkeypatch):
    monkeypatch.setenv("AUREON_SECRET_THING", "x")
    run, seen = fake_run(out(json.dumps(V)))
    wd = tmp_path / "wd"
    r = cc.call("PROMPT", "opus", event="P", bin="claude", workdir=str(wd), run=run, image=png_file(tmp_path))
    assert r.ok and r.image
    assert (wd / "snaps" / "chart.png").exists()
    assert seen["cmd"][-2:] == ["--allowedTools", "Read"]
    assert seen["input"].startswith("PROMPT") and "snaps/chart.png" in seen["input"] and "ONLY for structure" in seen["input"]
    assert not any(k.startswith("AUREON_") for k in seen["env"])


def test_call_without_image_is_byte_identical(tmp_path):
    run, seen = fake_run(out(json.dumps(V)))
    r = cc.call("PROMPT", "opus", event="P", bin="claude", workdir=str(tmp_path), run=run)
    assert seen["cmd"][1:] == ["-p", "--output-format", "json", "--model", "opus"] and seen["input"] == "PROMPT"
    assert r.image is False


def test_call_missing_image_falls_back_to_text(tmp_path):
    run, seen = fake_run(out(json.dumps(V)))
    r = cc.call("PROMPT", "opus", event="P", bin="claude", workdir=str(tmp_path), run=run, image=str(tmp_path / "nope.png"))
    assert "--allowedTools" not in seen["cmd"] and seen["input"] == "PROMPT" and r.ok and not r.image


# ------------------------------------------------------------------ (d) READ schema
@pytest.mark.parametrize("v,ok_", [
    ({"decision": "HOLD"}, True), ({"decision": "EXIT", "side": "BUY"}, True), ({"decision": "SKIP"}, True),
    ({"decision": "ENTER", "side": "SELL"}, True), ({"decision": "ENTER"}, False),
    ({"decision": "TIGHTEN", "tighten_to": 4100}, False), ({"decision": "TAKE", "side": "BUY"}, False),
])
def test_read_schema(v, ok_):
    if ok_:
        assert cc.validate(v, "READ")["decision"] == v["decision"]
    else:
        with pytest.raises(ValueError):
            cc.validate(v, "READ")


# ------------------------------------------------------------------ (e)-(g) entry jobs carry the chart
def test_entry_jobs_carry_png_for_p_and_cross_not_reentry(rig, tmp_path):
    r = rig(mode="advisory", reply=ok("TAKE", side="BUY"))
    cli = ImageCLI(ok("TAKE", side="BUY")); r.adv.caller = cli
    png = png_file(tmp_path)
    df = frame(); bar_t = int(df["time"].iloc[-1])
    for kind in ("P", "cross", "reentry"):
        assert r.adv.request_entry(r.ag, kind, "LONG", bar_t, df, card_key=f"k{kind}", png=png)
        job = r.adv.q.get_nowait()
        assert ("png" in job) == (kind != "reentry")
        r.adv.process(job)
    assert cli.images == [png, png, None]
    assert [v["image"] for v in verdicts(r.j)] == [True, True, False]
    rows = [json.loads(l) for l in open(os.path.join(r.cfg.log_dir, "claude_usage.jsonl"), encoding="utf-8")]
    assert [x["image"] for x in rows] == [True, True, False]


def test_image_events_empty_means_no_png(rig, tmp_path):
    r = rig(mode="advisory", reply=ok("TAKE", side="BUY")); r.cfg.claude_image_events = ()
    df = frame()
    r.adv.request_entry(r.ag, "P", "LONG", int(df["time"].iloc[-1]), df, card_key="k", png=png_file(tmp_path))
    assert "png" not in r.adv.q.get_nowait()


def test_codex_accepts_and_drops_image(rig, tmp_path):
    r = rig(mode="advisory")
    r.cfg.codex_mode = "advisory"
    seen = []
    def caller(prompt, **kw):
        seen.append(kw); return cc.CliResult(True, verdict=cc.validate(ok("SKIP"), kw["event"]), latency_s=0.5)
    cx = CodexAdvisor(r.cfg, r.n, r.j, caller=caller, start=False)
    assert not cx.wants_image("P")
    df = frame()
    cx.process({"type": "entry", "event": "P", "symbol": "XAUUSD", "side": "LONG", "bar_t": int(df["time"].iloc[-1]), "off": 3.0,
                "snapshot": {"mode": "ema5080"}, "card_key": "k", "agent": r.ag, "model": "", "mode_name": "ema5080",
                "display": "x", "png": png_file(tmp_path)})
    assert seen and "image" not in seen[0]


# ------------------------------------------------------------------ (h)-(l) read_now
def _with_frame(r, price):
    r.ag.last_df = frame(price=price)
    r.ag._refresh_snapshot(True)


def test_read_now_in_position_hold_shown_with_chart(rig):
    r = rig(mode="manage", price=4206.0)
    cli = ImageCLI(read_v("HOLD", "BUY")); r.adv.caller = cli
    guard(r.ag, r.fake, frame(price=4206.0), sgn=1, new_bar=False)
    sends_before = len(r.fake.sends)
    _with_frame(r, 4206.0)
    res = r.adv.read_now(r.ag)
    assert res["ok"] and res["verdict"]["decision"] == "HOLD" and res["png"] and os.path.exists(res["png"])
    assert "OPERATOR READ" in cli.calls[0]["prompt"] and cli.images[0] == res["png"]
    snap = res["snapshot"]
    assert snap["position_open"] and snap["entry"] == 4200.0 and snap["sl"] and snap["side"] == "BUY"
    assert len(r.fake.sends) == sends_before                                  # nothing sent to MT5 by the read
    row = [x for x in r.j.read(0) if x["event"] == "claude_read"][-1]
    assert row["acted"] is False and row["decision"] == "HOLD" and row["image"] is True


def test_read_now_flat_enter_or_skip(rig):
    r = rig(mode="advisory"); r.fake.closed = True
    r.adv.caller = ImageCLI(read_v("ENTER", "SELL"))
    _with_frame(r, 4200.0)
    res = r.adv.read_now(r.ag)
    assert res["ok"] and res["verdict"]["decision"] == "ENTER" and res["verdict"]["side"] == "SELL" and not res["snapshot"]["position_open"]


@pytest.mark.parametrize("flat,decision", [(False, "ENTER"), (False, "SKIP"), (True, "HOLD"), (True, "EXIT")])
def test_read_now_contradictions_dropped(rig, flat, decision):
    r = rig(mode="advisory"); r.fake.closed = flat
    r.adv.caller = ImageCLI(read_v(decision, "BUY"))
    _with_frame(r, 4206.0)
    res = r.adv.read_now(r.ag)
    assert res["verdict"] is None and decision in res["error"]


def test_read_now_no_frame_and_budget(rig):
    r = rig(mode="advisory", max_calls=0)
    assert r.adv.read_now(r.ag)["error"] == "no cached closed-bar frame yet"
    _with_frame(r, 4200.0)
    res = r.adv.read_now(r.ag)
    assert res["budget_used"] and not r.cli.calls


def test_read_without_chart_when_read_not_in_image_events(rig):
    r = rig(mode="advisory", price=4206.0); r.cfg.claude_image_events = ("P", "CROSS")
    cli = ImageCLI(read_v("HOLD", "BUY")); r.adv.caller = cli
    _with_frame(r, 4206.0)
    res = r.adv.read_now(r.ag)
    assert res["ok"] and res["png"] is None and cli.images == [None]


# ------------------------------------------------------------------ (m) wiring + hard rules
def test_command_registered_and_text():
    src = open(os.path.join(ROOT, "aureon_mt5", "bot.py"), encoding="utf-8").read()
    assert 'name="claude-read"' in src and '"claude-read":' in src
    from aureon_mt5.bot import claude_read_text
    txt = claude_read_text("XAUUSD", {"snapshot": {"bar_time_ist": "2026-10-15 07:35"}, "position": None, "png": "x.png",
                                      "verdict": {"decision": "SKIP", "confidence": "low", "reason": "chop", "risks": ["news"]},
                                      "latency": 4.1, "stale": False})
    assert "no open position" in txt and "SHOWN ONLY · chart read · CLI 4.1s · no orders or SL changes" in txt and "risks: news" in txt


def test_advisor_imports_no_broker():
    tree = ast.parse(open(os.path.join(ROOT, "aureon_mt5", "claude_advisor.py"), encoding="utf-8").read())
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            assert not any("broker" in a.name for a in n.names)
        if isinstance(n, ast.ImportFrom):
            assert "broker" not in (n.module or "") and not any(a.name == "broker" for a in n.names)
