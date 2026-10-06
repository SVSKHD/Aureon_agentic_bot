"""Weekly report: closed trades from MT5 history + journal (signals by signal_kind, secure steps, exits), per mode."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from . import broker
from .config import VERSION

IST = timezone(timedelta(hours=5, minutes=30))


def week_bounds(previous: bool = False) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    if previous:
        monday -= timedelta(days=7)
    return monday, monday + timedelta(days=7)


def weekly_report(cfg, agents, journal, previous: bool = False) -> str:
    start, end = week_bounds(previous)
    mode = next(iter(agents.values())).S.display_name if agents else cfg.mode
    L = [f"**AUREON WEEKLY REPORT** · v{VERSION}", f"Mode: {mode}", f"Week: {start:%d %b} → {(end - timedelta(days=1)):%d %b %Y}"]
    total = n = wins = 0
    for sym in cfg.symbols:
        deals = broker.closed_deals(sym, start, end)
        if deals:
            net = sum(d["profit"] for d in deals); w = sum(1 for d in deals if d["profit"] > 0)
            total += net; n += len(deals); wins += w
            L.append(f"\n**{sym}**: {len(deals)} trades · net {net:+.2f} · wins {w}/{len(deals)}")
            for d in deals[-15:]:
                L.append(f"  · {datetime.fromtimestamp(d['time'], tz=IST):%a %d %H:%M} {d['direction']} {d['volume']} @ {d['price']:.2f} → {d['profit']:+.2f}")
    if n:
        L.append(f"\n**All: {n} trades · net {total:+.2f} · win {100 * wins / n:.0f}%**")
    recs = journal.read(int(start.timestamp()), int(end.timestamp()))
    sig = Counter(r.get("signal_kind", "?") for r in recs if r.get("event") == "signal")
    sec = [r for r in recs if r.get("event") == "secured"]; ex = Counter(r.get("reason", "?") for r in recs if r.get("event") == "exit")
    L.append(f"\nSignals: " + (", ".join(f"{k} {v}" for k, v in sig.items()) or "none"))
    L.append(f"Secure steps: {len(sec)} · locked total +{sum(float(r.get('step', 0)) for r in sec):g}")
    L.append("Auto exits: " + (", ".join(f"{k} {v}" for k, v in ex.items()) or "none"))
    if not n and not recs:
        L.append("nothing recorded this week")
    return "\n".join(L)
