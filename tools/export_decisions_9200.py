"""v2.15.0 — the ema9200 TAKE / SKIP log → the replay's decisions file.

    python tools/export_decisions_9200.py                                   # logs/journal.jsonl → logs/decisions_9200.json
    python tools/export_decisions_9200.py --log-dir logs --symbol XAUUSD --from 2026-10-19 --to 2026-10-24
    python tools/replay_9200.py --csv xauusd_m30_....csv --decisions logs/decisions_9200.json

Output: {"<bar_time_epoch>": true | false}. The key is the candidate's exec-TF bar time, the same server-time epoch the bars
CSV uses, so `tools/replay_9200.py --decisions` replays that week.
    true   TAKE (the button, /take, the pad or push — the `decision` row, even if the order was then refused)
    false  SKIP or EXPIRED, and a `signal` row with no decision at all (an ask nobody answered: EXPIRED is only journaled
           by the Discord view's timeout, so without the bot running the unanswered ask leaves no row)
The last decision on a bar wins. Only mode=ema9200 rows are read."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

MODE = "ema9200"


def _day(s: str | None, end: bool = False) -> int | None:
    if not s:
        return None
    t = int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    return t + 86400 if end else t


def read_rows(path: str) -> list[dict]:
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def decisions(rows: list[dict], *, symbol: str | None = None, since: int | None = None, until: int | None = None) -> dict:
    """{bar_time: bool} from journal rows (pure)."""
    out: dict[int, bool] = {}

    def keep(r) -> bool:
        if r.get("mode") != MODE or r.get("bar") is None:
            return False
        if symbol and str(r.get("symbol", "")).upper() != symbol.upper():
            return False
        b = int(r["bar"])
        return (since is None or b >= since) and (until is None or b < until)

    for r in rows:                                   # every candidate offered starts as "not taken"
        if r.get("event") == "signal" and keep(r):
            out.setdefault(int(r["bar"]), False)
    for r in rows:                                   # journal order: the last decision on a bar wins
        if r.get("event") == "decision" and keep(r):
            out[int(r["bar"])] = str(r.get("decision", "")).upper() == "TAKE"
    return dict(sorted(out.items()))


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--journal", help="default: <log-dir>/journal.jsonl")
    ap.add_argument("--out", help="default: <log-dir>/decisions_9200.json")
    ap.add_argument("--symbol", default="")
    ap.add_argument("--from", dest="date_from", help="YYYY-MM-DD (bar time, server epoch)")
    ap.add_argument("--to", dest="date_to", help="YYYY-MM-DD, inclusive")
    a = ap.parse_args(argv)
    src = a.journal or os.path.join(a.log_dir, "journal.jsonl")
    out_path = a.out or os.path.join(a.log_dir, "decisions_9200.json")
    d = decisions(read_rows(src), symbol=a.symbol or None, since=_day(a.date_from), until=_day(a.date_to, end=True))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in d.items()}, f, indent=1)
    taken = sum(d.values())
    print(f"{len(d)} candidates · TAKE {taken} · SKIP/EXPIRED {len(d) - taken} → {out_path}")
    return d


if __name__ == "__main__":
    main()
