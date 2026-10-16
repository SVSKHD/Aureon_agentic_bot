"""One agent per symbol (own thread), driven by the selected Strategy.

detect  : closed M5 bars → P / confirmed cross → 🔫 Discord signal, published as `latest_signal` for the guardian
guardian: manages every position on the symbol (only when the strategy provides a guardian profile):
          protect (no SL → −pre_stop) · P phase: −pre_stop / timeout / separation abort · opposite P → close (flip, no auto-open)
          cross confirmed → SL to slow EMA ± buffer · SECURE at +secure_at (announced only when MT5 confirms)
          ride: +ride_step steps · close on fast-EMA turn · news safeguard
Invariants: live SL is the truth (state rebuilt every poll) · an SL is never loosened · failures are reported, never assumed.
"""
from __future__ import annotations

import queue
import threading
import time as _time
from datetime import datetime, timedelta, timezone

import numpy as np

from . import broker, telemetry
from .config import Config
from .journal import Journal
from .notify import Notifier, signal_fields, position_fields, field, title as ctitle, footer as cfooter, EVENT_COLOURS
from .strategies import Strategy
from .common.source import Bars
from .common.timeutil import IST, bar_key, ist_str as ist
from .common.news import session_of
from .common.context_chart import context_chart
from .strategies.ema5080.trend import find_reentries, state_of
from . import alerts as alerts_mod
from .common.chop import chop_state as _chop_state
from . import leg_guardian as lg
from . import setup as setup_mod
import os
import numpy as np


_pending_path = lambda log_dir: os.path.join(log_dir, "pending.json")


def _load_pending(log_dir: str, symbol: str, ttl_min: int) -> dict | None:
    """v2.3.6: load pending TAKE from logs/pending.json for this symbol, if still within TTL."""
    try:
        import json as _json
        with open(_pending_path(log_dir), encoding="utf-8") as f:
            data = _json.load(f)
        pa = data.get(symbol.upper())
        if pa and _time.time() - pa.get("t", 0) <= ttl_min * 60:
            return pa
    except Exception:
        pass
    return None


def _save_pending(log_dir: str, symbol: str, pa: dict | None):
    """v2.3.6: persist pending TAKE to logs/pending.json atomically."""
    import json as _json
    path = _pending_path(log_dir)
    try:
        with open(path, encoding="utf-8") as f:
            data = _json.load(f)
    except Exception:
        data = {}
    if pa is not None:
        data[symbol.upper()] = pa
    else:
        data.pop(symbol.upper(), None)
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        _json.dump(data, f)
    os.replace(tmp, path)


def _load_open_asks(log_dir: str, symbol: str) -> list[dict]:
    """v2.3.9: load open asks from logs/pending.json for this symbol."""
    try:
        import json as _json
        with open(_pending_path(log_dir), encoding="utf-8") as f:
            data = _json.load(f)
        return data.get(f"{symbol.upper()}:asks", [])
    except Exception:
        return []


def _save_open_asks(log_dir: str, symbol: str, asks: list[dict]):
    """v2.3.9: persist open asks to logs/pending.json."""
    import json as _json
    path = _pending_path(log_dir)
    try:
        with open(path, encoding="utf-8") as f:
            data = _json.load(f)
    except Exception:
        data = {}
    key = f"{symbol.upper()}:asks"
    if asks:
        data[key] = asks
    else:
        data.pop(key, None)
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        _json.dump(data, f)
    os.replace(tmp, path)


