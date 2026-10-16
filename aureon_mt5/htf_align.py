"""v2.14.0 — the higher-timeframe alignment gate (AUREON_HTF_ALIGN=off|show|block, default off).

For each confirm timeframe (AUREON_HTF_CONFIRM_TFS, default the two rungs above the exec TF), on its CLOSED bars, with the
same EMA pair as the strategy:

    gap              EMA fast − EMA slow, signed toward the trade (positive = with the trade)
    gap_x_range      gap ÷ that TF's 20-bar average range
    gap_slope3       gap − gap 3 bars ago, toward the trade (positive = converging toward / widening our way)
    bars_since_cross +n = crossed our way n bars ago · −n = crossed against n bars ago · None = no cross in the data

    with        gap > 0 and the cross is fresh (bars_since_cross <= AUREON_HTF_WITH_MAX_BARS)
    with_stale  gap > 0 but the cross is older — counts as `with` unless AUREON_HTF_REQUIRE_FRESH=1
    pre         gap <= 0 (still against) BUT converging (gap_slope3 > 0) AND |gap_x_range| <= AUREON_HTF_PRE_MAX_GAP
    against     anything else (a wide gap against, or the gap widening against)
    unknown     no data for that TF (fails closed: never counts as with / pre)

Gate (AUREON_HTF_ALIGN_MIN): all = every confirm TF is with / pre · any = at least one with and none against.
Open positions are never touched by this gate. Pure functions; the agent supplies the frames."""
from __future__ import annotations

import numpy as np

STATES = ("with", "with_stale", "pre", "against", "unknown")


def htf_state(df, direction: str, *, cf: str = "ema_fast", cs: str = "ema_slow", pre_max_gap: float = 0.35,
              with_max_bars: int = 8) -> dict:
    """df: closed bars of one TF with the EMA columns. direction: LONG / SHORT (or BUY / SELL)."""
    s = 1 if str(direction).upper() in ("LONG", "BUY", "BULL") else -1
    if df is None or len(df) < 25 or cf not in df or cs not in df:
        return {"state": "unknown", "gap": None, "gap_x_range": None, "gap_slope3": None, "bars_since_cross": None}
    raw = (df[cf] - df[cs]).to_numpy(dtype=float)
    ok = ~np.isnan(raw)
    raw = raw[ok]
    if len(raw) < 4:
        return {"state": "unknown", "gap": None, "gap_x_range": None, "gap_slope3": None, "bars_since_cross": None}
    rng = (df["high"] - df["low"]).to_numpy(dtype=float)[ok]
    avg = float(np.mean(rng[-20:])) if len(rng) else 0.0
    gap = float(raw[-1]) * s
    slope3 = float(raw[-1] - raw[-4]) * s
    sg = np.sign(raw)
    idx = [k for k in range(1, len(sg)) if sg[k] != 0 and sg[k - 1] != 0 and sg[k] != sg[k - 1]]
    if idx:
        n = len(sg) - 1 - idx[-1]
        bsc = n if sg[-1] * s > 0 else -n
    else:
        bsc = None
    gx = gap / avg if avg > 0 else 0.0
    if gap > 0:
        state = "with" if (bsc is not None and 0 <= bsc <= with_max_bars) else "with_stale"
    elif slope3 > 0 and abs(gx) <= pre_max_gap:
        state = "pre"
    else:
        state = "against"
    return {"state": state, "gap": round(gap, 3), "gap_x_range": round(gx, 3), "gap_slope3": round(slope3, 3),
            "bars_since_cross": bsc}


def gate(states: dict, *, mode_min: str = "all", require_fresh: bool = False) -> tuple[bool, list[str]]:
    """states: {tf: htf_state(...)}. Returns (allowed, reasons for a refusal)."""
    def eff(st):
        x = st["state"]
        if x == "with_stale":
            return "against" if require_fresh else "with"
        return x
    effs = {tf: eff(st) for tf, st in states.items()}
    if not effs:
        return True, []
    bad = [f"{tf} {states[tf]['state']}" for tf, e in effs.items() if e in ("against", "unknown")]
    if mode_min == "any":
        ok = not bad and any(e == "with" for e in effs.values())
        if not bad and not ok:
            bad = ["no confirm TF has crossed our way yet (any: needs one `with`)"]
        return ok, bad
    return not bad, bad


def describe(tf: str, st: dict) -> str:
    x = st["state"]
    if x == "unknown":
        return f"{tf} no data"
    if x in ("with", "with_stale"):
        b = st.get("bars_since_cross")
        return f"{tf} {x} ({'crossed ' + str(b) + ' bars ago' if b is not None and b >= 0 else 'no fresh cross'})"
    gx = st.get("gap_x_range") or 0.0
    trend = "converging" if (st.get("gap_slope3") or 0) > 0 else "widening against"
    return f"{tf} {x} (gap {gx:+.2f}× rng, {trend})"


def line(states: dict) -> str:
    """`HTF · M45 pre (gap −0.21× rng, converging) · H1 with (crossed 2 bars ago)`"""
    return "HTF · " + " · ".join(describe(tf, st) for tf, st in states.items())


def compact(states: dict) -> dict:
    """The snapshot block (per TF: state, gap_x_range, gap_slope3, bars_since_cross)."""
    return {tf: {k: st.get(k) for k in ("state", "gap_x_range", "gap_slope3", "bars_since_cross")} for tf, st in states.items()}


def confirm_tfs_for(cfg) -> list[str]:
    from .common.tf_synth import confirm_tfs
    tfs = [str(x).upper() for x in (getattr(cfg, "htf_confirm_tfs", ()) or ())]
    if tfs:
        return tfs
    try:
        return list(confirm_tfs(str(getattr(cfg, "exec_tf", "M5")).upper()))
    except ValueError:
        return []
