"""v2.0.2 — /alert card with two verdicts (AGENT first, CLAUDE edited in or posted as a follow-up), decision journal fields, PREVIEW at
creation with zero Claude calls, ORDER PLACED card + requote retry + ❌, execution-off attach + card edit on first position_seen,
command sync dropping stale commands, /pull-history (ok, busy), /git-history parsers and the no-git path."""
import asyncio, json, os, sys, threading, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import pytest
from aureon_mt5 import alerts as A, bot, broker
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.bot import Reply, respond
from aureon_mt5.claude_advisor import ClaudeAdvisor
from aureon_mt5.common import claude_cli as cc, gitinfo
from aureon_mt5.common.timeutil import now_server
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.strategies import get_strategy
from aureon_mt5.strategies.ema2050 import journeys as J
from tests.test_claude_advisor import Cap, FakeCLI, ok
from tests.test_v200_alerts import TickMT5
from tests.test_v200_ema2050 import _bull, OFF

S2050 = get_strategy("ema2050")
plain = lambda r: {"card": r.card, "content": r.content}


class Inter:
    def __init__(self):
        self.calls = []; self.response = types.SimpleNamespace(defer=self._defer); self.followup = types.SimpleNamespace(send=self._send); self.user = "tester"
    async def _defer(self, **kw): self.calls.append(("defer", kw))
    async def _send(self, **kw): self.calls.append(("send", kw))


def make(tmp_path, monkeypatch, mode="advisory", reply=None, max_calls=30, execution=False):
    fake = TickMT5(price=4000.5); monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.claude_mode = mode; cfg.claude_workdir = str(tmp_path / "wd")
    cfg.claude_max_calls = max_calls; cfg.execution_enabled = execution
    n = Cap(); j = Journal(str(tmp_path)); cli = FakeCLI(reply)
    adv = ClaudeAdvisor.create(cfg, n, j, caller=cli, start=False)
    store = A.AlertStore(str(tmp_path))
    ag = SymbolAgent("XAUUSD", cfg, S2050, n, j, (), claude=adv, alerts=store); ag.off = OFF
    return types.SimpleNamespace(fake=fake, cfg=cfg, n=n, j=j, cli=cli, adv=adv, ag=ag, store=store, agents={"XAUUSD": ag})


def fire(r, df=None):
    df = df if df is not None else _bull(n=12, touch_at=(11,))
    res = J.build(df, J.rules_for("XAUUSD"), OFF); res["df"] = df; r.ag.last_df = df; r.ag.last_res = res; r.ag.last_bar = int(df["time"].iloc[-1])
    r.ag._refresh_snapshot(True)
    r.store.arm("XAUUSD", 4000.0, 4010.0, "lvl")
    r.fake.bid, r.fake.ask = 3999.5, 3999.8; r.fake.tick_time = now_server(OFF)
    r.n.bot_ready = True                                                           # buttons path: the card goes to the ask queue
    r.ag._check_alerts(df, df, int(df["time"].iloc[-1]))
    return r.n.ask_queue.get_nowait()


def vals(item):
    return {f["name"]: f["value"] for f in item["fields"]}


# ------------------------------------------------------------------ card: both blocks, agent first, Claude edited in
def test_card_has_agent_block_before_claude_and_edit_agrees(tmp_path, monkeypatch):
    v = ok("TAKE", side="BUY", reason="first touch, 1 cross today"); v["evidence"] = ["confirm_state", "crosses_today"]; v["p_win"] = 0.68
    r = make(tmp_path, monkeypatch, reply=v)
    item = fire(r)
    names = [f["name"] for f in item["fields"]]
    assert names.index("AGENT VERDICT") < names.index("CLAUDE VERDICT")
    assert vals(item)["AGENT VERDICT"].startswith("**AGENT: LONG — confirmed cross") and vals(item)["CLAUDE VERDICT"].startswith("⏳ asking Claude (")
    assert r.cli.calls == [] and r.adv.q.qsize() == 1                             # the agent block exists before any Claude call
    edits = []
    r.n.card_edit_hook = lambda key, upd: edits.append((key, upd)) or True
    r.adv.process(r.adv.q.get_nowait())
    assert len(r.cli.calls) == 1 and edits[0][0] == "alert:XAUUSD:A1"
    blk = edits[0][1]["CLAUDE VERDICT"]
    assert blk.startswith("**CLAUDE: LONG · TAKE · p_win 0.68 · confidence medium**") and '"first touch, 1 cross today"' in blk
    assert "evidence: confirm_state, crosses_today" in blk and "AGREES with agent ✅" in blk
    a = r.store.get("A1"); assert a["claude_side"] == "LONG" and a["claude_agreed"] is True and a["claude_p_win"] == 0.68


