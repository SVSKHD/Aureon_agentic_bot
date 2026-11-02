"""Read-only status export for Aureon_Agentic. Off unless AUREON_STATUS_EXPORT=1. Contract: docs/STATUS_EXPORT.md.

What it does: every AUREON_STATUS_INTERVAL_S seconds a daemon thread builds one JSON document from what the bot ALREADY
holds in memory or on disk and writes it atomically to logs/status.json (tmp + os.replace). Optionally (only when
AUREON_STATUS_TOKEN is 32+ chars) a stdlib HTTP server serves the same document:

    GET /status     the latest document (Bearer AUREON_STATUS_TOKEN, constant-time compare)
    anything else   404; any non-GET method 405

Sources (no new MT5 calls, no new computation):
  * agent.snapshot / agent.health / agent.latest_signal   — refreshed by each SymbolAgent after its poll
  * the console state builder (regime, session, timeframe) — the same pure function GET /console/state uses
  * logs/journal.jsonl tail (allowlisted events), logs/halt.json, logs/ema921e_trades.csv tail
  * out/*.json backtest summaries (the `summary` block run.py writes)

Safety: this module never imports broker or MetaTrader5 (tests/test_status_export.py checks the AST), never writes
anything but its own status file, and has no write endpoint. A failure is logged once per minute and never reaches
the agents, the guardian or Discord."""
from __future__ import annotations

import csv
import glob
import hmac
import json
import os
import threading
import time as _time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCHEMA = "aureon.mt5.status/v1"

# Journal events copied into the export. Everything else (console calls, voice, tuning, repair...) stays local.
EVENTS = frozenset({
    "signal", "position_seen", "exit", "order_placed", "pending_placed", "secured", "partial", "closed_by_rule",
    "cross_confirmed", "regime_change", "error", "process_start", "process_exit", "watchdog_open", "watchdog_clear",
})
# Fields never exported even inside an allowlisted event.
DROP_FIELDS = frozenset({"token", "auth", "authorization", "password", "secret", "webhook", "url"})


def _int(e, name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(e.get(name, default)).strip() or default)))
    except ValueError:
        return default


class Settings:
    """Env-only settings (nothing is added to Config, so a bot without these variables behaves exactly as before)."""

    def __init__(self, env=None, log_dir: str = "logs"):
        e = os.environ if env is None else env
        self.enabled = str(e.get("AUREON_STATUS_EXPORT", "0")).strip() == "1"
        self.path = str(e.get("AUREON_STATUS_FILE", "") or "").strip() or os.path.join(log_dir, "status.json")
        self.interval_s = _int(e, "AUREON_STATUS_INTERVAL_S", 15, 5, 300)
        self.max_events = _int(e, "AUREON_STATUS_EVENTS", 500, 0, 5000)
        self.journal_hours = _int(e, "AUREON_STATUS_JOURNAL_HOURS", 48, 1, 24 * 14)
        self.max_trades = _int(e, "AUREON_STATUS_TRADES", 200, 0, 5000)
        self.backtest_dir = str(e.get("AUREON_STATUS_BACKTEST_DIR", "out") or "out").strip()
        self.token = str(e.get("AUREON_STATUS_TOKEN", "") or "").strip()
        self.bind = str(e.get("AUREON_STATUS_BIND", "127.0.0.1:8788") or "").strip()
        self.allow_public = str(e.get("AUREON_STATUS_ALLOW_PUBLIC", "0")).strip() == "1"


# ----------------------------------------------------------------------------- sources (all read-only)
def _utc(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def tail_lines(path: str, max_lines: int, max_bytes: int = 4_000_000) -> list[str]:
    """The last `max_lines` lines of a text file, reading at most `max_bytes` from its end."""
    if max_lines <= 0 or not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(0, size - max_bytes)
        f.seek(start)
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]                     # the first line is probably cut
    return lines[-max_lines:]


def journal_events(log_dir: str, *, since: int, limit: int) -> list[dict]:
    """Allowlisted journal events with t >= since, oldest first, at most `limit`."""
    if limit <= 0:
        return []
    out: deque = deque(maxlen=limit)
    for line in tail_lines(os.path.join(log_dir, "journal.jsonl"), limit * 40):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict) or r.get("event") not in EVENTS:
            continue
        try:
            if int(r.get("t", 0)) < since:
                continue
        except (TypeError, ValueError):
            continue
        out.append({k: v for k, v in r.items() if str(k).lower() not in DROP_FIELDS})
    return list(out)


