"""Aureon MT5 — DETECTOR. Verify what the selected strategy sees against your MT5 chart.

  python detect.py --mode ema5080 --date 2026-10-05
  python detect.py --mode ema5080 --live [--webhook ...]
"""
from __future__ import annotations

import argparse
import os
import sys
import time as _time
from datetime import date, datetime, timedelta, timezone

import numpy as np

from aureon_mt5 import broker
from aureon_mt5.strategies import UnknownMode, get_strategy, mode_help, resolve_mode
from aureon_mt5.common.news import load_news, session_of
from aureon_mt5.common.source import Bars, load_all

IST = timezone(timedelta(hours=5, minutes=30))


def fmt(ts, off):
    return f"raw {ts} · server {datetime.fromtimestamp(ts, tz=timezone.utc):%d %b %H:%M} · IST {datetime.fromtimestamp(ts - off * 3600, tz=IST):%H:%M}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode"); ap.add_argument("--symbol", default="XAUUSD"); ap.add_argument("--date"); ap.add_argument("--live", action="store_true")
    ap.add_argument("--webhook", default=os.environ.get("DISCORD_WEBHOOK")); ap.add_argument("--server-utc-offset", type=float, default=3.0)
    ap.add_argument("--poll", type=int, default=10); ap.add_argument("--source", choices=["mt5", "synthetic"], default="mt5"); ap.add_argument("--news")
    a = ap.parse_args()
    try:
        S = get_strategy(resolve_mode(a.mode))
    except UnknownMode as e:
        print(mode_help(str(e))); sys.exit(2)
    off = a.server_utc_offset
    news = load_news(a.news, date.today().year)
    cf, cs = S.ema_cols
    if a.date:
        day = date.fromisoformat(a.date)
        bars, rng = load_all(a.symbol, a.source, 600, None, day, tfs=("M5",))
        res = S.analyse(bars, a.symbol, off, news, display_from=rng["prev_start"], trade_from=rng["day_start"])["M5"]
        df = res["df"]; t = df["time"].to_numpy(); c = df["close"].to_numpy(); ef = df[cf].to_numpy(); es = df[cs].to_numpy()
        sign = np.sign(ef - es); start = int(np.searchsorted(t, rng["day_start"]))
        print(f"{a.symbol} · {S.display_name} · {day:%A %d %b %Y}\nopen state: EMA{S.fast} {ef[start]:.2f} {'>' if sign[start] > 0 else '<'} EMA{S.slow} {es[start]:.2f}\n" + "-" * 110)
        pre = {p.index: p for p in res.get("pre", [])}
        evs = {}
        for e in res.get("events", []):
            evs.setdefault(e.index, []).append(e)
        for i in range(max(start, 1), len(df)):
            tags = []
            if i in pre: tags.append(f"P {'SHORT' if pre[i].label == 'PS' else 'LONG'} coming")
            if sign[i] != 0 and sign[i - 1] != 0 and sign[i] != sign[i - 1]: tags.append("CROSS " + ("BULL" if sign[i] > 0 else "BEAR"))
            for e in evs.get(i, []): tags.append(f"{e.label} {e.reason}")
            if tags:
                print(f"{fmt(int(t[i]), off)} · close {c[i]:.2f} · EMA{S.fast} {ef[i]:.2f} · EMA{S.slow} {es[i]:.2f} · gap {ef[i]-es[i]:+.2f} · " + " | ".join(tags))
        return
    if not a.live:
        ap.error("give --date or --live")
    last = None; last_sign = None
    print(f"{a.symbol} · {S.display_name} · live detector · one line per closed M5 bar")
    while True:
        try:
            m5 = broker.bars(a.symbol, 400, a.source, off)
            closed = m5.df[m5.df["time"] + 300 <= broker.now_server(off)].reset_index(drop=True)
            if len(closed) < 100: _time.sleep(a.poll); continue
            bt = int(closed["time"].iloc[-1])
            if bt != last:
                last = bt; df = S.add_emas(closed); ef = float(df[cf].iloc[-1]); es = float(df[cs].iloc[-1]); c = float(df["close"].iloc[-1])
                sgn = 1 if ef > es else -1
                res = S.analyse({"M5": Bars(a.symbol, "M5", closed)}, a.symbol, off, news)["M5"]
                is_p = any(p.index == len(df) - 1 for p in res.get("pre", []))
                evs = [e for e in res.get("events", []) if e.index == len(df) - 1]
                wz = any("whipsaw" in e.reason for e in evs)
                blocked = [e.reason for e in evs if e.label == "F"]
                tutc = bt - off * 3600; news_now = any(r - 3600 <= tutc < r + 1800 for r in news)
                cross = "yes " + ("BULL" if sgn > 0 else "BEAR") if (last_sign is not None and sgn != last_sign) else "no"
                last_sign = sgn
                line = (f"\n{a.symbol} · {S.display_name}\n{fmt(bt, off)}\nClose {c:.2f} · EMA{S.fast} {ef:.2f} · EMA{S.slow} {es:.2f} · gap {ef-es:+.2f} · {'fast>slow' if sgn > 0 else 'fast<slow'}\n"
                        f"P {('yes ' + ('LONG' if sgn < 0 else 'SHORT')) if is_p else 'no'} · Cross {cross} · Whipsaw {'yes' if wz else 'no'} · News {'BLOCK' if news_now else 'clear'} · "
                        f"Entry {'allowed' if not blocked else 'blocked: ' + '; '.join(blocked)}")
                print(line, flush=True)
                if (is_p or cross != "no") and a.webhook:
                    try:
                        import requests; requests.post(a.webhook, json={"content": line}, timeout=10)
                    except Exception as e:
                        print("discord failed:", e)
        except KeyboardInterrupt:
            return
        except Exception as e:
            print("error:", e)
        _time.sleep(a.poll)


if __name__ == "__main__":
    main()
