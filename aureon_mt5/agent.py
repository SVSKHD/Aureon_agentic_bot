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
import os
import numpy as np


class SymbolAgent(threading.Thread):
    def __init__(self, symbol: str, cfg: Config, strategy: Strategy, notify: Notifier, journal: Journal, news: tuple, claude=None):
        super().__init__(daemon=True, name=f"agent-{symbol}")
        # v1.10.0 Claude add-on: hooks only when the advisor advises this strategy (ema5080, advisory/manage). None = v1.9.x behaviour.
        self.claude = claude if (claude is not None and claude.advises(strategy)) else None
        self.claude_inbox: "queue.Queue[dict]" = queue.Queue()
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
        self.caught_up = False            # late-start catch-up card sent once
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
        self.health = {"status": "starting", "last_bar": None, "last_poll": None, "errors": 0, "managed": 0,
                       "close": None, "ema_fast": None, "ema_slow": None, "offset_h": self.off, "last_signal": None,
                       "last_broker_action": None, "guardian": "ON" if g else "OFF (no profile for this mode/symbol)"}
        # v1.9.8: cached status for slash commands (replaced atomically each poll; the bot never calls MT5 for status)
        self.snapshot: dict = {"at": None}
        self._pos_seen: list | None = None
        self._today_cache: tuple = (0.0, None)
        self._stop = threading.Event()
        for t, st in list(self.state.items()):
            if st.get("mode") and st["mode"] != strategy.name:
                notify.card(ctitle(symbol, "MODE MISMATCH"), "Guardian keeps the existing SL (never loosened); no cross-strategy rules applied.",
                            fields=[field("Ticket", f"#{t}"), field("Managed by", st["mode"]), field("Runtime mode", strategy.name)],
                            color=EVENT_COLOURS["flip"], footer=cfooter(strategy.display_name))
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
                self.last_df = df
                cf, cs = self.S.ema_cols
                ef = float(df[cf].iloc[-1]); es = float(df[cs].iloc[-1]); close = float(df["close"].iloc[-1])
                sgn = 1 if ef > es else -1
                self.health.update(last_poll=datetime.now(IST).strftime("%H:%M:%S"), close=close, ema_fast=ef, ema_slow=es)
                if new_bar:
                    self.last_bar = bar_t; self.health["last_bar"] = ist(bar_t, self.off)
                    self.last_bar_wall = _time.time()
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
            except Exception as e:
                self.health["errors"] += 1; self.health["status"] = f"error: {e}"
                telemetry.failure(self.notify, self.journal, title="AUREON AGENT ERROR", key=self.key("ERROR", f"loop-{type(e).__name__}"),
                                  mode=self.S.name, symbol=self.symbol, action="loop", exc=e)
            _time.sleep(self.cfg.poll_seconds)

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
            trend = "UP ▲ (EMA 50 crossed above 80)" if long else "DOWN ▼ (EMA 50 crossed below 80)"
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
        self.notify.send(self.key("STARTUP_EARLIER", bar_key(bar_t, off)), ctitle(self.symbol, "EARLIER TODAY · 50/80 LEGS"),
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
        """AUREON-007 (advisory): today's closed losses from MT5 history across symbols."""
        today = datetime.now(IST).date()
        cache = getattr(self, "_limit_cache", None)
        if cache is not None and cache[0] == (today, self.last_bar):
            return cache[1]
        msg = None
        try:
            start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            deals = broker.closed_deals(None, start, datetime.now(timezone.utc) + timedelta(minutes=1))
            net = sum(d["profit"] for d in deals); losses = sum(1 for d in deals if d["profit"] < 0)
            acc = broker.account()
            loss_lim = (acc["balance"] * self.cfg.daily_loss_limit_pct / 100) if acc else None
            if losses >= self.cfg.daily_max_losses:
                msg = f"⛔ {losses} losing trades today (limit {self.cfg.daily_max_losses}) — I'd sit out"
            elif loss_lim and net <= -loss_lim:
                msg = f"⛔ day P&L {net:,.0f} (limit −{loss_lim:,.0f}, {self.cfg.daily_loss_limit_pct:g}%) — I'd sit out"
        except Exception:
            msg = None
        self._limit_cache = ((today, self.last_bar), msg)
        if msg:
            self.notify.send(self.key("LIMIT", str(today)), ctitle(self.symbol, "DAILY LIMIT REACHED"), [msg, "signals continue — nothing is blocked"],
                             color=EVENT_COLOURS["error"], footer=cfooter(self.S.display_name, "risk"))
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

    def _streak(self) -> str | None:
        """Consecutive losing closed trades (last 3 days, all symbols). Card once when it reaches 2."""
        cache = getattr(self, "_streak_cache", None)
        if cache is not None and cache[0] == self.last_bar:
            return cache[1]
        msg = None
        try:
            deals = sorted(broker.closed_deals(None, datetime.now(timezone.utc) - timedelta(days=3), datetime.now(timezone.utc) + timedelta(minutes=1)),
                           key=lambda d: d["time"])
            n = 0
            for d in reversed(deals):
                if d["profit"] < 0: n += 1
                else: break
            if n >= 2:
                msg = f"🔻 {n} losses in a row — next one at half size?"
                self.notify.send(self.key("STREAK", f"{n}-{deals[-1]['time']}"), ctitle(self.symbol, f"{n} LOSSES IN A ROW"),
                                 [msg, "advisory only — signals continue"], color=EVENT_COLOURS["closed_loss"], footer=cfooter(self.S.display_name, "risk"))
        except Exception:
            msg = None
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

    def _suggested(self, stop_distance: float) -> dict:
        try:
            r = broker.lot_for_risk(self.symbol, abs(stop_distance), self.cfg.risk_pct) if not self.dry else None
        except Exception:
            r = None
        return {"lots": min(r["lots"], self.cfg.max_lots) if r else None}

    def _lot_field(self, stop_distance: float):
        """AUREON-005: 'risk 1% → 0.08 lots' — display only, never sizes an order."""
        try:
            r = broker.lot_for_risk(self.symbol, abs(stop_distance), self.cfg.risk_pct) if not self.dry else None
        except Exception:
            r = None
        if not r:
            return []
        return [field("Size", f"**{r['lots']:g} lots** = {r['risk_pct']:g}% risk ({r['risk_money']:,.0f} {r['currency']}) for a {r['stop']:.1f} stop", inline=False)]

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
            self.journal.log("signal", symbol=self.symbol, mode=self.S.name, side=r.side, signal_kind="reentry", price=r.price, bar=bar_t)
            self.latest_signal = {"bar_time": bar_t, "symbol": self.symbol, "strategy": self.S.name, "kind": "reentry", "direction": r.side, "consumed": False}
            if not self.leg or self.leg["side"] != r.side:
                self.leg = {"side": r.side, "kind": "reentry", "start_t": bar_t, "start_i_time": bar_t, "start_px": r.price,
                            "best": 0.0, "best_px": r.price, "best_t": bar_t, "against": 0.0, "steps_sent": 0}
            stop = r.pullback_extreme - s_ * 0.5                       # just beyond the pullback extreme
            png = context_chart(df, self.symbol, self.S.display_name, self.S.fast, self.S.slow, self.S.ema_cols, self.off, self.chart_dir,
                                bars=120, title_extra="re-entry", reentries=[{"index": r.index, "side": r.side}],
                                marker={"index": r.index, "side": r.side, "label": "RE", "stop": stop,
                                        "secure": (r.price + s_ * g.secure_at) if g else None})
            re_key = self.key(f"RE_{r.side}", bar_key(bar_t, self.off))
            self.notify.ask(re_key, ctitle(self.symbol, "RE-ENTRY", r.side),
                            [f"pullback to EMA {self.S.fast} rejected — trend continues", f"**Trade {r.side} now?**"], png=png,
                            fields=[field("Side", f"**{r.side}**"), field("Price", f"**{r.price:.2f}**"), field("Bar", f"{ist(bar_t, self.off)} IST"),
                                    field(f"EMA {self.S.fast}", f"{r.ema50:.2f}"), field(f"EMA {self.S.slow}", f"{r.ema80:.2f}"),
                                    field("Pullback reached", f"{r.pullback_extreme:.2f}"),
                                    field("Stop", f"{stop:.2f} (beyond the pullback)", inline=False)]
                                   + ([field("Secure", f"at {r.price + s_ * g.secure_at:.2f} (+{g.secure_at:g}) → then ride", inline=False)] if g else [])
                                   + self._lot_field(r.price - stop) + self._evidence_field("reentry", r.side, bar_t) + self._context_fields(df),
                            color=EVENT_COLOURS["signal_long" if r.side == "LONG" else "signal_short"], footer=cfooter(self.S.display_name, "re-entry"),
                            meta={"symbol": self.symbol, "side": r.side, "kind": "reentry", "price": r.price, "bar": bar_t, "mode": self.S.name,
                                  "sl": stop, **self._suggested(r.price - stop)})
            if self.claude is not None:
                self.claude.request_entry(self, "reentry", r.side, bar_t, df, card_key=re_key)

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
            self._end_leg(df, bar_t, "EMA 50/80 crossed back")
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
                             color=EVENT_COLOURS["flip"], footer=cfooter(self.S.display_name, "advisor"))
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
            if self.leg and self.leg["side"] != side:
                self._end_leg(df, bar_t, f"opposite {kind} {side}")
            if not self.leg or self.leg["side"] != side:
                self.leg = {"side": side, "kind": kind, "start_t": bar_t, "start_i_time": bar_t, "start_px": close,
                            "best": 0.0, "best_px": close, "best_t": bar_t, "against": 0.0, "steps_sent": 0}
            self.health["last_signal"] = f"{side} {kind} @ {ist(bar_t, self.off)}"
            self.journal.log("signal", symbol=self.symbol, mode=self.S.name, side=side, signal_kind=kind, price=close, bar=bar_t)
            g = self.g
            fields = signal_fields(side=side, kind={"P": "P · pre-cross", "cross": "50/80 cross confirmed", "late": "late entry", "pullback": "pullback entry"}.get(kind, kind),
                                   price=close, ema_fast=ef, ema_slow=es, fast=self.S.fast, slow=self.S.slow,
                                   stop=(close - s * g.pre_stop) if g else None, secure_at=(close + s * g.secure_at) if g else None,
                                   bar_ist=ist(bar_t, self.off))
            note = "place it — I manage it" if g else "signal only — no guardian profile for this mode/symbol"
            png = context_chart(df, self.symbol, self.S.display_name, self.S.fast, self.S.slow, self.S.ema_cols, self.off, self.chart_dir,
                                marker={"index": last, "side": side, "label": kind.upper(),
                                        "stop": (close - s * g.pre_stop) if g else None, "secure": (close + s * g.secure_at) if g else None})
            fields = fields + (self._lot_field(g.pre_stop) if g else []) + self._evidence_field(kind, side, bar_t) + self._context_fields(df)
            ask_key = self.key(f"{kind.upper()}_{side}", bar_key(bar_t, self.off))
            self.notify.ask(ask_key, ctitle(self.symbol, {"P": "P PRE-CROSS", "cross": "CONFIRMED CROSS", "late": "LATE ENTRY", "pullback": "PULLBACK ENTRY"}.get(kind, kind), side), [note, f"**Trade {side} now?**"], png=png,
                             meta={"symbol": self.symbol, "side": side, "kind": kind, "price": close, "bar": bar_t, "mode": self.S.name,
                                   "sl": (close - s * g.pre_stop) if g else None, **(self._suggested(g.pre_stop) if g else {})},
                             fields=fields, color=EVENT_COLOURS["signal_long" if side == "LONG" else "signal_short"],
                             footer=cfooter(self.S.display_name, f"bar {ist(bar_t, self.off)}"))
            if self.claude is not None and kind in ("P", "cross"):          # v1.10.0: enqueue only — never blocks the poll
                self.claude.request_entry(self, kind, side, bar_t, df, card_key=ask_key)

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
            return True
        telemetry.failure(self.notify, self.journal, title="AUREON GUARDIAN FAILURE", key=self.key("ERROR", f"sl-{p['ticket']}-{action}-{r.retcode}"),
                          mode=self.S.name, symbol=self.symbol, ticket=p["ticket"], action=action, requested=r.requested_sl or sl,
                          current=r.existing_sl, bid=r.bid, ask=r.ask, retcode=r.retcode, comment=r.comment, last_error=r.last_error)
        return False

    def _close(self, p, reason, title, key, st, journal_reason=None, bar_t=None):
        event_name = {"p_flip": "FLIP CLOSE", "news_flat": "BANKED BEFORE NEWS", "claude_close": "CLAUDE CLOSED"}.get(journal_reason or "", None)
        if bar_t is not None and st.get("closing_bar") == bar_t:
            return                                      # one close attempt per bar
        st["closing_bar"] = bar_t
        r = broker.close(p["ticket"], self.symbol, "aureon") if not self.dry else broker.BrokerResult(True, "DRY", changed=True)
        if r.ok:
            self.closed_by_aureon.add(p["ticket"])
        self.health["last_broker_action"] = f"close {r.status} {datetime.now(IST):%H:%M:%S}"
        self.journal.log("exit", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], points=p["points"],
                         reason=journal_reason or reason, auto=bool(r.ok))
        if r.ok:
            self.notify.send(key, ctitle(self.symbol, event_name or ("CLOSED " + ("WIN" if p["points"] > 0 else "LOSS")), p["direction"]), [reason],
                             fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"],
                                                    points=p["points"], secured=st.get("secured"), peak=st["peak"]),
                             color=EVENT_COLOURS["closed_win" if p["points"] > 0 else "closed_loss"], footer=cfooter(self.S.display_name, journal_reason or ""))
        else:
            telemetry.failure(self.notify, self.journal, title="AUREON GUARDIAN FAILURE", key=self.key("ERROR", f"close-{p['ticket']}-{r.retcode}"),
                              mode=self.S.name, symbol=self.symbol, ticket=p["ticket"], action=f"CLOSE ({reason})", bid=r.bid, ask=r.ask,
                              retcode=r.retcode, comment=r.comment, last_error=r.last_error, retry="close by hand if it persists")

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
                if self.claude is not None:
                    self.claude.forget(self.symbol, t)
                final, approx = self._final_points(t, st)             # v1.11.0: for the weekly compare (measurement only)
                self.journal.log("closed", symbol=self.symbol, mode=self.S.name, ticket=t, direction=st.get("direction"),
                                 entry=st.get("entry"), secured=st.get("secured"), peak=st.get("peak"),
                                 final_points=final, final_approx=approx)
                if t not in self.closed_by_aureon:            # closed by you (or your own SL/TP): ask why, one tap
                    last = st.get("last_points", 0.0)
                    self.notify.ask(self.key("CLOSED_BY_YOU", t), ctitle(self.symbol, "CLOSED BY YOU", st.get("direction")),
                                    [f"last seen {last:+.2f} · peak +{st.get('peak', 0):.2f} · secured +{st.get('secured', 0):g}", "**why?** one tap — it goes in the weekly review"],
                                    fields=[field("Ticket", f"#{t}"), field("Entry", f"{st.get('entry', 0):.2f}"), field("Peak", f"+{st.get('peak', 0):.2f}")],
                                    color=EVENT_COLOURS["info"], footer=cfooter(self.S.display_name, "reason"),
                                    meta={"reason_for": "close", "symbol": self.symbol, "ticket": t, "side": (st.get("direction") or "").upper(),
                                          "bar": self.last_bar, "kind": "close", "mode": self.S.name, "price": st.get("entry")})
                self.closed_by_aureon.discard(t)
        self.health["managed"] = len(pos)
        sig = self.latest_signal
        for p in pos:
            s = 1 if p["direction"] == "long" else -1
            if p["ticket"] not in self.state:                    # v1.11.0: first sight of a trade (compare: 'Me · TAKEN')
                self.journal.log("position_seen", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"],
                                 side="LONG" if s > 0 else "SHORT", entry=p["price_open"], open_time=p.get("time"))
            st = self.state.setdefault(p["ticket"], {"symbol": self.symbol, "mode": self.S.name, "direction": p["direction"],
                                                     "entry": p["price_open"], "peak": p["points"], "pre": sgn != s, "bars": 0,
                                                     "news_done": False, "secured": 0.0})
            st["peak"] = max(st["peak"], p["points"]); st["last_points"] = p["points"]
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
                    self.notify.send(self.key("PROTECTED", p["ticket"]), ctitle(self.symbol, "PROTECTED", p["direction"]), [f"initial stop −{g.pre_stop:g}"],
                                     fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"], points=p["points"], sl=sl),
                                     color=EVENT_COLOURS["protected"], footer=cfooter(self.S.display_name))

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
                self.notify.send(self.key("CROSS", p["ticket"]), ctitle(self.symbol, "CROSS CONFIRMED", p["direction"]),
                                 [f"EMA {self.S.fast} crossed {'above' if s > 0 else 'below'} EMA {self.S.slow}" + ("" if ok else " · SL move pending — see failure alert")],
                                 fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"], points=p["points"], sl=sl,
                                                        extra={"Next": f"secure at {p['price_open'] + s * g.secure_at:.2f} (+{g.secure_at:g})"}),
                                 color=EVENT_COLOURS["cross"], footer=cfooter(self.S.display_name))
            elif not st["pre"] and st["secured"] < g.secure_level and new_bar:
                self._move_sl(p, es - s * g.ema_slow_sl_buffer, "FOLLOW EMA80", quiet=True)   # same safe path: retcode, telemetry, never-loosen

            # 3) SECURE — first priority; announced only when confirmed
            if st["secured"] < g.secure_level and p["points"] >= g.secure_at:
                sl = p["price_open"] + s * g.secure_level
                if self._move_sl(p, sl, f"SECURE +{g.secure_level:g}"):
                    st["secured"] = g.secure_level                                             # 1. state
                    if self.cfg.partial_close_pct > 0 and not st.get("partial_done") and not self.dry:                # AUREON-012
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
                    self.journal.log("secured", symbol=self.symbol, mode=self.S.name, ticket=p["ticket"], level=g.secure_level, step=g.secure_level, price=p["current"])  # 2. journal
                    self.notify.send(self.key("SECURED", f"{p['ticket']}-{g.secure_level:g}"), ctitle(self.symbol, f"SECURED +{g.secure_level:g}", p["direction"]), ["riding — lock steps up every +" + f"{g.ride_step:g}"],
                                     fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"], points=p["points"], sl=sl, secured=g.secure_level, peak=st["peak"]),
                                     color=EVENT_COLOURS["secured"], footer=cfooter(self.S.display_name))
            # 4) ride
            elif st["secured"] >= g.secure_level:
                nxt = st["secured"] + g.ride_step
                if p["points"] >= nxt + g.ride_step * 0.4:
                    sl = p["price_open"] + s * nxt
                    if self._move_sl(p, sl, f"RIDE +{nxt:g}"):
                        st["secured"] = nxt
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
                if st["secured"] >= g.secure_level and against_f:
                    reason, jr = f"bar closed through EMA {self.S.fast} ({ef:.2f}) — ride over", "ema_fast_turn"
                elif st["pre"] and p["points"] <= -g.pre_stop and not (
                        self.cfg.respect_manual_sl and p["sl"] and abs(p["price_open"] - p["sl"]) > g.pre_stop + 1e-6):
                    reason, jr = f"P failed: {p['points']:+.2f}, lines not crossed", "pre_stop"
                elif separating and st["bars"] >= 2:
                    reason, jr = f"P aborted: EMA {self.S.fast}/{self.S.slow} separating again before the cross", "pre_abort"
                elif st["pre"] and st["bars"] >= g.pre_timeout_bars and st["secured"] == 0 and not st.get("timeout_hold"):
                    if p["points"] > 0 or (self.cfg.respect_manual_sl and p.get("tp")):
                        # in profit, or you set your own TP: hold — lock breakeven instead of closing
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
                elif not st["pre"] and st["secured"] < g.secure_level and against_s:
                    reason, jr = f"bar closed through EMA {self.S.slow} ({es:.2f})", "ema_slow_stop"
                if reason:
                    self._close(p, reason, f"{'✅' if p['points'] > 0 else '🛑'} CLOSED — {tag}", self.key("CLOSED", f"{p['ticket']}-{bar_key(bar_t, self.off)}"), st, jr, bar_t)
                    continue

            # 5b) advisor + periodic snapshot (notifications only — no broker action)
            if new_bar:
                if not st.get("tracking_sent"):
                    st["tracking_sent"] = True
                    self.notify.send(self.key("TRACKING", p["ticket"]), ctitle(self.symbol, "TRACKING", p["direction"]),
                                     ["I see your position — managing it from here"],
                                     fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"],
                                                            points=p["points"], sl=p["sl"] or None),
                                     color=EVENT_COLOURS["cross"], footer=cfooter(self.S.display_name))
                codes = self._advise(p, st, bar_t, df, ef, es, g)
                if self.claude is not None:                                  # v1.10.0: enqueue only
                    self.claude.maybe_pullback(self, p, st, bar_t, df, codes or ())
                if st["bars"] and st["bars"] % self.SNAPSHOT_BARS == 0:
                    self._snapshot(p, st, bar_t)

            # 6) news safeguard
            tutc = int(_time.time())
            if not st["news_done"] and any(r - g.news_flat_min * 60 <= tutc < r for r in self.news):
                st["news_done"] = True
                if p["points"] >= g.secure_at * 0.1:
                    self._close(p, "news in <15 min", f"📰 BANKED before news — {tag}", self.key("NEWS", p["ticket"]), st, "news_flat", bar_t)
                elif st["secured"] == 0:
                    if self._move_sl(p, p["price_open"] - s * g.pre_stop, "NEWS CAP"):
                        self.notify.send(self.key("NEWS", p["ticket"]), ctitle(self.symbol, f"NEWS <{g.news_flat_min} MIN", p["direction"]), [f"loss capped at −{g.pre_stop:g}"],
                                         fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"], points=p["points"], sl=p["price_open"] - s * g.pre_stop),
                                         color=EVENT_COLOURS["news"], footer=cfooter(self.S.display_name))
        if sig and not sig["consumed"] and sig["bar_time"] == bar_t:
            sig["consumed"] = True                       # nothing to flip; signal spent
        if self.claude is not None:                      # v1.10.0: Claude verdicts AFTER the guardian — its hard exits win
            self.claude.drain(self, pos)
        self.journal.save_state(self.symbol, self.state)
