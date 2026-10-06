"""One agent per symbol (own thread), driven by the selected Strategy.

detect  : closed M5 bars → P / confirmed cross → 🔫 Discord signal, published as `latest_signal` for the guardian
guardian: manages every position on the symbol (only when the strategy provides a guardian profile):
          protect (no SL → −pre_stop) · P phase: −pre_stop / timeout / separation abort · opposite P → close (flip, no auto-open)
          cross confirmed → SL to slow EMA ± buffer · SECURE at +secure_at (announced only when MT5 confirms)
          ride: +ride_step steps · close on fast-EMA turn · news safeguard
Invariants: live SL is the truth (state rebuilt every poll) · an SL is never loosened · failures are reported, never assumed.
"""
from __future__ import annotations

import threading
import time as _time
from datetime import datetime, timedelta, timezone

import numpy as np

from . import broker, telemetry
from .config import Config
from .journal import Journal
from .notify import Notifier
from .strategies import Strategy
from .common.source import Bars
from .common.timeutil import IST, bar_key, ist_str as ist


class SymbolAgent(threading.Thread):
    def __init__(self, symbol: str, cfg: Config, strategy: Strategy, notify: Notifier, journal: Journal, news: tuple):
        super().__init__(daemon=True, name=f"agent-{symbol}")
        self.symbol, self.cfg, self.S, self.notify, self.journal, self.news = symbol, cfg, strategy, notify, journal, news
        self.dry = cfg.dry
        g = strategy.guardian_for(symbol)
        if g is not None and not g.enabled and not (cfg.enable_silver and symbol.upper().startswith("XAG")):
            g = None
        self.g = g
        self.off = cfg.server_utc_offset
        self.state: dict[int, dict] = journal.load_state(symbol)
        self.latest_signal: dict | None = None
        self.last_bar: int | None = None
        self.health = {"status": "starting", "last_bar": None, "last_poll": None, "errors": 0, "managed": 0,
                       "close": None, "ema_fast": None, "ema_slow": None, "offset_h": self.off, "last_signal": None,
                       "last_broker_action": None, "guardian": "ON" if g else "OFF (no profile for this mode/symbol)"}
        self._stop = threading.Event()
        for t, st in list(self.state.items()):
            if st.get("mode") and st["mode"] != strategy.name:
                notify.raw(f"⚠️ **MODE MISMATCH** · {symbol} #{t} was managed by {st['mode']} · runtime mode {strategy.name} · "
                           f"guardian will only keep the existing SL (never loosened); no cross-strategy rules applied")
                st["frozen"] = True

    def stop(self): self._stop.set()

    def key(self, event: str, ident) -> str:
        """Symbol-aware dedupe key: mode:symbol:event:ticket|bar."""
        return f"{self.S.name}:{self.symbol}:{event}:{ident}"

    # ------------------------------------------------------------------ loop
    def run(self):
        if not self.dry:
            self.off = broker.server_offset_hours(self.symbol, self.cfg.server_utc_offset); self.health["offset_h"] = self.off
        announced_closed = False
        while not self._stop.is_set():
            try:
                if not broker.market_open(self.symbol, self.off, self.dry):
                    self.health["status"] = "market closed"
                    if not announced_closed:
                        announced_closed = True
                        self.notify.send(self.key("MARKET", f"closed-{datetime.now(IST):%Y-%m-%d}"), f"⏸ {self.symbol} market closed", ["agent sleeping"])
                    _time.sleep(30); continue
                if announced_closed:
                    announced_closed = False
                    self.notify.send(self.key("MARKET", f"open-{datetime.now(IST):%Y-%m-%d-%H}"), f"▶ {self.symbol} market open", ["Aureon resumed"])
                self.health["status"] = "running"
                m5 = broker.bars(self.symbol, self.cfg.bars, self.cfg.source, self.off)
                closed = m5.df[m5.df["time"] + 300 <= broker.now_server(self.off)].reset_index(drop=True)
                if len(closed) < 120:
                    _time.sleep(self.cfg.poll_seconds); continue
                bar_t = int(closed["time"].iloc[-1]); new_bar = bar_t != self.last_bar
                df = self.S.add_emas(closed)
                cf, cs = self.S.ema_cols
                ef = float(df[cf].iloc[-1]); es = float(df[cs].iloc[-1]); close = float(df["close"].iloc[-1])
                sgn = 1 if ef > es else -1
                self.health.update(last_poll=datetime.now(IST).strftime("%H:%M:%S"), close=close, ema_fast=ef, ema_slow=es)
                if new_bar:
                    self.last_bar = bar_t; self.health["last_bar"] = ist(bar_t, self.off)
                    self._detect(closed, df, bar_t, close, ef, es)
                if self.g is not None:
                    self._guard(new_bar, bar_t, df, close, ef, es, sgn)
            except Exception as e:
                self.health["errors"] += 1; self.health["status"] = f"error: {e}"
                telemetry.failure(self.notify, self.journal, title="AUREON AGENT ERROR", key=self.key("ERROR", f"loop-{type(e).__name__}"),
                                  mode=self.S.name, symbol=self.symbol, action="loop", exc=e)
            _time.sleep(self.cfg.poll_seconds)

    # ------------------------------------------------------------------ detection
    def _detect(self, closed, df, bar_t, close, ef, es):
        res = self.S.analyse({"M5": Bars(self.symbol, "M5", closed)}, self.symbol, self.off, self.news)["M5"]
        last = len(df) - 1
        for ev in res["events"]:
            if ev.index != last or not ev.label.startswith(("EB", "ES")):
                continue
            side = "LONG" if ev.direction == "bull" else "SHORT"; s = 1 if side == "LONG" else -1
            if "pre" in ev.label: kind = "P"
            elif "late" in ev.label: kind = "late"
            elif "pb" in ev.label: kind = "pullback"
            else: kind = "cross"
            self.latest_signal = {"bar_time": bar_t, "symbol": self.symbol, "strategy": self.S.name, "kind": kind, "direction": side, "consumed": False}
            self.health["last_signal"] = f"{side} {kind} @ {ist(bar_t, self.off)}"
            self.journal.log("signal", symbol=self.symbol, mode=self.S.name, side=side, signal_kind=kind, price=close, bar=bar_t)
            g = self.g
            lines = [f"**{side} {kind.upper()}** · {ist(bar_t, self.off)} IST · price **{close:.2f}** · EMA{self.S.fast} {ef:.2f} · EMA{self.S.slow} {es:.2f}"]
            if g:
                lines.append(f"stop {close - s * g.pre_stop:.2f} until the lines cross, then EMA {self.S.slow} · secure at {close + s * g.secure_at:.2f} (+{g.secure_at:g}) — place it, I manage it")
            else:
                lines.append("signal only — no guardian profile for this mode/symbol")
            self.notify.send(self.key(f"{kind.upper()}_{side}", bar_key(bar_t, self.off)), f"🔫 {self.symbol} · {self.S.display_name}", lines)

    # ------------------------------------------------------------------ guardian helpers
    def _locked(self, p: dict) -> float:
        if not p["sl"]:
            return 0.0
        v = (p["sl"] - p["price_open"]) if p["direction"] == "long" else (p["price_open"] - p["sl"])
        return max(0.0, v)

    def _move_sl(self, p, sl, action, quiet=False) -> bool:
        """MT5 SL modification through the one safe path. Returns True when the protection is in place
        (changed or already better). Never sends Discord itself — callers update state, journal, then notify."""
        if self.dry:
            self.health["last_broker_action"] = f"{action} DRY {datetime.now(IST):%H:%M:%S}"; return True
        r = broker.set_sl(p["ticket"], self.symbol, sl)
        self.health["last_broker_action"] = f"{action} {r.status} {datetime.now(IST):%H:%M:%S}"
        if r.status in ("SL_CHANGED", "ALREADY_BETTER"):
            return True
        telemetry.failure(self.notify, self.journal, title="AUREON GUARDIAN FAILURE", key=self.key("ERROR", f"sl-{p['ticket']}-{action}-{r.retcode}"),
                          mode=self.S.name, symbol=self.symbol, ticket=p["ticket"], action=action, requested=r.requested_sl or sl,
                          current=r.existing_sl, bid=r.bid, ask=r.ask, retcode=r.retcode, comment=r.comment, last_error=r.last_error)
        return False

    def _close(self, p, reason, title, key, st, journal_reason=None, bar_t=None):
        if bar_t is not None and st.get("closing_bar") == bar_t:
            return                                      # one close attempt per bar
        st["closing_bar"] = bar_t
        r = broker.close(p["ticket"], self.symbol, "aureon") if not self.dry else broker.BrokerResult(True, "DRY", changed=True)
        self.health["last_broker_action"] = f"close {r.status} {datetime.now(IST):%H:%M:%S}"
        self.journal.log("exit", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], points=p["points"],
                         reason=journal_reason or reason, auto=bool(r.ok))
        if r.ok:
            self.notify.send(key, title, [f"{reason} · result {p['points']:+.2f} · peak +{st['peak']:.2f}"])
        else:
            telemetry.failure(self.notify, self.journal, title="AUREON GUARDIAN FAILURE", key=self.key("ERROR", f"close-{p['ticket']}-{r.retcode}"),
                              mode=self.S.name, symbol=self.symbol, ticket=p["ticket"], action=f"CLOSE ({reason})", bid=r.bid, ask=r.ask,
                              retcode=r.retcode, comment=r.comment, last_error=r.last_error, retry="close by hand if it persists")

    # ------------------------------------------------------------------ guardian
    def _guard(self, new_bar, bar_t, df, close, ef, es, sgn):
        g = self.g; cf, cs = self.S.ema_cols
        gap = (df[cf] - df[cs]).to_numpy()
        pos = broker.positions(self.symbol)
        live = {p["ticket"] for p in pos}
        for t in list(self.state):
            if t not in live:
                st = self.state.pop(t)
                self.journal.log("closed", symbol=self.symbol, mode=self.S.name, ticket=t, direction=st.get("direction"),
                                 entry=st.get("entry"), secured=st.get("secured"), peak=st.get("peak"))
        self.health["managed"] = len(pos)
        sig = self.latest_signal
        for p in pos:
            s = 1 if p["direction"] == "long" else -1
            st = self.state.setdefault(p["ticket"], {"symbol": self.symbol, "mode": self.S.name, "direction": p["direction"],
                                                     "entry": p["price_open"], "peak": p["points"], "pre": sgn != s, "bars": 0,
                                                     "news_done": False, "secured": 0.0})
            st["peak"] = max(st["peak"], p["points"])
            if new_bar: st["bars"] += 1
            st["secured"] = max(st.get("secured", 0.0), self._locked(p))      # live SL is the truth; never lower
            tag = f"{self.symbol} #{p['ticket']} {p['direction'].upper()} @ {p['price_open']:.2f}"
            if st.get("frozen"):
                continue                                   # mode mismatch: keep SL, apply no strategy rules

            # 0) protect a naked position (never touch an existing manual SL here)
            if not p["sl"]:
                sl = p["price_open"] - s * g.pre_stop
                if self._move_sl(p, sl, "PROTECT"):
                    self.journal.log("protected", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], sl=sl)
                    self.notify.send(self.key("PROTECTED", p["ticket"]), f"🛡 PROTECTED — {tag}", [f"initial SL {sl:.2f} (−{g.pre_stop:g})"])

            # 1) opposite P → close (flip). The replacement trade is yours to place.
            if sig and not sig["consumed"] and sig["kind"] == "P" and sig["bar_time"] == bar_t and sig["direction"] != p["direction"].upper():
                sig["consumed"] = True
                self._close(p, f"opposite P ({sig['direction']}) — flip", f"🔁 FLIP CLOSE — {tag}", self.key("FLIP", f"{p['ticket']}-{bar_key(bar_t, self.off)}"), st, "p_flip", bar_t)
                continue

            # 2) P phase → cross confirmed: SL to the slow EMA ± buffer (tighten only)
            if st["pre"] and sgn == s:
                st["pre"] = False
                sl = es - s * g.ema_slow_sl_buffer
                ok = self._move_sl(p, sl, "CROSS SL")
                self.journal.log("cross_confirmed", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], sl=sl, ok=ok)
                self.notify.send(self.key("CROSS", p["ticket"]), f"✅ CROSS CONFIRMED — {tag}",
                                 [f"{self.S.fast} crossed {'above' if s > 0 else 'below'} {self.S.slow} · {p['points']:+.2f} · SL → EMA {self.S.slow} {sl:.2f}" + ("" if ok else " (SL move pending — see failure alert)")])
            elif not st["pre"] and st["secured"] < g.secure_level and new_bar:
                self._move_sl(p, es - s * g.ema_slow_sl_buffer, "FOLLOW EMA80", quiet=True)   # same safe path: retcode, telemetry, never-loosen

            # 3) SECURE — first priority; announced only when confirmed
            if st["secured"] < g.secure_level and p["points"] >= g.secure_at:
                sl = p["price_open"] + s * g.secure_level
                if self._move_sl(p, sl, f"SECURE +{g.secure_level:g}"):
                    st["secured"] = g.secure_level                                             # 1. state
                    self.journal.log("secured", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], level=g.secure_level, step=g.secure_level, price=p["current"])  # 2. journal
                    self.notify.send(self.key("SECURED", f"{p['ticket']}-{g.secure_level:g}"), f"🔒 SECURED +{g.secure_level:g} — {tag}",       # 3. discord
                                     [f"now {p['current']:.2f} ({p['points']:+.2f}) · SL → {sl:.2f} · riding"])
            # 4) ride
            elif st["secured"] >= g.secure_level:
                nxt = st["secured"] + g.ride_step
                if p["points"] >= nxt + g.ride_step * 0.4:
                    sl = p["price_open"] + s * nxt
                    if self._move_sl(p, sl, f"RIDE +{nxt:g}"):
                        st["secured"] = nxt
                        self.journal.log("secured", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], level=nxt, step=g.ride_step, price=p["current"])
                        self.notify.send(self.key("SECURED", f"{p['ticket']}-{nxt:g}"), f"🔒 SECURED +{nxt:g} — {tag}",
                                         [f"now {p['current']:.2f} ({p['points']:+.2f}) · SL → {sl:.2f} · peak +{st['peak']:.2f}"])

            # 5) exits on bar close
            if new_bar:
                against_f = (close < ef) if s > 0 else (close > ef)
                against_s = (close < es) if s > 0 else (close > es)
                separating = st["pre"] and len(gap) >= 3 and abs(gap[-1]) > abs(gap[-2]) > abs(gap[-3])
                reason = jr = None
                if st["secured"] >= g.secure_level and against_f:
                    reason, jr = f"bar closed through EMA {self.S.fast} ({ef:.2f}) — ride over", "ema_fast_turn"
                elif st["pre"] and p["points"] <= -g.pre_stop:
                    reason, jr = f"P failed: {p['points']:+.2f}, lines not crossed", "pre_stop"
                elif separating and st["bars"] >= 2:
                    reason, jr = f"P aborted: EMA {self.S.fast}/{self.S.slow} separating again before the cross", "pre_abort"
                elif st["pre"] and st["bars"] >= g.pre_timeout_bars and st["secured"] == 0:
                    reason, jr = f"P timeout: {st['bars']} bars without the cross", "pre_timeout"
                elif not st["pre"] and st["secured"] < g.secure_level and against_s:
                    reason, jr = f"bar closed through EMA {self.S.slow} ({es:.2f})", "ema_slow_stop"
                if reason:
                    self._close(p, reason, f"{'✅' if p['points'] > 0 else '🛑'} CLOSED — {tag}", self.key("CLOSED", f"{p['ticket']}-{bar_key(bar_t, self.off)}"), st, jr, bar_t)
                    continue

            # 6) news safeguard
            tutc = int(_time.time())
            if not st["news_done"] and any(r - g.news_flat_min * 60 <= tutc < r for r in self.news):
                st["news_done"] = True
                if p["points"] >= g.secure_at * 0.1:
                    self._close(p, "news in <15 min", f"📰 BANKED before news — {tag}", self.key("NEWS", p["ticket"]), st, "news_flat", bar_t)
                elif st["secured"] == 0:
                    if self._move_sl(p, p["price_open"] - s * g.pre_stop, "NEWS CAP"):
                        self.notify.send(self.key("NEWS", p["ticket"]), f"📰 NEWS <{g.news_flat_min} min — {tag}", [f"{p['points']:+.2f} · loss capped at −{g.pre_stop:g}"])
        if sig and not sig["consumed"] and sig["bar_time"] == bar_t:
            sig["consumed"] = True                       # nothing to flip; signal spent
        self.journal.save_state(self.symbol, self.state)
