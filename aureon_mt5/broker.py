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
def bars(symbol: str, n: int, source: str, off: float) -> Bars:
    m = mt5()
    if source == "mt5" and m:
        with _lock:
            m.symbol_select(symbol, True)
            rates = m.copy_rates_from_pos(symbol, m.TIMEFRAME_M5, 0, n)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"copy_rates_from_pos({symbol}) empty: {_err()}")
        df = pd.DataFrame(rates).rename(columns={"tick_volume": "volume"})
        df = df[["time", "open", "high", "low", "close", "volume"]].copy()
        df["time"] = df["time"].astype("int64")
        return Bars(symbol, "M5", df)
    return resample(synthetic_m1(symbol, n * 5, end_time=now_server(off)), "M5")


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


def digits(symbol: str) -> int:
    m = mt5()
    try:
        with _lock:
            return m.symbol_info(symbol).digits if m else 2
    except Exception:
        return 2


# ----------------------------------------------------------------------------- orders
def set_sl(ticket: int, symbol: str, sl: float, *, allow_loosen: bool = False) -> BrokerResult:
    """Modify SL. Never loosens: if the live SL is already at least as good, returns ok=True/ALREADY_BETTER without touching it."""
    m = mt5()
    if not m:
        return BrokerResult(False, "NO_CONNECTION", comment="no MT5")
    with _lock:
        pos = m.positions_get(ticket=ticket)
        if not pos:
            return BrokerResult(False, "POSITION_NOT_FOUND", comment="position not found")
        p = pos[0]; tick = m.symbol_info_tick(symbol)
        sl = round(sl, digits(symbol))
        is_long = p.type == m.POSITION_TYPE_BUY
        if p.sl and not allow_loosen and ((is_long and sl <= p.sl + 1e-9) or ((not is_long) and sl >= p.sl - 1e-9)):
            return BrokerResult(True, "ALREADY_BETTER", changed=False, existing_sl=p.sl, requested_sl=sl)
        r = m.order_send({"action": m.TRADE_ACTION_SLTP, "position": ticket, "symbol": symbol, "sl": sl, "tp": p.tp})
    bid, ask = (tick.bid, tick.ask) if tick else (None, None)
    if r is None:
        return BrokerResult(False, "BROKER_REJECTED", comment="order_send returned None", last_error=_err(), existing_sl=p.sl, requested_sl=sl, bid=bid, ask=ask)
    ok = r.retcode == m.TRADE_RETCODE_DONE
    return BrokerResult(ok, "SL_CHANGED" if ok else "BROKER_REJECTED", changed=ok, retcode=r.retcode, comment=getattr(r, "comment", ""),
                        last_error="" if ok else _err(), existing_sl=p.sl, requested_sl=sl, bid=bid, ask=ask)


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
               "deviation": 30, "comment": comment[:31], "type_filling": m.ORDER_FILLING_IOC}
        r = m.order_send(req)
        if r is not None and r.retcode != m.TRADE_RETCODE_DONE:
            req["type_filling"] = m.ORDER_FILLING_FOK; r = m.order_send(req)
    if r is None:
        return BrokerResult(False, "BROKER_REJECTED", comment="order_send returned None", last_error=_err())
    ok = r.retcode == m.TRADE_RETCODE_DONE
    return BrokerResult(ok, "CLOSED" if ok else "BROKER_REJECTED", changed=ok, retcode=r.retcode, comment=getattr(r, "comment", ""),
                        last_error="" if ok else _err(), bid=tick.bid if tick else None, ask=tick.ask if tick else None)


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
                 retry_sleep: float = 2.0, sleep=_time.sleep) -> BrokerResult:
    """v2.0.0 — the ONE order-placement path (used by the alert LONG/SHORT buttons when AUREON_EXECUTION=1).
    Callers decide the policy (execution_enabled, demo/allow_live, max_lots); this only sends the request and reports the retcode.
    v2.0.2: a requote / price-changed rejection is retried ONCE after `retry_sleep` s with a fresh tick; the result carries
    ticket, fill price and volume."""
    m = mt5()
    if not m:
        return BrokerResult(False, "NO_CONNECTION", comment="no MT5")
    is_buy = side.upper() in ("LONG", "BUY")
    attempts = 0; r = None; t = None
    while attempts < 2:
        attempts += 1
        with _lock:
            m.symbol_select(symbol, True)
            t = m.symbol_info_tick(symbol)
            if not t:
                return BrokerResult(False, "BROKER_REJECTED", comment="no tick", last_error=_err())
            req = {"action": m.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(lots),
                   "type": m.ORDER_TYPE_BUY if is_buy else m.ORDER_TYPE_SELL, "price": t.ask if is_buy else t.bid,
                   "deviation": 30, "comment": comment[:31], "type_filling": m.ORDER_FILLING_IOC}
            if sl is not None:
                req["sl"] = round(float(sl), digits(symbol))
            r = m.order_send(req)
            if r is not None and r.retcode != m.TRADE_RETCODE_DONE and not _is_requote(r):
                req["type_filling"] = m.ORDER_FILLING_FOK; r = m.order_send(req)
        if r is not None and r.retcode == m.TRADE_RETCODE_DONE:
            break
        if _is_requote(r) and attempts < 2:
            sleep(retry_sleep); continue
        break
    if r is None:
        return BrokerResult(False, "BROKER_REJECTED", comment="order_send returned None", last_error=_err(), bid=t.bid, ask=t.ask)
    ok = r.retcode == m.TRADE_RETCODE_DONE
    ticket = getattr(r, "order", None) or getattr(r, "deal", None)
    price = getattr(r, "price", None)
    return BrokerResult(ok, "PLACED" if ok else "BROKER_REJECTED", changed=ok, retcode=r.retcode, comment=getattr(r, "comment", ""),
                        last_error="" if ok else _err(), requested_sl=sl, bid=t.bid, ask=t.ask,
                        ticket=int(ticket) if ticket else None, price=float(price) if price else None, volume=float(getattr(r, "volume", lots) or lots))


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
    tf_map = {"M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15", "H1": "TIMEFRAME_H1"}
    if source == "mt5" and m and timeframe in tf_map:
        with _lock:
            m.symbol_select(symbol, True)
            rates = m.copy_rates_from_pos(symbol, getattr(m, tf_map[timeframe]), 0, n)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"copy_rates_from_pos({symbol}, {timeframe}) empty: {_err()}")
        df = pd.DataFrame(rates).rename(columns={"tick_volume": "volume"})
        df = df[["time", "open", "high", "low", "close", "volume"]].copy(); df["time"] = df["time"].astype("int64")
        return df
    mins = {"M1": 1, "M5": 5, "M15": 15, "H1": 60}[timeframe]
    m1 = synthetic_m1(symbol, n * mins, end_time=now_server(off))
    return resample(m1, timeframe).df if timeframe in ("M5", "M15") else m1.df


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
