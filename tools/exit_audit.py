"""v2.12.0 — which software exits close trades early? One-off diagnostic, read-only.

    python tools/exit_audit.py --days 7 [--log-dir logs] [--bars file.json|parquet] [--after 12] [--early 10]

For every journal `exit` row (a close the guardian made: ema_fast_turn, giveback, p_flip, pre_timeout, setup_invalidated, …)
it measures MFE over the next --after bars after the close: the best price in the trade's direction minus the close price,
in points. Per rule: count, avg points at close, avg MFE-after-close, and the share where price went >= --early points further
in the trade's direction within those bars ("closed early"). Bars: --bars, else logs/bars/*.parquet, else MT5.
Journal times are UTC; bar times are server time (--offset hours, default AUREON_SERVER_OFFSET or 3)."""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from collections import OrderedDict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
BAR_S = 300


def load_journal(log_dir: str, since: float) -> list[dict]:
    path = os.path.join(log_dir, "journal.jsonl")
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if float(r.get("t", 0)) >= since:
                    out.append(r)
    except OSError:
        pass
    return out


def load_bars(path: str | None, log_dir: str):
    import pandas as pd
    if path:
        if path.endswith(".parquet"):
            return pd.read_parquet(path)
        d = json.load(open(path, encoding="utf-8"))
        return pd.DataFrame(d.get("bars", d) if isinstance(d, dict) else d)
    files = sorted(glob.glob(os.path.join(log_dir, "bars", "*.parquet")))
    if files:
        return pd.concat([pd.read_parquet(f) for f in files]).drop_duplicates("time").sort_values("time").reset_index(drop=True)
    try:
        from aureon_mt5 import broker
        if broker.connect():
            return broker.bars_tf("XAUUSD", "M5", 20000, "mt5", 3.0)
    except Exception:
        pass
    return None


def audit(journal: list[dict], bars, *, after: int = 12, early: float = 10.0, offset_h: float = 3.0) -> "OrderedDict[str, dict]":
    """Per exit rule: {n, avg_points, avg_mfe_after, early_share, unmeasured}."""
    seen = {r.get("ticket"): r for r in journal if r.get("event") == "position_seen"}
    rows: "OrderedDict[str, dict]" = OrderedDict()
    for r in journal:
        if r.get("event") != "exit" or r.get("points") is None:
            continue
        rule = str(r.get("reason") or "?")
        ps = seen.get(r.get("ticket")) or {}
        s = 1 if str(ps.get("side", "")).upper() in ("LONG", "BUY") else -1 if ps else None
        t = rows.setdefault(rule, {"n": 0, "pts": [], "mfe": [], "early": 0, "unmeasured": 0})
        t["n"] += 1; t["pts"].append(float(r["points"]))
        if s is None or ps.get("entry") is None or bars is None or len(bars) == 0:
            t["unmeasured"] += 1; continue
        close_px = float(ps["entry"]) + s * float(r["points"])
        t_server = float(r["t"]) + offset_h * 3600
        nxt = bars[(bars["time"] > t_server - BAR_S) & (bars["time"] <= t_server + after * BAR_S)]
        if len(nxt) == 0:
            t["unmeasured"] += 1; continue
        best = float(nxt["high"].max()) if s > 0 else float(nxt["low"].min())
        mfe = (best - close_px) * s
        t["mfe"].append(mfe)
        t["early"] += int(mfe >= early)
    out: "OrderedDict[str, dict]" = OrderedDict()
    for rule, t in sorted(rows.items(), key=lambda kv: -kv[1]["n"]):
        m = len(t["mfe"])
        out[rule] = {"n": t["n"], "avg_points": round(sum(t["pts"]) / t["n"], 2),
                     "avg_mfe_after": round(sum(t["mfe"]) / m, 2) if m else None,
                     "early_share": round(100.0 * t["early"] / m) if m else None, "unmeasured": t["unmeasured"]}
    return out


def table(res, *, days: int, after: int, early: float) -> str:
    lines = [f"exit audit · last {days} days · MFE over {after} bars after the close · early = ≥ +{early:g} pts",
             f"{'rule':<20} {'n':>4} {'avg pts @close':>15} {'avg MFE after':>14} {'closed early':>13}"]
    if not res:
        lines.append("(no software exits in the journal for this window)")
    for rule, r in res.items():
        mfe = f"{r['avg_mfe_after']:+.2f}" if r["avg_mfe_after"] is not None else "—"
        er = f"{r['early_share']}%" if r["early_share"] is not None else "—"
        lines.append(f"{rule:<20} {r['n']:>4} {r['avg_points']:>+15.2f} {mfe:>14} {er:>13}"
                     + (f"   ({r['unmeasured']} without bars)" if r["unmeasured"] else ""))
    return "\n".join(lines)


def main(argv=None) -> str:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--log-dir", default=os.path.join(ROOT, "logs"))
    ap.add_argument("--bars")
    ap.add_argument("--after", type=int, default=12)
    ap.add_argument("--early", type=float, default=10.0)
    ap.add_argument("--offset", type=float, default=float(os.environ.get("AUREON_SERVER_OFFSET", "3")))
    a = ap.parse_args(argv)
    j = load_journal(a.log_dir, time.time() - a.days * 86400)
    bars = load_bars(a.bars, a.log_dir) if any(r.get("event") == "exit" for r in j) else None
    out = table(audit(j, bars, after=a.after, early=a.early, offset_h=a.offset), days=a.days, after=a.after, early=a.early)
    print(out)
    return out


if __name__ == "__main__":
    main()