def test_disagree_and_follow_up_card_when_edit_fails(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, reply=ok("SKIP", reason="chop"))
    item = fire(r)
    r.n.card_edit_hook = None                                                     # webhook only: no card to edit
    r.adv.process(r.adv.q.get_nowait())
    t = next(t for t in r.n.titles if "ALERT VERDICT · A1" in t)
    body = r.n.bodies[r.n.titles.index(t)]
    assert "AGENT VERDICT" in body and "CLAUDE VERDICT" in body and "DISAGREES with agent ⚠️ (agent LONG, Claude SKIP)" in body
    assert r.store.get("A1")["claude_agreed"] is False and r.store.get("A1")["claude_side"] is None


def test_unavailable_edits_block(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, reply=cc.CliResult(False, error="timeout after 90 s", timed_out=True))
    fire(r); edits = []
    r.n.card_edit_hook = lambda key, upd: edits.append(upd) or True
    r.adv.process(r.adv.q.get_nowait())
    assert edits[0]["CLAUDE VERDICT"] == "— timeout after 90 s"


# ------------------------------------------------------------------ decision journal fields
def test_decision_journals_both_verdicts(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, reply=ok("TAKE", side="SELL"))
    item = fire(r); r.n.card_edit_hook = lambda *a: True
    r.adv.process(r.adv.q.get_nowait())                                           # Claude SHORT, agent LONG
    res = bot.alert_decision(r.cfg, r.agents, r.j, r.store, item, "LONG", "me")
    d = [x for x in r.j.read(0) if x["event"] == "alert_decision"][-1]
    assert d["agent_verdict"].startswith("AGENT: LONG") and d["claude_verdict"] == "TAKE" and d["claude_side"] == "SHORT"
    assert d["agreed_agent"] is True and d["agreed_claude"] is False and res["line"].startswith("TAKEN LONG · waiting for your ticket…")
    res = bot.alert_decision(r.cfg, r.agents, r.j, r.store, item, "SKIP", "me")
    d = [x for x in r.j.read(0) if x["event"] == "alert_decision"][-1]
    assert d["side"] == "skip" and d["agreed_agent"] is False and d["agreed_claude"] is False and res["line"] == "SKIPPED"
    st = A.stats(r.j.read(0))
    assert st["taken"] == 1 and st["agreed_agent"] == 1 and st["agreed_claude"] == 0 and st["with_claude"] == 1


# ------------------------------------------------------------------ preview at creation: zero Claude calls
def test_alert_preview_makes_no_claude_call(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, reply=ok("TAKE"))
    df = _bull(n=12, touch_at=(11,)); res = J.build(df, J.rules_for("XAUUSD"), OFF); res["df"] = df
    r.ag.last_df = df; r.ag.last_res = res; r.ag.last_bar = int(df["time"].iloc[-1]); r.ag._refresh_snapshot(True)
    a = r.store.arm("XAUUSD", 4100.0, 4000.5)
    card = bot.alert_preview(r.ag, a, 4000.5)
    names = [f["name"] for f in card["fields"]]
    assert names == ["PRICE · PREVIEW", "EMA", "TREND", "AGENT VERDICT · PREVIEW (this bar, not the hit)", "CLAUDE VERDICT"]
    assert card["title"].endswith("PREVIEW") and "AGENT: LONG" in card["fields"][3]["value"]
    assert r.adv.q.empty() and r.cli.calls == []


