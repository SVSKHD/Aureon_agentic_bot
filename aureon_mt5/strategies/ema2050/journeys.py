"""EMA 20/50 — the ACTIVE rules since v2.0.0 (researched Jul–Oct 2026 and Jun–Dec 2025, XAUUSD M5).

Vocabulary (events):
  CROSS      sign change of EMA20 − EMA50 on a closed bar (unconfirmed)
  CONFIRMED  the order has held every bar for >= confirm_bars bars AND |gap| >= confirm_gap, within confirm_window bars
             of the cross — at the FIRST bar that satisfies both
  MULTI      the order flipped back before the confirmation -> "multi cross", ignored (the flip is a new CROSS)
  EB / ES    enter buy / enter short — the pullback entry (reason "pullback" or "no-pullback" for the fallback)
  F          no entry at that bar (too far from EMA20 | no-entry hour | news | in position)
  WP         the cross never confirmed inside the window

Entry (pullback to EMA20) — v2.3.1: exactly research/ema2050_scan.py:
  * a touch on any bar in (cross, confirm] (from the bar AFTER the cross) -> enter at the confirm bar close ("pullback")
  * otherwise the first touch in (confirm, confirm + pullback_window] ("pullback")
  * else fallback at confirm + pullback_window only if |close − EMA20| <= max_chase and the order still holds ("no-pullback")
  * max_chase applies to the fallback ONLY, never to a touch entry
  * the only time filter: no entries server 21:00–23:59 (no IST window) · news block 60/30 min
Exits are the GUARDIAN's (replayed here from the Guardian profile): −pre_stop stop · early lock (+early_at seen ->
SL entry+early_level) · secure +10 -> SL +10 · ride +5 steps (2 pts air) · close on an EMA20 turn once secured ·
exit at the next CONFIRMED opposite cross · news flat · day end. No EMA50 follow-SL. One trade per cross.
Replay convention (same as the 50/80 replay): an SL level armed by bar k's extreme is tested from bar k+1; the stop check
on bar k uses the level in force at its open. Bar-close exits (EMA20 turn, opposite cross, day end) use the updated level.
Pre-cross shoot (report only): the move already made in the new direction over the shoot_bars bars before the cross.
Everything is in price points; on gold $1 of price = $1 per ounce.
"""
from __future__ import annotations

import dataclasses

from dataclasses import dataclass, asdict, field

import numpy as np
import pandas as pd

from ...common.news import SESSIONS, session_of
from .. import Guardian

# --------------------------------------------------------------------------- guardian profiles (live + replay, ONE source)
GUARDIANS = {
    "XAU": Guardian(secure_at=10.0, secure_level=10.0, pre_stop=12.0, ride_step=5.0, ema_slow_sl_buffer=0.0, enabled=True,
                    early_at=3.0, early_level=1.0, p_phase=False, slow_ema_sl=False, exit_on_confirmed_cross=True,
                    note="Gold profile (EMA 20/50): −12 stop · +3 → SL +1 · +10 → SL +10 · ride +5 · no EMA50 follow-SL"),
    "XAG": Guardian(secure_at=0.15, secure_level=0.15, pre_stop=0.18, ride_step=0.08, ema_slow_sl_buffer=0.0, enabled=False,
                    early_at=0.045, early_level=0.015, p_phase=False, slow_ema_sl=False, exit_on_confirmed_cross=True,
                    note="Silver profile — NOT validated for EMA 20/50, disabled"),
}


def guardian_for(symbol: str) -> Guardian | None:
    for k, g in GUARDIANS.items():
        if symbol.upper().startswith(k):
            return g
    return None


