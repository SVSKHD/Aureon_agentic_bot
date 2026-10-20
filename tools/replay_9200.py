"""Replay the EMA 9/200 mode on a bars CSV (fetch_bars.py output or MT5 export) — offline, no terminal needed.

    python tools/replay_9200.py --csv xauusd_m30_20260403_20261020.csv --from 2026-05-01
    python tools/replay_9200.py --csv ... --decisions logs/decisions_9200.json      # replay a recorded TAKE/SKIP log

Prints the take-all baseline (every candidate taken: the mechanical floor the human is measured against), the week table,
and writes a PNG + HTML report next to the CSV. `--decisions` is {bar_time_epoch: true|false}.
"""
from __future__ import annotations

import argparse, json, os, sys, types
sys.modules.setdefault("MetaTrader5", types.ModuleType("MetaTrader5"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
from aureon_mt5.common.source import Bars
from aureon_mt5.strategies import get_strategy

p = argparse.ArgumentParser()
p.add_argument("--csv", required=True); p.add_argument("--from", dest="start"); p.add_argument("--symbol", default="XAUUSD")
p.add_argument("--decisions"); p.add_argument("--offset", type=float, default=3.0)
a = p.parse_args()

S = get_strategy("ema9200")
df = pd.read_csv(a.csv)[["time", "open", "high", "low", "close"]]
trade_from = int(pd.Timestamp(a.start).timestamp()) if a.start else None
kw = {}
if a.decisions:
    kw["decisions"] = {int(k): bool(v) for k, v in json.load(open(a.decisions)).items()}
res = S.analyse({"M5": Bars(a.symbol, "M5", df)}, a.symbol, a.offset, (), trade_from=trade_from, **kw)["M5"]
s = res["summary"]
print(f"{S.display_name} · {a.csv}\ncandidates {s['candidates']} (pullback {s['pullback']}, momentum {s['momentum']}) · trades {s['trades']} · "
      f"win {s['win_rate'] * 100:.0f}% · net {s['net']:+.1f} pts (${s['net'] * 100:,.0f} / lot) · stops {s['stops']} secured {s['secured']} ema9_turn {s['ema9_turn']}")
J = pd.DataFrame([j.to_dict() for j in res["journeys"]])
if len(J):
    J["dt"] = pd.to_datetime(J.entry_time, unit="s"); J["week"] = J.dt.dt.strftime("%G-W%V")
    W = J.groupby("week").result.sum()
    print(f"weeks {len(W)} · positive {(W > 0).sum()} · negative {(W < 0).sum()} · worst {W.min():+.1f} · best {W.max():+.1f}")
    print(W.round(1).to_string())
base = os.path.splitext(a.csv)[0]
S.render_png(res, a.symbol, base + "_9200.png", title=os.path.basename(a.csv))
S.render_html(res, a.symbol, base + "_9200.html", title=os.path.basename(a.csv), server_offset_h=a.offset)
print("wrote", base + "_9200.png", base + "_9200.html")
