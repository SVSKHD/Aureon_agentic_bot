"""Aureon MT5 — static replay of one trading day for the selected mode.
  python run.py --mode ema5080 --date 2026-10-05 --news news_blackout.txt
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

from aureon_mt5.strategies import UnknownMode, get_strategy, mode_help, resolve_mode
from aureon_mt5.common.news import load_news
from aureon_mt5.common.source import load_all

IST = timezone(timedelta(hours=5, minutes=30))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode"); ap.add_argument("--symbol", default="XAUUSD"); ap.add_argument("--date"); ap.add_argument("--bars", type=int, default=600)
    ap.add_argument("--source", choices=["mt5", "csv", "synthetic"], default="mt5"); ap.add_argument("--csv"); ap.add_argument("--news"); ap.add_argument("--no-nfp", action="store_true")
    ap.add_argument("--server-utc-offset", type=float, default=3.0); ap.add_argument("--out", default="out")
    a = ap.parse_args()
    try:
        S = get_strategy(resolve_mode(a.mode))
    except UnknownMode as e:
        print(mode_help(str(e))); sys.exit(2)
    os.makedirs(a.out, exist_ok=True)
    day = date.fromisoformat(a.date) if a.date else None
    bars, rng = load_all(a.symbol, a.source, a.bars, a.csv, day, tfs=("M5",))
    year = (day or date.today()).year
    news = () if (a.no_nfp and not a.news) else load_news(a.news, year)
    analysed = S.analyse(bars, a.symbol, a.server_utc_offset, news, display_from=rng["prev_start"] if rng else None, trade_from=rng["day_start"] if rng else None)
    tag = (f"_{rng['day']:%Y%m%d}" if rng else "") + f"_{S.name}"
    png = S.render_png(a.symbol, analysed, os.path.join(a.out, f"{a.symbol}{tag}.png"), a.server_utc_offset, rng=rng)
    html = S.render_html(a.symbol, analysed, os.path.join(a.out, f"{a.symbol}{tag}_replay.html"), a.server_utc_offset, 24, rng=rng)
    m5 = analysed["M5"]; sm = m5["summary"]
    print(f"{S.display_name} · {a.symbol}" + (f" · {rng['day']:%a %d %b}" if rng else ""))
    print(f"{sm['trades']} trades · net {sm['net']:+.2f} · win {sm['win_rate']}% · worst dd {sm['worst_drawdown']:+.2f} · removed {sm['filtered']} {sm['filtered_reasons']}")
    for j in m5["journeys"]:
        t = datetime.fromtimestamp(j.entry_time - a.server_utc_offset * 3600, tz=IST); x = datetime.fromtimestamp(j.exit_time - a.server_utc_offset * 3600, tz=IST)
        print(f"  {j.direction:5} {t:%d %b %H:%M} @ {j.entry_price:.2f} → {x:%H:%M} @ {j.exit_price:.2f} · {j.result:+.2f} ({j.exit_reason}) · peak {j.mfe:+.1f} · dd {j.mae:+.1f}")
    with open(os.path.join(a.out, f"{a.symbol}{tag}.json"), "w") as f:
        json.dump({"mode": S.name, "symbol": a.symbol, "summary": sm, "journeys": [j.to_dict() for j in m5["journeys"]]}, f, indent=2, default=str)
    print(png); print(html)


if __name__ == "__main__":
    main()
