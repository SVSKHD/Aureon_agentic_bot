"""Read-only deal history + open positions for the push bridge's `trades` section (docs/TRADE_ORIGINS.md).

Who made a trade:
    actor(magic, reason) of one deal
        "bot"      magic == the bot's magic (AUREON_MAGIC) or reason == DEAL_REASON_EXPERT
        "manual"   magic == 0 and reason is CLIENT / MOBILE / WEB
        "broker"   reason is SL / TP / SO (the server executed a stop, a target or a stop-out)
        "unknown"  anything else (the raw reason is kept)
    origin of a position = the actor of its opening deal (or of the position itself: positions_get has magic and
    reason); "broker" cannot open a position, so it becomes "unknown". Every deal of the position carries that origin.
    guardian_touched = origin "manual" and the bot changed it: a bot deal on it, a guardian journal event for its ticket,
    or guardian state for it right now. The origin stays "manual".

Only *_get / account_info calls, under broker's lock (tests/test_trade_history.py checks the AST). Server-time epochs
are converted to UTC with the measured server offset. Nothing here can place, change or cancel anything.
"""
from __future__ import annotations

import hashlib
import time as _time
from datetime import datetime, timezone

from . import broker

REASON_NAMES = {0: "client", 1: "mobile", 2: "web", 3: "expert", 4: "sl", 5: "tp", 6: "so", 7: "rollover",
                8: "vmargin", 9: "split"}
MANUAL_REASONS = frozenset({0, 1, 2})
BROKER_REASONS = frozenset({4, 5, 6})
EXPERT = 3
ENTRY_NAMES = {0: "in", 1: "out", 2: "inout", 3: "out_by"}
SIDES = {0: "buy", 1: "sell"}                       # DEAL_TYPE_BUY / SELL; balance, credit, ... are not trades
TRADE_MODES = {0: "demo", 1: "contest", 2: "real"}
MAX_DEALS = 5000
BACKFILL_DAYS = 7
BACKFILL_EVERY_S = 24 * 3600
OVERLAP_S = 3600                                    # incremental windows re-send the last hour (the server dedupes)


def actor(magic, reason, bot_magic: int) -> str:
    try:
        magic, reason = int(magic or 0), int(reason if reason is not None else -1)
    except (TypeError, ValueError):
        return "unknown"
    if magic == bot_magic or reason == EXPERT:
        return "bot"
    if magic == 0 and reason in MANUAL_REASONS:
        return "manual"
    if reason in BROKER_REASONS:
        return "broker"
    return "unknown"


def origin_of(a: str) -> str:
    return a if a in ("bot", "manual") else "unknown"


def acct_key(server, login) -> str:
    return hashlib.sha256(f"{server}|{login}".encode()).hexdigest()[:16]


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x != x else x


def _utc(epoch, off_h: float) -> int:
    return int(int(epoch or 0) - off_h * 3600)


def classify(deals: list[dict], positions: list[dict], *, bot_magic: int, touched=frozenset()) -> None:
    """In place: actor on every deal; origin + guardian_touched on every deal and position (pure; tests use it)."""
    touched = {int(t) for t in touched if str(t).lstrip("-").isdigit()}
    opening: dict[int, str] = {}
    bot_hands: set[int] = set()
    for d in deals:
        d["actor"] = actor(d.get("magic"), d.get("reason"), bot_magic)
        pid = int(d.get("position_id") or 0)
        if d.get("entry") in ("in", "inout") and pid not in opening:
            opening[pid] = d["actor"]
        if d["actor"] == "bot":
            bot_hands.add(pid)
    for p in positions:
        pid = int(p.get("position_id") or p.get("ticket") or 0)
        p["origin"] = origin_of(opening.get(pid) or actor(p.get("magic"), p.get("reason"), bot_magic))
        opening.setdefault(pid, p["origin"])
    for row in deals + positions:
        pid = int(row.get("position_id") or row.get("ticket") or 0)
        o = origin_of(opening.get(pid, row.get("actor", "unknown")))
        row["origin"] = o
        row["guardian_touched"] = o == "manual" and (pid in bot_hands or pid in touched)


