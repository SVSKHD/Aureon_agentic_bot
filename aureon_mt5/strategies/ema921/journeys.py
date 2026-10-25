"""EMA 9/21 — v2.18.0: six symbols on M15, the PULLBACK-to-EMA9 entry (v2.16.0: gold M30, every cross at the close).

Research (10 months M15, 1 lot, bar by bar, spread deducted): the raw cross at the close had ~0 edge on every symbol; the
pullback to EMA 9 after the cross was positive on all six — XAUUSD, XAGUSD, EURUSD, GBPUSD, AUDUSD, USDJPY.

Rules per symbol (SYMBOLS, rules_for): every distance in broker POINTS (sl_pts, tp_pts, lock_at_pts, lock_to_pts,
limit_offset_pts, cost_pts); to_price(pts, point) is the only place points become price. Env overrides per symbol:
AUREON_921_TP_<SYM> / _SL_ / _LOCK_AT_ / _LOCK_TO_ / _COST_ (points), AUREON_921_HOURS_<SYM>="9-15" (server hours).
A symbol without a row raises UnknownSymbol (guardian_for / rules_for): it never runs unmanaged.

Vocabulary (events):
  PRE         EMA 9 converging on EMA 21 — informational card, no action
  ARM         (ema9touch) the cross: the side is armed, waiting for the pullback — card "CROSS — waiting for pullback"
  CANCEL      (ema9touch) an opposite cross while armed — card "cross cancelled" (and the new side arms)
  EXPIRE      (ema9touch) no EMA 9 touch within arm_ttl_bars (16 = 4 h on M15) — journal only
  EB / ES     the entry: ema9touch = the first LATER bar touching EMA 9 (touches_ema9 — the one test, replay = live), at the
              EMA 9 level · market = the cross bar's close · limit = a limit back from that close (v2.17.0)
  F           a cross that was not taken (in position / day / hour / Friday / news)

Exits (one object for replay and live): broker SL −sl_pts · broker TP +tp_pts · once +lock_at_pts has printed (checked at the
bar close) → SL to entry + lock_to_pts (a bar that closes below that level is closed at the close) · exit_mode "bracket"
(default) has no close-through-EMA exit; "e9" / "e9>e200" are the v2.16 exits · Friday flat at friday_flat_hour server.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field

import numpy as np
import pandas as pd

from ...common.news import session_of
from .. import Guardian

# ----------------------------------------------------------------------------- v2.18.0 per-symbol rules, in broker POINTS
# M15 pullback-to-EMA9 study (10 months, 1 lot, spread deducted). point = the reference point the *_pts values are written
# in; contract = units per lot (for the account-currency line on the cards); hours = server hours entries are allowed (None = all).
SYMBOLS = {
    "XAUUSD": dict(point=0.01, tp_pts=1000, sl_pts=1000, lock_at_pts=300, lock_to_pts=150, hours=None, contract=100.0, cost_pts=20),
    "XAGUSD": dict(point=0.001, tp_pts=200, sl_pts=100, lock_at_pts=60, lock_to_pts=30, hours=(9, 15), contract=5000.0, cost_pts=25),
    "EURUSD": dict(point=0.00001, tp_pts=100, sl_pts=200, lock_at_pts=50, lock_to_pts=20, hours=(9, 22), contract=100000.0, cost_pts=10),
    "GBPUSD": dict(point=0.00001, tp_pts=100, sl_pts=200, lock_at_pts=50, lock_to_pts=20, hours=(9, 22), contract=100000.0, cost_pts=15),
    "AUDUSD": dict(point=0.00001, tp_pts=100, sl_pts=200, lock_at_pts=50, lock_to_pts=20, hours=(9, 15), contract=100000.0, cost_pts=12),
    "USDJPY": dict(point=0.001, tp_pts=350, sl_pts=350, lock_at_pts=150, lock_to_pts=60, hours=None, contract=100000.0, cost_pts=15),
}
PREFIXES = {"XAU": "XAUUSD", "XAG": "XAGUSD", "EUR": "EURUSD", "GBP": "GBPUSD", "AUD": "AUDUSD", "USDJPY": "USDJPY"}
_POINT: dict = {}                                   # {symbol: broker point} — set by the agent at start (broker.symbol_info)


class UnknownSymbol(ValueError):
    pass


def to_price(pts, point: float) -> float:
    """The ONLY place a points distance becomes a price distance."""
    return round(float(pts) * float(point), 10)


def table_key(symbol: str) -> str:
    """XAUUSD / XAUUSD.m / EURUSDx → its table row. Raises UnknownSymbol for a symbol without rules (EURJPY, XPTUSD …)."""
    u = symbol.upper()
    for name in SYMBOLS:
        if u.startswith(name):
            return name
    raise UnknownSymbol(f"ema921 has no rules for {symbol} — supported: {', '.join(SYMBOLS)}")


def set_point(symbol: str, point: float | None):
    """The broker's point for the symbol (agent start). None / 0 → the table's reference point."""
    if point:
        _POINT[symbol.upper()] = float(point)
    else:
        _POINT.pop(symbol.upper(), None)


def _guardian(row: dict, name: str) -> Guardian:
    pt = row["point"]
    return Guardian(secure_at=to_price(row["lock_at_pts"], pt), secure_level=to_price(row["lock_to_pts"], pt),
                    pre_stop=to_price(row["sl_pts"], pt), ride_step=0.0, ema_slow_sl_buffer=0.0, enabled=True,
                    early_at=None, early_level=0.0, p_phase=False, slow_ema_sl=False, exit_on_confirmed_cross=False,
                    note=f"{name} (EMA 9/21 pullback): SL −{row['sl_pts']} pts · TP +{row['tp_pts']} pts · "
                         f"+{row['lock_at_pts']} → SL +{row['lock_to_pts']} pts")


GUARDIANS = {k: _guardian(SYMBOLS[v], v) for k, v in PREFIXES.items()}


def guardian_for(symbol: str) -> Guardian:
    """Raises UnknownSymbol: a symbol without rules can never run unmanaged."""
    name = table_key(symbol)
    return next(g for k, g in GUARDIANS.items() if PREFIXES[k] == name)


@dataclass
class Rules:
    # ---- instrument (v2.18.0): every distance below is in points of `point`; to_price() converts at the edge
    point: float = 0.01
    symbol: str = "XAUUSD"
    contract: float = 100.0
    # ---- entry
    days: tuple = (0, 1, 2, 3, 4)          # weekday numbers allowed for NEW entries (0 = Monday)
    friday_no_entry_from: float = 16.0     # server hour
    no_entry_server_hours: tuple = ()      # ((lo, hi), …) server hours with no new entries — the complement of the session
    # entry style (AUREON_921_ENTRY): ema9touch (v2.18.0 default) = after a cross, the first later bar touching EMA 9 within
    # arm_ttl_bars · market = the cross bar's close · limit = a limit order limit_offset_pts back from that close
    entry: str = "ema9touch"
    arm_ttl_bars: int = 16                 # 16 × M15 = 4 h
    limit_offset_pts: int = 150
    limit_ttl_bars: int = 2
    # ---- pre-cross card
    pre_gap_atr: float = 0.35              # |EMA9 − EMA21| below this × ATR(14), narrowing 2 bars → PRE card
    pre_cooldown_bars: int = 6
    # ---- exits (points)
    sl_pts: int = 1000                     # broker SL
    tp_pts: int = 1000                     # broker TP (0 = none)
    lock_at_pts: int = 300                 # once this much has printed (checked at the bar close)...
    lock_to_pts: int = 150                 # ...the broker SL goes to entry + this
    cost_pts: int = 20                     # spread deducted from every replay trade ($0.20 on gold)
    exit_mode: str = "bracket"             # bracket (SL / TP / lock only) · e9 · e9>e200 (v2.16 closes through the EMA)
    n_close: int = 1                       # closes through the exit line needed (e9 modes)
    friday_flat: bool = True
    friday_flat_hour: float = 22.0         # server hour: the first Friday bar opening at or after it is the flat bar (replay = live)
    # ---- news (common/news.py)
    news_times: tuple = ()
    news_entry_block_min: int = 60
    news_rearm_min: int = 30
    news_flat_min: int = 15
    guardian: Guardian = field(default_factory=lambda: GUARDIANS["XAU"])

    # price distances — derived, never stored
    @property
    def stop(self) -> float:
        return to_price(self.sl_pts, self.point)

    @property
    def tp(self) -> float:
        return to_price(self.tp_pts, self.point)

    @property
    def lock_at(self) -> float:
        return to_price(self.lock_at_pts, self.point)

    @property
    def lock_to(self) -> float:
        return to_price(self.lock_to_pts, self.point)

    @property
    def limit_offset(self) -> float:
        return to_price(self.limit_offset_pts, self.point)

    @property
    def digits(self) -> int:
        return max(0, len(f"{self.point:.10f}".rstrip("0").split(".")[1]))

    def pts(self, price_distance: float) -> float:
        return float(price_distance) / float(self.point)


PRICE_KEYS = {"stop": "sl_pts", "tp": "tp_pts", "lock_at": "lock_at_pts", "lock_to": "lock_to_pts", "limit_offset": "limit_offset_pts"}


def with_overrides(rules: Rules, ov: dict | None) -> Rules:
    """dataclasses.replace that also takes the v2.16 price keys (stop=30 → sl_pts = 30 / point)."""
    import dataclasses
    out = {}
    for k, v in (ov or {}).items():
        if k in PRICE_KEYS:
            out[PRICE_KEYS[k]] = int(round(float(v) / rules.point))
        else:
            out[k] = v
    return dataclasses.replace(rules, **out)


def hours_complement(hours) -> tuple:
    """(9, 15) → ((0, 9), (15, 24)): the no-entry windows around the allowed one. None → ()."""
    if not hours:
        return ()
    lo, hi = float(hours[0]), float(hours[1])
    if lo <= 0 and hi >= 24:
        return ()
    if lo < hi:
        return tuple(x for x in ((0.0, lo), (hi, 24.0)) if x[1] > x[0])
    return ((hi, lo),)                                       # an overnight window, e.g. 22–6


def parse_hours(raw: str | None):
    s = str(raw or "").strip().lower()
    if not s or s == "all":
        return None
    a, b = s.replace("–", "-").split("-", 1)
    return (float(a), float(b))


def rules_for(symbol: str, environ=None) -> Rules:
    """The symbol's rules: the table row, AUREON_921_* overrides (points / server hours), the broker's point. A broker point
    that differs from the table's reference point rescales the points so the price distances stay as researched.
    Raises UnknownSymbol for a symbol without a table row."""
    import os
    env = os.environ if environ is None else environ
    name = table_key(symbol); row = dict(SYMBOLS[name]); u = symbol.upper().replace(".", "_")
    ref = row["point"]; point = _POINT.get(symbol.upper(), ref)
    scale = ref / point if point else 1.0
    def num(key, default):
        raw = env.get(f"AUREON_921_{key}_{u}") or env.get(f"AUREON_921_{key}_{name}")
        return int(float(raw)) if raw not in (None, "") else int(round(default * scale))
    hours = row["hours"]
    raw_h = env.get(f"AUREON_921_HOURS_{u}") or env.get(f"AUREON_921_HOURS_{name}")
    if raw_h not in (None, ""):
        hours = parse_hours(raw_h)
    entry = str(env.get("AUREON_921_ENTRY", "ema9touch") or "ema9touch").strip().lower()
    exit_mode = str(env.get("AUREON_921_EXIT", "bracket") or "bracket").strip().lower()
    g = guardian_for(symbol)
    raw_off = env.get("AUREON_921_LIMIT_OFFSET")                # v2.17.0: a price distance ($1.5 on gold)
    limit_pts = int(round(float(raw_off) / point)) if raw_off not in (None, "") else int(round(row["sl_pts"] * 0.15 * scale))
    return Rules(point=point, symbol=name, contract=row["contract"], no_entry_server_hours=hours_complement(hours),
                 entry=entry if entry in ("ema9touch", "market", "limit") else "ema9touch",
                 exit_mode=exit_mode if exit_mode in ("bracket", "e9", "e9>e200") else "bracket",
                 arm_ttl_bars=int(env.get("AUREON_921_ARM_TTL_BARS") or 16),
                 limit_offset_pts=limit_pts,
                 limit_ttl_bars=int(env.get("AUREON_921_LIMIT_TTL_BARS") or 2),
                 sl_pts=num("SL", row["sl_pts"]), tp_pts=num("TP", row["tp_pts"]), lock_at_pts=num("LOCK_AT", row["lock_at_pts"]),
                 lock_to_pts=num("LOCK_TO", row["lock_to_pts"]), cost_pts=num("COST", row["cost_pts"]), guardian=g)


GOLD = Rules()


def banner_lines(symbols, environ=None) -> list[str]:
    """One line per symbol for the startup banner: the resolved rules (after env overrides and the broker point)."""
    out = []
    for sym in symbols:
        try:
            r = rules_for(sym, environ)
        except UnknownSymbol as e:
            out.append(f"⛔ {sym}: {e}"); continue
        hrs = "all" if not r.no_entry_server_hours else "–".join(
            f"{int(x)}" for x in _allowed(r.no_entry_server_hours))
        out.append(f"{sym}: {r.entry} · TP +{r.tp_pts} · SL −{r.sl_pts} · +{r.lock_at_pts}→+{r.lock_to_pts} pts "
                   f"(point {r.point:g}) · hours {hrs} server · exit {r.exit_mode}")
    return out


def _allowed(no_entry: tuple) -> tuple:
    ws = sorted(no_entry)
    if len(ws) == 2 and ws[0][0] == 0 and ws[1][1] == 24:
        return (ws[0][1], ws[1][0])
    if len(ws) == 1:
        lo, hi = ws[0]
        return (hi, lo) if lo > 0 and hi < 24 else ((hi, 24) if lo == 0 else (0, lo))
    return (0, 24)


def touches_ema9(direction: int, high: float, low: float, ema9: float) -> bool:
    """v2.18.0 — THE pullback test, one function for the replay and live: a long bar's low reaches EMA 9, a short bar's high."""
    return (low <= ema9) if direction > 0 else (high >= ema9)


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


def friday_last_bar(ts: int, rules: Rules) -> bool:
    """The Friday flat bar: a Friday bar opening at or after rules.friday_flat_hour (server). The first such bar closes the
    trade; any later one closes a trade that is somehow still open (the next bar close)."""
    return bool(rules.friday_flat and server_weekday(int(ts)) == 4 and server_hour(int(ts)) >= rules.friday_flat_hour)


def on_bar_close(rules: Rules, *, peak: float, locked: bool, close_pts: float, against: bool, run: int,
                 friday_last: bool) -> tuple[bool, str | None, int]:
    """v2.16.0 — the bar-close decision, shared by the live guardian and the paper tracker (same order as build()'s
    run_exit): (lock now?, close reason | None, the new against-run).
        lock_close   +lock_at has printed but this bar CLOSED below +lock_to → out at the close (the lock never arms intrabar)
        ema_close    n_close closes through the exit line (EMA 9, or EMA 200 after the lock in e9>e200)
        friday_flat  the Friday flat bar"""
    lock = False
    against = bool(against) and rules.exit_mode != "bracket"          # v2.18.0: bracket = SL / TP / lock only, no close-through exit
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
    busy_until = -1; last_pre = -999; unfilled = 0
    last_k = n - 1
    if day_end is not None:
        inside = np.nonzero(time < day_end)[0]; last_k = int(inside[-1]) if len(inside) else n - 1

    def info_at(k, d):
        last3 = [f"{'G' if close[j] >= opn[j] else 'R'}{close[j] - opn[j]:+.1f}" for j in range(max(0, k - 2), k + 1)]
        return {"direction": "bull" if d > 0 else "bear", "ema9": float(e9[k]), "ema21": float(e21[k]), "ema200": float(e200[k]),
                "gap": float(e9[k] - e21[k]), "dist9": float(close[k] - e9[k]), "dist200": float(close[k] - e200[k]), "last3": last3,
                "session": session_of(int(time[k]), server_offset_h), "atr": float(atr[k]) if not np.isnan(atr[k]) else None}

    def run_exit(jn: Journey, k0: int, s: int, fill_bar: bool = False) -> int:
        """fill_bar: k0 is the bar a limit filled on — its stop and its close count, its TP / peak only from the close (the
        bar's favourable extreme may have printed before the fill)."""
        e = jn.entry_price; sl = -rules.stop; peak = 0.0; mae = 0.0; run = 0; end = last_k
        for k in range(k0 if fill_bar else k0 + 1, end + 1):
            hi = (high[k] - e) if s > 0 else (e - low[k]); lo = (low[k] - e) if s > 0 else (e - high[k])
            if fill_bar and k == k0:
                hi = (close[k] - e) * s
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
            against = ((close[k] < line[k]) if s > 0 else (close[k] > line[k])) and rules.exit_mode != "bracket"
            run = run + 1 if against else 0
            if run >= rules.n_close:
                jn.exit_index, jn.exit_time, jn.exit_price = k, int(time[k]), close[k]
                jn.exit_reason = "ema_close"; jn.exit_rule = exit_line(rules, peak); jn.result = (close[k] - e) * s; break
            if friday_last_bar(int(time[k]), rules):                  # v2.16.0: the live rule (was: the last Friday bar in the data)
                jn.exit_index, jn.exit_time, jn.exit_price = k, int(time[k]), close[k]
                jn.exit_reason = "friday_flat"; jn.exit_rule = "friday"; jn.result = (close[k] - e) * s; break
        else:
            k = end; jn.exit_reason = "open"; jn.result = (close[k] - e) * s
        nd = rules.digits                                              # v2.18.0: FX distances are 0.00x — never round to cents
        jn.mfe = round(peak, nd); jn.mae = round(mae, nd); jn.bars = (jn.exit_index or end) - k0; jn.sl_final = sl
        jn.result = round(jn.result - to_price(rules.cost_pts, rules.point), nd)
        return jn.exit_index if jn.exit_index is not None else end

    def touch_step(k, armed):
        """IDLE → ARMED(dir, cross bar) → entry on the first LATER bar touching EMA 9 → IDLE. Opposite cross: cancel (and arm
        the new side). Not touched within arm_ttl_bars: expire. Entry gates (day / hours / Friday / news) apply at the touch;
        a blocked touch keeps waiting. Events: ARM · CANCEL · EXPIRE · EB / ES (the entry) · F (a cross ignored in position)."""
        nonlocal busy_until
        cross = sg[k] != sg[k - 1] and sg[k] != 0
        if armed is not None:
            d0, k0 = armed
            if cross and int(sg[k]) != d0:
                events.append(CrossEvent(k, int(time[k]), "bull" if d0 > 0 else "bear", "CANCEL", "opposite cross", info_at(k, d0)))
                armed = None
            elif k - k0 > int(rules.arm_ttl_bars):
                events.append(CrossEvent(k, int(time[k]), "bull" if d0 > 0 else "bear", "EXPIRE",
                                         f"no EMA 9 touch in {rules.arm_ttl_bars} bars", info_at(k, d0)))
                armed = None
            elif k > k0 and touches_ema9(d0, high[k], low[k], e9[k]):
                why = "in position" if k <= busy_until else (entry_block(int(time[k]), rules)
                                                             or ("news" if news_block(int(time[k]), rules, server_offset_h) else None))
                if why is None:
                    info = dict(info_at(k, d0), level=float(e9[k]), cross_index=k0, bars_after_cross=k - k0)
                    events.append(CrossEvent(k, int(time[k]), info["direction"], "EB" if d0 > 0 else "ES", "ema9 touch", info))
                    if trade_from is None or time[k] >= trade_from:
                        jn = Journey("long" if d0 > 0 else "short", k0, k, int(time[k]), float(e9[k]), session=info["session"],
                                     pullback=True, notes=f"EMA 9 touch {k - k0} bar(s) after the cross")
                        busy_until = run_exit(jn, k, d0, fill_bar=True); journeys.append(jn)
                    return None
        if cross and armed is None:
            d = int(sg[k]); info = info_at(k, d)
            if k <= busy_until:
                events.append(CrossEvent(k, int(time[k]), info["direction"], "F", "in position", info))
            else:
                events.append(CrossEvent(k, int(time[k]), info["direction"], "ARM", "cross — waiting for pullback", info))
                return (d, k)
        return armed

    armed = None
    for k in range(201, n):
        if np.isnan(e200[k]) or np.isnan(atr[k]):
            continue
        # pre-cross (informational)
        gap = abs(e9[k] - e21[k]); gap1 = abs(e9[k - 1] - e21[k - 1]); gap2 = abs(e9[k - 2] - e21[k - 2])
        if gap < rules.pre_gap_atr * atr[k] and gap < gap1 < gap2 and k - last_pre > rules.pre_cooldown_bars and sg[k] == sg[k - 1]:
            d = -int(sg[k]) if sg[k] != 0 else 0
            if d:
                events.append(CrossEvent(k, int(time[k]), "bull" if d > 0 else "bear", "PRE", "converging", info_at(k, d))); last_pre = k
        if rules.entry == "ema9touch":                                 # v2.18.0: the pullback (one state machine, replay = live)
            armed = touch_step(k, armed); continue
        # the cross
        if sg[k] != sg[k - 1] and sg[k] != 0:
            d = int(sg[k]); info = info_at(k, d); tradeable = trade_from is None or time[k] >= trade_from
            why = "in position" if k <= busy_until else (entry_block(int(time[k]), rules) or ("news" if news_block(int(time[k]), rules, server_offset_h) else None))
            if why:
                events.append(CrossEvent(k, int(time[k]), info["direction"], "F", why, info)); continue
            events.append(CrossEvent(k, int(time[k]), info["direction"], "EB" if d > 0 else "ES", "cross", info))
            if not tradeable:
                continue
            if rules.entry == "limit":
                lvl = float(close[k]) - d * float(rules.limit_offset); kf = None
                for j in range(k + 1, min(k + int(rules.limit_ttl_bars), last_k) + 1):
                    if (d > 0 and low[j] <= lvl) or (d < 0 and high[j] >= lvl):
                        kf = j; break
                    if sg[j] != d:                                    # an opposite cross cancels the pending order
                        break
                if kf is None:
                    unfilled += 1; busy_until = k; continue
                jn = Journey("long" if d > 0 else "short", k, kf, int(time[kf]), lvl, session=info["session"])
                jn.notes = f"limit {lvl:.2f} filled bar +{kf - k} · {(float(close[k]) - lvl) * d:+.2f} vs the cross close"
                busy_until = run_exit(jn, kf, d, fill_bar=True); journeys.append(jn)
                continue
            jn = Journey("long" if d > 0 else "short", k, k, int(time[k]), float(close[k]), session=info["session"])
            busy_until = run_exit(jn, k, d); journeys.append(jn)

    done = [j for j in journeys if j.exit_reason not in (None, "open")]; wins = [j for j in done if j.result > 0]
    summary = {"crosses": sum(1 for e in events if e.label in (("ARM", "CANCEL") if rules.entry == "ema9touch" else ("EB", "ES"))),
               "pre": sum(1 for e in events if e.label == "PRE"),
               "trades": len(done), "wins": len(wins), "win_rate": round(len(wins) / len(done), 3) if done else 0.0,
               "net": round(sum(j.result for j in done), 2), "avg": round(sum(j.result for j in done) / len(done), 2) if done else 0.0,
               **{r: sum(1 for j in done if j.exit_reason == r) for r in ("stop", "locked", "tp", "ema_close", "friday_flat")},
               "entry": rules.entry, "unfilled": unfilled,
               "armed": sum(1 for e in events if e.label == "ARM"), "cancelled": sum(1 for e in events if e.label == "CANCEL"),
               "expired": sum(1 for e in events if e.label == "EXPIRE")}
    bar_states = ["bull" if sg[i] > 0 else "bear" if sg[i] < 0 else "n/a" for i in range(n)]
    return {"events": events, "journeys": journeys, "summary": summary, "bar_states": bar_states, "cross_infos": [], "pre": [e for e in events if e.label == "PRE"]}
