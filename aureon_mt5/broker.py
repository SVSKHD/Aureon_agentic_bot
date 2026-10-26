"""MT5 layer. ONE persistent connection for the whole process; every call returns a BrokerResult with the real retcode.
Degrades to synthetic bars / no-ops when MT5 is absent (dry runs). Never calls mt5.shutdown() except in close_connection()."""
from __future__ import annotations

import threading
import time as _time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

from .common.source import Bars, resample, synthetic_m1

_m = None            # the MetaTrader5 module once initialised, False if unavailable
_lock = threading.RLock()
_offset_h: float | None = None   # measured server clock offset from UTC, hours


@dataclass
class BrokerResult:
    """ok = the desired protection is in place (changed or already better). changed = a modification was sent and accepted.
    status: SL_CHANGED | ALREADY_BETTER | BROKER_REJECTED | NO_CONNECTION | POSITION_NOT_FOUND | CLOSED | DRY"""
    ok: bool
    status: str = ""
    changed: bool = False
    retcode: int | None = None
    comment: str = ""
    last_error: str = ""
    existing_sl: float | None = None
    requested_sl: float | None = None
    bid: float | None = None
    ask: float | None = None
    ticket: int | None = None        # v2.0.2 place_market: the position / order ticket
    price: float | None = None       # v2.0.2 place_market: the fill price
    volume: float | None = None
    fill_mode: str | None = None     # v2.16.1: the filling type the order went through (ioc | fok | return)
    tp: float | None = None          # v2.16.1 modify_sltp: the TP as sent

    def __bool__(self):
        return self.ok

    def why(self) -> str:
        return f"{self.status} retcode {self.retcode} {self.comment} {self.last_error}".strip()


def connect() -> bool:
    global _m
    with _lock:
        if _m is None:
            try:
                import MetaTrader5 as m  # type: ignore
                _m = m if m.initialize() else False
            except Exception:
                _m = False
        return bool(_m)


def reset():
    """v2.10.1: forget a failed initialize() so connect() can retry (MT5 may still be starting after a reboot)."""
    global _m
    with _lock:
        if not _m:
            _m = None


def mt5():
    return _m if connect() else None


def close_connection():
    global _m
    with _lock:
        if _m:
            _m.shutdown()
        _m = None


@contextmanager
def try_lock(timeout: float = 2.0):
    """For slash-command paths only: wait at most `timeout` s for the shared MT5 lock. Yields True when held
    (MT5 calls inside re-enter the RLock), False when an agent is busy — the caller answers from cache instead."""
    got = _lock.acquire(timeout=timeout)
    try:
        yield got
    finally:
        if got:
            _lock.release()


def _err() -> str:
    try:
        with _lock:
            return str(mt5().last_error())
    except Exception:
        return ""


# ----------------------------------------------------------------------------- time
def server_offset_hours(symbol: str, fallback: float) -> float:
    """MT5 reports bar/tick times as the broker's server clock encoded as a UTC epoch. Measure the offset once from a
    fresh tick instead of hard-coding it (it flips with the broker's DST)."""
    global _offset_h
    if _offset_h is not None:
        return _offset_h
    m = mt5()
    if m:
        with _lock:
            t = m.symbol_info_tick(symbol)
        if t and t.time:
            raw = (t.time - _time.time()) / 3600.0
            if abs(raw) <= 14:                    # only trust it if the tick is fresh (market open)
                _offset_h = round(raw * 2) / 2     # brokers use whole or half hours
                return _offset_h
    return fallback


def now_server(off: float) -> int:
    """Current time on the server clock, encoded the same way MT5 encodes bar times."""
    return int(_time.time() + off * 3600)


# ----------------------------------------------------------------------------- data
NATIVE_TF = {"M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M10": "TIMEFRAME_M10", "M15": "TIMEFRAME_M15", "M20": "TIMEFRAME_M20",
             "M30": "TIMEFRAME_M30", "H1": "TIMEFRAME_H1", "H2": "TIMEFRAME_H2", "H3": "TIMEFRAME_H3", "H4": "TIMEFRAME_H4",
             "H6": "TIMEFRAME_H6", "H8": "TIMEFRAME_H8", "H12": "TIMEFRAME_H12", "D1": "TIMEFRAME_D1"}


