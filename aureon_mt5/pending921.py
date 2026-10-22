"""v2.17.0 — ema921 pending-order entry (AUREON_921_ENTRY=limit), behind /auto-trade.

    level     LONG  → buy limit  `offset` BELOW the cross close · SHORT → sell limit `offset` ABOVE it
    stops     SL / TP from the level (the rule's −stop / +tp), carried on the order
    expiry    the cross bar's close + ttl bars (server clock, like bar times) — ORDER_TIME_SPECIFIED
    fill      the position is adopted like a market entry (same guardian, lock, exit); `saved` = points better than the
              cross close
    paper     the replay's fill rule: the first LATER bar whose extreme reaches the level (never the signal bar)

State (logs/state_<SYMBOL>.json "pending"): {ticket|id, side, level, sl, tp, lots, bar, expires, cross_close, paper, by}.
"""
from __future__ import annotations


def side_sign(side: str) -> int:
    return 1 if side.upper() in ("LONG", "BUY") else -1


def level(side: str, close: float, offset: float) -> float:
    return round(float(close) - side_sign(side) * float(offset), 2)


def order_type(side: str) -> str:
    return "buy limit" if side_sign(side) > 0 else "sell limit"


def stops(side: str, lvl: float, stop: float, tp: float | None) -> tuple[float, float | None]:
    s = side_sign(side)
    return round(lvl - s * float(stop), 2), (round(lvl + s * float(tp), 2) if tp else None)


def expiry(bar_t: int, bar_s: int, ttl_bars: int) -> int:
    """The signal bar's close + ttl bars: a 2-bar TTL on M30 = the next two bars may fill it."""
    return int(bar_t) + int(bar_s) * (1 + int(ttl_bars))


def marketable(side: str, lvl: float, bid: float, ask: float) -> bool:
    """The level is already reached (a buy limit at/above the ask, a sell limit at/below the bid) — MT5 refuses such a
    limit; it fills at the level or better as a market order."""
    return (ask <= lvl) if side_sign(side) > 0 else (bid >= lvl)


def bar_fills(side: str, lvl: float, high: float, low: float) -> bool:
    return (low <= lvl) if side_sign(side) > 0 else (high >= lvl)


def saved(side: str, cross_close: float | None, fill: float) -> float | None:
    """Points better than a market entry at the cross close (+ = better)."""
    if cross_close is None:
        return None
    return round(side_sign(side) * (float(cross_close) - float(fill)), 2)


def title(p: dict, ist_fmt) -> str:
    """`PENDING SHORT · sell limit 4012.50 · SL 4042.50 · TP — · expires 14:30 IST`"""
    return (f"PENDING {p['side']} · {order_type(p['side'])} {p['level']:.2f} · SL {p['sl']:.2f} · "
            f"TP {format(p['tp'], '.2f') if p.get('tp') else '—'} · expires {ist_fmt(p['expires'])}"
            + (" · PAPER" if p.get("paper") else ""))


def parse_limit(raw, side: str, bid: float, ask: float) -> float:
    """/place … limit=<price|±pts>: a number ≤ 200 is a distance BACK from the market (buy below the ask, sell above the bid;
    the sign is ignored), larger is a price that must be on that side."""
    v = float(str(raw).strip().lower().replace("limit=", "").replace("limit:", ""))
    s = side_sign(side)
    if abs(v) <= 200:
        if abs(v) == 0:
            raise ValueError("limit=0 is a market order — drop limit=")
        return round((ask if s > 0 else bid) - s * abs(v), 2)
    if s > 0 and v >= ask:
        raise ValueError(f"a buy limit must be below the ask {ask:.2f} (got {v:.2f}) — /place long without limit= for a market order")
    if s < 0 and v <= bid:
        raise ValueError(f"a sell limit must be above the bid {bid:.2f} (got {v:.2f}) — /place short without limit= for a market order")
    return round(v, 2)
