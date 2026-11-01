#!/usr/bin/env python3
"""v3.0 acceptance test 2 — restart parity: kill the ema921e process mid-trade (paper / SimBroker), restart it, and check
the engine adopts the open position with the bars_in, floor and MFE an uninterrupted run has at that bar, and that what
it does next matches the uninterrupted run.

    python tools/restart_ema921e.py --csv data/XAUUSD_M30.csv

Settings = config/ema921e.yaml (the paper config: lot 0.3, ATR gate 0.7, month stop, 3×ATR disaster stop). Bars from the
CSV are fed exactly as in tools/replay_ema921e.py. Each case runs in real child processes on its own logs dir:
part 1 from 1 Jan to the kill bar, then SIGKILL (os._exit on Windows — no cleanup, nothing flushed but what the runner
already wrote); part 2 a fresh process that starts at the restart bar (warmup + reconcile) and runs to the end.

    A  immediate restart, the floor at/above the second rung (broker SL on the floor)
    B  immediate restart, bar 1 — no floor yet, the bar-2 cut still ahead
    C  restart after a gap, still open, the floor moved while the process was down (rebuilt by the engine replay)
    D  restart after a gap in which the engine would have exited → closed at once on restart, reason "<why> (late)"
    D2 the same, but (as on a live MT5) the floor broker SL filled while down → the closing deal logged, then flat
    E  the position closed by hand while the process was down → restart flat, the deal logged "external (while down)"

A, B, C: adoption = the reference's bars_in / floor / MFE / entry at that bar, and the whole trades + skips CSV equals the
uninterrupted run's. D: the late exit, then every trade opened after the restart equals the reference's. E: flat, then the
same. Exit 0 = all pass.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.replay_ema921e import ReplayFeed, load_bars                      # noqa: E402
from aureon_mt5.strategies.ema921e import config as C                       # noqa: E402
from aureon_mt5.strategies.ema921e.brokers import SimBroker                 # noqa: E402
from aureon_mt5.strategies.ema921e.mode import EMode                        # noqa: E402

START = "2026-01-01"


def rows_from(csv: str) -> list[dict]:
    df = load_bars(csv, START, "2026-10-31")
    return [{"time": int(pd.Timestamp(t).tz_localize("UTC").timestamp()), "open": float(o), "high": float(h), "low": float(l),
             "close": float(c)} for t, o, h, l, c in df[["time", "open", "high", "low", "close"]].itertuples(index=False)]


def first_n(rows) -> int:
    st = int(pd.Timestamp(START).tz_localize("UTC").timestamp())
    return next(i for i, r in enumerate(rows) if r["time"] >= st) + 1


def build(rows, n, log_dir) -> tuple[EMode, ReplayFeed]:
    feed = ReplayFeed(rows, n)
    settings, problems = C.load(str(ROOT / "config" / "ema921e.yaml"))
    assert not problems, problems
    cfg = types.SimpleNamespace(max_dd_usd=0, regime_gate="off", execution_enabled=False, allow_live=False,
                                daily_report_ist="23:00", server_utc_offset=0.0)
    r = EMode(cfg, settings, SimBroker("XAUUSD", feed, log_dir), log_dir=log_dir)
    r._show_gates = lambda bar: None                       # show-only, never a decision
    r.summary_tick = lambda now=None: None                 # wall clock
    return r, feed


def snap(r: EMode) -> dict | None:
    if r.eng.pos is None:
        return None
    ps = r.eng.pos; s = r.eng.status()
    return {"ticket": r.st.get("ticket"), "side": ps.side, "entry": ps.entry, "entry_fill": r.st.get("entry_fill"),
            "bars_in": s["bars_in"], "floor_atr": ps.floor_atr, "floor_px": ps.floor_price, "mfe_pts": s["mfe_pts"],
            "broker_floor": r.st.get("broker_floor") or 0.0}


def run_part(rows, start_n, stop_n, log_dir, *, kill=False, snap_out=None, fresh=False):
    """One process lifetime: start at start_n (startup = warmup + reconcile), feed to stop_n, then die or return."""
    r, feed = build(rows, start_n, log_dir)
    if fresh:
        r.set_enabled(True)
    r.startup()
    if snap_out:
        with open(snap_out, "w") as f:
            json.dump({"n": start_n, "pos": snap(r), "last_bar": r.last_bar}, f)
    while feed.n < stop_n:
        feed.n += 1
        r.poll_bars(); r.poll_tick()
    if kill:
        sys.stdout.flush()
        if hasattr(signal, "SIGKILL"):
            os.kill(os.getpid(), signal.SIGKILL)
        os._exit(9)
    return r


def reference(rows, log_dir) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    """Uninterrupted run; the position snapshot after every feed position n."""
    r, feed = build(rows, first_n(rows), log_dir)
    r.set_enabled(True); r.startup()
    per_n = {}
    while feed.n < len(rows):
        feed.n += 1
        r.poll_bars(); r.poll_tick()
        per_n[feed.n] = snap(r)
    return per_n, _csv(log_dir, "ema921e_trades.csv"), _csv(log_dir, "ema921e_skips.csv")


def _csv(d, name):
    p = os.path.join(d, name)
    return pd.read_csv(p) if os.path.exists(p) else pd.DataFrame()


def child(csv, log_dir, start_n, stop_n, *, kill, snap_out=None, fresh=False):
    cmd = [sys.executable, os.path.abspath(__file__), "--csv", csv, "--child", log_dir, str(start_n), str(stop_n)]
    cmd += (["--kill"] if kill else []) + (["--snap", snap_out] if snap_out else []) + (["--fresh"] if fresh else [])
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    if kill and res.returncode == 0:
        raise RuntimeError("the child was supposed to be killed")
    if not kill and res.returncode != 0:
        raise RuntimeError(f"child failed: {res.stderr[-2000:]}")
    return res.returncode


def pick_cases(per_n, rows) -> dict:
    ns = sorted(per_n)
    def open_at(n): return per_n.get(n)
    def same(n1, n2): return open_at(n1) and open_at(n2) and open_at(n1)["ticket"] == open_at(n2)["ticket"]
    sr = 0.25
    A = next(n for n in ns if open_at(n) and open_at(n)["floor_atr"] is not None and open_at(n)["floor_atr"] >= sr
             and open_at(n)["bars_in"] >= 3 and same(n, n + 1))
    B = next(n for n in ns if n > A + 50 and open_at(n) and open_at(n)["bars_in"] == 1 and open_at(n)["floor_atr"] is None
             and same(n, n + 1))
    C = next((n, n + g) for n in ns if n > B + 50 for g in (3, 4, 5) if open_at(n) and same(n, n + g)
             and open_at(n + g)["floor_atr"] is not None and open_at(n)["floor_atr"] != open_at(n + g)["floor_atr"])
    D = next((n, n + 3) for n in ns if n > C[1] + 50 and open_at(n) and open_at(n)["ticket"] and open_at(n)["bars_in"] >= 2
             and not open_at(n + 3))                       # live at the kill, flat in the reference by the restart
    E = next((n, n + 2) for n in ns if n > D[1] + 50 and open_at(n) and same(n, n + 2))
    return {"A": (A, A), "B": (B, B), "C": C, "D": D, "D2": D, "E": E}


def _norm(v):
    try:
        f = float(v)
        return "" if f != f else f"{f:.10g}"
    except (TypeError, ValueError):
        return str(v)


def cols(df, keys=("time", "dir", "exit_time", "reason", "bars", "entry", "exit", "pnl_usd")):
    """Values compared as numbers where numeric (a column with one empty cell is read as float: 2 == 2.0)."""
    return [[_norm(v) for v in row] for row in df[list(keys)].values.tolist()] if len(df) else []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=str(ROOT / "data" / "XAUUSD_M30.csv"))
    ap.add_argument("--child", nargs=3, metavar=("LOG_DIR", "START_N", "STOP_N"))
    ap.add_argument("--kill", action="store_true"); ap.add_argument("--fresh", action="store_true"); ap.add_argument("--snap")
    a = ap.parse_args(argv)
    rows = rows_from(a.csv)
    if a.child:
        d, s0, s1 = a.child
        run_part(rows, int(s0), int(s1), d, kill=a.kill, snap_out=a.snap, fresh=a.fresh)
        return 0

    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        per_n, ref_tr, ref_sk = reference(rows, os.path.join(tmp, "ref"))
        print(f"reference (uninterrupted, config/ema921e.yaml): {len(ref_tr)} trades "
              f"{ref_tr['reason'].value_counts().to_dict()} · {len(ref_sk)} skips")
        cases = pick_cases(per_n, rows)
        t = lambda n: pd.Timestamp(rows[n - 2]["time"], unit="s").strftime("%Y-%m-%d %H:%M")     # the last closed bar at n
        for name, (kill_n, restart_n) in cases.items():
            d = os.path.join(tmp, name); os.makedirs(d); sp = os.path.join(d, "adopt.json")
            child(a.csv, d, first_n(rows), kill_n, kill=True, fresh=True)
            if name == "D2":                                      # real MT5: the floor SL fills while we are down
                book = json.load(open(os.path.join(d, "ema921e_sim.json"))); p0 = book["positions"][0]
                book["closed"][str(p0["ticket"])] = p0["sl"]; book["positions"] = []; sl_at_kill = p0["sl"]
                json.dump(book, open(os.path.join(d, "ema921e_sim.json"), "w"))
            if name == "E":                                       # closed by hand while down
                book = json.load(open(os.path.join(d, "ema921e_sim.json")))
                book["closed"][str(book["positions"][0]["ticket"])] = book["positions"][0]["price_open"]; book["positions"] = []
                json.dump(book, open(os.path.join(d, "ema921e_sim.json"), "w"))
            child(a.csv, d, restart_n, len(rows), kill=False, snap_out=sp)
            got = json.load(open(sp))["pos"]; want = per_n[restart_n]; was = per_n[kill_n]
            tr, sk = _csv(d, "ema921e_trades.csv"), _csv(d, "ema921e_skips.csv")
            line = f"{name}: killed at {t(kill_n)} (#{was['ticket']} {'LONG' if was['side'] > 0 else 'SHORT'}, bars_in {was['bars_in']}"
            line += f", floor {('+%.2f ATR' % was['floor_atr']) if was['floor_atr'] is not None else 'none'}) · restart at {t(restart_n)} → "
            if name in ("A", "B", "C"):
                ok = got is not None and all(
                    (abs(got[k] - want[k]) < 1e-9 if isinstance(want[k], float) else got[k] == want[k]) for k in
                    ("ticket", "side", "entry", "entry_fill", "bars_in", "floor_atr", "mfe_pts") if want[k] is not None or got[k] is not None) \
                    and ((got["floor_px"] is None and want["floor_px"] is None) or abs(got["floor_px"] - want["floor_px"]) < 1e-9)
                ok_csv = cols(tr) == cols(ref_tr) and cols(sk, ("time", "reason")) == cols(ref_sk, ("time", "reason"))
                line += (f"adopted bars_in {got['bars_in']}, floor " + (f"{got['floor_px']:.2f} (+{got['floor_atr']:.2f} ATR)" if got and got['floor_px'] else "none")
                         + f", MFE +{got['mfe_pts']:.2f} · broker SL {got['broker_floor'] or 'disaster stop'}" if got else "NOT ADOPTED")
                line += f" | reference {want['bars_in']} / " + (f"{want['floor_px']:.2f}" if want['floor_px'] else "none")
                line += f" | rest of the run identical: {ok_csv}"
                if not (ok and ok_csv):
                    fails.append(name)
                if name == "A" and not (got and got["broker_floor"] and abs(got["broker_floor"] - got["floor_px"]) < 1e-9):
                    fails.append("A broker SL not on the floor")
            elif name == "D2":
                row = tr[tr["reason"].astype(str).str.contains("while down")]
                after = tr[pd.to_datetime(tr["time"]) > pd.Timestamp(rows[restart_n - 2]["time"], unit="s")]
                ref_after = ref_tr[pd.to_datetime(ref_tr["time"]) > pd.Timestamp(rows[restart_n - 2]["time"], unit="s")]
                ref_trade = ref_tr[ref_tr["time"].astype(str) == str(row["time"].iat[0])] if len(row) else ref_tr.iloc[0:0]
                ok = len(row) == 1 and got is None and row["reason"].iat[0] == "floor (broker SL, while down)" and len(ref_trade) == 1 \
                    and abs(float(row["exit"].iat[0]) - sl_at_kill) < 1e-6 and cols(after) == cols(ref_after)
                line += (f"MT5 had closed it at the broker floor SL in place at the kill ({sl_at_kill:.2f}): {row['reason'].iat[0]} @ "
                         f"{row['exit'].iat[0]:.2f} (reference, never down: {ref_trade['reason'].iat[0]} @ {ref_trade['exit'].iat[0]:.2f}"
                         f" — its floor kept rising) · logged, flat · later trades identical: "
                         f"{cols(after) == cols(ref_after)}" if len(row) == 1 and len(ref_trade) else "NOT LOGGED")
                if not ok:
                    fails.append(name)
            elif name == "D":
                late = tr[tr["reason"].astype(str).str.endswith("(late)")]
                after = tr[pd.to_datetime(tr["time"]) > pd.Timestamp(rows[restart_n - 2]["time"], unit="s")]
                ref_after = ref_tr[pd.to_datetime(ref_tr["time"]) > pd.Timestamp(rows[restart_n - 2]["time"], unit="s")]
                ref_trade = ref_tr[ref_tr["time"].astype(str) == str(late["time"].iat[0])] if len(late) else ref_tr.iloc[0:0]
                ok = len(late) == 1 and got is None and len(ref_trade) == 1 \
                    and late["reason"].iat[0].split(" ")[0] == ref_trade["reason"].iat[0] and cols(after) == cols(ref_after)
                line += (f"closed on restart: {late['reason'].iat[0]} @ {late['exit'].iat[0]:.2f} (reference: {ref_trade['reason'].iat[0]} "
                         f"@ {ref_trade['exit'].iat[0]:.2f}) · then flat · later trades identical: {cols(after) == cols(ref_after)}"
                         if len(late) == 1 and len(ref_trade) else "NO LATE EXIT")
                if not ok:
                    fails.append(name)
            else:
                after = tr[pd.to_datetime(tr["time"]) > pd.Timestamp(rows[restart_n - 2]["time"], unit="s")]
                ref_after = ref_tr[pd.to_datetime(ref_tr["time"]) > pd.Timestamp(rows[restart_n - 2]["time"], unit="s")]
                st = json.load(open(os.path.join(d, "ema921e_state.json")))
                ext = tr[tr["reason"].astype(str) == "external (while down)"]
                ok = got is None and not st.get("ticket") and len(ext) == 1 and cols(after) == cols(ref_after)
                line += (f"flat (MT5 had none) · logged {ext['reason'].iat[0] if len(ext) else 'NOTHING'} @ "
                         f"{(ext['exit'].iat[0] if len(ext) else float('nan')):.2f} · later trades identical: {cols(after) == cols(ref_after)}")
                if not ok:
                    fails.append(name)
            print(line)
    print("RESTART PARITY ✓" if not fails else f"RESTART PARITY ✗ {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