# ------------------------------------------------------------------ placement feedback
class ReqMT5(TickMT5):
    """Rejects the first market order with a requote, accepts the retry."""
    def __init__(self, *a, requotes=1, final_ok=True, **k):
        super().__init__(*a, **k); self.requotes = requotes; self.final_ok = final_ok; self.attempts = 0
    def order_send(self, req):
        if req["action"] == self.TRADE_ACTION_DEAL and "position" not in req:
            self.attempts += 1
            if self.attempts <= self.requotes:
                return types.SimpleNamespace(retcode=10004, comment="Requote")
            if not self.final_ok:
                return types.SimpleNamespace(retcode=10019, comment="No money")
            self.orders.append(req); return types.SimpleNamespace(retcode=10009, comment="done", order=1234567, deal=99, price=req["price"] + 0.1, volume=req["volume"])
        return super().order_send(req)


def test_order_placed_card_and_journal(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_lots = 0.5
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    item = {"meta": {"kind": "alert", "alert_id": "A1", "symbol": "XAUUSD", "price": 4000.6, "bar": 1, "mode": "ema2050", "suggested_side": "LONG",
                     "agent_verdict": "AGENT: LONG — x"}}
    r.store.arm("XAUUSD", 4000.0, 4010.0)
    res = bot.alert_decision(r.cfg, r.agents, r.j, r.store, item, "LONG", "me", notify=r.n)
    assert res["ok"] and res["ticket"] == 1234567 and res["line"] == "TAKEN LONG · ticket 1234567 · filled 4000.90"
    o = [x for x in r.j.read(0) if x["event"] == "order_placed"][-1]
    assert o["ok"] is True and o["ticket"] == 1234567 and abs(o["fill"] - 4000.9) < 1e-9
    t = next(t for t in r.n.titles if "ORDER PLACED · LONG" in t); body = r.n.bodies[r.n.titles.index(t)]
    for key in ("Ticket", "#1234567", "Lots", "0.5", "Fill", "4000.90", "Slippage vs card", "+0.30", "SL as set", "3988.80", "Spread at fill", "0.30", "Time"):
        assert key in body, key
    assert r.ag.pending_alert["alert_id"] == "A1"


def test_requote_retried_once_then_rejected(tmp_path, monkeypatch):
    sleeps = []
    fake = ReqMT5(price=4000.5, requotes=1); monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    r = broker.place_market("XAUUSD", "LONG", 0.1, 3988.0, retry_sleep=2.0, sleep=lambda s: sleeps.append(s))
    assert r.ok and fake.attempts == 2 and sleeps == [2.0] and r.ticket == 1234567
    fake2 = ReqMT5(price=4000.5, requotes=1, final_ok=False); monkeypatch.setattr(broker, "_m", fake2)
    r2 = broker.place_market("XAUUSD", "LONG", 0.1, 3988.0, sleep=lambda s: None)
    assert not r2.ok and fake2.attempts == 3 and r2.retcode == 10019            # requote → retry → IOC then FOK fallback, both refused
    fake3 = ReqMT5(price=4000.5, requotes=5); monkeypatch.setattr(broker, "_m", fake3)
    r3 = broker.place_market("XAUUSD", "LONG", 0.1, 3988.0, sleep=lambda s: None)
    assert not r3.ok and fake3.attempts == 2                                     # one retry only
    rr = make(tmp_path, monkeypatch, execution=True); monkeypatch.setattr(broker, "_m", fake3)
    item = {"meta": {"kind": "alert", "alert_id": "A1", "symbol": "XAUUSD", "price": 4000.6, "bar": 1, "mode": "ema2050", "suggested_side": "LONG"}}
    res = bot.alert_decision(rr.cfg, rr.agents, rr.j, rr.store, item, "LONG", "me", notify=rr.n, retry_sleep=0.0)
    assert not res["ok"] and res["line"].startswith("TAKEN LONG · ❌ order rejected") and res["text"].startswith("❌ order rejected")


def test_execution_off_attaches_and_edits_card_on_first_position(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=False)
    item = {"meta": {"kind": "alert", "alert_id": "A1", "symbol": "XAUUSD", "price": 4000.6, "bar": 1, "mode": "ema2050", "suggested_side": "LONG"}}
    res = bot.alert_decision(r.cfg, r.agents, r.j, r.store, item, "LONG", "me", notify=r.n)
    assert res["line"] == "TAKEN LONG · waiting for your ticket… · SL 3988.80 (−12) (guardian will set it)" and r.fake.orders == []
    edits = []
    r.n.card_edit_hook = lambda key, upd: edits.append((key, upd)) or True
    r.fake.closed = False; r.fake.pos.price_open = 4000.8; r.fake.pos.sl = 3988.8; r.fake.price = 4001.0
    n = 200; df = pd.DataFrame({"time": np.arange(n) * 300, "close": 4001.0, "high": 4001.0, "low": 4001.0, "ema20": 3995.0, "ema50": 3990.0})
    r.ag._guard(True, int(df["time"].iloc[-1]), df, 4001.0, 3995.0, 3990.0, 1)                # first poll with the position
    assert r.ag.state[1]["alert_id"] == "A1"
    assert edits and edits[0][0] == "alert:XAUUSD:A1" and edits[0][1]["Decision"].startswith("ATTACHED · ticket 1 · LONG @ 4000.80 · SL 3988.8")
    assert any("PROTECTED · LONG" in t for t in r.n.titles) and "set with the order" in " ".join(r.n.bodies)


# ------------------------------------------------------------------ command sync
class FakeTree:
    def __init__(self, local, remote):
        self.local = [types.SimpleNamespace(name=n) for n in local]; self.remote = [types.SimpleNamespace(name=n) for n in remote]; self.copied = None
    def get_commands(self): return self.local
    def copy_global_to(self, guild=None): self.copied = guild
    async def fetch_commands(self, guild=None): return self.remote
    async def sync(self, guild=None): self.remote = list(self.local); return self.local


def test_sync_removes_stale_commands_and_logs():
    tree = FakeTree({"status", "alert", "pull-history"}, {"status", "old-cmd", "pull-history"}); logs = []
    out = asyncio.run(bot.sync_commands(tree, None, log=logs.append))
    assert out["removed"] == ["old-cmd"] and out["added"] == ["alert"] and sorted(out["final"]) == ["alert", "pull-history", "status"]
    assert logs and "removed stale: ['old-cmd']" in logs[0] and {c.name for c in tree.remote} == {"status", "alert", "pull-history"}
    assert bot.sync_plan({"a", "b"}, {"b", "c"}) == {"add": ["a"], "keep": ["b"], "remove": ["c"]}


# ------------------------------------------------------------------ /pull-history
class HistMT5(TickMT5):
    def __init__(self, deals, **k):
        super().__init__(**k); self.deals = deals
    def history_deals_get(self, a, b): return self.deals


def _deal(ticket, price, profit, t, dtype=1, symbol="XAUUSD"):
    return types.SimpleNamespace(ticket=ticket + 1000, position_id=ticket, symbol=symbol, time=t, price=price, volume=0.1, profit=profit, type=dtype, entry=1, comment="")


def test_pull_history_matches_and_grades(tmp_path, monkeypatch):
    now = int(time.time())
    fake = HistMT5([_deal(11, 4010.0, 95.0, now - 3600), _deal(12, 3990.0, -60.0, now - 7200, dtype=0), _deal(13, 4005.0, 10.0, now - 100)], price=4000.5)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); j = Journal(str(tmp_path))
    j.log("position_seen", symbol="XAUUSD", ticket=11, side="LONG", entry=4000.0, alert_id="A1")
    j.log("closed", symbol="XAUUSD", ticket=11, direction="long", entry=4000.0, final_points=None, final_approx=True, alert_id="A1")
    j.log("position_seen", symbol="XAUUSD", ticket=12, side="SHORT", entry=4000.0)
    inter = Inter()
    t0 = time.monotonic()
    asyncio.run(respond(inter, "pull-history", lambda: bot.pull_history(cfg, {}, j, 7, None), timeout=30, to_kwargs=plain))
    assert time.monotonic() - t0 < 5 and inter.calls[-1][0] == "send"
    card = inter.calls[-1][1]["card"]; v = {f["name"]: f["value"] for f in card["fields"]}
    assert "found **3**" in v["Deals"] and "matched **2**" in v["Deals"] and "newly graded **2**" in v["Deals"] and "unmatched 1" in v["Deals"]
    assert "+20.0 pts" in v["Net"] and "#13" in v["Unmatched tickets"] and "Per day" in v
    fp = [r for r in j.read(0) if r["event"] == "final_points"]
    assert {r["ticket"]: r["final_points"] for r in fp} == {11: 10.0, 12: 10.0} and next(r for r in fp if r["ticket"] == 11)["alert_id"] == "A1"
    hp = [r for r in j.read(0) if r["event"] == "history_pull"][-1]
    assert (hp["rows"], hp["matched"], hp["unmatched"], hp["newly_graded"]) == (3, 2, 1, 2)
    asyncio.run(respond(inter, "pull-history", lambda: bot.pull_history(cfg, {}, j, 7, None), timeout=30, to_kwargs=plain))
    assert len([r for r in j.read(0) if r["event"] == "final_points"]) == 2                  # not graded twice
    assert A.stats(j.read(0) + [{"t": 1, "event": "alert_decision", "alert_id": "A1", "side": "LONG"}])["points"] == 10.0