class SymbolAgent(threading.Thread):
    def __init__(self, symbol: str, cfg: Config, strategy: Strategy, notify: Notifier, journal: Journal, news: tuple, claude=None, alerts=None,
                 codex=None):
        super().__init__(daemon=True, name=f"agent-{symbol}")
        # v1.10.0 Claude add-on: hooks only when the advisor advises this strategy (advisory/manage). None = v1.9.x behaviour.
        self.claude = claude if (claude is not None and claude.advises(strategy)) else None
        self.claude_any = claude              # v2.0.0: the advisor in ANY mode (review included) — the alert card's add-on line
        self.alerts = alerts if alerts is not None else alerts_mod.AlertStore(cfg.log_dir)   # v2.0.0 /alert store (shared by main)
        self.pending_alert: dict | None = _load_pending(cfg.log_dir, symbol, cfg.ask_ttl_min)   # v2.3.6: survives restart
        self.claude_inbox: "queue.Queue[dict]" = queue.Queue()
        # v2.3.0 Codex add-on: same hooks as Claude; its verdicts are journaled and shown, never applied
        self.codex = codex if (codex is not None and codex.advises(strategy)) else None
        self.codex_any = codex
        self.codex_inbox: "queue.Queue[dict]" = queue.Queue()
        self.symbol, self.cfg, self.S, self.notify, self.journal, self.news = symbol, cfg, strategy, notify, journal, news
        self.dry = cfg.dry
        g = strategy.guardian_for(symbol)
        if g is not None and not g.enabled and not (cfg.enable_silver and symbol.upper().startswith("XAG")):
            g = None
        self.g = g
        # v2.7.1: resolve guardian profile once at start (demo detection happens here)
        try:
            _demo = broker.is_demo() if not cfg.dry else None
        except Exception:
            _demo = None
        self._demo = _demo
        self._resolved_profile = cfg.resolved_guardian_profile(_demo, symbol)
        self.off = cfg.server_utc_offset
        self.state: dict[int, dict] = journal.load_state(symbol)
        for _st in self.state.values():                    # v2.12.0: a pending pre-exit ask never survives a restart
            if isinstance(_st, dict):
                _st.pop("pending_exit", None)
        from .regime import RegimeTracker                                # v2.10.0 regime: computed every closed bar
        self.regime_tracker = RegimeTracker(cfg, journal, symbol)
        self.regime: dict | None = None
        self.latest_signal: dict | None = None
        self.last_confirmed_cross: dict | None = None   # v2.0.0 ema2050: {"bar_time", "direction"} — the guardian's exit signal
        self.last_bar: int | None = None
        self.caught_up = False            # late-start catch-up card sent once
        self._resumed_posted = False      # v2.3.6: RESUMED card once per process start
        self.started_wall = _time.time()
        self.last_bar_wall: float | None = None   # wall clock of the last processed closed bar (heartbeat)
        self.closed_by_aureon: set = set()        # tickets Aureon closed (anything else that disappears was closed by you)
        self._evidence = {"at": 0.0, "graded": [], "busy": False}
        self.leg: dict | None = None      # the move since the last P / cross signal (progress updates)
        self.MOVE_STEP = 10.0 if not symbol.upper().startswith("XAG") else 0.15   # progress card every +step of move
        self.SNAPSHOT_BARS = 6            # open-position snapshot every 6 closed bars (30 min)
        self.trend_state: str | None = None
        self.last_trend_note: dict = {}   # state -> bar_time of last card (6-bar cooldown per state)
        self.last_session: str | None = None
        self.session_open: dict | None = None
        self.seen_re: set = set()
        self.chart_dir = os.path.join(cfg.log_dir, "charts")
        self.last_df = None               # last closed-bar frame with EMAs (for /chart)
        self.last_res = None              # last analysis result (per-bar states for /alert suggestions)
        self.chop: dict | None = None                  # v2.3.5: {"chop", "crosses", "day", "threshold"}; sticky per server day
        self.open_asks: list[dict] = _load_open_asks(cfg.log_dir, symbol)  # v2.3.9: pending signal asks awaiting position
        self.open_misses: list[dict] = []             # v2.3.9: misses awaiting /override
        self.health = {"status": "starting", "last_bar": None, "last_poll": None, "errors": 0, "managed": 0,
                       "close": None, "ema_fast": None, "ema_slow": None, "offset_h": self.off, "last_signal": None,
                       "last_broker_action": None, "guardian": "ON" if g else "OFF (no profile for this mode/symbol)",
                       "chop": None}
        # v1.9.8: cached status for slash commands (replaced atomically each poll; the bot never calls MT5 for status)
        self.snapshot: dict = {"at": None}
        self._pos_seen: list | None = None
        self._today_cache: tuple = (0.0, None)
        self._stop_evt = threading.Event()   # v2.0.1: was self._stop, which shadowed Thread._stop() and broke join()
        for t, st in list(self.state.items()):
            if st.get("mode") and st["mode"] != strategy.name:
                notify.card(ctitle(symbol, "MODE MISMATCH"), "Guardian keeps the existing SL (never loosened); no cross-strategy rules applied.",
                            fields=[field("Ticket", f"#{t}"), field("Managed by", st["mode"]), field("Runtime mode", strategy.name)],
                            color=EVENT_COLOURS["flip"], footer=cfooter(strategy.display_name))
                st["frozen"] = True

    def stop(self): self._stop_evt.set()

    def key(self, event: str, ident) -> str:
        """Symbol-aware dedupe key: mode:symbol:event:ticket|bar."""
        return f"{self.S.name}:{self.symbol}:{event}:{ident}"

    # ------------------------------------------------------------------ loop
    def run(self):
        if not self.dry:
            self.off = broker.server_offset_hours(self.symbol, self.cfg.server_utc_offset); self.health["offset_h"] = self.off
        announced_closed = False
        while not self._stop_evt.is_set():
            try:
                if not broker.market_open(self.symbol, self.off, self.dry):
                    self.health["status"] = "market closed"
                    if not announced_closed:
                        announced_closed = True
                        self.notify.send(self.key("MARKET", f"closed-{datetime.now(IST):%Y-%m-%d}"), ctitle(self.symbol, "MARKET CLOSED"), ["agent sleeping"], color=EVENT_COLOURS["market"], footer=cfooter(self.S.display_name))
                    self._refresh_snapshot(False)
                    _time.sleep(30); continue
                if announced_closed:
                    announced_closed = False
                    self.notify.send(self.key("MARKET", f"open-{datetime.now(IST):%Y-%m-%d-%H}"), ctitle(self.symbol, "MARKET OPEN"), ["Aureon resumed"], color=EVENT_COLOURS["secured"], footer=cfooter(self.S.display_name))
                self.health["status"] = "running"
                m5 = broker.bars(self.symbol, self.cfg.bars, self.cfg.source, self.off)
                closed = m5.df[m5.df["time"] + 300 <= broker.now_server(self.off)].reset_index(drop=True)
                if len(closed) < 120:
                    self._refresh_snapshot(True)
                    _time.sleep(self.cfg.poll_seconds); continue
                bar_t = int(closed["time"].iloc[-1]); new_bar = bar_t != self.last_bar
                df = self.S.add_emas(closed)
                if self.cfg.zl_context:                                        # v2.10.0: zero-lag context columns (no rule reads them)
                    from .common.zl import add_zl
                    df = add_zl(df, self.off)
                self.last_df = df
                if new_bar:
                    self._update_regime(df, bar_t)
                cf, cs = self.S.ema_cols
                ef = float(df[cf].iloc[-1]); es = float(df[cs].iloc[-1]); close = float(df["close"].iloc[-1])
                sgn = 1 if ef > es else -1
                self.health.update(last_poll=datetime.now(IST).strftime("%H:%M:%S"), close=close, ema_fast=ef, ema_slow=es)
                if new_bar:
                    self.last_bar = bar_t; self.health["last_bar"] = ist(bar_t, self.off)
                    self.last_bar_wall = _time.time()
                    self._update_chop(df, bar_t)
                    if not self.caught_up:
                        self.caught_up = True
                        try:
                            self._catch_up(closed, df, bar_t, close, ef, es)
                        except Exception as e:
                            telemetry.failure(self.notify, self.journal, title="AUREON CATCH-UP ERROR", key=self.key("ERROR", "catchup"),
                                              mode=self.S.name, symbol=self.symbol, action="catch-up", exc=e)
                    self._detect(closed, df, bar_t, close, ef, es)
                    if self.S.name == "ema5080":
                        self._trend_and_reentry(df, bar_t, close, ef, es)
                    self._session(df, bar_t, close, ef, es)
                self._track_leg(df, bar_t, new_bar, sgn)
                if self.g is not None:
                    self._guard(new_bar, bar_t, df, close, ef, es, sgn)
                self._refresh_snapshot(True)
                self._check_alerts(closed, df, bar_t)
                self._check_misses()
            except Exception as e:
                from .crashes import report as report_crash
                self.health["errors"] += 1; self.health["status"] = f"error: {e}"
                report_crash(self, e)
                return  # supervisor restarts this agent, unless crash guard paused it
            _time.sleep(self.cfg.poll_seconds)

    def advisors(self) -> list:
        """v2.3.0: the advisors attached to this agent (advisory / manage), Claude first."""
        return [a for a in (self.claude, self.codex) if a is not None]

    def chart_now(self) -> str | None:
        """Context chart of the latest closed bars (used by /chart)."""
        if self.last_df is None:
            return None
        return context_chart(self.last_df, self.symbol, self.S.display_name, self.S.fast, self.S.slow, self.S.ema_cols, self.off,
                             self.chart_dir, bars=120, title_extra="on request")

    # ------------------------------------------------------------------ v1.9.8 cached status snapshot (read by slash commands)
    def _refresh_snapshot(self, market_open: bool):
        """Runs on the agent thread after each poll. Never raises; slash commands read `self.snapshot` only."""
        try:
            now = _time.time()
            pos = self._pos_seen if (self.g is not None and market_open and self._pos_seen is not None) else broker.positions(self.symbol)
            self._pos_seen = None
            if self._today_cache[1] is None or now - self._today_cache[0] >= 60:
                self._today_cache = (now, self.journal.today(self.symbol))
            self.snapshot = {
                "at": now, "market_open": bool(market_open), "connected": broker.connected() or self.dry,
                "mt5": "CONNECTED" if broker.connected() else ("DRY" if self.dry else "DISCONNECTED"),
                "tick_age": broker.tick_age(self.symbol, self.off),
                "positions": [dict(p) for p in (pos or [])],
                "state": {t: dict(st) for t, st in self.state.items()},
                "today": dict(self._today_cache[1]),
                "health": dict(self.health),
            }
        except Exception as e:                       # a snapshot failure must never stop the agent
            telemetry.info(f"snapshot refresh failed {self.symbol}: {e!r}")

    # ------------------------------------------------------------------ late-start catch-up (once)
    def _catch_up(self, closed, df, bar_t, close, ef, es):
        """We may have started mid-day. 1) say where the lines are RIGHT NOW (pre-cross forming / cross confirmed / nothing),
        2) then list what already happened today: every 50/80 cross and P since 00:00 IST, with time and session."""
        off = self.off
        ist_midnight = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_srv = int(ist_midnight.timestamp() + off * 3600)
        cf, cs = self.S.ema_cols
        t = df["time"].to_numpy(); c = df["close"].to_numpy(); e_f = df[cf].to_numpy(); e_s = df[cs].to_numpy()
        sign = np.sign(e_f - e_s)
        res = self.S.analyse({"M5": Bars(self.symbol, "M5", closed)}, self.symbol, off, self.news)["M5"]
        pres = {p.index: p for p in res.get("pre", [])}
        sess = lambda ts: {"asia": "Asia", "london": "London", "ny": "New York", "off": "off-hours"}.get(session_of(int(ts), off), "—")
        start = int(np.searchsorted(t, day_start_srv))
        n = len(df) - 1

        # ---- 1) current state at start-up
        order = f"EMA {self.S.fast} {'above' if sign[n] > 0 else 'below'} EMA {self.S.slow} (gap {e_f[n] - e_s[n]:+.2f})"
        last_cross = next((i for i in range(n, max(start, 1) - 1, -1) if sign[i] != 0 and sign[i - 1] != 0 and sign[i] != sign[i - 1]), None)
        last_p = next((i for i in range(n, max(start, 1) - 1, -1) if i in pres), None)
        if last_p is not None and (last_cross is None or last_p > last_cross) and n - last_p <= 12:
            state = f"**PRE-CROSS forming** — P {'LONG' if pres[last_p].label == 'PB' else 'SHORT'} at {ist(int(t[last_p]), off)} IST, {n - last_p} bars ago, lines not crossed yet"
            colour = EVENT_COLOURS["signal_long" if pres[last_p].label == "PB" else "signal_short"]
        elif last_cross is not None and n - last_cross <= 24:
            side = "LONG" if sign[last_cross] > 0 else "SHORT"
            state = f"**CROSS confirmed {n - last_cross} bars ago** — {side} at {ist(int(t[last_cross]), off)} IST @ {c[last_cross]:.2f} · {sess(t[last_cross])}"
            colour = EVENT_COLOURS["cross"]
        else:
            state = "no setup right now — waiting for the next approach"; colour = EVENT_COLOURS["info"]
        png = context_chart(df, self.symbol, self.S.display_name, self.S.fast, self.S.slow, self.S.ema_cols, off, self.chart_dir,
                            marker=({"index": last_p, "side": "LONG" if pres[last_p].label == "PB" else "SHORT", "label": "P"} if (last_p is not None and (last_cross is None or last_p > last_cross)) else
                                    ({"index": last_cross, "side": "LONG" if sign[last_cross] > 0 else "SHORT", "label": "CROSS"} if last_cross is not None else None)),
                            bars=160, title_extra="start-up")
        self.notify.send(self.key("STARTUP", bar_key(bar_t, off)), ctitle(self.symbol, "STARTED · WHERE WE ARE"), [state], png,
                         fields=[field("Started", f"{datetime.now(IST):%d %b %H:%M} IST"), field("Session", sess(bar_t)),
                                 field("Last bar", f"{ist(bar_t, off)} IST"), field("Price", f"**{close:.2f}**"),
                                 field(f"EMA {self.S.fast}", f"{ef:.2f}"), field(f"EMA {self.S.slow}", f"{es:.2f}"),
                                 field("Lines", order, inline=False)],
                         color=colour, footer=cfooter(self.S.display_name, "late-start catch-up"))

        # ---- 2) what already happened: the leg we OPENED the day in + every leg since 00:00 IST, with how far each moved
        cross_idx = [i for i in range(1, n + 1) if sign[i] != 0 and sign[i - 1] != 0 and sign[i] != sign[i - 1]]
        before = [i for i in cross_idx if i < start]
        legs_from = ([before[-1]] if before else []) + [i for i in cross_idx if i >= start]
        h_ = df["high"].to_numpy(); l_ = df["low"].to_numpy()
        legs = []
        for k, ci in enumerate(legs_from):
            nxt = next((j for j in cross_idx if j > ci), n)
            seg = slice(ci, nxt + 1 if nxt < n else n + 1)
            long = sign[ci] > 0
            if long:
                pk = ci + int(np.argmax(h_[seg])); move = float(h_[pk] - c[ci]); adverse = float(c[ci] - l_[seg].min())
            else:
                pk = ci + int(np.argmin(l_[seg])); move = float(c[ci] - l_[pk]); adverse = float(h_[seg].max() - c[ci])
            legs.append({"start": ci, "end": min(nxt, n), "side": "LONG" if long else "SHORT", "move": move, "adverse": adverse,
                         "peak_idx": pk, "open": nxt >= n, "label": "CROSS", "before_day": ci < start})
        ps_today = [(i, "LONG" if pres[i].label == "PB" else "SHORT") for i in sorted(pres) if i >= start]
        if not legs and not ps_today:
            return
        fields = []
        for lg in legs[-8:]:
            ci, pk = lg["start"], lg["peak_idx"]
            long = lg["side"] == "LONG"
            till_px = float(h_[pk] if long else l_[pk])
            trend = f"UP ▲ (EMA {self.S.fast} crossed above {self.S.slow})" if long else f"DOWN ▼ (EMA {self.S.fast} crossed below {self.S.slow})"
            head = f"{'🟢' if long else '🔴'} {ist(int(t[ci]), off)} IST · {sess(t[ci])}" + (" · before day start" if lg["before_day"] else "")
            status = "still running" if lg["open"] else f"leg ended {ist(int(t[lg['end']]), off)[-5:]} IST"
            fields.append(field(head,
                                f"Trend **{trend}**\n"
                                f"Cross at **{c[ci]:.2f}** → moved till **{till_px:.2f}** at {ist(int(t[pk]), off)[-5:]} IST\n"
                                f"Move **{lg['move']:+.1f}** · against −{lg['adverse']:.1f} · {status}", inline=False))
        if ps_today:
            fields.append(field("P signals today", " · ".join(f"{ist(int(t[i]), off)[-5:]} {sd}" for i, sd in ps_today[-8:]), inline=False))
        total = sum(lg["move"] for lg in legs if not lg["before_day"])
        for lg in legs:
            long = lg["side"] == "LONG"; pk = lg["peak_idx"]
            lg["label"] = f"{c[lg['start']]:.1f} → {float(h_[pk] if long else l_[pk]):.1f}"
        png2 = context_chart(df, self.symbol, self.S.display_name, self.S.fast, self.S.slow, self.S.ema_cols, off, self.chart_dir,
                             bars=max(160, n - (legs[0]["start"] if legs else n) + 20), title_extra="legs since day start",
                             regions=legs, day_start_ts=day_start_srv)
        self.notify.send(self.key("STARTUP_EARLIER", bar_key(bar_t, off)), ctitle(self.symbol, f"EARLIER TODAY · {self.S.fast}/{self.S.slow} LEGS"),
                         [f"{sum(not lg['before_day'] for lg in legs)} cross(es) since 00:00 IST · available move today **{total:+.1f}** · "
                          f"shaded on the chart: cross → peak of each leg"],
                         png2, fields=fields, color=EVENT_COLOURS["info"], footer=cfooter(self.S.display_name, "late-start catch-up"))

    # ------------------------------------------------------------------ AUREON-007 / 008 / 009 context on ask cards
    def _htf(self) -> str:
        """M15 / H1 50/80 state — information only, never a filter."""
        key = self.last_bar
        cache = getattr(self, "_htf_cache", None)
        if cache is not None and cache[0] == key:
            return cache[1]
        from .strategies.ema5080.trend import state_of as _st
        parts = []
        for tf in ("M15", "H1"):
            try:
                d = broker.bars_tf(self.symbol, tf, 300, self.cfg.source, self.off)
                d = self.S.add_emas(d.iloc[:-1] if len(d) > 1 else d)      # closed bars only
                cf, cs = self.S.ema_cols
                parts.append(f"{tf} {_st(float(d['close'].iloc[-1]), float(d[cf].iloc[-1]), float(d[cs].iloc[-1])).lower()}")
            except Exception:
                parts.append(f"{tf} —")
        txt = " · ".join(parts); self._htf_cache = (key, txt)
        return txt

    # ------------------------------------------------------------------ v2.10.0 regime gate
    def _update_regime(self, df, bar_t):
        """Every closed bar (any gate setting): state + journal on change. block: DEAD / NO-TRADE DAY cards. Never touches positions."""
        try:
            cf, cs = self.S.ema_cols
            prev = self.regime_tracker.state
            self.regime = self.regime_tracker.update(df, bar_t, cf, cs)
            self.health["regime"] = self.regime_tracker.line()
            if self.cfg.regime_gate != "block":
                return
            tr = self.regime_tracker
            if tr.state == "DEAD" and prev != "DEAD":
                self.notify.send(self.key("NO_TRADE_REGIME", bar_key(bar_t, self.off)), ctitle(self.symbol, "NO-TRADE REGIME"),
                                 [f"regime DEAD ({tr.score:.2f}): " + (", ".join(tr.reasons) or "no movement"),
                                  "entries closed until the regime turns CHOPPY → CLEAN · open trades unaffected"],
                                 color=EVENT_COLOURS["market"], footer=cfooter(self.S.display_name, "regime"))
            if tr.no_trade_day:
                self.notify.send(self.key("NO_TRADE_DAY", datetime.now(IST).strftime("%Y-%m-%d")), ctitle(self.symbol, "NO-TRADE DAY"),
                                 [f"CHOPPY/DEAD covered ≥ {self.cfg.no_trade_day_frac:.0%} of today's session — not a lock: "
                                  f"a CLEAN streak reopens entries as usual"], color=EVENT_COLOURS["market"],
                                 footer=cfooter(self.S.display_name, "regime"))
        except Exception as e:
            telemetry.info(f"regime update failed {self.symbol}: {e!r}")

    def regime_blocks(self) -> bool:
        return self.regime_tracker.blocks()

    def _entry_ask(self, key, title, lines, **kw) -> bool:
        """notify.ask for P / cross / RE / ENTER cards. show: + REGIME line. block in CHOPPY/DEAD: the signal is carded
        'CHOP · not tradeable' with no buttons, no tracked ask, no Claude entry read — returns False."""
        from . import netguard as _ng, server as _server
        if (_ng.degraded() or _server.network_degraded()) and not getattr(self.cfg, "network_degraded_entries", False):
            _ng.suppress_ask(self.journal, self.symbol, key, title)     # v2.11.2: journaled + console, never auto-taken
            return False
        tr = self.regime_tracker; gate = self.cfg.regime_gate
        if gate in ("show", "block") and tr.state:
            kw["fields"] = list(kw.get("fields") or []) + [field("Regime", tr.line(), inline=False)]
        if tr.blocks():
            self.journal.log("entry_blocked_regime", symbol=self.symbol, key=key, state=tr.state, score=tr.score, reasons=tr.reasons)
            self.notify.send(key, f"{title} · CHOP · not tradeable", [lines[0] if lines else "",
                             f"regime {tr.state}: {', '.join(tr.reasons) or 'score ' + format(tr.score, '.2f')} — no ask, no Claude entry read"],
                             kw.get("png"), fields=kw.get("fields"), color=EVENT_COLOURS["market"], footer=kw.get("footer"))
            return False
        self.notify.ask(key, title, lines, **kw)
        return True

    def _m15_state(self) -> str | None:
        """v2.8.0: raw M15 50/80 state on closed bars ("BULLISH · PULLBACK", …), cached per M5 bar. Any failure → None
        (None never counts as evidence or as a failed setup check)."""
        cache = getattr(self, "_m15_cache", None)
        if cache is not None and cache[0] == self.last_bar:
            return cache[1]
        state = None
        try:
            d = broker.bars_tf(self.symbol, "M15", 300, self.cfg.source, self.off)
            d = self.S.add_emas(d.iloc[:-1] if len(d) > 1 else d)      # closed bars only
            cf, cs = self.S.ema_cols
            state = state_of(float(d["close"].iloc[-1]), float(d[cf].iloc[-1]), float(d[cs].iloc[-1]))
        except Exception:
            state = None
        self._m15_cache = (self.last_bar, state)
        return state

    def _flip_evidence(self, p, df, close, ef, es, gap) -> list[str]:
        """v2.8.0: what on the closed P bar says the trade's structure is really broken (AUREON_FLIP_REQUIRE_STRUCTURE)."""
        s = 1 if p["direction"] == "long" else -1
        cf, cs = self.S.ema_cols
        ev = []
        if (close < es) if s > 0 else (close > es):
            ev.append(f"close through EMA{self.S.slow}")
        try:
            f = df[cf].to_numpy()
            slope = float(f[-1] - f[-4]) if len(f) >= 4 else 0.0
            if slope * s < 0:
                ev.append(f"EMA{self.S.fast} slope against")
        except Exception:
            pass
        try:
            m15 = self._m15_state()
        except Exception:
            m15 = None
        if m15 and m15.upper().startswith("BEAR" if s > 0 else "BULL"):
            ev.append("M15 against")
        rng = (df["high"] - df["low"]).to_numpy()
        avg = float(rng[-21:-1].mean()) if len(rng) > 21 else float(rng.mean()) if len(rng) else 0.0
        if len(gap) and avg > 0 and abs(float(gap[-1])) >= self.cfg.flip_gap_x_range * avg:
            ev.append(f"gap wide: {abs(float(gap[-1])):.1f} vs avg range {avg:.1f}")
        if getattr(self.cfg, "flip_use_zl", False):                    # v2.10.0: optional; only ever ADDS evidence
            from .common.zl import zl_cross_against
            if zl_cross_against(df, p["direction"]):
                ev.append("ZL cross against")
        return ev

    def _warnings(self, df) -> list[str]:
        w = []
        sp = broker.spread(self.symbol) if not self.dry else None
        lim = next((v for k, v in self.cfg.spread_warn.items() if self.symbol.upper().startswith(k)), None)
        if sp is not None and lim is not None and sp > lim:
            w.append(f"⚠️ wide spread {sp:.2f} (> {lim:g})")
        rng = (df["high"] - df["low"]).to_numpy()
        avg = float(rng[-21:-1].mean()) if len(rng) > 21 else 0.0
        if avg > 0 and rng[-1] >= self.cfg.vol_spike_mult * avg:
            w.append(f"⚠️ volatile bar {rng[-1] / avg:.1f}× average range")
        return w

    def _daily_limit(self) -> str | None:
        """v2.3.12: governor state (single source of truth). Replaces the old advisory-only text."""
        today = datetime.now(IST).date()
        cache = getattr(self, "_limit_cache", None)
        if cache is not None and cache[0] == (today, self.last_bar):
            return cache[1]
        msg = None
        try:
            from .governor import Governor
            gov = Governor(self.cfg)
            start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            deals = broker.closed_deals(None, start, datetime.now(timezone.utc) + timedelta(minutes=1))
            trades = len(deals); losses = sum(1 for d in deals if d["profit"] < 0)
            net = sum(d["profit"] for d in deals)
            acc = broker.account()
            equity = acc["balance"] if acc else 0.0
            ctx = {"trades_today": trades, "losses_today": losses, "realised_money_today": net, "equity": equity,
                   "ist_now": datetime.now(IST), "news_block": False, "open_positions": []}
            dec = gov.check(self.symbol, "LONG", 0.1, ctx)
            if not dec.allowed:
                msg = f"⛔ governor: {dec.rule} · {dec.detail}"
                self.notify.send(self.key("LIMIT", str(today)), ctitle(self.symbol, "GOVERNOR LIMIT"),
                                 [msg, "entries blocked by the governor"],
                                 color=EVENT_COLOURS.get("error", 0xED4245), footer=cfooter(self.S.display_name, "governor"))
        except Exception:
            msg = None
        self._limit_cache = ((today, self.last_bar), msg)
        return msg

    def _refresh_evidence(self):
        """Grade the last 28 days of signals in the background, at most once an hour (it fetches ~8k bars)."""
        ev = self._evidence
        if ev["busy"] or _time.time() - ev["at"] < 3600:
            return
        ev["busy"] = True
        def work():
            try:
                from .reports import scorecard
                graded, _ = scorecard(self.cfg, {self.symbol: self}, self.journal, int(_time.time() - 28 * 86400))
                ev["graded"] = graded; ev["at"] = _time.time()
            except Exception:
                ev["at"] = _time.time() - 3000          # retry in ~10 min
            finally:
                ev["busy"] = False
        import threading
        threading.Thread(target=work, daemon=True).start()

    def _evidence_field(self, kind: str, side: str, bar_t: int) -> list[dict]:
        """'RE · SHORT · London — last 4 weeks: 9 signals, 7 hit +10 (78%)'"""
        self._refresh_evidence()
        sess = session_of(bar_t, self.off)
        rows = [g for g in self._evidence["graded"] if g.kind == kind and g.grade != "OPEN"]
        same = [g for g in rows if g.session == sess]
        def fmt(xs, label):
            if not xs:
                return f"{label}: no history yet"
            w = sum(g.grade in ("WIN10", "WIN20") for g in xs); st = sum(g.grade == "STOP" for g in xs)
            return f"{label}: {len(xs)} signals · **{w} hit +10 ({round(100 * w / len(xs))}%)** · {st} stopped"
        names = {"asia": "Asia", "london": "London", "ny": "New York", "off": "off-hours"}
        return [field("Track record (4 weeks)", fmt(rows, f"{kind} all sessions") + "\n" + fmt(same, f"{kind} in {names.get(sess, sess)}"), inline=False)]

    # ------------------------------------------------------------------ v2.3.5 chop-day flag
    def _chop_day_journaled(self, day: str) -> bool:
        """v2.3.8: True if journal already has a chop_day event for this symbol and day (survives restart)."""
        try:
            recs = self.journal.read(int(_time.time()) - 2 * 86400)
            return any(r["event"] == "chop_day" and r.get("symbol") == self.symbol and r.get("day") == day for r in recs)
        except Exception:
            return False

    def _update_chop(self, df, bar_t):
        """Recompute chop state once per closed bar. Sticky: once chop=True for a server day, it stays True."""
        cf, cs = self.S.ema_cols
        cs_new = _chop_state(df, cf, cs, self.off, self.cfg.chop_crosses)
        prev = self.chop
        if prev is not None and prev.get("chop") and prev.get("day") == cs_new["day"]:
            cs_new["chop"] = True  # sticky for the day
            cs_new["crosses"] = max(cs_new["crosses"], prev["crosses"])
        was_chop = prev is not None and prev.get("chop") and prev.get("day") == cs_new["day"]
        if cs_new["chop"] and not was_chop and not self._chop_day_journaled(cs_new["day"]):
            self.journal.log("chop_day", symbol=self.symbol, mode=self.S.name, day=cs_new["day"],
                             crosses=cs_new["crosses"], bar=bar_t)
            self.notify.send(self.key("CHOP", cs_new["day"]), ctitle(self.symbol, "CHOP DAY"),
                             [f"{cs_new['crosses']} crosses before 11:00 server — ENTER cards today carry a warning and half size",
                              "deterministic · no AI"],
                             color=EVENT_COLOURS.get("info", 0xFEE75C),
                             footer=cfooter(self.S.display_name, "deterministic · no AI"))
        self.chop = cs_new
        self.health["chop"] = f"CHOP ({cs_new['crosses']} crosses <11:00)" if cs_new["chop"] else None

    def _is_chop(self) -> bool:
        return bool(self.chop and self.chop.get("chop"))

    def _chop_crosses(self) -> int:
        return self.chop["crosses"] if self.chop else 0

    def _spread_pts(self) -> float | None:
        """v2.3.14: current spread from broker tick (None in dry mode)."""
        if self.dry:
            return None
        try:
            tk = broker.tick(self.symbol)
            return round(tk["ask"] - tk["bid"], 5) if tk else None
        except Exception:
            return None

    # ------------------------------------------------------------------ v2.3.9 missed setup
    def track_ask(self, card_key: str, side: str, kind: str, price: float, bar: int, lots: float | None, sl: float | None):
        """Record that a signal ask card was posted. Called from _detect / _detect_2050.
        v2.4.2: if AGENT mode, schedule delayed autonomous entry."""
        ask = {"card_key": card_key, "side": side, "kind": kind, "price": price, "bar": bar,
               "lots": lots, "sl": sl, "posted_t": _time.time(), "decided": None, "placed": False}
        self.open_asks.append(ask)
        _save_open_asks(self.cfg.log_dir, self.symbol, self.open_asks)
        try:
            from .agent_executor import maybe_schedule
            maybe_schedule(self.cfg, {self.symbol: self}, self.journal, self.notify, self, dict(ask))
        except Exception as _e:
            pass  # MANUAL mode or import error — benign

    def mark_ask_decided(self, card_key: str, decision: str, placed: bool = False):
        """Mark an ask as answered (TAKE/SKIP/EXPIRED). Called from bot.py button handlers."""
        for a in self.open_asks:
            if a["card_key"] == card_key:
                a["decided"] = decision; a["placed"] = placed; break
        _save_open_asks(self.cfg.log_dir, self.symbol, self.open_asks)

    def _has_position_in_direction(self, side: str) -> bool:
        pos = broker.positions(self.symbol) if not self.dry else []
        direction = "long" if side == "LONG" else "short"
        return any(p["direction"] == direction for p in pos)

    def _check_misses(self):
        """Every poll: check if any tracked ask has expired its grace window without a position appearing."""
        if not self.open_asks:
            return
        now = _time.time()
        grace = self.cfg.missed_grace_min * 60
        remaining = []
        for a in self.open_asks:
            elapsed = now - a["posted_t"]
            if a.get("decided") == "SKIP" or a.get("placed"):
                continue  # answered SKIP or order placed → not missed
            if elapsed < grace:
                remaining.append(a)
                continue
            # Grace expired — check if a position appeared in that direction
            if self._has_position_in_direction(a["side"]):
                continue  # position exists → not missed
            # Check blackout: market closed / news / 23:00–05:30 IST
            ist_h = datetime.now(IST).hour + datetime.now(IST).minute / 60
            skip_session = getattr(self.cfg, "_test_skip_session", False)
            if not broker.market_open(self.symbol, self.off, self.dry) or (not skip_session and (ist_h >= 23 or ist_h < 5.5)):
                remaining.append(a)  # keep checking later
                continue
            # It's a miss
            reason = "TAKE pressed, no ticket" if a.get("decided") == "TAKE" else "no position"
            tk = None
            if not self.dry:
                try: tk = broker.tick(self.symbol)
                except Exception: pass
            now_px = tk["bid"] if (tk and a["side"] == "LONG") else (tk["ask"] if tk else a["price"])
            s = 1 if a["side"] == "LONG" else -1
            drift = round(s * (now_px - a["price"]), 2) if now_px else 0.0
            self.journal.log("setup_missed", symbol=self.symbol, mode=self.S.name, card_key=a["card_key"],
                             side=a["side"], kind=a["kind"], price=a["price"], now=now_px, drift=drift,
                             chop=self._is_chop(), half_size=bool(self.chop and self.chop.get("chop")) or self._half_size(),
                             reason=reason)
            miss = {**a, "missed_t": now, "now_price": now_px, "drift": drift, "reason": reason}
            self.open_misses.append(miss)
            self.notify.send(self.key("MISSED", a["card_key"]),
                             ctitle(self.symbol, f"SETUP MISSED · {a['side']}"),
                             [f"No position within {self.cfg.missed_grace_min} min — /override to let Aureon take it"],
                             fields=[field("Signal", f"{a['kind']} · {ist(a['bar'], self.off)} IST"),
                                     field("Card price", f"{a['price']:.2f}"),
                                     field("Now", f"{now_px:.2f}" if now_px else "—"),
                                     field("Drift", f"{drift:+.2f} pts"),
                                     field("Suggested lots", f"{a.get('lots', '—')}"),
                                     field("SL", f"{a.get('sl', '—')}"),
                                     field("Minutes since signal", f"{elapsed / 60:.0f}")],
                             color=EVENT_COLOURS.get("info", 0xFEE75C),
                             footer=cfooter(self.S.display_name, "missed setup"))
        self.open_asks = remaining
        _save_open_asks(self.cfg.log_dir, self.symbol, self.open_asks)

    def check_override(self, miss_key: str | None = None) -> dict | None:
        """Check if the latest (or specified) miss is still overridable. Returns the miss dict or None with reason."""
        if not self.open_misses:
            return None
        miss = None
        if miss_key:
            miss = next((m for m in self.open_misses if m["card_key"] == miss_key), None)
        else:
            miss = self.open_misses[-1] if self.open_misses else None
        if not miss:
            return None
        now = _time.time()
        elapsed = now - miss["missed_t"]
        if elapsed > self.cfg.override_window_min * 60:
            miss["expired_reason"] = "time"; return None
        # Check drift
        tk = None
        if not self.dry:
            try: tk = broker.tick(self.symbol)
            except Exception: pass
        now_px = (tk["bid"] if miss["side"] == "LONG" else tk["ask"]) if tk else miss.get("now_price", miss["price"])
        s = 1 if miss["side"] == "LONG" else -1
        drift_against = round(-s * (now_px - miss["price"]), 2)  # positive = against the trade
        if drift_against > self.cfg.override_max_drift:
            miss["expired_reason"] = f"drift {drift_against:+.1f}"; return None
        # Check SL crossed
        if miss.get("sl") is not None:
            if (miss["side"] == "LONG" and now_px <= miss["sl"]) or (miss["side"] == "SHORT" and now_px >= miss["sl"]):
                miss["expired_reason"] = "SL crossed"; return None
        # Check blackout
        ist_h = datetime.now(IST).hour + datetime.now(IST).minute / 60
        if not broker.market_open(self.symbol, self.off, self.dry):
            miss["expired_reason"] = "market closed"; return None
        if not getattr(self.cfg, "_test_skip_session", False) and (ist_h >= 23 or ist_h < 5.5):
            miss["expired_reason"] = "blackout hours"; return None
        miss["now_price"] = now_px
        miss["drift_at_press"] = round(s * (now_px - miss["price"]), 2)
        return miss

    def consume_miss(self, card_key: str):
        """Remove a miss from the open list (after override or ignore)."""
        self.open_misses = [m for m in self.open_misses if m["card_key"] != card_key]

    def _streak_n(self) -> int:
        """Consecutive losing closed deals (last 3 days), cached per bar."""
        cache = getattr(self, "_streak_n_cache", None)
        if cache is not None and cache[0] == self.last_bar:
            return cache[1]
        n = 0
        try:
            deals = sorted(broker.closed_deals(None, datetime.now(timezone.utc) - timedelta(days=3), datetime.now(timezone.utc) + timedelta(minutes=1)),
                           key=lambda d: d["time"])
            for d in reversed(deals):
                if d["profit"] < 0: n += 1
                else: break
        except Exception:
            n = 0
        self._streak_n_cache = (self.last_bar, n)
        return n

    def _half_size(self) -> bool:
        return self.cfg.streak_half_after > 0 and self._streak_n() >= self.cfg.streak_half_after

    def _streak(self) -> str | None:
        """Consecutive losing closed trades (last 3 days, all symbols). Card once when it reaches 2."""
        cache = getattr(self, "_streak_cache", None)
        if cache is not None and cache[0] == self.last_bar:
            return cache[1]
        n = self._streak_n()
        msg = None
        if n >= 2:
            half = self._half_size()
            if half:
                msg = f"🔻 {n} losses in a row — **half size** applied to the next one"
            else:
                msg = f"🔻 {n} losses in a row — next one at half size?"
            try:
                deals = sorted(broker.closed_deals(None, datetime.now(timezone.utc) - timedelta(days=3), datetime.now(timezone.utc) + timedelta(minutes=1)),
                               key=lambda d: d["time"])
                self.notify.send(self.key("STREAK", f"{n}-{deals[-1]['time']}"), ctitle(self.symbol, f"{n} LOSSES IN A ROW"),
                                 [msg, "advisory only — signals continue"], color=EVENT_COLOURS["closed_loss"], footer=cfooter(self.S.display_name, "risk"))
            except Exception:
                pass
        self._streak_cache = (self.last_bar, msg)
        return msg

    def _context_fields(self, df) -> list[dict]:
        f = [field("HTF context", self._htf(), inline=False)]
        w = self._warnings(df)
        if w: f.append(field("Warnings", " · ".join(w), inline=False))
        lim = self._daily_limit()
        if lim: f.append(field("Risk", lim, inline=False))
        stk = self._streak()
        if stk: f.append(field("Streak", stk, inline=False))
        return f

    def _risk_lots(self, stop_distance: float) -> dict:
        """Lot sizing with optional half-size on a losing streak or chop day (halve once, not twice).
        Returns {"lots", "half", "half_reason", "raw"}."""
        streak_half = self._half_size()
        chop_half = self._is_chop()
        half = streak_half or chop_half
        risk = self.cfg.risk_pct / 2 if half else self.cfg.risk_pct
        try:
            r = broker.lot_for_risk(self.symbol, abs(stop_distance), risk) if not self.dry else None
        except Exception:
            r = None
        lots = min(r["lots"], self.cfg.max_lots) if r else None
        reason = ""
        if streak_half and chop_half:
            reason = f"chop day · {self._streak_n()} losses in a row"
        elif chop_half:
            reason = "chop day"
        elif streak_half:
            reason = f"{self._streak_n()} losses in a row"
        return {"lots": lots, "half": half, "half_reason": reason, "raw": r}

    def _suggested(self, stop_distance: float) -> dict:
        rl = self._risk_lots(stop_distance)
        return {"lots": rl["lots"]}

    def _lot_field(self, stop_distance: float):
        """AUREON-005: 'risk 1% → 0.08 lots' — display only, never sizes an order."""
        rl = self._risk_lots(stop_distance)
        r = rl["raw"]
        if not r:
            return []
        line = f"**{r['lots']:g} lots** = {r['risk_pct']:g}% risk ({r['risk_money']:,.0f} {r['currency']}) for a {r['stop']:.1f} stop"
        if rl["half"]:
            line += f" · **half size** ({rl['half_reason']})"
        return [field("Size", line, inline=False)]

    # ------------------------------------------------------------------ trend states + re-entries (ema5080)
    def _turning(self, df, bar_t, trend_side: str) -> str | None:
        """Is the move AGAINST the trend actually a turn forming (not a pullback)? Returns a reason or None.
        Same ingredients as the P rule: gap small vs. average range and shrinking — or an opposite P / cross on this bar or recently."""
        cf, cs = self.S.ema_cols
        gap = (df[cf] - df[cs]).abs().to_numpy()
        rng = float((df["high"] - df["low"]).to_numpy()[-21:-1].mean()) if len(df) > 21 else 0.0
        shrinking = len(gap) >= 4 and sum(gap[-k] < gap[-k - 1] for k in (1, 2, 3)) >= 2
        if rng > 0 and gap[-1] <= 2.0 * rng and shrinking:
            return f"EMA {self.S.fast}/{self.S.slow} gap {gap[-1]:.2f} and closing in"
        sig = self.latest_signal
        if sig and sig["direction"] != trend_side and sig.get("kind") in ("P", "cross") and bar_t - sig["bar_time"] <= 12 * 300:
            return f"opposite {sig['kind']} {sig['direction']} at {ist(sig['bar_time'], self.off)[-5:]}"
        return None

    def _trend_and_reentry(self, df, bar_t, close, ef, es):
        g = self.g
        st = state_of(close, ef, es)
        if st.endswith("PULLBACK"):
            trend_side = "SHORT" if st.startswith("BEARISH") else "LONG"
            why = self._turning(df, bar_t, trend_side)
            if why:
                st = st.replace("PULLBACK", "WEAKENING")
                self._weak_why = why
        prev = self.trend_state; self.trend_state = st
        self.health["trend"] = st
        if prev and st != prev and prev.split(" ·")[0] == st.split(" ·")[0]:        # sub-state change inside the same trend
            last = self.last_trend_note.get(st, -10**12)
            if bar_t - last >= 6 * 300:
                self.last_trend_note[st] = bar_t
                bear = st.startswith("BEARISH")
                if st.endswith("PULLBACK"):
                    text = (f"price back between EMA {self.S.fast} and EMA {self.S.slow}, lines still apart — "
                            f"**healthy pullback, watch for a re-entry {'SHORT' if bear else 'LONG'}**")
                    colour = EVENT_COLOURS["news"]
                elif st.endswith("WEAKENING"):
                    text = (f"price between EMA {self.S.fast} and EMA {self.S.slow} **but this is not a pullback** — {getattr(self, '_weak_why', '')}. "
                            f"**{'Bullish' if bear else 'Bearish'} turn forming — don't {'sell' if bear else 'buy'} this dip**")
                    colour = EVENT_COLOURS["signal_long" if bear else "signal_short"]
                elif st.endswith("CHALLENGED"):
                    text = (f"price closed {'above' if bear else 'below'} EMA {self.S.slow} — {'bearish' if bear else 'bullish'} trend at risk, "
                            f"**{'bullish' if bear else 'bearish'} turn forming** (needs the 50/80 cross)")
                    colour = EVENT_COLOURS["signal_long" if bear else "signal_short"]
                else:
                    text = f"price back {'below' if bear else 'above'} EMA {self.S.fast} — trend resumed"
                    colour = EVENT_COLOURS["signal_short" if bear else "signal_long"]
                self.notify.send(self.key("TREND", f"{st}-{bar_key(bar_t, self.off)}"), ctitle(self.symbol, f"TREND · {st}"), [text],
                                 fields=[field("Price", f"**{close:.2f}**"), field(f"EMA {self.S.fast}", f"{ef:.2f}"), field(f"EMA {self.S.slow}", f"{es:.2f}"),
                                         field("Was", prev), field("Bar", f"{ist(bar_t, self.off)} IST")],
                                 color=colour, footer=cfooter(self.S.display_name, "trend"))
        elif prev and prev.split(" ·")[0] != st.split(" ·")[0]:
            self.notify.send(self.key("TREND", f"flip-{bar_key(bar_t, self.off)}"), ctitle(self.symbol, f"TREND FLIPPED · {st.split(' ·')[0]}"),
                             [f"EMA {self.S.fast} crossed {'above' if st.startswith('BULLISH') else 'below'} EMA {self.S.slow} — new trend"],
                             fields=[field("Price", f"**{close:.2f}**"), field(f"EMA {self.S.fast}", f"{ef:.2f}"), field(f"EMA {self.S.slow}", f"{es:.2f}")],
                             color=EVENT_COLOURS["signal_long" if st.startswith("BULLISH") else "signal_short"], footer=cfooter(self.S.display_name, "trend"))
        # re-entry on THIS closed bar
        res = find_reentries(df, *self.S.ema_cols)
        if res and res[-1].index == len(df) - 1 and res[-1].time not in self.seen_re and self._turning(df, bar_t, res[-1].side):
            self.seen_re.add(res[-1].time)                     # suppressed: the 'pullback' is a turn forming
            self.journal.log("signal_suppressed", symbol=self.symbol, side=res[-1].side, signal_kind="reentry", bar=bar_t,
                             why=self._turning(df, bar_t, res[-1].side))
        if res and res[-1].index == len(df) - 1 and res[-1].time not in self.seen_re:
            r = res[-1]; self.seen_re.add(r.time)
            s_ = 1 if r.side == "LONG" else -1
            self.health["last_signal"] = f"{r.side} RE @ {ist(bar_t, self.off)}"
            _chop = self._is_chop(); _chop_n = self._chop_crosses(); _spread = self._spread_pts()
            self.journal.log("signal", symbol=self.symbol, mode=self.S.name, side=r.side, signal_kind="reentry", price=r.price, bar=bar_t,
                             chop=_chop, chop_crosses=_chop_n, spread_pts=_spread)
            self.latest_signal = {"bar_time": bar_t, "symbol": self.symbol, "strategy": self.S.name, "kind": "reentry", "direction": r.side, "consumed": False}
            if not self.leg or self.leg["side"] != r.side:
                self.leg = {"side": r.side, "kind": "reentry", "start_t": bar_t, "start_i_time": bar_t, "start_px": r.price,
                            "best": 0.0, "best_px": r.price, "best_t": bar_t, "against": 0.0, "steps_sent": 0}
            stop = r.pullback_extreme - s_ * 0.5                       # just beyond the pullback extreme
            re_fields = [field("Side", f"**{r.side}**"), field("Price", f"**{r.price:.2f}**"), field("Bar", f"{ist(bar_t, self.off)} IST"),
                         field(f"EMA {self.S.fast}", f"{r.ema50:.2f}"), field(f"EMA {self.S.slow}", f"{r.ema80:.2f}"),
                         field("Pullback reached", f"{r.pullback_extreme:.2f}"),
                         field("Stop", f"{stop:.2f} (beyond the pullback)", inline=False)]
            if g:
                re_fields.append(field("Secure", f"at {r.price + s_ * g.secure_at:.2f} (+{g.secure_at:g}) → then ride", inline=False))
            if _chop:
                re_fields.append(field("⚠️ Chop day", f"{_chop_n} crosses before 11:00 server — half size suggested", inline=False))
            png = context_chart(df, self.symbol, self.S.display_name, self.S.fast, self.S.slow, self.S.ema_cols, self.off, self.chart_dir,
                                bars=120, title_extra="re-entry", reentries=[{"index": r.index, "side": r.side}],
                                marker={"index": r.index, "side": r.side, "label": "RE", "stop": stop,
                                        "secure": (r.price + s_ * g.secure_at) if g else None})
            re_key = self.key(f"RE_{r.side}", bar_key(bar_t, self.off))
            asked = self._entry_ask(re_key, ctitle(self.symbol, "RE-ENTRY", r.side),
                            [f"pullback to EMA {self.S.fast} rejected — trend continues", f"**Trade {r.side} now?**"], png=png,
                            fields=re_fields + self._lot_field(r.price - stop) + self._evidence_field("reentry", r.side, bar_t) + self._context_fields(df),
                            color=EVENT_COLOURS["signal_long" if r.side == "LONG" else "signal_short"], footer=cfooter(self.S.display_name, "re-entry"),
                            meta={"symbol": self.symbol, "side": r.side, "kind": "reentry", "price": r.price, "bar": bar_t, "mode": self.S.name,
                                  "sl": stop, "chop": _chop, "chop_crosses": _chop_n, **self._suggested(r.price - stop)})
            if not asked:
                return                                                   # v2.10.0 regime gate (block): carded, not tradeable
            self.track_ask(re_key, r.side, "reentry", r.price, bar_t, self._suggested(r.price - stop).get("lots"), stop)
            for adv in self.advisors():
                adv.request_entry(self, "reentry", r.side, bar_t, df, card_key=re_key, png=png)

    # ------------------------------------------------------------------ session dividers as cards
    def _session(self, df, bar_t, close, ef, es):
        name = session_of(bar_t, self.off)
        o = float(df["open"].iloc[-1]); h = float(df["high"].iloc[-1]); l = float(df["low"].iloc[-1])
        so = self.session_open
        if so and so["name"] == name:
            so["high"] = max(so["high"], h); so["low"] = min(so["low"], l); so["last"] = close
        if name == self.last_session:
            return
        prev, self.last_session = self.last_session, name
        names = {"asia": "ASIA", "london": "LONDON", "ny": "NEW YORK", "off": "OFF-HOURS"}
        label = names.get(name, name.upper())
        fields = [field("Price", f"**{close:.2f}**"), field(f"EMA {self.S.fast}", f"{ef:.2f}"), field(f"EMA {self.S.slow}", f"{es:.2f}"),
                  field("Trend", self.trend_state or state_of(close, ef, es), inline=False)]
        if so and prev is not None:
            mv = so["last"] - so["open"]
            fields.append(field(f"{names.get(so['name'], so['name']).title()} session",
                                f"open {so['open']:.2f} → close {so['last']:.2f} (**{mv:+.1f}**) · range {so['high'] - so['low']:.1f} "
                                f"(H {so['high']:.2f} / L {so['low']:.2f})", inline=False))
        self.session_open = {"name": name, "t": bar_t, "open": o, "high": h, "low": l, "last": close}
        if prev is None:
            return                                  # first bar after start: the start-up cards already cover it
        png = context_chart(df, self.symbol, self.S.display_name, self.S.fast, self.S.slow, self.S.ema_cols, self.off, self.chart_dir,
                            bars=160, title_extra=f"{label.lower()} open")
        self.notify.send(self.key("SESSION", f"{name}-{bar_key(bar_t, self.off)}"), ctitle(self.symbol, f"{label} OPEN"),
                         [f"session divider · {ist(bar_t, self.off)} IST"], png, fields=fields,
                         color=EVENT_COLOURS["info"], footer=cfooter(self.S.display_name, "session"))

    # ------------------------------------------------------------------ move since the signal
    def _track_leg(self, df, bar_t, new_bar, sgn):
        lg = self.leg
        if not lg:
            return
        long = lg["side"] == "LONG"
        after = df[df["time"] > lg["start_t"]]
        if len(after):
            hi = float(after["high"].max()); lo = float(after["low"].min())
            best = (hi - lg["start_px"]) if long else (lg["start_px"] - lo)
            if best > lg["best"]:
                lg["best"] = best; lg["best_px"] = hi if long else lo
                row = after.loc[after["high"].idxmax()] if long else after.loc[after["low"].idxmin()]
                lg["best_t"] = int(row["time"])
            lg["against"] = max(lg["against"], (lg["start_px"] - lo) if long else (hi - lg["start_px"]))
        # milestone cards: +10, +20, +30 ... (each once)
        step = self.MOVE_STEP
        reached = int(lg["best"] // step)
        if reached > lg["steps_sent"]:
            lg["steps_sent"] = reached
            close = float(df["close"].iloc[-1])
            now_move = (close - lg["start_px"]) if long else (lg["start_px"] - close)
            self.notify.send(self.key("MOVE", f"{bar_key(lg['start_t'], self.off)}-{reached}"),
                             ctitle(self.symbol, f"MOVED +{reached * step:g}", lg["side"]),
                             [f"since the {lg['kind'].upper()} signal at {ist(lg['start_t'], self.off)} IST"],
                             fields=[field("Signal", f"{lg['kind'].upper()} **{lg['side']}**"), field("From", f"{lg['start_px']:.2f}"),
                                     field("Best", f"**{lg['best_px']:.2f}** ({lg['best']:+.1f})"), field("Now", f"{close:.2f} ({now_move:+.1f})"),
                                     field("Against", f"−{lg['against']:.1f}"),
                                     field("Running", f"{max(0, (bar_t - lg['start_t']) // 300)} bars · {ist(lg['best_t'], self.off)[-5:]} peak")],
                             color=EVENT_COLOURS["signal_long" if long else "signal_short"], footer=cfooter(self.S.display_name, "leg progress"))
        # the leg ends when the 50/80 order turns against it
        if new_bar and ((long and sgn < 0) or ((not long) and sgn > 0)) and lg["kind"] != "P":
            self._end_leg(df, bar_t, f"EMA {self.S.fast}/{self.S.slow} crossed back")
        elif new_bar and lg["kind"] == "P" and (bar_t - lg["start_t"]) // 300 > 48 and lg["best"] < step:
            self._end_leg(df, bar_t, "P never developed (4h)")

    def _end_leg(self, df, bar_t, why):
        lg = self.leg; self.leg = None
        if not lg:
            return
        long = lg["side"] == "LONG"; close = float(df["close"].iloc[-1])
        png = None
        try:
            t = df["time"].to_numpy()
            si = int(np.searchsorted(t, lg["start_t"])); pi = int(np.searchsorted(t, lg["best_t"])); ei = len(df) - 1
            png = context_chart(df, self.symbol, self.S.display_name, self.S.fast, self.S.slow, self.S.ema_cols, self.off, self.chart_dir,
                                bars=max(120, ei - si + 30), title_extra="leg summary",
                                regions=[{"start": si, "end": ei, "side": lg["side"], "move": lg["best"], "peak_idx": pi,
                                          "label": f"{lg['start_px']:.1f} → {lg['best_px']:.1f}"}])
        except Exception:
            png = None
        self.journal.log("leg", symbol=self.symbol, mode=self.S.name, side=lg["side"], signal_kind=lg["kind"], start=lg["start_px"], start_t=lg["start_t"],
                         best=lg["best"], best_px=lg["best_px"], against=lg["against"], why=why)
        self.notify.send(self.key("LEG_END", bar_key(lg["start_t"], self.off)), ctitle(self.symbol, "LEG ENDED", lg["side"]),
                         [f"{why} · the move from the {lg['kind'].upper()} signal is over"], png,
                         fields=[field("Signal", f"{lg['kind'].upper()} at {ist(lg['start_t'], self.off)} IST"),
                                 field("From", f"{lg['start_px']:.2f}"), field("Moved till", f"**{lg['best_px']:.2f}** at {ist(lg['best_t'], self.off)[-5:]}"),
                                 field("Best move", f"**{lg['best']:+.1f}**"), field("Against", f"−{lg['against']:.1f}"),
                                 field("Ended", f"{ist(bar_t, self.off)[-5:]} IST @ {close:.2f}")],
                         color=EVENT_COLOURS["info"], footer=cfooter(self.S.display_name, "leg summary"))

    # ------------------------------------------------------------------ position advisor (asks, never closes by itself)
    def _advise(self, p, st, bar_t, df, ef, es, g):
        """Question cards: 'should we close?' — the guardian's mechanical rules still decide automatic exits."""
        s = 1 if p["direction"] == "long" else -1
        close = float(df["close"].iloc[-1]); peak = st["peak"]; pts = p["points"]
        reasons = []
        if peak >= g.secure_at and pts <= peak * 0.6:
            reasons.append(("giveback", f"gave back {peak - pts:.1f} of a +{peak:.1f} peak (now {pts:+.1f})"))
        last3 = df["close"].to_numpy()[-3:]
        if len(last3) == 3 and pts > 0 and ((s > 0 and last3[2] < last3[1] < last3[0]) or (s < 0 and last3[2] > last3[1] > last3[0])):
            reasons.append(("momentum", "3 bars closing against the trade"))
        dist = (close - ef) * s
        if st.get("secured", 0) >= g.secure_level and 0 <= dist <= g.pre_stop * 0.25:
            reasons.append(("ema", f"price {dist:.1f} from EMA {self.S.fast} — the ride's exit line"))
        ist_h = float(datetime.fromtimestamp(bar_t - self.off * 3600, tz=IST).strftime("%H")) + float(datetime.fromtimestamp(bar_t - self.off * 3600, tz=IST).strftime("%M")) / 60
        if pts > 0 and 22.5 <= ist_h < 23.0:
            reasons.append(("dayend", "entry window closes 23:00 IST — overnight is thin"))
        tutc = int(_time.time())
        if pts > 0 and any(r - 45 * 60 <= tutc < r - g.news_flat_min * 60 for r in self.news):
            reasons.append(("news", "high-impact release in under 45 min"))
        for code, text in reasons:
            self.notify.send(self.key("ASK", f"{p['ticket']}-{code}-{bar_key(bar_t, self.off) if code in ('momentum', 'ema') else ''}"),
                             ctitle(self.symbol, "SHOULD WE CLOSE?", p["direction"]),
                             [f"**{text}**", "your call — I will keep managing it by the rules if you hold"],
                             fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"],
                                                    points=pts, sl=p["sl"] or None, secured=st.get("secured"), peak=peak,
                                                    extra={"If closed now": f"**{pts:+.2f}** banked"}),
                             color=EVENT_COLOURS["flip"], footer=cfooter(self.S.display_name, "advisor"), event="POSITION_ASK")
        return [code for code, _ in reasons]

    def _snapshot(self, p, st, bar_t):
        self.notify.send(self.key("SNAPSHOT", f"{p['ticket']}-{bar_key(bar_t, self.off)}"), ctitle(self.symbol, "POSITION UPDATE", p["direction"]),
                         [f"open {st['bars']} bars · phase {'pre-cross' if st['pre'] else 'post-cross'}"],
                         fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"],
                                                points=p["points"], sl=p["sl"] or None, secured=st.get("secured"), peak=st["peak"],
                                                extra={"Made so far": f"**{p['points']:+.2f}** now · locked +{st.get('secured', 0):g}"}),
                         color=EVENT_COLOURS["closed_win" if p["points"] > 0 else "closed_loss"], footer=cfooter(self.S.display_name, "every 30 min"))

    # ------------------------------------------------------------------ detection
    def _detect(self, closed, df, bar_t, close, ef, es):
        res = self.S.analyse({"M5": Bars(self.symbol, "M5", closed)}, self.symbol, self.off, self.news)["M5"]
        self.last_res = res
        last = len(df) - 1
        if self.S.name == "ema2050":
            return self._detect_2050(res, df, bar_t, close, ef, es)
        for ev in res["events"]:
            if ev.index != last or not ev.label.startswith(("EB", "ES")):
                continue
            side = "LONG" if ev.direction == "bull" else "SHORT"; s = 1 if side == "LONG" else -1
            if "pre" in ev.label: kind = "P"
            elif "late" in ev.label: kind = "late"
            elif "pb" in ev.label: kind = "pullback"
            else: kind = "cross"
            self.latest_signal = {"bar_time": bar_t, "symbol": self.symbol, "strategy": self.S.name, "kind": kind, "direction": side, "consumed": False}
            if kind == "P":
                self.regime_tracker.note_p(bar_t, side)                  # v2.10.0: failed-P count for the regime
            if self.leg and self.leg["side"] != side:
                self._end_leg(df, bar_t, f"opposite {kind} {side}")
            if not self.leg or self.leg["side"] != side:
                self.leg = {"side": side, "kind": kind, "start_t": bar_t, "start_i_time": bar_t, "start_px": close,
                            "best": 0.0, "best_px": close, "best_t": bar_t, "against": 0.0, "steps_sent": 0}
            self.health["last_signal"] = f"{side} {kind} @ {ist(bar_t, self.off)}"
            _chop = self._is_chop(); _chop_n = self._chop_crosses(); _spread = self._spread_pts()
            self.journal.log("signal", symbol=self.symbol, mode=self.S.name, side=side, signal_kind=kind, price=close, bar=bar_t,
                             chop=_chop, chop_crosses=_chop_n, spread_pts=_spread)
            g = self.g
            fields = signal_fields(side=side, kind={"P": "P · pre-cross", "cross": "50/80 cross confirmed", "late": "late entry", "pullback": "pullback entry"}.get(kind, kind),
                                   price=close, ema_fast=ef, ema_slow=es, fast=self.S.fast, slow=self.S.slow,
                                   stop=(close - s * g.pre_stop) if g else None, secure_at=(close + s * g.secure_at) if g else None,
                                   bar_ist=ist(bar_t, self.off))
            note = "place it — I manage it" if g else "signal only — no guardian profile for this mode/symbol"
            if _chop:
                fields.append(field("⚠️ Chop day", f"{_chop_n} crosses before 11:00 server — half size suggested", inline=False))
            png = context_chart(df, self.symbol, self.S.display_name, self.S.fast, self.S.slow, self.S.ema_cols, self.off, self.chart_dir,
                                marker={"index": last, "side": side, "label": kind.upper(),
                                        "stop": (close - s * g.pre_stop) if g else None, "secure": (close + s * g.secure_at) if g else None})
            fields = fields + (self._lot_field(g.pre_stop) if g else []) + self._evidence_field(kind, side, bar_t) + self._context_fields(df)
            ask_key = self.key(f"{kind.upper()}_{side}", bar_key(bar_t, self.off))
            asked = self._entry_ask(ask_key, ctitle(self.symbol, {"P": "P PRE-CROSS", "cross": "CONFIRMED CROSS", "late": "LATE ENTRY", "pullback": "PULLBACK ENTRY"}.get(kind, kind), side), [note, f"**Trade {side} now?**"], png=png,
                             meta={"symbol": self.symbol, "side": side, "kind": kind, "price": close, "bar": bar_t, "mode": self.S.name,
                                   "sl": (close - s * g.pre_stop) if g else None, "chop": _chop, "chop_crosses": _chop_n,
                                   **(self._suggested(g.pre_stop) if g else {})},
                             fields=fields, color=EVENT_COLOURS["signal_long" if side == "LONG" else "signal_short"],
                             footer=cfooter(self.S.display_name, f"bar {ist(bar_t, self.off)}"))
            if not asked:
                return                                                   # v2.10.0 regime gate (block): carded, not tradeable
            self.track_ask(ask_key, side, kind, close, bar_t, self._suggested(g.pre_stop).get("lots") if g else None,
                          (close - s * g.pre_stop) if g else None)
            if kind in ("P", "cross"):                                       # v1.10.0: enqueue only — never blocks the poll
                for adv in self.advisors():
                    adv.request_entry(self, kind, side, bar_t, df, card_key=ask_key, png=png)

    # ------------------------------------------------------------------ v2.0.0 price alerts (every poll, on the guardian's tick)
    def _tick(self, df) -> dict | None:
        if self.dry:
            px = float(df["close"].iloc[-1]); return {"bid": px, "ask": px, "time": broker.now_server(self.off)}
        return broker.tick(self.symbol)

    def _check_alerts(self, closed, df, bar_t):
        """Fires once per alert when the tick crosses the level from the side_hint side. Stale tick (> heartbeat_stale_min) or no tick:
        nothing fires — re-checked when fresh. Card = alerts.reached_card (EMA + trend + guardian + the mode's own SUGGEST line)."""
        store = self.alerts
        try:
            if store is None or not store.armed(self.symbol):
                return
            tk = self._tick(df)
            if not tk:
                return
            if broker.now_server(self.off) - tk["time"] > self.cfg.heartbeat_stale_min * 60:
                return
            for a in store.check(self.symbol, tk["bid"], tk["ask"], bar_t):
                self.journal.log("alert_fired", symbol=self.symbol, mode=self.S.name, alert_id=a["id"], price=a["price"], hit=a["hit_price"],
                                 side_hint=a["side_hint"], note=a.get("note"), bar=bar_t, created_t=a.get("created_t"))
                res = self.last_res
                if res is None:
                    res = self.S.analyse({"M5": Bars(self.symbol, "M5", closed)}, self.symbol, self.off, self.news)["M5"]; self.last_res = res
                card = alerts_mod.reached_card(self, a, a["hit_price"], tk["time"], bar_t, df, res)
                png = context_chart(df, self.symbol, self.S.display_name, self.S.fast, self.S.slow, self.S.ema_cols, self.off, self.chart_dir,
                                    bars=120, title_extra=f"alert {a['price']:.2f}")
                self.notify.ask(card["key"], card["title"], card["lines"], png=png, fields=card["fields"], color=card["color"],
                                footer=card["footer"], meta=card["meta"])
                self.health["last_signal"] = f"ALERT {a['price']:.2f} @ {ist(bar_t, self.off)}"
                cl = self.claude_any
                if card.get("claude_state") == "asking" and cl is not None and hasattr(cl, "request_alert"):
                    cl.request_alert(self, a, card, df, bar_t)                   # never before the agent block exists (it is on the card)
                cx = self.codex_any
                if card.get("codex_state") == "asking" and cx is not None and hasattr(cx, "request_alert"):
                    cx.request_alert(self, a, card, df, bar_t)
        except Exception as e:
            telemetry.failure(self.notify, self.journal, title="AUREON ALERT ERROR", key=self.key("ERROR", "alerts"), mode=self.S.name,
                              symbol=self.symbol, action="alert check", exc=e)

    def attach_alert(self, alert_id: str, side: str, card_key: str | None = None):
        """A LONG/SHORT button or TAKE: 'place it in MT5, I will manage it' — the next position seen carries the alert id."""
        self.pending_alert = {"alert_id": alert_id, "side": side.upper(), "t": _time.time(), "card_key": card_key}
        _save_pending(self.cfg.log_dir, self.symbol, self.pending_alert)

    def _save_state_now(self):
        """Immediate save after a critical state mutation (secured, early lock, ride step, etc.)."""
        self.journal.save_state(self.symbol, self.state)

    # ------------------------------------------------------------------ detection, EMA 20/50 (v2.0.0)
    def _detect_2050(self, res, df, bar_t, close, ef, es):
        """CROSS (unconfirmed) · CROSS CONFIRMED · MULTI CROSS (ignored) · ENTER (pullback | no-pullback) · NO ENTRY (reason).
        Every card: cross time, confirm time, pre-cross shoot, session, EMA 20/50, bars since cross. Same dedupe/order as 50/80."""
        last = len(df) - 1; off = self.off; g = self.g; bk = bar_key(bar_t, off)
        names = {"asia": "Asia", "london": "London", "ny": "New York", "off": "off-hours"}
        def info_fields(info: dict) -> list[dict]:
            return [field("Cross", f"{ist(info['cross_time'], off)} IST"),
                    field("Confirm", f"{ist(info['confirm_time'], off)} IST" if info.get("confirm_time") else "—"),
                    field("Pre-cross shoot", f"{info.get('shoot', 0.0):+.1f} pts"), field("Session", names.get(info.get("session", ""), "—")),
                    field(f"EMA {self.S.fast}", f"{info['ema20']:.2f}"), field(f"EMA {self.S.slow}", f"{info['ema50']:.2f}"),
                    field("Bars since cross", str(info.get("bars_since_cross", 0)))]
        for ev in res["events"]:
            if ev.index != last:
                continue
            side = "LONG" if ev.direction == "bull" else "SHORT"; s = 1 if side == "LONG" else -1
            info = dict(ev.info); colour = EVENT_COLOURS["signal_long" if side == "LONG" else "signal_short"]
            if ev.label == "CROSS":
                self.health["last_signal"] = f"{side} cross @ {ist(bar_t, off)}"
                self.journal.log("cross", symbol=self.symbol, mode=self.S.name, side=side, price=close, bar=bar_t, shoot=info.get("shoot"))
                self.notify.send(self.key(f"CROSS_{side}", bk), ctitle(self.symbol, f"CROSS {side} (unconfirmed)"),
                                 [f"EMA {self.S.fast} crossed {'above' if s > 0 else 'below'} EMA {self.S.slow} — not a trade yet",
                                  f"confirmed when the order holds 3 bars and the gap reaches 1.5 (within 18 bars); a flip before that = multi cross"],
                                 fields=info_fields(info) + [field("Price", f"**{close:.2f}**")], color=EVENT_COLOURS["cross"],
                                 footer=cfooter(self.S.display_name, f"bar {ist(bar_t, off)}"))
            elif ev.label == "CONFIRMED":
                self.last_confirmed_cross = {"bar_time": bar_t, "direction": side, "cross_time": info.get("cross_time")}
                self.journal.log("cross_confirmed", symbol=self.symbol, mode=self.S.name, side=side, price=close, bar=bar_t,
                                 cross_bar=info.get("cross_time"), shoot=info.get("shoot"))
                self.notify.send(self.key(f"CROSS_CONFIRMED_{side}", bk), ctitle(self.symbol, "CROSS CONFIRMED", side),
                                 [ev.reason, f"waiting for the pullback to EMA {self.S.fast} (±1.5) — up to 12 bars, then a fallback only within 5 of it"],
                                 fields=info_fields(info) + [field("Price", f"**{close:.2f}**")], color=EVENT_COLOURS["cross"],
                                 footer=cfooter(self.S.display_name, f"bar {ist(bar_t, off)}"))
            elif ev.label == "MULTI":
                self.journal.log("multi_cross", symbol=self.symbol, mode=self.S.name, side=side, bar=bar_t, cross_bar=info.get("cross_time"))
                self.notify.send(self.key("MULTI_CROSS", bk), ctitle(self.symbol, f"MULTI CROSS (ignored) · {side}"),
                                 [f"the {side.lower()} cross flipped back before it could confirm — no trade", ev.reason],
                                 fields=info_fields(info), color=EVENT_COLOURS["flip"], footer=cfooter(self.S.display_name, "multi cross"))
            elif ev.label == "WP":
                self.notify.send(self.key(f"NO_CONFIRM_{side}", bk), ctitle(self.symbol, f"CROSS {side} NOT CONFIRMED"),
                                 [ev.reason + " — no trade"], fields=info_fields(info), color=EVENT_COLOURS["info"],
                                 footer=cfooter(self.S.display_name, "no confirmation"))
            elif ev.label == "F":
                self.journal.log("no_entry", symbol=self.symbol, mode=self.S.name, side=side, bar=bar_t, why=ev.reason)
                self.notify.send(self.key(f"NO_ENTRY_{side}", bk), ctitle(self.symbol, f"NO ENTRY ({ev.reason.split(' (')[0]})", side),
                                 [f"**{ev.reason}**", "still watching the pullback window" if "position" in ev.reason else "this cross is skipped"],
                                 fields=info_fields(info) + [field("Price", f"**{close:.2f}**")], color=EVENT_COLOURS["info"],
                                 footer=cfooter(self.S.display_name, "no entry"))
            elif ev.label == "PRE":
                # v2.5.2: PRE-CROSS heads-up (notification only, no buttons, no trade)
                if self.cfg.pre_alert_2050:
                    gap_now = info.get("gap", 0); gap_atr = info.get("gap_atr", 0)
                    ist_h = datetime.now(IST).hour + datetime.now(IST).minute / 60
                    skip_session = getattr(self.cfg, "_test_skip_session", False)
                    blackout = "" if skip_session or (5.5 <= ist_h < 23) else " (no entries now)"
                    self.journal.log("pre_cross", symbol=self.symbol, mode=self.S.name, side=side, price=close, bar=bar_t,
                                     gap=gap_now, gap_atr=gap_atr, chop=self._is_chop())
                    self.health["last_pre"] = f"PRE {side} @ {ist(bar_t, off)}"
                    self.notify.send(self.key(f"PRE_{side}", bk), ctitle(self.symbol, f"PRE-CROSS forming · {side}"),
                                     [f"EMA {self.S.fast} is closing on EMA {self.S.slow} {'from above' if side == 'SHORT' else 'from below'}"
                                      f" — a {side} cross may form · not a trade{blackout}",
                                      f"entry still needs: cross → confirm (3 bars, gap 1.5) → pullback"],
                                     fields=[field("Gap now", f"{gap_now:+.2f} pts ({gap_atr:.1f}× ATR)"),
                                             field(f"EMA {self.S.fast}", f"{ef:.2f}"), field(f"EMA {self.S.slow}", f"{es:.2f}"),
                                             field("Price", f"**{close:.2f}**"), field("Session", names.get(session_of(bar_t, off), "—")),
                                             field("Chop", "yes" if self._is_chop() else "no")],
                                     color=EVENT_COLOURS.get("cross", 0xFEE75C),
                                     footer=cfooter(self.S.display_name, f"bar {ist(bar_t, off)} · PRE only"))
            elif ev.label in ("EB", "ES"):
                kind = "pullback" if ev.reason == "pullback" else "no-pullback"
                self.latest_signal = {"bar_time": bar_t, "symbol": self.symbol, "strategy": self.S.name, "kind": kind, "direction": side, "consumed": False}
                if self.leg and self.leg["side"] != side:
                    self._end_leg(df, bar_t, f"opposite entry {side}")
                if not self.leg or self.leg["side"] != side:
                    self.leg = {"side": side, "kind": kind, "start_t": bar_t, "start_i_time": bar_t, "start_px": close,
                                "best": 0.0, "best_px": close, "best_t": bar_t, "against": 0.0, "steps_sent": 0}
                self.health["last_signal"] = f"{side} enter ({kind}) @ {ist(bar_t, off)}"
                _chop = self._is_chop(); _chop_n = self._chop_crosses(); _spread = self._spread_pts()
                self.journal.log("signal", symbol=self.symbol, mode=self.S.name, side=side, signal_kind=kind, price=close, bar=bar_t,
                                 cross_bar=info.get("cross_time"), confirm_bar=info.get("confirm_time"), shoot=info.get("shoot"),
                                 chop=_chop, chop_crosses=_chop_n, spread_pts=_spread)
                stop = (close - s * g.pre_stop) if g else None
                fields = ([field("Side", f"**{side}**"), field("Setup", f"confirmed cross · {kind} entry"), field("Price", f"**{close:.2f}**")]
                          + info_fields(info)
                          + ([field("Stop", f"{stop:.2f} (−{g.pre_stop:g}) · +{g.early_at:g} seen → SL entry +{g.early_level:g}", inline=False),
                              field("Secure", f"at {close + s * g.secure_at:.2f} (+{g.secure_at:g}) → SL +{g.secure_level:g} · then ride +{g.ride_step:g} steps · "
                                              f"close on an EMA {self.S.fast} turn once secured · exit on the next confirmed cross", inline=False)] if g else []))
                note = "place it — I manage it" if g else "signal only — no guardian profile for this symbol"
                if _chop:
                    fields.append(field("⚠️ Chop day", f"{_chop_n} crosses before 11:00 server — half size suggested", inline=False))
                png = context_chart(df, self.symbol, self.S.display_name, self.S.fast, self.S.slow, self.S.ema_cols, off, self.chart_dir,
                                    marker={"index": last, "side": side, "label": "ENTER", "stop": stop,
                                            "secure": (close + s * g.secure_at) if g else None})
                fields = fields + (self._lot_field(g.pre_stop) if g else []) + self._context_fields(df)
                ask_key = self.key(f"ENTER_{side}", bk)
                asked = self._entry_ask(ask_key, ctitle(self.symbol, f"ENTER {side} ({kind})"), [note, f"**Trade {side} now?**"], png=png,
                                meta={"symbol": self.symbol, "side": side, "kind": kind, "price": close, "bar": bar_t, "mode": self.S.name,
                                      "sl": stop, "chop": _chop, "chop_crosses": _chop_n, **(self._suggested(g.pre_stop) if g else {})},
                                fields=fields, color=colour, footer=cfooter(self.S.display_name, f"bar {ist(bar_t, off)}"))
                if not asked:
                    return                                               # v2.10.0 regime gate (block): carded, not tradeable
                self.track_ask(ask_key, side, kind, close, bar_t, self._suggested(g.pre_stop).get("lots") if g else None, stop)
                for adv in self.advisors():                                    # the ENTER bar only — never raw CROSS / MULTI
                    adv.request_entry(self, "enter", side, bar_t, df, card_key=ask_key, png=png)

    # ------------------------------------------------------------------ guardian helpers
    def _locked(self, p: dict) -> float:
        if not p["sl"]:
            return 0.0
        v = (p["sl"] - p["price_open"]) if p["direction"] == "long" else (p["price_open"] - p["sl"])
        return max(0.0, v)

    def _final_points(self, ticket, st) -> tuple[float | None, bool]:
        """Closing result in price points from the MT5 closing deal; falls back to the last polled points (approx)."""
        last = st.get("last_points")
        try:
            if not self.dry:
                now = datetime.now(timezone.utc)
                deals = [d for d in broker.closed_deals(self.symbol, now - timedelta(days=3), now + timedelta(minutes=1)) if d["ticket"] == ticket]
                if deals and st.get("entry") is not None:
                    px = float(deals[-1]["price"]); entry = float(st["entry"])
                    return round((px - entry) if st.get("direction") == "long" else (entry - px), 2), False
        except Exception:
            pass
        return (round(float(last), 2) if last is not None else None), True

    def _move_sl(self, p, sl, action, quiet=False) -> bool:
        """MT5 SL modification through the one safe path. Returns True when the protection is in place
        (changed or already better). Never sends Discord itself — callers update state, journal, then notify."""
        if self.dry:
            self.health["last_broker_action"] = f"{action} DRY {datetime.now(IST):%H:%M:%S}"; return True
        r = broker.set_sl(p["ticket"], self.symbol, sl)
        self.health["last_broker_action"] = f"{action} {r.status} {datetime.now(IST):%H:%M:%S}"
        if r.status in ("SL_CHANGED", "ALREADY_BETTER"):
            if r.status == "SL_CHANGED":
                self.note_action(self.state.get(p["ticket"]), "sl_move", f"{action} → {sl:.2f}" if sl is not None else action)
            return True
        telemetry.failure(self.notify, self.journal, title="AUREON GUARDIAN FAILURE", key=self.key("ERROR", f"sl-{p['ticket']}-{action}-{r.retcode}"),
                          mode=self.S.name, symbol=self.symbol, ticket=p["ticket"], action=action, requested=r.requested_sl or sl,
                          current=r.existing_sl, bid=r.bid, ask=r.ask, retcode=r.retcode, comment=r.comment, last_error=r.last_error)
        return False

    def _close(self, p, reason, title, key, st, journal_reason=None, bar_t=None, extra: dict | None = None):
        event_name = {"p_flip": "FLIP CLOSE", "news_flat": "BANKED BEFORE NEWS", "claude_close": "CLAUDE CLOSED",
                      "opposite_cross": "OPPOSITE CROSS · CLOSED", "giveback": "GIVEBACK · CLOSED",
                      "setup_invalidated": "SETUP INVALIDATED · CLOSED",
                      "claude_cut_loss": "CLAUDE CUT LOSS"}.get(journal_reason or "", None)
        if bar_t is not None and st.get("closing_bar") == bar_t:
            return                                      # one close attempt per bar
        st["closing_bar"] = bar_t
        r = broker.close(p["ticket"], self.symbol, "aureon") if not self.dry else broker.BrokerResult(True, "DRY", changed=True)
        if r.ok:
            self.closed_by_aureon.add(p["ticket"])
        self.health["last_broker_action"] = f"close {r.status} {datetime.now(IST):%H:%M:%S}"
        self.journal.log("exit", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], points=p["points"],
                         reason=journal_reason or reason, auto=bool(r.ok), alert_id=st.get("alert_id"),
                         **({"flip_evidence": st["flip_evidence"]} if journal_reason == "p_flip" and st.get("flip_evidence") else {}),
                         **(extra or {}))
        st.pop("pending_exit", None)                    # v2.12.0: a close of any kind ends a pending pre-exit ask
        self.note_action(st, journal_reason or "close", reason)
        if r.ok:
            self.notify.send(key, ctitle(self.symbol, event_name or ("CLOSED " + ("WIN" if p["points"] > 0 else "LOSS")), p["direction"]), [reason],
                             fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"],
                                                    points=p["points"], secured=st.get("secured"), peak=st["peak"]),
                             color=EVENT_COLOURS["closed_win" if p["points"] > 0 else "closed_loss"], footer=cfooter(self.S.display_name, journal_reason or ""),
                             event="CLOSED" if p["points"] > 0 else "STOP", points=p["points"])
        else:
            telemetry.failure(self.notify, self.journal, title="AUREON GUARDIAN FAILURE", key=self.key("ERROR", f"close-{p['ticket']}-{r.retcode}"),
                              mode=self.S.name, symbol=self.symbol, ticket=p["ticket"], action=f"CLOSE ({reason})", bid=r.bid, ask=r.ask,
                              retcode=r.retcode, comment=r.comment, last_error=r.last_error, retry="close by hand if it persists")

    # ------------------------------------------------------------------ guardian: classic profile (pre-v2.7.0 behaviour)
    def _guard_classic(self, p, st, g, s, new_bar, bar_t, df, close, ef, es, sgn, gap, tag):
        sig = self.latest_signal
        # 1c) early lock: soft (bar close) | hard (broker SL) | off
        _elk = self.cfg.early_lock_mode
        if g.early_at is not None and not st.get("soft_lock") and st["secured"] < g.early_level and st["peak"] >= g.early_at and _elk != "off":
            if _elk == "soft":
                st["soft_lock"] = g.early_level; st["soft_lock_t"] = bar_t; self._save_state_now()
                self.journal.log("early_lock", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], level=g.early_level,
                                 peak=st["peak"], price=p["current"], soft=True)
                lock_px = p["price_open"] + s * g.early_level
                self.notify.send(self.key("EARLY_LOCK", p["ticket"]), ctitle(self.symbol, f"EARLY LOCK (soft) +{g.early_level:g}", p["direction"]),
                                 [f"+{g.early_at:g} seen — I'll close if an M5 bar CLOSES {'below' if s > 0 else 'above'} {lock_px:.2f} · broker SL stays at −{g.pre_stop:g} · next: secure at +{g.secure_at:g}"],
                                 fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"],
                                                        points=p["points"], sl=p["sl"], peak=st["peak"]),
                                 color=EVENT_COLOURS["secured"], footer=cfooter(self.S.display_name))
            else:
                sl = p["price_open"] + s * g.early_level
                if self._move_sl(p, sl, f"EARLY LOCK +{g.early_level:g}"):
                    st["secured"] = g.early_level; self._save_state_now()
                    self.journal.log("early_lock", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], level=g.early_level,
                                     peak=st["peak"], price=p["current"], soft=False)
                    self.notify.send(self.key("EARLY_LOCK", p["ticket"]), ctitle(self.symbol, f"EARLY LOCK +{g.early_level:g}", p["direction"]),
                                     [f"+{g.early_at:g} seen — SL moved to entry +{g.early_level:g}, next: secure at +{g.secure_at:g}"],
                                     fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"],
                                                            points=p["points"], sl=sl, secured=g.early_level, peak=st["peak"]),
                                     color=EVENT_COLOURS["secured"], footer=cfooter(self.S.display_name))

        # 1d) soft early lock exit
        if new_bar and st.get("soft_lock") and _elk == "soft":
            lock_level = st["soft_lock"]; lock_px = p["price_open"] + s * lock_level
            if (s > 0 and close <= lock_px) or (s < 0 and close >= lock_px):
                self.journal.log("soft_lock_exit", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"],
                                 close=close, level=lock_level, points_at_exit=p["points"], peak=st["peak"])
                self._close(p, f"soft early lock — bar closed at {close:.2f}", f"SOFT LOCK EXIT — {tag}",
                            self.key("SOFT_LOCK_EXIT", p["ticket"]), st, "soft_early_lock", bar_t)
                return

        # 2) P phase → cross confirmed
        if st["pre"] and sgn == s:
            st["pre"] = False; self._save_state_now()
            sl = es - s * g.ema_slow_sl_buffer
            ok = self._move_sl(p, sl, "CROSS SL")
            self.journal.log("cross_confirmed", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], sl=sl, ok=ok)
            self.notify.send(self.key("CROSS", p["ticket"]), ctitle(self.symbol, "CROSS CONFIRMED", p["direction"]),
                             [f"EMA {self.S.fast} crossed {'above' if s > 0 else 'below'} EMA {self.S.slow}" + ("" if ok else " · SL move pending — see failure alert")],
                             fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"], points=p["points"], sl=sl,
                                                    extra={"Next": f"secure at {p['price_open'] + s * g.secure_at:.2f} (+{g.secure_at:g})"}),
                             color=EVENT_COLOURS["cross"], footer=cfooter(self.S.display_name))
        elif g.slow_ema_sl and not st["pre"] and st["secured"] < g.secure_level and new_bar:
            self._move_sl(p, es - s * g.ema_slow_sl_buffer, "FOLLOW EMA80", quiet=True)

        # 3) SECURE
        if st["secured"] < g.secure_level and p["points"] >= g.secure_at:
            sl = p["price_open"] + s * g.secure_level
            if self._move_sl(p, sl, f"SECURE +{g.secure_level:g}"):
                st["secured"] = g.secure_level; self._save_state_now()
                if self.cfg.partial_close_pct > 0 and not st.get("partial_done") and not self.dry:
                    pr = broker.close_partial(p["ticket"], self.symbol, self.cfg.partial_close_pct / 100.0)
                    st["partial_done"] = True
                    if pr.ok:
                        self.journal.log("partial", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], volume=pr.requested_sl, points=p["points"])
                        self.notify.send(self.key("PARTIAL", p["ticket"]), ctitle(self.symbol, f"PARTIAL CLOSED {self.cfg.partial_close_pct:g}%", p["direction"]),
                                         [f"{pr.requested_sl:g} lots banked at {p['points']:+.2f} — rest rides with SL at +{g.secure_level:g}"],
                                         color=EVENT_COLOURS["secured"], footer=cfooter(self.S.display_name, "partial"))
                    else:
                        telemetry.failure(self.notify, self.journal, title="AUREON PARTIAL CLOSE FAILED", key=self.key("ERROR", f"partial-{p['ticket']}"),
                                          mode=self.S.name, symbol=self.symbol, ticket=p["ticket"], action="PARTIAL CLOSE",
                                          retcode=pr.retcode, comment=pr.comment, last_error=pr.last_error, retry="not retried — full position keeps riding")
                if st.get("soft_lock"):
                    self.journal.log("soft_lock_cleared", symbol=self.symbol, ticket=p["ticket"], reason="secure moved SL above soft level")
                    st.pop("soft_lock", None); st.pop("soft_lock_t", None)
                self.journal.log("secured", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], level=g.secure_level, step=g.secure_level, price=p["current"])
                self.notify.send(self.key("SECURED", f"{p['ticket']}-{g.secure_level:g}"), ctitle(self.symbol, f"SECURED +{g.secure_level:g}", p["direction"]), ["riding — lock steps up every +" + f"{g.ride_step:g}"],
                                 fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"], points=p["points"], sl=sl, secured=g.secure_level, peak=st["peak"]),
                                 color=EVENT_COLOURS["secured"], footer=cfooter(self.S.display_name))
        # 4) ride
        elif st["secured"] >= g.secure_level:
            nxt = st["secured"] + g.ride_step
            if p["points"] >= nxt + g.ride_step * 0.4:
                sl = p["price_open"] + s * nxt
                if self._move_sl(p, sl, f"RIDE +{nxt:g}"):
                    st["secured"] = nxt; self._save_state_now()
                    self.journal.log("secured", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], level=nxt, step=g.ride_step, price=p["current"])
                    self.notify.send(self.key("SECURED", f"{p['ticket']}-{nxt:g}"), ctitle(self.symbol, f"SECURED +{nxt:g}", p["direction"]), ["lock stepped up"],
                                     fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"], points=p["points"], sl=sl, secured=nxt, peak=st["peak"]),
                                     color=EVENT_COLOURS["secured"], footer=cfooter(self.S.display_name))

        # 5) exits on bar close
        if new_bar:
            against_f = (close < ef) if s > 0 else (close > ef)
            against_s = (close < es) if s > 0 else (close > es)
            separating = st["pre"] and len(gap) >= 3 and abs(gap[-1]) > abs(gap[-2]) > abs(gap[-3])
            reason = jr = None
            # v2.8.0 AUREON_SETUP_STOP: a CLOSE that kills the setup ends the trade; converging lines pause the P timeout
            _setup_on = self.cfg.setup_stop and st.get("setup") is not None and st["secured"] < g.secure_level
            _dead = _setup_on and not setup_mod.setup_alive(st["setup"])
            _paused = (_setup_on and st.get("setup_alive") and st["pre"] and st["bars"] >= g.pre_timeout_bars and st["secured"] == 0
                       and len(gap) >= 2 and abs(gap[-1]) < abs(gap[-2]))
            if _paused and not st.get("timeout_paused_logged"):
                st["timeout_paused_logged"] = True
                self.journal.log("pre_timeout_paused", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], bars=st["bars"],
                                 points=p["points"], gap=round(float(gap[-1]), 2))
            if st["secured"] >= g.secure_level and against_f:
                reason, jr = f"bar closed through EMA {self.S.fast} ({ef:.2f}) — ride over", "ema_fast_turn"
            elif _dead:
                reason, jr = f"setup invalidated: {', '.join(setup_mod.failed_checks(st['setup']))}", "setup_invalidated"
                self.journal.log("setup_invalidated", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"],
                                 failed=setup_mod.failed_checks(st["setup"]), close=close, points=p["points"])
            elif st["pre"] and p["points"] <= -g.pre_stop and not (
                    self.cfg.respect_manual_sl and p["sl"] and abs(p["price_open"] - p["sl"]) > g.pre_stop + 1e-6):
                reason, jr = f"P failed: {p['points']:+.2f}, lines not crossed", "pre_stop"
            elif separating and st["bars"] >= 2:
                reason, jr = f"P aborted: EMA {self.S.fast}/{self.S.slow} separating again before the cross", "pre_abort"
            elif st["pre"] and st["bars"] >= g.pre_timeout_bars and st["secured"] == 0 and not st.get("timeout_hold") and not _paused:
                if p["points"] > 0 or (self.cfg.respect_manual_sl and p.get("tp")):
                    st["timeout_hold"] = True
                    be = p["price_open"] + s * 0.5 if p["points"] > 1.0 else None
                    ok = self._move_sl(p, be, "TIMEOUT → BREAKEVEN") if be is not None else False
                    self.journal.log("pre_timeout_hold", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], points=p["points"], be=be, sl_ok=ok)
                    self.notify.send(self.key("TIMEOUT_HOLD", p["ticket"]), ctitle(self.symbol, "P TIMEOUT · HOLDING", p["direction"]),
                                     [f"{st['bars']} bars without the cross, but the trade is {p['points']:+.2f}"
                                      + (" and you set your own TP" if p.get("tp") else "") + " — **not closing**",
                                      (f"SL → entry +0.5 ({be:.2f})" if ok else "SL unchanged") + " · I'll keep managing it"],
                                     fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"],
                                                            points=p["points"], sl=(be if ok else (p["sl"] or None)), peak=st["peak"]),
                                     color=EVENT_COLOURS["news"], footer=cfooter(self.S.display_name, "guardian"))
                else:
                    reason, jr = f"P timeout: {st['bars']} bars without the cross", "pre_timeout"
            elif g.slow_ema_sl and not st["pre"] and st["secured"] < g.secure_level and against_s:
                reason, jr = f"bar closed through EMA {self.S.slow} ({es:.2f})", "ema_slow_stop"
            if reason:
                self._close_or_defer(p, reason, f"{'✅' if p['points'] > 0 else '🛑'} CLOSED — {tag}",
                                     self.key("CLOSED", f"{p['ticket']}-{bar_key(bar_t, self.off)}"), st, jr, bar_t, df)

    # ------------------------------------------------------------------ guardian: leg profile (v2.7.0/v2.7.1)
    def _guard_leg(self, p, st, g, s, new_bar, bar_t, df, close, ef, es, sgn, gap, tag):
        """Leg-based guardian. Phase A = before move proven. Phase B = riding with the leg."""
        cfg = self.cfg; entry = p["price_open"]
        phase = st.get("leg_phase", "A")
        arm_pts = max(g.early_at or 0, cfg.soft_arm_pts)  # v2.7.1: soft arm threshold

        # Phase A → Phase B transition: peak >= secure_at AND current points >= floor + buffer
        if phase == "A" and st["peak"] >= g.secure_at and p["points"] >= cfg.ride_floor + cfg.ride_buffer:
            floor_pts = cfg.ride_floor
            sl = entry + s * floor_pts
            if self._move_sl(p, sl, f"FLOOR +{floor_pts:g}"):
                phase = "B"
                st["leg_phase"] = "B"
                st["floor_pts"] = floor_pts
                st["trail_pts"] = floor_pts
                st["secured"] = floor_pts; self._save_state_now()
                # partial close unchanged
                if cfg.partial_close_pct > 0 and not st.get("partial_done") and not self.dry:
                    pr = broker.close_partial(p["ticket"], self.symbol, cfg.partial_close_pct / 100.0)
                    st["partial_done"] = True
                    if pr.ok:
                        self.journal.log("partial", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], volume=pr.requested_sl, points=p["points"])
                self.journal.log("move_proven", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"],
                                 peak=st["peak"], floor=floor_pts, guardian_profile="leg")
                self.notify.send(self.key("MOVE_PROVEN", p["ticket"]), ctitle(self.symbol, f"MOVE PROVEN +{st['peak']:.1f} · floor +{floor_pts:g}", p["direction"]),
                                 [f"riding with the leg · SL floor at entry +{floor_pts:g}"],
                                 fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=entry, now=p["current"],
                                                        points=p["points"], sl=entry + s * floor_pts, secured=floor_pts, peak=st["peak"]),
                                 color=EVENT_COLOURS["secured"], footer=cfooter(self.S.display_name, "leg guardian"))
            # else: floor SL move rejected — stay in Phase A, retry next poll

        if phase == "A":
            # Phase A: broker SL stays at -pre_stop (hard). Soft trail on closed bars.
            _elk = cfg.early_lock_mode
            if g.early_at is not None and st["peak"] >= arm_pts and not st.get("soft_trail_armed") and _elk != "off":
                st["soft_trail_armed"] = True
                trail_level = lg.soft_trail_level(st["peak"], arm_pts, g.early_level, cfg.soft_giveback_pts, cfg.soft_giveback_frac)
                st["soft_lock"] = trail_level; st["soft_lock_t"] = bar_t; self._save_state_now()
                self.journal.log("early_lock", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], level=trail_level,
                                 peak=st["peak"], price=p["current"], soft=True, guardian_profile="leg")
                self.notify.send(self.key("EARLY_LOCK", p["ticket"]), ctitle(self.symbol, f"SOFT TRAIL +{trail_level:.1f}", p["direction"]),
                                 [f"+{arm_pts:g} seen — trailing lock at +{trail_level:.1f} (bar close) · broker SL stays at −{g.pre_stop:g}"],
                                 fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=entry, now=p["current"],
                                                        points=p["points"], sl=p["sl"], peak=st["peak"]),
                                 color=EVENT_COLOURS["secured"], footer=cfooter(self.S.display_name, "leg guardian"))
            # Update soft trail level as peak grows
            if st.get("soft_trail_armed") and st.get("soft_lock") is not None:
                new_trail = lg.soft_trail_level(st["peak"], arm_pts, g.early_level, cfg.soft_giveback_pts, cfg.soft_giveback_frac)
                if new_trail > st["soft_lock"]:
                    st["soft_lock"] = new_trail

            # Soft trail exit on bar close
            if new_bar and st.get("soft_lock") is not None and st.get("soft_trail_armed"):
                lock_px = entry + s * st["soft_lock"]
                if (s > 0 and close <= lock_px) or (s < 0 and close >= lock_px):
                    self.journal.log("soft_lock_exit", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"],
                                     close=close, level=st["soft_lock"], points_at_exit=p["points"], peak=st["peak"],
                                     guardian_profile="leg", exit_rule="soft_trail")
                    self._close_leg(p, f"soft trail — bar closed at {close:.2f} (lock +{st['soft_lock']:.1f})",
                                    tag, st, "soft_trail", bar_t)
                    return

            # v2.8.0 AUREON_SETUP_STOP: a CLOSE that kills the setup ends the trade (no second stop in the leg profile)
            if new_bar and cfg.setup_stop and st.get("setup") is not None and not setup_mod.setup_alive(st["setup"]):
                self.journal.log("setup_invalidated", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"],
                                 failed=setup_mod.failed_checks(st["setup"]), close=close, points=p["points"], guardian_profile="leg")
                self._close_leg(p, f"setup invalidated: {', '.join(setup_mod.failed_checks(st['setup']))}", tag, st, "setup_invalidated", bar_t,
                                df=df)
                return

            # P phase → cross confirmed
            if st["pre"] and sgn == s:
                st["pre"] = False; self._save_state_now()
                sl = es - s * g.ema_slow_sl_buffer
                self._move_sl(p, sl, "CROSS SL")

        elif phase == "B":
            # Phase B: trail on closed bars, ride with the leg
            if new_bar:
                rng_col = df["high"].to_numpy() - df["low"].to_numpy()
                avg_range = float(np.mean(rng_col[-20:])) if len(rng_col) >= 20 else float(np.mean(rng_col[-max(1, len(rng_col)):]))
                bars_close = df["close"].to_numpy()[-cfg.ride_swing_bars:].tolist()
                bars_low = df["low"].to_numpy()[-cfg.ride_swing_bars:].tolist()
                bars_high = df["high"].to_numpy()[-cfg.ride_swing_bars:].tolist()
                # v2.7.1: peak_price correct for both directions
                peak_px = entry + s * st["peak"]

                old_trail = st.get("trail_pts", st.get("floor_pts", cfg.ride_floor))
                new_trail = lg.phase_b_trail(bars_close, bars_low, bars_high, p["direction"],
                                             peak_px, entry, st.get("floor_pts", cfg.ride_floor),
                                             old_trail, cfg.ride_swing_bars, cfg.ride_buffer,
                                             cfg.ride_atr_mult, avg_range)
                if new_trail > old_trail:
                    sl = entry + s * new_trail
                    if self._move_sl(p, sl, f"TRAIL +{new_trail:.1f}"):
                        st["trail_pts"] = new_trail; st["secured"] = new_trail; self._save_state_now()
                        last_reported = st.get("trail_reported", st.get("floor_pts", cfg.ride_floor))
                        if new_trail - last_reported >= 3.0:
                            st["trail_reported"] = new_trail
                            self.journal.log("trail_update", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"],
                                             trail=new_trail, peak=st["peak"], guardian_profile="leg")
                            self.notify.send(self.key("TRAIL", f"{p['ticket']}-{int(new_trail)}"), ctitle(self.symbol, f"TRAIL +{new_trail:.1f}", p["direction"]),
                                             [f"SL trailing the move · floor +{st.get('floor_pts', cfg.ride_floor):g}"],
                                             fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=entry, now=p["current"],
                                                                    points=p["points"], sl=sl, secured=new_trail, peak=st["peak"]),
                                             color=EVENT_COLOURS["secured"], footer=cfooter(self.S.display_name, "leg guardian"))

            # Phase B exits on bar close — use st["leg"] for leg_alive (sticky)
            if new_bar:
                against_f = lg.bar_closes_through_ema(close, ef, p["direction"])
                against_s = lg.bar_closes_through_ema(close, es, p["direction"])
                reason = jr = None

                if against_s:
                    reason, jr = f"bar closed through EMA {self.S.slow} ({es:.2f})", "ema_slow_close"
                elif not lg.leg_alive(st.get("leg"), p["direction"], ef, es):
                    reason, jr = f"leg ended — EMA {self.S.fast}/{self.S.slow} crossed back", "leg_end"
                elif against_f:
                    low_buf = float(df["low"].iloc[-1]) - cfg.ride_buffer if s > 0 else None
                    high_buf = float(df["high"].iloc[-1]) + cfg.ride_buffer if s < 0 else None
                    tighten_px = low_buf if s > 0 else high_buf
                    tighten_pts = (tighten_px - entry) if s > 0 else (entry - tighten_px)
                    old_t = st.get("trail_pts", st.get("floor_pts", cfg.ride_floor))
                    if tighten_pts > old_t:
                        sl = entry + s * tighten_pts
                        if self._move_sl(p, sl, f"EMA{self.S.fast} TIGHTEN +{tighten_pts:.1f}"):
                            st["trail_pts"] = tighten_pts; st["secured"] = tighten_pts; self._save_state_now()
                            self.journal.log("ema_fast_tighten", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"],
                                             trail=tighten_pts, guardian_profile="leg")

                if reason:
                    self._close_leg(p, reason, tag, st, jr, bar_t)
                    return

    def _advisor_tick(self, p, st, bar_t, df, ef, es, g, new_bar):
        """5b) advisor + periodic snapshot (notifications only — no broker action). Shared by every profile."""
        if not new_bar:
            return
        if not st.get("tracking_sent"):
            st["tracking_sent"] = True
            self.notify.send(self.key("TRACKING", p["ticket"]), ctitle(self.symbol, "TRACKING", p["direction"]),
                             ["I see your position — managing it from here"],
                             fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"],
                                                    points=p["points"], sl=p["sl"] or None),
                             color=EVENT_COLOURS["cross"], footer=cfooter(self.S.display_name))
        codes = self._advise(p, st, bar_t, df, ef, es, g)
        for adv in self.advisors():                                  # v1.10.0: enqueue only (one job per pullback, per advisor)
            adv.maybe_pullback(self, p, st, bar_t, df, codes or ())
            adv.maybe_drawdown(self, p, st, bar_t, df)               # v2.8.0: event-driven READs under water
        if st["bars"] and st["bars"] % self.SNAPSHOT_BARS == 0:
            self._snapshot(p, st, bar_t)

    # ------------------------------------------------------------------ v2.13.0 follow profile
    def _new_ticket_profile(self) -> str:
        """The profile a NEW ticket gets: the live config (so /feature-set guardian_profile applies to the next trade)."""
        try:
            return self.cfg.resolved_guardian_profile(getattr(self, "_demo", None), self.symbol)
        except Exception:
            return self._resolved_profile

    def follow_stop_pts(self, df) -> float:
        cfg = self.cfg
        base = float(cfg.follow_stop)
        if float(cfg.follow_stop_x_range) > 0 and df is not None and len(df):
            rng = (df["high"] - df["low"]).to_numpy()
            avg = float(rng[-20:].mean())
            base = max(base, float(cfg.follow_stop_x_range) * avg)
        return round(base, 2)

    def follow_lock_target(self, peak: float) -> float | None:
        """The follow stop in points from entry for this peak (None before SECURE_AT). Shared by the guardian and the
        guardian block. step: k = floor((peak − CLEARANCE) / STEP); k >= 1 → (k − 1) · STEP, never below SECURE_LEVEL.
        continuous: max(SECURE_LEVEL, peak − STEP)."""
        cfg = self.cfg
        if peak < float(cfg.follow_secure_at):
            return None
        sec, step = float(cfg.follow_secure_level), float(cfg.follow_step)
        if cfg.follow_trail_mode == "continuous":
            return max(sec, round(peak - step, 2))
        k = int(np.floor((peak - float(cfg.follow_clearance)) / step)) if step > 0 else 0
        return max(sec, (k - 1) * step) if k >= 1 else sec

    def _follow_stopped(self, st, final) -> bool:
        """The ticket vanished: was it the broker stop? Final points within 1.5 of the stop level (the lock, or −FOLLOW_STOP)."""
        if final is None:
            return False
        lvl = float(st.get("secured") or 0.0) if float(st.get("secured") or 0.0) > 0 else -float(st.get("follow_stop_pts") or 0.0)
        return abs(float(final) - lvl) <= 1.5

    def _follow_log(self, event, p, st, **kw):
        self.journal.log(event, symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], profile="follow",
                         lock_level=st.get("secured", 0.0), peak=st.get("peak", 0.0), **kw)

    def _guard_follow(self, p, st, g, s, new_bar, bar_t, df, close, ef, es, sgn, gap, tag):
        """v2.13.0 AUREON_GUARDIAN_PROFILE=follow. Once in, the trade is protected by the broker stop and nothing else:
        adopt the stop (entry − FOLLOW_STOP, never loosened) → secure +SECURE_LEVEL at +SECURE_AT → lock (k−1)·STEP once the
        peak is k·STEP + CLEARANCE. Exits: the broker stop, news flat while not secured, an opposite CONFIRMED cross with
        >= FLIP_MIN_EVIDENCE (EXIT_ON_FLIP=structure), MAX_HOLD_BARS (unsecured AND setup dead). Nothing else."""
        cfg = self.cfg; entry = float(p["price_open"])
        st["pre"] = False                                              # no P phase in this profile
        # 1) adopt: the stop, never loosened
        if "follow_stop_pts" not in st:
            st["follow_stop_pts"] = self.follow_stop_pts(df)
        stop_pts = float(st["follow_stop_pts"])
        want = round(entry - s * stop_pts, 2)
        cur = float(p["sl"]) if p.get("sl") else None
        looser = cur is None or ((cur < want) if s > 0 else (cur > want))
        if st["secured"] <= 0 and looser and not st.get("follow_adopted_fail"):
            if self._move_sl(p, want, f"FOLLOW STOP −{stop_pts:g}"):
                p = {**p, "sl": want}
                self._follow_log("protected", p, st, sl=want, stop_pts=stop_pts)
                self.notify.send(self.key("PROTECTED", p["ticket"]), ctitle(self.symbol, "PROTECTED", p["direction"]),
                                 [f"follow · stop −{stop_pts:g} · secures +{cfg.follow_secure_level:g} at +{cfg.follow_secure_at:g} · "
                                  f"then locks every +{cfg.follow_step:g} · no other exits"],
                                 fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=entry, now=p["current"],
                                                        points=p["points"], sl=want),
                                 color=EVENT_COLOURS["protected"], footer=cfooter(self.S.display_name, "follow"))
            else:
                st["follow_adopted_fail"] = True                       # failure card already sent by _move_sl; retry next start
        # 2) secure + 3) follow
        target = self.follow_lock_target(float(st["peak"]))
        first = float(st["secured"]) < float(cfg.follow_secure_level) - 1e-9
        if first and float(p["points"]) < float(cfg.follow_secure_at):
            target = None                                              # secure on LIVE points (a rebuilt peak is not enough)
        if target is not None and target > float(p["points"]):        # the peak's lock is above the price (rebuilt peak):
            target = self.follow_lock_target(float(p["points"]))       # take the ladder value the live price supports
        if target is not None and target > float(st["secured"]) + 1e-9 and (float(p["current"]) - (entry + s * target)) * s >= 0:
            lvl = round(entry + s * target, 2)                         # (a lock is never above the live price)
            if self._move_sl(p, lvl, f"FOLLOW {'SECURE' if first else 'LOCK'} +{target:g}"):
                p = {**p, "sl": lvl}
                prev = float(st["secured"]); st["secured"] = target; self._save_state_now()
                self._follow_log("secured", p, st, level=target, price=p["current"], mode_trail=cfg.follow_trail_mode)
                if first:
                    title = f"SECURED +{target:g}"
                    lines = [f"stop → entry +{target:g} · next lock at +{self._follow_next_peak(target):g}"]
                elif cfg.follow_trail_mode == "continuous" and int(target // cfg.follow_step) == int(prev // cfg.follow_step):
                    title = None                                       # continuous: card once per STEP, the SL moves silently
                else:
                    title = f"LOCK +{target:g} · peak +{st['peak']:.1f}"
                    lines = [f"stop → entry +{target:g} · next lock at +{self._follow_next_peak(target):g}"]
                if title:
                    self.notify.send(self.key("FOLLOW_LOCK", f"{p['ticket']}-{target:g}"), ctitle(self.symbol, title, p["direction"]), lines,
                                     fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=entry, now=p["current"],
                                                            points=p["points"], sl=lvl, secured=target, peak=st["peak"]),
                                     color=EVENT_COLOURS["secured"], footer=cfooter(self.S.display_name, "follow"))
        # the step ladder, journaled once per k (k = 1 locks the secure level, so its row moves nothing)
        if cfg.follow_trail_mode == "step" and float(st["secured"]) >= float(cfg.follow_secure_level) - 1e-9:
            k = int(np.floor((float(st["peak"]) - float(cfg.follow_clearance)) / float(cfg.follow_step))) if cfg.follow_step > 0 else 0
            if k >= 1 and k > int(st.get("follow_k", 0)):
                for kk in range(int(st.get("follow_k", 0)) + 1, k + 1):
                    self._follow_log("follow_lock", p, st, k=kk, level=max(float(cfg.follow_secure_level), (kk - 1) * float(cfg.follow_step)))
                st["follow_k"] = k
        # 4) exits on a bar close — the only software ones this profile has
        if new_bar:
            cc = self.last_confirmed_cross
            if (cfg.follow_exit_on_flip == "structure" and cc and cc.get("bar_time") == bar_t
                    and cc.get("direction") != p["direction"].upper() and not st.get("follow_flip_seen") == bar_t):
                st["follow_flip_seen"] = bar_t
                ev = self._flip_evidence(p, df, close, ef, es, gap)
                if len(ev) >= int(cfg.flip_min_evidence):
                    st["flip_evidence"] = ev
                    reason = f"opposite confirmed cross ({cc['direction']}) · structure confirms: {', '.join(ev)}"
                    title = f"🔁 FLIP CLOSE — {tag}"; key = self.key("FLIP", f"{p['ticket']}-{bar_key(bar_t, self.off)}")
                    if cfg.follow_pre_exit_read:
                        self._close_or_defer(p, reason, title, key, st, "flip", bar_t, df, extra=self._follow_extra(st))
                    else:
                        self._close(p, reason, title, key, st, "flip", bar_t, extra=self._follow_extra(st))
                    return
                self._follow_log("opposite_cross_ignored", p, st, direction=cc.get("direction"), evidence=ev, points=p["points"])
                self.note_action(st, "FLIP_HOLD", f"cross {cc.get('direction')} not confirmed by structure ({len(ev)}/{cfg.flip_min_evidence})")
            mh = int(cfg.follow_max_hold_bars)
            if mh > 0 and st["bars"] >= mh and float(st["secured"]) <= 0 and st.get("setup_alive") is False:
                self._close(p, f"max hold: {st['bars']} bars, never secured, setup dead", f"⌛ MAX HOLD — {tag}",
                            self.key("CLOSED", f"{p['ticket']}-{bar_key(bar_t, self.off)}"), st, "max_hold", bar_t,
                            extra=self._follow_extra(st))
                return
        # news: flat before high-impact news while not secured (the existing window)
        tutc = int(_time.time())
        if cfg.follow_news_flat and not st["news_done"] and g is not None and any(r - g.news_flat_min * 60 <= tutc < r for r in self.news):
            st["news_done"] = True
            if float(st["secured"]) <= 0:
                self._close(p, f"news in <{g.news_flat_min} min (not secured)", f"📰 FLAT before news — {tag}", self.key("NEWS", p["ticket"]),
                            st, "news_flat", bar_t, extra=self._follow_extra(st))

    def _follow_extra(self, st) -> dict:
        return {"profile": "follow", "lock_level": st.get("secured", 0.0), "peak": st.get("peak", 0.0)}

    def _follow_next_peak(self, target: float) -> float:
        """The peak at which the next lock happens. step: lock (k−1)·STEP > target needs k = floor(target/STEP) + 2, at
        peak k·STEP + CLEARANCE. continuous: any new peak above target + STEP."""
        cfg = self.cfg
        step = float(cfg.follow_step)
        if cfg.follow_trail_mode == "continuous":
            return round(target + step, 2)
        k = int(np.floor(target / step)) + 2
        return round(k * step + float(cfg.follow_clearance), 2)

    # ------------------------------------------------------------------ v2.12.0 Part A: what the guardian is about to do
    def _giveback_fires(self, pts_at_close: float, peak: float) -> bool:
        """The 1c giveback rule (bar CLOSE): fires when the close kept <= (1 − pct) of the peak. Shared with guardian_intent."""
        kept = pts_at_close / peak if peak > 0 else 1.0
        return kept <= 1.0 - self.cfg.giveback_pct / 100.0

    def _giveback_floor_pts(self, peak: float) -> float:
        return peak * (1.0 - self.cfg.giveback_pct / 100.0)

    @staticmethod
    def note_action(st: dict | None, rule: str, detail: str = "", t: float | None = None):
        """v2.12.0: ring buffer of the last 3 guardian actions on this ticket (closes, SL moves, structure SL, flip holds)."""
        if st is None:
            return
        ra = list(st.get("recent_actions") or [])
        ra.append({"t": int(t if t is not None else _time.time()), "rule": rule, "detail": str(detail)[:120]})
        st["recent_actions"] = ra[-3:]

    def guardian_intent(self, p: dict, st: dict, df, close: float, ef: float, es: float, gap=None) -> dict:
        """Pure read of the state the guardian already holds (no MT5 call): phase, levels and the NEXT exit rule with its
        distance. Levels come from the same thresholds / helpers the guardian uses."""
        g = self.g; cfg = self.cfg
        s = 1 if p["direction"] == "long" else -1
        entry = float(p["price_open"]); pts = float(p["points"])
        pts_close = (float(close) - entry) * s
        secured = float(st.get("secured", 0.0)); peak = float(st.get("peak", 0.0))
        prof = st.get("guardian_profile", getattr(self, "_resolved_profile", "classic"))
        if prof == "follow":
            return self._follow_intent(p, st, entry, s, pts, secured, peak)
        leg = prof == "leg"
        pre = bool(st.get("pre"))
        px = lambda pt: round(entry + s * pt, 2)
        if leg:
            phase = "ride" if st.get("leg_phase") == "B" else ("pre" if pre else "protect")
        elif pre:
            phase = "pre"
        elif g is not None and secured >= g.secure_level:
            phase = "ride" if secured > g.secure_level else "secured"
        else:
            phase = "protect"
        cands = []                                           # (rule, level, distance_pts, bars_left, fires_on)

        def add(rule, level, dist, bars_left=None, fires="close"):
            cands.append({"rule": rule, "level": level, "distance_pts": None if dist is None else round(float(dist), 2),
                          "bars_left": bars_left, "fires_on": fires})
        sl = float(p["sl"]) if p.get("sl") else None
        if sl is not None:                                   # the broker stop: always the last line (tick)
            sl_pts = (sl - entry) * s
            add("ride_step" if secured > 0 else "pre_stop", round(sl, 2), pts - sl_pts, fires="tick")
        elif g is not None:
            add("pre_stop", px(-g.pre_stop), pts + g.pre_stop, fires="tick")
        if g is not None and not leg:
            if secured >= g.secure_level:
                add("ema_fast_turn", round(float(ef), 2), (float(close) - float(ef)) * s)
            if pre:
                add("pre_stop", px(-g.pre_stop), pts_close + g.pre_stop)
                if secured == 0 and not st.get("timeout_hold"):
                    left = max(0, int(g.pre_timeout_bars) - int(st.get("bars", 0)))
                    add("pre_timeout", None, max(0.0, pts_close) if left <= 1 else None, bars_left=left)
            if g.slow_ema_sl and not pre and secured < g.secure_level:
                add("ema_slow_stop", round(float(es), 2), (float(close) - float(es)) * s)
        if leg and g is not None:
            if st.get("leg_phase") == "B":
                add("ema_slow_close", round(float(es), 2), (float(close) - float(es)) * s)
            elif st.get("soft_trail_armed") and st.get("soft_lock") is not None:
                add("soft_trail", px(float(st["soft_lock"])), pts_close - float(st["soft_lock"]))
        gb_level = None
        if cfg.giveback_pct > 0 and peak >= cfg.giveback_arm:
            floor = self._giveback_floor_pts(peak)
            gb_level = px(floor)
            add("giveback", gb_level, pts_close - floor)
        structure = cap_px = None
        cap_pts = st.get("cap_pts")
        if cap_pts:
            cap_px = px(-float(cap_pts))
        if st.get("setup") is not None and (cfg.setup_stop or st.get("setup_alive") is not None):
            try:
                cap = float(cap_pts or self._setup_cap(df))
                spts, _sw = self._structure_stop(p, df, cap, (sl - entry) * s if sl else -cap)
                structure = px(spts)
            except Exception:
                structure = None
            if cfg.setup_stop and secured < (g.secure_level if g is not None else 1e9):
                add("setup_invalidated", structure, 0.0 if st.get("setup_alive") is False else
                    ((float(close) - structure) * s if structure is not None else None))
        ranked = [c for c in cands if c["distance_pts"] is not None]
        nxt = min(ranked, key=lambda c: c["distance_pts"]) if ranked else \
            (min(cands, key=lambda c: c["bars_left"] if c["bars_left"] is not None else 1e9) if cands else None)
        trail = None
        if leg and st.get("trail_pts") is not None:
            trail = px(float(st["trail_pts"]))
        elif secured > 0:
            trail = px(secured)
        mae = float(st.get("mae", min(0.0, pts)))
        return {"phase": phase, "profile": prof, "bars_in_trade": int(st.get("bars", 0)), "secured": round(secured, 2),
                "peak": round(peak, 2), "mae": round(mae, 2), "trail_level": trail, "giveback_level": gb_level,
                "structure_stop": structure, "cap": cap_px,
                "next_exit": nxt or {"rule": "none", "level": None, "distance_pts": None, "bars_left": None, "fires_on": None},
                "flip_evidence": list(st["flip_evidence"]) if st.get("flip_evidence") else None,
                "setup_alive": st.get("setup_alive"), "recent_actions": list(st.get("recent_actions") or [])[-3:]}

    def _follow_intent(self, p, st, entry, s, pts, secured, peak) -> dict:
        """v2.13.0: the follow profile has one exit — the broker stop. next_exit = that stop (rule `stop` before securing,
        `ride_step` after) with its distance; next_lock = the next lock level and the peak that triggers it."""
        cfg = self.cfg
        px = lambda pt: round(entry + s * pt, 2)
        sl = float(p["sl"]) if p.get("sl") else px(-float(st.get("follow_stop_pts") or cfg.follow_stop))
        sl_pts = (sl - entry) * s
        phase = "protect" if secured <= 0 else ("secured" if secured <= float(cfg.follow_secure_level) + 1e-9 else "ride")
        if secured <= 0:
            nxt_lock_pts, at_peak = float(cfg.follow_secure_level), float(cfg.follow_secure_at)
        else:
            at_peak = self._follow_next_peak(secured)
            nxt_lock_pts = self.follow_lock_target(at_peak)
        return {"phase": phase, "profile": "follow", "bars_in_trade": int(st.get("bars", 0)), "secured": round(secured, 2),
                "peak": round(peak, 2), "mae": round(float(st.get("mae", min(0.0, pts))), 2), "trail_level": px(secured) if secured > 0 else None,
                "giveback_level": None, "structure_stop": None, "cap": px(-float(st.get("follow_stop_pts") or cfg.follow_stop)),
                "next_exit": {"rule": "ride_step" if secured > 0 else "stop", "level": round(sl, 2), "distance_pts": round(pts - sl_pts, 2),
                              "bars_left": None, "fires_on": "tick"},
                "next_lock": {"level": px(nxt_lock_pts), "lock_pts": nxt_lock_pts, "at_peak": at_peak,
                              "distance_pts": round(at_peak - pts, 2)},
                "flip_evidence": list(st["flip_evidence"]) if st.get("flip_evidence") else None,
                "setup_alive": st.get("setup_alive"), "recent_actions": list(st.get("recent_actions") or [])[-3:]}

    # ------------------------------------------------------------------ v2.12.0 Part B: pre-exit read (show | defer)
    PRE_EXIT_RULES = ("ema_fast_turn", "giveback", "setup_invalidated", "pre_timeout", "p_flip", "flip")

    def _pre_exit_claude(self):
        return next((a for a in self.advisors() if getattr(a, "SOURCE", "") == "claude"
                     and hasattr(a, "request_pre_exit_read") and a.advises(self.S)), None)

    def _pre_exit_cap(self, st) -> float:
        if st.get("guardian_profile") == "follow" and st.get("follow_stop_pts"):
            return float(st["follow_stop_pts"])                     # v2.13.0: the follow stop is the cap
        if self.cfg.setup_stop and st.get("cap_pts"):
            return float(st["cap_pts"])
        return float(self.g.pre_stop) if self.g is not None else float(self.cfg.setup_max_dd)

    def _close_or_defer(self, p, reason, title, key, st, jr, bar_t, df=None, detail: str = "", extra: dict | None = None):
        """v2.12.0: every close-based software exit goes through here. AUREON_PRE_EXIT_READ=off → _close exactly as before.
        show → _close + a pre_exit read (journaled next to the close). defer → no close this bar; Claude is asked; the verdict
        is applied in drain() (EXIT / timeout / error close by the original rule; HOLD waits one more bar, at most MAX)."""
        mode = getattr(self.cfg, "pre_exit_read", "off")
        if mode not in ("show", "defer") or jr not in self.PRE_EXIT_RULES or df is None:
            return self._close(p, reason, title, key, st, jr, bar_t, extra=extra)
        claude = self._pre_exit_claude()
        if claude is None or p["points"] <= -self._pre_exit_cap(st) + 2.0:      # no voter, or within 2 pts of the cap
            return self._close(p, reason, title, key, st, jr, bar_t, extra=extra)
        detail = detail or reason
        if mode == "show":
            self._close(p, reason, title, key, st, jr, bar_t, extra=extra)
            claude.request_pre_exit_read(self, p, st, bar_t, df, rule=jr, detail=detail, show=True)
            return
        pe = st.get("pending_exit")
        if pe and pe.get("rule") != jr:                                       # a different rule fired: the old ask is moot
            self._cancel_pending_exit(p, st, f"superseded by {jr}")
            pe = None
        if pe:
            pe["seen_bar"] = bar_t
            if pe.get("bar_t") == bar_t or pe.get("state") == "waiting":
                return                                                        # still waiting for this bar's verdict
        n = int(st.get("deferrals", 0))
        if n >= int(self.cfg.pre_exit_max_defer):
            st.pop("pending_exit", None)
            return self._close(p, reason, title, key, st, jr, bar_t, extra={**(extra or {}), "claude_pre_exit": "max_deferrals", "deferrals": n})
        job_id = f"{p['ticket']}-{bar_t}-{n}"
        st["pending_exit"] = {"rule": jr, "bar_t": bar_t, "seen_bar": bar_t, "detail": detail, "reason": reason, "title": title,
                              "key": key, "t": _time.time(), "state": "waiting", "job_id": job_id, "extra": extra or {}}
        self.journal.log("exit_pending", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], rule=jr, detail=detail,
                         points=p["points"], deferral_n=n)
        self.notify.send(self.key("EXIT_PENDING", f"{p['ticket']}-{bar_key(bar_t, self.off)}"),
                         ctitle(self.symbol, f"EXIT PENDING · {jr} · asking Claude", p["direction"]),
                         [detail, f"the broker stop stays where it is · deferral {n}/{self.cfg.pre_exit_max_defer} used · "
                                  f"closes by the rule if no answer in {self.cfg.pre_exit_wait_s:g} s"],
                         fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"],
                                                points=p["points"], sl=p["sl"] or None, secured=st.get("secured"), peak=st["peak"]),
                         color=EVENT_COLOURS["flip"], footer=cfooter(self.S.display_name, "pre-exit"))
        if not claude.request_pre_exit_read(self, p, st, bar_t, df, rule=jr, detail=detail, job_id=job_id, deferral_n=n):
            st.pop("pending_exit", None)                                      # could not even ask (queue full): close now
            return self._close(p, reason, title, key, st, jr, bar_t, extra={**(extra or {}), "claude_pre_exit": "error"})

    def finish_pending_exit(self, p, st, outcome: str):
        """Close by the pending rule (EXIT / timeout / error) — the original reason, title and journal rule."""
        pe = st.pop("pending_exit", None)
        if not pe:
            return False
        self._close(p, pe["reason"], pe["title"], pe["key"], st, pe["rule"], None,
                    extra={**(pe.get("extra") or {}), "claude_pre_exit": outcome, "deferrals": int(st.get("deferrals", 0))})
        return True

    def _cancel_pending_exit(self, p, st, why: str):
        pe = st.pop("pending_exit", None)
        if pe:
            self.journal.log("exit_cancelled", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], rule=pe["rule"], why=why,
                             points=p["points"])

    def _pending_exit_tick(self, p, st, new_bar, bar_t):
        """Every poll: a verdict that did not come in AUREON_PRE_EXIT_WAIT_S closes by the rule. On a new bar close where the
        pending rule did not fire again, the ask is moot (exit_cancelled). Never touches the SL."""
        pe = st.get("pending_exit")
        if not pe:
            return
        if pe.get("state") == "waiting" and _time.time() - float(pe.get("t", 0)) > float(self.cfg.pre_exit_wait_s):
            self.finish_pending_exit(p, st, "timeout")

    def _pending_exit_moot(self, p, st, new_bar, bar_t):
        """After this bar's exit rules ran: a pending ask whose rule did not fire again on this close is cancelled."""
        pe = st.get("pending_exit")
        if pe and new_bar and pe.get("seen_bar") != bar_t and pe.get("bar_t") != bar_t:
            self._cancel_pending_exit(p, st, "rule no longer true on the close")

    def _close_leg(self, p, reason, tag, st, jr, bar_t, df=None):
        """Close with profile + exit_rule in the card and journal. v2.12.0: df given → may be deferred (pre-exit read)."""
        profile = st.get("guardian_profile", "leg")
        label = f"{profile} · {jr}" if jr else profile
        self._close_or_defer(p, f"{reason} [{label}]",
                             f"{'✅' if p['points'] > 0 else '🛑'} CLOSED — {tag}",
                             self.key("CLOSED", f"{p['ticket']}-{bar_key(bar_t, self.off)}"), st, jr, bar_t, df)

    # ------------------------------------------------------------------ v2.8.0 setup view (AUREON_SETUP_STOP / Claude snapshot)
    def _setup_wanted(self) -> bool:
        return bool(self.cfg.setup_stop) or any(getattr(a, "SOURCE", "") == "claude" for a in self.advisors())

    def _setup_cap(self, df) -> float:
        rng = (df["high"] - df["low"]).to_numpy()
        avg = float(rng[-20:].mean()) if len(rng) else 0.0
        return setup_mod.cap_points(self.cfg.setup_max_dd, self.cfg.setup_max_dd_x_range, avg)

    def _structure_stop(self, p, df, cap, current_pts) -> tuple[float, float | None]:
        """(points from entry, swing price) for the setup stop on the latest closed bars."""
        n = self.cfg.setup_swing_bars
        lows = df["low"].to_numpy()[-n:].tolist(); highs = df["high"].to_numpy()[-n:].tolist()
        pts = setup_mod.structure_stop_pts(p["direction"], p["price_open"], lows, highs, n, self.cfg.setup_buffer, cap, current_pts)
        swing = (min(lows) if p["direction"] == "long" else max(highs)) if lows else None
        return pts, swing

    def _update_setup(self, p, st, s, new_bar, df, close, ef, es):
        """Per ticket: st["setup"], st["setup_alive"], st["cap_pts"], st["dist_to_cap"], st["bars_in_dd"] (closed bars only)."""
        if not (new_bar or "setup" not in st):
            return
        n = self.cfg.setup_swing_bars
        prior = df.iloc[:-1]
        checks = setup_mod.setup_checks(p["direction"], close, ef, es, self.cfg.setup_buffer,
                                        prior["low"].to_numpy()[-n:].tolist(), prior["high"].to_numpy()[-n:].tolist(), n,
                                        self._m15_state())
        if st.get("pre"):
            checks["ema_order"] = None          # P phase: the lines have not crossed yet by definition — not a failure
        st["setup"] = checks; st["setup_alive"] = setup_mod.setup_alive(checks)
        cap = st.get("cap_pts") or self._setup_cap(df); st["cap_pts"] = cap
        under = max(0.0, (p["price_open"] - close) * s)
        st["dist_to_cap"] = round(cap - under, 2)
        if new_bar:
            st["bars_in_dd"] = 0 if (close - p["price_open"]) * s >= 0 else int(st.get("bars_in_dd", 0)) + 1

    # ------------------------------------------------------------------ guardian
    def _guard(self, new_bar, bar_t, df, close, ef, es, sgn):
        g = self.g; cf, cs = self.S.ema_cols
        gap = (df[cf] - df[cs]).to_numpy()
        pos = broker.positions(self.symbol)
        self._pos_seen = pos
        live = {p["ticket"] for p in pos}
        for t in list(self.state):
            if t not in live:
                st = self.state.pop(t)
                for adv in self.advisors():
                    adv.forget(self.symbol, t)
                final, approx = self._final_points(t, st)             # v1.11.0: for the weekly compare (measurement only)
                follow_stop = (st.get("guardian_profile") == "follow" and t not in self.closed_by_aureon
                               and self._follow_stopped(st, final))
                self.journal.log("closed", symbol=self.symbol, mode=self.S.name, ticket=t, direction=st.get("direction"),
                                 entry=st.get("entry"), secured=st.get("secured"), peak=st.get("peak"),
                                 final_points=final, final_approx=approx, alert_id=st.get("alert_id"),
                                 **({"reason": "stop", "profile": "follow", "lock_level": st.get("secured", 0.0)} if follow_stop else {}))
                if follow_stop:                                # v2.13.0: the follow profile's only exit — no "why?" ask
                    self.notify.send(self.key("CLOSED", f"{t}-stop"), ctitle(self.symbol, f"STOPPED {'+' if (final or 0) >= 0 else ''}{(final or 0):.1f}",
                                                                              st.get("direction")),
                                     [f"follow · broker stop at entry {'+' if st.get('secured', 0) > 0 else '−'}"
                                      f"{(st.get('secured') or st.get('follow_stop_pts') or 0):g} · peak +{st.get('peak', 0):.1f}"],
                                     color=EVENT_COLOURS["closed_win" if (final or 0) > 0 else "closed_loss"],
                                     footer=cfooter(self.S.display_name, "follow · stop"), event="CLOSED" if (final or 0) > 0 else "STOP",
                                     points=final)
                    self.closed_by_aureon.add(t)
                if t not in self.closed_by_aureon:            # closed by you (or your own SL/TP): ask why, one tap
                    last = st.get("last_points", 0.0)
                    self.notify.ask(self.key("CLOSED_BY_YOU", t), ctitle(self.symbol, "CLOSED BY YOU", st.get("direction")),
                                    [f"last seen {last:+.2f} · peak +{st.get('peak', 0):.2f} · secured +{st.get('secured', 0):g}", "**why?** one tap — it goes in the weekly review"],
                                    fields=[field("Ticket", f"#{t}"), field("Entry", f"{st.get('entry', 0):.2f}"), field("Peak", f"+{st.get('peak', 0):.2f}")],
                                    color=EVENT_COLOURS["info"], footer=cfooter(self.S.display_name, "reason"),
                                    meta={"reason_for": "close", "symbol": self.symbol, "ticket": t, "side": (st.get("direction") or "").upper(),
                                          "bar": self.last_bar, "kind": "close", "mode": self.S.name, "price": st.get("entry")},
                                    event="CLOSED" if last > 0 else "STOP", points=last)
                self.closed_by_aureon.discard(t)
        self.health["managed"] = len(pos)
        sig = self.latest_signal
        for p in pos:
            s = 1 if p["direction"] == "long" else -1
            alert_id = None
            open_read_key = False                                # v2.8.0: False = not first sight; else the ask card key (or None)
            if p["ticket"] not in self.state:                    # v1.11.0: first sight of a trade (compare: 'Me · TAKEN')
                # v2.3.6: try journal reconstruction before creating fresh state
                rebuilt, src = self.journal.rebuild_state_for_ticket(p["ticket"])
                if rebuilt is not None:
                    rebuilt["secured"] = max(rebuilt.get("secured", 0.0), self._locked(p))  # live SL is the truth
                    self.state[p["ticket"]] = rebuilt
                    self.journal.log("state_rebuilt", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"],
                                     source=src, secured=rebuilt["secured"], peak=rebuilt.get("peak", 0),
                                     alert_id=rebuilt.get("alert_id"))
                else:
                    self.journal.log("state_rebuilt", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"],
                                     source="none")
                pa = self.pending_alert
                if pa and pa["side"] == ("LONG" if s > 0 else "SHORT") and _time.time() - pa["t"] <= self.cfg.ask_ttl_min * 60:
                    alert_id = pa["alert_id"]; pa_card_key = pa.get("card_key")
                    self.pending_alert = None; _save_pending(self.cfg.log_dir, self.symbol, None)   # consumed
                else:
                    pa_card_key = None
                self.journal.log("position_seen", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"],
                                 side="LONG" if s > 0 else "SHORT", entry=p["price_open"], open_time=p.get("time"), alert_id=alert_id)
                if rebuilt is None:                                                # a new trade, not a restart
                    open_read_key = pa_card_key
                if alert_id:                                                       # v2.0.2: the card shows the ticket (≤ 1 poll)
                    self.notify.edit_card(pa_card_key or alerts_mod.key_for(self.symbol, alert_id),
                                          {"Decision": f"ATTACHED · ticket {p['ticket']} · {p['direction'].upper()} @ {p['price_open']:.2f}"
                                                       f" · SL {p['sl'] if p['sl'] else '— (protecting)'} · {ist(bar_t, self.off)} IST"})
                    if p["sl"]:                                                    # SL came with the order: say so (the guardian's PROTECTED card)
                        self.notify.send(self.key("PROTECTED", p["ticket"]), ctitle(self.symbol, "PROTECTED", p["direction"]),
                                         [f"initial stop −{g.pre_stop:g} set with the order (alert {alert_id})"],
                                         fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"],
                                                                points=p["points"], sl=p["sl"]),
                                         color=EVENT_COLOURS["protected"], footer=cfooter(self.S.display_name))
            st = self.state.setdefault(p["ticket"], {"symbol": self.symbol, "mode": self.S.name, "direction": p["direction"],
                                                     "entry": p["price_open"], "peak": p["points"], "pre": (sgn != s) if g.p_phase else False, "bars": 0,
                                                     "news_done": False, "secured": 0.0, "alert_id": alert_id})
            if p["points"] > st["peak"] or "peak_t" not in st:
                st["peak_t"] = bar_t                                                   # v2.0.0: bars_since_peak for the Claude snapshot
            st["peak"] = max(st["peak"], p["points"]); st["last_points"] = p["points"]
            st["mae"] = min(st.get("mae", 0.0), p["points"])
            if new_bar: st["bars"] += 1
            st["secured"] = max(st.get("secured", 0.0), self._locked(p))      # live SL is the truth; never lower
            tag = f"{self.symbol} #{p['ticket']} {p['direction'].upper()} @ {p['price_open']:.2f}"
            if st.get("frozen"):
                continue                                   # mode mismatch: keep SL, apply no strategy rules
            if st.get("pending_exit"):                     # v2.12.0: verdict timeout / a moot ask (never touches the SL)
                self._pending_exit_tick(p, st, new_bar, bar_t)
                if st.get("pending_exit") is None and p["ticket"] in self.closed_by_aureon:
                    continue
            if self._setup_wanted():                                           # v2.8.0
                self._update_setup(p, st, s, new_bar, df, close, ef, es)
            if open_read_key is not False:
                for adv in self.advisors():                                    # v2.8.0: one READ at open (Claude; shown only)
                    adv.request_open_read(self, p, st, bar_t, df, card_key=open_read_key)
            if "guardian_profile" not in st and self._new_ticket_profile() == "follow":
                st["guardian_profile"] = "follow"                              # v2.13.0: sticky per ticket, like classic / leg
            if st.get("guardian_profile") == "follow":
                if not self._setup_wanted():
                    self._update_setup(p, st, s, new_bar, df, close, ef, es)   # setup_alive for max_hold / the snapshot
                self._guard_follow(p, st, g, s, new_bar, bar_t, df, close, ef, es, sgn, gap, tag)
                if st.get("pending_exit"):
                    self._pending_exit_moot(p, st, new_bar, bar_t)
                if p["ticket"] not in self.closed_by_aureon:
                    self._advisor_tick(p, st, bar_t, df, ef, es, g, new_bar)
                continue
            _structure = self.cfg.setup_stop and st.get("guardian_profile", self._resolved_profile) != "leg"

            # 0) protect a naked position (never touch an existing manual SL here)
            if not p["sl"]:
                if _structure:                                                 # v2.8.0: the stop is the setup
                    cap = st.get("cap_pts") or self._setup_cap(df)
                    pts, swing = self._structure_stop(p, df, cap, -cap)
                    sl = p["price_open"] + s * pts
                    text = (f"initial stop {'−' if pts < 0 else '+'}{abs(pts):.1f} (structure: swing {'low' if s > 0 else 'high'} "
                            f"{swing:.1f} {'−' if s > 0 else '+'} {self.cfg.setup_buffer:.1f}, cap −{cap:g})")
                else:
                    sl = p["price_open"] - s * g.pre_stop
                    text = f"initial stop −{g.pre_stop:g}"
                if self._move_sl(p, sl, "PROTECT"):
                    self.journal.log("protected", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], sl=sl)
                    self.notify.send(self.key("PROTECTED", p["ticket"]), ctitle(self.symbol, "PROTECTED", p["direction"]), [text],
                                     fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"], points=p["points"], sl=sl),
                                     color=EVENT_COLOURS["protected"], footer=cfooter(self.S.display_name))
                    if _structure:
                        p = {**p, "sl": sl}                                    # (v2.7.2 path keeps p unchanged this poll)
            elif _structure and new_bar and st["secured"] == 0:
                # v2.8.0: the structure stop follows the swing up (never loosens; _move_sl refuses a worse level anyway)
                cap = st.get("cap_pts") or self._setup_cap(df)
                cur = (p["sl"] - p["price_open"]) * s
                pts, swing = self._structure_stop(p, df, cap, cur)
                if pts > cur + 1e-9:
                    lvl = p["price_open"] + s * pts
                    if self._move_sl(p, lvl, "STRUCTURE SL"):
                        self.journal.log("structure_sl", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], swing=swing, cap=cap,
                                         level=lvl, points=round(pts, 2))
                        self.note_action(st, "structure_sl", f"swing {swing} → {lvl:.2f}")
                        p = {**p, "sl": lvl}

            # 1) opposite P → close (flip). The replacement trade is yours to place.
            #    v2.8.0 AUREON_FLIP_REQUIRE_STRUCTURE=1: only when the structure confirms (>= FLIP_MIN_EVIDENCE items)
            if sig and not sig["consumed"] and sig["kind"] == "P" and sig["bar_time"] == bar_t and sig["direction"] != p["direction"].upper():
                sig["consumed"] = True
                ev = self._flip_evidence(p, df, close, ef, es, gap) if self.cfg.flip_require_structure else None
                if ev is None or len(ev) >= self.cfg.flip_min_evidence:
                    if ev is None:
                        self._close_or_defer(p, f"opposite P ({sig['direction']}) — flip", f"🔁 FLIP CLOSE — {tag}",
                                             self.key("FLIP", f"{p['ticket']}-{bar_key(bar_t, self.off)}"), st, "p_flip", bar_t, df)
                    else:
                        st["flip_evidence"] = ev
                        self._close_or_defer(p, f"opposite P ({sig['direction']}) — flip · structure confirms: {', '.join(ev)}",
                                             f"🔁 FLIP CLOSE — {tag}", self.key("FLIP", f"{p['ticket']}-{bar_key(bar_t, self.off)}"), st,
                                             "p_flip", bar_t, df)
                    continue
                # held: the structure does not confirm the opposite P — keep the SL, keep managing by the other rules
                self.journal.log("opposite_p_ignored", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], direction=sig["direction"],
                                 evidence=ev, points=p["points"], peak=st["peak"])
                self.note_action(st, "FLIP_HOLD", f"P {sig['direction']} not confirmed ({len(ev)}/{self.cfg.flip_min_evidence})")
                self.notify.send(self.key("FLIP_HOLD", f"{p['ticket']}-{bar_key(bar_t, self.off)}"), ctitle(self.symbol, "OPPOSITE P · HOLDING", p["direction"]),
                                 [f"P {sig['direction']} formed but structure does not confirm ({len(ev)}/{self.cfg.flip_min_evidence}: "
                                  f"{', '.join(ev) or 'none'}) — keeping the SL, not flipping"],
                                 fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"],
                                                        points=p["points"], sl=p["sl"] or None, secured=st.get("secured"), peak=st["peak"]),
                                 color=EVENT_COLOURS["info"], footer=cfooter(self.S.display_name, "guardian"))
                for adv in self.advisors():
                    adv.request_flip_read(self, p, st, bar_t, df, evidence=ev)

            # 1b) v2.0.0 (ema2050): the next CONFIRMED opposite cross closes the trade (same confirm rule as the entry)
            cc = self.last_confirmed_cross
            if g.exit_on_confirmed_cross and new_bar and cc and cc["bar_time"] == bar_t and cc["direction"] != p["direction"].upper():
                self._close(p, f"confirmed opposite cross ({cc['direction']}) — the leg is over", f"🔁 OPPOSITE CROSS — {tag}",
                            self.key("OPP_CROSS", f"{p['ticket']}-{bar_key(bar_t, self.off)}"), st, "opposite_cross", bar_t)
                continue

            # 1c) v2.8.0 giveback from peak on a bar CLOSE (AUREON_GIVEBACK_PCT, armed at GIVEBACK_ARM). Off by default.
            if new_bar and self.cfg.giveback_pct > 0 and st["peak"] >= self.cfg.giveback_arm:
                pts_at_close = (close - p["price_open"]) * s                 # the bar CLOSE, never the tick
                kept = pts_at_close / st["peak"] if st["peak"] > 0 else 1.0
                if self._giveback_fires(pts_at_close, st["peak"]):
                    self._close_or_defer(p, f"gave back {100 * (1 - kept):.0f}% of peak +{st['peak']:.1f} (closed {pts_at_close:+.1f})",
                                         (f"✅ GIVEBACK EXIT — {tag}" if pts_at_close > 0 else f"🛑 GIVEBACK EXIT — {tag}"),
                                         self.key("GIVEBACK", f"{p['ticket']}-{bar_key(bar_t, self.off)}"), st, "giveback", bar_t, df)
                    continue

            # v2.7.0/v2.7.1: guardian profile — "leg" or "classic"
            # sticky: once a ticket gets a profile, it keeps it (stored in st)
            if "guardian_profile" not in st:
                st["guardian_profile"] = self._resolved_profile
            _is_leg = st["guardian_profile"] == "leg"

            # v2.7.1: sticky leg ownership via st["leg"] snapshot
            if _is_leg and "leg" not in st:
                if self.leg and self.leg["side"].upper() == p["direction"].upper():
                    st["leg"] = lg.make_leg_snapshot(self.leg)
                else:
                    # create a leg snapshot starting at this position's entry
                    st["leg"] = {"side": p["direction"].upper(), "start_px": p["price_open"],
                                 "start_t": st.get("entry_t", bar_t), "best": max(0.0, p["points"]),
                                 "best_px": p["price_open"] + s * max(0.0, p["points"]), "against": 0.0}
            # update the snapshot's best/against from the live leg if it matches
            if _is_leg and st.get("leg") and self.leg and self.leg["side"].upper() == st["leg"]["side"].upper():
                if self.leg["best"] > st["leg"]["best"]:
                    st["leg"]["best"] = self.leg["best"]
                    st["leg"]["best_px"] = self.leg["best_px"]

            if _is_leg:
                self._guard_leg(p, st, g, s, new_bar, bar_t, df, close, ef, es, sgn, gap, tag)
            else:
                self._guard_classic(p, st, g, s, new_bar, bar_t, df, close, ef, es, sgn, gap, tag)
            if st.get("pending_exit"):
                self._pending_exit_moot(p, st, new_bar, bar_t)
            if p["ticket"] not in self.state:
                continue  # position was closed

            # 5b) advisor + periodic snapshot (notifications only — no broker action)
            self._advisor_tick(p, st, bar_t, df, ef, es, g, new_bar)

            # 6) news safeguard
            tutc = int(_time.time())
            if not st["news_done"] and any(r - g.news_flat_min * 60 <= tutc < r for r in self.news):
                st["news_done"] = True
                if _is_leg and st.get("leg_phase") == "B":
                    # v2.7.0 Phase B leg: tighten, don't close
                    news_sl = p["price_open"] + s * max(st.get("trail_pts", 0), p["points"] - 3)
                    if self._move_sl(p, news_sl, "NEWS TIGHTEN"):
                        trail_now = max(st.get("trail_pts", 0), p["points"] - 3)
                        st["trail_pts"] = trail_now; self._save_state_now()
                        self.notify.send(self.key("NEWS", p["ticket"]), ctitle(self.symbol, f"NEWS <{g.news_flat_min} MIN · TIGHTENED", p["direction"]),
                                         [f"Phase B — SL tightened to +{trail_now:.1f}, not closing"],
                                         fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"],
                                                                points=p["points"], sl=news_sl, peak=st["peak"]),
                                         color=EVENT_COLOURS["news"], footer=cfooter(self.S.display_name))
                elif _is_leg and st.get("leg_phase", "A") == "A":
                    # v2.7.1 Phase A leg: do NOT close; cap broker SL at -pre_stop;
                    # if soft lock armed, convert to hard SL at that level (never loosen)
                    self._move_sl(p, p["price_open"] - s * g.pre_stop, "NEWS CAP")
                    if st.get("soft_lock") is not None and st.get("soft_trail_armed"):
                        hard_sl = p["price_open"] + s * st["soft_lock"]
                        if self._move_sl(p, hard_sl, f"NEWS LOCK +{st['soft_lock']:.1f}"):
                            st["secured"] = max(st.get("secured", 0), st["soft_lock"])
                            self._save_state_now()
                    self.notify.send(self.key("NEWS", p["ticket"]), ctitle(self.symbol, f"NEWS <{g.news_flat_min} MIN · CAPPED", p["direction"]),
                                     [f"Phase A — SL capped, not closing" + (f" · soft lock converted to hard +{st.get('soft_lock', 0):.1f}" if st.get("soft_lock") else "")],
                                     fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"],
                                                            points=p["points"], sl=p["sl"], peak=st["peak"]),
                                     color=EVENT_COLOURS["news"], footer=cfooter(self.S.display_name))
                elif p["points"] >= g.secure_at * 0.1:
                    self._close(p, "news in <15 min", f"📰 BANKED before news — {tag}", self.key("NEWS", p["ticket"]), st, "news_flat", bar_t)
                elif st["secured"] == 0:
                    if self._move_sl(p, p["price_open"] - s * g.pre_stop, "NEWS CAP"):
                        self.notify.send(self.key("NEWS", p["ticket"]), ctitle(self.symbol, f"NEWS <{g.news_flat_min} MIN", p["direction"]), [f"loss capped at −{g.pre_stop:g}"],
                                         fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"], points=p["points"], sl=p["price_open"] - s * g.pre_stop),
                                         color=EVENT_COLOURS["news"], footer=cfooter(self.S.display_name))
        if sig and not sig["consumed"] and sig["bar_time"] == bar_t:
            sig["consumed"] = True                       # nothing to flip; signal spent
        for adv in self.advisors():                      # v1.10.0: advisor verdicts AFTER the guardian — its hard exits win
            adv.drain(self, pos)                         # v2.3.0: Claude first (may apply in manage), then Codex (shown only)
        # v2.3.6: RESUMED card once after (re)start when positions are present
        if pos and not self._resumed_posted:
            self._resumed_posted = True
            armed = len(self.alerts.armed(self.symbol)) if self.alerts else 0
            rlines = []
            for p in pos:
                st = self.state.get(p["ticket"], {})
                sec = st.get("secured", 0.0); pk = st.get("peak", 0.0)
                if sec >= (g.secure_level if g else 1e9):
                    ph = f"RIDING +{sec:g}"
                elif sec >= (g.early_level if g else 1e9):
                    ph = f"SECURED +{sec:g}"
                elif sec > 0:
                    ph = f"EARLY +{sec:g}"
                else:
                    ph = "PROTECT"
                src = "state file"
                if any(r.get("event") == "state_rebuilt" and r.get("ticket") == p["ticket"] for r in self.journal.read(int(self.started_wall))):
                    rb = next((r for r in self.journal.read(int(self.started_wall)) if r.get("event") == "state_rebuilt" and r.get("ticket") == p["ticket"]), None)
                    src = rb.get("source", "state file") if rb else "state file"
                frozen = " · MODE MISMATCH (read-only)" if st.get("frozen") else ""
                # v2.7.1: show guardian profile + leg phase in RESUMED
                gp = st.get("guardian_profile", self._resolved_profile)
                leg_info = ""
                if gp == "leg":
                    lp = st.get("leg_phase", "A")
                    if lp == "B":
                        leg_info = f" · leg B floor +{st.get('floor_pts', 0):g} trail +{st.get('trail_pts', 0):.1f}"
                    else:
                        sl_info = f"+{st.get('soft_lock', 0):.1f}" if st.get("soft_lock") else "—"
                        leg_info = f" · leg A soft {sl_info}"
                rlines.append(f"#{p['ticket']} {p['direction'].upper()} @ {p['price_open']:.2f} · {p['points']:+.1f} pts · "
                              f"{ph} · SL {p['sl'] or '—'} · {gp}{leg_info} · source: {src}{frozen}")
            self.notify.send(self.key("RESUMED", f"{pos[0]['ticket']}-{int(self.started_wall)}"),
                             ctitle(self.symbol, "RESUMED"),
                             rlines + ([f"alerts armed: {armed}"] if armed else []),
                             color=EVENT_COLOURS.get("info", 0xFEE75C),
                             footer=cfooter(self.S.display_name, "restart-safe"))
        elif not pos:
            self._resumed_posted = True  # no positions — nothing to resume
        self.journal.save_state(self.symbol, self.state)
