"""Bar-by-bar replay of the 2026-10-15 XAUUSD long (entry 4103.5, 01:30 server) through the live guardian (_guard) with a fake
MT5. The P SHORT is published on the 02:05 bar; M15 is BULLISH (as reported). An SL is hit when a bar's LOW touches it.

    python tests/fixtures/replay_20261015.py      # prints one line per knob (the CHANGELOG lines)

The bars are SYNTHETIC (see make_xauusd_20261015.py) — reconstructed from the incident numbers, not pulled from MT5."""
from __future__ import annotations

import json, os, sys, tempfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
import pandas as pd  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xauusd_20261015_m5.json")


def load():
    d = json.load(open(FIX, encoding="utf-8"))
    return pd.DataFrame(d["bars"]), d["meta"]


def hhmm(t: int) -> str:
    return datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%H:%M")


def replay(m15: str | None = "BULLISH", **cfg_over) -> dict:
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):                              # the cards print to the console
        return _replay(m15, **cfg_over)


def _replay(m15, **cfg_over) -> dict:
    from aureon_mt5 import broker
    from aureon_mt5.agent import SymbolAgent
    from aureon_mt5.config import Config
    from aureon_mt5.journal import Journal
    from aureon_mt5.notify import Notifier
    from aureon_mt5.strategies import get_strategy
    from tests.test_v181 import FakeMT5

    bars, meta = load()
    times = [hhmm(t) for t in bars["time"]]
    day15 = [datetime.fromtimestamp(int(t), tz=timezone.utc).day == 15 for t in bars["time"]]
    entry_i = next(i for i in range(len(bars)) if day15[i] and times[i] == meta["entry_bar"])
    p_i = next(i for i in range(len(bars)) if day15[i] and times[i] == meta["p_short_bar"])
    entry = float(meta["entry"])
    fake = FakeMT5("long", entry, 0.0, float(bars["close"][entry_i]))
    old = (broker._m, broker._offset_h)
    broker._m, broker._offset_h = fake, 3.0
    tmp = tempfile.mkdtemp(prefix="aureon-replay-")
    try:
        cfg = Config(); cfg.dry = False; cfg.log_dir = tmp; cfg.claude_open_read = False
        for k, v in cfg_over.items():
            setattr(cfg, k, v)
        ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), Notifier(None), Journal(tmp), ())
        ag._m15_state = lambda: m15
        out = {"initial_sl": None, "worst_close": 0.0, "peak_close": 0.0, "exit": None}
        for i in range(entry_i, len(bars)):
            row = bars.iloc[i]
            sl = fake.pos.sl
            if i > entry_i and sl and float(row["low"]) <= sl:                       # stopped intrabar
                out["exit"] = {"time": times[i], "price": round(sl, 2), "points": round(sl - entry, 2), "reason": "sl_hit"}
                break
            c = float(row["close"]); fake.price = c
            df = bars.iloc[: i + 1].reset_index(drop=True)
            ef, es = float(row["ema_fast"]), float(row["ema_slow"])
            if i == p_i:
                ag.latest_signal = {"bar_time": int(row["time"]), "symbol": "XAUUSD", "strategy": "ema5080", "kind": "P",
                                    "direction": "SHORT", "consumed": False}
            ag.last_bar = int(row["time"])
            ag._guard(True, int(row["time"]), df, c, ef, es, 1 if ef > es else -1)
            if out["initial_sl"] is None and fake.pos.sl:
                out["initial_sl"] = round(fake.pos.sl, 2)
            out["worst_close"] = min(out["worst_close"], round(c - entry, 2))
            out["peak_close"] = max(out["peak_close"], round(c - entry, 2))
            if fake.closed:
                ex = [r for r in ag.journal.read(0) if r["event"] == "exit"][-1]
                out["exit"] = {"time": times[i], "price": c, "points": round(c - entry, 2), "reason": ex["reason"]}
                break
        out["last"] = {"time": times[len(bars) - 1], "close": float(bars["close"].iloc[-1]), "sl": round(fake.pos.sl, 2)}
        out["journal"] = ag.journal.read(0)
        return out
    finally:
        broker._m, broker._offset_h = old


def line(name: str, r: dict) -> str:
    ex = r["exit"]
    end = (f"closed {ex['time']} at {ex['price']:.2f} ({ex['points']:+.1f}, {ex['reason']})" if ex
           else f"still open at {r['last']['time']} (close {r['last']['close']:.2f}, {r['last']['close'] - 4103.5:+.1f}, SL {r['last']['sl']:.2f})")
    return f"{name}: initial SL {r['initial_sl']} · worst close {r['worst_close']:+.1f} · {end}"


if __name__ == "__main__":
    print(line("v2.7.2 defaults", replay()))
    print(line("AUREON_FLIP_REQUIRE_STRUCTURE=1", replay(flip_require_structure=True)))
    print(line("AUREON_GIVEBACK_PCT=50 (flip default)", replay(giveback_pct=50)))
    print(line("AUREON_GIVEBACK_PCT=50 + FLIP_REQUIRE_STRUCTURE=1", replay(giveback_pct=50, flip_require_structure=True)))
    print(line("AUREON_SETUP_STOP=1 (flip default)", replay(setup_stop=True)))
    print(line("AUREON_SETUP_STOP=1 + FLIP_REQUIRE_STRUCTURE=1", replay(setup_stop=True, flip_require_structure=True)))
