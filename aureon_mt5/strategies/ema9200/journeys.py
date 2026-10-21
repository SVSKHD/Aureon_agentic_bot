"""EMA 9/200 on M30 — v2.15.0. The "you decide, I execute" mode.

Researched Apr–Oct 2026 on XAUUSD M30 (26 weeks) and three blind replay weeks traded by hand. Nothing here is derived
from ema5080 / ema2050.

What this mode does differently: it does NOT trade on its own signal. It finds CANDIDATES, posts each one once with full
context, and the human answers TAKE / SKIP. The research showed that every mechanical version of these entries loses
(−$50k…−$77k / 26 weeks at 1 lot) while the hand-picked subset wins — so the selection stays human, the execution is ours.

Vocabulary (events):
  EB / ES    a candidate: enter buy / enter short (reason "pullback" | "momentum")
  F          a bar that matched but was not offered (cooldown, in position, no-entry hour, news)

Bias      sign(EMA9 − EMA200) on the closed M30 bar.
Pullback  close within `pull_tol` of EMA9 on the bias side, and the bar reached back to within `pull_tol` of EMA9.
Momentum  bar body >= `mom_body` in the bias direction, close beyond EMA9.
Cooldown  one ask per cluster: `cooldown_bars` bars or `cooldown_move` points of travel since the last ask, whichever first.

Exits = the GUARDIAN profile (same object live + replay): −30 stop · secure at +40 → SL +30 · ride +10 steps ·
close on an EMA9 turn once secured · Friday last bar flat (day_end on Fridays) · no early lock · no exit-on-cross.
Everything is in price points; on gold $1 of price = $1 per ounce.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, asdict, field

import numpy as np
import pandas as pd

from ...common.news import session_of
from .. import Guardian

# --------------------------------------------------------------------------- guardian profiles (live + replay, ONE source)
GUARDIANS = {
    "XAU": Guardian(secure_at=40.0, secure_level=30.0, pre_stop=30.0, ride_step=10.0, ema_slow_sl_buffer=0.0, enabled=True,
                    early_at=None, early_level=0.0, p_phase=False, slow_ema_sl=False, exit_on_confirmed_cross=False,
                    note="Gold profile (EMA 9/200 · M30): −30 stop · +40 → SL +30 · ride +10 · close on EMA9 turn once secured · Friday flat"),
}


def guardian_for(symbol: str) -> Guardian | None:
    for k, g in GUARDIANS.items():
        if symbol.upper().startswith(k):
            return g
    return None


@dataclass
class Rules:
    # ---- candidates
    pull_tol: float = 8.0           # pullback: |close − EMA9| <= tol and the bar reached EMA9 ± tol, on the bias side
    mom_body: float = 30.0          # momentum: bar body >= this, in the bias direction, close beyond EMA9
    cooldown_bars: int = 4          # one ask per cluster...
    cooldown_move: float = 10.0     # ...re-armed earlier if price travelled this far since the last ask
    ask_ttl_bars: int = 1           # the ask is valid until the next bar close, then EXPIRED (= skip)
    # ---- time (server hours)
    no_entry_server_hours: tuple = (21.0, 24.0)   # same quiet window as the other modes
    friday_no_entry_from: float = 17.0            # no new asks Friday from this server hour
    friday_flat: bool = True                      # replay: close an open trade at Friday's last bar
    # ---- news (common/news.py)
    news_times: tuple = ()
    news_entry_block_min: int = 60
    news_rearm_min: int = 30
    news_flat_min: int = 15
    # ---- exits
    guardian: Guardian = field(default_factory=lambda: GUARDIANS["XAU"])
    ema_turn_close: bool = True     # close on a bar closing through EMA9 once secured (the human's "manual exit")
    hour_filter: bool = True
    news_filter: bool = True


GOLD = Rules()
UNFILTERED = Rules(hour_filter=False, news_filter=False, friday_no_entry_from=24.0)


def rules_for(symbol: str) -> Rules:
    return GOLD


def unfiltered_for(symbol: str) -> Rules:
    return UNFILTERED


def _bar_s() -> int:
    from ...common.tf_synth import bar_s
    return bar_s()


# --------------------------------------------------------------------------- data classes (shape kept for the renderers / verdict)
@dataclass
class Journey:
    direction: str            # "long" | "short"
    cross_index: int          # kept for interface compatibility: the candidate bar
    entry_index: int
    entry_time: int
    entry_price: float
    exit_index: int | None = None
    exit_time: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None   # stop | secured | ema9_turn | day_end | open
    lock_index: int | None = None
    target_index: int | None = None  # bar where +secure_at first printed
    result: float = 0.0
    mfe: float = 0.0
    mae: float = 0.0
    bars: int = 0
    session: str = ""
    grade: str = ""
    late: bool = False
    reentry: bool = False
    pullback: bool = False    # True = pullback candidate, False = momentum candidate
    pre: bool = False
    adds: list = field(default_factory=list)
    leg_result: float = 0.0
    score: int = 0
    m15_trend: str = ""
    notes: str = ""
    confirm_index: int | None = None
    shoot: float = 0.0
    sl_final: float = 0.0
    exit_rule: str = ""
    guardian_profile: str = "classic"

    def to_dict(self):
        return asdict(self)


@dataclass
class CrossEvent:
    index: int
    time: int
    direction: str            # bull | bear
    label: str                # EB | ES | F
    reason: str = ""
    info: dict = field(default_factory=dict)


def _sign(v):
    return 0 if np.isnan(v) else (1 if v > 0 else -1 if v < 0 else 0)


def server_hour(ts_server: int) -> float:
    return (ts_server % 86400) / 3600


def server_weekday(ts_server: int) -> int:
    """0 = Monday (server time is UTC+offset; the day boundary error is at most the offset, fine for a Friday rule)."""
    return int(((ts_server // 86400) + 3) % 7)   # 1970-01-01 was a Thursday


def entry_hour_block(ts_server: int, rules: Rules) -> str | None:
    sh = server_hour(ts_server); lo_s, hi_s = rules.no_entry_server_hours
    if lo_s <= sh < hi_s:
        return f"no-entry hour (server {int(sh):02d}:{int((sh % 1) * 60):02d})"
    if server_weekday(ts_server) == 4 and sh >= rules.friday_no_entry_from:
        return f"Friday after {int(rules.friday_no_entry_from):02d}:00 server"
    return None


def news_block(ts_server: int, rules: Rules, server_offset_h: float) -> bool:
    t_utc = ts_server - server_offset_h * 3600
    return any(r - rules.news_entry_block_min * 60 <= t_utc < r + rules.news_rearm_min * 60 for r in rules.news_times)


def candidate(df_close, df_open, df_high, df_low, e9, e200, k: int, rules: Rules) -> tuple[int, str]:
    """(+1 | −1 | 0, reason). The candidate rule on bar k, no filters."""
    b = _sign(e9[k] - e200[k])
    if b == 0 or np.isnan(e200[k]):
        return 0, ""
    d9 = df_close[k] - e9[k]
    if b == 1 and 0 < d9 <= rules.pull_tol and df_low[k] <= e9[k] + rules.pull_tol:
        return 1, "pullback"
    if b == -1 and -rules.pull_tol <= d9 < 0 and df_high[k] >= e9[k] - rules.pull_tol:
        return -1, "pullback"
    body = (df_close[k] - df_open[k]) * b
    if body >= rules.mom_body and d9 * b > 0:
        return b, "momentum"
    return 0, ""


def build(df: pd.DataFrame, rules: Rules, server_offset_h: float = 3.0, trade_from: int | None = None,
          day_end: int | None = None, take_all: bool = True, decisions: dict | None = None, **_ignored) -> dict:
    """Candidates → events; `take_all=True` replays every offered candidate through the guardian (the mechanical baseline
    the human is measured against). `decisions` = {bar_time: True|False} replays a recorded TAKE/SKIP log instead."""
    close = df["close"].to_numpy(); opn = df["open"].to_numpy(); high = df["high"].to_numpy(); low = df["low"].to_numpy()
    e9 = df["ema9"].to_numpy(); e200 = df["ema200"].to_numpy(); time = df["time"].to_numpy()
    n = len(df); g = rules.guardian
    events: list[CrossEvent] = []; journeys: list[Journey] = []
    busy_until = -1; last_ask_i = -10_000; last_ask_px = np.nan
    last_k = n - 1
    if day_end is not None:
        inside = np.nonzero(time < day_end)[0]
        last_k = int(inside[-1]) if len(inside) else n - 1

    def info_fields(k: int, d: int, reason: str) -> dict:
        last3 = [f"{'G' if close[j] >= opn[j] else 'R'}{close[j] - opn[j]:+.1f}" for j in range(max(0, k - 2), k + 1)]
        return {"direction": "bull" if d > 0 else "bear", "reason": reason, "ema9": float(e9[k]), "ema200": float(e200[k]),
                "dist9": float(close[k] - e9[k]), "dist200": float(close[k] - e200[k]), "last3": last3,
                "session": session_of(int(time[k]), server_offset_h), "bias": "bull" if e9[k] > e200[k] else "bear"}

    def run_exit(jn: Journey, k0: int, s: int):
        """Guardian replay from the bar after entry. An SL armed by bar k's extreme is tested from bar k+1."""
        e = jn.entry_price; sl = -g.pre_stop; secured = 0.0; peak = 0.0; mae = 0.0
        end = last_k
        for k in range(k0 + 1, end + 1):
            hi = (high[k] - e) if s > 0 else (e - low[k]); lo = (low[k] - e) if s > 0 else (e - high[k])
            mae = max(mae, -lo)
            if lo <= sl:                                              # stop / locked level hit (level in force at the open)
                jn.exit_index, jn.exit_time, jn.exit_price = k, int(time[k]), e + s * sl
                jn.exit_reason = "stop" if sl < 0 else "secured"; jn.exit_rule = "stop" if sl < 0 else "trail_hit"
                jn.result = sl; break
            peak = max(peak, hi)
            if secured < g.secure_level and peak >= g.secure_at:         # secure
                secured = g.secure_level; sl = max(sl, secured); jn.target_index = jn.target_index or k
            while secured >= g.secure_level and peak >= secured + g.ride_step + g.ride_step * 0.4:   # ride steps (2 pts air → 0.4×step)
                secured += g.ride_step; sl = max(sl, secured)
            if rules.ema_turn_close and secured >= g.secure_level and ((s > 0 and close[k] < e9[k]) or (s < 0 and close[k] > e9[k])):
                jn.exit_index, jn.exit_time, jn.exit_price = k, int(time[k]), close[k]
                jn.exit_reason = "ema9_turn"; jn.exit_rule = "ema_fast_close"; jn.result = (close[k] - e) * s; break
        else:
            k = end
            if day_end is not None and (time[k] + _bar_s() >= day_end):
                jn.exit_index, jn.exit_time, jn.exit_price = k, int(time[k]), close[k]
                jn.exit_reason = "day_end"; jn.exit_rule = "day_end"; jn.result = (close[k] - e) * s
            else:
                jn.exit_reason = "open"; jn.result = (close[k] - e) * s
        jn.mfe = round(peak, 2); jn.mae = round(mae, 2); jn.bars = (jn.exit_index or end) - k0; jn.sl_final = sl
        jn.result = round(jn.result, 2)
        return jn.exit_index if jn.exit_index is not None else end

    for k in range(200, n):
        if np.isnan(e200[k]):
            continue
        d, reason = candidate(close, opn, high, low, e9, e200, k, rules)
        if d == 0:
            continue
        tradeable = trade_from is None or time[k] >= trade_from
        why = None
        if k <= busy_until:
            why = "in position"
        elif (k - last_ask_i) < rules.cooldown_bars and (np.isnan(last_ask_px) or abs(close[k] - last_ask_px) < rules.cooldown_move):
            why = f"cooldown ({k - last_ask_i} bars since the last ask)"
        elif rules.hour_filter and entry_hour_block(int(time[k]), rules):
            why = entry_hour_block(int(time[k]), rules)
        elif rules.news_filter and news_block(int(time[k]), rules, server_offset_h):
            why = "news"
        info = info_fields(k, d, reason)
        if why:
            events.append(CrossEvent(k, int(time[k]), info["direction"], "F", why, info)); continue
        last_ask_i, last_ask_px = k, close[k]
        events.append(CrossEvent(k, int(time[k]), info["direction"], "EB" if d > 0 else "ES", reason, info))
        if not tradeable:
            continue
        take = take_all if decisions is None else bool(decisions.get(int(time[k]), False))
        if not take:
            continue
        jn = Journey("long" if d > 0 else "short", k, k, int(time[k]), float(close[k]), session=info["session"], pullback=(reason == "pullback"))
        busy_until = run_exit(jn, k, d)
        journeys.append(jn)

    offered = [e for e in events if e.label in ("EB", "ES") and (trade_from is None or e.time >= trade_from)]
    done = [j for j in journeys if j.exit_reason not in (None, "open")]
    wins = [j for j in done if j.result > 0]
    summary = {"candidates": len(offered), "pullback": sum(1 for e in offered if e.reason == "pullback"),
               "momentum": sum(1 for e in offered if e.reason == "momentum"),
               "trades": len(done), "wins": len(wins), "win_rate": round(len(wins) / len(done), 3) if done else 0.0,
               "net": round(sum(j.result for j in done), 2), "avg": round(sum(j.result for j in done) / len(done), 2) if done else 0.0,
               "stops": sum(1 for j in done if j.exit_reason == "stop"), "secured": sum(1 for j in done if j.exit_reason == "secured"),
               "ema9_turn": sum(1 for j in done if j.exit_reason == "ema9_turn"), "day_end": sum(1 for j in done if j.exit_reason == "day_end")}
    bar_states = ["bull" if _sign(e9[i] - e200[i]) > 0 else "bear" if _sign(e9[i] - e200[i]) < 0 else "n/a" for i in range(n)]
    return {"events": events, "journeys": journeys, "summary": summary, "bar_states": bar_states, "cross_infos": [], "pre": []}
