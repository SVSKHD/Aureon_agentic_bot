"""v3.0 ema921e — Discord cards. One line each (the card title; no fields, no chart), formats from the brief:

    ENTER LONG 4012.3 | ATR 18.4 | cut@bar2 | floor rungs 4.6/9.2/18.4 | stop 3957
    FLOOR +0.25 ATR → 4016.9
    EXIT floor +9.1 pts +$273 (bars 6)
    SKIP spike · LONG 4012.3

Cards(runner, notify) subscribes to the runner's events. daily_summary() is the 23:00 IST card (once per IST day).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
BACKTEST_PER_TRADE_1LOT = 400.0          # ≈ $410 / trade at 1 lot, Jan–Oct 2026 (291 trades, $119,346)
BACKTEST_TRADES_PER_WEEK = 7
COLOURS = {"enter": 0x2ECC71, "floor": 0x3498DB, "exit_win": 0x2ECC71, "exit_loss": 0xE67E22, "skip": 0x95A5A6,
           "info": 0x5865F2, "error": 0xE74C3C}


def _side(d) -> str:
    return "LONG" if int(d) > 0 else "SHORT"


def _g(x, nd=1) -> str:
    return f"{float(x):.{nd}f}"


def enter_line(d: dict) -> str:
    rungs = "/".join(_g(t) for t, _ in (d.get("ladder_pts") or []))
    stop = f" | stop {float(d['stop_px']):.0f}" if d.get("stop_px") else " | stop none"
    return (f"ENTER {_side(d['side'])} {_g(d['price'])} | ATR {_g(d.get('atr') or 0)} | cut@bar{d.get('cut_at_bar', 2)} | "
            f"floor rungs {rungs}{stop}")


def floor_line(d: dict) -> str:
    return f"FLOOR +{float(d['lock_atr']):.2f} ATR → {_g(d['price'])}"


def exit_line(d: dict) -> str:
    return f"EXIT {d['reason']} {float(d['pnl_pts']):+.1f} pts {_usd(d['pnl_usd'])} (bars {d.get('bars') if d.get('bars') is not None else '—'})"


def skip_line(d: dict) -> str:
    return f"SKIP {d['reason']} · {_side(d['side'])} {_g(d['price'])}"


def _usd(x) -> str:
    x = float(x)
    return f"{'+' if x >= 0 else '−'}${abs(x):,.0f}"


def ist_of(ts_server: int, off_h: float) -> datetime:
    return datetime.fromtimestamp(int(ts_server) - off_h * 3600, tz=IST)


def _server_ts(s) -> int | None:
    """'YYYY-MM-DD HH:MM:SS' (server clock, the trades CSV) → server epoch."""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return int(s)
    return int(datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())


def daily_summary(rows: list[dict], skips: list[tuple], *, day: str, off_h: float, lot: float) -> str:
    """rows: closed trades (trades CSV dicts); skips: (server_ts, reason). `day` = IST date YYYY-MM-DD."""
    def ist_day(r):
        t = _server_ts(r.get("exit_time"))
        return ist_of(t, off_h).strftime("%Y-%m-%d") if t is not None else ""
    today = [r for r in rows if ist_day(r) == day]
    month = [r for r in rows if ist_day(r)[:7] == day[:7]]
    c = Counter(str(r.get("reason", "")).split(" ")[0] for r in today)
    usd_day = sum(float(r.get("pnl_usd") or 0) for r in today)
    usd_month = sum(float(r.get("pnl_usd") or 0) for r in month)
    exp = BACKTEST_PER_TRADE_1LOT * lot
    sk = Counter(r for t, r in skips if ist_of(t, off_h).strftime("%Y-%m-%d") == day)
    won = sum(1 for r in today if float(r.get("pnl_usd") or 0) > 0)
    return (f"E DAY {day} · {len(today)} trades ({won} won) · cut {c['cut']} floor {c['floor']} cross {c['cross']} stop {c['stop']}"
            + (f" other {sum(c.values()) - c['cut'] - c['floor'] - c['cross'] - c['stop']}" if sum(c.values()) > c['cut'] + c['floor'] + c['cross'] + c['stop'] else "")
            + f" · day {_usd(usd_day)} · month {_usd(usd_month)} ({len(month)} trades)"
            + f" · backtest ≈ {_usd(exp)}/trade → {_usd(exp * len(today))} today, ~{BACKTEST_TRADES_PER_WEEK}/week"
            + (" · skips " + ", ".join(f"{k} {v}" for k, v in sk.items()) if sk else ""))


class Cards:
    """The runner's events → one-line cards on the trading channel."""

    def __init__(self, runner, notify):
        self.r, self.notify = runner, notify
        runner.listeners.append(self.on_event)

    def _send(self, key: str, line: str, colour: int, event: str | None = None, points: float | None = None):
        self.notify.send(f"ema921e:{key}", line, [], color=colour, footer=None, event=event, points=points)

    def on_event(self, kind: str, d: dict):
        bk = d.get("bar_time") or self.r.last_bar
        if kind == "enter":
            self._send(f"ENTER:{d.get('ticket')}", enter_line(d), COLOURS["enter"], event="FILLED")
        elif kind == "floor":
            self._send(f"FLOOR:{d.get('ticket')}:{d['lock_atr']}", floor_line(d), COLOURS["floor"])
        elif kind == "exit":
            win = float(d["pnl_usd"]) > 0
            self._send(f"EXIT:{d.get('time')}:{d.get('exit_time')}", exit_line(d), COLOURS["exit_win" if win else "exit_loss"],
                       event="CLOSED" if win else "STOP", points=float(d["pnl_pts"]))
        elif kind == "skip":
            self._send(f"SKIP:{bk}:{d['reason']}", skip_line(d), COLOURS["skip"])
        elif kind == "adopt":
            self._send(f"ADOPT:{d.get('ticket')}", f"ADOPTED #{d.get('ticket')} {_side(d['side'])} {_g(d['entry'])} | bars in "
                       f"{d.get('bars_in')} | floor {_g(d['floor_px']) if d.get('floor_px') is not None else 'none'}", COLOURS["info"])
        elif kind == "config":
            ch = ", ".join(f"{k} {a!r}→{b!r}" for k, (a, b) in d["changes"].items())
            self._send(f"CONFIG:{ch}", f"E CONFIG ({d['source']}) {ch}", COLOURS["info"])
        elif kind in ("error", "note"):
            self._send(f"{kind.upper()}:{d.get('text')}", f"E {'⚠️ ' if kind == 'error' else ''}{d.get('text')}",
                       COLOURS["error" if kind == "error" else "info"])
        elif kind == "summary":
            self._send(f"SUMMARY:{d['day']}", d["text"], COLOURS["info"])