def test_pull_history_busy_lock(tmp_path, monkeypatch):
    fake = HistMT5([], price=4000.5); monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); j = Journal(str(tmp_path))
    held = threading.Event(); release = threading.Event()
    def hog():
        with broker._lock:
            held.set(); release.wait(5)
    th = threading.Thread(target=hog); th.start(); held.wait(2)
    try:
        r = bot.pull_history(cfg, {}, j, 7, None, lock_timeout=0.3)
        assert r.content == "MT5 busy — try again" and r.source == "cache"
    finally:
        release.set(); th.join()


# ------------------------------------------------------------------ /git-history
def test_git_log_parser_links_and_dates():
    m = gitinfo.parse_log_line("8d6ba37|2026-10-11 09:42:10 +0530|Hithesh|Merge pull request #6 from SVSKHD/x")
    assert m["sha"] == "8d6ba37" and m["author"] == "Hithesh" and m["prs"] == [6] and m["date"].utcoffset().total_seconds() == 19800
    assert gitinfo.fmt_dates(m["date"], m["raw_date"]) == "2026-10-11 09:42 IST (09:42 +0530)"
    m2 = gitinfo.parse_log_line("5181b48|2026-10-08 10:12:00 +0000|x|Merge pull request #5 and #4")
    assert gitinfo.fmt_dates(m2["date"]) == "2026-10-08 15:42 IST (10:12 +0000)"
    assert gitinfo.pr_links("Merge pull request #5 and #4") == ("Merge pull request [#5](https://github.com/SVSKHD/Aureon_agentic_bot/pull/5) "
                                                                "and [#4](https://github.com/SVSKHD/Aureon_agentic_bot/pull/4)")
    assert gitinfo.parse_log_line("garbage") is None and gitinfo.parse_tag_line("v2.0.1|2026-10-11 08:00:00 +0530")["tag"] == "v2.0.1"
    c = gitinfo.card({"ok": True, "running": {"describe": "v2.0.1", "head": "8d6ba37", "status": "up to date with origin/master", "head_date_text": "x"},
                      "merges": [m, m2], "tags": []}, "2.0.1", 10)
    assert c["description"].startswith("Running: **v2.0.1** · `v2.0.1` · up to date with origin/master") and "[#6](" in c["fields"][0]["value"]