@dataclass
class Rules:
    # ---- detection
    confirm_bars: int = 3           # the order must hold every bar for this many bars (cross bar included)...
    confirm_gap: float = 1.5        # ...AND |EMA20 − EMA50| >= this (points) -> confirmed at the first bar with both
    confirm_window: int = 18        # ...within this many bars of the cross; any flip before -> multi cross
    confirm_gap_x_range: float = 0.0  # v2.14.0: if > 0 the gap test is |gap| >= this × 20-bar avg range (exec TF ≠ M5)
    touch_tol: float = 1.5          # pullback: low <= EMA20 + tol (longs) / high >= EMA20 − tol (shorts)
    pullback_window: int = 12       # first touch after the confirm bar, up to confirm + this
    max_chase: float = 5.0          # the FALLBACK entry must close within this of EMA20 (never applied to touch entries)
    # ---- v2.5.2 PRE-CROSS alert (notification only, never creates a trade)
    pre_alert: bool = True
    pre_gap_atr: float = 1.5         # gap must be <= this × avg range
    pre_sep_atr: float = 1.5         # lines must have been separated by >= this × avg range within pre_sep_bars
    pre_sep_bars: int = 24
    pre_through_bars: int = 2        # price must close through EMA20 for this many consecutive bars
    pre_cooldown_bars: int = 12      # one PRE per approach, re-arm after a cross or cooldown
    shoot_bars: int = 40            # pre-cross shoot: bars before the cross (report only)
    min_shoot: float = 0.0          # 0 = no filter (tested: filters failed 2025)
    no_entry_server_hours: tuple = (21.0, 24.0)   # no entries from 21:00 server to midnight — the only time filter
    entry_hours_ist: tuple | None = None          # v2.3.1: no IST window (the research has none); a tuple re-enables it
    # ---- kept at the researched OFF values (tested: trend/hold/filter variants failed 2025 — do not turn on)
    reentry_max: int = 0
    skip_sessions: tuple = ()
    m15_align: bool = False
    min_slope50: float = 0.0
    max_extension: float = 0.0
    max_extension_atr: float = 0.0
    slope_bars: int = 4             # display-only trend label (up/down/range) for the renderers
    # ---- news (common/news.py — same numbers as ema5080)
    news_times: tuple = ()
    news_entry_block_min: int = 60
    news_rearm_min: int = 30
    news_flat_min: int = 15
    news_flat_min_profit: float = 1.0
    news_flat_losers: bool = False
    # ---- exits = the guardian profile (replayed from the same object the live guardian uses)
    guardian: Guardian = field(default_factory=lambda: GUARDIANS["XAU"])
    day_end_close: bool = True      # replay: an open trade is closed at the last bar of the day ("day_end")
    hour_filter: bool = True        # UNFILTERED turns these off
    news_filter: bool = True


GOLD = Rules()
SILVER = Rules(confirm_gap=0.02, touch_tol=0.02, max_chase=0.07, guardian=GUARDIANS["XAG"])
UNFILTERED = Rules(hour_filter=False, news_filter=False)
UNFILTERED_SILVER = Rules(confirm_gap=0.02, touch_tol=0.02, max_chase=0.07, guardian=GUARDIANS["XAG"], hour_filter=False, news_filter=False)


def rules_for(symbol: str) -> Rules:
    s = symbol.upper()
    base = SILVER if ("XAG" in s or "SILVER" in s) else GOLD
    from ...common.tf_synth import strategy_overrides             # v2.14.0: exec TF ≠ M5 → minute-based windows, TF gap
    over = strategy_overrides("ema2050")
    if over and ("XAG" in s or "SILVER" in s):
        over = {k: v for k, v in over.items() if k not in ("confirm_gap", "confirm_gap_x_range")}
    return dataclasses.replace(base, **over) if over else base


def _bar_s() -> int:
    from ...common.tf_synth import bar_s
    return bar_s()


def unfiltered_for(symbol: str) -> Rules:
    s = symbol.upper()
    return UNFILTERED_SILVER if ("XAG" in s or "SILVER" in s) else UNFILTERED


# --------------------------------------------------------------------------- data classes (shape kept for the renderers)
@dataclass
class Journey:
    direction: str            # "long" | "short"
    cross_index: int
    entry_index: int          # entry at this bar's close
    entry_time: int
    entry_price: float
    exit_index: int | None = None
    exit_time: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None   # stop | early | secured | ema20_turn | opposite_cross | news_flat | day_end | open
    lock_index: int | None = None    # bar where the early lock armed
    target_index: int | None = None  # bar where +secure_at first printed (None if never)
    result: float = 0.0
    mfe: float = 0.0
    mae: float = 0.0
    bars: int = 0
    session: str = ""
    grade: str = ""
    late: bool = False
    reentry: bool = False
    pullback: bool = False    # True = entered on a touch of EMA20; False = fallback ("no-pullback")
    pre: bool = False
    adds: list = field(default_factory=list)
    leg_result: float = 0.0
    score: int = 0
    m15_trend: str = ""
    notes: str = ""
    confirm_index: int | None = None
    shoot: float = 0.0        # pre-cross shoot (points)
    sl_final: float = 0.0     # SL level (points from entry) in force at the exit
    exit_rule: str = ""       # v2.7.0: detailed exit rule name (trail_hit, ema_slow_close, leg_end, soft_trail, etc.)
    guardian_profile: str = "classic"  # v2.7.0: "classic" or "leg"

    def to_dict(self):
        return asdict(self)