def bars(symbol: str, n: int, source: str, off: float, tf: str = "M5") -> Bars:
    """The execution bars. v2.14.0: `tf` = AUREON_EXEC_TF (default M5, the v2.13.0 call exactly). The Bars label stays "M5":
    it is the strategies' key for 'the exec frame'."""
    tf = str(tf or "M5").upper()
    m = mt5()
    if source == "mt5" and m:
        with _lock:
            m.symbol_select(symbol, True)
            rates = m.copy_rates_from_pos(symbol, m.TIMEFRAME_M5 if tf == "M5" else getattr(m, NATIVE_TF[tf]), 0, n)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"copy_rates_from_pos({symbol}) empty: {_err()}")
        df = pd.DataFrame(rates).rename(columns={"tick_volume": "volume"})
        df = df[["time", "open", "high", "low", "close", "volume"]].copy()
        df["time"] = df["time"].astype("int64")
        return Bars(symbol, "M5", df)
    if tf == "M5":
        return resample(synthetic_m1(symbol, n * 5, end_time=now_server(off)), "M5")
    from .common.tf_synth import resample_tf, tf_minutes
    m1 = synthetic_m1(symbol, n * tf_minutes(tf), end_time=now_server(off))
    return Bars(symbol, "M5", resample_tf(m1.df, tf))


def positions(symbol: str) -> list[dict]:
    m = mt5()
    if not m:
        return []
    with _lock:
        pos = m.positions_get(symbol=symbol)
        tick = m.symbol_info_tick(symbol)
    out = []
    for p in pos or []:
        d = "long" if p.type == m.POSITION_TYPE_BUY else "short"
        cur = tick.bid if d == "long" else tick.ask
        out.append({"ticket": p.ticket, "symbol": symbol, "direction": d, "price_open": p.price_open, "time": int(p.time),
                    "sl": p.sl, "tp": p.tp, "volume": p.volume, "current": cur, "comment": p.comment, "magic": p.magic,
                    "points": (cur - p.price_open) if d == "long" else (p.price_open - cur)})
    return out


PENDING_TYPES = {2: "buy_limit", 3: "sell_limit", 4: "buy_stop", 5: "sell_stop", 6: "buy_stop_limit", 7: "sell_stop_limit"}


def pending_orders(symbol: str) -> list[dict] | None:
    """v2.17.0: the symbol's pending orders (orders_get). [] without MT5; None when MT5 is there but the call fails."""
    m = mt5()
    if not m:
        return []
    if not hasattr(m, "orders_get"):
        return []
    try:
        with _lock:
            got = m.orders_get(symbol=symbol)
    except Exception:
        return None
    if got is None:
        return None if getattr(m, "last_error", lambda: (0, ""))()[0] not in (0, 1) else []
    out = []
    for o in got:
        t = int(getattr(o, "type", -1))
        out.append({"ticket": int(o.ticket), "symbol": symbol, "type": PENDING_TYPES.get(t, str(t)),
                    "side": "LONG" if t in (2, 4, 6) else "SHORT", "price": float(getattr(o, "price_open", 0.0)),
                    "sl": float(getattr(o, "sl", 0.0) or 0.0), "tp": float(getattr(o, "tp", 0.0) or 0.0),
                    "volume": float(getattr(o, "volume_current", getattr(o, "volume_initial", 0.0)) or 0.0),
                    "expiration": int(getattr(o, "time_expiration", 0) or 0), "comment": str(getattr(o, "comment", "") or ""),
                    "magic": int(getattr(o, "magic", 0) or 0), "time_setup": int(getattr(o, "time_setup", 0) or 0)})
    return out


def select(symbol: str) -> bool:
    """v2.18.1 /attach: symbol_select(symbol, True) and the symbol exists on this account."""
    m = mt5()
    if not m:
        return False
    try:
        with _lock:
            return bool(m.symbol_select(symbol, True)) and m.symbol_info(symbol) is not None
    except Exception:
        return False


def point(symbol: str) -> float | None:
    """v2.18.0: the symbol's point (symbol_info.point). None without MT5."""
    m = mt5()
    if not m:
        return None
    try:
        with _lock:
            info = m.symbol_info(symbol)
        return float(getattr(info, "point", 0.0) or 0.0) or None
    except Exception:
        return None


def point_value(symbol: str) -> float | None:
    """v2.18.0: account-currency value of ONE point at 1 lot (tick_value / tick_size × point). None without MT5."""
    m = mt5()
    if not m:
        return None
    try:
        with _lock:
            info = m.symbol_info(symbol)
        tv = float(getattr(info, "trade_tick_value", 0.0) or 0.0); ts = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
        pt = float(getattr(info, "point", 0.0) or 0.0)
        return tv / ts * pt if tv and ts and pt else None
    except Exception:
        return None


def digits(symbol: str) -> int:
    m = mt5()
    try:
        with _lock:
            return m.symbol_info(symbol).digits if m else 2
    except Exception:
        return 2


# ----------------------------------------------------------------------------- orders
# ----------------------------------------------------------------------------- v2.16.2 stops validation
SINK = {"log": None, "alert": None}   # main wires journal.log and a red health card; broker itself never needs Discord
INVALID_STOPS = 10016                 # TRADE_RETCODE_INVALID_STOPS
STOPS_COOLDOWN_S = 300.0
_SL_COOLDOWN: dict = {}               # ticket → monotonic time until which set_sl is not retried after a 10016


