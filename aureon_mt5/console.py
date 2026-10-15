"""v2.11.0 — the action console: the phone's TAKE / SKIP / CLOSE / READ land here. Contract: docs/CONSOLE.md.

    POST /console/action          {"action": take|skip|close|read, "symbol": "XAUUSD", "ask_key": "...", "lots": 0.1?}
    GET  /console/state           {"v": 1, regime, session, positions, pending_ask, last_reads, health}
    GET  /console/chart/<key>.png the ask's chart (ask key) or a chart file by name

Security: binds AUREON_CONSOLE_BIND (127.0.0.1:8787) — a non-loopback bind is refused unless AUREON_CONSOLE_ALLOW_PUBLIC=1
(which /env-check flags loudly); reach it from the phone over Tailscale (`tailscale serve`). Bearer auth: the console token
(AUREON_CONSOLE_TOKEN, ≥ 32 chars, constant-time compare) or a scoped single-use action token from push.ActionTokens
(only for that action on that ask). 20 requests / minute per client. Every call is journaled `console_call` with
source="push". The console never touches MetaTrader5: actions go through aureon_mt5.actions (the /take /skip /close path).
uvicorn runs on its own thread, started and stopped with the bot."""

# no `from __future__ import annotations`: FastAPI must resolve the locally imported Request annotation at def time
import hmac
import os
import re
import threading
import time as _time
from collections import deque
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
RATE_N, RATE_WINDOW_S = 20, 60.0
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]+\.png$")


class RateLimit:
    def __init__(self, n=RATE_N, window=RATE_WINDOW_S, clock=_time.monotonic):
        self.n, self.window, self.clock = n, window, clock
        self.hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, who: str) -> bool:
        now = self.clock()
        with self._lock:
            q = self.hits.setdefault(who, deque())
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.n:
                return False
            q.append(now)
            return True


def master_ok(cfg, header: str | None) -> bool:
    tok = str(getattr(cfg, "console_token", "") or "")
    got = str(header or "")
    if len(tok) < 32 or not got.startswith("Bearer "):
        return False
    return hmac.compare_digest(got[7:].encode(), tok.encode())


# ----------------------------------------------------------------------------- /console/state (pure)
def console_state(cfg, agents: dict, actions, notify=None, *, started: float | None = None, clock=_time.time) -> dict:
    from . import config as C
    from . import repair, server
    now = clock()
    regime, positions = {}, []
    for sym, ag in (agents or {}).items():
        tr = getattr(ag, "regime_tracker", None)
        if tr is not None:
            regime[sym] = {"state": tr.state, "score": round(float(tr.score), 3), "reasons": list(getattr(tr, "reasons", []) or []),
                           "gate": getattr(cfg, "regime_gate", "off"), "blocks": bool(tr.blocks())}
        for p in (getattr(ag, "snapshot", {}) or {}).get("positions") or []:
            positions.append({"symbol": sym, "ticket": p.get("ticket"), "direction": p.get("direction"),
                              "entry": p.get("price_open"), "now": p.get("current"), "points": p.get("points"), "sl": p.get("sl") or None})
    ask = actions.latest_open() if actions is not None else None
    pending = None
    if ask is not None:
        m = ask.get("meta") or {}
        age = actions.age_s(ask); ttl = float(getattr(cfg, "ask_ttl_min", 20)) * 60
        why = actions.refusal(ask, "TAKE")
        pending = {"key": ask["key"], "symbol": m.get("symbol"), "side": m.get("side") or m.get("suggested_side"),
                   "kind": m.get("kind"), "title": ask.get("title"), "price": m.get("price"), "age_s": round(age, 1),
                   "expires_in_s": round(max(0.0, ttl - age), 1), "take_refused": why,
                   "chart": f"/console/chart/{ask['key']}.png" if ask.get("png") else None,
                   "actions": ["take", "skip", "read"]}
    ist_now = datetime.fromtimestamp(now, tz=IST)
    return {
        "v": 1,
        "version": C.VERSION,
        "t": ist_now.isoformat(timespec="seconds"),
        "session": {"ist": ist_now.strftime("%H:%M"), "window": getattr(cfg, "session_ist", "05:30-23:00"),
                    "in_session": repair.in_session(cfg, ist_now)},
        "regime": regime,
        "positions": positions,
        "pending_ask": pending,
        "last_reads": list(getattr(actions, "last_reads", []) or [])[-5:],
        "network": _network_state(),
        "health": {"uptime_s": round(now - started) if started else None,
                   "discord_ok": bool(getattr(notify, "last_ok", True)),
                   "network_degraded": bool(server.network_degraded()),
                   "push": [b.lower() for b in (getattr(cfg, "push", ()) or ())],
                   "agents": {s: {"status": (getattr(a, "health", {}) or {}).get("status"),
                                  "last_bar": (getattr(a, "health", {}) or {}).get("last_bar")} for s, a in (agents or {}).items()}},
    }


