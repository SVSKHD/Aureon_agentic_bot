#!/usr/bin/env python3
"""
e_strategy_backtest.py — bar-by-bar backtest of the "E + safeguard" 9/21 strategy, multi-symbol.

Rules (each one a flag):
  ENTRY   every EMA 9/21 cross, at the cross bar's close
          - skip spike-shaped crosses (EMA 9 slope into the cross > --spike ATR/bar)      [--no-spike-skip to disable]
          - no entries on Monday                                                          [--no-monday]
          - skip the cross that comes right after a held trade                            [--skip-after-held]
          - only enter between --hours a-b (server hours, b exclusive)                    [--hours 0-17]
  CUT     at the close of bar +N (--cut-bars, default 2): if close <= entry + --cut-thr x ATR -> exit now
  RIDE    hold to the opposite cross (always closes the trade, taken or not), with the safeguard ladder:
          --ladder "5:2,10:5,20:10"  (trigger:lock pairs)  --step 10   (beyond the last trigger, lock rises
          by 'step' for every 'step' of new MFE).  --ladder-unit pts (price units, gold default) or atr
          (all values x ATR at entry, so the same ladder carries to silver/FX).
          Floor is updated at each bar's close from that bar's high/low and checked on the NEXT bar
          (no look-ahead). Optional disaster stop --stop-atr (e.g. 3) on every bar while in.
  COST    spread per symbol deducted once per trade.

Run:
    python e_strategy_backtest.py --symbols XAUUSD --tf M30 --no-monday
    python e_strategy_backtest.py --tf M30 --no-monday --skip-after-held --ladder-unit atr --ladder "0.25:0.1,0.5:0.25,1:0.5" --step 0.5
    python e_strategy_backtest.py --tf M15 --csv-dir ./data
Output: per-symbol monthly table + a ranking table across symbols; trades_<SYMBOL>_<TF>.csv in --out.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_SYMBOLS = ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY"]
LOT_VALUE = {"XAUUSD": 100.0, "XAGUSD": 5000.0, "EURUSD": 100000.0, "GBPUSD": 100000.0, "USDJPY": 100000.0}
SPREAD = {"XAUUSD": 0.25, "XAGUSD": 0.020, "EURUSD": 0.00010, "GBPUSD": 0.00012, "USDJPY": 0.012}
TF_MINUTES = {"M1": 1, "M5": 5, "M10": 10, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def usd(symbol, pts, price, lot):
    v = LOT_VALUE.get(symbol, 100000.0) * lot
    return pts * (v / price if symbol.endswith("JPY") else v)


# ----------------------------------------------------------------------------- data
def fetch_mt5(symbol, tf, start, end):
    import MetaTrader5 as mt5  # type: ignore
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    tfm = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M10": mt5.TIMEFRAME_M10, "M15": mt5.TIMEFRAME_M15,
           "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1}
    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_range(symbol, tfm[tf], start.to_pydatetime(), end.to_pydatetime())
    mt5.shutdown()
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"no bars for {symbol} {tf}")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df[["time", "open", "high", "low", "close"]]


def fetch_csv(symbol, tf, csv_dir, start, end):
    for p in (csv_dir / f"{symbol}_{tf}.csv", csv_dir / f"{symbol}.csv"):
        if p.exists():
            df = pd.read_csv(p)
            df.columns = [c.lower() for c in df.columns]
            df["time"] = pd.to_datetime(df["time"])
            return df[(df["time"] >= start - pd.Timedelta(days=30)) & (df["time"] <= end)][
                ["time", "open", "high", "low", "close"]].reset_index(drop=True)
    raise FileNotFoundError(f"no CSV for {symbol} in {csv_dir}")


def load(symbol, tf, start, end, csv_dir):
    if csv_dir is not None:
        return fetch_csv(symbol, tf, csv_dir, start, end)
    return fetch_mt5(symbol, tf, start - pd.Timedelta(minutes=TF_MINUTES.get(tf, 15) * 400), end)


def add_indicators(df):
    df = df.copy()
    c = df["close"]
    df["ema9"] = c.ewm(span=9, adjust=False).mean()
    df["ema21"] = c.ewm(span=21, adjust=False).mean()
    pc = c.shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    df["s9"] = df["ema9"].diff(3) / 3 / df["atr"]
    df["ema200"] = c.ewm(span=200, adjust=False).mean()
    df["dist200"] = (c - df["ema200"]) / df["atr"]            # +ve = price above 200, in ATR
    df["s200"] = df["ema200"].diff(3) / 3 / df["atr"]        # 200 slope, ATR/bar
    df["side"] = np.sign(df["ema9"] - df["ema21"])
    df["atr_ref"] = df["atr"].rolling(2880, min_periods=200).mean()   # ~60 days of M30 bars
    df["atr_ratio"] = df["atr"] / df["atr_ref"]
    x = ((df["side"] != df["side"].shift(1)) & (df["side"] != 0) & (df["side"].shift(1) != 0)).astype(int)
    df["crosses20"] = x.rolling(20, min_periods=1).sum()               # crosses in the last 20 bars (incl. this one)
    return df


# ----------------------------------------------------------------------------- strategy
def parse_ladder(s):
    out = []
    for part in s.split(","):
        t, l = part.split(":")
        out.append((float(t), float(l)))
    return sorted(out)


def floor_for(mfe, ladder, step):
    """Lock level reached for a given MFE (both in the ladder's unit). None if no trigger reached."""
    lock = None
    for t, l in ladder:
        if mfe >= t:
            lock = l
    if lock is not None and step > 0 and mfe >= ladder[-1][0] + step:
        lock = ladder[-1][1] + np.floor((mfe - ladder[-1][0]) / step) * step
    return lock


def _x200(ema21, ema200, i, w=5):
    """True if EMA 21 crossed EMA 200 within +-w bars of i (a genuine 200 cross, allowed even when near)."""
    lo, hi = max(1, i - w), min(len(ema21) - 1, i + w)
    sgn = np.sign(ema21[lo:hi + 1] - ema200[lo:hi + 1])
    return len(set(sgn[sgn != 0])) > 1


def backtest(df, symbol, a):
    side = df["side"].values
    hi, lo, cl = df["high"].values, df["low"].values, df["close"].values
    atr, s9 = df["atr"].values, df["s9"].values
    dist200, s200, ema200, ema21 = df["dist200"].values, df["s200"].values, df["ema200"].values, df["ema21"].values
    atr_ratio, crosses20 = df["atr_ratio"].values, df["crosses20"].values
    month_pnl, cur_month = 0.0, None
    times = df["time"]
    n = len(df)
    start = pd.Timestamp(a.start)
    h0, h1 = a.hours
    ladder = parse_ladder(a.ladder)
    trades = []
    pos = None               # dict while in a trade
    prev_held = False
    spread = SPREAD.get(symbol, 0.0)

    for i in range(1, n):
        crossed = side[i] != 0 and side[i - 1] != 0 and side[i] != side[i - 1]

        # ---------- manage open position on this bar
        if pos is not None:
            d, e = pos["dir"], pos["entry"]
            unit = pos["unit"]
            b = i - pos["i"]
            exit_px, reason = None, None
            # 1) floor / disaster stop checked against this bar's range, using floor set at previous close
            if pos["floor"] is not None:
                fpx = e + d * pos["floor"] * unit
                if (lo[i] <= fpx) if d > 0 else (hi[i] >= fpx):
                    exit_px, reason = fpx, "floor"
            if exit_px is None and a.stop_atr:
                spx = e - d * a.stop_atr * pos["atr"]
                if (lo[i] <= spx) if d > 0 else (hi[i] >= spx):
                    exit_px, reason = spx, "stop"
            # 2) opposite cross closes at this bar's close
            if exit_px is None and crossed:
                exit_px, reason = cl[i], "cross"
            # 3) bar +N cut
            if exit_px is None and b == a.cut_bars and (cl[i] - e) * d <= a.cut_thr * pos["atr"]:
                exit_px, reason = cl[i], "cut"
            if exit_px is not None:
                pts = (exit_px - e) * d - spread
                trades.append({**pos["rec"], "exit_time": times.iat[i], "exit": exit_px, "reason": reason,
                               "bars": b, "mfe_pts": pos["mfe"] * unit, "pnl_pts": pts,
                               "pnl_usd": usd(symbol, pts, e, a.lot)})
                prev_held = reason != "cut"
                month_pnl += usd(symbol, pts, e, a.lot)
                pos = None
            else:
                # update MFE and floor at this bar's close (used from next bar on)
                m = ((hi[i] - e) if d > 0 else (e - lo[i])) / unit
                if m > pos["mfe"]:
                    pos["mfe"] = m
                    f = floor_for(m, ladder, a.step)
                    if f is not None and (pos["floor"] is None or f > pos["floor"]):
                        pos["floor"] = f

        # ---------- new entry on a cross
        if crossed and pos is None and times.iat[i] >= start:
            d = int(side[i])
            t = times.iat[i]
            if cur_month != (t.year, t.month):
                cur_month, month_pnl = (t.year, t.month), 0.0
            skip = None
            if not a.no_spike_skip and s9[i - 2] * d > a.spike:
                skip = "spike"
            elif a.no_monday and t.weekday() == 0:
                skip = "monday"
            elif a.skip_after_held and prev_held:
                skip = "after_held"
            elif not (h0 <= t.hour < h1):
                skip = "hours"
            elif a.with200 and dist200[i] * d <= 0:
                skip = "against200"           # price on the wrong side of EMA 200
            elif a.slope200 and s200[i] * d < -a.slope200_tol:
                skip = "slope200"             # EMA 200 sloping against the cross
            elif a.near200 and abs(dist200[i]) < a.near200 and not _x200(ema21, ema200, i):
                skip = "near200"              # sitting on the 200 without crossing it
            elif a.atr_gate and not np.isnan(atr_ratio[i]) and atr_ratio[i] < a.atr_gate:
                skip = "low_atr"              # volatility below its 60-day norm: range regime
            elif a.chop_gate and crosses20[i] > a.chop_gate:
                skip = "chop"                 # too many crosses recently: braided 9/21
            elif a.month_stop and month_pnl <= -a.month_stop * a.lot:
                skip = "month_stop"           # month already down this much: stand down till next month
            if skip:
                trades.append({"symbol": symbol, "time": t, "dir": "LONG" if d > 0 else "SHORT", "entry": cl[i],
                               "skipped": skip, "exit_time": t, "exit": cl[i], "reason": "skip", "bars": 0,
                               "mfe_pts": 0.0, "pnl_pts": 0.0, "pnl_usd": 0.0})
                prev_held = False
                continue
            unit = atr[i] if a.ladder_unit == "atr" else 1.0
            pos = {"i": i, "dir": d, "entry": cl[i], "atr": atr[i], "unit": unit, "mfe": 0.0, "floor": None,
                   "rec": {"symbol": symbol, "time": t, "dir": "LONG" if d > 0 else "SHORT", "entry": cl[i], "skipped": ""}}
        elif crossed and pos is None:
            prev_held = False
    return pd.DataFrame(trades)


# ----------------------------------------------------------------------------- report
def summarize(tr, symbol, a):
    t = tr[tr["reason"] != "skip"].copy()
    if t.empty:
        return None
    t["m"] = pd.to_datetime(t["time"]).dt.to_period("M").astype(str)
    s = t["pnl_usd"]
    mon = s.groupby(t["m"]).sum()
    runs, cur = [], 0
    for v in s.values:
        if v <= 0:
            cur += 1
        else:
            if cur:
                runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)
    wins, losses = s[s > 0], s[s <= 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else float("inf")
    return {
        "symbol": symbol, "trades": len(t), "skipped": int((tr["reason"] == "skip").sum()),
        "cut": int((t["reason"] == "cut").sum()), "floor": int((t["reason"] == "floor").sum()),
        "cross": int((t["reason"] == "cross").sum()), "stop": int((t["reason"] == "stop").sum()),
        "win%": 100 * (s > 0).mean(), "gross": s.sum(), "yours": s.sum() * a.split, "avg": s.mean(),
        "neg_months": int((mon < 0).sum()), "worst_month": mon.min(), "best_month": mon.max(),
        "maxDD": (s.cumsum() - s.cumsum().cummax()).min(), "PF": pf, "streak": max(runs) if runs else 0,
        "monthly": mon, "table": t,
        "skips": tr[tr["reason"] == "skip"]["skipped"].value_counts().to_dict(),
    }


def tr_skips(r):
    return r.get("skips", {})


def print_symbol(r, a):
    t = r["table"]
    print(f"\n{'#' * 78}\n# {r['symbol']} {a.tf}  trades={r['trades']}  skipped={r['skipped']}  "
          f"cut={r['cut']} floor={r['floor']} cross={r['cross']} stop={r['stop']}  win {r['win%']:.0f}%  "
          f"gross ${r['gross']:,.0f}  yours ${r['yours']:,.0f}  avg ${r['avg']:,.0f}  "
          f"maxDD ${r['maxDD']:,.0f}  PF {r['PF']:.2f}  streak {r['streak']}")
    print(f"{'month':<9}{'trades':>7}{'cut':>5}{'held':>5}{'win%':>6}{'$':>10}{'yours':>10}{'avg bars':>9}")
    for m, g in t.groupby("m"):
        held = g[g["reason"] != "cut"]
        print(f"{m:<9}{len(g):>7}{(g['reason'] == 'cut').sum():>5}{len(held):>5}{100 * (g['pnl_usd'] > 0).mean():>5.0f}%"
              f"{g['pnl_usd'].sum():>10,.0f}{g['pnl_usd'].sum() * a.split:>10,.0f}{held['bars'].mean() if len(held) else 0:>9.1f}")
    sk = tr_skips(r)
    if sk:
        print("skips: " + ", ".join(f"{k} {v}" for k, v in sk.items()))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--tf", default="M30")
    ap.add_argument("--from", dest="start", default="2026-01-01")
    ap.add_argument("--to", dest="end", default="2026-10-31")
    ap.add_argument("--csv-dir", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=Path("e_out"))
    # entry filters
    ap.add_argument("--spike", type=float, default=0.15, help="skip if EMA9 slope 2 bars before cross > this (ATR/bar)")
    ap.add_argument("--no-spike-skip", action="store_true")
    ap.add_argument("--no-monday", action="store_true")
    ap.add_argument("--skip-after-held", action="store_true")
    ap.add_argument("--hours", default="0-24", help="server hours a-b for entries, b exclusive (e.g. 0-17)")
    # EMA 200 gates
    ap.add_argument("--with200", action="store_true", help="only enter when price is on the cross side of EMA 200")
    ap.add_argument("--slope200", action="store_true", help="skip when EMA 200 slopes against the cross")
    ap.add_argument("--slope200-tol", type=float, default=0.0, help="tolerance in ATR/bar for --slope200 (0 = any opposing slope)")
    ap.add_argument("--near200", type=float, default=0.0, help="skip when |price-EMA200| < this many ATR unless 21 is crossing the 200 (0 = off)")
    # range-regime gates
    ap.add_argument("--atr-gate", type=float, default=0.0, help="skip entries when ATR < this x its 60-day average (e.g. 0.7; 0 = off)")
    ap.add_argument("--chop-gate", type=int, default=0, help="skip entries when more than this many 9/21 crosses in the last 20 bars (e.g. 3; 0 = off)")
    ap.add_argument("--month-stop", type=float, default=0.0, help="stop new entries for the month once month PnL <= -this $ per lot (0 = off)")
    # cut
    ap.add_argument("--cut-bars", type=int, default=2)
    ap.add_argument("--cut-thr", type=float, default=0.0, help="cut if close <= entry + thr x ATR (0 = at/below entry)")
    # ladder
    ap.add_argument("--ladder", default="5:2,10:5,20:10", help="trigger:lock pairs")
    ap.add_argument("--step", type=float, default=10.0, help="after last trigger, lock +step per +step of MFE (0 = off)")
    ap.add_argument("--ladder-unit", choices=["pts", "atr"], default="pts")
    ap.add_argument("--stop-atr", type=float, default=0.0, help="disaster stop in ATR (0 = none)")
    # money
    ap.add_argument("--lot", type=float, default=1.0)
    ap.add_argument("--split", type=float, default=0.90)
    a = ap.parse_args()
    a.hours = tuple(int(x) for x in a.hours.split("-"))

    start, end = pd.Timestamp(a.start), pd.Timestamp(a.end) + pd.Timedelta(days=1)
    a.out.mkdir(parents=True, exist_ok=True)
    print(f"tf={a.tf} period={a.start}..{a.end} lot={a.lot} split={int(a.split * 100)}% | spike-skip={'off' if a.no_spike_skip else a.spike} "
          f"no-monday={a.no_monday} skip-after-held={a.skip_after_held} hours={a.hours[0]}-{a.hours[1]} | "
          f"200: with={a.with200} slope={a.slope200} near={a.near200 or 'off'} | range gates: atr<{a.atr_gate or 'off'} chop>{a.chop_gate or 'off'} month-stop={a.month_stop or 'off'} | cut bar+{a.cut_bars} thr={a.cut_thr}ATR | ladder {a.ladder} step {a.step} ({a.ladder_unit}) | stop={a.stop_atr or 'none'}xATR")
    if a.ladder_unit == "pts" and any(s != "XAUUSD" for s in a.symbols):
        print("NOTE: ladder is in price units (gold scale). For other symbols use --ladder-unit atr "
              "--ladder 0.25:0.1,0.5:0.25,1:0.5 --step 0.5 (gold M30 equivalent).")
    results = []
    for sym in sorted(a.symbols, key=lambda s: (s != "XAUUSD", s)):
        try:
            raw = load(sym, a.tf, start, end, a.csv_dir)
        except Exception as e:  # noqa: BLE001
            print(f"[{sym}] load failed: {e}", file=sys.stderr)
            continue
        df = add_indicators(raw)
        tr = backtest(df, sym, a)
        if tr.empty:
            print(f"[{sym}] no trades")
            continue
        tr.to_csv(a.out / f"trades_{sym}_{a.tf}.csv", index=False)
        r = summarize(tr, sym, a)
        if r:
            print_symbol(r, a)
            results.append(r)
    if results:
        print(f"\n=== RANKING ({a.tf}, {a.lot} lot) — sort by gross $; PF and maxDD decide between close ones ===")
        print(f"{'symbol':<8}{'trades':>7}{'win%':>6}{'gross $':>11}{'yours $':>10}{'avg $':>8}{'negMo':>6}{'worst mo':>10}"
              f"{'maxDD':>10}{'PF':>6}{'streak':>7}")
        for r in sorted(results, key=lambda r: -r["gross"]):
            print(f"{r['symbol']:<8}{r['trades']:>7}{r['win%']:>5.0f}%{r['gross']:>11,.0f}{r['yours']:>10,.0f}{r['avg']:>8,.0f}"
                  f"{r['neg_months']:>6}{r['worst_month']:>10,.0f}{r['maxDD']:>10,.0f}{r['PF']:>6.2f}{r['streak']:>7}")
    print(f"\nwritten: {a.out}/trades_<symbol>_{a.tf}.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
