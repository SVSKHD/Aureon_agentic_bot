"""v1.10.0 — Claude add-on safety: fail closed, never loosen, CLOSE only in manage + profit, once per pullback,
never block the poll, no order placement, AUREON_CLAUDE=off = zero calls. The CLI is always mocked."""
import ast, json, os, subprocess, sys, threading, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import pytest
from aureon_mt5 import broker
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.claude_advisor import ClaudeAdvisor, build_snapshot
from aureon_mt5.common import claude_cli as cc
from aureon_mt5.common.timeutil import now_server
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.strategies import get_strategy
from tests.test_v181 import FakeMT5, guard

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Cap(Notifier):
    def __init__(self): super().__init__(None); self.titles = []; self.bodies = []
    def raw(self, body, png=None, *, embed=None):
        self.titles.append(embed["title"] if embed else body.splitlines()[0]); self.bodies.append(str(embed or body)); return True


class FakeCLI:
    """Stands in for claude_cli.call. `reply` is a CliResult, a dict verdict, or a callable."""
    def __init__(self, reply=None, delay=0.0):
        self.reply, self.delay, self.calls = reply, delay, []
    def __call__(self, prompt, model, *, event, bin, workdir, timeout):
        self.calls.append({"prompt": prompt, "model": model, "event": event}); time.sleep(self.delay)
        r = self.reply(event) if callable(self.reply) else self.reply
        if isinstance(r, dict):
            return cc.CliResult(True, verdict=cc.validate(r, event), latency_s=1.2)
        return r


def ok(decision, **kw):
    return {"decision": decision, "side": kw.pop("side", None), "tighten_to": kw.pop("tighten_to", None),
            "confidence": kw.pop("confidence", "medium"), "reason": kw.pop("reason", "test reason")}


def frame(n=200, price=4200.0, t0=None):
    t = (np.arange(n) * 300) + (t0 if t0 is not None else now_server(3.0) - n * 300)
    c = np.full(n, price)
    return pd.DataFrame({"time": t, "open": c, "high": c + 1.0, "low": c - 1.0, "close": c, "ema_fast": price - 10.0, "ema_slow": price - 20.0})


@pytest.fixture
def rig(tmp_path, monkeypatch):
    def make(mode="manage", reply=None, delay=0.0, direction="long", entry=4200.0, sl=4194.0, price=4208.0, start=False, strategy="ema5080",
             max_calls=30):
        fake = FakeMT5(direction, entry, sl, price)
        monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", 3.0)
        cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.claude_mode = mode; cfg.claude_workdir = str(tmp_path / "wd")
        cfg.claude_max_calls = max_calls
        n = Cap(); j = Journal(str(tmp_path)); cli = FakeCLI(reply, delay)
        adv = ClaudeAdvisor(cfg, n, j, caller=cli, start=start)
        ag = SymbolAgent("XAUUSD", cfg, get_strategy(strategy), n, j, (), claude=adv)
        ag.off = 3.0
        return types.SimpleNamespace(fake=fake, cfg=cfg, n=n, j=j, cli=cli, adv=adv, ag=ag)
    return make


def verdicts(j):
    return [r for r in j.read(0) if r["event"] == "claude_verdict"]


def entry_job(r, kind="P", side="LONG", bar_t=None):
    df = frame()
    bar_t = int(df["time"].iloc[-1]) if bar_t is None else bar_t
    assert r.adv.request_entry(r.ag, kind, side, bar_t, df, card_key="k")
    return r.adv.q.get_nowait()


def pullback_job(r, peak=12.0):
    pos = broker.positions("XAUUSD")
    guard(r.ag, r.fake, frame(price=r.fake.price), sgn=1, new_bar=False)       # state exists
    st = r.ag.state[1]; st["peak"] = peak
    assert r.adv.maybe_pullback(r.ag, pos[0], st, now_server(3.0) - 300, frame(price=r.fake.price), ["giveback"])
    return r.adv.q.get_nowait()


def run_pullback(r, job):
    r.adv.process(job)
    r.adv.drain(r.ag, broker.positions("XAUUSD"))


# ------------------------------------------------------------------ fail closed
def test_timeout_no_verdict_card_unchanged(rig):
    r = rig(mode="advisory", reply=cc.CliResult(False, error="timeout after 90 s", timed_out=True))
    r.adv.process(entry_job(r))
    assert not any("CLAUDE" in t for t in r.n.titles)
    v = verdicts(r.j)[-1]
    assert v["decision"] is None and v["stale"] is False and "timeout" in v["error"] and v["acted"] is False


