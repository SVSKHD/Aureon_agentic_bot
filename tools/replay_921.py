"""Replay the EMA 9/21 mode on a bars CSV (offline). Same rule, same guardian object as live.

    python tools/replay_921.py --csv xauusd_m30_20260101_20261021.csv --from 2026-01-01
    python tools/replay_921.py --csv ... --from 2026-01-01 --set exit_mode=e9>e200 --set stop=30
    python tools/replay_921.py --csv ... --set entry=limit --set limit_offset=1.5 --set limit_ttl_bars=2
        v2.17.0: a limit fills on the first LATER bar whose extreme reaches close ∓ offset (never on the signal bar), else it
        expires after ttl bars; the run also prints the comparison line against entry=market.
"""
from __future__ import annotations

import argparse, os, sys, types
sys.modules.setdefault("MetaTrader5", types.ModuleType("MetaTrader5"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd
from aureon_mt5.common.source import Bars
from aureon_mt5.strategies import get_strategy

USD_PER_POINT_PER_LOT = 100.0


def parse_sets(items) -> dict:
    ov = {}
    for kv in items or []:
        k, v = kv.split("=", 1)
        if k in ("exit_mode", "entry"):
            ov[k] = v
        elif k == "days":
            ov[k] = tuple(int(x) for x in v.split(","))
        elif k in ("limit_ttl_bars", "n_close", "pre_cooldown_bars"):
            ov[k] = int(float(v))
        else:
            ov[k] = float(v)
    return ov


def run(df: pd.DataFrame, overrides: dict, *, start=None, symbol="XAUUSD", offset=3.0) -> dict:
    S = get_strategy("ema921")
    trade_from = int(pd.Timestamp(start).timestamp()) if start else None
    return S.analyse({"M5": Bars(symbol, "M5", df[["time", "open", "high", "low", "close"]])}, symbol, offset, (),
                     trade_from=trade_from, overrides=overrides)["M5"]


def stats(res: dict) -> dict:
    s = res["summary"]
    J = pd.DataFrame([j.to_dict() for j in res["journeys"] if j.exit_reason not in (None, "open")])
    out = {"trades": s["trades"], "win": s["win_rate"] * 100, "net": s["net"], "usd": s["net"] * USD_PER_POINT_PER_LOT,
           "unfilled": s.get("unfilled", 0), "weeks_pos": 0, "weeks_neg": 0, "max_dd_usd": 0.0}
    if len(J):
        J["week"] = pd.to_datetime(J.entry_time, unit="s").dt.strftime("%G-W%V")
        W = J.groupby("week").result.sum(); c = J.result.cumsum() * USD_PER_POINT_PER_LOT
        out.update(weeks_pos=int((W > 0).sum()), weeks_neg=int((W < 0).sum()), max_dd_usd=float((c - c.cummax()).min()))
    return out


def comparison_line(limit: dict, market: dict, ov: dict) -> str:
    return (f"entry=limit offset {ov.get('limit_offset', 1.5):g} ttl {ov.get('limit_ttl_bars', 2)}: trades {limit['trades']} "
            f"(unfilled {limit['unfilled']}) · win {limit['win']:.0f}% · net {limit['net']:+.1f} pts ${limit['usd']:+,.0f}/lot · "
            f"weeks +{limit['weeks_pos']}/−{limit['weeks_neg']}  vs  entry=market: trades {market['trades']} · win {market['win']:.0f}% · "
            f"net {market['net']:+.1f} pts ${market['usd']:+,.0f}/lot · weeks +{market['weeks_pos']}/−{market['weeks_neg']}  "
            f"→ limit {limit['usd'] - market['usd']:+,.0f} $/lot")


def main(argv=None) -> dict:
    p = argparse.ArgumentParser(); p.add_argument("--csv", required=True); p.add_argument("--from", dest="start")
    p.add_argument("--symbol", default="XAUUSD"); p.add_argument("--offset", type=float, default=3.0)
    p.add_argument("--set", action="append", default=[], help="rule override key=value"); p.add_argument("--no-render", action="store_true")
    a = p.parse_args(argv)
    S = get_strategy("ema921"); df = pd.read_csv(a.csv)[["time", "open", "high", "low", "close"]]
    ov = parse_sets(a.set)
    res = run(df, ov, start=a.start, symbol=a.symbol, offset=a.offset)
    s = res["summary"]; r = res["rules"]
    print(f"{S.display_name} · entry {r.entry}" + (f" {r.limit_offset:g}/{r.limit_ttl_bars} bars" if r.entry == "limit" else "")
          + f" · exit {r.exit_mode} · SL −{r.stop:g} TP {('+' + format(r.tp, 'g')) if r.tp else 'none'} · lock {r.lock_at:g}→{r.lock_to:g} · days {r.days}")
    print(f"crosses {s['crosses']} · pre cards {s['pre']} · trades {s['trades']} · win {s['win_rate'] * 100:.0f}% · net {s['net']:+.1f} pts (${s['net'] * 100:,.0f}/lot) · "
          f"stop {s['stop']} locked {s['locked']} tp {s['tp']} ema_close {s['ema_close']} friday {s['friday_flat']}"
          + (f" · unfilled {s['unfilled']}" if r.entry == "limit" else ""))
    J = pd.DataFrame([j.to_dict() for j in res["journeys"]])
    if len(J):
        J["dt"] = pd.to_datetime(J.entry_time, unit="s"); J["week"] = J.dt.dt.strftime("%G-W%V"); J["month"] = J.dt.dt.strftime("%Y-%m")
        W = J.groupby("week").result.sum(); c = J.result.cumsum() * 100
        print(f"weeks {len(W)} · positive {(W > 0).sum()} · negative {(W < 0).sum()} · worst ${W.min() * 100:,.0f} · best ${W.max() * 100:,.0f} · max DD ${(c - c.cummax()).min():,.0f}")
        print((J.groupby("month").result.sum() * 100).round(0).to_string())
    out = {"res": res, "stats": stats(res)}
    if ov.get("entry") == "limit":
        market = stats(run(df, {**ov, "entry": "market"}, start=a.start, symbol=a.symbol, offset=a.offset))
        out["compare"] = comparison_line(out["stats"], market, ov); out["market"] = market
        print(out["compare"])
    if not a.no_render:
        base = os.path.splitext(a.csv)[0]
        S.render_png(res, a.symbol, base + "_921.png", title=os.path.basename(a.csv)); S.render_html(res, a.symbol, base + "_921.html", title=os.path.basename(a.csv))
        print("wrote", base + "_921.png", base + "_921.html")
    return out


if __name__ == "__main__":
    main()
