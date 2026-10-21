"""Replay the EMA 9/21 mode on a bars CSV (offline). Same rule, same guardian object as live.

    python tools/replay_921.py --csv xauusd_m30_20260101_20261021.csv --from 2026-01-01
    python tools/replay_921.py --csv ... --from 2026-01-01 --set exit_mode=e9>e200 --set stop=30
"""
from __future__ import annotations

import argparse, os, sys, types
sys.modules.setdefault("MetaTrader5", types.ModuleType("MetaTrader5"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd
from aureon_mt5.common.source import Bars
from aureon_mt5.strategies import get_strategy

p = argparse.ArgumentParser(); p.add_argument("--csv", required=True); p.add_argument("--from", dest="start"); p.add_argument("--symbol", default="XAUUSD")
p.add_argument("--offset", type=float, default=3.0); p.add_argument("--set", action="append", default=[], help="rule override key=value")
a = p.parse_args()
S = get_strategy("ema921"); df = pd.read_csv(a.csv)[["time", "open", "high", "low", "close"]]
ov = {}
for kv in a.set:
    k, v = kv.split("=", 1)
    ov[k] = v if k == "exit_mode" else (tuple(int(x) for x in v.split(",")) if k == "days" else float(v))
trade_from = int(pd.Timestamp(a.start).timestamp()) if a.start else None
res = S.analyse({"M5": Bars(a.symbol, "M5", df)}, a.symbol, a.offset, (), trade_from=trade_from, overrides=ov)["M5"]
s = res["summary"]; r = res["rules"]
print(f"{S.display_name} · exit {r.exit_mode} · SL −{r.stop:g} TP {("+" + format(r.tp, "g")) if r.tp else "none"} · lock {r.lock_at:g}→{r.lock_to:g} · days {r.days}")
print(f"crosses {s['crosses']} · pre cards {s['pre']} · trades {s['trades']} · win {s['win_rate'] * 100:.0f}% · net {s['net']:+.1f} pts (${s['net'] * 100:,.0f}/lot) · "
      f"stop {s['stop']} locked {s['locked']} tp {s['tp']} ema_close {s['ema_close']} friday {s['friday_flat']}")
J = pd.DataFrame([j.to_dict() for j in res["journeys"]])
if len(J):
    J["dt"] = pd.to_datetime(J.entry_time, unit="s"); J["week"] = J.dt.dt.strftime("%G-W%V"); J["month"] = J.dt.dt.strftime("%Y-%m")
    W = J.groupby("week").result.sum(); c = J.result.cumsum() * 100
    print(f"weeks {len(W)} · positive {(W > 0).sum()} · negative {(W < 0).sum()} · worst ${W.min() * 100:,.0f} · best ${W.max() * 100:,.0f} · max DD ${(c - c.cummax()).min():,.0f}")
    print((J.groupby("month").result.sum() * 100).round(0).to_string())
base = os.path.splitext(a.csv)[0]
S.render_png(res, a.symbol, base + "_921.png", title=os.path.basename(a.csv)); S.render_html(res, a.symbol, base + "_921.html", title=os.path.basename(a.csv))
print("wrote", base + "_921.png", base + "_921.html")
