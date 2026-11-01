"""v3.0 ema921e — the two execution back-ends the mode runner talks to. Same small interface:

    bars(n)            last n M30 bars (dicts: time int server-epoch, open, high, low, close), the forming bar last
    now_server()       server clock (epoch s)
    market_open()      the feed is live
    tick()             {bid, ask, time} or None
    positions()        open ema921e positions only (comment COMMENT) — other modes' positions are never touched
    open(side, lots, sl)   market order, broker SL = the disaster stop only, never a TP
    close(ticket)      market close
    set_sl(ticket, sl) never loosens (aureon_mt5.broker.set_sl)
    close_price(ticket)    the closing deal's price (a broker SL fill), None when unknown
    account_kind()     "demo" | "real" | None
    equity()           account equity or None

MT5Broker is a thin wrapper over aureon_mt5.broker — the 2.16 filling fallback (AUREON_FILLING, ioc → fok → return on
INVALID_FILL only), the requote retry, the stops clamp and the one-position-per-symbol cap all come from there.
SimBroker fills at the feed's tick (bid / ask), checks the broker SL on every tick, and persists its book to
logs/ema921e_sim.json so a killed process finds its position again (acceptance test 2).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

COMMENT = "aureon ema921e"
TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600}


def _ok(**kw):
    from ... import broker
    return broker.BrokerResult(True, kw.pop("status", "OK"), changed=True, **kw)


def _fail(status, comment=""):
    from ... import broker
    return broker.BrokerResult(False, status, comment=comment)


# ----------------------------------------------------------------------------- MT5
class MT5Broker:
    name = "MT5"

    def __init__(self, symbol: str, tf: str, cfg, off: float):
        self.symbol, self.tf, self.cfg, self.off = symbol, tf, cfg, off

    @property
    def b(self):
        from ... import broker
        return broker

    def bars(self, n: int) -> list[dict]:
        df = self.b.bars(self.symbol, int(n), self.cfg.source, self.off, tf=self.tf).df
        return [{"time": int(r.time), "open": float(r.open), "high": float(r.high), "low": float(r.low), "close": float(r.close)}
                for r in df.itertuples(index=False)]

    def now_server(self) -> int:
        return int(self.b.now_server(self.off))

    def market_open(self) -> bool:
        return self.b.market_open(self.symbol, self.off, False)

    def tick(self):
        return self.b.tick(self.symbol)

    def positions(self) -> list[dict]:
        return [p for p in self.b.positions(self.symbol) if str(p.get("comment", "")).startswith(COMMENT)]

    def open(self, side: str, lots: float, sl: float | None):
        return self.b.place_market(self.symbol, side, lots, sl, comment=COMMENT, tp=None)

    def close(self, ticket: int):
        return self.b.close(ticket, self.symbol, COMMENT)

    def set_sl(self, ticket: int, sl: float):
        return self.b.set_sl(ticket, self.symbol, sl)

    def close_price(self, ticket: int):
        now = datetime.now(timezone.utc)
        deals = [d for d in self.b.closed_deals(self.symbol, now - timedelta(days=5), now + timedelta(minutes=1)) if d["ticket"] == ticket]
        return float(deals[-1]["price"]) if deals else None

    def account_kind(self):
        d = self.b.is_demo()
        return None if d is None else ("demo" if d else "real")

    def equity(self):
        a = self.b.account()
        return float(a["equity"]) if a else None

    def account_login(self):
        """The full login, only for the /e live confirmation (never logged or carded)."""
        m = self.b.mt5()
        if not m:
            return None
        with self.b._lock:
            a = m.account_info()
        return str(getattr(a, "login", "")) if a is not None else None


# ----------------------------------------------------------------------------- simulated
class SimBroker:
    """Paper fills against a feed (anything with bars(n) / tick() / now_server()). The book survives restarts."""
    name = "SIM"

    def __init__(self, symbol: str, feed, log_dir: str, *, equity0: float = 100_000.0):
        self.symbol, self.feed = symbol, feed
        self.path = os.path.join(log_dir, "ema921e_sim.json")
        self.book = {"next": 1, "positions": [], "closed": {}, "realized": 0.0, "equity0": equity0}
        try:
            with open(self.path, encoding="utf-8") as f:
                self.book.update(json.load(f))
        except (OSError, ValueError):
            pass

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.book, f, indent=1)
        os.replace(tmp, self.path)

    def bars(self, n):
        return self.feed.bars(n)

    def now_server(self):
        return int(self.feed.now_server())

    def market_open(self):
        return True

    def tick(self):
        t = self.feed.tick()
        if t:
            for p in list(self.book["positions"]):            # the broker SL works on the sim book too
                sl = p.get("sl")
                if sl and ((t["bid"] <= sl) if p["direction"] == "long" else (t["ask"] >= sl)):
                    self._settle(p, sl)
        return t

    def positions(self):
        return [dict(p, comment=COMMENT) for p in self.book["positions"]]

    def open(self, side, lots, sl):
        if self.book["positions"]:
            return _fail("POSITION_CAP", f"{self.symbol} already has an open position")
        t = self.feed.tick()
        if not t:
            return _fail("BROKER_REJECTED", "no tick")
        long_ = side.upper() in ("LONG", "BUY")
        px = t["ask"] if long_ else t["bid"]
        tk = int(self.book["next"]); self.book["next"] = tk + 1
        self.book["positions"].append({"ticket": tk, "symbol": self.symbol, "direction": "long" if long_ else "short",
                                       "price_open": px, "time": int(t["time"]), "sl": float(sl) if sl else 0.0, "tp": 0.0,
                                       "volume": float(lots)})
        self._save()
        return _ok(status="PLACED", ticket=tk, price=px, volume=float(lots), fill_mode="sim")

    def _settle(self, p, px):
        s = 1 if p["direction"] == "long" else -1
        self.book["realized"] = float(self.book.get("realized", 0.0)) + (px - p["price_open"]) * s * p["volume"] * 100.0
        self.book["positions"] = [q for q in self.book["positions"] if q["ticket"] != p["ticket"]]
        self.book["closed"][str(p["ticket"])] = px
        self._save()

    def close(self, ticket):
        p = next((q for q in self.book["positions"] if q["ticket"] == ticket), None)
        if p is None:
            return _fail("POSITION_NOT_FOUND", "position not found")
        t = self.feed.tick()
        if not t:
            return _fail("BROKER_REJECTED", "no tick")
        px = t["bid"] if p["direction"] == "long" else t["ask"]
        self._settle(p, px)
        return _ok(status="CLOSED", price=px, fill_mode="sim")

    def set_sl(self, ticket, sl):
        p = next((q for q in self.book["positions"] if q["ticket"] == ticket), None)
        if p is None:
            return _fail("POSITION_NOT_FOUND")
        better = (sl > p["sl"]) if p["direction"] == "long" else (not p["sl"] or sl < p["sl"])
        if p["sl"] and not better:
            return _ok(status="ALREADY_BETTER", existing_sl=p["sl"])
        p["sl"] = float(sl); self._save()
        return _ok(status="SL_CHANGED")

    def close_price(self, ticket):
        v = self.book["closed"].get(str(ticket))
        return float(v) if v is not None else None

    def account_kind(self):
        return "demo"

    def account_login(self):
        return "SIM"

    def equity(self):
        return float(self.book.get("equity0", 0.0)) + float(self.book.get("realized", 0.0))


class LiveFeed:
    """The --dry / AUREON_PAPER feed for SimBroker: aureon_mt5.broker bars (MT5 or synthetic) and the last close as the tick."""

    def __init__(self, symbol, tf, cfg, off, spread: float = 0.25):
        self.symbol, self.tf, self.cfg, self.off, self.spread = symbol, tf, cfg, off, spread
        self._last = None

    def bars(self, n):
        rows = MT5Broker(self.symbol, self.tf, self.cfg, self.off).bars(n)
        if rows:
            self._last = rows[-1]
        return rows

    def now_server(self):
        from ... import broker
        return broker.now_server(self.off)

    def tick(self):
        from ... import broker
        t = broker.tick(self.symbol)
        if t:
            return t
        if self._last is None:
            self.bars(2)
        if self._last is None:
            return None
        c = float(self._last["close"])
        return {"bid": c, "ask": c + self.spread, "time": int(self.now_server())}