def _emit(kind: str, **kw):
    fn = SINK.get("log")
    if fn is not None:
        try:
            fn(kind, **kw)
        except Exception:
            pass


def _alert(title: str, body: str):
    fn = SINK.get("alert")
    if fn is not None:
        try:
            fn(title, body)
        except Exception:
            pass


def stops_info(m, symbol: str) -> dict:
    """{dist, stops_level, freeze_level, point}: dist = max(stops_level, freeze_level) × point. MT5's rule is inclusive
    (a buy: Bid − SL ≥ StopsLevel × point), so with both levels 0 an SL at the bid is legal and nothing is clamped."""
    info = m.symbol_info(symbol)
    d = int(getattr(info, "digits", 2) or 2)
    point = float(getattr(info, "point", 0.0) or 10 ** -d)
    lv = int(getattr(info, "trade_stops_level", 0) or 0); fz = int(getattr(info, "freeze_level", 0) or 0)
    return {"dist": max(lv, fz) * point, "stops_level": lv, "freeze_level": fz, "point": point, "digits": d}


def clamp_stops(is_long: bool, bid: float, ask: float, sl: float | None, tp: float | None, dist: float, d: int = 2):
    """The nearest allowed SL / TP on the correct side. A long is priced at the bid: SL ≤ bid − dist, TP ≥ bid + dist.
    A short at the ask: SL ≥ ask + dist, TP ≤ ask − dist. None / 0 stays as it is (= none)."""
    def r(x):
        return round(float(x), d)
    if is_long:
        sl2 = r(min(sl, bid - dist)) if sl else sl
        tp2 = r(max(tp, bid + dist)) if tp else tp
    else:
        sl2 = r(max(sl, ask + dist)) if sl else sl
        tp2 = r(min(tp, ask - dist)) if tp else tp
    return sl2, tp2


def _invalid_stops(m, symbol, ticket, side, requested, sent, tick, si, *, cooldown: bool):
    if cooldown and ticket is not None:
        _SL_COOLDOWN[ticket] = _time.monotonic() + STOPS_COOLDOWN_S
    bid, ask = (tick.bid, tick.ask) if tick else (None, None)
    _emit("guardian_failure", symbol=symbol, ticket=ticket, side=side, retcode=INVALID_STOPS, requested=requested, sent=sent, bid=bid,
          ask=ask, stops_level=si["stops_level"], freeze_level=si["freeze_level"], cooldown_s=STOPS_COOLDOWN_S if cooldown else 0)
    _alert("🔴 AUREON · MT5 · GUARDIAN FAILURE · INVALID STOPS",
           f"{symbol} {'#' + str(ticket) if ticket else 'new order'} · {side} · retcode 10016 after clamping\n"
           f"requested {requested} · sent {sent} · bid {bid} · ask {ask} · stops_level {si['stops_level']} · freeze_level "
           f"{si['freeze_level']} pts" + (f"\nSL changes on this ticket paused {STOPS_COOLDOWN_S / 60:.0f} min — the broker SL "
                                          "already on the position stays" if cooldown else ""))


def set_sl(ticket: int, symbol: str, sl: float, *, allow_loosen: bool = False) -> BrokerResult:
    """Modify SL. Never loosens: if the live SL is already at least as good, returns ok=True/ALREADY_BETTER without touching it.
    v2.16.2: the SL (and the TP sent with it) is clamped to the broker's stops / freeze level on the correct side
    (sl_clamped row); a 10016 after clamping pauses this ticket for 5 min (status COOLDOWN) with one red card."""
    m = mt5()
    if not m:
        return BrokerResult(False, "NO_CONNECTION", comment="no MT5")
    until = _SL_COOLDOWN.get(ticket)
    if until is not None:
        if _time.monotonic() < until:
            return BrokerResult(False, "COOLDOWN", comment="10016 invalid stops — retries paused", requested_sl=sl)
        _SL_COOLDOWN.pop(ticket, None)
    with _lock:
        pos = m.positions_get(ticket=ticket)
        if not pos:
            return BrokerResult(False, "POSITION_NOT_FOUND", comment="position not found")
        p = pos[0]; tick = m.symbol_info_tick(symbol)
        is_long = p.type == m.POSITION_TYPE_BUY; side = "LONG" if is_long else "SHORT"
        si = stops_info(m, symbol)
        requested = round(sl, si["digits"])
        sl, tp = (clamp_stops(is_long, tick.bid, tick.ask, requested, p.tp or None, si["dist"], si["digits"]) if tick
                  else (requested, p.tp or None))
        if sl != requested or (tp or 0.0) != (p.tp or 0.0):
            _emit("sl_clamped", symbol=symbol, ticket=ticket, side=side, requested=requested, sent=sl, tp_requested=p.tp or None,
                  tp_sent=tp, bid=tick.bid if tick else None, ask=tick.ask if tick else None, stops_level=si["stops_level"],
                  freeze_level=si["freeze_level"])
        if p.sl and not allow_loosen and ((is_long and sl <= p.sl + 1e-9) or ((not is_long) and sl >= p.sl - 1e-9)):
            return BrokerResult(True, "ALREADY_BETTER", changed=False, existing_sl=p.sl, requested_sl=sl)
        r = m.order_send({"action": m.TRADE_ACTION_SLTP, "position": ticket, "symbol": symbol, "sl": sl, "tp": tp or 0.0})
    bid, ask = (tick.bid, tick.ask) if tick else (None, None)
    if r is None:
        return BrokerResult(False, "BROKER_REJECTED", comment="order_send returned None", last_error=_err(), existing_sl=p.sl, requested_sl=sl, bid=bid, ask=ask)
    ok = r.retcode == m.TRADE_RETCODE_DONE
    if r.retcode == INVALID_STOPS:
        _invalid_stops(m, symbol, ticket, side, requested, sl, tick, si, cooldown=True)
    return BrokerResult(ok, "SL_CHANGED" if ok else "BROKER_REJECTED", changed=ok, retcode=r.retcode, comment=getattr(r, "comment", ""),
                        last_error="" if ok else _err(), existing_sl=p.sl, requested_sl=sl, bid=bid, ask=ask)


