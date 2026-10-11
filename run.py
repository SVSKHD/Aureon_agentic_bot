"""Aureon MT5 — static replay for the selected mode: one day, or a date range with the research summary.
  python run.py --mode ema5080 --date 2026-10-05 --news news_blackout.txt
  python run.py --mode ema2050 --date 2026-10-07                        # one day: trades + the research block for that day
  python run.py --mode ema2050 --from 2026-07-01 --to 2026-10-09 --lot 1  # day-by-day replay + research summary + month table
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone

from aureon_mt5.strategies import UnknownMode, get_strategy, mode_help, resolve_mode
from aureon_mt5.common.news import load_news
from aureon_mt5.common.source import Bars, load_all, load_range

IST = timezone(timedelta(hours=5, minutes=30))
POINT_VALUE = {"XAU": 100.0, "XAG": 5000.0}     # $ per 1.0 price point per lot (100 oz gold, 5000 oz silver)
GOAL_PTS = 10.0


def analyse_day(S, bars_all, rng, symbol, off, news):
    """One trading day: EMAs on the full history, trades only inside the day, open trades closed at the day end (if the mode replays it)."""
    df = bars_all["M5"].df
    cut = df[df["time"] < rng["day_end"]].reset_index(drop=True)
    extra = {"day_end": rng["day_end"]} if getattr(S, "day_end_exit", False) else {}
    return S.analyse({"M5": Bars(symbol, "M5", cut)}, symbol, off, news, display_from=rng["prev_start"], trade_from=rng["day_start"], **extra)


def research_summary(days: list[dict], S, symbol: str, lot: float, point_value: float, off: float) -> str:
    """days: [{"day": date, "journeys": [...], "summary": {...}}]. Points, $ at --lot, offered/available/captured, counts, goal days,
    best/worst day, max drawdown (trade by trade, closed trades), month table."""
    closed = [(d["day"], j) for d in days for j in d["journeys"] if j.exit_reason != "open"]
    net = sum(j.result for _, j in closed)
    wins = [j for _, j in closed if j.result > 0]; losers = [j for _, j in closed if j.result <= 0]
    by_reason = {}
    for _, j in closed:
        by_reason[j.exit_reason] = by_reason.get(j.exit_reason, 0) + 1
    offered = sum(d["summary"].get("offered", 0.0) for d in days)
    available = sum(j.mfe for _, j in closed)
    captured = (100 * net / available) if available > 0 else 0.0
    day_net = OrderedDict((d["day"], round(sum(j.result for j in d["journeys"] if j.exit_reason != "open"), 2)) for d in days)
    goal_days = sum(1 for v in day_net.values() if v >= GOAL_PTS); losing_days = sum(1 for v in day_net.values() if v < 0)
    best = max(day_net.items(), key=lambda kv: kv[1]) if day_net else (None, 0.0)
    worst = min(day_net.items(), key=lambda kv: kv[1]) if day_net else (None, 0.0)
    eq = peak = 0.0; dd = 0.0; dd_from = dd_to = peak_day = None
    for d, j in closed:
        eq += j.result
        if eq > peak:
            peak, peak_day = eq, d
        if eq - peak < dd:
            dd, dd_from, dd_to = eq - peak, peak_day, d
    L = [f"RESEARCH SUMMARY · {S.display_name} · {symbol} · {days[0]['day']:%Y-%m-%d} → {days[-1]['day']:%Y-%m-%d} · {len(days)} trading days",
         f"profit {net:+.1f} pts  (${net * lot * point_value:,.0f} at {lot:g} lot, ${point_value:g}/pt/lot)   ·   "
         f"moves offered {offered:+.1f} · available at entry {available:+.1f} · captured {captured:.1f}%",
         f"trades {len(closed)} · winners {len(wins)} · losers {len(losers)} · " + " · ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(by_reason.items())),
         f"goal days (≥ +{GOAL_PTS:g}) {goal_days}/{len(days)} · losing days {losing_days} · "
         f"best day {best[1]:+.1f} ({best[0]}) · worst day {worst[1]:+.1f} ({worst[0]}) · "
         f"max drawdown {dd:+.1f} pts" + (f" ({dd_from} → {dd_to})" if dd_from else "")]
    months = OrderedDict()
    for d in days:
        m = months.setdefault(d["day"].strftime("%b %Y"), {"days": 0, "trades": 0, "win": 0, "lose": 0, "stops": 0, "net": 0.0, "goal": 0, "losing": 0})
        js = [j for j in d["journeys"] if j.exit_reason != "open"]; dn = day_net[d["day"]]
        m["days"] += 1; m["trades"] += len(js); m["win"] += sum(j.result > 0 for j in js); m["lose"] += sum(j.result <= 0 for j in js)
        m["stops"] += sum(j.exit_reason == "stop" for j in js); m["net"] += dn; m["goal"] += dn >= GOAL_PTS; m["losing"] += dn < 0
    L.append(f"{'month':<10}{'days':>5}{'trades':>7}{'win':>5}{'lose':>5}{'stops':>6}{'net pts':>10}{'$':>10}{'goal':>5}{'losing':>7}")
    for name, m in months.items():
        L.append(f"{name:<10}{m['days']:>5}{m['trades']:>7}{m['win']:>5}{m['lose']:>5}{m['stops']:>6}{m['net']:>+10.1f}{m['net'] * lot * point_value:>10,.0f}{m['goal']:>5}{m['losing']:>7}")
    return "\n".join(L)


def trade_line(j, off):
    t = datetime.fromtimestamp(j.entry_time - off * 3600, tz=IST); x = datetime.fromtimestamp(j.exit_time - off * 3600, tz=IST)
    extra = f" · {'pullback' if getattr(j, 'pullback', False) else 'no-pullback'}" if getattr(j, "confirm_index", None) is not None else ""
    return (f"  {j.direction:5} {t:%d %b %H:%M} @ {j.entry_price:.2f} → {x:%d %b %H:%M} @ {j.exit_price:.2f} · {j.result:+.2f} ({j.exit_reason}) · "
            f"peak {j.mfe:+.1f} · dd {j.mae:+.1f}{extra}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode"); ap.add_argument("--symbol", default="XAUUSD"); ap.add_argument("--date"); ap.add_argument("--bars", type=int, default=600)
    ap.add_argument("--from", dest="from_day"); ap.add_argument("--to", dest="to_day")
    ap.add_argument("--source", choices=["mt5", "csv", "synthetic"], default="mt5"); ap.add_argument("--csv"); ap.add_argument("--news"); ap.add_argument("--no-nfp", action="store_true")
    ap.add_argument("--server-utc-offset", type=float, default=3.0); ap.add_argument("--out", default="out")
    ap.add_argument("--lot", type=float, default=1.0); ap.add_argument("--point-value", type=float, help="$ per point per lot (default: 100 gold, 5000 silver)")
    ap.add_argument("--trades", action="store_true", help="range replay: list every trade")
    a = ap.parse_args()
    try:
        S = get_strategy(resolve_mode(a.mode))
    except UnknownMode as e:
        print(mode_help(str(e))); sys.exit(2)
    os.makedirs(a.out, exist_ok=True)
    off = a.server_utc_offset
    pv = a.point_value or next((v for k, v in POINT_VALUE.items() if a.symbol.upper().startswith(k)), 100.0)

    # ------------------------------------------------------------------ date range: day-by-day replay + research summary
    if a.from_day:
        d0 = date.fromisoformat(a.from_day); d1 = date.fromisoformat(a.to_day) if a.to_day else d0
        years = sorted({d0.year, d1.year})
        news = () if (a.no_nfp and not a.news) else tuple(sorted({t for y in years for t in load_news(a.news, y)}))
        bars_all, rngs = load_range(a.symbol, a.source, d0, d1, a.csv)
        days = []
        print(f"{S.display_name} · {a.symbol} · {d0} → {d1} · replay day by day (entries inside the server day, open trades closed at the day end)")
        for rng in rngs:
            if not len(bars_all["M5"].df[(bars_all["M5"].df["time"] >= rng["day_start"]) & (bars_all["M5"].df["time"] < rng["day_end"])]):
                continue                                                       # holiday / no bars
            m5 = analyse_day(S, bars_all, rng, a.symbol, off, news)["M5"]; sm = m5["summary"]
            days.append({"day": rng["day"], "journeys": m5["journeys"], "summary": sm})
            print(f"{rng['day']:%a %d %b}: {sm['trades']} trades · net {sm['net']:+.1f} · crosses {sm.get('crosses', '—')} · confirmed {sm.get('confirmed', '—')} · multi {sm.get('whipsaws', 0)}")
            if a.trades:
                for j in m5["journeys"]:
                    print(trade_line(j, off))
        if not days:
            print("no bars in that range"); sys.exit(1)
        block = research_summary(days, S, a.symbol, a.lot, pv, off)
        print("\n" + block)
        tag = f"_{d0:%Y%m%d}_{d1:%Y%m%d}_{S.name}"
        with open(os.path.join(a.out, f"{a.symbol}{tag}.json"), "w") as f:
            json.dump({"mode": S.name, "symbol": a.symbol, "from": str(d0), "to": str(d1), "lot": a.lot, "point_value": pv, "summary_text": block,
                       "days": [{"day": str(d["day"]), "summary": d["summary"], "journeys": [j.to_dict() for j in d["journeys"]]} for d in days]},
                      f, indent=2, default=str)
        return

    # ------------------------------------------------------------------ one day (or the latest bars): PNG, HTML, JSON, per-trade console
    day = date.fromisoformat(a.date) if a.date else None
    bars, rng = load_all(a.symbol, a.source, a.bars, a.csv, day, tfs=("M5",))
    year = (day or date.today()).year
    news = () if (a.no_nfp and not a.news) else load_news(a.news, year)
    extra = {"day_end": rng["day_end"]} if (rng and getattr(S, "day_end_exit", False)) else {}
    analysed = S.analyse(bars, a.symbol, off, news, display_from=rng["prev_start"] if rng else None, trade_from=rng["day_start"] if rng else None, **extra)
    tag = (f"_{rng['day']:%Y%m%d}" if rng else "") + f"_{S.name}"
    png = S.render_png(a.symbol, analysed, os.path.join(a.out, f"{a.symbol}{tag}.png"), off, rng=rng)
    html = S.render_html(a.symbol, analysed, os.path.join(a.out, f"{a.symbol}{tag}_replay.html"), off, 24, rng=rng)
    m5 = analysed["M5"]; sm = m5["summary"]
    print(f"{S.display_name} · {a.symbol}" + (f" · {rng['day']:%a %d %b}" if rng else ""))
    print(f"{sm['trades']} trades · net {sm['net']:+.2f} · win {sm['win_rate']}% · worst dd {sm['worst_drawdown']:+.2f} · removed {sm['filtered']} {sm['filtered_reasons']}")
    for j in m5["journeys"]:
        print(trade_line(j, off))
    if S.name == "ema2050":
        for e in m5["events"]:
            if e.label in ("CROSS", "CONFIRMED", "MULTI", "WP", "F"):
                print(f"    {datetime.fromtimestamp(e.time - off * 3600, tz=IST):%H:%M} IST  {e.label:<9} {e.direction:<4} {e.reason}")
        if rng:
            print("\n" + research_summary([{"day": rng["day"], "journeys": m5["journeys"], "summary": sm}], S, a.symbol, a.lot, pv, off))
    with open(os.path.join(a.out, f"{a.symbol}{tag}.json"), "w") as f:
        json.dump({"mode": S.name, "symbol": a.symbol, "summary": sm, "journeys": [j.to_dict() for j in m5["journeys"]]}, f, indent=2, default=str)
    print(png); print(html)


if __name__ == "__main__":
    main()