@dataclass
class CrossEvent:
    index: int
    time: int
    direction: str            # bull | bear
    label: str                # CROSS | CONFIRMED | MULTI | WP | EB | ES | F
    reason: str = ""
    info: dict = field(default_factory=dict)   # cross_index, cross_time, confirm_index, confirm_time, shoot, bars_since_cross...


@dataclass
class PreCross:               # kept for interface compatibility: the 20/50 system has no P phase
    index: int
    time: int
    label: str
    gap: float


@dataclass
class CrossInfo:
    """Everything the rules decided about one cross (used by the events, the per-bar states and the exits)."""
    index: int
    time: int
    direction: str            # bull | bear
    next_index: int           # first bar of the next cross (any direction) or len(df)
    shoot: float
    confirm_index: int | None = None
    multi: bool = False       # flipped before confirmation
    timeout: bool = False     # no confirmation inside the window
    touched_before_confirm: bool = False
    entry_index: int | None = None
    entry_kind: str = ""      # pullback | no-pullback
    fails: dict = field(default_factory=dict)      # bar index -> reason (no entry at that bar)
    offered: float = 0.0      # best move from the confirm bar close until the leg ends
    tradeable: bool = True    # False = before trade_from (context only)


def trend_series(df: pd.DataFrame, slope_bars: int = 4, min_slope: float = 0.0) -> list[str]:
    """Per-bar DISPLAY label from EMA order + EMA50 slope: up | down | range. Not an entry rule."""
    e20 = df["ema20"].to_numpy(); e50 = df["ema50"].to_numpy()
    out = []
    for i in range(len(df)):
        if i < slope_bars or np.isnan(e50[i]) or np.isnan(e50[i - slope_bars]):
            out.append("range"); continue
        sl = e50[i] - e50[i - slope_bars]
        if e20[i] > e50[i] and sl > min_slope: out.append("up")
        elif e20[i] < e50[i] and sl < -min_slope: out.append("down")
        else: out.append("range")
    return out


def _sign(v):
    return 0 if np.isnan(v) else (1 if v > 0 else -1 if v < 0 else 0)


def ist_hour(ts_server: int, server_offset_h: float) -> float:
    return ((ts_server - server_offset_h * 3600 + 5.5 * 3600) % 86400) / 3600


def server_hour(ts_server: int) -> float:
    return (ts_server % 86400) / 3600


def entry_hour_block(ts_server: int, rules: Rules, server_offset_h: float) -> str | None:
    """None when the hour allows an entry, else the reason. Server 21:00–23:59; an IST window only if entry_hours_ist is set."""
    sh = server_hour(ts_server); lo_s, hi_s = rules.no_entry_server_hours
    if lo_s <= sh < hi_s:
        return f"no-entry hour (server {int(sh):02d}:{int((sh % 1) * 60):02d})"
    if rules.entry_hours_ist is None:
        return None
    ih = ist_hour(ts_server, server_offset_h); lo_i, hi_i = rules.entry_hours_ist
    if not (lo_i <= ih < hi_i):
        return f"no-entry hour (outside {lo_i:g}-{hi_i:g} IST)"
    return None


def news_block(ts_server: int, rules: Rules, server_offset_h: float) -> bool:
    t_utc = ts_server - server_offset_h * 3600
    return any(r - rules.news_entry_block_min * 60 <= t_utc < r + rules.news_rearm_min * 60 for r in rules.news_times)