# ----------------------------------------------------------------------------- v2.16.1 filling type (AUREON_FILLING)
FILL_ORDER = ("ioc", "fok", "return")
FILLING = {"mode": "ioc"}
INVALID_FILL = 10030                  # TRADE_RETCODE_INVALID_FILL: the symbol does not accept this filling type


def configure_filling(mode: str | None) -> str:
    mode = str(mode or "ioc").strip().lower()
    FILLING["mode"] = mode if mode in FILL_ORDER else "ioc"
    return FILLING["mode"]


def fill_sequence(first: str | None = None) -> list[str]:
    """The configured type first, then the other two in FILL_ORDER (ioc → fok → return)."""
    first = first if first in FILL_ORDER else FILLING["mode"]
    return [first] + [x for x in FILL_ORDER if x != first]


def _fill_const(m, name: str) -> int:
    return {"fok": getattr(m, "ORDER_FILLING_FOK", 0), "ioc": getattr(m, "ORDER_FILLING_IOC", 1),
            "return": getattr(m, "ORDER_FILLING_RETURN", 2)}[name]


def _send_filled(m, req: dict) -> tuple:
    """order_send with the configured filling type; on TRADE_RETCODE_INVALID_FILL only, the next type. (result, type used)."""
    bad = getattr(m, "TRADE_RETCODE_INVALID_FILL", INVALID_FILL); r = None; used = None
    for name in fill_sequence():
        req["type_filling"] = _fill_const(m, name); used = name
        r = m.order_send(req)
        if r is None or getattr(r, "retcode", None) != bad:
            break
    return r, used


def filling_supported(symbol: str) -> dict | None:
    """{ioc, fok, return} from symbol_info.filling_mode (bit 1 = FOK, bit 2 = IOC); RETURN is refused on market execution.
    None without MT5."""
    m = mt5()
    if not m:
        return None
    try:
        with _lock:
            info = m.symbol_info(symbol)
        flags = int(getattr(info, "filling_mode", 0) or 0)
        exe = getattr(info, "trade_exemode", None)
        market = exe is not None and exe == getattr(m, "SYMBOL_TRADE_EXECUTION_MARKET", 2)
        return {"fok": bool(flags & 1), "ioc": bool(flags & 2), "return": not market}
    except Exception:
        return None


def modify_sltp(ticket: int, symbol: str, sl: float | None, tp: float | None) -> BrokerResult:
    """v2.16.1 /sl /tp: ONE TRADE_ACTION_SLTP carrying BOTH values — MT5 overwrites both, so the caller passes the current
    value of the one it is not changing. sl / tp None or 0 = none. No loosen check here: the caller decides (/sl force)."""
    m = mt5()
    if not m:
        return BrokerResult(False, "NO_CONNECTION", comment="no MT5")
    with _lock:
        pos = m.positions_get(ticket=ticket)
        if not pos:
            return BrokerResult(False, "POSITION_NOT_FOUND", comment="position not found")
        p = pos[0]; d = digits(symbol)
        req = {"action": m.TRADE_ACTION_SLTP, "position": ticket, "symbol": symbol,
               "sl": round(float(sl), d) if sl else 0.0, "tp": round(float(tp), d) if tp else 0.0}
        r = m.order_send(req)
    if r is None:
        return BrokerResult(False, "BROKER_REJECTED", comment="order_send returned None", last_error=_err(), existing_sl=p.sl)
    ok = r.retcode == m.TRADE_RETCODE_DONE
    return BrokerResult(ok, "SLTP_CHANGED" if ok else "BROKER_REJECTED", changed=ok, retcode=r.retcode, comment=getattr(r, "comment", ""),
                        last_error="" if ok else _err(), existing_sl=p.sl, requested_sl=req["sl"], tp=req["tp"])