@pytest.mark.parametrize("bad", [cc.CliResult(False, error="CLI output is not JSON"), cc.CliResult(False, error="invalid verdict: decision MAYBE")])
def test_invalid_reply_no_verdict(rig, bad):
    r = rig(mode="advisory", reply=bad)
    r.adv.process(entry_job(r))
    assert not any("CLAUDE" in t for t in r.n.titles) and verdicts(r.j)[-1]["decision"] is None


def test_entry_side_mismatch_rejected(rig):
    r = rig(mode="advisory", reply=ok("TAKE", side="SELL"))
    r.adv.process(entry_job(r, side="LONG"))
    assert verdicts(r.j)[-1]["decision"] is None and not any("CLAUDE" in t for t in r.n.titles)


def test_entry_verdict_shown_and_journaled(rig):
    r = rig(mode="advisory", reply=ok("TAKE", confidence="high", reason="clean separation"))
    r.adv.process(entry_job(r))
    assert any("CLAUDE · P PRE-CROSS · LONG" in t for t in r.n.titles)
    v = verdicts(r.j)[-1]
    assert v["decision"] == "TAKE" and v["side"] == "BUY" and v["source"] == "claude" and v["claude_event"] == "P" and not v["stale"]
    assert r.cli.calls[0]["model"] == r.cfg.claude_entry_model


def test_entry_verdict_edits_open_ask_card(rig):
    r = rig(mode="advisory", reply=ok("SKIP"))
    edits = []
    r.n.claude_edit_hook = lambda key, fld: edits.append((key, fld)) or True
    r.adv.process(entry_job(r))
    assert edits and edits[0][0] == "k" and "SKIP" in edits[0][1]["value"]
    assert not any("CLAUDE" in t for t in r.n.titles)                       # edited in place, no extra card


def test_auth_error_unavailable_card_once(rig):
    r = rig(mode="advisory", reply=cc.CliResult(False, error="Please run /login", auth_error=True))
    r.adv.process(entry_job(r)); r.adv.process(entry_job(r))
    assert sum("CLAUDE UNAVAILABLE" in t for t in r.n.titles) == 1
    assert r.adv.status()["login"] == "FAILED"


def test_budget_used_skips_call_card_once_guardian_unaffected(rig):
    r = rig(mode="advisory", reply=ok("TAKE"), max_calls=1)
    r.adv.process(entry_job(r)); r.adv.process(entry_job(r)); r.adv.process(entry_job(r))
    assert len(r.cli.calls) == 1
    assert sum("CLAUDE BUDGET USED" in t for t in r.n.titles) == 1
    assert verdicts(r.j)[-1]["error"] == "budget used"
    r.fake.price = 4212.0
    guard(r.ag, r.fake, frame(price=4212.0), sgn=1)                          # guardian still secures +10
    assert r.fake.pos.sl == 4210.0


def test_late_entry_verdict_is_stale_shown_not_acted(rig):
    r = rig(mode="advisory", reply=ok("TAKE"))
    r.adv.process(entry_job(r, bar_t=now_server(3.0) - 3 * 300))
    v = verdicts(r.j)[-1]
    assert v["stale"] is True and v["acted"] is False
    assert any("STALE" in b for b in r.n.bodies)


def test_stale_pullback_not_acted(rig):
    r = rig(reply=ok("TIGHTEN", tighten_to=4205.0))
    job = pullback_job(r); job["bar_t"] = now_server(3.0) - 3 * 300
    run_pullback(r, job)
    assert r.fake.pos.sl == 4194.0 and verdicts(r.j)[-1]["stale"] is True and verdicts(r.j)[-1]["acted"] is False


# ------------------------------------------------------------------ TIGHTEN
def test_tighten_never_loosens(rig):
    r = rig(reply=ok("TIGHTEN", tighten_to=4190.0))                          # below the live SL 4194 for a long
    run_pullback(r, pullback_job(r))
    assert r.fake.pos.sl == 4194.0 and not any("CLAUDE TIGHTENED" in t for t in r.n.titles)
    assert verdicts(r.j)[-1]["acted"] is False


def test_tighten_goes_through_move_sl_and_announces_after_confirm(rig, monkeypatch):
    r = rig(reply=ok("TIGHTEN", tighten_to=4205.0))
    calls = []
    orig = r.ag._move_sl
    monkeypatch.setattr(r.ag, "_move_sl", lambda p, sl, action, quiet=False: calls.append((sl, action)) or orig(p, sl, action, quiet))
    run_pullback(r, pullback_job(r))
    assert calls == [(4205.0, "CLAUDE TIGHTEN")] and r.fake.pos.sl == 4205.0
    assert any("CLAUDE TIGHTENED" in t for t in r.n.titles) and verdicts(r.j)[-1]["acted"] is True


