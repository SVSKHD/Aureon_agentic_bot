#!/usr/bin/env python3
"""v3.0 acceptance test 1 — replay parity: the ema921e runner (EMode + EngineE) vs tools/e_strategy_backtest.py.

    python tools/replay_ema921e.py --csv data/XAUUSD_M30.csv          # the committed export
    python tools/replay_ema921e.py                                    # MT5 (Windows, terminal running)

Bars: loaded with the backtester's own load() (same window: --from − 30 days on a CSV, − 400 bars from MT5), so both
sides see the same bars. Engine flags = the backtest's: no-monday, spike 0.15, ladder 0.25:0.05,0.5:0.25,1:0.5 step
0.5 ATR, no ATR gate, no month stop, no disaster stop, lot 1.0. Bars before --from warm the engine up (the backtester
takes no entry before --from either); every later bar goes through EMode.on_bar exactly as live, the tick between
closes = the next bar's open (SimBroker fills).

The backtester runs as a subprocess with the brief's command (+ --csv-dir for a CSV) and its trades_XAUUSD_M30.csv is
compared with the runner's: trade count, exit-reason counts, gross $ (engine books, the backtester's accounting), and
row for row entry time / dir / exit time / reason / bars. Exit 0 = parity, 1 = a difference (printed).
"""
from __future__ import annotations

import argparse
import collections
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import e_strategy_backtest as BT                                  # noqa: E402
from aureon_mt5.strategies.ema921e.brokers import SimBroker                  # noqa: E402
from aureon_mt5.strategies.ema921e.mode import EMode                         # noqa: E402

LADDER = "0.25:0.05,0.5:0.25,1:0.5"
BACKTEST_FLAGS = ["--symbols", "XAUUSD", "--tf", "M30", "--no-monday", "--ladder-unit", "atr", "--ladder", LADDER, "--step", "0.5"]
SETTINGS = {"symbol": "XAUUSD", "timeframe": "M30", "lot": 1.0, "spike": 0.15, "no_monday": True, "hours": [0, 24],
            "atr_gate": 0.0, "month_stop_per_lot": 0.0, "cut_bars": 2, "ladder": [[0.25, 0.05], [0.5, 0.25], [1.0, 0.5]],
            "step": 0.5, "stop_atr": 0.0, "paper": True}
EXPECTED = {"trades": 291, "cut": 49, "floor": 215, "cross": 27, "gross": 119_346}      # the brief, Jan–Oct 2026


class ReplayFeed:
    """rows[:n] are visible; rows[n-1] is the forming bar. tick = the forming bar's open (the first price after a close)."""

    def __init__(self, rows: list[dict], n: int):
        self.rows, self.n = rows, n

    def bars(self, k):
        return self.rows[max(0, self.n - int(k)):self.n]

    def now_server(self):
        return int(self.rows[self.n - 1]["time"]) + 60

    def tick(self):
        b = self.rows[self.n - 1]
        return {"bid": float(b["open"]), "ask": float(b["open"]) + BT.SPREAD["XAUUSD"], "time": int(b["time"])}


def load_bars(csv: str | None, start: str, end: str) -> pd.DataFrame:
    s, e = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1)
    if csv:
        p = Path(csv)
        if p.name != "XAUUSD_M30.csv":
            raise SystemExit(f"{csv}: the backtester's --csv-dir loader reads <dir>/XAUUSD_M30.csv — name the file that")
        return BT.load("XAUUSD", "M30", s, e, p.parent)
    return BT.load("XAUUSD", "M30", s, e, None)


def run_replay(df: pd.DataFrame, start: str, log_dir: str, *, show_gates: bool = True) -> dict:
    rows = [{"time": int(pd.Timestamp(t).tz_localize("UTC").timestamp()), "open": float(o), "high": float(h), "low": float(l),
             "close": float(c)} for t, o, h, l, c in df[["time", "open", "high", "low", "close"]].itertuples(index=False)]
    st = int(pd.Timestamp(start).tz_localize("UTC").timestamp())
    first = next(i for i, r in enumerate(rows) if r["time"] >= st)            # the first bar the backtester may enter on
    feed = ReplayFeed(rows, first + 1)                                         # rows[:first] closed, rows[first] forming
    cfg = types.SimpleNamespace(max_dd_usd=0, regime_gate="off", execution_enabled=False, allow_live=False,
                                daily_report_ist="23:00", server_utc_offset=0.0)
    r = EMode(cfg, SETTINGS, SimBroker("XAUUSD", feed, log_dir), log_dir=log_dir)
    r.st["enabled"] = True
    r.summary_tick = lambda now=None: None                                    # wall-clock card, not part of the replay
    if not show_gates:                                                         # show-only (they never change a decision); ~7 ms/bar
        r._show_gates = lambda bar: None
    r.startup()
    assert len(r.times) == first, (len(r.times), first)                        # warmup = every bar before --from
    while feed.n < len(rows):
        feed.n += 1
        r.poll_bars()
        r.poll_tick()
    tr = pd.read_csv(os.path.join(log_dir, "ema921e_trades.csv")) if os.path.exists(os.path.join(log_dir, "ema921e_trades.csv")) \
        else pd.DataFrame(columns=["reason"])
    sk = pd.read_csv(os.path.join(log_dir, "ema921e_skips.csv")) if os.path.exists(os.path.join(log_dir, "ema921e_skips.csv")) \
        else pd.DataFrame(columns=["reason"])
    return {"rows": tr, "skips": sk, "engine_gross": sum(t["pnl_usd"] for t in r.eng.closed_trades),
            "engine_trades": len(r.eng.closed_trades), "open_at_end": r.eng.pos is not None, "runner": r}