def close(ticket: int, symbol: str, comment: str = "aureon") -> BrokerResult:
    m = mt5()
    if not m:
        return BrokerResult(False, "NO_CONNECTION", comment="no MT5")
    with _lock:
        pos = m.positions_get(ticket=ticket)
        if not pos:
            return BrokerResult(False, "POSITION_NOT_FOUND", comment="position not found")
        p = pos[0]; tick = m.symbol_info_tick(symbol)
        is_buy = p.type == m.POSITION_TYPE_BUY
        req = {"action": m.TRADE_ACTION_DEAL, "position": ticket, "symbol": symbol, "volume": p.volume,
               "type": m.ORDER_TYPE_SELL if is_buy else m.ORDER_TYPE_BUY, "price": tick.bid if is_buy else tick.ask,
               "deviation": 30, "comment": comment[:31]}
        r, used = _send_filled(m, req)                     # v2.16.1: AUREON_FILLING, fallback on INVALID_FILL only
    if r is None:
        return BrokerResult(False, "BROKER_REJECTED", comment="order_send returned None", last_error=_err())
    ok = r.retcode == m.TRADE_RETCODE_DONE
    return BrokerResult(ok, "CLOSED" if ok else "BROKER_REJECTED", changed=ok, retcode=r.retcode, comment=getattr(r, "comment", ""),
                        last_error="" if ok else _err(), bid=tick.bid if tick else None, ask=tick.ask if tick else None, fill_mode=used)


def tick(symbol: str) -> dict | None:
    """The same tick the guardian prices positions with: {bid, ask, time} (server epoch). None without MT5 / no tick."""
    m = mt5()
    if not m:
        return None
    with _lock:
        t = m.symbol_info_tick(symbol)
    if not t or not t.time:
        return None
    return {"bid": float(t.bid), "ask": float(t.ask), "time": int(t.time)}


def account() -> dict | None:
    """{balance, equity, currency, trade_mode: demo|contest|real, login_masked}. None without MT5. Never logged in full."""
    m = mt5()
    if not m:
        return None
    with _lock:
        a = m.account_info()
    if a is None:
        return None
    mode = {0: "demo", 1: "contest", 2: "real"}.get(int(getattr(a, "trade_mode", 2)), "real")
    return {"balance": float(a.balance), "equity": float(getattr(a, "equity", a.balance)), "currency": str(getattr(a, "currency", "")),
            "trade_mode": mode, "login_masked": f"***{str(getattr(a, 'login', ''))[-3:]}"}


def is_demo() -> bool | None:
    """True on a demo/contest account, False on real, None when unknown (no MT5)."""
    a = account()
    return None if a is None else (a["trade_mode"] in ("demo", "contest"))


REQUOTE_RETCODES = (10004, 10020, 10021)      # TRADE_RETCODE_REQUOTE, PRICE_CHANGED, PRICE_OFF


def _is_requote(r) -> bool:
    if r is None:
        return False
    c = str(getattr(r, "comment", "") or "").lower()
    return getattr(r, "retcode", None) in REQUOTE_RETCODES or "requote" in c or "price changed" in c


