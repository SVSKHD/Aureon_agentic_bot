"""v3.0 ema921e — config (step 3). Later steps add the runner, cards, logs and the two acceptance tests."""
import os
import shutil
import types

import pytest

from aureon_mt5.strategies.ema921e import config as C
from aureon_mt5.strategies.ema921e.mode import EMode, params_from

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YAML = os.path.join(REPO, "config", "ema921e.yaml")


def _copy(tmp_path):
    p = tmp_path / "ema921e.yaml"; shutil.copy(YAML, p); return str(p)


def test_shipped_yaml_is_the_brief():
    s, problems = C.load(YAML)
    assert problems == []
    assert s == {"symbol": "XAUUSD", "timeframe": "M30", "lot": 0.3, "spike": 0.15, "no_monday": True, "hours": [0, 24],
                 "atr_gate": 0.7, "month_stop_per_lot": 2000.0, "cut_bars": 2,
                 "ladder": [[0.25, 0.05], [0.5, 0.25], [1.0, 0.5]], "step": 0.5, "stop_atr": 3.0,
                 "news_pause_minutes": 30, "paper": True}
    p = params_from(s)
    assert (p.lot, p.atr_gate, p.month_stop_per_lot, p.stop_atr, p.spike, p.no_monday, p.cut_bars, p.step) == \
           (0.3, 0.7, 2000.0, 3.0, 0.15, True, 2, 0.5)
    assert p.ladder == ((0.25, 0.05), (0.5, 0.25), (1.0, 0.5)) and (p.hours_from, p.hours_to) == (0, 24)


def test_set_key_rewrites_one_line_and_keeps_comments(tmp_path):
    path = _copy(tmp_path)
    before = open(path).read().splitlines()
    assert C.set_key(path, "lot", "0.5") == ("lot", 0.5)
    assert C.set_key(path, "news_pause_minutes", "45") == ("news_pause_minutes", 45)
    after = open(path).read().splitlines()
    assert len(after) == len(before)
    assert "lot: 0.5" in after
    assert any(l.startswith("news_pause_minutes: 45") and "# optional" in l for l in after)
    assert C.load(path)[0]["lot"] == 0.5
    assert C.set_key(path, "ladder", "0.25:0.05,0.5:0.25,1:0.5")[1] == [[0.25, 0.05], [0.5, 0.25], [1.0, 0.5]]
    assert C.set_key(path, "hours", "1-23")[1] == [1, 23]


@pytest.mark.parametrize("key,value", [("lot", "0"), ("lot", "9"), ("lot", "abc"), ("cut_bars", "1.5"), ("hours", "5-3"),
                                       ("ladder", "0.5:0.6"), ("symbol", "XAGUSD"), ("timeframe", "M15"), ("paper", "false"),
                                       ("nope", "1")])
def test_set_key_refuses(tmp_path, key, value):
    path = _copy(tmp_path); before = open(path).read()
    with pytest.raises(C.ConfigError):
        C.set_key(path, key, value)
    assert open(path).read() == before


def test_bad_file_falls_back_loudly(tmp_path):
    p = tmp_path / "x.yaml"; p.write_text("lot: lots\nspike: 0.2\n")
    s, problems = C.load(str(p))
    assert s["lot"] == 0.3 and s["spike"] == 0.2
    assert any(x.startswith("lot:") for x in problems) and any("missing" in x for x in problems)


