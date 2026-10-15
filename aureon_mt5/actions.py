"""v2.11.0 — the trade actions shared by Discord (/take /skip /close /claude-read, the buttons) and the console / push layer.

One path per action, so a TAKE from the phone is refused for exactly the reasons a /take is (governor, regime gate, live
account) — plus the console's own: the ask must still be open and not stale (older than AUREON_ASK_TTL_MIN).
Every result is {"ok": bool, "text": str, "reasons": [str], ...}. Nothing here touches MetaTrader5 except through the
existing handlers (execute_entry, halt.close_all_positions) and Claude only through claude.read_now (shown only)."""
from __future__ import annotations

import threading
import time as _time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
ACTIONS = ("take", "skip", "close", "read")


def record_decision(journal, item: dict, decision: str, user):
    """The /skip journal line (and EXPIRED) — unchanged from the bot's _decide()."""
    m = item.get("meta", {})
    journal.log("decision", symbol=m.get("symbol"), mode=m.get("mode"), side=m.get("side"), signal_kind=m.get("kind"),
                price=m.get("price"), bar=m.get("bar"), decision=decision, by=str(user))
    item["decided"] = decision


def _res(ok: bool, text: str, reasons=None, **kw) -> dict:
    return {"ok": bool(ok), "text": text, "reasons": list(reasons or []), **kw}