class Collector:
    """Builds the `trades` section. The bridge commits the window on a 2xx (commit()), so a failed push is re-sent."""

    def __init__(self, *, clock=_time.time, mt5=None, lock=None):
        self.clock = clock
        self._mt5 = mt5                                   # tests inject a fake; normally broker.mt5()
        self._lock = lock if lock is not None else broker._lock
        self.deal_t: int | None = None                   # newest UTC deal time acked by the server
        self.backfill_at: float = 0.0                    # when the last 7-day window was acked
        self._pending: tuple | None = None

    def restore(self, cur: dict) -> None:
        self.deal_t = int(cur["deal_t"]) if cur.get("deal_t") is not None else None
        self.backfill_at = float(cur.get("backfill_at") or 0.0)

    def state(self) -> dict:
        return {"deal_t": self.deal_t, "backfill_at": self.backfill_at}

    def commit(self) -> None:
        if self._pending:
            kind, newest = self._pending
            if newest is not None:
                self.deal_t = max(newest, self.deal_t or 0)
            if kind == "backfill":
                self.backfill_at = self.clock()
            self._pending = None

    def collect(self, *, off_h: float, touched=frozenset()) -> dict | None:
        m = self._mt5 if self._mt5 is not None else broker.mt5()
        if not m:
            return None
        now = int(self.clock())
        backfill = self.deal_t is None or now - self.backfill_at >= BACKFILL_EVERY_S
        frm = now - BACKFILL_DAYS * 86400 if backfill else max(self.deal_t - OVERLAP_S, now - BACKFILL_DAYS * 86400)
        # history_* take datetimes the terminal reads as server time: ask a day wider on both sides, filter in UTC
        pad = int(abs(off_h) * 3600) + 86400
        d_from = datetime.fromtimestamp(frm - pad, tz=timezone.utc)
        d_to = datetime.fromtimestamp(now + pad, tz=timezone.utc)
        with self._lock:
            info = m.account_info()
            raw_deals = m.history_deals_get(d_from, d_to) or ()
            raw_orders = m.history_orders_get(d_from, d_to) or ()
            raw_pos = m.positions_get() or ()
        if info is None:
            return None
        orders = {int(o.ticket): o for o in raw_orders}
        bot_magic = broker.bot_magic()
        deals = []
        for d in raw_deals:
            side = SIDES.get(int(getattr(d, "type", -1)))
            t = _utc(d.time, off_h)
            if side is None or t < frm:
                continue
            o = orders.get(int(getattr(d, "order", 0) or 0))
            deals.append({
                "ticket": int(d.ticket), "order": int(getattr(d, "order", 0) or 0),
                "position_id": int(getattr(d, "position_id", 0) or 0), "symbol": str(d.symbol), "side": side,
                "entry": ENTRY_NAMES.get(int(getattr(d, "entry", -1)), str(getattr(d, "entry", ""))),
                "volume": _f(d.volume), "price": _f(d.price),
                "sl": _f(getattr(o, "sl", None)) if o is not None else None,
                "tp": _f(getattr(o, "tp", None)) if o is not None else None,
                "profit": _f(d.profit), "commission": _f(getattr(d, "commission", 0.0)), "swap": _f(getattr(d, "swap", 0.0)),
                "fee": _f(getattr(d, "fee", 0.0)), "time": t,
                "time_msc": (int(d.time_msc) - int(off_h * 3600_000)) if getattr(d, "time_msc", 0) else t * 1000,
                "magic": int(getattr(d, "magic", 0) or 0), "comment": str(getattr(d, "comment", "") or ""),
                "reason": int(getattr(d, "reason", -1)), "reason_name": REASON_NAMES.get(int(getattr(d, "reason", -1)), "other"),
            })
        deals.sort(key=lambda x: (x["time"], x["ticket"]))
        truncated = len(deals) > MAX_DEALS
        if truncated:                                     # oldest first; the rest goes next push (the cursor moves less)
            deals = deals[:MAX_DEALS]
        opened_sl: dict[int, float] = {}
        for d in deals:
            if d["entry"] in ("in", "inout") and d["sl"]:
                opened_sl.setdefault(d["position_id"], d["sl"])
        positions = []
        for p in raw_pos:
            side = SIDES.get(int(getattr(p, "type", -1)))
            pid = int(getattr(p, "identifier", 0) or p.ticket)
            positions.append({
                "ticket": int(p.ticket), "position_id": pid, "symbol": str(p.symbol), "side": side,
                "volume": _f(p.volume), "price_open": _f(p.price_open), "price_current": _f(getattr(p, "price_current", None)),
                "sl": _f(p.sl), "tp": _f(p.tp), "profit": _f(p.profit), "swap": _f(getattr(p, "swap", 0.0)),
                "time": _utc(p.time, off_h), "magic": int(getattr(p, "magic", 0) or 0),
                "comment": str(getattr(p, "comment", "") or ""), "reason": int(getattr(p, "reason", -1)),
                "reason_name": REASON_NAMES.get(int(getattr(p, "reason", -1)), "other"),
                "initial_sl": opened_sl.get(pid),
            })
        classify(deals, positions, bot_magic=bot_magic, touched=touched)
        kind = "backfill" if backfill else "incremental"
        newest = deals[-1]["time"] if deals else None
        self._pending = (kind, newest)
        return {
            "acct": acct_key(getattr(info, "server", ""), getattr(info, "login", "")),
            "server": str(getattr(info, "server", "") or ""),
            "trade_mode": TRADE_MODES.get(int(getattr(info, "trade_mode", -1)), "unknown"),
            "currency": str(getattr(info, "currency", "") or ""),
            "magic": bot_magic, "server_offset_h": off_h,
            "window": {"from": frm, "to": now, "kind": kind, "deal_count": len(deals), "truncated": truncated},
            "deals": deals, "positions": positions,
        }
