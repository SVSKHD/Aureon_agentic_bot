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

    def decision(self, event: str, *, actor: str = "human", rationale: str = "", p_win: float | None = None, **fields):
        """v2.4.0: log a decision-type event with actor/rationale/p_win filled."""
        self.log(event, actor=actor, rationale=rationale, p_win=p_win, **fields)

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

    # ---- v2.4.0 control mode: {symbol: {mode, since, by, until}} in guardian_state.json top-level "control"
    def load_control(self, symbol: str) -> dict:
        """Load control_mode for a symbol. Default MANUAL."""
        with _state_lock:
            ctrl = self._load_all().get("control", {}).get(symbol.upper(), {})
        return ctrl if ctrl.get("mode") else {"mode": "MANUAL", "since": None, "by": None, "until": None}

    def save_control(self, symbol: str, mode: str, by: str = "", until: float | None = None):
        """Save control_mode for a symbol."""
        import time as _t
        with _state_lock:
            allst = self._load_all()
            ctrl = allst.setdefault("control", {})
            ctrl[symbol.upper()] = {"mode": mode.upper(), "since": int(_t.time()), "by": str(by)[:64], "until": until}
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(allst, f)
            os.replace(tmp, self.state_path)

    def rebuild_state_for_ticket(self, ticket: int) -> tuple[dict | None, str]:
        """Reconstruct guardian state for a ticket from journal events. Returns (state_dict, source).
        source: "journal" if events found, "none" if not."""
        recs = self.read(0)
        ticket_recs = [r for r in recs if r.get("ticket") == ticket or r.get("ticket") == str(ticket)]
        if not ticket_recs:
            return None, "none"
        seen = next((r for r in ticket_recs if r["event"] == "position_seen"), None)
        if seen is None:
            return None, "none"
        secured_levels = [float(r.get("level", 0)) for r in ticket_recs if r["event"] == "secured"]
        peaks = [float(r.get("peak", 0)) for r in ticket_recs if r.get("peak") is not None]
        has_post_cross = any(r["event"] in ("cross_confirmed", "secured", "early_lock") for r in ticket_recs)
        st = {
            "symbol": seen.get("symbol"),
            "mode": seen.get("mode"),
            "direction": "long" if seen.get("side", "").upper() == "LONG" else "short",
            "entry": seen.get("entry"),
            "peak": max(peaks) if peaks else 0.0,
            "pre": not has_post_cross,
            "bars": 0,
            "news_done": any(r["event"] == "news_flat" for r in ticket_recs),
            "secured": max(secured_levels) if secured_levels else 0.0,
            "alert_id": seen.get("alert_id"),
        }
        if any(r.get("profile") == "follow" for r in ticket_recs):           # v2.13.0: the profile is sticky across restarts
            st["guardian_profile"] = "follow"; st["pre"] = False
        return st, "journal"
