"""v2.10.0 — backtest the guardian (and the regime gate) on recorded M5 bars. No Claude, no Discord.

    python tools/replay_guardian.py --symbol XAUUSD --from 2026-10-01 --to 2026-10-15 \\
        --set giveback_pct=50 --set flip_require_structure=1 [--set setup_stop=1] [--set flip_use_zl=1] [--compare]
    python tools/replay_guardian.py --bars tests/fixtures/xauusd_2day_m5.json --trade 10-15T01:30,long,4103.5 \\
        --signal 10-15T02:05,short --compare --set giveback_pct=50 --set flip_require_structure=1

Bars: --bars <json|parquet>, else logs/bars/<SYMBOL>_<from>_<to>.parquet, else MT5 (broker.bars_tf). Entries: every journey
the strategy's own analysis finds, plus each --trade MM-DDTHH:MM,long|short[,price]. Each entry runs through the LIVE
`_guard` (tools/guardian_sim.py): fills at the bar close, stops honoured intrabar. Trades are scored independently.
Per combination: trades, win rate, avg win/loss, total points, max drawdown, close reasons. `--set regime_gate=block`
also reports entries_blocked → would_have_lost / would_have_won (the blocked trades are simulated anyway) and no_trade_days.
The summary is written to logs/replay/<date>.json so tuning.propose can cite it."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402

from tools.guardian_sim import hhmm, p_signals, simulate  # noqa: E402

SIGN_OFF = "the gate is acceptable when blocked losers >= 3 x blocked winners over the window"


def load_bars(args, base_tf: str = "M5") -> pd.DataFrame:
    if args.bars:
        if args.bars.endswith(".parquet"):
            df = pd.read_parquet(args.bars)
        else:
            d = json.load(open(args.bars, encoding="utf-8")); df = pd.DataFrame(d.get("bars", d))
    else:
        cache = os.path.join(ROOT, "logs", "bars", f"{args.symbol}_{args.date_from}_{args.date_to}"
                                                    f"{'' if base_tf == 'M5' else '_' + base_tf}.parquet")
        if os.path.exists(cache):
            df = pd.read_parquet(cache)
        else:
            from aureon_mt5 import broker
            if not broker.connect():
                sys.exit("no --bars file, no cached parquet and MT5 is not connected")
            df = broker.bars_tf(args.symbol, base_tf, 20000, "mt5", 3.0)
            a = int(datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
            b = int(datetime.strptime(args.date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) + 86400
            df = df[(df["time"] >= a - 300 * 300) & (df["time"] < b)].reset_index(drop=True)
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            try:
                df.to_parquet(cache)
            except Exception:
                pass
    return df.reset_index(drop=True)


def prepare(df: pd.DataFrame, mode: str = "ema5080", zl: bool = False) -> pd.DataFrame:
    from aureon_mt5.strategies import get_strategy
    S = get_strategy(mode)
    out = S.add_emas(df[["time", "open", "high", "low", "close"] + (["volume"] if "volume" in df else [])])
    if zl:
        from aureon_mt5.common.zl import add_zl
        out = add_zl(out)
    return out


def entries(bars: pd.DataFrame, trades: list[str], mode: str = "ema5080", symbol: str = "XAUUSD") -> list[dict]:
    from aureon_mt5.common.source import Bars
    from aureon_mt5.strategies import get_strategy
    df = bars[["time", "open", "high", "low", "close"]].copy(); df["volume"] = 1
    js = get_strategy(mode).analyse({"M5": Bars(symbol, "M5", df)}, symbol, 3.0, ())["M5"]["journeys"]
    out = [{"i": j.entry_index, "direction": j.direction, "entry": j.entry_price, "source": "analysis"} for j in js]
    for t in trades or []:
        parts = t.split(",")
        when, side = parts[0], parts[1].strip().lower()
        i = next((k for k, ts in enumerate(bars["time"]) if hhmm(ts).replace(" ", "T") == when), None)
        if i is None:
            sys.exit(f"--trade {t}: no bar at {when}")
        out.append({"i": i, "direction": side, "entry": float(parts[2]) if len(parts) > 2 else None, "source": "trade"})
    return sorted(out, key=lambda e: e["i"])


def parse_sets(sets: list[str]) -> dict:
    from aureon_mt5 import features as F
    out = {}
    for s in sets or []:
        k, v = s.split("=", 1)
        f = F.get(k.strip())
        out[f.attr] = F.parse(f, v)
    return out


def extra_signals(bars, specs: list[str]) -> dict:
    """--signal MM-DDTHH:MM,short|long → {bar_time: 'SHORT'|'LONG'} (a P seen live that the analysis of these bars misses)."""
    out = {}
    for spec in specs or []:
        when, side = [x.strip() for x in spec.split(",")[:2]]
        t = next((int(ts) for ts in bars["time"] if hhmm(ts).replace(" ", "T") == when), None)
        if t is None:
            sys.exit(f"--signal {spec}: no bar at {when}")
        out[t] = side.upper().replace("LONG", "LONG").replace("SHORT", "SHORT")
    return out


def htf_at(frames: dict, t_close: int, direction: str, over: dict) -> tuple[bool, dict, list]:
    """v2.14.0: the alignment gate at an exec bar that closed at t_close, on each confirm TF's bars closed by then."""
    from aureon_mt5 import htf_align as H
    from aureon_mt5.common.tf_synth import bar_s
    states = {}
    for tf, df in frames.items():
        d = df[df["time"] + bar_s(tf) <= t_close].reset_index(drop=True)
        states[tf] = H.htf_state(d.tail(300), "LONG" if direction == "long" else "SHORT",
                                 pre_max_gap=float(over.get("htf_pre_max_gap", 0.35)), with_max_bars=int(over.get("htf_with_max_bars", 8)))
    ok, reasons = H.gate(states, mode_min=over.get("htf_align_min", "all"), require_fresh=bool(over.get("htf_require_fresh", False)))
    return ok, states, reasons


