"""v1.8.7 — trend states, re-entry detector, session cards, ask-to-trade fallback. Signals/notifications only."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
from aureon_mt5.strategies.ema5080.trend import state_of, find_reentries
from aureon_mt5.strategies import get_strategy
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.agent import SymbolAgent


def test_state_rules_match_drawing():
    # bearish lines (50 < 80)
    assert state_of(4100, 4110, 4120) == "BEARISH"                 # below 50
    assert state_of(4115, 4110, 4120) == "BEARISH · PULLBACK"      # between 50 and 80
    assert state_of(4125, 4110, 4120) == "BEARISH · CHALLENGED"    # above 80 -> bullish turn forming
    # bullish mirror
    assert state_of(4130, 4120, 4110) == "BULLISH" and state_of(4115, 4120, 4110) == "BULLISH · PULLBACK"
    assert state_of(4105, 4120, 4110) == "BULLISH · CHALLENGED"


def downtrend_with_pullback():
    """Steady downtrend, one pullback that tags EMA 50 then rejects."""
    n = 300; t = np.arange(n) * 300
    base = 4200 - np.arange(n) * 0.35
    close = base.copy()
    close[250:254] += np.array([4.0, 7.5, 9.0, 8.0])           # pullback up into EMA 50
    close[254] = close[253] - 4.0                               # rejection bar
    o = np.r_[close[0], close[:-1]]
    h = np.maximum(o, close) + 0.3; l = np.minimum(o, close) - 0.3
    df = pd.DataFrame({"time": t, "open": o, "high": h, "low": l, "close": close})
    return get_strategy("ema5080").add_emas(df)


def test_reentry_short_found_once():
    df = downtrend_with_pullback()
    res = [r for r in find_reentries(df) if r.index >= 240]
    assert res and res[0].side == "SHORT" and 250 <= res[0].index <= 256
    assert len([r for r in res if r.index <= 260]) == 1           # one per pullback


class Cap(Notifier):
    def __init__(self): super().__init__(None); self.titles = []
    def raw(self, body, png=None, *, embed=None): self.titles.append(embed["title"] if embed else body); return True


def test_reentry_card_asks_with_webhook_fallback(tmp_path):
    df = downtrend_with_pullback(); n = Cap()
    cfg = Config(); cfg.dry = True; cfg.log_dir = str(tmp_path)
    ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), n, Journal(str(tmp_path)), ())
    idx = [r.index for r in find_reentries(df) if r.index >= 240][0]
    d = df.iloc[: idx + 1].reset_index(drop=True)
    ag._trend_and_reentry(d, int(d.time.iloc[-1]), float(d.close.iloc[-1]), float(d.ema_fast.iloc[-1]), float(d.ema_slow.iloc[-1]))
    assert any("RE-ENTRY · SHORT" in t for t in n.titles)
    assert any(r["event"] == "signal" and r.get("signal_kind") == "reentry" for r in ag.journal.read(0))


def test_ask_uses_queue_when_bot_ready():
    n = Notifier(None); n.bot_ready = True
    assert n.ask("k", "XAUUSD · MT5 · RE-ENTRY · SHORT", ["Trade SHORT now?"], meta={"side": "SHORT"})
    item = n.ask_queue.get_nowait(); assert item["meta"]["side"] == "SHORT"
    assert not n.ask("k", "dup", [])                               # deduped


def test_session_card_on_boundary(tmp_path):
    n = Cap(); cfg = Config(); cfg.dry = True; cfg.log_dir = str(tmp_path)
    ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), n, Journal(str(tmp_path)), ())
    df = downtrend_with_pullback()
    # server offset 3h: Asia 00-07 UTC = 03-10 server; pick bars around 10:00 server (London open)
    base = 1_791_000_000 - (1_791_000_000 % 86400) + 9 * 3600 + 30 * 60        # 09:30 server
    df["time"] = base + np.arange(len(df)) * 300 - 280 * 300
    for i in range(270, 290):
        d = df.iloc[: i + 1]
        ag._session(d, int(d.time.iloc[-1]), float(d.close.iloc[-1]), float(d.ema_fast.iloc[-1]), float(d.ema_slow.iloc[-1]))
    assert any("LONDON OPEN" in t for t in n.titles)