def test_tighten_rejected_by_mt5_not_announced(rig):
    r = rig(reply=ok("TIGHTEN", tighten_to=4205.0))
    job = pullback_job(r); r.fake.reject = True
    run_pullback(r, job)
    assert r.fake.pos.sl == 4194.0 and not any("CLAUDE TIGHTENED" in t for t in r.n.titles)
    assert verdicts(r.j)[-1]["acted"] is False


def test_tighten_shown_not_applied_in_advisory(rig):
    r = rig(mode="advisory", reply=ok("TIGHTEN", tighten_to=4205.0))
    run_pullback(r, pullback_job(r))
    assert r.fake.pos.sl == 4194.0 and any("CLAUDE PULLBACK VERDICT" in t for t in r.n.titles)


# ------------------------------------------------------------------ CLOSE
def test_close_refused_when_losing(rig):
    r = rig(reply=ok("CLOSE"), price=4198.0)
    run_pullback(r, pullback_job(r))
    assert not r.fake.closed and verdicts(r.j)[-1]["acted"] is False


def test_close_refused_in_advisory(rig):
    r = rig(mode="advisory", reply=ok("CLOSE"))
    run_pullback(r, pullback_job(r))
    assert not r.fake.closed


def test_close_in_profit_in_manage(rig):
    r = rig(reply=ok("CLOSE", reason="trend turning"))
    run_pullback(r, pullback_job(r))
    assert r.fake.closed and any("CLAUDE CLOSED" in t for t in r.n.titles)
    assert any(x["event"] == "exit" and x["reason"] == "claude_close" for x in r.j.read(0))


def test_guardian_close_wins_over_claude(rig):
    r = rig(reply=ok("HOLD"))
    job = pullback_job(r); r.adv.process(job)
    r.ag.closed_by_aureon.add(1)                                             # the guardian closed it this poll
    r.adv.drain(r.ag, broker.positions("XAUUSD"))
    assert "no longer open" in (verdicts(r.j)[-1]["note"] or "")


# ------------------------------------------------------------------ once per pullback
def test_once_per_pullback_rearmed_after_new_peak(rig):
    r = rig(reply=ok("HOLD"))
    p = broker.positions("XAUUSD")[0]
    guard(r.ag, r.fake, frame(price=r.fake.price), sgn=1, new_bar=False)
    st = r.ag.state[1]; st["peak"] = 12.0
    df = frame(price=r.fake.price); bt = now_server(3.0) - 300
    assert r.adv.maybe_pullback(r.ag, p, st, bt, df, ["giveback"])
    assert not r.adv.maybe_pullback(r.ag, p, st, bt + 300, df, ["giveback"])     # same pullback
    st["peak"] = 15.0                                                            # new peak
    assert r.adv.maybe_pullback(r.ag, p, st, bt + 600, df, ["giveback"])


def test_pullback_triggers(rig):
    r = rig(reply=ok("HOLD"), price=4205.0)
    p = broker.positions("XAUUSD")[0]
    assert r.adv.pullback_reason(r.ag, p, {"peak": 4.0}, []) is None
    assert "60%" in r.adv.pullback_reason(r.ag, p, {"peak": 10.0}, [])         # +5 ≤ 60% of +10
    r.ag.trend_state = "BULLISH · WEAKENING"
    assert "WEAKENING" in r.adv.pullback_reason(r.ag, p, {"peak": 4.0}, [])
    r.ag.trend_state = "BEARISH · PULLBACK"                                     # a pullback in the other trend: not ours
    assert r.adv.pullback_reason(r.ag, p, {"peak": 4.0}, []) is None


# ------------------------------------------------------------------ never block the poll
def test_poll_not_blocked_by_slow_cli(rig):
    r = rig(reply=ok("HOLD"), delay=3.0, start=True, price=4205.0)
    r.ag.trend_state = "BULLISH · PULLBACK"
    t = time.monotonic()
    guard(r.ag, r.fake, frame(price=4205.0), sgn=1, new_bar=True)              # triggers a pullback request
    assert time.monotonic() - t < 0.5
    time.sleep(0.2)
    assert len(r.cli.calls) == 1                                               # the worker is busy with it, the poll is not
    t = time.monotonic(); guard(r.ag, r.fake, frame(price=4205.0), sgn=1, new_bar=True)
    assert time.monotonic() - t < 0.5


