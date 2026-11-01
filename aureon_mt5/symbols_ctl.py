"""v2.18.1 — attach / detach symbols while live (`/attach`, `/detach`, `/symbols`).

    /attach XAGUSD,EURUSD   per symbol: rules_for / guardian_for (raises → refused with the reason), symbol_select + the
                            broker point, start the agent thread (it adopts an open position on that symbol on its first poll).
                            Card `ATTACHED · XAGUSD · rules: tp 200 / sl 100 pts · London`. Refused when already running or over
                            AUREON_MAX_SYMBOLS (8); queued (PENDING ATTACH) while that symbol's market is closed.
    /detach EURUSD [now]    no new entries at once. Flat → the thread stops, `DETACHED`. In a position (live, paper or a
                            pending order) → `DETACH PENDING · managing N position` and the guardian keeps running until the first
                            flat moment. `now` only when flat — nothing is ever closed. symbols[0] can't be detached.
    persistence             AUREON_SYMBOLS= in .env is rewritten (the only key the bot writes there), symbols[0] kept first.

Commands validate at once; the change itself is applied by tick() on the main loop, so no thread mutates the agents dict
while another iterates it.
"""
from __future__ import annotations

import os
import queue
import threading

from . import broker

SESSIONS = {(9.0, 15.0): "London", (9.0, 22.0): "London+NY", None: "all sessions"}


def parse(text: str) -> list[str]:
    return [x.strip().upper() for x in str(text or "").replace(" ", ",").split(",") if x.strip()]


def write_env_symbols(env_path: str, symbols: list[str]) -> bool:
    """Rewrite AUREON_SYMBOLS= only. False when there is no .env (nothing is created)."""
    from pathlib import Path
    from dotenv import set_key
    p = Path(env_path)
    if not p.is_file():
        return False
    set_key(str(p), "AUREON_SYMBOLS", ",".join(symbols), quote_mode="never")
    return True


def read_env_symbols(env_path: str) -> list[str]:
    from dotenv import dotenv_values
    return parse((dotenv_values(env_path) or {}).get("AUREON_SYMBOLS", ""))


