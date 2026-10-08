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


def weekly_report(cfg, agents, journal, previous: bool = False, mt5: bool = True) -> str:
    """mt5=False: journal part only (used by /report when the MT5 lock is busy)."""
    start, end = week_bounds(previous)
    mode = next(iter(agents.values())).S.display_name if agents else cfg.mode
    L = [f"**AUREON WEEKLY REPORT** · v{VERSION}", f"Mode: {mode}", f"Week: {start:%d %b} → {(end - timedelta(days=1)):%d %b %Y}"]
    total = n = wins = 0
    if not mt5:
        L.append("\n⏳ MT5 busy — closed trades skipped, journal data only")
    for sym in (cfg.symbols if mt5 else []):
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
    L += claude_summary(recs)
    if not n and not recs:
        L.append("nothing recorded this week")
    return "\n".join(L)


# ----------------------------------------------------------------------------- v1.10.0 Claude add-on scorecard
def _leg_for(verdict: dict, legs: list[dict]) -> dict | None:
    """The move that followed a signal: the leg on the same symbol/side that was running at the signal bar."""
    side = {"BUY": "LONG", "SELL": "SHORT"}.get(verdict.get("side"), verdict.get("side"))
    cands = [g for g in legs if g.get("symbol") == verdict.get("symbol") and g.get("side") == side
             and g.get("start_t") is not None and verdict.get("bar") is not None and g["start_t"] <= verdict["bar"]]
    return max(cands, key=lambda g: g["start_t"]) if cands else None


def claude_summary(recs: list[dict]) -> list[str]:
    """Claude TAKE vs SKIP outcomes · my decision vs Claude's view · pullback verdicts vs the guardian-only result."""
    cv = [r for r in recs if r.get("event") == "claude_verdict" and r.get("decision")]
    if not cv:
        return []
    legs = [r for r in recs if r.get("event") == "leg"]
    mine = {(r.get("symbol"), r.get("bar")): r.get("decision") for r in recs if r.get("event") == "decision"}
    exits = {r.get("ticket"): r for r in recs if r.get("event") == "exit"}
    closed = {r.get("ticket"): r for r in recs if r.get("event") == "closed"}
    L = ["\n**Claude add-on**"]
    entries = [r for r in cv if r.get("claude_event") in ("P", "CROSS", "RE")]
    for dec in ("TAKE", "SKIP"):
        xs = [r for r in entries if r["decision"] == dec]
        if xs:
            graded = [(_leg_for(r, legs) or {}).get("best") for r in xs]
            done = [b for b in graded if b is not None]
            hit = sum(1 for b in done if b >= 10)
            L.append(f"Claude {dec}: {len(xs)} · move known {len(done)} · reached +10: {hit}" + (" (missed)" if dec == "SKIP" and hit else ""))
    pairs = [(mine.get((r.get("symbol"), r.get("bar"))), r["decision"]) for r in entries]
    pairs = [(m, c) for m, c in pairs if m in ("TAKE", "SKIP")]
    if pairs:
        agree = sum(1 for m, c in pairs if m == c)
        L.append(f"You vs Claude: {len(pairs)} decided · agreed {agree} · differed {len(pairs) - agree}")
    pb = [r for r in cv if r.get("claude_event") == "pullback"]
    if pb:
        parts = []
        for dec in ("HOLD", "TIGHTEN", "CLOSE"):
            xs = [r for r in pb if r["decision"] == dec]
            if not xs:
                continue
            diffs = []
            for r in xs:
                ex = exits.get(r.get("ticket"))
                if ex is not None and r.get("open_profit") is not None and ex.get("points") is not None:
                    diffs.append(float(ex["points"]) - float(r["open_profit"]))
            acted = sum(1 for r in xs if r.get("acted"))
            txt = f"{dec} {len(xs)} (applied {acted})"
            if diffs:
                txt += f" · guardian exit vs verdict-time profit {sum(diffs) / len(diffs):+.1f} avg"
            parts.append(txt)
        L.append("Pullback verdicts: " + " · ".join(parts))
        unknown = sum(1 for r in pb if r.get("ticket") not in exits and r.get("ticket") in closed)
        if unknown:
            L.append(f"  ({unknown} closed by you/SL — exit points not journaled)")
    stale = sum(1 for r in cv if r.get("stale"))
    if stale:
        L.append(f"Stale verdicts (not acted): {stale}")
    return L


def claude_rows_for_batch(journal, since: int, until: int | None = None) -> list[dict]:
    """Rows for the existing Saturday Supabase batch → `aureon_decisions` with source='claude'. No new requests here."""
    out = []
    for r in journal.read(since, until):
        if r.get("event") != "claude_verdict":
            continue
        out.append({"source": "claude", "t": r.get("t"), "symbol": r.get("symbol"), "kind": r.get("claude_event"), "decision": r.get("decision"), "side": r.get("side"),
                    "confidence": r.get("confidence"), "reason": r.get("reason"), "model": r.get("model"),
                    "latency_s": r.get("latency_s"), "acted": r.get("acted"), "stale": r.get("stale"),
                    "bar": r.get("bar"), "ticket": r.get("ticket"), "my_action": r.get("my_action"), "error": r.get("error")})
    return out