def test_git_graceful_without_git(tmp_path):
    def no_git(*a, **k): raise FileNotFoundError("git")
    h = gitinfo.history(5, cwd=str(tmp_path), run=no_git)
    assert not h["ok"] and h["error"] == "not a git checkout" and gitinfo.card(h, "2.0.2")["description"] == "not a git checkout"
    assert gitinfo.head_merge_date(cwd=str(tmp_path), run=no_git) == ""
    h2 = gitinfo.history(5, cwd=str(tmp_path))                                   # a real dir that is not a repo
    assert not h2["ok"] and h2["error"] == "not a git checkout"


def test_git_history_live_fetch_failure_shows_local(monkeypatch):
    real = gitinfo.subprocess.run
    def run(cmd, **k):
        if "fetch" in cmd: raise gitinfo.subprocess.TimeoutExpired(cmd, 5)
        return real(cmd, **k)
    h = gitinfo.history(3, run=run)
    assert h["ok"] and h["running"]["status"].startswith("fetch failed — showing local") and h["running"]["head"]


def test_new_commands_go_through_run():
    import ast
    tree = ast.parse(open(bot.__file__, encoding="utf-8").read())
    names = {f.name for f in ast.walk(tree) if isinstance(f, ast.AsyncFunctionDef)
             and any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "command" for d in f.decorator_list)}
    assert {"pull_history_cmd", "git_history_cmd", "discord_bot_sync"} <= names
