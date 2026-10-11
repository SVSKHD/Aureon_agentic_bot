"""The mode's own entry verdict for one closed bar — read from the strategy's analysis, never a new rule.

    v = entry_verdict(S, res, i, server_offset_h)   # {"decision": LONG|SHORT|WAIT|NO TRADE, "reason": str, "kind": str, ...}

ema2050: the per-bar state journeys.build() computed (bar_states[i]).
ema5080: the events the 50/80 rules produced on that bar (EB/ES = an entry, F = blocked, WP = whipsaw), else WAIT.
detect.py prints these per bar and the ALERT REACHED card's SUGGEST line uses the same function (a test checks they agree)."""
from __future__ import annotations

import numpy as np


def entry_verdict(S, res: dict, i: int, server_offset_h: float = 3.0) -> dict:
    df = res["df"]
    cf, cs = S.ema_cols
    n = len(df)
    if i < 0 or i >= n:
        return {"decision": "WAIT", "reason": "no bar", "kind": ""}
    ef = float(df[cf].iloc[i]); es = float(df[cs].iloc[i])
    if np.isnan(ef) or np.isnan(es):
        return {"decision": "WAIT", "reason": "EMAs warming up", "kind": ""}
    if getattr(S, "name", "") == "ema2050" and res.get("bar_states"):
        st = res["bar_states"][i]
        return {"decision": st["verdict"], "reason": st["reason"], "kind": st.get("entry") and "enter" or "",
                "confirm": st["confirm"], "touch": st["touch"], "bars_since_cross": st["bars_since_cross"],
                "direction": st["direction"], "shoot": st["shoot"]}
    # ---- ema5080: read the bar's events
    evs = [e for e in res.get("events", []) if e.index == i]
    sign = np.sign(df[cf].to_numpy() - df[cs].to_numpy())
    j = i
    while j > 0 and sign[j - 1] == sign[i]:
        j -= 1
    since = i - j
    order = f"EMA {S.fast} {'above' if ef > es else 'below'} EMA {S.slow}"
    for e in evs:
        if e.label.startswith(("EB", "ES")):
            side = "LONG" if e.direction == "bull" else "SHORT"
            kind = "P" if "pre" in e.label else "late" if "late" in e.label else "pullback" if "pb" in e.label else "cross"
            text = {"P": "P pre-cross", "cross": f"{S.fast}/{S.slow} cross confirmed", "late": "late entry", "pullback": "pullback entry"}[kind]
            return {"decision": side, "reason": f"{text} on this bar · {order} · {since} bars since cross", "kind": kind,
                    "bars_since_cross": since, "direction": "bull" if ef > es else "bear"}
    for e in evs:
        if e.label == "F":
            return {"decision": "NO TRADE", "reason": e.reason, "kind": "", "bars_since_cross": since, "direction": "bull" if ef > es else "bear"}
    for e in evs:
        if e.label == "WP":
            return {"decision": "WAIT", "reason": f"whipsaw — {e.reason}", "kind": "", "bars_since_cross": since, "direction": "bull" if ef > es else "bear"}
    return {"decision": "WAIT", "reason": f"no setup on this bar · {order} · {since} bars since cross", "kind": "",
            "bars_since_cross": since, "direction": "bull" if ef > es else "bear"}
