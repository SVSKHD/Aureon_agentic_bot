"""EMA 9/21 on M30 — v2.16.0. Fully mechanical: every 9/21 cross is taken at the bar close.

Researched Jan–Oct 2026 on XAUUSD M30 (43 weeks; Jan–Apr were held out and never used to tune anything).
Backtest, 1 lot, Mon–Fri, no Friday entries from 16:00: +$54k (exit e9) / +$60k (exit e9>e200), 25 green weeks of 43,
worst week −$15k, max drawdown −$28k. Size the lot from the drawdown, never from the profit (see README).

Vocabulary (events):
  PRE        EMA 9 converging on EMA 21 (gap < pre_gap_atr × ATR and narrowing for 2 bars) — informational card, no action
  EB / ES    the cross: enter buy / enter short at this bar's close — placed automatically
  F          a cross that was not taken (day / hour / Friday / news / in position / drawdown guard)

Exits (the GUARDIAN profile, one object for replay and live):
  broker SL = entry − stop (safety net, −30) · broker TP only if tp > 0 (default none — every cap tested cost money)
  once +lock_at (20) has printed → broker SL moved to entry + lock_to (15)
  exit_mode "e9"      first M30 close through EMA 9 against the trade → market close
  exit_mode "e9>e200" the EMA 9 rule until +lock_at has printed, then the first close through EMA 200
  Friday: flat at the last bar (friday_flat)
Everything in price points; on gold $1 of price = $1 per ounce = $100 per lot.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field

import numpy as np
import pandas as pd

from ...common.news import session_of
from .. import Guardian

GUARDIANS = {
    "XAU": Guardian(secure_at=20.0, secure_level=15.0, pre_stop=30.0, ride_step=0.0, ema_slow_sl_buffer=0.0, enabled=True,
                    early_at=None, early_level=0.0, p_phase=False, slow_ema_sl=False, exit_on_confirmed_cross=False,
                    note="Gold profile (EMA 9/21 · M30): SL −30 · no TP · +20 → SL +15 · exit on the first close through EMA 9 · Friday flat"),
}


def guardian_for(symbol: str) -> Guardian | None:
    for k, g in GUARDIANS.items():
        if symbol.upper().startswith(k):
            return g
    return None


@dataclass
class Rules:
    # ---- entry
    days: tuple = (0, 1, 2, 3, 4)          # weekday numbers allowed for NEW entries (0 = Monday). Research: all five; Tue–Thu was a fit.
    friday_no_entry_from: float = 16.0     # server hour
    no_entry_server_hours: tuple = ()      # none — every session paid in the research
    # ---- pre-cross card
    pre_gap_atr: float = 0.35              # |EMA9 − EMA21| below this × ATR(14), narrowing 2 bars → PRE card
    pre_cooldown_bars: int = 6
    # ---- exits
    stop: float = 30.0                     # broker SL below entry (safety net)
    tp: float = 0.0                        # broker TP; 0 = none. Tested: +60 cost $11k/26 wks, +100 cost $4k — every cap cuts the runners
    lock_at: float = 20.0                  # once this much has printed...
    lock_to: float = 15.0                  # ...the broker SL goes to entry + this
    exit_mode: str = "e9"                  # "e9" | "e9>e200"
    n_close: int = 1                       # closes through the exit line needed
    friday_flat: bool = True
    # ---- news (common/news.py)
    news_times: tuple = ()
    news_entry_block_min: int = 60
    news_rearm_min: int = 30
    news_flat_min: int = 15
    guardian: Guardian = field(default_factory=lambda: GUARDIANS["XAU"])


GOLD = Rules()


def rules_for(symbol: str) -> Rules:
    return GOLD


def _bar_s() -> int:
    from ...common.tf_synth import bar_s
    return bar_s()


@dataclass
class Journey:
    direction: str; cross_index: int; entry_index: int; entry_time: int; entry_price: float
    exit_index: int | None = None; exit_time: int | None = None; exit_price: float | None = None; exit_reason: str | None = None
    lock_index: int | None = None; target_index: int | None = None
    result: float = 0.0; mfe: float = 0.0; mae: float = 0.0; bars: int = 0; session: str = ""; grade: str = ""
    late: bool = False; reentry: bool = False; pullback: bool = False; pre: bool = False; adds: list = field(default_factory=list)
    leg_result: float = 0.0; score: int = 0; m15_trend: str = ""; notes: str = ""; confirm_index: int | None = None
    shoot: float = 0.0; sl_final: float = 0.0; exit_rule: str = ""; guardian_profile: str = "ema921"

    def to_dict(self):
        return asdict(self)


@dataclass
class CrossEvent:
    index: int; time: int; direction: str; label: str; reason: str = ""; info: dict = field(default_factory=dict)


def server_hour(ts: int) -> float:
    return (ts % 86400) / 3600


def server_weekday(ts: int) -> int:
    return int(((ts // 86400) + 3) % 7)


def entry_block(ts: int, rules: Rules) -> str | None:
    wd = server_weekday(ts); sh = server_hour(ts)
    if wd not in rules.days:
        return "day not traded"
    if wd == 4 and sh >= rules.friday_no_entry_from:
        return f"Friday from {int(rules.friday_no_entry_from):02d}:00 server"
    for lo, hi in (rules.no_entry_server_hours or ()):
        if lo <= sh < hi:
            return f"no-entry hour {int(lo):02d}–{int(hi):02d}"
    return None


def news_block(ts: int, rules: Rules, off: float) -> bool:
    t = ts - off * 3600
    return any(r - rules.news_entry_block_min * 60 <= t < r + rules.news_rearm_min * 60 for r in rules.news_times)


def exit_line(rules: Rules, peak: float) -> str:
    return "ema200" if (rules.exit_mode == "e9>e200" and peak >= rules.lock_at) else "ema9"


FRIDAY_FLAT_FROM = 22.0      # live / paper: the first Friday bar opening at or after this server hour is the flat bar


def friday_last_bar(ts: int, rules: Rules) -> bool:
    return bool(rules.friday_flat and server_weekday(int(ts)) == 4 and server_hour(int(ts)) >= FRIDAY_FLAT_FROM)


def on_bar_close(rules: Rules, *, peak: float, locked: bool, close_pts: float, against: bool, run: int,
                 friday_last: bool) -> tuple[bool, str | None, int]:
    """v2.16.0 — the bar-close decision, shared by the live guardian and the paper tracker (same order as build()'s
    run_exit): (lock now?, close reason | None, the new against-run).
        lock_close   +lock_at has printed but this bar CLOSED below +lock_to → out at the close (the lock never arms intrabar)
        ema_close    n_close closes through the exit line (EMA 9, or EMA 200 after the lock in e9>e200)
        friday_flat  the Friday flat bar"""
    lock = False
    if peak >= rules.lock_at and not locked:
        if close_pts < rules.lock_to:
            return False, "lock_close", run
        lock = True
    run = run + 1 if against else 0
    if run >= rules.n_close:
        return lock, "ema_close", run
    if friday_last:
        return lock, "friday_flat", run
    return lock, None, run


def build(df: pd.DataFrame, rules: Rules, server_offset_h: float = 3.0, trade_from: int | None = None, day_end: int | None = None, **_ignored) -> dict:
    close = df["close"].to_numpy(); opn = df["open"].to_numpy(); high = df["high"].to_numpy(); low = df["low"].to_numpy()
    e9 = df["ema9"].to_numpy(); e21 = df["ema21"].to_numpy(); e200 = df["ema200"].to_numpy(); time = df["time"].to_numpy()
    atr = pd.Series(high - low).rolling(14).mean().to_numpy()
    n = len(df); sg = np.sign(e9 - e21)
    events: list[CrossEvent] = []; journeys: list[Journey] = []
    busy_until = -1; last_pre = -999
    last_k = n - 1
    if day_end is not None:
        inside = np.nonzero(time < day_end)[0]; last_k = int(inside[-1]) if len(inside) else n - 1

    def info_at(k, d):
        last3 = [f"{'G' if close[j] >= opn[j] else 'R'}{close[j] - opn[j]:+.1f}" for j in range(max(0, k - 2), k + 1)]
        return {"direction": "bull" if d > 0 else "bear", "ema9": float(e9[k]), "ema21": float(e21[k]), "ema200": float(e200[k]),
                "gap": float(e9[k] - e21[k]), "dist9": float(close[k] - e9[k]), "dist200": float(close[k] - e200[k]), "last3": last3,
                "session": session_of(int(time[k]), server_offset_h), "atr": float(atr[k]) if not np.isnan(atr[k]) else None}

    def run_exit(jn: Journey, k0: int, s: int) -> int:
        e = jn.entry_price; sl = -rules.stop; peak = 0.0; mae = 0.0; run = 0; end = last_k
        for k in range(k0 + 1, end + 1):
            hi = (high[k] - e) if s > 0 else (e - low[k]); lo = (low[k] - e) if s > 0 else (e - high[k])
            mae = max(mae, -lo)
            if lo <= sl:
                jn.exit_index, jn.exit_time, jn.exit_price = k, int(time[k]), e + s * sl
                jn.exit_reason = "stop" if sl < 0 else "locked"; jn.exit_rule = "broker_sl"; jn.result = sl; break
            if rules.tp and hi >= rules.tp:
                jn.exit_index, jn.exit_time, jn.exit_price = k, int(time[k]), e + s * rules.tp
                jn.exit_reason = "tp"; jn.exit_rule = "broker_tp"; jn.result = rules.tp; break
            peak = max(peak, hi)
            # the lock is ARMED AT THE BAR CLOSE (live guardian does the same): a bar that prints +lock_at and pulls back inside the
            # same bar does not stop the trade. If that bar closes below the lock level, the trade is closed at the bar close.
            if peak >= rules.lock_at and sl < rules.lock_to:
                cpts = (close[k] - e) * s
                if cpts < rules.lock_to:
                    jn.exit_index, jn.exit_time, jn.exit_price = k, int(time[k]), close[k]
                    jn.exit_reason = "locked"; jn.exit_rule = "lock_close"; jn.result = cpts; break
                sl = rules.lock_to; jn.lock_index = k
            line = e200 if exit_line(rules, peak) == "ema200" else e9
            against = (close[k] < line[k]) if s > 0 else (close[k] > line[k])
            run = run + 1 if against else 0
            if run >= rules.n_close:
                jn.exit_index, jn.exit_time, jn.exit_price = k, int(time[k]), close[k]
                jn.exit_reason = "ema_close"; jn.exit_rule = exit_line(rules, peak); jn.result = (close[k] - e) * s; break
            if rules.friday_flat and server_weekday(int(time[k])) == 4 and (k == end or server_weekday(int(time[k + 1])) != 4):
                jn.exit_index, jn.exit_time, jn.exit_price = k, int(time[k]), close[k]
                jn.exit_reason = "friday_flat"; jn.exit_rule = "friday"; jn.result = (close[k] - e) * s; break
        else:
            k = end; jn.exit_reason = "open"; jn.result = (close[k] - e) * s
        jn.mfe = round(peak, 2); jn.mae = round(mae, 2); jn.bars = (jn.exit_index or end) - k0; jn.sl_final = sl; jn.result = round(jn.result - 0.2, 2)
        return jn.exit_index if jn.exit_index is not None else end

    for k in range(201, n):
        if np.isnan(e200[k]) or np.isnan(atr[k]):
            continue
        # pre-cross (informational)
        gap = abs(e9[k] - e21[k]); gap1 = abs(e9[k - 1] - e21[k - 1]); gap2 = abs(e9[k - 2] - e21[k - 2])
        if gap < rules.pre_gap_atr * atr[k] and gap < gap1 < gap2 and k - last_pre > rules.pre_cooldown_bars and sg[k] == sg[k - 1]:
            d = -int(sg[k]) if sg[k] != 0 else 0
            if d:
                events.append(CrossEvent(k, int(time[k]), "bull" if d > 0 else "bear", "PRE", "converging", info_at(k, d))); last_pre = k
        # the cross
        if sg[k] != sg[k - 1] and sg[k] != 0:
            d = int(sg[k]); info = info_at(k, d); tradeable = trade_from is None or time[k] >= trade_from
            why = "in position" if k <= busy_until else (entry_block(int(time[k]), rules) or ("news" if news_block(int(time[k]), rules, server_offset_h) else None))
            if why:
                events.append(CrossEvent(k, int(time[k]), info["direction"], "F", why, info)); continue
            events.append(CrossEvent(k, int(time[k]), info["direction"], "EB" if d > 0 else "ES", "cross", info))
            if not tradeable:
                continue
            jn = Journey("long" if d > 0 else "short", k, k, int(time[k]), float(close[k]), session=info["session"])
            busy_until = run_exit(jn, k, d); journeys.append(jn)

    done = [j for j in journeys if j.exit_reason not in (None, "open")]; wins = [j for j in done if j.result > 0]
    summary = {"crosses": sum(1 for e in events if e.label in ("EB", "ES")), "pre": sum(1 for e in events if e.label == "PRE"),
               "trades": len(done), "wins": len(wins), "win_rate": round(len(wins) / len(done), 3) if done else 0.0,
               "net": round(sum(j.result for j in done), 2), "avg": round(sum(j.result for j in done) / len(done), 2) if done else 0.0,
               **{r: sum(1 for j in done if j.exit_reason == r) for r in ("stop", "locked", "tp", "ema_close", "friday_flat")}}
    bar_states = ["bull" if sg[i] > 0 else "bear" if sg[i] < 0 else "n/a" for i in range(n)]
    return {"events": events, "journeys": journeys, "summary": summary, "bar_states": bar_states, "cross_infos": [], "pre": [e for e in events if e.label == "PRE"]}