def run_backtester(csv: str | None, start: str, end: str, out: str) -> pd.DataFrame:
    cmd = [sys.executable, str(ROOT / "tools" / "e_strategy_backtest.py"), *BACKTEST_FLAGS, "--from", start, "--to", end, "--out", out]
    if csv:
        cmd += ["--csv-dir", str(Path(csv).parent)]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    if res.returncode != 0:
        raise SystemExit(f"backtester failed:\n{res.stdout}\n{res.stderr}")
    print("backtester: " + next((l for l in res.stdout.splitlines() if l.startswith("# XAUUSD")), "?").lstrip("# "))
    return pd.read_csv(os.path.join(out, "trades_XAUUSD_M30.csv"))


def compare(rep: dict, bt: pd.DataFrame) -> list[str]:
    a = rep["rows"]; b = bt[bt["reason"] != "skip"].reset_index(drop=True); bs = bt[bt["reason"] == "skip"].reset_index(drop=True)
    diffs = []
    ca, cb = collections.Counter(a["reason"]), collections.Counter(b["reason"])
    ga, gb = rep["engine_gross"], float(b["pnl_usd"].sum())
    print(f"replay:     trades={len(a)} cut={ca['cut']} floor={ca['floor']} cross={ca['cross']} stop={ca['stop']} "
          f"gross ${ga:,.0f} (engine books, {rep['engine_trades']} trades) · skips {dict(collections.Counter(rep['skips']['reason']))}")
    print(f"backtest:   trades={len(b)} cut={cb['cut']} floor={cb['floor']} cross={cb['cross']} stop={cb['stop']} "
          f"gross ${gb:,.0f} · skips {dict(collections.Counter(bs['skipped']))}")
    if len(a) != len(b):
        diffs.append(f"trade count {len(a)} vs {len(b)}")
    if ca != cb:
        diffs.append(f"exit reasons {dict(ca)} vs {dict(cb)}")
    if rep["engine_trades"] != len(b):
        diffs.append(f"engine trades {rep['engine_trades']} vs {len(b)}")
    if abs(ga - gb) > 0.01:
        diffs.append(f"gross ${ga:,.2f} vs ${gb:,.2f}")
    n = min(len(a), len(b))
    for col in ("time", "dir", "exit_time", "reason", "bars"):
        bad = [i for i in range(n) if str(a[col].iat[i]) != str(b[col].iat[i])]
        if bad:
            i = bad[0]
            diffs.append(f"{col} differs on {len(bad)} rows, first #{i}: replay {a.iloc[i].to_dict()} · backtest {b.iloc[i].to_dict()}")
    if n and (a["mfe_pts"].head(n) - b["mfe_pts"].head(n)).abs().max() > 1e-6:
        diffs.append("mfe_pts differs")
    sk = rep["skips"]
    if len(sk) != len(bs) or list(sk["time"]) != list(bs["time"].astype(str)) or list(sk["reason"]) != list(bs["skipped"]):
        diffs.append(f"skips differ: {len(sk)} vs {len(bs)}")
    return diffs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", help="XAUUSD M30 export named XAUUSD_M30.csv (else MT5)")
    ap.add_argument("--from", dest="start", default="2026-01-01")
    ap.add_argument("--to", dest="end", default="2026-10-31")
    ap.add_argument("--expect", action="store_true", help="also require the brief's figures (291 / 49-215-27 / $119,346)")
    ap.add_argument("--no-show-gates", action="store_true", help="skip the show-only regime / drawdown computation (faster)")
    a = ap.parse_args(argv)
    df = load_bars(a.csv, a.start, a.end)
    print(f"bars: {len(df)} XAUUSD M30 {df['time'].iloc[0]} → {df['time'].iloc[-1]} ({'csv ' + a.csv if a.csv else 'MT5'})")
    with tempfile.TemporaryDirectory() as tmp:
        rep = run_replay(df, a.start, os.path.join(tmp, "replay"), show_gates=not a.no_show_gates)
        bt = run_backtester(a.csv, a.start, a.end, os.path.join(tmp, "bt"))
        diffs = compare(rep, bt)
    if a.expect:
        b = bt[bt["reason"] != "skip"]; c = collections.Counter(b["reason"])
        got = {"trades": len(b), "cut": c["cut"], "floor": c["floor"], "cross": c["cross"], "gross": round(float(b["pnl_usd"].sum()))}
        if got != EXPECTED:
            diffs.append(f"brief figures {EXPECTED} vs {got}")
    print("PARITY ✓" if not diffs else "PARITY ✗\n  " + "\n  ".join(diffs))
    return 0 if not diffs else 1


if __name__ == "__main__":
    raise SystemExit(main())