def place_market(symbol: str, side: str, lots: float, sl: float | None = None, comment: str = "aureon-alert", *,
                 retry_sleep: float = 2.0, sleep=_time.sleep, tp: float | None = None) -> BrokerResult:
    """v2.0.0 — the ONE order-placement path (used by the alert LONG/SHORT buttons when AUREON_EXECUTION=1).
    Callers decide the policy (execution_enabled, demo/allow_live, max_lots); this only sends the request and reports the retcode.
    v2.0.2: a requote / price-changed rejection is retried ONCE after `retry_sleep` s with a fresh tick; the result carries
    ticket, fill price and volume."""
    m = mt5()
    if not m:
        return BrokerResult(False, "NO_CONNECTION", comment="no MT5")
    is_buy = side.upper() in ("LONG", "BUY")
    attempts = 0; r = None; t = None; used = None
    while attempts < 2:
        attempts += 1
        with _lock:
            m.symbol_select(symbol, True)
            t = m.symbol_info_tick(symbol)
            if not t:
                return BrokerResult(False, "BROKER_REJECTED", comment="no tick", last_error=_err())
            req = {"action": m.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(lots),
                   "type": m.ORDER_TYPE_BUY if is_buy else m.ORDER_TYPE_SELL, "price": t.ask if is_buy else t.bid,
                   "deviation": 30, "comment": comment[:31]}
            si = stops_info(m, symbol)                       # v2.16.2: clamp to the stops / freeze level, correct side
            req_sl = round(float(sl), si["digits"]) if sl is not None else None
            req_tp = round(float(tp), si["digits"]) if tp is not None else None
            c_sl, c_tp = clamp_stops(is_buy, t.bid, t.ask, req_sl, req_tp, si["dist"], si["digits"])
            if c_sl != req_sl or c_tp != req_tp:
                _emit("sl_clamped", symbol=symbol, ticket=None, side="LONG" if is_buy else "SHORT", requested=req_sl, sent=c_sl,
                      tp_requested=req_tp, tp_sent=c_tp, bid=t.bid, ask=t.ask, stops_level=si["stops_level"], freeze_level=si["freeze_level"])
            if c_sl is not None:
                req["sl"] = c_sl
            if c_tp is not None:                                 # v2.16.0: broker-side take-profit (ema921 cap)
                req["tp"] = c_tp
            held = list(m.positions_get(symbol=symbol) or [])    # v2.16.2: the position cap, under the lock (no racing taps)
            if hasattr(m, "orders_get"):                     # v2.17.0: a pending order counts as "in position"
                held += list(m.orders_get(symbol=symbol) or [])
            if held:
                _emit("entry_blocked_position_cap", symbol=symbol, side="LONG" if is_buy else "SHORT", source=f"broker:{comment}",
                      open=[getattr(x, "ticket", None) for x in held])
                return BrokerResult(False, "POSITION_CAP", comment=f"{symbol} already has {len(held)} open position(s)",
                                    bid=t.bid, ask=t.ask)
            r, used = _send_filled(m, req)                 # v2.16.1: AUREON_FILLING first, the others only on INVALID_FILL
        if r is not None and r.retcode == m.TRADE_RETCODE_DONE:
            break
        if _is_requote(r) and attempts < 2:
            sleep(retry_sleep); continue
        break
    if r is None:
        return BrokerResult(False, "BROKER_REJECTED", comment="order_send returned None", last_error=_err(), bid=t.bid, ask=t.ask)
    ok = r.retcode == m.TRADE_RETCODE_DONE
    if r.retcode == INVALID_STOPS:
        _invalid_stops(m, symbol, None, "LONG" if is_buy else "SHORT", req_sl, req.get("sl"), t, si, cooldown=False)
    ticket = getattr(r, "order", None) or getattr(r, "deal", None)
    price = getattr(r, "price", None)
    return BrokerResult(ok, "PLACED" if ok else "BROKER_REJECTED", changed=ok, retcode=r.retcode, comment=getattr(r, "comment", ""),
                        last_error="" if ok else _err(), requested_sl=req.get("sl", sl), bid=t.bid, ask=t.ask,
                        ticket=int(ticket) if ticket else None, price=float(price) if price else None, volume=float(getattr(r, "volume", lots) or lots),
                        fill_mode=used)


PLACED_RETCODES = (10008, 10009)                    # TRADE_RETCODE_PLACED (a pending order) · TRADE_RETCODE_DONE
INVALID_EXPIRATION = 10022                          # TRADE_RETCODE_INVALID_EXPIRATION