def _network_state() -> dict:
    """v2.11.2: degraded flag, why, since when, and the entry asks suppressed meanwhile (never auto-taken)."""
    from . import netguard
    st = netguard.STATE
    return {"degraded": bool(st["degraded"]), "reasons": list(st["reasons"]), "since": st["since"],
            "suppressed_asks": list(st["suppressed"])[-10:], "dns": netguard.dns_line()}


# ----------------------------------------------------------------------------- the app
def create_app(cfg, actions, journal, *, tokens=None, state_fn=None, chart_dir: str | None = None, rate: RateLimit | None = None):
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse

    app = FastAPI(title="Aureon console", version="1", docs_url=None, redoc_url=None, openapi_url=None)
    rate = rate or RateLimit()
    chart_dir = os.path.realpath(chart_dir or os.path.join(cfg.log_dir, "charts"))

    def log(request, path, status, **kw):
        try:
            journal.log("console_call", source="push", path=path, status=status,
                        client=getattr(request.client, "host", None) if request.client else None, **kw)
        except Exception:
            pass

    def gate(request, path):
        who = getattr(request.client, "host", "?") if request.client else "?"
        if not rate.allow(who):
            log(request, path, 429)
            return JSONResponse({"ok": False, "text": "rate limited (20 / min)"}, status_code=429)
        return None

    @app.post("/console/action")
    async def action(request: Request):
        if (r := gate(request, "/console/action")) is not None:
            return r
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            log(request, "/console/action", 400)
            return JSONResponse({"ok": False, "text": "body must be a JSON object"}, status_code=400)
        act = str(body.get("action", "")).lower(); sym = str(body.get("symbol") or ""); key = str(body.get("ask_key") or "")
        auth = request.headers.get("authorization", "")
        scoped = None
        if not master_ok(cfg, auth):
            scoped = (tokens.verify(auth[7:], act, sym, key) if (tokens is not None and auth.startswith("Bearer act."))
                      else "missing or wrong bearer token")
            if scoped:
                log(request, "/console/action", 401, action=act, symbol=sym, key=key, why=scoped)
                return JSONResponse({"ok": False, "text": "unauthorized", "reasons": [scoped]}, status_code=401)
        lots = body.get("lots")
        try:
            lots = float(lots) if lots not in (None, "") else None
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "text": "lots must be a number"}, status_code=400)
        from starlette.concurrency import run_in_threadpool
        res = await run_in_threadpool(actions.run, act, symbol=sym or None, ask_key=key or None, lots=lots, user="console:push")
        status = 200 if res.get("ok") else 409
        log(request, "/console/action", status, action=act, symbol=sym, key=key, ok=bool(res.get("ok")), reasons=res.get("reasons"))
        return JSONResponse({k: v for k, v in res.items() if k in ("ok", "text", "reasons", "key", "placed", "ticket", "pending")},
                            status_code=status)

    @app.get("/console/state")
    async def state(request: Request):
        if (r := gate(request, "/console/state")) is not None:
            return r
        if not master_ok(cfg, request.headers.get("authorization")):
            log(request, "/console/state", 401)
            return JSONResponse({"ok": False, "text": "unauthorized"}, status_code=401)
        log(request, "/console/state", 200)
        return JSONResponse(state_fn() if state_fn else {"v": 1})

    @app.get("/console/chart/{name:path}")
    async def chart(request: Request, name: str):
        if (r := gate(request, "/console/chart")) is not None:
            return r
        if not master_ok(cfg, request.headers.get("authorization")):
            log(request, "/console/chart", 401)
            return JSONResponse({"ok": False, "text": "unauthorized"}, status_code=401)
        key = name[:-4] if name.endswith(".png") else name
        item = (actions.asks() or {}).get(key) if actions is not None else None
        path = item.get("png") if item else (os.path.join(chart_dir, name) if SAFE_NAME.match(name) else None)
        real = os.path.realpath(path) if path else ""
        if not real or not real.startswith(chart_dir + os.sep) or not os.path.isfile(real):
            log(request, "/console/chart", 404, key=key)
            return JSONResponse({"ok": False, "text": "no such chart"}, status_code=404)
        log(request, "/console/chart", 200, key=key)
        return FileResponse(real, media_type="image/png")

    return app