def test_hot_reload_swaps_engine_params(tmp_path):
    path = _copy(tmp_path)
    s, _ = C.load(path)
    cfg = types.SimpleNamespace(max_dd_usd=0, execution_enabled=False, allow_live=False)
    e = EMode(cfg, s, broker=types.SimpleNamespace(name="SIM"), log_dir=str(tmp_path), config_path=path)
    seen = []; e.listeners.append(lambda k, d: seen.append((k, d)))
    e.on_loop(); assert seen == []                                      # unchanged file: nothing
    txt = open(path).read().replace("atr_gate: 0.7", "atr_gate: 0.0")
    open(path, "w").write(txt); os.utime(path, ns=(1, os.stat(path).st_mtime_ns + 10_000_000))
    e.on_loop()
    assert e.eng.p.atr_gate == 0.0 and e.settings["atr_gate"] == 0.0
    assert seen[-1][0] == "config" and seen[-1][1]["changes"] == {"atr_gate": (0.7, 0.0)}
    k, v = e.set("lot", "0.4")
    assert (k, v) == ("lot", 0.4) and e.eng.p.lot == 0.4 and e.lot == 0.4 and C.load(path)[0]["lot"] == 0.4
    n = len(seen); e.on_loop(); assert len(seen) == n                   # our own write is not reloaded twice
    open(path, "w").write(open(path).read().replace("lot: 0.4", "lot: -1"))
    os.utime(path, ns=(1, os.stat(path).st_mtime_ns + 20_000_000))
    e.on_loop()
    assert e.lot == 0.4 and seen[-1][0] == "error" and "reload refused" in seen[-1][1]["text"]


# ----------------------------------------------------------------------------- step 4: cards, commands, the daily summary
from aureon_mt5.strategies.ema921e import cards as K, commands as X


def test_card_lines_match_the_brief():
    assert K.enter_line({"side": 1, "price": 4012.3, "atr": 18.4, "cut_at_bar": 2,
                         "ladder_pts": [(4.6, 0.92), (9.2, 4.6), (18.4, 9.2)], "stop_px": 3957.1}) == \
        "ENTER LONG 4012.3 | ATR 18.4 | cut@bar2 | floor rungs 4.6/9.2/18.4 | stop 3957"
    assert K.floor_line({"lock_atr": 0.25, "price": 4016.9}) == "FLOOR +0.25 ATR → 4016.9"
    assert K.exit_line({"reason": "floor", "pnl_pts": 9.1, "pnl_usd": 273.0, "bars": 6}) == "EXIT floor +9.1 pts +$273 (bars 6)"
    assert K.skip_line({"reason": "spike", "side": -1, "price": 4001.24}) == "SKIP spike · SHORT 4001.2"
    for line in (K.enter_line({"side": -1, "price": 1, "atr": 1, "ladder_pts": [], "stop_px": None}),):
        assert "\n" not in line


class _FakeBroker:
    name = "SIM"; off = 2.0
    def __init__(self, now): self.now = now
    def now_server(self): return self.now
    def account_login(self): return "51234567"
    def account_kind(self): return "demo"


def _runner(tmp_path, now=1_793_000_000):
    s, _ = C.load(YAML); path = _copy(tmp_path)
    cfg = types.SimpleNamespace(max_dd_usd=0, execution_enabled=False, allow_live=False, daily_report_ist="23:00")
    return EMode(cfg, s, broker=_FakeBroker(now), log_dir=str(tmp_path), config_path=path)


def test_commands_on_off_set_live(tmp_path):
    r = _runner(tmp_path)
    assert not r.enabled
    assert "ON" in X.on(r) and r.enabled and r._load_state()["enabled"] is True
    assert "OFF" in X.off(r) and not r.enabled
    assert X.set_(r, "lot", "0.5") == "ema921e lot = 0.5" and r.lot == 0.5
    assert X.set_(r, "lot", "50").startswith("refused")
    assert X.set_(r, "paper", "false").startswith("refused") and r.settings["paper"] is True
    assert "AUREON_ALLOW_LIVE" in X.live(r, "51234567", allow_live=False) and r.settings["paper"] is True
    assert "type the account number" in X.live(r, "", allow_live=True) and r.settings["paper"] is True
    assert "type the account number" in X.live(r, "5123456", allow_live=True) and r.settings["paper"] is True
    out = X.live(r, "51234567", allow_live=True)
    assert "LIVE" in out and "51234567" not in out and r.settings["paper"] is False and C.load(r.config_path)[0]["paper"] is False
    assert "PAPER" in X.demo(r) and r.settings["paper"] is True and C.load(r.config_path)[0]["paper"] is True
    with pytest.raises(LookupError):
        X.status(lambda: None)