def place_pending(symbol: str, side: str, lots: float, level: float, sl: float | None, tp: float | None, expiration: int,
                  comment: str = "aureon 921") -> BrokerResult:
    """v2.17.0 — a BUY_LIMIT (long, below the ask) / SELL_LIMIT (short, above the bid) at `level` with SL / TP on the order,
    TRADE_ACTION_PENDING · ORDER_TIME_SPECIFIED · expiration (server-clock epoch, like bar times). Same cap as place_market,
    under the lock: an open position or another pending order on the symbol refuses it."""
    m = mt5()
    if not m:
        return BrokerResult(False, "NO_CONNECTION", comment="no MT5")
    is_buy = side.upper() in ("LONG", "BUY")
    with _lock:
        m.symbol_select(symbol, True)
        t = m.symbol_info_tick(symbol)
        if not t:
            return BrokerResult(False, "BROKER_REJECTED", comment="no tick", last_error=_err())
        held = list(m.positions_get(symbol=symbol) or []) + (list(m.orders_get(symbol=symbol) or []) if hasattr(m, "orders_get") else [])
        if held:
            _emit("entry_blocked_position_cap", symbol=symbol, side="LONG" if is_buy else "SHORT", source=f"broker:{comment}",
                  open=[getattr(x, "ticket", None) for x in held])
            return BrokerResult(False, "POSITION_CAP", comment=f"{symbol} already has {len(held)} open position / order(s)",
                                bid=t.bid, ask=t.ask)
        si = stops_info(m, symbol); d = si["digits"]
        req = {"action": getattr(m, "TRADE_ACTION_PENDING", 5), "symbol": symbol, "volume": float(lots),
               "type": getattr(m, "ORDER_TYPE_BUY_LIMIT", 2) if is_buy else getattr(m, "ORDER_TYPE_SELL_LIMIT", 3),
               "price": round(float(level), d), "deviation": 30, "comment": comment[:31],
               "type_time": getattr(m, "ORDER_TIME_SPECIFIED", 2), "expiration": int(expiration)}
        if sl is not None:
            req["sl"] = round(float(sl), d)
        if tp is not None:
            req["tp"] = round(float(tp), d)
        bad = getattr(m, "TRADE_RETCODE_INVALID_FILL", INVALID_FILL); r = None; used = None
        for name in fill_sequence("return"):                  # pending orders: RETURN first, the others only on INVALID_FILL
            req["type_filling"] = _fill_const(m, name); used = name
            r = m.order_send(req)
            if r is None or getattr(r, "retcode", None) != bad:
                break
        if r is not None and getattr(r, "retcode", None) == INVALID_EXPIRATION:   # the symbol refuses a set expiry:
            req["type_time"] = getattr(m, "ORDER_TIME_GTC", 0); req.pop("expiration", None)   # GTC, Aureon cancels at expiry
            r = m.order_send(req); used = f"{used} · gtc"
    if r is None:
        return BrokerResult(False, "BROKER_REJECTED", comment="order_send returned None", last_error=_err(), bid=t.bid, ask=t.ask)
    ok = r.retcode in PLACED_RETCODES
    ticket = getattr(r, "order", None)
    return BrokerResult(ok, "PENDING_PLACED" if ok else "BROKER_REJECTED", changed=ok, retcode=r.retcode,
                        comment=getattr(r, "comment", ""), last_error="" if ok else _err(), requested_sl=req.get("sl"),
                        bid=t.bid, ask=t.ask, ticket=int(ticket) if ticket else None, price=req["price"], volume=float(lots),
                        fill_mode=used, tp=req.get("tp"))


def cancel_order(ticket: int, symbol: str = "") -> BrokerResult:
    """v2.17.0 — remove a pending order (TRADE_ACTION_REMOVE). Gone already → ORDER_NOT_FOUND (not an error for callers)."""
    m = mt5()
    if not m:
        return BrokerResult(False, "NO_CONNECTION", comment="no MT5")
    with _lock:
        if hasattr(m, "orders_get") and not (m.orders_get(ticket=ticket) or []):
            return BrokerResult(False, "ORDER_NOT_FOUND", comment="order not found")
        r = m.order_send({"action": getattr(m, "TRADE_ACTION_REMOVE", 8), "order": int(ticket)})
    if r is None:
        return BrokerResult(False, "BROKER_REJECTED", comment="order_send returned None", last_error=_err())
    ok = r.retcode in PLACED_RETCODES
    return BrokerResult(ok, "CANCELLED" if ok else "BROKER_REJECTED", changed=ok, retcode=r.retcode, comment=getattr(r, "comment", ""),
                        last_error="" if ok else _err())


def spread(symbol: str) -> float | None:
    """Current ask − bid in price units (None without MT5)."""
    t = tick(symbol)
    return None if t is None else round(t["ask"] - t["bid"], 5)


def lot_for_risk(symbol: str, stop_distance: float, risk_pct: float) -> dict | None:
    """Display helper for the ask cards: lots so that `stop_distance` costs `risk_pct` % of the balance. Never sizes an order."""
    m = mt5()
    if not m or stop_distance <= 0:
        return None
    with _lock:
        a = m.account_info(); info = m.symbol_info(symbol)
    if a is None or info is None:
        return None
    contract = float(getattr(info, "trade_contract_size", 100.0) or 100.0)
    step = float(getattr(info, "volume_step", 0.01) or 0.01); vmin = float(getattr(info, "volume_min", 0.01) or 0.01)
    risk_money = float(a.balance) * risk_pct / 100.0
    lots = risk_money / (stop_distance * contract)
    lots = max(vmin, round(lots / step) * step)
    return {"lots": round(lots, 2), "risk_pct": risk_pct, "risk_money": risk_money, "currency": str(getattr(a, "currency", "")), "stop": stop_distance}