def halt_state(log_dir: str) -> dict | None:
    """logs/halt.json when on (read directly: aureon_mt5.halt imports broker)."""
    try:
        with open(os.path.join(log_dir, "halt.json"), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not d.get("on"):
        return None
    return {k: d.get(k) for k in ("on", "by", "reason", "t")}


def ema921e_trades(log_dir: str, limit: int) -> list[dict]:
    path = os.path.join(log_dir, "ema921e_trades.csv")
    if limit <= 0 or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        header = f.readline()
    lines = tail_lines(path, limit + 1)
    if lines and lines[0] == header.rstrip("\r\n"):
        lines = lines[1:]
    rows = list(csv.DictReader([header, *lines]))
    return rows[-limit:]


class BacktestCache:
    """`summary` blocks of out/*.json (run.py output), re-read only when a file's mtime changes."""

    def __init__(self, folder: str, limit: int = 100):
        self.folder, self.limit = folder, limit
        self._seen: dict[str, tuple[float, dict | None]] = {}

    def read(self) -> list[dict]:
        files = sorted(glob.glob(os.path.join(self.folder, "*.json")), key=lambda p: -os.path.getmtime(p))[: self.limit]
        out = []
        for p in files:
            m = os.path.getmtime(p)
            cached = self._seen.get(p)
            if cached is None or cached[0] != m:
                item = None
                try:
                    with open(p, encoding="utf-8") as f:
                        d = json.load(f)
                    if isinstance(d, dict) and isinstance(d.get("summary"), dict):
                        item = {"file": os.path.basename(p), "mode": d.get("mode"), "symbol": d.get("symbol"),
                                "summary": d["summary"], "modified_at": _utc(m)}
                except (OSError, ValueError):
                    item = None
                cached = (m, item)
                self._seen[p] = cached
            if cached[1] is not None:
                out.append(cached[1])
        return out


def agent_status(sym: str, ag) -> dict:
    """What one SymbolAgent already holds: its snapshot (positions, MT5 link, tick age, today's counters), its health
    and its latest signal. Bar times stay in broker-server epoch seconds; health.offset_h converts them to UTC."""
    snap = dict(getattr(ag, "snapshot", None) or {})
    health = dict(getattr(ag, "health", None) or {})
    strat = getattr(getattr(ag, "S", None), "name", None)
    return {
        "symbol": sym,
        "strategy": strat,
        "exec_tf": getattr(ag, "exec_tf", None),
        "alive": bool(ag.is_alive()) if hasattr(ag, "is_alive") else None,
        "snapshot_at": _utc(snap.get("at")),
        "market_open": snap.get("market_open"),
        "connected": snap.get("connected"),
        "mt5": snap.get("mt5"),
        "tick_age": snap.get("tick_age"),
        "positions": [dict(p) for p in snap.get("positions") or []],
        "today": dict(snap.get("today") or {}),
        "health": health,
        "offset_h": health.get("offset_h"),
        "latest_signal": dict(getattr(ag, "latest_signal", None) or {}) or None,
    }


def build_status(cfg, agents: dict, *, settings: Settings, state_fn=None, ema921e_fn=None, backtests: BacktestCache | None = None,
                 started: float | None = None, clock=_time.time) -> dict:
    """The whole document. Every section is independent: one failing section becomes {"error": ...}."""
    from . import config as C
    now = clock()
    log_dir = getattr(cfg, "log_dir", "logs") or "logs"
    errors: dict[str, str] = {}

    def section(name, fn, default=None):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — a section failure is reported, never raised
            errors[name] = f"{type(e).__name__}: {e}"[:200]
            return default

    doc = {
        "schema": SCHEMA,
        "generated_at": _utc(now),
        "generated_ts": int(now),
        "bot": {"version": getattr(C, "VERSION", None), "mode": getattr(cfg, "mode", None),
                "symbols": list(getattr(cfg, "symbols", []) or []), "exec_tf": str(getattr(cfg, "exec_tf", "") or "").upper(),
                "dry": bool(getattr(cfg, "dry", False)), "server_utc_offset": getattr(cfg, "server_utc_offset", None),
                "uptime_s": round(now - started) if started else None, "pid": os.getpid()},
        "agents": section("agents", lambda: {s: agent_status(s, a) for s, a in dict(agents or {}).items()}, {}),
        "halt": section("halt", lambda: halt_state(log_dir)),
        "console": section("console", (lambda: _console_subset(state_fn())) if state_fn else (lambda: None)),
        "ema921e": section("ema921e", (lambda: _ema921e(ema921e_fn)) if ema921e_fn else (lambda: None)),
        "events": section("events", lambda: journal_events(log_dir, since=int(now) - settings.journal_hours * 3600,
                                                           limit=settings.max_events), []),
        "ema921e_trades": section("ema921e_trades", lambda: ema921e_trades(log_dir, settings.max_trades), []),
        "backtests": section("backtests", lambda: backtests.read() if backtests else [], []),
    }
    doc["errors"] = errors
    return doc


def _console_subset(state: dict | None) -> dict | None:
    """Only the read-only parts of /console/state (no pending ask keys, no chart paths)."""
    if not isinstance(state, dict):
        return None
    return {k: state.get(k) for k in ("version", "t", "session", "regime", "timeframe", "network", "health")}


def _ema921e(get) -> dict | None:
    r = get()
    if r is None:
        return None
    st = r.status() if hasattr(r, "status") else None
    return {"symbol": getattr(r, "symbol", None), "tf": getattr(r, "tf", None), "enabled": getattr(r, "enabled", None),
            "status": st}


# ----------------------------------------------------------------------------- writer thread
def write_atomic(path: str, doc: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, default=str, separators=(",", ":"))
    os.replace(tmp, path)


class StatusExporter:
    """Daemon thread: build → write → keep the last document for the HTTP server. stop() ends it."""

    def __init__(self, settings: Settings, build, *, log=None):
        self.settings, self.build = settings, build
        self.log = log or (lambda msg: None)
        self.latest: bytes | None = None
        self.last_ok: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_err_log = 0.0

    def tick(self) -> bool:
        try:
            doc = self.build()
            write_atomic(self.settings.path, doc)
            self.latest = json.dumps(doc, default=str, separators=(",", ":")).encode()
            self.last_ok = _time.time()
            return True
        except Exception as e:  # noqa: BLE001 — never raise into the bot
            now = _time.time()
            if now - self._last_err_log >= 60:
                self._last_err_log = now
                self.log(f"status export failed: {e!r}")
            return False

    def start(self) -> None:
        def loop():
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(self.settings.interval_s)
        self._thread = threading.Thread(target=loop, daemon=True, name="aureon-status-export")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


# ----------------------------------------------------------------------------- optional GET-only HTTP
def http_bind(settings: Settings) -> tuple[str, int] | str:
    """(host, port), or why the server stays off."""
    from .config import LOOPBACK, console_host_port
    if len(settings.token) < 32:
        return "AUREON_STATUS_TOKEN not set (32+ chars) — status HTTP off (file export only)"
    try:
        host, port = console_host_port(settings.bind)
    except ValueError as e:
        return f"status HTTP off: {e}"
    if host not in LOOPBACK and not settings.allow_public:
        return f"status HTTP off: {host} is not loopback and AUREON_STATUS_ALLOW_PUBLIC is not 1"
    return host, port


def make_handler(settings: Settings, exporter: StatusExporter):
    token = settings.token.encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "aureon-status"
        sys_version = ""

        def log_message(self, *args):          # no access log (the Authorization header is never printed)
            return

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.split("?", 1)[0] != "/status":
                return self._send(404, b'{"error":"not_found"}')
            got = str(self.headers.get("Authorization") or "")
            if not got.startswith("Bearer ") or not hmac.compare_digest(got[7:].encode(), token):
                return self._send(401, b'{"error":"unauthorized"}')
            if exporter.latest is None:
                return self._send(503, b'{"error":"not_ready"}')
            return self._send(200, exporter.latest)

        def _refuse(self):
            self.send_response(405)
            self.send_header("Allow", "GET")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _refuse  # noqa: N815

    return Handler


class StatusServer:
    def __init__(self, settings: Settings, exporter: StatusExporter):
        self.settings, self.exporter = settings, exporter
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> str:
        b = http_bind(self.settings)
        if isinstance(b, str):
            return b
        try:
            self.httpd = ThreadingHTTPServer(b, make_handler(self.settings, self.exporter))
        except OSError as e:
            return f"status HTTP off: {e}"
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="aureon-status-http")
        self.thread.start()
        return ""

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()


def start(cfg, agents: dict, *, state_fn=None, ema921e_fn=None, started=None, log=print, env=None):
    """main.py's one call. Returns (exporter, server) or (None, None) when AUREON_STATUS_EXPORT is not 1."""
    settings = Settings(env, log_dir=getattr(cfg, "log_dir", "logs") or "logs")
    if not settings.enabled:
        return None, None
    bt = BacktestCache(settings.backtest_dir)
    exporter = StatusExporter(settings, lambda: build_status(cfg, agents, settings=settings, state_fn=state_fn,
                                                             ema921e_fn=ema921e_fn, backtests=bt, started=started), log=log)
    exporter.tick()
    exporter.start()
    server = StatusServer(settings, exporter)
    why = server.start()
    log(f"status export: {settings.path} every {settings.interval_s} s; "
        + (why or f"HTTP GET /status on {settings.bind}"))
    return exporter, (server if not why else None)