def test_status_shows_cut_time_and_floor_pts_and_atr(tmp_path):
    from aureon_mt5.strategies.ema921e.engine import Position
    r = _runner(tmp_path)
    t0 = 1_792_990_000 - 1_792_990_000 % 1800
    for j in range(60):
        c = 4000 + j * 0.5
        r._feed({"time": t0 + j * 1800, "open": c, "high": c + 2, "low": c - 2, "close": c})
    k = 58
    r.eng.pos = Position(i=k, side=1, entry=4029.0, atr=10.0, mfe_atr=0.6, floor_atr=0.25)
    r.st.update(ticket=7, entry_fill=4029.1)
    out = X.status(r)
    cut_close = t0 + (k + 2) * 1800 + 1800
    assert f"bar 2 closes {K.ist_of(cut_close, 2.0):%H:%M} IST" in out
    assert "floor 4031.50 (+2.5 pts · +0.25 ATR)" in out and "bars in 1" in out and "MFE +6.0 pts" in out
    assert "ticket #7" in out and "(fill 4029.10)" in out and "disaster stop 3999.00" in out


def test_daily_summary_once_per_ist_day(tmp_path):
    from datetime import datetime
    r = _runner(tmp_path)
    r.trade_rows = [{"time": "2026-11-02 10:00:00", "exit_time": "2026-11-02 12:00:00", "reason": "floor", "pnl_usd": 300.0},
                    {"time": "2026-11-02 13:00:00", "exit_time": "2026-11-02 14:00:00", "reason": "cut", "pnl_usd": -90.0},
                    {"time": "2026-11-01 13:00:00", "exit_time": "2026-11-01 14:00:00", "reason": "cross", "pnl_usd": 50.0}]
    sent = []; r.listeners.append(lambda k, d: sent.append((k, d)))
    at = lambda hm: datetime.strptime(f"2026-11-02 {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=K.IST).timestamp()
    assert r.summary_tick(at("22:59")) is None
    txt = r.summary_tick(at("23:00"))
    assert txt.startswith("E DAY 2026-11-02 · 2 trades (1 won) · cut 1 floor 1 cross 0 stop 0 · day +$210 · month +$260 (3 trades)")
    assert "backtest ≈ +$120/trade" in txt and "\n" not in txt
    assert r.summary_tick(at("23:30")) is None and len([k for k, _ in sent if k == "summary"]) == 1
    assert _runner(tmp_path).summary_tick(at("23:40")) is None       # persisted: a restart does not resend


def test_cards_listener_one_line_each(tmp_path):
    r = _runner(tmp_path); sent = []
    notify = types.SimpleNamespace(send=lambda key, title, lines, **kw: sent.append((key, title, lines)))
    K.Cards(r, notify)
    r.emit("enter", side=1, price=4012.3, atr=18.4, cut_at_bar=2, ladder_pts=[(4.6, 1), (9.2, 4.6), (18.4, 9.2)], stop_px=3957.1, ticket=1)
    r.emit("floor", side=1, price=4016.9, lock_atr=0.25, ticket=1)
    r.emit("exit", reason="floor", pnl_pts=9.1, pnl_usd=273.0, bars=6, time="a", exit_time="b")
    r.emit("skip", reason="monday", side=-1, price=4000.0)
    assert [t for _, t, _ in sent] == ["ENTER LONG 4012.3 | ATR 18.4 | cut@bar2 | floor rungs 4.6/9.2/18.4 | stop 3957",
                                       "FLOOR +0.25 ATR → 4016.9", "EXIT floor +9.1 pts +$273 (bars 6)", "SKIP monday · SHORT 4000.0"]
    assert all(lines == [] for _, _, lines in sent)
    assert r.skip_log[-1][1] == "monday" and r.trade_rows[-1]["pnl_usd"] == 273.0
