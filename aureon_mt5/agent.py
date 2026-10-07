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
from .notify import Notifier, signal_fields, position_fields, field, title as ctitle, footer as cfooter, EVENT_COLOURS
from .strategies import Strategy
from .common.source import Bars
from .common.timeutil import IST, bar_key, ist_str as ist
from .common.news import session_of
from .common.context_chart import context_chart
import os
import numpy as np


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
        self.caught_up = False            # late-start catch-up card sent once
        self.leg: dict | None = None      # the move since the last P / cross signal (progress updates)
        self.MOVE_STEP = 10.0 if not symbol.upper().startswith("XAG") else 0.15   # progress card every +step of move
        self.SNAPSHOT_BARS = 6            # open-position snapshot every 6 closed bars (30 min)
        self.chart_dir = os.path.join(cfg.log_dir, "charts")
        self.last_df = None               # last closed-bar frame with EMAs (for /chart)
        self.health = {"status": "starting", "last_bar": None, "last_poll": None, "errors": 0, "managed": 0,
                       "close": None, "ema_fast": None, "ema_slow": None, "offset_h": self.off, "last_signal": None,
                       "last_broker_action": None, "guardian": "ON" if g else "OFF (no profile for this mode/symbol)"}
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
                    _time.sleep(30); continue
                if announced_closed:
                    announced_closed = False
                    self.notify.send(self.key("MARKET", f"open-{datetime.now(IST):%Y-%m-%d-%H}"), ctitle(self.symbol, "MARKET OPEN"), ["Aureon resumed"], color=EVENT_COLOURS["secured"], footer=cfooter(self.S.display_name))
                self.health["status"] = "running"
                m5 = broker.bars(self.symbol, self.cfg.bars, self.cfg.source, self.off)
                closed = m5.df[m5.df["time"] + 300 <= broker.now_server(self.off)].reset_index(drop=True)
                if len(closed) < 120:
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
                    if not self.caught_up:
                        self.caught_up = True
                        try:
                            self._catch_up(closed, df, bar_t, close, ef, es)
                        except Exception as e:
                            telemetry.failure(self.notify, self.journal, title="AUREON CATCH-UP ERROR", key=self.key("ERROR", "catchup"),
                                              mode=self.S.name, symbol=self.symbol, action="catch-up", exc=e)
                    self._detect(closed, df, bar_t, close, ef, es)
                self._track_leg(df, bar_t, new_bar, sgn)
                if self.g is not None:
                    self._guard(new_bar, bar_t, df, close, ef, es, sgn)
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
        self.journal.log("leg", symbol=self.symbol, mode=self.S.name, side=lg["side"], signal_kind=lg["kind"], start=lg["start_px"],
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
            self.notify.send(self.key(f"{kind.upper()}_{side}", bar_key(bar_t, self.off)), ctitle(self.symbol, {"P": "P PRE-CROSS", "cross": "CONFIRMED CROSS", "late": "LATE ENTRY", "pullback": "PULLBACK ENTRY"}.get(kind, kind), side), [note], png,
                             fields=fields, color=EVENT_COLOURS["signal_long" if side == "LONG" else "signal_short"],
                             footer=cfooter(self.S.display_name, f"bar {ist(bar_t, self.off)}"))

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
        event_name = {"p_flip": "FLIP CLOSE", "news_flat": "BANKED BEFORE NEWS"}.get(journal_reason or "", None)
        if bar_t is not None and st.get("closing_bar") == bar_t:
            return                                      # one close attempt per bar
        st["closing_bar"] = bar_t
        r = broker.close(p["ticket"], self.symbol, "aureon") if not self.dry else broker.BrokerResult(True, "DRY", changed=True)
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

            # 5b) advisor + periodic snapshot (notifications only — no broker action)
            if new_bar:
                if not st.get("tracking_sent"):
                    st["tracking_sent"] = True
                    self.notify.send(self.key("TRACKING", p["ticket"]), ctitle(self.symbol, "TRACKING", p["direction"]),
                                     ["I see your position — managing it from here"],
                                     fields=position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"],
                                                            points=p["points"], sl=p["sl"] or None),
                                     color=EVENT_COLOURS["cross"], footer=cfooter(self.S.display_name))
                self._advise(p, st, bar_t, df, ef, es, g)
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
        self.journal.save_state(self.symbol, self.state)