def run_combo(bars, ents, over: dict, m15: str | None = "BULLISH", signals: dict | None = None, htf: dict | None = None) -> dict:
    sig = {**p_signals(bars), **(signals or {})}
    gate = over.get("regime_gate", "off")
    reg_by_i = {}
    if gate != "off":
        from aureon_mt5 import regime as R
        reg_by_i, days = R.replay_states(bars, over)
    rows, blocked, htf_blocked = [], [], []
    for e in ents:
        r = simulate(bars, e["i"], e["direction"], e["entry"], signals=sig, m15=m15, cfg_over=over)
        ex = r["exit"] or {"i": len(bars) - 1, "time": r["last_time"], "points": round((r["last_close"] - r["entry"]) * (1 if e["direction"] == "long" else -1), 2),
                            "reason": "open_at_end"}
        follow = over.get("guardian_profile") == "follow"
        if follow and ex["reason"] == "sl_hit":
            ex = {**ex, "reason": "stop"}                         # v2.13.0: the follow profile's only exit
        row = {"entry_time": hhmm(bars["time"].iloc[e["i"]]), "direction": e["direction"], "entry": round(r["entry"], 2),
               "exit_time": ex["time"], "points": ex["points"], "reason": ex["reason"], "source": e["source"],
               **({"sequence": follow_sequence(r["journal"], ex["reason"])} if follow else {}),
               "events": sorted({x["event"] for x in r["journal"] if x["event"] in ("opposite_p_ignored", "structure_sl", "pre_timeout_paused")})}
        st = reg_by_i.get(e["i"])
        hgate = over.get("htf_align", "off")
        if htf and hgate != "off":
            from aureon_mt5.common.tf_synth import bar_s
            ok, hst, why = htf_at(htf, int(bars["time"].iloc[e["i"]]) + bar_s(), e["direction"], over)
            row["htf"] = {tf: x["state"] for tf, x in hst.items()}
            row["htf_entry"] = "blocked" if not ok else ("pre" if any(x["state"] == "pre" for x in hst.values()) else "with")
            if hgate == "block" and not ok:
                row["htf_why"] = why; htf_blocked.append(row); continue
        if gate == "block" and st in ("CHOPPY", "DEAD"):
            row["regime"] = st; blocked.append(row)
        else:
            rows.append(row)
    res = summarize(rows)
    if gate != "off":
        res["entries_blocked"] = len(blocked)
        res["would_have_lost"] = sum(1 for b in blocked if b["points"] <= 0)
        res["would_have_won"] = sum(1 for b in blocked if b["points"] > 0)
        res["blocked_points"] = round(sum(b["points"] for b in blocked), 2)
        res["no_trade_days"] = len(days)
        res["sign_off"] = SIGN_OFF
        res["gate_acceptable"] = res["would_have_lost"] >= 3 * res["would_have_won"] if blocked else None
        res["blocked"] = blocked
    if htf and over.get("htf_align", "off") != "off":
        res["asks_blocked_by_htf"] = len(htf_blocked)
        res["htf_would_have_lost"] = sum(1 for b in htf_blocked if b["points"] <= 0)
        res["htf_would_have_won"] = sum(1 for b in htf_blocked if b["points"] > 0)
        res["htf_blocked_points"] = round(sum(b["points"] for b in htf_blocked), 2)
        res["htf_blocked"] = htf_blocked
        res["by_htf_entry"] = {k: summarize([r for r in rows if r.get("htf_entry") == k]) for k in ("pre", "with")}
    res["trades_detail"] = rows
    return res