class SymbolController:
    def __init__(self, cfg, agents: dict, make_agent, journal, notify, *, env_path: str, strategy=None,
                 market_open=None, flat=None):
        self.cfg, self.agents, self.make_agent, self.journal, self.notify = cfg, agents, make_agent, journal, notify
        self.env_path, self.S = env_path, strategy
        self.market_open = market_open or (lambda sym: broker.market_open(sym, cfg.server_utc_offset, cfg.dry))
        self.flat = flat or self._flat
        self.pending_attach: list[str] = []
        self.pending_detach: set[str] = set()
        self.requested: set[str] = set()                         # attach requests waiting for tick()
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.lock = threading.Lock()

    # -- helpers
    def _card(self, title, body=""):
        try:
            self.notify.card(title, body, footer="Aureon MT5 · symbols")
        except Exception:
            pass

    def rules_line(self, sym: str) -> str:
        if getattr(self.cfg, "mode", "") != "ema921":
            g = self.S.guardian_for(sym) if self.S is not None else None
            return (g.note or "guardian") if g else "no guardian profile"
        from .strategies.ema921 import journeys as J
        r = J.rules_for(sym)
        hours = J._allowed(r.no_entry_server_hours) if r.no_entry_server_hours else None
        key = (float(hours[0]), float(hours[1])) if hours else None
        sess = SESSIONS.get(key) or f"{hours[0]:g}–{hours[1]:g} server"
        return f"tp {r.tp_pts} / sl {r.sl_pts} pts · {sess}"

    def _check_rules(self, sym: str) -> str | None:
        """The reason a symbol has no rules (→ refused), else None."""
        try:
            if getattr(self.cfg, "mode", "") == "ema921":
                from .strategies.ema921 import journeys as J
                J.rules_for(sym); J.guardian_for(sym)
            elif self.S is not None:
                self.S.guardian_for(sym)
        except Exception as e:
            return str(e)
        return None

    def _flat(self, sym: str) -> tuple[bool, int]:
        ag = self.agents.get(sym)
        if ag is None:
            return True, 0
        n = len((getattr(ag, "snapshot", None) or {}).get("positions") or [])
        try:
            if getattr(ag, "_paper_open", None) and (self.cfg.paper or self.cfg.dry) and getattr(ag.S, "name", "") == "ema921":
                n += len(ag._paper_open())
            pend = (ag._state921().get("pending") if getattr(ag.S, "name", "") == "ema921" else None)
            n += 1 if pend and not pend.get("paper") else 0
        except Exception:
            pass
        return n == 0, n

    def _persist(self):
        order = [s for s in self.cfg.symbols]
        if write_env_symbols(self.env_path, order):
            self.journal.log("symbols_written", symbols=order)

    # -- /attach
    def attach(self, text: str, user="") -> str:
        out = []
        for sym in parse(text):
            with self.lock:
                running = sym in self.agents and self.agents[sym].is_alive()
                total = len(self.agents) + len(set(self.pending_attach) | self.requested)
            if running or sym in self.pending_attach or sym in self.requested:
                out.append(f"⛔ {sym}: already {'running' if running else 'queued'}"); continue
            if total >= int(getattr(self.cfg, "max_symbols", 8)):
                out.append(f"⛔ {sym}: AUREON_MAX_SYMBOLS={self.cfg.max_symbols} reached"); continue
            why = self._check_rules(sym)
            if why:
                self.journal.log("attach_refused", symbol=sym, why=why, by=str(user))
                out.append(f"⛔ {sym}: {why}"); continue
            if not self.cfg.dry:
                if not broker.select(sym):
                    out.append(f"⛔ {sym}: symbol_select failed — not on this account / Market Watch"); continue
            self.requested.add(sym); self.q.put(("attach", sym, str(user)))
            out.append(f"✅ {sym}: attach requested — the ATTACHED card follows")
        return "\n".join(out) or "⚠️ /attach XAGUSD,EURUSD"

    def _do_attach(self, sym: str, user: str):
        self.requested.discard(sym)
        if not self.market_open(sym):
            if sym not in self.pending_attach:
                self.pending_attach.append(sym)
                self.journal.log("attach_queued", symbol=sym, by=user)
                self._card(f"ATTACH QUEUED · {sym} · market closed", f"applied at the open · rules: {self.rules_line(sym)}")
            return
        if getattr(self.cfg, "mode", "") == "ema921" and not self.cfg.dry:
            from .strategies.ema921 import journeys as J
            J.set_point(sym, broker.point(sym))
        ag = self.make_agent(sym)
        with self.lock:
            self.agents[sym] = ag
            if sym not in self.cfg.symbols:
                self.cfg.symbols.append(sym)
            if sym in self.pending_attach:
                self.pending_attach.remove(sym)
        ag.start()
        self._persist()
        self.journal.log("symbol_attached", symbol=sym, by=user, rules=self.rules_line(sym))
        self._card(f"ATTACHED · {sym} · rules: {self.rules_line(sym)}",
                   "agent thread started · an open position on this symbol is adopted on its first poll"
                   + ("" if sym.upper() in tuple(getattr(self.cfg, "claude_symbols", ()) or ()) else
                      " · no advisor (not in AUREON_CLAUDE_SYMBOLS)"))

    # -- /detach
    def detach(self, sym: str, now: bool = False, user="") -> str:
        sym = sym.strip().upper()
        if self.cfg.symbols and sym == self.cfg.symbols[0]:
            return f"⛔ {sym} is symbols[0] — it drives market open / the server offset and can't be detached"
        if sym in self.pending_attach:
            self.pending_attach.remove(sym)
            return f"✅ {sym}: queued attach dropped"
        ag = self.agents.get(sym)
        if ag is None:
            return f"⛔ {sym} is not running"
        ok, n = self.flat(sym)
        if now and not ok:
            return f"⛔ {sym} has {n} open position(s) / order(s) — `now` only when flat (nothing is ever closed); /detach {sym} waits"
        ag.detaching = True                                          # no new entries from this moment
        self.q.put(("detach", sym, str(user)))
        return f"✅ {sym}: no new entries · " + ("detaching now" if ok else f"DETACH PENDING · managing {n} position(s)")

    def _do_detach(self, sym: str, user: str):
        ok, n = self.flat(sym)
        if not ok:
            if sym not in self.pending_detach:
                self.pending_detach.add(sym)
                self.journal.log("detach_pending", symbol=sym, open=n, by=user)
                self._card(f"DETACH PENDING · {sym} · managing {n} position{'s' if n != 1 else ''}",
                           "no new entries · the guardian keeps managing it · detached at the first flat moment (nothing is closed)")
            return
        with self.lock:
            ag = self.agents.pop(sym, None)
            if sym in self.cfg.symbols:
                self.cfg.symbols.remove(sym)
            self.pending_detach.discard(sym)
        if ag is not None:
            ag.stop(); ag.join(timeout=10)
        self._persist()
        self.journal.log("symbol_detached", symbol=sym, by=user)
        self._card(f"DETACHED · {sym}", "agent thread stopped · AUREON_SYMBOLS updated")

    # -- the main loop
    def tick(self):
        while True:
            try:
                kind, sym, user = self.q.get_nowait()
            except queue.Empty:
                break
            (self._do_attach if kind == "attach" else self._do_detach)(sym, user)
        for sym in list(self.pending_attach):
            if self.market_open(sym):
                self._do_attach(sym, "queued")
        for sym in list(self.pending_detach):
            self._do_detach(sym, "pending")

    # -- /symbols
    def state_of(self, sym: str) -> str:
        if sym in self.pending_attach:
            return "PENDING ATTACH"
        if sym in self.pending_detach or getattr(self.agents.get(sym), "detaching", False):
            return "DETACH PENDING"
        ag = self.agents.get(sym)
        return "RUNNING" if ag is not None and ag.is_alive() else "RESTARTING"

    def listing(self) -> str:
        syms = list(self.agents) + [s for s in self.pending_attach if s not in self.agents]
        lines = [f"**symbols** · {len(syms)}/{self.cfg.max_symbols} (AUREON_MAX_SYMBOLS) · first: {self.cfg.symbols[0] if self.cfg.symbols else '—'}"]
        for s in syms:
            try:
                rl = self.rules_line(s)
            except Exception as e:
                rl = f"rules: {e}"
            lines.append(f"`{s}` {self.state_of(s)} · {rl}")
        return "\n".join(lines)


CURRENT: dict = {}                                   # {"ctl": SymbolController} (set in main)
