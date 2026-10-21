"""v2.16.2 — explain a burst of orders from the journal (e.g. the five positions of 2026-10-15 15:23 IST).

    python tools/order_burst.py --from "2026-10-15 15:20" --to "2026-10-15 15:26"          # IST, logs/journal.jsonl
    python tools/order_burst.py --journal D:/Aureon/logs/journal.jsonl --from ... --to ...

Prints every order-related row in the window (order_placed, decision / alert_decision, console_call / console_refused,
governor_block, position_seen, entry_blocked_position_cap), then classifies the burst:
    walk-back      several order_placed rows on DIFFERENT ask keys by the same user within seconds — /take or a phone
                   TAKE without a key takes the newest *undecided* ask each time (Actions.latest_open), so every tap opens a
                   position on the next-older open ask (P / CROSS / ENTER / RE cards stack up during a move)
    same-card      several order_placed rows on the SAME key — a card answered twice (copies, a stale view after a restart)
    console/push   console_call rows (source=push / console) right before the orders — the phone actions
    rejected-but-filled  order_placed ok=False followed by position_seen — the order reported as failed (timeout,
                   10010 partial fill) and the human placed again
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
EVENTS = ("order_placed", "decision", "alert_decision", "console_call", "console_refused", "governor_block", "position_seen",
          "entry_blocked_position_cap", "take_refused_regime", "paper_fill")


def _t(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp()


def window(rows: list[dict], t0: float, t1: float) -> list[dict]:
    return sorted((r for r in rows if r.get("event") in EVENTS and t0 <= float(r.get("t", 0)) <= t1), key=lambda r: r.get("t", 0))


def key_of(r: dict) -> str:
    return str(r.get("alert_id") or r.get("key") or r.get("card_key") or "")


def classify(rows: list[dict]) -> list[str]:
    orders = [r for r in rows if r.get("event") == "order_placed"]
    filled = [r for r in orders if r.get("ok")]
    seen = [r for r in rows if r.get("event") == "position_seen"]
    out = []
    if len(seen) >= 2 or len(filled) >= 2:
        keys = [key_of(r) for r in orders if key_of(r)]
        users = {str(r.get("by")) for r in orders}
        if len(set(keys)) >= 2 and len(users) == 1:
            out.append(f"walk-back: {len(orders)} orders on {len(set(keys))} different asks by {users.pop()} — /take or a phone TAKE "
                       "without a key took the next-older open ask each time")
        if len(keys) != len(set(keys)):
            dup = sorted({k for k in keys if keys.count(k) > 1})
            out.append(f"same-card: key(s) {', '.join(dup)} placed more than once")
        if any(r.get("event") == "console_call" for r in rows):
            srcs = sorted({str(r.get("source")) for r in rows if r.get("event") == "console_call"})
            out.append(f"console/push: console_call rows in the window (source {', '.join(srcs)})")
        bad = [r for r in orders if not r.get("ok")]
        if bad and len(seen) > len(filled):
            out.append(f"rejected-but-filled: {len(bad)} order(s) reported failed ({', '.join(str(r.get('retcode')) for r in bad)}) "
                       f"but {len(seen)} positions appeared")
        if len(seen) > len(orders):
            out.append(f"{len(seen) - len(orders)} position(s) appeared with no order_placed row — placed in MT5 by hand / another EA")
    return out or ["no burst in this window"]


def main(argv=None) -> list[str]:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--journal", default=os.path.join("logs", "journal.jsonl"))
    ap.add_argument("--from", dest="t0", required=True); ap.add_argument("--to", dest="t1", required=True)
    a = ap.parse_args(argv)
    rows = []
    with open(a.journal, encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    w = window(rows, _t(a.t0), _t(a.t1))
    for r in w:
        ts = datetime.fromtimestamp(float(r["t"]), tz=IST).strftime("%H:%M:%S")
        print(f"{ts} {r['event']:<28} key={key_of(r) or '—':<36} by={r.get('by', '—')} side={r.get('side', '—')} "
              f"ok={r.get('ok', '—')} retcode={r.get('retcode', '—')} ticket={r.get('ticket', '—')} lots={r.get('lots', '—')} "
              f"source={r.get('source', r.get('lots_source', '—'))}")
    verdict = classify(w)
    print("\n".join("→ " + v for v in verdict))
    return verdict


if __name__ == "__main__":
    main()