class Actions:
    def __init__(self, cfg, agents: dict, journal, notify, *, claude=None, alerts=None, clock=_time.time, executor=None,
                 reader=None, closer=None):
        """executor(item, decision, user, lots) / reader(agent) / closer(symbols) are seams for tests; default to the real paths."""
        self.cfg, self.agents, self.journal, self.notify = cfg, agents, journal, notify
        self.claude, self.alerts, self.clock = claude, alerts, clock
        self._executor, self._reader, self._closer = executor, reader, closer
        self.last_reads: list[dict] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ asks
    def asks(self) -> dict:
        return getattr(self.notify, "open_asks", {}) or {}

    def latest_open(self, ask_key: str | None = None, symbol: str | None = None) -> dict | None:
        items = list(self.asks().values())
        if ask_key:
            return self.asks().get(ask_key)
        items = [i for i in items if not i.get("decided") and (not symbol or (i.get("meta") or {}).get("symbol") == symbol)]
        return items[-1] if items else None

    def age_s(self, item: dict) -> float:
        return max(0.0, self.clock() - float(item.get("t") or self.clock()))

    def refusal(self, item: dict | None, decision: str) -> list[str]:
        """Why a TAKE / SKIP on this ask is refused now ([] = go)."""
        if item is None:
            return ["no open trade question"]
        if item.get("decided"):
            return [f"already answered: {item['decided']}"]
        ttl = float(getattr(self.cfg, "ask_ttl_min", 20)) * 60
        if self.age_s(item) > ttl:
            return [f"stale: asked {self.age_s(item) / 60:.0f} min ago (asks expire after {ttl / 60:g} min)"]
        if decision == "TAKE":
            m = item.get("meta") or {}
            ag = self.agents.get(m.get("symbol"))
            tr = getattr(ag, "regime_tracker", None) if ag is not None else None
            if tr is not None and tr.blocks():
                return [f"regime {tr.state}"] + list(tr.reasons or [f"score {tr.score:.2f}"])
            if m.get("kind") == "alert" and not (m.get("suggested_side") or m.get("side")):
                return ["alert card without a suggested side — answer LONG / SHORT in Discord"]
        return []

    def _decision_field(self, item, line, user):
        self.notify.edit_card(item.get("key"), {"Decision": f"{line} · {user} · {datetime.now(IST):%H:%M} IST"[:1024]})

    def take(self, ask_key: str | None = None, lots: float | None = None, user="console") -> dict:
        from . import bot as B
        with self._lock:
            item = self.latest_open(ask_key)
            why = self.refusal(item, "TAKE")
            if why:
                self.journal.log("console_refused", action="take", key=(item or {}).get("key"), reasons=why, by=str(user))
                return _res(False, "refused — " + "; ".join(why), why, key=(item or {}).get("key"))
            m = item.get("meta") or {}
            if self._executor is not None:
                r = self._executor(item, "TAKE", user, lots)
                item["decided"] = "TAKE"
            elif m.get("kind") == "alert":
                side = (m.get("suggested_side") or m.get("side")).upper()
                r = B.alert_decision(self.cfg, self.agents, self.journal, self.alerts, item, side, user, self.notify, lots=lots)
                item["decided"] = side
            else:
                r = B.ask_decision(self.cfg, self.agents, self.journal, item, "TAKE", user, self.notify, lots=lots)
            ag = self.agents.get(m.get("symbol"))
            if ag is not None and hasattr(ag, "mark_ask_decided"):
                ag.mark_ask_decided(item.get("key", ""), "TAKE", placed=r.get("placed", False))
            self._decision_field(item, f"✅ **{r.get('line', 'TAKEN')}** — {r.get('text', '')}", user)
            return _res(r.get("ok", False), f"{r.get('line', '')} — {r.get('text', '')}", [] if r.get("ok") else [r.get("text", "")],
                        key=item.get("key"), placed=r.get("placed", False), ticket=r.get("ticket"))

    def skip(self, ask_key: str | None = None, user="console") -> dict:
        with self._lock:
            item = self.latest_open(ask_key)
            why = self.refusal(item, "SKIP")
            if why:
                return _res(False, "refused — " + "; ".join(why), why, key=(item or {}).get("key"))
            m = item.get("meta") or {}
            if m.get("kind") == "alert" and self.alerts is not None and self._executor is None:
                from . import bot as B
                B.alert_decision(self.cfg, self.agents, self.journal, self.alerts, item, "SKIP", user, self.notify)
                item["decided"] = "SKIP"
            else:
                record_decision(self.journal, item, "SKIP", user)
            ag = self.agents.get(m.get("symbol"))
            if ag is not None and hasattr(ag, "mark_ask_decided"):
                ag.mark_ask_decided(item.get("key", ""), "SKIP", placed=False)
            self._decision_field(item, "⏭ skipped", user)
            return _res(True, f"⏭ skipped {m.get('side') or ''} {m.get('symbol') or ''}".strip(), key=item.get("key"))

    # ------------------------------------------------------------------ close (the /close path)
    def close(self, symbol: str | None = None, user="console") -> dict:
        from . import halt as halt_mod
        sym = (symbol or self.cfg.symbols[0]).upper()
        ag = self.agents.get(sym)
        if ag is None:
            return _res(False, f"no agent for {sym}", [f"not running: {sym}"])
        pos = (getattr(ag, "snapshot", {}) or {}).get("positions") or []
        if not pos:
            return _res(False, f"{sym}: no open position", ["flat"])
        self.journal.decision("manual_close", actor="human", rationale="close via " + ("console" if str(user).startswith("console") else "discord"),
                              symbol=sym, by=str(user), tickets=[p["ticket"] for p in pos])
        if getattr(self.cfg, "dry", False) or not self.cfg.execution_enabled:
            lines = [f"would close #{p['ticket']} {p['direction'].upper()} {float(p['points']):+.1f}" for p in pos]
            return _res(False, f"{sym}: execution off — close it in MT5 · " + " · ".join(lines), ["execution off (AUREON_EXECUTION=0)"])
        for p in pos:                                               # the guardian must not ask 'why did you close?'
            getattr(ag, "closed_by_aureon", set()).add(p["ticket"])
        results = (self._closer or (lambda syms: halt_mod.close_all_positions(syms, comment="aureon-close")))([sym])
        ok = bool(results) and all(r["ok"] for r in results)
        text = " · ".join(f"#{r['ticket']} {'closed' if r['ok'] else 'FAILED'} {float(r['points']):+.1f}" for r in results) or "nothing closed"
        return _res(ok, f"{sym}: {text}", [] if ok else [text])

    # ------------------------------------------------------------------ read (claude.read_now, shown only)
    def read(self, symbol: str | None = None, user="console", wait: bool = False) -> dict:
        """Claude reads now; the verdict is pushed as a high event (and kept in last_reads). Non-blocking unless wait."""
        sym = (symbol or self.cfg.symbols[0]).upper()
        ag = self.agents.get(sym)
        if ag is None:
            return _res(False, f"no agent for {sym}", [f"not running: {sym}"])
        if self.claude is None and self._reader is None:
            return _res(False, "Claude add-on is off (AUREON_CLAUDE=off)", ["claude off"])

        def work():
            from . import bot as B
            try:
                r = (self._reader or self.claude.read_now)(ag)
                text = B.claude_read_text(sym, r)
            except Exception as e:
                r, text = {"error": repr(e)}, f"{sym} · Claude read failed: {e!r}"
            v = r.get("verdict") or {}
            rec = {"t": self.clock(), "symbol": sym, "decision": v.get("decision"), "confidence": v.get("confidence"),
                   "text": text, "by": str(user)}
            self.last_reads = (self.last_reads + [rec])[-10:]
            self.journal.log("console_read", symbol=sym, decision=v.get("decision"), by=str(user))
            push = getattr(self.notify, "push", None)
            if push is not None:
                push.notify(key=f"read:{sym}:{int(self.clock())}", title=f"{sym} · CLAUDE READ · {v.get('decision') or '—'}",
                            lines=text.splitlines()[:6], fields=None, png=r.get("png"), symbol=sym, event="READ_VERDICT",
                            klass="trade", points=None, meta={})
            return rec

        if wait:
            rec = work()
            return _res(rec.get("decision") is not None, rec["text"], read=rec)
        threading.Thread(target=work, name="aureon-console-read", daemon=True).start()
        return _res(True, f"{sym}: Claude is reading — the verdict arrives as a push", pending=True)

    # ------------------------------------------------------------------ dispatch
    def run(self, action: str, *, symbol: str | None = None, ask_key: str | None = None, lots: float | None = None,
            user="console") -> dict:
        a = str(action or "").lower()
        if a == "take":
            return self.take(ask_key, lots=lots, user=user)
        if a == "skip":
            return self.skip(ask_key, user=user)
        if a == "close":
            return self.close(symbol, user=user)
        if a == "read":
            return self.read(symbol, user=user)
        return _res(False, f"unknown action {action!r}", [f"one of {', '.join(ACTIONS)}"])
