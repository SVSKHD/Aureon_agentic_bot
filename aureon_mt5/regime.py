"""v2.10.0 — regime gate: trade when it is clean, never into whipsaw, no fixed count.

PRINCIPLE: every entry must lead away from drawdown. There is NO max-trades cap — if the market is clean all day, every valid
setup is tradeable; if it whipsaws all day, there are no trades that day. The gate is a REGIME, computed on every closed bar,
not a counter.

  regime_features(df)  crosses_2h · flat_gap_bars · adx14 (+ rising) · di_spread · failed_p_2h · range_compression
                       (· zl_crosses_2h when the zero-lag columns are present). Pure, last closed bar, no lookahead.
  regime_score()       0 = dead chop … 1 = clean trend: min(whipsaw half, energy half), each a weighted mean
                       (weights in Config.regime_weights)
  classify()           CLEAN | CHOPPY | DEAD with hysteresis: CHOPPY below AUREON_REGIME_CHOPPY, DEAD below
                       AUREON_REGIME_DEAD; CHOPPY → CLEAN only after AUREON_REGIME_CLEAN_BARS consecutive bars >=
                       AUREON_REGIME_CLEAN; DEAD → CLEAN only via CHOPPY.
  no_trade_day()       CHOPPY/DEAD covered >= AUREON_NO_TRADE_DAY_FRAC of today's session bars AND past NY open. A card,
                       not a lock: a later CLEAN streak reopens entries exactly as usual.
  AUREON_REGIME_GATE   off (compute + journal) · show (REGIME line on entry cards) · block (CHOPPY/DEAD: signal carded as
                       "CHOP · not tradeable", no ask, no Claude entry read, /take refused). Open positions are never touched.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone

import numpy as np

IST = timezone(timedelta(hours=5, minutes=30))
WINDOW = 24                    # 2 h of M5 bars (v2.14.0: _window() converts 2 h to the exec TF)
FLAT_WINDOW = 12               # 1 h of M5 bars (_flat_window())


def _window() -> int:
    from .common.tf_synth import bars_for, exec_tf
    return WINDOW if exec_tf() == "M5" else bars_for(120)


def _bar_s() -> int:
    from .common.tf_synth import bar_s
    return bar_s()


def _flat_window() -> int:
    from .common.tf_synth import bars_for, exec_tf
    return FLAT_WINDOW if exec_tf() == "M5" else bars_for(60)
SESSION_START_IST = (5, 30)
NY_OPEN_IST = (19, 0)          # 09:30 New York ≈ 19:00 IST
DEFAULT_WEIGHTS = {"crosses": 0.30, "flat_gap": 0.20, "adx": 0.20, "di": 0.10, "failed_p": 0.10, "compression": 0.10}


def _sign_changes(a: np.ndarray) -> int:
    s = np.sign(a); s = s[s != 0]
    return int(np.sum(s[1:] != s[:-1])) if len(s) > 1 else 0


def failed_ps(gap: np.ndarray, p_idx: list[tuple[int, int]], window: int | None = None) -> int:
    """P's (index, +1 long / −1 short) in the last `window` bars whose cross never came within 6 bars, or came and reversed
    within 3. A P younger than 6 bars without its cross yet is not counted."""
    n = len(gap); bad = 0
    for k, d in p_idx:
        if k < n - (window or _window()) or k >= n:
            continue
        j = next((i for i in range(k + 1, min(n, k + 7)) if np.sign(gap[i]) == d), None)
        if j is None:
            bad += int(n - 1 - k >= 6)
        elif any(np.sign(gap[i]) == -d for i in range(j + 1, min(n, j + 4))):
            bad += 1
    return bad


def regime_features(df, cf: str = "ema_fast", cs: str = "ema_slow", p_idx: list | None = None) -> dict:
    gap = (df[cf] - df[cs]).to_numpy(dtype=float)
    rng = (df["high"] - df["low"]).to_numpy(dtype=float)
    avg20 = float(np.nanmean(rng[-20:])) if len(rng) else 0.0
    if "adx14" in df:
        adx14 = df["adx14"].to_numpy(dtype=float); dip = df["di_plus"].to_numpy(dtype=float); dim = df["di_minus"].to_numpy(dtype=float)
    else:
        from .common.zl import adx
        a, p, m = adx(df, 14); adx14, dip, dim = a.to_numpy(), p.to_numpy(), m.to_numpy()
    r20 = (df["high"].rolling(20).max() - df["low"].rolling(20).min()).to_numpy(dtype=float)
    ref = np.nanmean(r20[-120:]) if np.isfinite(r20[-120:]).any() else np.nan
    f = {"crosses_2h": _sign_changes(gap[-(_window() + 1):]),
         "flat_gap_bars": int(np.sum(np.abs(gap[-_flat_window():]) < 0.3 * avg20)) if avg20 > 0 else 0,
         "adx14": round(float(adx14[-1]), 1) if len(adx14) else 0.0,
         "adx14_rising": bool(len(adx14) > 3 and adx14[-1] > adx14[-4]),
         "di_spread": round(float(abs(dip[-1] - dim[-1])), 1) if len(dip) else 0.0,
         "failed_p_2h": failed_ps(gap, p_idx or []),
         "range_compression": round(float(r20[-1] / ref), 2) if ref and np.isfinite(ref) and ref > 0 and np.isfinite(r20[-1]) else 1.0}
    if "zl50" in df:
        f["zl_crosses_2h"] = _sign_changes((df["zl50"] - df["zl80"]).to_numpy(dtype=float)[-(_window() + 1):])
    return f


def components(f: dict) -> dict:
    clip = lambda x: float(min(1.0, max(0.0, x)))
    crosses = max(f["crosses_2h"], f.get("zl_crosses_2h", 0) - 1)
    adx = clip((f["adx14"] - 15.0) / 20.0) + (0.15 if f["adx14_rising"] and f["adx14"] >= 15 else 0.0)
    return {"crosses": 1.0 - min(crosses, 3) / 3.0, "flat_gap": 1.0 - f["flat_gap_bars"] / _flat_window(), "adx": clip(adx),
            "di": clip(f["di_spread"] / 20.0), "failed_p": 1.0 - min(f["failed_p_2h"], 2) / 2.0,
            "compression": clip(f["range_compression"])}


def regime_score(f: dict, weights: dict | None = None) -> float:
    """min(whipsaw, energy): a market must be BOTH untangled (few crosses, a real gap, P's that worked) AND moving (ADX, DI
    spread, a normal range) to score as clean. Each half is a weighted mean of its components (weights in Config)."""
    w = weights or DEFAULT_WEIGHTS; c = components(f)
    def wmean(keys):
        tot = sum(w.get(k, 0) for k in keys)
        return sum(w.get(k, 0) * c[k] for k in keys) / tot if tot else 1.0
    return round(min(wmean(("crosses", "flat_gap", "failed_p")), wmean(("adx", "di", "compression"))), 3)


def reasons(f: dict) -> list[str]:
    out = []
    if f["crosses_2h"] >= 2:
        out.append(f"{f['crosses_2h']} crosses in 2 h")
    if f.get("zl_crosses_2h", 0) >= 3:
        out.append(f"{f['zl_crosses_2h']} ZL crosses in 2 h")
    if f["flat_gap_bars"] >= 6:
        out.append(f"EMA gap flat {f['flat_gap_bars']}/{_flat_window()} bars")
    if f["adx14"] < 20:
        out.append(f"ADX 14 {'flat' if not f['adx14_rising'] else 'low'} ({f['adx14']:.0f})")
    if f["di_spread"] < 5:
        out.append(f"DI spread {f['di_spread']:.0f}")
    if f["failed_p_2h"]:
        out.append(f"{f['failed_p_2h']} failed P in 2 h")
    if f["range_compression"] < 0.6:
        out.append(f"range compressed {f['range_compression']:.1f}×")
    return out


def classify(score: float, prev: str | None, clean_streak: int, cfg) -> tuple[str, int]:
    """→ (state, clean_streak). Hysteresis as in the module docstring."""
    choppy, dead, clean, need = cfg.regime_choppy, cfg.regime_dead, cfg.regime_clean, int(cfg.regime_clean_bars)
    if prev in (None, "CLEAN"):
        return ("DEAD" if score < dead else "CHOPPY" if score < choppy else "CLEAN"), 0
    if prev == "DEAD":
        return ("CHOPPY", 0) if score >= choppy else ("DEAD", 0)
    if score < dead:                                         # CHOPPY
        return "DEAD", 0
    streak = clean_streak + 1 if score >= clean else 0
    return ("CLEAN", 0) if streak >= need else ("CHOPPY", streak)


def _session_start(now_ist: datetime) -> datetime:
    s = now_ist.replace(hour=SESSION_START_IST[0], minute=SESSION_START_IST[1], second=0, microsecond=0)
    return s if now_ist >= s else s - timedelta(days=1)


def no_trade_day(regime_log: list[tuple[int, str]], now_ist: datetime, frac: float) -> bool:
    """regime_log: [(unix_time, state)] per closed bar. True once CHOPPY/DEAD covered >= frac of today's session bars
    so far AND the clock is past NY open."""
    if (now_ist.hour, now_ist.minute) < NY_OPEN_IST:
        return False
    start = _session_start(now_ist).timestamp()
    today = [s for t, s in regime_log if t >= start]
    return bool(today) and sum(s in ("CHOPPY", "DEAD") for s in today) / len(today) >= frac


class RegimeTracker:
    """Per agent. update() once per closed bar; never touches positions."""

    def __init__(self, cfg, journal=None, symbol: str = ""):
        self.cfg, self.journal, self.symbol = cfg, journal, symbol
        self.state: str | None = None; self.score = 1.0; self.since_bar: int | None = None; self.clean_streak = 0
        self.reasons: list[str] = []; self.features: dict = {}
        self.log: deque = deque(maxlen=400)                  # (unix wall time, state) per bar for no_trade_day
        self.p_hist: deque = deque(maxlen=50)                 # (bar_time, +1/−1) P signals seen by the agent
        self.no_trade_day = False

    def note_p(self, bar_time: int, side: str):
        self.p_hist.append((int(bar_time), 1 if side.upper() == "LONG" else -1))

    def update(self, df, bar_t: int, cf: str = "ema_fast", cs: str = "ema_slow", wall: float | None = None) -> dict:
        times = df["time"].to_numpy()
        idx = {int(t): i for i, t in enumerate(times[-(_window() + 10):], start=len(times) - min(len(times), _window() + 10))}
        p_idx = [(idx[t], d) for t, d in self.p_hist if t in idx]
        f = regime_features(df, cf, cs, p_idx)
        score = regime_score(f, getattr(self.cfg, "regime_weights", None))
        prev = self.state
        state, self.clean_streak = classify(score, prev, self.clean_streak, self.cfg)
        self.features, self.score, self.reasons = f, score, reasons(f)
        if state != prev:
            self.since_bar = int(bar_t)
            if self.journal is not None:
                self.journal.log("regime_change", symbol=self.symbol, old=prev, new=state, score=score, reasons=self.reasons,
                                 features=f)
        self.state = state
        import time as _t
        now = _t.time() if wall is None else wall
        self.log.append((now, state))
        self.no_trade_day = no_trade_day(list(self.log), datetime.fromtimestamp(now, tz=IST), self.cfg.no_trade_day_frac)
        return self.as_dict(bar_t)

    def as_dict(self, bar_t: int | None = None) -> dict:
        since = int((int(bar_t) - self.since_bar) // _bar_s()) if (bar_t is not None and self.since_bar is not None) else None
        return {"state": self.state, "score": self.score, "reasons": list(self.reasons), "since_bar": self.since_bar,
                "since_bars": since, "no_trade_day": self.no_trade_day, "features": dict(self.features)}

    def blocks(self) -> bool:
        return getattr(self.cfg, "regime_gate", "off") == "block" and self.state in ("CHOPPY", "DEAD")

    def line(self) -> str:
        why = ", ".join(self.reasons[:3])
        return f"REGIME · {self.state or '—'} · {self.score:.2f}" + (f" · {why}" if why else "")


def replay_states(bars, over: dict | None = None, cf: str = "ema_fast", cs: str = "ema_slow") -> tuple[dict, list]:
    """For the backtest: {bar index: state} over the whole frame and the list of no-trade days (IST date strings).
    P signals come from the strategy's analysis of the same bars."""
    import types
    from .config import Config
    cfg = Config()
    for k, v in (over or {}).items():
        setattr(cfg, k, v)
    tr = RegimeTracker(cfg)
    try:
        from tools.guardian_sim import p_signals
        ps = p_signals(bars)
    except Exception:
        ps = {}
    states, days = {}, set()
    start = max(100, 0)
    for i in range(start, len(bars)):
        t = int(bars["time"].iloc[i])
        if t in ps:
            tr.note_p(t, ps[t])
        wall = t - 3 * 3600                                    # server time → UTC (replays use the fixtures' +3 h offset)
        tr.update(bars.iloc[: i + 1], t, cf, cs, wall=wall)
        states[i] = tr.state
        if tr.no_trade_day:
            days.add(datetime.fromtimestamp(wall, tz=IST).strftime("%Y-%m-%d"))
    return states, sorted(days)