def build(df: pd.DataFrame, rules: Rules, server_offset_h: float = 3.0, trade_from: int | None = None,
          day_end: int | None = None, m15=None, m1=None, guardian_profile: str = "classic",
          ride_floor: float = 4.0, ride_swing_bars: int = 6, ride_buffer: float = 1.0,
          ride_atr_mult: float = 3.0, giveback_pts: float = 3.0, giveback_frac: float = 0.5,
          soft_arm_pts: float = 5.0) -> dict:
    """`trade_from`: server-time epoch; crosses before it are context only (no entries, not counted).
    `day_end`: server-time epoch; the last bar before it closes any open trade with reason day_end (replay)."""
    gap = df["spread"].to_numpy()
    close = df["close"].to_numpy(); high = df["high"].to_numpy(); low = df["low"].to_numpy()
    e20 = df["ema20"].to_numpy(); e50 = df["ema50"].to_numpy(); time = df["time"].to_numpy()
    n = len(df)
    g = rules.guardian
    sign = np.array([_sign(x) for x in gap])
    avg_rng = pd.Series(high - low).rolling(20, min_periods=1).mean().to_numpy() if rules.confirm_gap_x_range else None
    cross_idx = [i for i in range(1, n) if sign[i] != 0 and sign[i - 1] != 0 and sign[i] != sign[i - 1]]
    cross_set = set(cross_idx)
    last_k = n - 1                                     # the day-end bar (forced close) when the data reaches the day end
    if day_end is not None:
        inside = np.nonzero(time < day_end)[0]
        last_k = int(inside[-1]) if len(inside) else n - 1
        at_day_end = (time[last_k] + _bar_s() >= day_end)
    else:
        at_day_end = False

    def touch(k: int, s: int) -> bool:
        return (low[k] <= e20[k] + rules.touch_tol) if s > 0 else (high[k] >= e20[k] - rules.touch_tol)

    # ---- 1) every cross: confirm / multi / timeout / shoot
    infos: list[CrossInfo] = []
    for ci, i in enumerate(cross_idx):
        nxt = cross_idx[ci + 1] if ci + 1 < len(cross_idx) else n
        d = "bull" if sign[i] > 0 else "bear"; s = 1 if sign[i] > 0 else -1
        lo_b = max(0, i - rules.shoot_bars)
        shoot = float((close[i] - low[lo_b:i].min()) if s > 0 else (high[lo_b:i].max() - close[i])) if i > lo_b else 0.0
        info = CrossInfo(i, int(time[i]), d, nxt, round(shoot, 2), tradeable=(trade_from is None or time[i] >= trade_from))
        for j in range(i + rules.confirm_bars - 1, min(i + rules.confirm_window, nxt - 1) + 1):
            if abs(gap[j]) >= max(rules.confirm_gap, rules.confirm_gap_x_range * avg_rng[j] if rules.confirm_gap_x_range else 0.0):
                info.confirm_index = j; break
        if info.confirm_index is None:
            if nxt < n and nxt <= i + rules.confirm_window:
                info.multi = True
            elif i + rules.confirm_window < n:
                info.timeout = True
        if info.confirm_index is not None:
            j = info.confirm_index
            end = min(nxt - 1, last_k if day_end is not None else n - 1)
            if end > j:
                seg_h = high[j + 1:end + 1]; seg_l = low[j + 1:end + 1]
                info.offered = round(float(max((seg_h - close[j]).max() if s > 0 else (close[j] - seg_l).max(), 0.0)), 2)
        infos.append(info)
    by_index = {x.index: x for x in infos}
    opp_confirm_of = {x.confirm_index: x for x in infos if x.confirm_index is not None}

    # ---- 2) entries and exits, cross by cross (one position at a time, one trade per cross)
    events: list[CrossEvent] = []
    journeys: list[Journey] = []
    busy_until = -1

    def info_fields(x: CrossInfo, k: int) -> dict:
        return {"cross_index": x.index, "cross_time": x.time, "confirm_index": x.confirm_index,
                "confirm_time": int(time[x.confirm_index]) if x.confirm_index is not None else None,
                "shoot": x.shoot, "bars_since_cross": k - x.index, "session": session_of(int(time[k]), server_offset_h),
                "ema20": float(e20[k]), "ema50": float(e50[k]), "gap": float(gap[k]), "multi": x.multi,
                "direction": x.direction}

    def allowed(k: int, s: int, x: CrossInfo, fallback: bool) -> str | None:
        """None = entry allowed at bar k, else the reason. max_chase is the fallback's condition only."""
        dist = abs(close[k] - e20[k])
        if fallback and dist > rules.max_chase:
            return f"too far from EMA20 ({dist:.1f} > {rules.max_chase:g})"
        if rules.hour_filter:
            why = entry_hour_block(int(time[k]), rules, server_offset_h)
            if why:
                return why
        if rules.news_filter and news_block(int(time[k]), rules, server_offset_h):
            return "news"
        if rules.min_shoot > 0 and x.shoot < rules.min_shoot:
            return f"shoot {x.shoot:.1f} < {rules.min_shoot:g}"
        if k < busy_until:
            return "in position"
        return None

    def run_exit(jn: Journey, j: int, s: int, x: CrossInfo, guardian_profile: str = "classic",
                 ride_floor: float = 4.0, swing_bars: int = 6, ride_buffer: float = 1.0,
                 atr_mult: float = 3.0, giveback_pts: float = 3.0, giveback_frac: float = 0.5,
                 _soft_arm_pts: float = 5.0):
        sl = -g.pre_stop; mfe = 0.0; mae = 0.0
        end_k = last_k if day_end is not None else n - 1
        phase = "A"  # leg profile phases
        floor_pts = ride_floor
        trail_pts = floor_pts
        arm_pts = max(g.early_at or 0, _soft_arm_pts) if guardian_profile == "leg" else (g.early_at or 0)
        for k in range(j + 1, end_k + 1):
            fav = (high[k] - jn.entry_price) if s > 0 else (jn.entry_price - low[k])
            adv = (low[k] - jn.entry_price) if s > 0 else (jn.entry_price - high[k])
            mfe = max(mfe, fav); mae = min(mae, adv)
            unreal = s * (close[k] - jn.entry_price)
            tk_utc = int(time[k]) - server_offset_h * 3600
            reason = None; px = float(close[k])
            # 1) the SL in force at this bar's open
            is_news = rules.news_filter and any(r - rules.news_flat_min * 60 <= tk_utc < r for r in rules.news_times) and \
                    (unreal >= rules.news_flat_min_profit or rules.news_flat_losers)
            if is_news:
                if guardian_profile == "leg" and phase == "B":
                    # Phase B + news: tighten, don't close
                    news_trail = max(trail_pts, fav - 3)
                    if news_trail > trail_pts:
                        trail_pts = news_trail; sl = trail_pts
                elif guardian_profile == "leg" and phase == "A":
                    # v2.7.1 Phase A + news: do NOT close; cap SL, convert soft lock to hard
                    if sl > -g.pre_stop:
                        pass  # soft lock already acting as SL — keep it
                    # don't set reason — no close
                else:
                    reason = "news_flat"
            elif adv <= sl:
                if guardian_profile == "leg" and phase == "B":
                    reason = "trail_hit"
                elif guardian_profile == "leg" and phase == "A" and sl > -g.pre_stop:
                    reason = "soft_trail"
                else:
                    reason = "stop" if sl <= -g.pre_stop else ("early" if sl < g.secure_level else "secured")
                px = jn.entry_price + s * sl
            if reason is None:
                if guardian_profile == "leg":
                    # --- LEG PROFILE ---
                    # Phase A → B transition: peak >= secure_at AND current bar >= floor + buffer
                    if phase == "A" and mfe >= g.secure_at and unreal >= floor_pts + ride_buffer:
                        phase = "B"
                        sl = floor_pts; trail_pts = floor_pts
                        jn.target_index = jn.target_index if jn.target_index is not None else k
                    if phase == "A":
                        # soft trailing lock: arm at arm_pts, trail with peak
                        if g.early_at is not None and mfe >= arm_pts:
                            giveback = max(giveback_pts, giveback_frac * mfe)
                            new_lock = max(g.early_level, mfe - giveback)
                            if new_lock > sl:
                                sl = new_lock; jn.lock_index = jn.lock_index or k
                    elif phase == "B":
                        # trail on closed bars: swing trail + ATR trail
                        lo_b = max(j + 1, k - swing_bars + 1)
                        if s > 0:
                            swing_px = float(low[lo_b:k + 1].min()) - ride_buffer
                            swing_trail = swing_px - jn.entry_price
                        else:
                            swing_px = float(high[lo_b:k + 1].max()) + ride_buffer
                            swing_trail = jn.entry_price - swing_px
                        # ATR trail — v2.7.1: peak_px correct for both directions
                        atr_lo = max(0, k - 19)
                        avg_rng = float(np.mean(high[atr_lo:k + 1] - low[atr_lo:k + 1]))
                        peak_px = jn.entry_price + s * mfe
                        if s > 0:
                            atr_trail = (peak_px - atr_mult * avg_rng) - jn.entry_price
                        else:
                            atr_trail = jn.entry_price - (peak_px + atr_mult * avg_rng)
                        new_trail = max(floor_pts, swing_trail, atr_trail)
                        trail_pts = max(trail_pts, new_trail)
                        sl = trail_pts
                        # bar-close exits in Phase B
                        if (close[k] < e50[k]) if s > 0 else (close[k] > e50[k]):
                            reason = "ema_slow_close"
                        elif (s > 0 and e20[k] < e50[k]) or (s < 0 and e20[k] > e50[k]):
                            reason = "leg_end"
                        elif (close[k] < e20[k]) if s > 0 else (close[k] > e20[k]):
                            if s > 0:
                                tighten_pts = float(low[k]) - ride_buffer - jn.entry_price
                            else:
                                tighten_pts = jn.entry_price - (float(high[k]) + ride_buffer)
                            if tighten_pts > trail_pts:
                                trail_pts = tighten_pts; sl = trail_pts
                        if reason is None and k == end_k and day_end is not None and at_day_end and rules.day_end_close:
                            reason = "day_end"
                else:
                    # --- CLASSIC PROFILE ---
                    # 2) locks from this bar's favourable extreme: early lock, secure, ride steps (2 pts air)
                    if g.early_at is not None and mfe >= g.early_at and sl < g.early_level:
                        sl = g.early_level; jn.lock_index = k
                    if mfe >= g.secure_at and sl < g.secure_level:
                        sl = g.secure_level; jn.target_index = jn.target_index if jn.target_index is not None else k
                    while sl >= g.secure_level and mfe >= sl + g.ride_step + g.ride_step * 0.4:
                        sl += g.ride_step
                    # 3) bar-close exits
                    if sl >= g.secure_level and ((close[k] < e20[k]) if s > 0 else (close[k] > e20[k])):
                        reason = "ema20_turn"
                    elif k in opp_confirm_of and opp_confirm_of[k].direction != x.direction:
                        reason = "opposite_cross"
                    elif k == end_k and day_end is not None and at_day_end and rules.day_end_close:
                        reason = "day_end"
            if reason:
                jn.exit_index, jn.exit_time, jn.exit_price, jn.exit_reason = k, int(time[k]), float(px), reason
                break
        if jn.exit_index is None:
            k = end_k
            jn.exit_index, jn.exit_time, jn.exit_price, jn.exit_reason = k, int(time[k]), float(close[k]), "open"
        jn.sl_final = sl
        jn.result = round(s * (jn.exit_price - jn.entry_price), 2)
        jn.mfe = round(mfe, 2); jn.mae = round(mae, 2); jn.bars = jn.exit_index - j

    for x in infos:
        i, d = x.index, x.direction; s = 1 if d == "bull" else -1
        if not x.tradeable:
            continue
        events.append(CrossEvent(i, int(time[i]), d, "CROSS", "unconfirmed", info_fields(x, i)))
        if x.multi:
            events.append(CrossEvent(x.next_index, int(time[x.next_index]), d, "MULTI",
                                     f"flipped after {x.next_index - i} bars — ignored", info_fields(x, x.next_index)))
            continue
        if x.timeout:
            k = i + rules.confirm_window
            events.append(CrossEvent(k, int(time[k]), d, "WP", f"no confirmation in {rules.confirm_window} bars", info_fields(x, k)))
            continue
        if x.confirm_index is None:
            continue                                        # still waiting at the end of the data
        j = x.confirm_index
        events.append(CrossEvent(j, int(time[j]), d, "CONFIRMED", f"{j - i + 1} bars · gap {abs(gap[j]):.2f}", info_fields(x, j)))
        x.touched_before_confirm = any(touch(k, s) for k in range(i + 1, j + 1))      # from the bar AFTER the cross
        first = j if x.touched_before_confirm else j + 1
        last_b = min(j + rules.pullback_window, x.next_index - 1)
        for k in range(first, last_b + 1):
            is_touch = (k == j and x.touched_before_confirm) or (k > j and touch(k, s))
            is_fallback = (k == j + rules.pullback_window) and not is_touch
            if not (is_touch or is_fallback):
                continue
            why = allowed(k, s, x, fallback=is_fallback)
            if why is not None:
                x.fails[k] = why
                events.append(CrossEvent(k, int(time[k]), d, "F", why, info_fields(x, k)))
                continue
            x.entry_index, x.entry_kind = k, ("pullback" if is_touch else "no-pullback")
            break
        if x.entry_index is None:
            continue
        k = x.entry_index
        jn = Journey("long" if s > 0 else "short", i, k, int(time[k]), float(close[k]), session=session_of(int(time[k]), server_offset_h),
                     pullback=(x.entry_kind == "pullback"), confirm_index=j, shoot=x.shoot, notes=x.entry_kind)
        events.append(CrossEvent(k, int(time[k]), d, "EB" if s > 0 else "ES", x.entry_kind, info_fields(x, k)))
        run_exit(jn, k, s, x, guardian_profile=guardian_profile, ride_floor=ride_floor,
                 swing_bars=ride_swing_bars, ride_buffer=ride_buffer, atr_mult=ride_atr_mult,
                 giveback_pts=giveback_pts, giveback_frac=giveback_frac, _soft_arm_pts=soft_arm_pts)
        jn.exit_rule = jn.exit_reason or ""
        jn.guardian_profile = guardian_profile
        busy_until = jn.exit_index
        journeys.append(jn)

    # ---- 3) per-bar states (detect.py, the alert suggestion and the live cards read these — ONE source of truth)
    exits_of = {jn.cross_index: jn for jn in journeys}
    states = []
    latest: CrossInfo | None = None
    for k in range(n):
        if k in by_index:
            latest = by_index[k]
        st = {"index": k, "time": int(time[k]), "close": float(close[k]), "ema20": float(e20[k]), "ema50": float(e50[k]),
              "gap": float(gap[k]), "cross": ("bull" if sign[k] > 0 else "bear") if k in cross_set else None,
              "direction": latest.direction if latest else None, "bars_since_cross": (k - latest.index) if latest else None,
              "confirm": "no cross", "confirmed": False, "touch": False, "entry": None, "allowed": False,
              "verdict": "WAIT", "reason": "no cross yet", "shoot": latest.shoot if latest else 0.0,
              "session": session_of(int(time[k]), server_offset_h), "multi": False}
        if latest is None or np.isnan(gap[k]):
            states.append(st); continue
        x = latest; s = 1 if x.direction == "bull" else -1; held = k - x.index + 1
        side = "LONG" if s > 0 else "SHORT"
        st["touch"] = bool(touch(k, s))
        if x.confirm_index is None or k < x.confirm_index:
            if held > rules.confirm_window:
                st["confirm"] = f"not confirmed in {rules.confirm_window} bars"; st["verdict"] = "NO TRADE"; st["reason"] = st["confirm"]
            else:
                st["confirm"] = f"bar {held} of {rules.confirm_bars}" + (f" · gap {abs(gap[k]):.2f} < {rules.confirm_gap:g}" if held >= rules.confirm_bars else "")
                st["verdict"] = "WAIT"; st["reason"] = f"cross not confirmed ({st['confirm']})"
            states.append(st); continue
        j = x.confirm_index
        st["confirmed"] = True
        st["confirm"] = "confirmed" if k == j else f"confirmed {k - j} bars ago"
        jn = exits_of.get(x.index)
        if x.entry_index is not None and k == x.entry_index:
            st["entry"] = side; st["allowed"] = True; st["verdict"] = side
            st["reason"] = (f"confirmed {x.direction} cross, price back at EMA20 (pullback)" if x.entry_kind == "pullback"
                            else f"confirmed {x.direction} cross, no pullback — fallback within {rules.max_chase:g} of EMA20")
        elif x.entry_index is not None and k > x.entry_index:
            if jn is not None and (jn.exit_reason == "open" or k <= jn.exit_index):
                st["verdict"] = "WAIT"; st["reason"] = f"in trade since bar {x.entry_index}"
            else:
                st["verdict"] = "NO TRADE"; st["reason"] = "one trade per cross (closed)"
        elif k in x.fails:
            st["verdict"] = "NO TRADE"; st["reason"] = x.fails[k]
        elif not x.tradeable and k >= j:
            st["verdict"] = "WAIT"; st["reason"] = "cross before the trading window (context only)"
        elif k <= j + rules.pullback_window:
            st["verdict"] = "WAIT"; st["reason"] = f"waiting for the pullback to EMA20 ({k - j} of {rules.pullback_window})"
        else:
            st["verdict"] = "NO TRADE"; st["reason"] = "pullback window passed"
        states.append(st)

    # ---- 4) summary (keys the renderers expect, plus the research block inputs)
    closed = [x for x in journeys if x.exit_reason != "open"]
    wins = [x for x in closed if x.result > 0]
    losers = [x for x in closed if x.result <= 0]
    traded = [x for x in infos if x.tradeable]
    confirmed = [x for x in traded if x.confirm_index is not None]
    fails = [e for e in events if e.label == "F"]
    summary = {
        "trades": len(closed), "wins": len(wins), "losers": len(losers),
        "win_rate": round(100 * len(wins) / len(closed), 1) if closed else 0.0,
        "net": round(sum(x.result for x in closed), 2),
        "avg_win": round(sum(x.result for x in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(x.result for x in losers) / len(losers), 2) if losers else 0.0,
        "worst_drawdown": round(float(min((x.mae for x in closed), default=0.0)), 2),
        "stops": sum(x.exit_reason == "stop" for x in closed),
        "early": sum(x.exit_reason == "early" for x in closed),
        "secured_exits": sum(x.exit_reason == "secured" for x in closed),
        "ema20_turns": sum(x.exit_reason == "ema20_turn" for x in closed),
        "opposite_cross_exits": sum(x.exit_reason == "opposite_cross" for x in closed),
        "day_end_exits": sum(x.exit_reason == "day_end" for x in closed),
        "news_flats": sum(x.exit_reason == "news_flat" for x in closed),
        "crosses": len(traded), "confirmed": len(confirmed),
        "whipsaws": sum(x.multi for x in traded), "multi": sum(x.multi for x in traded),
        "unconfirmed": sum(x.timeout for x in traded),
        "filtered": len(fails),
        "filtered_reasons": {r: sum(1 for e in fails if e.reason == r) for r in sorted({e.reason for e in fails})},
        "waited_for_pullback": sum(1 for x in confirmed if x.entry_index is not None and not x.touched_before_confirm),
        "pullback_entries": sum(x.pullback for x in closed),
        "no_pullback_entries": sum((not x.pullback) for x in closed),
        "hit_target": sum(x.target_index is not None for x in closed),
        "best_run": round(max((x.result for x in closed), default=0.0), 2),
        "offered": round(sum(x.offered for x in confirmed), 2),
        "available": round(sum(x.mfe for x in closed), 2),
        "captured_pct": (round(100 * sum(x.result for x in closed) / sum(x.mfe for x in closed), 1)
                         if sum(x.mfe for x in closed) > 0 else 0.0),
        # legacy keys (renderers) — the 20/50 system has none of these
        "late_entries": 0, "reentries": 0, "pre_entries": 0, "reentry_net": 0.0, "adds": 0, "adds_net": 0.0, "leg_mode": False,
        "spike_locks": 0, "by_grade": {gr: {"trades": 0, "net": 0.0} for gr in "ABC"}, "runner_whatif": [], "news_whatif": [],
        "by_session": {},
    }
    for sname, *_ in SESSIONS:
        xs = [x for x in closed if x.session == sname]
        summary["by_session"][sname] = {"trades": len(xs), "net": round(sum(x.result for x in xs), 2)}
    # ---- v2.5.2 PRE-CROSS detection (notification only, never creates a trade)
    pre_events: list[CrossEvent] = []
    if rules.pre_alert:
        avg_rng = pd.Series(high - low).rolling(20, min_periods=1).mean().to_numpy()
        pre_armed = True; pre_last = -rules.pre_cooldown_bars
        for i in range(max(rules.pre_through_bars, 2), n):
            if i in cross_set:
                pre_armed = True; pre_last = i; continue  # re-arm after a cross
            if i - pre_last < rules.pre_cooldown_bars:
                continue  # cooldown
            if not pre_armed or sign[i] == 0:
                continue
            # The coming cross direction is AGAINST the current EMA order
            d_new = "bear" if sign[i] > 0 else "bull"
            s_new = -1 if d_new == "bear" else 1
            # Price must close through EMA20 for pre_through_bars consecutive bars
            through = True
            for k in range(rules.pre_through_bars):
                idx = i - k
                if s_new < 0:
                    through = through and close[idx] < e20[idx]
                else:
                    through = through and close[idx] > e20[idx]
            if not through:
                continue
            # Gap must be shrinking
            if not (abs(gap[i]) < abs(gap[i - 1])):
                continue
            # Gap must be <= pre_gap_atr × avg range
            ar = avg_rng[i] if not np.isnan(avg_rng[i]) else 0.0
            if ar <= 0 or abs(gap[i]) > rules.pre_gap_atr * ar:
                continue
            # Lines must have been separated within pre_sep_bars
            lo_b = max(0, i - rules.pre_sep_bars)
            if np.isnan(avg_rng[i]) or np.nanmax(np.abs(gap[lo_b:i + 1])) < rules.pre_sep_atr * avg_rng[i]:
                continue
            pre_events.append(CrossEvent(i, int(time[i]), d_new, "PRE",
                                          f"EMA 20 closing on EMA 50 — {d_new} cross may form",
                                          info={"gap": round(float(gap[i]), 2), "gap_atr": round(abs(gap[i]) / ar, 2) if ar else 0}))
            pre_armed = False; pre_last = i

    return {"journeys": journeys, "events": events + pre_events, "pre": pre_events, "summary": summary, "bar_states": states,
            "cross_infos": infos, "day_end_bar": last_k if day_end is not None else None}


def state_at_bar(res: dict, i: int) -> dict | None:
    """The per-bar state build() computed for bar i (None when unavailable)."""
    states = res.get("bar_states") or []
    return states[i] if 0 <= i < len(states) else None
