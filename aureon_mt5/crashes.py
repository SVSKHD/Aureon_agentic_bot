"""Agent crash reporting and bounded automatic recovery."""
from __future__ import annotations
import threading
import time
import traceback
from collections import defaultdict, deque

_lock = threading.Lock()
_repeats = defaultdict(deque)
paused_symbols = set()
crash_reasons = {}

def report(agent, exc, window=600, limit=3):
    sym = agent.symbol
    typ = type(exc).__name__
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    trace = "\n".join(tb.splitlines()[-15:])
    phase = (getattr(agent, "health", {}) or {}).get("status", "?")
    try:
        positions = (getattr(agent, "snapshot", {}) or {}).get("positions") or []
        position = str(positions[:1])[:250] if positions else "none"
    except Exception:
        position = "unknown"
    with _lock:
        q = _repeats[(sym, typ)]
        now = time.monotonic()
        while q and now - q[0] > window: q.popleft()
        q.append(now)
        repeated = len(q) >= limit
        if repeated: paused_symbols.add(sym)
        crash_reasons[sym] = f"{typ}: {str(exc)[:160]}"
    agent.journal.log("agent_crash", symbol=sym, exc=crash_reasons[sym], trace=trace, phase=phase)
    message = (f"{typ}: {str(exc)[:160]}\nLast bar {agent.health.get('last_bar') or '—'} · "
               f"phase {phase} · position {position}\n```python\n{trace[-1000:]}\n```")
    try:
        agent.notify.card(f"⚠️ AGENT CRASH · {sym}", message[:3800],
                          footer="restarting in 30 s" if not repeated else "paused — /restart to retry")
        if repeated:
            agent.notify.card(f"⚠️ REPEATED CRASH · {sym}", "agent paused after 3 repeated exception types in 10 min; /restart to retry")
    except Exception:
        pass
    return repeated

def should_restart(symbol):
    with _lock: return symbol not in paused_symbols

def reason(symbol):
    with _lock: return crash_reasons.get(symbol, "")
