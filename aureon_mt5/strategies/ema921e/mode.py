"""v3.0 ema921e — the bar-close runner around EngineE (engine.py is the spec; nothing here decides a trade).

    on each new M30 bar     the previous bar is closed → EngineE.on_bar(bar) → its actions, immediately and in order:
        enter    market order at `lot`, broker SL at the 3×ATR disaster stop only, no TP
        floor    bot-side: the live tick is checked between bar closes and the position is closed at market the moment
                 price trades through it; the broker SL follows the floor only once the floor is ≥ the second rung
        exit     cut / cross (and floor / stop on the bar that touched them): market close now
        skip     logged (logs/ema921e_skips.csv) and carded
    startup                 warmup() with the last 3,000 closed bars, then reconcile with the open MT5 position:
                            MT5 has one, the engine none → adopt it (entry = open price, bars_in from the open time, the
                            floor rebuilt by replaying the bars since through the engine); the state file expected one and
                            MT5 has none → log and continue flat
    guardian / drawdown guard / regime gate   SHOW only: logged with every entry, never override the engine

Execution realities the engine cannot see are handled by dropping the engine's position (engine.pos = None) — the
engine's own logic is never changed: an entry refused (switched off, gate, broker reject, stale bar after a reconnect),
or a position closed outside the engine (/e flat, by hand). Each one is journaled.
"""
from __future__ import annotations

import json
import os
import threading
import time as _time
from datetime import datetime, timezone

from .brokers import TF_SECONDS
from .engine import Action, EngineE, Params, Position

WARMUP_BARS = 3000
USD_PER_POINT_PER_LOT = 100.0          # gold


def params_from(settings: dict) -> Params:
    """The engine Params for a settings dict (config/ema921e.yaml keys)."""
    hours = settings.get("hours") or [0, 24]
    return Params(spike=float(settings.get("spike", 0.15)), no_monday=bool(settings.get("no_monday", True)),
                  hours_from=int(hours[0]), hours_to=int(hours[1]), atr_gate=float(settings.get("atr_gate", 0.7)),
                  month_stop_per_lot=float(settings.get("month_stop_per_lot", 2000)), cut_bars=int(settings.get("cut_bars", 2)),
                  ladder=tuple((float(t), float(l)) for t, l in (settings.get("ladder") or ((0.25, 0.05), (0.5, 0.25), (1.0, 0.5)))),
                  step=float(settings.get("step", 0.5)), stop_atr=float(settings.get("stop_atr", 3.0)),
                  lot=float(settings.get("lot", 0.3)))


def second_rung(p: Params) -> float:
    """The lock of the second ladder rung (+0.25 ATR in the locked set): the broker SL follows the floor from here on."""
    return float(sorted(p.ladder)[1][1]) if len(p.ladder) > 1 else float(p.ladder[0][1])


