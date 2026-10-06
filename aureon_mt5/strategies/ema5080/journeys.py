"""Turns raw EMA 20/50 crosses into journeys with the user's vocabulary:

  PB / PS   pre-bull / pre-short  — EMA20 converging on EMA50, cross not yet happened
  EB / ES   enter buy / enter short — a cross that passes the clean-entry filter
  CB / ESC  close buy / close short — the exit of that journey
  WP        whipsaw — a cross that failed the filter (no trade)

Everything is in price points; on gold $1 of price = $1 per ounce.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field

import numpy as np
import pandas as pd


@dataclass
class Rules:
    confirm_bars: int = 2       # bars after the cross the gap must keep growing
    late_entry_bars: int = 12   # after a WP for "gap did not expand", keep watching this many bars for a late entry
    pullback_wait_bars: int = 24  # extended at the cross -> wait this many bars for a pullback to the fast EMA, enter there
    reentry_max: int = 1        # pullback re-entries allowed per trend leg after a profitable close (0 = off)
    reentry_window: int = 36    # bars after a profitable close in which a re-entry may happen
    reentry_min_bars: int = 3   # the pullback must start at least this many bars after the close
    be_lock: float = 1.0        # once a trade has shown trail_arm, it can never close below this (breakeven lock)
    leg_mode: bool = False      # hold the leg: exit on close through the slow EMA / opposite cross / leg floor (no 15/12/lock)
    pre_entry: bool = False     # enter at the PB/PS pre-cross signal (gap shrinking, small) instead of waiting for the cross
    p_mode: bool = False        # P pre-cross: price closed through the fast EMA in the new direction, fast EMA slope has
                                # turned that way, gap shrinking and within p_gap_atr x avg range of the slow EMA
    p_gap_atr: float = 2.0
    p_slope_bars: int = 3
    p_need_slope: bool = False  # False = your P: price through EMA 50 against the EMA order + small gap. No slope test.
    p_sep_bars: int = 24        # a P is an APPROACH: the gap must have been >= p_sep_atr x avg range within the last p_sep_bars
    p_sep_atr: float = 1.5      #   (lines lying flat and tangled for hours produce no P)
    p_converge: bool = True     # and the gap must be shrinking (2 of the last 3 bars)
    wz_bars: int = 12           # WHIPSAW ZONE: price flipped sides of EMA 50 this many times...
    wz_flips: int = 3           #   ...within the last wz_bars -> no P; the cross itself (confirmed by one bar) is the entry
    p_flip: bool = True         # an opposite P while in a trade closes it (p_flip) and enters the other way
    entry_hours_ist: tuple = (5.5, 23.0)   # no new entries outside this IST window (hours, 24h)
    pre_stop: float = 6.0       # pre-entry hard stop (points against) until the lines actually cross
    pre_timeout: int = 8        # pre-entry: if the cross has not happened within this many bars, get out
    trail_on_fast: bool = False # trail / runner exit on a close through the FAST EMA (slow pairs); False = standard
    pyramid_adds: int = 0       # leg mode: add this many positions on pullbacks to the fast EMA (0 = base position only)
    pyramid_min_bars: int = 3   # earliest add after the previous entry
    leg_protect_at: float = 15.0  # leg mode: once the base position has shown this, the leg floor arms...
    leg_floor: float = 2.0        # ...at this many points on the base position (pullbacks to the fast EMA must survive)
    min_gap: float = 1.0        # EMA20-EMA50 gap needed at confirmation (price points)
    whipsaw_bars: int = 6       # opposite cross within this many bars = whipsaw
    target: float = 15.0        # probable target: once reached the trade is protected, not closed
    let_run: bool = True        # True = run past target on the EMA20 trail; False = close at target
    protect: float = 3.0        # after target, never give back more than this below target (floor = target - protect)
    trail_arm: float = 6.0      # once MFE reaches this, a close back through EMA20 exits
    max_bars: int = 96          # time stop (M5 bars)
    pre_gap: float = 2.5        # gap below which a converging pair is flagged PB/PS
    pre_shrink_bars: int = 3    # gap must have shrunk this many bars in a row
    # ---- filters (all on by default)
    max_extension: float = 0.0          # fixed cap on distance from EMA20 at entry (0 = off; use --max-ext to force a number)
    max_extension_atr: float = 2.5      # volatility cap: distance from EMA20 must be <= this x average bar range (20 bars)
    spike_mult: float = 3.0             # a bar whose range >= spike_mult x average range of the last 20 bars is a spike
    spike_lock: float = 2.0             # on a spike, if the trade is already >= spike_lock_min in profit, floor moves to entry + 2
    spike_lock_min: float = 6.0
    news_times: tuple = ()              # UTC epochs of high-impact releases (NFP added automatically)
    news_entry_block_min: int = 60      # no new entries from this many minutes before a release...
    news_rearm_min: int = 30            # ...until this many minutes after it
    news_flat_min: int = 15             # closing agent acts this many minutes before the release
    news_flat_min_profit: float = 1.0   # ...and only if the open trade is at least this much in profit
    news_flat_losers: bool = False      # False = never close a losing trade for news; True = flatten regardless
    m15_align: bool = True              # M15 EMA20 must be on the trade's side of EMA50 (last completed M15 bar)
    skip_sessions: tuple = ("asia",)    # no entries in these sessions
    min_slope50: float = 0.2            # EMA50 must be tilting the trade's way: |e_slow[j]-e_slow[j-4]| >= this
    slope_bars: int = 4


# ---- EMA 50/80 system. One rule set. Fixed by design.
#   entry : P — price has closed through EMA 50 against the current 50/80 order for 2 bars, the 50/80 gap within
#           2x avg range AND the lines are approaching: gap was >= 1.5x avg range in the last 24 bars and is shrinking.
#           Flat, tangled lines produce no P. WHIPSAW ZONE (price flipped sides of EMA 50 >= 3 times in the last hour):
#           no P at all; the entry is the 50/80 cross confirmed by the next bar closing beyond both lines.
#           Failing a P, the cross bar close. An opposite P flips an open trade.
#           No entries 23:00-05:30 IST.
#   ride  : no breakeven lock, no early trail. Until the lines cross: 6-point stop / 12-bar timeout.
#           After: stop only on a close through EMA 80. +15 arms a +10 floor; then exit on a close back through EMA 50.
#   cont. : OFF (reentry_max=0). One trade per leg. Set reentry_max=1 to allow a continuation after a +12 exit.
#   skip  : whipsaw (opposite cross within 6 bars), news (NFP auto + file), one position at a time
RULES = Rules(confirm_bars=0, late_entry_bars=0, min_gap=0.0, min_slope50=0.0,
              max_extension=0.0, max_extension_atr=0.0,                                  # no extension test: at a 50/80 cross gold is always 6-8 pts from EMA 50
              pre_entry=True, reentry_max=0,   # single trade per leg (continuation off)
              p_mode=True, pre_stop=6.0, pre_timeout=12,                                 # P entry, fails cheaply
              target=15.0, protect=5.0, trail_arm=1e9, be_lock=0.0,                      # ride: no breakeven lock, no early trail;
                                                                                         # +15 arms a +10 floor, then EMA 50 close
              m15_align=False, skip_sessions=(), leg_mode=False, pyramid_adds=0)
RULES_SILVER = Rules(confirm_bars=0, late_entry_bars=0, min_gap=0.0, min_slope50=0.0,
                     max_extension=0.0, max_extension_atr=0.0, pre_entry=True, reentry_max=0,
                     m15_align=False, skip_sessions=(), leg_mode=False, pyramid_adds=0,
                     target=0.25, protect=0.05, trail_arm=0.10, be_lock=0.02, pre_gap=0.04, spike_lock=0.03, spike_lock_min=0.10)
UNFILTERED = Rules(confirm_bars=0, late_entry_bars=0, min_gap=0.0, min_slope50=0.0, max_extension=0.0, max_extension_atr=0.0,
                   pre_entry=False, reentry_max=0, m15_align=False, skip_sessions=(), whipsaw_bars=0, spike_mult=1e9)


def rules_for(symbol: str) -> Rules:
    return RULES_SILVER if ("XAG" in symbol.upper() or "SILVER" in symbol.upper()) else RULES


from ...common.news import SESSIONS, session_of, nfp_times, load_news  # shared helpers live in common

def runner_whatif(journeys, df, rules, floors=(12.0, 10.0, 8.0)) -> list[dict]:
    """For trades that reached the target: result with different floors, EMA20-trail only, and the peak."""
    close = df["close"].to_numpy(); high = df["high"].to_numpy(); low = df["low"].to_numpy(); e_fast = df["ema_fast"].to_numpy(); e_slow = df["ema_slow"].to_numpy()
    n = len(df); out = []
    for jn in journeys:
        if jn.target_index is None:
            continue
        s = 1 if jn.direction == "long" else -1
        row = {"entry": jn.entry_time, "direction": jn.direction, "actual": jn.result, "actual_reason": jn.exit_reason, "peak": jn.mfe}
        for fl in list(floors) + [None]:
            res = None
            for k in range(jn.target_index + 1, n):
                adv = (low[k] - jn.entry_price) if s > 0 else (jn.entry_price - high[k])
                if fl is not None and adv <= fl:
                    res = fl; break
                if (s > 0 and close[k] < e_slow[k]) or (s < 0 and close[k] > e_slow[k]):
                    res = round(float(s * (close[k] - jn.entry_price)), 2); break
                if (s > 0 and close[k] < e_fast[k]) or (s < 0 and close[k] > e_fast[k]):
                    res = round(float(s * (close[k] - jn.entry_price)), 2); break
            row[f"floor_{int(fl)}" if fl is not None else "trail_only"] = res if res is not None else "open"
        out.append(row)
    return out


def news_whatif(journeys, df, rules, server_offset_h, minutes=(15, 30, 60, 120)) -> list[dict]:
    """For every trade that was open going into a release: P&L if flattened N minutes before, vs actual."""
    time = df["time"].to_numpy(); close = df["close"].to_numpy()
    out = []
    for jn in journeys:
        s = 1 if jn.direction == "long" else -1
        for r in rules.news_times:
            r_srv = r + server_offset_h * 3600
            still_open_near = (jn.exit_time or time[-1]) >= r_srv - rules.news_flat_min * 60
            if not (jn.entry_time < r_srv and still_open_near):
                continue
            row = {"entry": jn.entry_time, "release_utc": r, "actual": jn.result, "actual_reason": jn.exit_reason}
            for m in minutes:
                k = int(np.searchsorted(time, r_srv - m * 60, side="right")) - 1
                row[f"flat_{m}"] = round(float(s * (close[k] - jn.entry_price)), 2) if k >= jn.entry_index else None
            out.append(row)
    return out




# ------------------------------------------------------------------ journeys
@dataclass
class Journey:
    direction: str            # "long" | "short"
    cross_index: int
    entry_index: int          # confirmation bar (entry at its close)
    entry_time: int
    entry_price: float
    exit_index: int | None = None
    exit_time: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None   # target | floor | breakeven | trail | runner | stop_ema50 | opposite_cross | time | spike_lock | news_flat | open
    lock_index: int | None = None    # bar where the spike lock armed
    target_index: int | None = None  # bar where +target first printed (None if never)
    result: float = 0.0       # points captured (+/-)
    mfe: float = 0.0          # best unrealised
    mae: float = 0.0          # worst drawdown
    bars: int = 0
    session: str = ""
    grade: str = ""           # A | B | C
    late: bool = False        # entered after a whipsaw, once the gap finally expanded
    reentry: bool = False     # pullback re-entry in an established trend after a profitable close
    pullback: bool = False    # entered on the first pullback to the fast EMA because the cross itself was extended
    pre: bool = False         # entered at the pre-cross signal, before the lines crossed
    adds: list = field(default_factory=list)   # leg mode: [{"index","time","price","result"}] positions added on pullbacks
    leg_result: float = 0.0   # leg mode: base result + adds
    score: int = 0
    m15_trend: str = ""       # up | down | range at entry
    notes: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class CrossEvent:
    index: int
    time: int
    direction: str            # bull | bear
    label: str                # EB | ES | WP | F  (F = filtered out by a rule)
    reason: str = ""          # why WP / F


@dataclass
class PreCross:
    index: int
    time: int
    label: str                # PB | PS
    gap: float


def trend_series(df: pd.DataFrame, slope_bars: int = 4, min_slope: float = 0.0) -> list[str]:
    """Per-bar trend label from EMA state + EMA50 slope: up | down | range."""
    e_fast = df["ema_fast"].to_numpy(); e_slow = df["ema_slow"].to_numpy()
    out = []
    for i in range(len(df)):
        if i < slope_bars or np.isnan(e_slow[i]) or np.isnan(e_slow[i - slope_bars]):
            out.append("range"); continue
        sl = e_slow[i] - e_slow[i - slope_bars]
        if e_fast[i] > e_slow[i] and sl > min_slope: out.append("up")
        elif e_fast[i] < e_slow[i] and sl < -min_slope: out.append("down")
        else: out.append("range")
    return out


class Context:
    """Look up the last *completed* bar of a higher/lower timeframe at a given time (no look-ahead)."""
    def __init__(self, df: pd.DataFrame | None, tf_minutes: int, trend: list[str] | None = None):
        self.df = df; self.tf = tf_minutes; self.trend = trend
        self.t = df["time"].to_numpy() if df is not None else None

    def at(self, ts: int):
        if self.df is None: return None
        k = int(np.searchsorted(self.t, ts, side="right")) - 1   # bar containing ts
        if self.t is not None and k >= 0 and self.t[k] + self.tf * 60 > ts:
            k -= 1                                               # not finished yet -> previous bar
        return k if k >= 0 else None

    def state(self, ts: int) -> str | None:
        k = self.at(ts)
        if k is None: return None
        r = self.df.iloc[k]
        if np.isnan(r["spread"]): return None
        return "bull" if r["spread"] > 0 else "bear"

    def trend_at(self, ts: int) -> str:
        k = self.at(ts)
        return self.trend[k] if (k is not None and self.trend) else "range"


def _sign(v):
    return 0 if np.isnan(v) else (1 if v > 0 else -1 if v < 0 else 0)


def build(df: pd.DataFrame, rules: Rules, server_offset_h: float = 3.0,
          m15: Context | None = None, m1: Context | None = None, trade_from: int | None = None) -> dict:
    """`trade_from`: server-time epoch; crosses before it are context only (no PB/PS, no entries, not counted)."""
    gap = df["spread"].to_numpy()
    close = df["close"].to_numpy(); high = df["high"].to_numpy(); low = df["low"].to_numpy()
    e_fast = df["ema_fast"].to_numpy(); e_slow = df["ema_slow"].to_numpy(); time = df["time"].to_numpy()
    n = len(df)
    sign = np.array([_sign(g) for g in gap])
    rng_ = high - low
    avg_rng = pd.Series(rng_).rolling(20, min_periods=5).mean().shift(1).to_numpy()
    side = np.sign(close - e_fast)                                   # which side of EMA 50 each close is on
    flips = np.zeros(n, dtype=int)
    for k in range(1, n):
        lo_k = max(1, k - rules.wz_bars + 1)
        flips[k] = int(np.sum(side[lo_k:k + 1] != side[lo_k - 1:k]))
    whipsaw = flips >= rules.wz_flips

    m5_trend = trend_series(df, rules.slope_bars, rules.min_slope50) if m15 is None else None
    rng_ = high - low
    avg_rng = pd.Series(rng_).rolling(20, min_periods=5).mean().shift(1).to_numpy()
    is_spike = np.array([(not np.isnan(avg_rng[k])) and rng_[k] >= rules.spike_mult * avg_rng[k] for k in range(n)])
    cross_idx = [i for i in range(1, n) if sign[i] != 0 and sign[i - 1] != 0 and sign[i] != sign[i - 1]]
    cross_set = set(cross_idx)

    # ---- pre-cross flags: gap shrinking k bars in a row and small, no cross yet
    events: list[CrossEvent] = []
    pres: list[PreCross] = []
    pre_entries: list[tuple[int, str]] = []
    armed = True
    for i in range(rules.pre_shrink_bars, n):
        if i in cross_set:
            armed = True
            continue
        if trade_from is not None and time[i] < trade_from:
            continue
        if rules.p_mode and not armed and sign[i] != 0:
            # re-arm once price has closed back on the EMA-order side of EMA 50
            if (close[i] > e_fast[i]) if sign[i] > 0 else (close[i] < e_fast[i]):
                armed = True
        if not armed or sign[i] == 0:
            continue
        if rules.p_mode:
            # P: the coming cross is against the current EMA order
            d_new = "bear" if sign[i] > 0 else "bull"                 # 50 above 80 -> next cross is bearish
            s_new = -1 if d_new == "bear" else 1
            slope = e_fast[i] - e_fast[i - rules.p_slope_bars]
            cap = rules.p_gap_atr * (avg_rng[i] if not np.isnan(avg_rng[i]) else 0)
            through = (close[i] < e_fast[i] and close[i - 1] < e_fast[i - 1]) if s_new < 0 else (close[i] > e_fast[i] and close[i - 1] > e_fast[i - 1])
            shrinking = abs(gap[i]) < abs(gap[i - 1]) < abs(gap[i - 2])
            ok_slope = (slope * s_new > 0) if rules.p_need_slope else True
            lo_b = max(0, i - rules.p_sep_bars)
            sep_ok = (not np.isnan(avg_rng[i])) and np.nanmax(np.abs(gap[lo_b:i + 1])) >= rules.p_sep_atr * avg_rng[i]
            conv_ok = (sum(abs(gap[i - k]) < abs(gap[i - k - 1]) for k in range(3)) >= 2) if rules.p_converge else True
            if through and ok_slope and sep_ok and conv_ok and cap > 0 and abs(gap[i]) <= cap and whipsaw[i]:
                events.append(CrossEvent(i, int(time[i]), d_new, "F", f"whipsaw zone ({flips[i]} flips) — wait for the cross"))
                armed = False
                continue
            if through and ok_slope and sep_ok and conv_ok and cap > 0 and abs(gap[i]) <= cap:
                pres.append(PreCross(i, int(time[i]), "PB" if sign[i] < 0 else "PS", float(gap[i])))
                armed = False
                if rules.pre_entry:
                    pre_entries.append((i, d_new))
            continue
        shrinking = all(abs(gap[i - k]) < abs(gap[i - k - 1]) for k in range(rules.pre_shrink_bars))
        if shrinking and abs(gap[i]) < rules.pre_gap:
            pres.append(PreCross(i, int(time[i]), "PB" if sign[i] < 0 else "PS", float(gap[i])))
            armed = False  # one flag per approach
            if rules.pre_entry:
                pre_entries.append((i, "bull" if sign[i] < 0 else "bear"))

    # ---- crosses -> EB / ES / WP, then run the journey
    journeys: list[Journey] = []

    def try_enter(i: int, j: int, d: str, late: bool, reentry: bool = False, pullback: bool = False):
        """Entry checks at confirmation bar j for cross bar i. Returns (journey|None, fail_reason)."""
        sess = session_of(int(time[j]), server_offset_h)
        ist_h = (((int(time[j]) - server_offset_h * 3600 + 5.5 * 3600) % 86400) / 3600)
        lo_h, hi_h = rules.entry_hours_ist
        if not (lo_h <= ist_h < hi_h):
            return None, f"outside {lo_h:g}-{hi_h:g} IST"
        m15_state = m15.state(int(time[j])) if m15 else None
        slope50 = (e_slow[j] - e_slow[j - rules.slope_bars]) if j >= rules.slope_bars else 0.0
        slope_ok = (slope50 > 0) if d == "bull" else (slope50 < 0)
        if rules.m15_align and m15_state is not None and m15_state != d:
            return None, "against M15"
        if sess in rules.skip_sessions:
            return None, f"{sess} session"
        if rules.min_slope50 > 0 and not (slope_ok and abs(slope50) >= rules.min_slope50):
            return None, "EMA50 flat"
        ext = abs(close[j] - e_fast[j])
        ext_cap = rules.max_extension if rules.max_extension > 0 else (
            rules.max_extension_atr * avg_rng[j] if rules.max_extension_atr > 0 and not np.isnan(avg_rng[j]) else 0.0)
        if ext_cap > 0 and ext > ext_cap and not pullback and not (rules.p_mode and j == i and (i, d) in pre_entries):
            return None, f"extended {ext:.1f} (cap {ext_cap:.1f})"
        tj_utc = int(time[j]) - server_offset_h * 3600
        if any(r - rules.news_entry_block_min * 60 <= tj_utc < r + rules.news_rearm_min * 60 for r in rules.news_times):
            return None, "news"
        m15_trend = m15.trend_at(int(time[j])) if m15 else "range"
        m1_state = m1.state(int(time[j])) if m1 else None
        score, notes = 0, []
        if m15 is not None:
            if m15_state == d: score += 2; notes.append("M15 aligned")
            if m15_trend == ("up" if d == "bull" else "down"): score += 1; notes.append("M15 trending")
        else:
            m15_trend = m5_trend[j] if m5_trend else "range"
            if m15_trend == ("up" if d == "bull" else "down"): score += 2; notes.append("M5 trending")
            if abs(gap[j]) >= 1.5 * rules.min_gap: score += 1; notes.append("gap building")
        if abs(gap[j]) >= 2 * rules.min_gap: score += 1; notes.append("wide gap")
        if slope_ok and abs(slope50) >= 2 * max(rules.min_slope50, 1e-9): score += 1; notes.append("EMA50 steep")
        if sess in ("london", "ny"): score += 1; notes.append(sess)
        if m1_state == d: score += 1; notes.append("M1 agrees")
        if late: notes.append("late entry after whipsaw")
        if reentry: notes.append("pullback re-entry")
        if pullback: notes.append("pullback entry after extended cross")
        grade = "A" if score >= 6 else "B" if score >= 4 else "C"
        jn = Journey("long" if d == "bull" else "short", i, j, int(time[j]), float(close[j]),
                     session=sess, grade=grade, score=score, m15_trend=m15_trend, notes=", ".join(notes), late=late, reentry=reentry, pullback=pullback)
        return jn, ""

    def confirmed(j: int, d: str) -> bool:
        if rules.confirm_bars == 0:
            return True                                   # simple mode: the cross itself is the signal
        growing = all(abs(gap[j - k]) > abs(gap[j - k - 1]) for k in range(rules.confirm_bars))
        right_side = (close[j] > e_fast[j]) if d == "bull" else (close[j] < e_fast[j])
        return growing and abs(gap[j]) >= rules.min_gap and right_side

    def run_exit(jn: Journey, j: int, s: int):
        adv_now = (lambda k: low[k] - jn.entry_price) if s > 0 else (lambda k: jn.entry_price - high[k])
        for k in range(j + 1, n):
            fav = s * (high[k] - jn.entry_price) if s > 0 else s * (low[k] - jn.entry_price)
            adv = s * (low[k] - jn.entry_price) if s > 0 else s * (high[k] - jn.entry_price)
            jn.mfe = max(jn.mfe, fav); jn.mae = min(jn.mae, adv)
            reason = None; px = close[k]
            tk_utc = int(time[k]) - server_offset_h * 3600
            unreal = s * (close[k] - jn.entry_price)
            if any(r - rules.news_flat_min * 60 <= tk_utc < r for r in rules.news_times):
                if unreal >= rules.news_flat_min_profit or rules.news_flat_losers:
                    reason = "news_flat"
            lock_px = jn.entry_price + s * rules.spike_lock
            if reason:
                pass
            elif jn.lock_index is None and is_spike[k] and jn.mfe >= rules.spike_lock_min and adv_now(k) > rules.spike_lock:
                jn.lock_index = k
            if fav >= rules.target and jn.target_index is None:
                jn.target_index = k
                if not rules.let_run:
                    reason, px = "target", jn.entry_price + s * rules.target
            if reason:
                pass
            elif jn.lock_index is not None and k > jn.lock_index and adv_now(k) <= rules.spike_lock and jn.target_index is None:
                reason, px = "spike_lock", lock_px
            elif jn.target_index is not None and adv_now(k) <= rules.target - rules.protect and k > jn.target_index:
                reason, px = "floor", jn.entry_price + s * (rules.target - rules.protect)
            elif jn.target_index is None and jn.mfe >= rules.trail_arm and adv_now(k) <= rules.be_lock and k > jn.entry_index + 0 and fav < rules.trail_arm:
                reason, px = "breakeven", jn.entry_price + s * rules.be_lock
            elif (not (jn.pre and sign[k] == -s)) and ((s > 0 and close[k] < e_slow[k]) or (s < 0 and close[k] > e_slow[k])):
                reason = "stop_ema80"            # only once the lines have crossed (a P sits between the lines by design)
            elif k in cross_set and sign[k] != s and not jn.pre:
                reason = "opposite_cross"
            elif jn.pre and sign[k] == -s and adv <= -rules.pre_stop:
                reason, px = "pre_stop", jn.entry_price - s * rules.pre_stop      # lines not crossed yet: tight stop
            elif jn.pre and sign[k] == -s and k - jn.entry_index >= rules.pre_timeout and jn.mfe < rules.trail_arm:
                reason = "pre_timeout"        # cross never came
            elif jn.pre and sign[k] == -s and k >= 2 and abs(gap[k]) > abs(gap[k - 1]) > abs(gap[k - 2]) and jn.mfe < rules.trail_arm:
                reason = "pre_abort"          # lines separating again before crossing
            elif (jn.target_index is not None or jn.mfe >= rules.trail_arm) and ((s > 0 and close[k] < e_fast[k]) or (s < 0 and close[k] > e_fast[k])):
                reason = "runner" if jn.target_index is not None else "trail"
                if jn.target_index is None and s * (close[k] - jn.entry_price) < rules.be_lock:
                    reason, px = "breakeven", jn.entry_price + s * rules.be_lock
            elif k - j >= rules.max_bars:
                reason = "time"
            if reason:
                jn.exit_index, jn.exit_time, jn.exit_price, jn.exit_reason = k, int(time[k]), float(px), reason
                break
        if jn.exit_index is None:
            k = n - 1
            jn.exit_index, jn.exit_time, jn.exit_price, jn.exit_reason = k, int(time[k]), float(close[k]), "open"
        jn.result = round(s * (jn.exit_price - jn.entry_price), 2)
        jn.mfe = round(jn.mfe, 2); jn.mae = round(jn.mae, 2); jn.bars = jn.exit_index - j

    def run_leg(jn: Journey, j: int, s: int):
        """Leg mode: hold from the cross, add on pullbacks to the fast EMA, exit all together."""
        adv_now = (lambda k: low[k] - jn.entry_price) if s > 0 else (lambda k: jn.entry_price - high[k])
        last_add = j; pulled = False; armed = False
        for k in range(j + 1, n):
            fav = s * (high[k] - jn.entry_price) if s > 0 else s * (low[k] - jn.entry_price)
            adv = s * (low[k] - jn.entry_price) if s > 0 else s * (high[k] - jn.entry_price)
            jn.mfe = max(jn.mfe, fav); jn.mae = min(jn.mae, adv)
            if fav >= rules.target and jn.target_index is None:
                jn.target_index = k
            if jn.mfe >= rules.leg_protect_at:
                armed = True
            reason = None; px = close[k]
            tk_utc = int(time[k]) - server_offset_h * 3600
            unreal = s * (close[k] - jn.entry_price)
            if any(r - rules.news_flat_min * 60 <= tk_utc < r for r in rules.news_times) and (unreal >= rules.news_flat_min_profit or rules.news_flat_losers):
                reason = "news_flat"
            elif (s > 0 and close[k] < e_slow[k]) or (s < 0 and close[k] > e_slow[k]):
                reason = "leg_end_ema_slow"
            elif k in cross_set and sign[k] != s:
                reason = "opposite_cross"
            elif armed and adv_now(k) <= rules.leg_floor:
                reason, px = "leg_floor", jn.entry_price + s * rules.leg_floor
            elif k - j >= rules.max_bars * 2:
                reason = "time"
            if reason:
                jn.exit_index, jn.exit_time, jn.exit_price, jn.exit_reason = k, int(time[k]), float(px), reason
                break
            # ---- adds on pullbacks to the fast EMA
            if len(jn.adds) < rules.pyramid_adds and k >= last_add + rules.pyramid_min_bars:
                touched = (low[k] <= e_fast[k]) if s > 0 else (high[k] >= e_fast[k])
                wrong = (close[k] <= e_fast[k]) if s > 0 else (close[k] >= e_fast[k])
                if touched or wrong:
                    pulled = True
                if pulled and not wrong and ((close[k] > e_fast[k]) if s > 0 else (close[k] < e_fast[k])) and k > last_add + rules.pyramid_min_bars:
                    jn.adds.append({"index": k, "time": int(time[k]), "price": float(close[k]), "result": 0.0})
                    last_add = k; pulled = False
        if jn.exit_index is None:
            k = n - 1
            jn.exit_index, jn.exit_time, jn.exit_price, jn.exit_reason = k, int(time[k]), float(close[k]), "open"
        jn.result = round(s * (jn.exit_price - jn.entry_price), 2)
        for a in jn.adds:
            a["result"] = round(s * (jn.exit_price - a["price"]), 2)
        jn.leg_result = round(jn.result + sum(a["result"] for a in jn.adds), 2)
        jn.mfe = round(jn.mfe, 2); jn.mae = round(jn.mae, 2); jn.bars = jn.exit_index - j

    def wait_pullback(i: int, j: int, d: str, nxt_i: int):
        """Cross confirmed at j but extended: wait for a pullback to the fast EMA, enter on the close back. Returns (jn|None, j)."""
        s_ = 1 if d == "bull" else -1; pulled = False
        for jj in range(j + 1, min(j + rules.pullback_wait_bars, nxt_i - 1) + 1):
            if (s_ > 0 and close[jj] < e_slow[jj]) or (s_ < 0 and close[jj] > e_slow[jj]):
                return None, j                                   # leg failed before any pullback
            touched = (low[jj] <= e_fast[jj]) if s_ > 0 else (high[jj] >= e_fast[jj])
            wrong = (close[jj] <= e_fast[jj]) if s_ > 0 else (close[jj] >= e_fast[jj])
            if touched or wrong:
                pulled = True
            if pulled and not wrong:
                cand, why2 = try_enter(i, jj, d, late=False, pullback=True)
                if cand is None:
                    events.append(CrossEvent(jj, int(time[jj]), d, "F", why2)); pulled = False; continue
                return cand, jj
        return None, j

    def truncate(jn: Journey, k: int, reason: str):
        """Close journey jn at bar k's close and recompute its stats up to k."""
        s = 1 if jn.direction == "long" else -1
        j = jn.entry_index
        hi = high[j + 1:k + 1]; lo = low[j + 1:k + 1]
        jn.mfe = round(float(max((s * (hi - jn.entry_price)).max() if s > 0 else (s * (lo - jn.entry_price)).max(), 0.0)), 2) if k > j else 0.0
        jn.mae = round(float(min((s * (lo - jn.entry_price)).min() if s > 0 else (s * (hi - jn.entry_price)).min(), 0.0)), 2) if k > j else 0.0
        jn.target_index = jn.target_index if (jn.target_index is not None and jn.target_index <= k) else None
        jn.lock_index = jn.lock_index if (jn.lock_index is not None and jn.lock_index <= k) else None
        jn.exit_index, jn.exit_time, jn.exit_price, jn.exit_reason = k, int(time[k]), float(close[k]), reason
        jn.result = round(s * (jn.exit_price - jn.entry_price), 2); jn.bars = k - j

    def _continuation(last: Journey, d: str, s_: int, nxt_i: int):
        """Find the continuation re-entry after `last` closed in profit; None if the leg ends first."""
        k0 = last.exit_index; pulled = False
        for k in range(k0 + 1, min(k0 + rules.reentry_window, nxt_i - 1) + 1):
            if k < k0 + rules.reentry_min_bars:
                continue
            if sign[k] != s_ or np.isnan(e_slow[k]):
                return None
            if (s_ > 0 and close[k] < e_slow[k]) or (s_ < 0 and close[k] > e_slow[k]):
                return None
            wrong_side = (close[k] <= e_fast[k]) if s_ > 0 else (close[k] >= e_fast[k])
            touched = (low[k] <= e_fast[k]) if s_ > 0 else (high[k] >= e_fast[k])
            if wrong_side or touched:
                pulled = True
                if wrong_side:
                    continue
            if pulled and ((close[k] > e_fast[k]) if s_ > 0 else (close[k] < e_fast[k])):
                c, why = try_enter(last.cross_index, k, d, late=False, reentry=True)
                if c is None:
                    events.append(CrossEvent(k, int(time[k]), d, "F", why)); pulled = False; continue
                return c
        return None

    busy_until = -1   # bar index until which a position is open (no overlapping trades)
    # merge pre-entries with crosses as entry candidates (pre-entry first if both exist for the same approach)
    candidates = [(i, None) for i in cross_idx]
    if rules.pre_entry:
        candidates = sorted(candidates + [(i, d) for i, d in pre_entries])
    for ci, (i, pre_d) in enumerate(candidates):
        if pre_d is not None:
            # pre-cross entry: direction is where the gap is heading; enter at this bar's close
            if trade_from is not None and time[i] < trade_from:
                continue
            if i < busy_until:
                open_j = journeys[-1] if journeys else None
                if rules.p_flip and open_j is not None and open_j.exit_index > i and open_j.direction != ("long" if pre_d == "bull" else "short"):
                    truncate(open_j, i, "p_flip")                     # opposite P: close the open trade here, then flip
                    busy_until = i
                else:
                    continue
            d = pre_d
            jn, why = try_enter(i, i, d, late=False)
            if jn is None:
                events.append(CrossEvent(i, int(time[i]), d, "F", why)); continue
            jn.pre = True
            events.append(CrossEvent(i, int(time[i]), d, ("EB" if d == "bull" else "ES") + "·pre", jn.grade))
            if rules.leg_mode:
                run_leg(jn, i, 1 if d == "bull" else -1)
            else:
                run_exit(jn, i, 1 if d == "bull" else -1)
            busy_until = jn.exit_index
            journeys.append(jn)
            # continuation after a pre-entry: same loop as after a cross entry
            nxt_i = next((c for c in cross_idx if c > jn.exit_index), n)     # any cross ends the continuation search
            s_ = 1 if d == "bull" else -1; taken = 0; last = jn
            while taken < rules.reentry_max and last.exit_reason in ("floor", "runner", "trail", "news_flat") and last.result >= rules.trail_arm:
                cand = _continuation(last, d, s_, nxt_i)
                if cand is None:
                    break
                events.append(CrossEvent(cand.entry_index, int(time[cand.entry_index]), d, ("EB" if d == "bull" else "ES") + "·re", cand.grade))
                run_exit(cand, cand.entry_index, s_)
                busy_until = cand.exit_index
                journeys.append(cand); last = cand; taken += 1
            continue
        ci = cross_idx.index(i)
        if trade_from is not None and time[i] < trade_from:
            continue
        d = "bull" if sign[i] > 0 else "bear"
        if i < busy_until and rules.pre_entry:
            continue                       # pre-entry already holds this leg; the cross is not a second entry
        nxt = cross_idx[ci + 1] if ci + 1 < len(cross_idx) else None
        nxt_i = cross_idx[ci + 1] if ci + 1 < len(cross_idx) else n   # next cross (any direction) or end
        # whipsaw: opposite cross too soon
        if nxt is not None and nxt - i <= rules.whipsaw_bars:
            events.append(CrossEvent(i, int(time[i]), d, "WP", f"{nxt - i} bars"))
            continue
        conf = max(rules.confirm_bars, 1) if whipsaw[i] else rules.confirm_bars
        j = i + conf
        if j >= n:
            events.append(CrossEvent(i, int(time[i]), d, "WP", "waiting"))
            continue
        if whipsaw[i]:
            s_ = 1 if d == "bull" else -1
            right = (close[j] > e_fast[j] and close[j] > e_slow[j]) if s_ > 0 else (close[j] < e_fast[j] and close[j] < e_slow[j])
            if not right:
                events.append(CrossEvent(i, int(time[i]), d, "WP", "cross not confirmed in whipsaw zone"))
                continue
        if i < busy_until:
            events.append(CrossEvent(i, int(time[i]), d, "F", "in position"))
            continue
        jn = None
        if confirmed(j, d):
            jn, why = try_enter(i, j, d, late=False)
            if jn is None and why.startswith("extended"):
                events.append(CrossEvent(i, int(time[i]), d, "F", why + " → waiting for pullback"))
                jn, j = wait_pullback(i, j, d, nxt_i)
                if jn is None:
                    continue
            elif jn is None:
                events.append(CrossEvent(i, int(time[i]), d, "F", why)); continue
        else:
            # not confirmed at j: keep watching the same side for a late entry until the next cross
            events.append(CrossEvent(i, int(time[i]), d, "WP", "gap"))
            for jj in range(j + 1, min(i + rules.confirm_bars + rules.late_entry_bars, nxt_i - 1) + 1):
                if confirmed(jj, d):
                    cand, why = try_enter(i, jj, d, late=True)
                    if cand is not None:
                        jn, j = cand, jj
                    elif why.startswith("extended"):
                        events.append(CrossEvent(jj, int(time[jj]), d, "F", why + " → waiting for pullback"))
                        cand, jj2 = wait_pullback(i, jj, d, nxt_i)
                        if cand is not None:
                            cand.late = True; jn, j = cand, jj2
                    else:
                        events.append(CrossEvent(jj, int(time[jj]), d, "F", why))
                    break
            if jn is None:
                continue
        label = ("EB" if d == "bull" else "ES") + ("·late" if jn.late else "") + ("·pb" if jn.pullback else "")
        events.append(CrossEvent(j if jn.late else i, int(time[j if jn.late else i]), d, label, jn.grade))
        if rules.leg_mode:
            run_leg(jn, j, 1 if d == "bull" else -1)
        else:
            run_exit(jn, j, 1 if d == "bull" else -1)
        busy_until = jn.exit_index
        journeys.append(jn)
        # ---- pullback re-entries while this trend leg lasts (until the next cross)
        s_ = 1 if d == "bull" else -1
        taken = 0; last = jn
        while not rules.leg_mode and taken < rules.reentry_max and last.exit_reason in ("floor", "runner", "trail", "news_flat") and last.result >= rules.trail_arm:
            k0 = last.exit_index; pulled = False; cand = None
            for k in range(k0 + 1, min(k0 + rules.reentry_window, nxt_i - 1) + 1):
                if k < k0 + rules.reentry_min_bars:
                    continue                                           # too soon after the close to call it a pullback
                if sign[k] != s_ or np.isnan(e_slow[k]):
                    break                                              # trend leg over
                if (s_ > 0 and close[k] < e_slow[k]) or (s_ < 0 and close[k] > e_slow[k]):
                    break                                              # through EMA50 -> no re-entry
                slope50 = e_slow[k] - e_slow[k - rules.slope_bars]
                if not ((slope50 > 0) if s_ > 0 else (slope50 < 0)):
                    continue
                wrong_side = (close[k] <= e_fast[k]) if s_ > 0 else (close[k] >= e_fast[k])
                touched = (low[k] <= e_fast[k]) if s_ > 0 else (high[k] >= e_fast[k])
                if wrong_side or touched:
                    pulled = True
                    if wrong_side:
                        continue
                if pulled and ((close[k] > e_fast[k]) if s_ > 0 else (close[k] < e_fast[k])):
                    c, why = try_enter(i, k, d, late=False, reentry=True)
                    if c is None:
                        events.append(CrossEvent(k, int(time[k]), d, "F", why)); pulled = False; continue
                    cand = c; break
            if cand is None:
                break
            events.append(CrossEvent(cand.entry_index, int(time[cand.entry_index]), d, ("EB" if d == "bull" else "ES") + "·re", cand.grade))
            run_exit(cand, cand.entry_index, s_)
            busy_until = cand.exit_index
            journeys.append(cand); last = cand; taken += 1

    closed = [x for x in journeys if x.exit_reason != "open"]
    if rules.leg_mode:                              # leg mode: judge legs by their total
        for x in journeys:
            x.result = x.leg_result
    wins = [x for x in closed if x.result > 0]
    summary = {
        "trades": len(closed), "wins": len(wins),
        "win_rate": round(100 * len(wins) / len(closed), 1) if closed else 0.0,
        "net": round(sum(x.result for x in closed), 2),
        "avg_win": round(sum(x.result for x in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(x.result for x in closed if x.result <= 0) / max(1, len(closed) - len(wins)), 2),
        "worst_drawdown": round(float(min((x.mae for x in closed), default=0.0)), 2),
        "whipsaws": sum(e.label == "WP" for e in events),
        "filtered": sum(e.label == "F" and "waiting" not in e.reason for e in events),
        "filtered_reasons": {r: sum(1 for e in events if e.label == "F" and e.reason == r)
                             for r in sorted({e.reason for e in events if e.label == "F" and "waiting" not in e.reason})},
        "waited_for_pullback": sum("waiting" in e.reason for e in events),
        "by_grade": {g: {"trades": len([x for x in closed if x.grade == g]),
                         "net": round(sum(x.result for x in closed if x.grade == g), 2)} for g in "ABC"},
        "hit_target": sum(x.target_index is not None for x in closed),
        "spike_locks": sum(x.exit_reason == "spike_lock" for x in closed),
        "news_flats": sum(x.exit_reason == "news_flat" for x in closed),
        "late_entries": sum(x.late for x in closed),
        "reentries": sum(x.reentry for x in closed),
        "pullback_entries": sum(x.pullback for x in closed),
        "pre_entries": sum(x.pre for x in closed),
        "reentry_net": round(sum(x.result for x in closed if x.reentry), 2),
        "adds": sum(len(x.adds) for x in closed),
        "adds_net": round(sum(a["result"] for x in closed for a in x.adds), 2),
        "leg_mode": rules.leg_mode,
        "runner_whatif": runner_whatif(journeys, df, rules),
        "news_whatif": news_whatif(journeys, df, rules, server_offset_h),
        "best_run": round(max((x.result for x in closed), default=0.0), 2),
        "by_session": {},
    }
    for sname, *_ in SESSIONS:
        xs = [x for x in closed if x.session == sname]
        summary["by_session"][sname] = {"trades": len(xs), "net": round(sum(x.result for x in xs), 2)}
    return {"journeys": journeys, "events": events, "pre": pres, "summary": summary}
