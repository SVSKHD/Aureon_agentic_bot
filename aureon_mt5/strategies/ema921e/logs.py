"""v3.0 ema921e — the acceptance data.

    logs/ema921e_trades.csv   one row per closed trade: the backtester's trades_<SYMBOL>_<TF>.csv columns
                              (symbol, time, dir, entry, skipped, exit_time, exit, reason, bars, mfe_pts, pnl_pts, pnl_usd)
                              + fill_slippage_pts (the engine's exit price vs the actual fill, + = filled worse).
                              Prices are the real fills; times are server time like the backtester's.
    logs/ema921e_skips.csv    one row per skipped cross (engine skips: spike / monday / hours / low_atr / month_stop, and
                              refusals: off / stale / execution_off / paper_needs_demo / order_failed:… / …)

TradeLogs(runner) appends as the runner emits; read_trades() / read_skips() rebuild the runner's memory at startup.
"""
from __future__ import annotations

import csv
import os
import threading
from datetime import datetime, timezone

TRADE_COLUMNS = ["symbol", "time", "dir", "entry", "skipped", "exit_time", "exit", "reason", "bars", "mfe_pts", "pnl_pts",
                 "pnl_usd", "fill_slippage_pts"]
SKIP_COLUMNS = ["symbol", "time", "dir", "price", "reason", "refused", "atr_ratio", "s9_pre"]
TRADES_FILE, SKIPS_FILE = "ema921e_trades.csv", "ema921e_skips.csv"
_lock = threading.Lock()


def _srv(t) -> str:
    """Server time as the backtester writes it: 'YYYY-MM-DD HH:MM:SS'."""
    if t is None:
        return ""
    if isinstance(t, datetime):
        return t.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return str(t)


def _srv_ts(s: str) -> int | None:
    try:
        return int(datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def append(path: str, columns: list[str], row: dict):
    with _lock:
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in columns})


def read_trades(log_dir: str) -> list[dict]:
    try:
        with open(os.path.join(log_dir, TRADES_FILE), encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def read_skips(log_dir: str) -> list[tuple]:
    """(server ts, reason) for the runner's skip_log."""
    out = []
    try:
        with open(os.path.join(log_dir, SKIPS_FILE), encoding="utf-8") as f:
            for r in csv.DictReader(f):
                t = _srv_ts(r.get("time", ""))
                if t is not None:
                    out.append((t, r.get("reason", "")))
    except OSError:
        pass
    return out


class TradeLogs:
    def __init__(self, runner):
        self.r = runner
        self.trades = os.path.join(runner.log_dir, TRADES_FILE)
        self.skips = os.path.join(runner.log_dir, SKIPS_FILE)
        runner.listeners.append(self.on_event)

    def on_event(self, kind: str, d: dict):
        if kind == "exit":
            append(self.trades, TRADE_COLUMNS, {**d, "time": _srv(d.get("time")), "exit_time": _srv(d.get("exit_time"))})
        elif kind == "skip":
            side = int(d.get("side") or 0)
            append(self.skips, SKIP_COLUMNS, {"symbol": d.get("symbol"), "time": _srv(d.get("bar_time") or self.r.last_bar),
                                              "dir": "LONG" if side > 0 else "SHORT", "price": d.get("price"),
                                              "reason": d.get("reason"), "refused": bool(d.get("refused", False)),
                                              "atr_ratio": d.get("atr_ratio"), "s9_pre": d.get("s9_pre")})
