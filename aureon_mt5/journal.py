"""Append-only JSON-lines journal + persisted guardian state (nested per symbol; an agent only touches its own section)."""
from __future__ import annotations

import json
import os
import threading
import time as _time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
_state_lock = threading.RLock()


class Journal:
    def __init__(self, log_dir: str):
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, "journal.jsonl")
        self.state_path = os.path.join(log_dir, "guardian_state.json")
        self._lock = threading.Lock()

    def log(self, event: str, **fields):
        rec = {"t": int(_time.time()), "event": event, **fields}
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def read(self, since: int, until: int | None = None) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r["t"] >= since and (until is None or r["t"] < until):
                    out.append(r)
        return out

    def today(self, symbol: str | None = None) -> dict:
        """IST-day counters derived from the journal (never from process uptime)."""
        start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        recs = [r for r in self.read(int(start.timestamp())) if symbol is None or r.get("symbol") == symbol]
        return {"signals": sum(r["event"] == "signal" for r in recs),
                "secured": sum(float(r.get("step", 0)) for r in recs if r["event"] == "secured"),
                "errors": sum(r["event"] == "error" for r in recs),
                "closed": sum(r["event"] == "exit" for r in recs)}

    # ---- guardian state: {symbol: {ticket: {...}}}
    def _load_all(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def load_state(self, symbol: str) -> dict:
        with _state_lock:
            return {int(k): v for k, v in self._load_all().get(symbol, {}).items()}

    def save_state(self, symbol: str, state: dict):
        with _state_lock:
            allst = self._load_all()
            allst[symbol] = {str(k): v for k, v in state.items()}
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(allst, f)
            os.replace(tmp, self.state_path)