def follow_sequence(journal: list[dict], exit_reason: str) -> str:
    """v2.13.0: `secured +3 → lock +3 → lock +10 → … → stop` from the follow profile's journal rows."""
    out = []
    for r in journal:
        if r.get("profile") != "follow":
            continue
        if r["event"] == "secured" and not out:
            out.append(f"secured +{r['level']:g}")
        elif r["event"] == "follow_lock":
            out.append(f"lock +{r['level']:g}")
    return " → ".join(out + [exit_reason])


def summarize(rows: list[dict]) -> dict:
    pts = [r["points"] for r in rows]
    wins = [p for p in pts if p > 0]; losses = [p for p in pts if p <= 0]
    eq = 0.0; peak = 0.0; dd = 0.0
    for p in pts:
        eq += p; peak = max(peak, eq); dd = max(dd, peak - eq)
    reasons: dict[str, int] = {}
    for r in rows:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        for ev in r["events"]:
            reasons[ev] = reasons.get(ev, 0) + 1
    return {"trades": len(rows), "win_rate": round(100 * len(wins) / len(rows), 1) if rows else 0.0,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0, "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
            "total_points": round(sum(pts), 2), "max_drawdown": round(dd, 2), "reasons": dict(sorted(reasons.items()))}


def table(name: str, r: dict) -> str:
    out = [f"{name}: trades {r['trades']} · win {r['win_rate']}% · avg win {r['avg_win']:+.2f} · avg loss {r['avg_loss']:+.2f} · "
           f"total {r['total_points']:+.2f} · max DD {r['max_drawdown']:.2f}",
           "   reasons: " + (", ".join(f"{k} {v}" for k, v in r["reasons"].items()) or "—")]
    for t in r["trades_detail"]:
        out.append(f"   {t['entry_time']} {t['direction']:<5} @ {t['entry']:.2f} → {t['exit_time']} {t['points']:+.2f} ({t['reason']})"
                   + (f" [{', '.join(t['events'])}]" if t["events"] else ""))
        if t.get("sequence"):
            out.append(f"      {t['sequence']}")
    if "asks_blocked_by_htf" in r:
        out.append(f"   HTF gate: asks_blocked_by_htf {r['asks_blocked_by_htf']} · would_have_lost {r['htf_would_have_lost']} · "
                   f"would_have_won {r['htf_would_have_won']} · blocked points {r['htf_blocked_points']:+.2f}")
        for k in ("pre", "with"):
            b = r["by_htf_entry"][k]
            out.append(f"   entered on `{k}`: trades {b['trades']} · win {b['win_rate']}% · total {b['total_points']:+.2f} · max DD {b['max_drawdown']:.2f}")
        for b in r["htf_blocked"]:
            out.append(f"   BLOCKED (HTF {', '.join(f'{k} {v}' for k, v in b['htf'].items())}) {b['entry_time']} {b['direction']} → "
                       f"{b['points']:+.2f} ({b['reason']})")
    if "entries_blocked" in r:
        out.append(f"   regime gate: entries_blocked {r['entries_blocked']} · would_have_lost {r['would_have_lost']} · "
                   f"would_have_won {r['would_have_won']} · blocked points {r['blocked_points']:+.2f} · no_trade_days {r['no_trade_days']}")
        for b in r["blocked"]:
            out.append(f"   BLOCKED ({b['regime']}) {b['entry_time']} {b['direction']} → {b['points']:+.2f} ({b['reason']})")
        out.append(f"   sign-off: {SIGN_OFF} → " + ("n/a (nothing blocked)" if r["gate_acceptable"] is None
                                                     else ("PASS" if r["gate_acceptable"] else "FAIL")))
    return "\n".join(out)


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", default="XAUUSD"); ap.add_argument("--mode", default="ema5080")
    ap.add_argument("--from", dest="date_from", default=""); ap.add_argument("--to", dest="date_to", default="")
    ap.add_argument("--bars"); ap.add_argument("--trade", action="append", default=[])
    ap.add_argument("--set", dest="sets", action="append", default=[]); ap.add_argument("--compare", action="store_true")
    ap.add_argument("--signal", action="append", default=[], help="extra P signal MM-DDTHH:MM,short|long (seen live, missed here)")
    ap.add_argument("--m15", default="BULLISH", help="M15 state fed to the guardian (no M15 bars in a replay); 'none' = unknown")
    ap.add_argument("--out", default=os.path.join(ROOT, "logs", "replay"))
    a = ap.parse_args(argv)
    over = parse_sets(a.sets)
    from types import SimpleNamespace
    from aureon_mt5.common import tf_synth
    exec_tf = str(over.get("exec_tf", "m5")).upper()               # v2.14.0: the execution timeframe of the replay
    base_tf = "M5" if (exec_tf == "M5" or tf_synth.tf_minutes(exec_tf) % 15) else "M15"
    raw = load_bars(a, base_tf)
    tf_synth.set_exec_tf(exec_tf, SimpleNamespace(**{k: over[k] for k in ("confirm_hold_bars", "confirm_gap_x_range",
                                                                          "confirm_window_bars") if k in over}))
    exec_raw = raw if exec_tf == "M5" else tf_synth.resample_tf(raw, exec_tf)
    bars = prepare(exec_raw, a.mode, zl=bool(over.get("zl_context") or over.get("flip_use_zl") or over.get("regime_gate", "off") != "off"))
    htf = None
    if over.get("htf_align", "off") != "off":
        from aureon_mt5.htf_align import confirm_tfs_for
        tfs = confirm_tfs_for(SimpleNamespace(exec_tf=exec_tf, htf_confirm_tfs=over.get("htf_confirm_tfs", ())))
        htf = {tf: prepare(tf_synth.resample_tf(raw, tf), a.mode) for tf in tfs}
    ents = entries(bars, a.trade, a.mode, a.symbol)
    m15 = None if a.m15.lower() == "none" else a.m15
    sig = extra_signals(bars, a.signal)
    result = {"symbol": a.symbol, "bars": len(bars), "from": hhmm(bars["time"].iloc[0]), "to": hhmm(bars["time"].iloc[-1]),
              "set": {k: (list(v) if isinstance(v, tuple) else v) for k, v in over.items()}}
    if a.compare:
        # v2.14.0: with the HTF gate in the set, the baseline is the SAME set with the gate off (block vs off); otherwise
        # the defaults on the same exec TF
        base_over = ({**over, "htf_align": "off"} if over.get("htf_align", "off") != "off"
                     else {"exec_tf": over["exec_tf"]} if "exec_tf" in over else {})
        base = run_combo(bars, ents, base_over, m15, sig); new = run_combo(bars, ents, over, m15, sig, htf=htf)
        result.update(defaults=base, combo=new,
                      delta={k: round(new[k] - base[k], 2) for k in ("trades", "win_rate", "total_points", "max_drawdown")})
        print(table("htf_align=off (same set)" if "htf_align" in over else "defaults", base)); print(table("set " + " ".join(a.sets), new))
        print("delta: " + " · ".join(f"{k} {v:+g}" for k, v in result["delta"].items()))
    else:
        result["combo"] = run_combo(bars, ents, over, m15, sig, htf=htf)
        print(table("set " + (" ".join(a.sets) or "(defaults)"), result["combo"]))
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, f"{datetime.now(timezone.utc):%Y-%m-%d}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, default=str)
    print(f"summary → {path}")
    tf_synth.set_exec_tf("M5")
    return result


if __name__ == "__main__":
    main()