# ------------------------------------------------------------------ hard rules
def test_no_order_placement_in_claude_modules():
    for rel in ("aureon_mt5/claude_advisor.py", "aureon_mt5/common/claude_cli.py"):
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        tree = ast.parse(src)
        imported = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        mods = {getattr(n, "module", None) or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert "broker" not in imported and not any("broker" in m for m in mods), rel
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for bad in ("broker", "order_send", "close_partial", "set_sl", "place_order", "open_position", "order_open", "market_order"):
            assert bad not in names, f"{rel} references {bad}"
    adv = open(os.path.join(ROOT, "aureon_mt5/claude_advisor.py"), encoding="utf-8").read()
    assert adv.count("agent._close(") == 1 and adv.count("agent._move_sl(") == 1   # the guardian's safe paths, once each


def test_off_means_zero_calls(tmp_path, monkeypatch):
    cfg = Config(); cfg.claude_mode = "off"; cfg.dry = True; cfg.source = "synthetic"; cfg.poll_seconds = 0.05; cfg.log_dir = str(tmp_path)
    assert ClaudeAdvisor.create(cfg, Cap(), Journal(str(tmp_path))) is None
    calls = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or real(*a, **k))
    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: calls.append(a) or real(*a, **k))
    ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), Cap(), Journal(str(tmp_path)), (), claude=None)
    assert ag.claude is None
    ag.start(); time.sleep(1.5); ag.stop(); ag.join(3)
    assert calls == [] and ag.health["errors"] == 0


def test_ema2050_never_hooked(rig):
    r = rig(mode="manage", reply=ok("HOLD"), strategy="ema2050")
    assert r.ag.claude is None


def test_review_mode_has_no_trade_hooks(rig):
    r = rig(mode="review", reply=ok("HOLD"))
    assert r.ag.claude is None and r.adv.mode == "review"


def test_snapshot_has_no_secrets_and_is_json(rig):
    r = rig(reply=ok("HOLD"))
    p = broker.positions("XAUUSD")[0]
    s = build_snapshot(r.ag, "pullback", "LONG", now_server(3.0) - 300, frame(), position=p, st={"peak": 12.0, "secured": 0.0, "bars": 4})
    json.dumps(s)
    want = {"event", "symbol", "mode", "side", "bar_time_ist", "close", "ema50", "ema80", "gap", "avg_range", "bars_ohlc_ist",
            "trend_state", "whipsaw", "htf_context", "spread_warning", "session", "minutes_to_news", "streak", "daily_limit_state",
            "track_record", "entry", "sl", "secured_level", "peak", "open_profit", "bars_held"}
    assert want <= set(s) and len(s["bars_ohlc_ist"]) == 30
    flat = json.dumps(s).lower()
    for bad in ("balance", "login", "account", "webhook", "token", "password", "equity", "ticket"):
        assert bad not in flat


# ------------------------------------------------------------------ daily review
def test_daily_review_posts_once_per_day(rig):
    r = rig(mode="review", reply=lambda ev: cc.CliResult(True, text="Good day: the London cross worked.", latency_s=2.0))
    r.j.log("signal", symbol="XAUUSD", side="LONG", signal_kind="cross", price=4200.0, bar=1)
    text = r.adv.daily_review()
    assert "London" in text and any("CLAUDE REVIEW" in t for t in r.n.titles)
    assert r.cli.calls[0]["model"] == r.cfg.claude_review_model and r.cli.calls[0]["event"] == "review"
    assert r.adv.daily_review(force=False).startswith("already reviewed") and len(r.cli.calls) == 1


def test_review_tick_fires_once_at_2300():
    import main
    from datetime import datetime
    from aureon_mt5.common.claude_cli import IST
    fired = []
    adv = types.SimpleNamespace(request_review=lambda day: fired.append(day))
    cfg = Config()
    d = main.claude_review_tick(adv, cfg, None, now=datetime(2026, 10, 8, 22, 59, tzinfo=IST)); assert d is None and not fired
    d = main.claude_review_tick(adv, cfg, d, now=datetime(2026, 10, 8, 23, 0, tzinfo=IST)); assert d == "2026-10-08"
    d = main.claude_review_tick(adv, cfg, d, now=datetime(2026, 10, 8, 23, 30, tzinfo=IST))
    assert fired == ["2026-10-08"]
    assert main.claude_review_tick(None, cfg, None) is None


def test_weekly_report_has_claude_section(rig):
    from aureon_mt5.reports import claude_summary, claude_rows_for_batch
    r = rig(mode="advisory", reply=ok("TAKE"))
    job = entry_job(r); r.adv.process(job)
    r.j.log("decision", symbol="XAUUSD", bar=job["bar_t"], decision="SKIP")
    r.j.log("leg", symbol="XAUUSD", side="LONG", start_t=job["bar_t"], best=14.0)
    lines = "\n".join(claude_summary(r.j.read(0)))
    assert "Claude TAKE: 1" in lines and "reached +10: 1" in lines and "differed 1" in lines
    rows = claude_rows_for_batch(r.j, 0)
    assert rows and rows[0]["source"] == "claude" and rows[0]["decision"] == "TAKE"