def bars_tf(symbol: str, timeframe: str, n: int, source: str, off: float) -> pd.DataFrame:
    """Bars of another timeframe (M15 / H1) for the HTF context line. Synthetic fallback without MT5."""
    m = mt5()
    timeframe = str(timeframe).upper()
    if timeframe == "M45":                                   # v2.14.0: synthetic, from 3 × M15 (closed groups only)
        from .common.tf_synth import synth_m45
        return synth_m45(bars_tf(symbol, "M15", n * 3 + 3, source, off))
    tf_map = NATIVE_TF
    if source == "mt5" and m and timeframe in tf_map:
        with _lock:
            m.symbol_select(symbol, True)
            rates = m.copy_rates_from_pos(symbol, getattr(m, tf_map[timeframe]), 0, n)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"copy_rates_from_pos({symbol}, {timeframe}) empty: {_err()}")
        df = pd.DataFrame(rates).rename(columns={"tick_volume": "volume"})
        df = df[["time", "open", "high", "low", "close", "volume"]].copy(); df["time"] = df["time"].astype("int64")
        return df
    mins = {"M1": 1, "M5": 5, "M15": 15, "H1": 60}.get(timeframe)
    if mins is not None:                                     # the v2.13.0 synthetic path, unchanged
        m1 = synthetic_m1(symbol, n * mins, end_time=now_server(off))
        return resample(m1, timeframe).df if timeframe in ("M5", "M15") else m1.df
    from .common.tf_synth import resample_tf, tf_minutes
    m1 = synthetic_m1(symbol, n * tf_minutes(timeframe), end_time=now_server(off))
    return resample_tf(m1.df, timeframe)


def close_partial(ticket: int, symbol: str, fraction: float, comment: str = "aureon-partial") -> BrokerResult:
    """Close `fraction` of a position (AUREON-012). requested_sl carries the closed volume for the card."""
    m = mt5()
    if not m:
        return BrokerResult(False, "NO_CONNECTION", comment="no MT5")
    with _lock:
        pos = m.positions_get(ticket=ticket)
        if not pos:
            return BrokerResult(False, "POSITION_NOT_FOUND", comment="position not found")
        p = pos[0]; tick_ = m.symbol_info_tick(symbol); info = m.symbol_info(symbol)
        step = float(getattr(info, "volume_step", 0.01) or 0.01) if info else 0.01
        vol = max(step, round(p.volume * fraction / step) * step)
        if vol >= p.volume:
            return BrokerResult(False, "BROKER_REJECTED", comment="partial volume would close the whole position")
        is_buy = p.type == m.POSITION_TYPE_BUY
        req = {"action": m.TRADE_ACTION_DEAL, "position": ticket, "symbol": symbol, "volume": vol,
               "type": m.ORDER_TYPE_SELL if is_buy else m.ORDER_TYPE_BUY, "price": tick_.bid if is_buy else tick_.ask,
               "deviation": 30, "comment": comment[:31], "type_filling": m.ORDER_FILLING_IOC}
        r = m.order_send(req)
    if r is None:
        return BrokerResult(False, "BROKER_REJECTED", comment="order_send returned None", last_error=_err())
    ok = r.retcode == m.TRADE_RETCODE_DONE
    return BrokerResult(ok, "CLOSED" if ok else "BROKER_REJECTED", changed=ok, retcode=r.retcode, comment=getattr(r, "comment", ""),
                        last_error="" if ok else _err(), requested_sl=vol)


def tick_age(symbol: str, off: float) -> int | None:
    m = mt5()
    if not m:
        return None
    with _lock:
        t = m.symbol_info_tick(symbol)
    return int(now_server(off) - t.time) if t and t.time else None


def connected() -> bool:
    m = mt5()
    try:
        with _lock:
            return bool(m and m.terminal_info() is not None)
    except Exception:
        return False


def closed_deals(symbol: str | None, start: datetime, end: datetime) -> list[dict]:
    m = mt5()
    if not m:
        return []
    with _lock:
        deals = m.history_deals_get(start, end) or []
    out = []
    for d in deals:
        if d.entry != m.DEAL_ENTRY_OUT or (symbol and d.symbol != symbol) or d.symbol == "":
            continue
        out.append({"ticket": d.position_id, "symbol": d.symbol, "time": int(d.time), "price": d.price, "volume": d.volume,
                    "profit": d.profit, "direction": "long" if d.type == m.DEAL_TYPE_SELL else "short", "comment": d.comment})
    return out


# ----------------------------------------------------------------------------- market
def market_open(symbol: str, off: float, dry: bool = False) -> bool:
    """Evidence-based: symbol tradeable + a tick within the last 10 minutes. Weekday rule is only the fallback (dry runs)."""
    if dry:
        return True
    m = mt5()
    if m:
        try:
            with _lock:
                info = m.symbol_info(symbol)
                t = m.symbol_info_tick(symbol)
            if info is None or info.trade_mode == m.SYMBOL_TRADE_MODE_DISABLED:
                return False
            return bool(t and t.time and (now_server(off) - t.time) < 10 * 60)
        except Exception:
            pass
    srv = datetime.now(timezone.utc) + timedelta(hours=off)
    return not (srv.weekday() >= 5 or (srv.weekday() == 4 and srv.hour == 23 and srv.minute >= 55))
