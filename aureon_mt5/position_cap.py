"""v2.16.2 — at most ONE open position per symbol, every mode, before any placement (auto, TAKE, alert buttons, /place,
/override, the console / phone). Callers refuse with the reason and journal entry_blocked_position_cap; broker.place_market
re-checks under its lock (the last line: two taps racing each other cannot both fill)."""
from __future__ import annotations

from . import broker

MAX_PER_SYMBOL = 1


def open_tickets(symbol: str, paper_positions=()) -> list:
    """Live positions + pending orders (v2.17.0: a pending order counts as "in position") + paper positions / pending."""
    try:
        live = [p["ticket"] for p in broker.positions(symbol)]
    except Exception:
        live = []
    try:
        live += [o["ticket"] for o in (broker.pending_orders(symbol) or [])]
    except Exception:
        pass
    return live + [p.get("id") for p in (paper_positions or [])]


def block(symbol: str, side: str, *, journal=None, source: str = "", paper_positions=(), by: str = "") -> str | None:
    """The refusal text when the symbol already has MAX_PER_SYMBOL open position(s), else None. Journals the block."""
    held = open_tickets(symbol, paper_positions)
    if len(held) < MAX_PER_SYMBOL:
        return None
    why = f"position cap: {symbol} already has {len(held)} open ({', '.join('#' + str(t) for t in held)}) — max {MAX_PER_SYMBOL} per symbol"
    if journal is not None:
        try:
            journal.log("entry_blocked_position_cap", symbol=symbol, side=str(side).upper(), source=source, open=held, by=str(by))
        except Exception:
            pass
    return why