# ----------------------------------------------------------------------------- the thread
class ConsoleServer:
    """uvicorn on its own thread. start() returns why it did not start ('' = started)."""

    def __init__(self, cfg, app):
        self.cfg, self.app = cfg, app
        self.server = None
        self.thread = None

    def bind(self) -> tuple[str, int] | str:
        from .config import LOOPBACK, console_host_port
        if len(str(getattr(self.cfg, "console_token", "") or "")) < 32:
            return "AUREON_CONSOLE_TOKEN not set (32+ chars) — console off"
        try:
            host, port = console_host_port(self.cfg.console_bind)
        except ValueError as e:
            return f"console off: {e}"
        if host not in LOOPBACK and not getattr(self.cfg, "console_allow_public", False):
            return f"console off: {host} is not loopback and AUREON_CONSOLE_ALLOW_PUBLIC is not 1"
        return host, port

    def start(self, backoff=(5.0, 60.0)) -> str:
        """v2.11.2: the uvicorn thread restarts itself (backoff 5 s → 60 s) when uvicorn dies or exits on its own — a bind
        failure, a network error, anything. Only stop() ends it. Never raises into the bot."""
        b = self.bind()
        if isinstance(b, str):
            return b
        import uvicorn  # type: ignore
        host, port = b
        self._stopping = False
        self.restarts = 0

        def make():
            return uvicorn.Server(uvicorn.Config(self.app, host=host, port=port, log_level="warning", access_log=False,
                                                 lifespan="off"))

        def loop():
            from . import netguard
            delay = backoff[0]
            while not self._stopping:
                self.server = make()
                t0 = _time.monotonic()
                try:
                    self.server.run()
                    why = "uvicorn returned"
                except BaseException as e:                    # SystemExit from a failed bind included
                    why = f"{type(e).__name__}: {e}"
                if self._stopping:
                    break
                self.restarts += 1
                if _time.monotonic() - t0 > 120:
                    delay = backoff[0]
                netguard.rlog("console", f"console stopped ({why}) — restarting in {delay:.0f} s")
                end = _time.monotonic() + delay
                while not self._stopping and _time.monotonic() < end:
                    _time.sleep(min(0.2, delay))
                delay = min(backoff[1], delay * 2)

        self.thread = threading.Thread(target=loop, name="aureon-console", daemon=True)
        self.thread.start()
        return ""

    def stop(self, timeout: float = 5.0):
        self._stopping = True
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout)