class EMode(threading.Thread):
    """One XAUUSD ema921e runner. Testable without a thread: startup(), poll_bars(), poll_tick(), flat()."""

    def __init__(self, cfg, settings: dict, broker, *, journal=None, log_dir: str = "logs", tick_s: float = 1.0, bar_poll_s: float = 5.0,
                 config_path: str | None = None):
        super().__init__(daemon=True, name="ema921e")
        self.cfg, self.settings, self.broker, self.journal = cfg, dict(settings), broker, journal
        self.symbol = str(self.settings.get("symbol", "XAUUSD")).upper()
        self.tf = str(self.settings.get("timeframe", "M30")).upper()
        self.bar_s = TF_SECONDS[self.tf]
        self.log_dir = log_dir; self.tick_s = tick_s; self.bar_poll_s = bar_poll_s
        self.state_path = os.path.join(log_dir, "ema921e_state.json")
        self.st = self._load_state()                       # enabled, ticket, entry fill, equity peak …
        self.eng = EngineE(self.symbol, params_from(self.settings))
        self.times: list[int] = []                         # server epoch of every bar fed to the engine (parallel to eng.t)
        self.last_bar: int | None = None
        self.listeners: list = []                          # fn(kind, data) — cards (step 4), CSV logs (step 5)
        self.intrabar_exit: dict | None = None            # the tick-floor close, recorded when the engine's exit arrives
        self.health = {"status": "starting", "broker": getattr(broker, "name", "?")}
        self.regime = None
        self._last_mfe = None
        self.off = float(getattr(broker, "off", None) or getattr(cfg, "server_utc_offset", 3.0) or 0.0)   # server − UTC, h
        self.skip_log: list[tuple] = []                    # (server ts, reason) — /e status, the daily summary
        self.trade_rows: list[dict] = []                   # closed trades this process knows (+ the CSV, step 5)
        self.summary_at = str(getattr(cfg, "daily_report_ist", "23:00") or "23:00")
        from .logs import TradeLogs
        self.trade_logs = TradeLogs(self)                  # logs/ema921e_trades.csv + logs/ema921e_skips.csv (always on)
        self.config_path = config_path
        from .config import ConfigWatcher
        self.watcher = ConfigWatcher(config_path) if config_path else None
        self._stop_evt = threading.Event()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ plumbing
    def stop(self):
        self._stop_evt.set()

    @property
    def enabled(self) -> bool:
        return bool(self.st.get("enabled", False))

    def _load_state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                st = json.load(f)
            return st if isinstance(st, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_state(self):
        os.makedirs(self.log_dir, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.st, f, indent=1, default=str)
        os.replace(tmp, self.state_path)

    def log(self, event: str, **kw):
        if self.journal is not None:
            try:
                self.journal.log(event, symbol=self.symbol, mode="ema921e", **kw)
            except Exception:
                pass

    def emit(self, kind: str, **data):
        data.setdefault("symbol", self.symbol)
        if kind == "skip":
            self.skip_log.append((int(self.last_bar or self.broker.now_server()), str(data.get("reason"))))
            del self.skip_log[:-500]
        elif kind == "exit":
            self.trade_rows.append({k: data.get(k) for k in ("time", "exit_time", "dir", "reason", "pnl_pts", "pnl_usd", "bars")})
        for fn in list(self.listeners):
            try:
                fn(kind, data)
            except Exception as e:                       # a card / CSV failure never stops trading
                self.log("ema921e_listener_error", kind=kind, error=repr(e))

    def _closed(self, rows: list[dict]) -> list[dict]:
        """A bar is closed once a later bar exists or its end has passed on the server clock."""
        if not rows:
            return []
        now = self.broker.now_server()
        return rows[:-1] + ([rows[-1]] if int(rows[-1]["time"]) + self.bar_s <= now else [])

    def _feed(self, bar: dict) -> list[Action]:
        before = self.eng.pos
        acts = self.eng.on_bar(_eng_bar(bar))
        self.times.append(int(bar["time"])); self.last_bar = int(bar["time"])
        if before is not None and self.eng.pos is not before and self.eng.closed_trades:
            self._last_mfe = self.eng.closed_trades[-1]["mfe_atr"] * before.atr     # the engine's MFE, in points
        return acts

    def _drop_engine_position(self, why: str):
        if self.eng.pos is not None:
            self.log("ema921e_engine_flat", why=why, entry=self.eng.pos.entry, side=self.eng.pos.side)
        self.eng.pos = None

    def set_enabled(self, on: bool):
        """/e on · /e off — new entries only; an open position keeps being managed either way."""
        with self._lock:
            self.st["enabled"] = bool(on); self.save_state()
            self.log("ema921e_enabled", enabled=bool(on))

    @property
    def lot(self) -> float:
        return float(self.settings.get("lot", 0.3))

    # ------------------------------------------------------------------ startup
    def startup(self):
        rows = self.broker.bars(WARMUP_BARS + 5)
        closed = self._closed(rows)[-WARMUP_BARS:]
        if not closed:
            raise RuntimeError("ema921e: no closed bars")
        live = self.broker.positions()
        if live:
            self._adopt(live[0], closed)
        else:
            self.eng.warmup(_eng_bar(b) for b in closed)
            self.times = [int(b["time"]) for b in closed]; self.last_bar = self.times[-1]
            if self.st.get("ticket"):
                self.log("ema921e_expected_position_missing", ticket=self.st.get("ticket"))
                self.emit("note", text=f"state expected #{self.st.get('ticket')} — none in MT5, continuing flat")
                self._clear_trade()
        self._restore_logs()
        self._restore_month()
        self.health["status"] = "running"
        self.log("ema921e_start", bars=len(self.times), broker=self.health["broker"], enabled=self.enabled,
                 in_position=self.eng.pos is not None, settings=self.settings)

    def _adopt(self, p: dict, closed: list[dict]):
        """MT5 holds an ema921e position the fresh engine does not know: entry = the open price, the entry bar = the last bar
        that closed at or before the open time, then the bars since replayed through the engine itself (its MFE / floor /
        exit rules). An exit found in that replay happened while we were down → closed now."""
        times = [int(b["time"]) for b in closed]
        k = max((j for j, t in enumerate(times) if t + self.bar_s <= int(p["time"])), default=0)
        side = 1 if p["direction"] == "long" else -1
        self.eng.warmup(_eng_bar(b) for b in closed[:k + 1])
        self.times = times[:k + 1]; self.last_bar = times[k]
        self.eng.pos = Position(i=k, side=side, entry=float(p["price_open"]), atr=float(self.eng.atr[k]), opened=self.eng.t[k])
        same = int(self.st.get("ticket") or 0) == int(p["ticket"])
        self.st.update(ticket=int(p["ticket"]), entry_fill=float(p["price_open"]),
                       entry_req=float(self.st.get("entry_req") or p["price_open"]) if same else float(p["price_open"]),
                       side=side, opened_bar=times[k], broker_floor=float(self.st.get("broker_floor") or 0.0) if same else 0.0)
        self.save_state()
        missed = []
        for b in closed[k + 1:]:
            for a in self._feed(b):
                if a.kind in ("exit", "floor"):
                    missed.append(a)
                elif a.kind == "enter":                   # a later cross after a missed exit: never taken late
                    self._drop_engine_position("stale entry during adoption replay")
        s = self.eng.status()
        self.log("ema921e_adopted", ticket=p["ticket"], entry=p["price_open"], side=side, entry_bar=times[k],
                 bars_in=s.get("bars_in"), floor_px=s.get("floor_px"), mfe_pts=s.get("mfe_pts"))
        self.emit("adopt", ticket=p["ticket"], side=side, entry=float(p["price_open"]), bars_in=s.get("bars_in"),
                  floor_px=s.get("floor_px"))
        exits = [a for a in missed if a.kind == "exit"]
        if exits:                                          # the engine says it should already be out
            self._exit(exits[-1], late=True)
        elif s.get("floor_px") is not None:
            self._sync_broker_floor()

    def _restore_logs(self):
        """/e status, the daily summary and the month stop read the CSVs back after a restart (rows emitted before this
        point in startup — an adoption's late exit — are already in the file, so the file is the whole truth)."""
        from .logs import read_skips, read_trades
        keep = ("time", "exit_time", "dir", "reason", "pnl_pts", "pnl_usd", "bars")
        self.trade_rows = [{k: r.get(k) for k in keep} for r in read_trades(self.log_dir)]
        self.skip_log = read_skips(self.log_dir)[-500:]

    def _restore_month(self):
        """The month-stop needs this month's P&L; warmup() cannot know it — rebuilt from the trades CSV (entry month, as the
        engine buckets it)."""
        now = datetime.fromtimestamp(self.broker.now_server(), tz=timezone.utc)
        self.eng.month = (now.year, now.month)
        ym = f"{now.year:04d}-{now.month:02d}"
        self.eng.month_pnl = sum(float(r.get("pnl_usd") or 0.0) for r in self.trade_rows if str(r.get("time") or "").startswith(ym))

    # ------------------------------------------------------------------ the bar loop
    def poll_bars(self) -> int:
        """Feed every newly closed bar. Bars older than the latest closed one (a reconnect) are fed for the indicators,
        their exits executed, their entries not taken late. Returns the number of bars fed."""
        gap = 0 if self.last_bar is None else max(0, (self.broker.now_server() - self.last_bar) // self.bar_s)
        rows = self.broker.bars(int(min(WARMUP_BARS, gap + 8)))       # a reconnect / the weekend: every bar since
        new = [b for b in self._closed(rows) if self.last_bar is None or int(b["time"]) > self.last_bar]
        for j, b in enumerate(new):
            self.on_bar(b, stale=j < len(new) - 1)
        return len(new)

    def on_bar(self, bar: dict, *, stale: bool = False):
        with self._lock:
            self._check_external_close()
            had = self.eng.pos is not None
            acts = self._feed(bar)
            self._show_gates(bar)
            for a in acts:
                self._execute(a, stale=stale)
            if had and self.eng.pos is not None and not self.st.get("ticket") and self.intrabar_exit is None:
                self._drop_engine_position("closed outside the engine")
            if self.intrabar_exit is not None and self.eng.pos is None:
                self.intrabar_exit = None
            elif self.intrabar_exit is not None and int(self.intrabar_exit.get("t") or 0) < int(bar["time"]) + self.bar_s:
                ie = self.intrabar_exit; self.intrabar_exit = None   # out during this bar, the engine did not exit on it
                self._record_trade(reason="floor" if ie["by"] in ("tick", "broker_sl") else "external", req=ie["req"],
                                   fill=ie["fill"], bars=self.eng.status().get("bars_in"), mfe=self._pos_mfe())
                self._drop_engine_position(f"closed during the bar ({ie['by']}), no engine exit on it")

    def _execute(self, a: Action, *, stale: bool):
        if a.kind == "skip":
            self.emit("skip", reason=a.reason, side=a.side, price=a.price, bar_time=a.bar_time, **a.extra)
        elif a.kind == "enter":
            self._enter(a, stale=stale)
        elif a.kind == "floor":
            if not self.st.get("ticket"):
                return
            self.emit("floor", side=a.side, price=a.price, lock_atr=float(a.reason.split("+")[1].split()[0]),
                      floor_pts=a.extra.get("floor_pts"), ticket=self.st.get("ticket"))
            self._sync_broker_floor()
        elif a.kind == "exit":
            self._exit(a)

    # ------------------------------------------------------------------ enter
    def _refusal(self) -> str | None:
        if not self.enabled:
            return "off"
        if self.st.get("ticket"):
            return "position_open"
        kind = self.broker.account_kind() if self.broker.name != "SIM" else "demo"
        if self.broker.name != "SIM":
            if not getattr(self.cfg, "execution_enabled", False):
                return "execution_off"
            if self.settings.get("paper", True) and kind != "demo":
                return "paper_needs_demo"
            if not self.settings.get("paper", True) and kind != "demo" and not getattr(self.cfg, "allow_live", False):
                return "live_needs_allow_live"
        return None

    def _enter(self, a: Action, *, stale: bool):
        side = "LONG" if a.side > 0 else "SHORT"
        why = "stale" if stale else self._refusal()
        if why:
            self._drop_engine_position(f"entry refused: {why}")
            self.emit("skip", reason=why, side=a.side, price=a.price, bar_time=a.bar_time, refused=True)
            return
        sl = a.extra.get("stop_px")
        r = self.broker.open(side, self.lot, sl)
        if not r.ok:
            self._drop_engine_position(f"order failed: {r.status}")
            self.log("ema921e_order_failed", side=side, status=r.status, retcode=r.retcode, comment=r.comment)
            self.emit("skip", reason=f"order_failed:{r.status}", side=a.side, price=a.price, bar_time=a.bar_time, refused=True)
            return
        fill = float(r.price or a.price)
        self.st.update(ticket=int(r.ticket), entry_fill=fill, entry_req=float(a.price), side=a.side, opened_bar=self.last_bar,
                       broker_floor=0.0)
        self.save_state()
        self.log("ema921e_enter", side=side, req=a.price, fill=fill, ticket=r.ticket, lots=self.lot, sl=sl, atr=a.extra.get("atr"),
                 fill_mode=getattr(r, "fill_mode", None), gates=self._gates_now())
        self.emit("enter", side=a.side, price=fill, req=a.price, atr=a.extra.get("atr"), cut_at_bar=a.extra.get("cut_at_bar"),
                  ladder_pts=a.extra.get("ladder_pts"), stop_px=sl, ticket=r.ticket, lots=self.lot, bar_time=a.bar_time)

    # ------------------------------------------------------------------ floor
    def _sync_broker_floor(self):
        """The broker SL follows the bot-side floor only once the floor is ≥ the second rung (never loosened)."""
        ps = self.eng.pos; tk = self.st.get("ticket")
        if ps is None or not tk or ps.floor_atr is None or ps.floor_atr + 1e-9 < second_rung(self.eng.p):
            return
        fpx = float(ps.floor_price)
        if self.st.get("broker_floor") and abs(float(self.st["broker_floor"]) - fpx) < 1e-9:
            return
        r = self.broker.set_sl(int(tk), fpx)
        self.log("ema921e_broker_floor", ticket=tk, sl=fpx, status=r.status, ok=bool(r.ok))
        if r.ok:
            self.st["broker_floor"] = fpx; self.save_state()

    def poll_tick(self) -> bool:
        """Between bar closes: price traded through the floor → market close now. True when it closed."""
        with self._lock:
            ps = self.eng.pos; tk = self.st.get("ticket")
            if ps is None or not tk or self.intrabar_exit is not None:
                return False
            t = self.broker.tick()                          # SimBroker settles its broker SL inside tick()
            fpx = ps.floor_price
            if not t or fpx is None or (self.last_bar is not None and int(t["time"]) < self.last_bar + self.bar_s):
                return False                                # a tick of a bar the engine has already closed
            bid = float(t["bid"])                           # the bars are bid bars: same touch rule as the backtest
            if not ((bid <= fpx) if ps.side > 0 else (bid >= fpx)):
                return False
            r = self.broker.close(int(tk))
            if not r.ok:
                if r.status == "POSITION_NOT_FOUND":          # the broker SL at the floor got there first
                    px = self.broker.close_price(int(tk))
                    self.intrabar_exit = {"req": fpx, "fill": float(px if px is not None else fpx), "by": "broker_sl", "t": int(t["time"])}
                    self.st["ticket"] = None; self.save_state()
                    return True
                self.log("ema921e_close_failed", ticket=tk, status=r.status, retcode=r.retcode, comment=r.comment, why="floor")
                return False
            self.intrabar_exit = {"req": fpx, "fill": float(r.price or bid), "by": "tick", "t": int(t["time"])}
            self.st["ticket"] = None; self.save_state()
            self.log("ema921e_floor_hit", ticket=tk, floor=fpx, bid=bid, fill=self.intrabar_exit["fill"])
            return True

    # ------------------------------------------------------------------ exit
    def _check_external_close(self):
        """The ticket vanished (the disaster stop / floor broker SL, or a close by hand): remember its fill for the engine's exit."""
        tk = self.st.get("ticket")
        if not tk or self.intrabar_exit is not None:
            return
        if any(int(p["ticket"]) == int(tk) for p in self.broker.positions()):
            return
        px = self.broker.close_price(int(tk))
        self.log("ema921e_closed_outside", ticket=tk, price=px)
        self.intrabar_exit = {"req": None, "fill": px, "by": "broker", "t": int(self.broker.now_server())}
        self.st["ticket"] = None; self.save_state()

    def _exit(self, a: Action, *, late: bool = False):
        bars = a.extra.get("bars")
        if self.intrabar_exit is not None:                  # already out between the closes (tick floor / broker SL)
            ie = self.intrabar_exit; self.intrabar_exit = None
            fill = ie["fill"] if ie["fill"] is not None else a.price
            self._record_trade(reason=a.reason, req=a.price, fill=fill, bars=bars, mfe=self._last_mfe)
            return
        tk = self.st.get("ticket")
        if not tk:
            return                                          # the engine's position was never filled
        r = self.broker.close(int(tk))
        if not r.ok and r.status != "POSITION_NOT_FOUND":
            self.log("ema921e_close_failed", ticket=tk, status=r.status, retcode=r.retcode, comment=r.comment, why=a.reason)
            self.emit("error", text=f"close #{tk} failed ({r.status}) — retrying next tick")
            self.st["close_retry"] = {"reason": a.reason, "req": a.price, "bars": bars, "mfe": self._last_mfe}; self.save_state()
            return
        fill = float(r.price) if (r.ok and r.price) else (self.broker.close_price(int(tk)) or a.price)
        self._record_trade(reason=a.reason + (" (late)" if late else ""), req=a.price, fill=fill, bars=bars,
                           mfe=a.extra.get("mfe", self._last_mfe))

    def _pos_mfe(self):
        ps = self.eng.pos
        return ps.mfe_atr * ps.atr if ps is not None else None

    def _record_trade(self, *, reason: str, req, fill: float, bars, mfe=None):
        d = int(self.st.get("side") or 0); e = float(self.st.get("entry_fill") or 0.0)
        pts = (float(fill) - e) * d
        slip = ((float(req) - float(fill)) * d) if req is not None else 0.0     # + = filled worse than the engine's price
        row = {"symbol": self.symbol, "time": _fmt(self.st.get("opened_bar")), "dir": "LONG" if d > 0 else "SHORT", "entry": e,
               "skipped": "", "exit_time": _fmt(self.last_bar), "exit": float(fill), "reason": reason, "bars": bars,
               "mfe_pts": mfe, "pnl_pts": pts, "pnl_usd": pts * self.lot * USD_PER_POINT_PER_LOT, "fill_slippage_pts": slip}
        self.log("ema921e_exit", **row, ticket=self.st.get("ticket"))
        self.emit("exit", **row)
        self._clear_trade()

    def _clear_trade(self):
        for k in ("ticket", "entry_fill", "entry_req", "side", "opened_bar", "broker_floor", "close_retry"):
            self.st.pop(k, None)
        self.save_state()

    def flat(self, why: str = "/e flat") -> str:
        """Close now. The engine's position is dropped (closed outside the engine's rules)."""
        with self._lock:
            tk = self.st.get("ticket")
            if not tk:
                self._drop_engine_position(why)
                return "flat already"
            r = self.broker.close(int(tk))
            if not r.ok and r.status != "POSITION_NOT_FOUND":
                return f"close #{tk} failed: {r.status} {r.comment}"
            fill = float(r.price) if (r.ok and r.price) else (self.broker.close_price(int(tk)) or 0.0)
            self._record_trade(reason="flat", req=None, fill=fill, bars=self.eng.status().get("bars_in"), mfe=self._pos_mfe())
            self._drop_engine_position(why)
            return f"closed #{tk} @ {fill:.2f}"

    # ------------------------------------------------------------------ show-only gates
    def _gates_now(self) -> dict:
        return {"regime": (self.regime or {}).get("state"), "dd": self.st.get("dd_show")}

    def _show_gates(self, bar):
        """Drawdown guard and regime gate in SHOW mode: computed, logged, never acted on."""
        try:
            eq = self.broker.equity()
            limit = float(getattr(self.cfg, "max_dd_usd", 0) or 0)
            if eq is not None:
                peak = max(float(self.st.get("equity_peak") or 0.0), eq); self.st["equity_peak"] = peak
                dd = peak - eq
                self.st["dd_show"] = {"equity": eq, "peak": peak, "dd": dd, "limit": limit, "would_block": bool(limit and dd >= limit)}
        except Exception:
            pass
        try:
            import pandas as pd
            from ...regime import RegimeTracker
            if getattr(self, "_regime_tr", None) is None:
                self._regime_tr = RegimeTracker(self.cfg, None, self.symbol)
            n = min(300, len(self.eng.c))
            df = pd.DataFrame({"time": self.times[-n:], "open": self.eng.o[-n:], "high": self.eng.h[-n:], "low": self.eng.l[-n:],
                               "close": self.eng.c[-n:], "ema9": self.eng.ema9[-n:], "ema21": self.eng.ema21[-n:]})
            self.regime = self._regime_tr.update(df, int(bar["time"]), "ema9", "ema21")
        except Exception:
            self.regime = None

    # ------------------------------------------------------------------ status / thread
    def status(self) -> dict:
        s = self.eng.status()
        s.update(enabled=self.enabled, broker=self.health["broker"], ticket=self.st.get("ticket"),
                 entry_fill=self.st.get("entry_fill"), regime=(self.regime or {}).get("state"), dd=self.st.get("dd_show"),
                 paper=bool(self.settings.get("paper", True)), last_bar=_fmt(self.last_bar))
        return s

    def run(self):
        try:
            self.startup()
        except Exception as e:
            self.health["status"] = f"startup failed: {e}"; self.log("ema921e_startup_failed", error=repr(e))
            self.emit("error", text=f"startup failed: {e}")
            return
        next_bar = 0.0
        while not self._stop_evt.is_set():
            try:
                if not self.broker.market_open():
                    self.health["status"] = "market closed"; self._stop_evt.wait(30); continue
                self.health["status"] = "running"
                cr = self.st.get("close_retry")
                if cr and self.st.get("ticket"):
                    self._exit(Action("exit", int(self.st.get("side") or 0), float(cr["req"]), cr["reason"],
                                      extra={"bars": cr.get("bars"), "mfe": cr.get("mfe")}))
                due = self.last_bar is None or self.broker.now_server() >= self.last_bar + 2 * self.bar_s
                if due or _time.monotonic() >= next_bar:      # a bar just closed: feed it before any tick check
                    next_bar = _time.monotonic() + self.bar_poll_s
                    self.poll_bars()
                    due = self.last_bar is None or self.broker.now_server() >= self.last_bar + 2 * self.bar_s
                if not due:                                    # the floor in force is the one set at the last close
                    self.poll_tick()
                self.on_loop()
            except Exception as e:
                self.health["status"] = f"error: {e}"; self.log("ema921e_loop_error", error=repr(e))
                self._stop_evt.wait(5)
            self._stop_evt.wait(self.tick_s)

    def on_loop(self, now: float | None = None):
        """Per-loop: config hot reload (an edit of config/ema921e.yaml takes effect on the next loop) · the 23:00 IST summary."""
        self.summary_tick(now)
        if self.watcher is not None:
            new = self.watcher.poll()
            if new is not None:
                self.apply_settings(new, source="file")
            elif self.watcher.last_problems and not getattr(self, "_reported_problems", None) == self.watcher.last_problems:
                self._reported_problems = list(self.watcher.last_problems)
                self.log("ema921e_config_problem", problems=self.watcher.last_problems)
                self.emit("error", text="config: " + "; ".join(self.watcher.last_problems))

    def summary_tick(self, now: float | None = None) -> str | None:
        """Once per IST day at/after daily_report_ist (23:00): the day card. Persisted, so a restart does not repeat it."""
        from .cards import IST, daily_summary
        t = datetime.fromtimestamp(_time.time() if now is None else now, tz=IST)
        day = t.strftime("%Y-%m-%d")
        if t.strftime("%H:%M") < self.summary_at or self.st.get("summary_sent") == day:
            return None
        text = daily_summary(self.trade_rows, self.skip_log, day=day, off_h=self.off, lot=self.lot)
        self.st["summary_sent"] = day; self.save_state()
        self.log("ema921e_daily", day=day, text=text)
        self.emit("summary", day=day, text=text)
        return text

    # ------------------------------------------------------------------ hot config
    def apply_settings(self, new: dict, *, source: str) -> dict:
        """Swap the running settings and the engine's Params (the open position keeps its entry ATR; a new ladder / step /
        cut applies from the next bar). symbol / timeframe changes are ignored until a restart. Returns the changes."""
        from .config import RESTART_ONLY
        with self._lock:
            changes = {k: (self.settings.get(k), v) for k, v in new.items() if self.settings.get(k) != v and k not in RESTART_ONLY}
            if not changes:
                return {}
            self.settings.update({k: v for k, (_, v) in changes.items()})
            self.eng.p = params_from(self.settings)
            self.log("ema921e_config", source=source, changes=changes)
            self.emit("config", source=source, changes=changes)
            return changes

    def set(self, key: str, value) -> tuple[str, object]:
        """/e set <key> <value>: validated, written to the yaml (comments kept), applied at once."""
        from .config import VIA_COMMAND, ConfigError, parse_value, set_key
        if self.config_path:
            k, v = set_key(self.config_path, key, value)
        elif key.strip().lower() in VIA_COMMAND:
            raise ConfigError(f"{key}: {VIA_COMMAND[key.strip().lower()]}")
        else:
            k, v = parse_value(key, value)
        if self.watcher is not None:
            self.watcher.mtime = self.watcher._mtime()          # our own write: no second reload
        self.apply_settings({**self.settings, k: v}, source="/e set")
        return k, v


def _eng_bar(bar: dict) -> dict:
    """The engine reads `time` as naive server time; an int is converted here (the value engine.py would compute itself)."""
    t = bar["time"]
    if isinstance(t, (int, float)):
        return {**bar, "time": datetime.fromtimestamp(int(t), tz=timezone.utc).replace(tzinfo=None)}
    return bar


def _fmt(t) -> str | None:
    if t is None:
        return None
    return datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
