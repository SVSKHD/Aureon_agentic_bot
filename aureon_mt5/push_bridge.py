"""Read-only PUSH bridge to the Aureon server. Off unless AUREON_PUSH_ENABLED=1. Contract: docs/PUSH_BRIDGE.md.

Every AUREON_PUSH_INTERVAL_S seconds a daemon thread builds one `aureon.mt5.push/v1` document — a superset of
`aureon.mt5.status/v1` (status_export.build_status) — and POSTs it to AUREON_PUSH_URL with
`Authorization: Bearer <MT5_INGEST_TOKEN>`:

    status/v1 sections  + health, guardian, signals_latest, events, tracebacks, strategy, candles

Sources (no new MT5 calls, no new computation of trading decisions):
  * everything status/v1 reads (agent snapshot / health / latest_signal, console state, halt.json, backtests ...)
  * logs/journal.jsonl from an unacked byte cursor (logs/push_cursor.json) — the cursor only moves on a 2xx answer
  * telemetry.RECENT (the in-process failure ring with tracebacks) and journal `agent_crash` rows
  * the bars each agent already caches: `last_df` (exec TF) and `_htf_frames_cache` (confirm TFs) — never MT5
  * notify.last_ok / netguard.STATE for the Discord state

Safety: never imports broker or MetaTrader5 (tests/test_push_bridge.py checks the AST); places, modifies and cancels
nothing; writes only logs/push_cursor.json. Keys that look secret are dropped at every depth. The token is never
logged, printed or put in an exception text. A failure is logged at most once a minute and never reaches the agents."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import threading
import time as _time
import urllib.error
import urllib.request
import uuid

from . import status_export as sx

SCHEMA = "aureon.mt5.push/v1"
DEFAULT_URL = "https://svskhd8.taila439d5.ts.net/api/trading/mt5/ingest"
MAX_BODY = int(1.5 * 1024 * 1024)            # server refuses > 2 MiB; stay well under it (uncompressed size)
MAX_EVENTS = 2000                            # per push; the rest goes next time (cursor)
MAX_TRACEBACKS = 20
MAX_TB_TEXT = 8 * 1024
READ_CHUNK = 4_000_000                       # journal bytes read per push
TIMEOUT_S = 10

# Journal events pushed: the status/v1 allowlist plus the guardian / crash rows the dashboard explains failures with.
EVENTS = sx.EVENTS | frozenset({
    "protected", "pre_cross", "locked", "early_lock", "soft_lock_exit", "soft_lock_cleared", "setup_invalidated",
    "trail_update", "structure_sl", "pending_filled", "pending_expired", "pending_cancelled", "agent_crash",
    "signal_suppressed", "cross_skipped", "pre_timeout_hold", "self_restart", "state_rebuilt", "entry_blocked_htf",
})
# Journal rows that mean the guardian acted on a ticket (a manual position with one becomes guardian_touched).
GUARDIAN_EVENTS = frozenset({"protected", "secured", "trail_update", "structure_sl", "locked", "early_lock",
                             "soft_lock_exit", "partial", "closed_by_rule", "exit", "pre_cross"})
# A key containing any of these (case-insensitive) is dropped at every depth.
SECRET_PARTS = tuple(sorted(set(sx.DROP_FIELDS) | {"token", "auth", "password", "secret", "webhook", "url", "login",
                                                   "account"}))


def _int(e, name, default, lo, hi):
    return sx._int(e, name, default, lo, hi)


class Settings:
    """Env-only. Nothing is added to Config: a bot without these variables behaves exactly as before."""

    def __init__(self, env=None, log_dir: str = "logs"):
        e = os.environ if env is None else env
        self.enabled = str(e.get("AUREON_PUSH_ENABLED", "0")).strip() == "1"
        self.url = str(e.get("AUREON_PUSH_URL", "") or "").strip() or DEFAULT_URL
        self._token = str(e.get("MT5_INGEST_TOKEN", "") or "").strip()
        status_iv = _int(e, "AUREON_STATUS_INTERVAL_S", 15, 5, 300)
        self.interval_s = _int(e, "AUREON_PUSH_INTERVAL_S", status_iv, 5, 300)
        self.gzip = str(e.get("AUREON_PUSH_GZIP", "1")).strip() != "0"
        self.candles = _int(e, "AUREON_PUSH_CANDLES", 200, 0, 300)
        self.journal_hours = _int(e, "AUREON_STATUS_JOURNAL_HOURS", 48, 1, 24 * 14)   # first push only (no cursor yet)
        self.log_dir = log_dir
        self.cursor_path = os.path.join(log_dir, "push_cursor.json")

    @property
    def token(self) -> str:
        return self._token

    def problem(self) -> str:
        """Why the bridge cannot start ('' = fine). Never contains the token."""
        if len(self._token) < 32:
            return "MT5_INGEST_TOKEN not set or shorter than 32 chars — push bridge off"
        if not self.url.lower().startswith(("https://", "http://")):
            return "AUREON_PUSH_URL is not an http(s) URL — push bridge off"
        return ""

    def __repr__(self) -> str:                  # the token never shows up in a repr / traceback
        return f"push_bridge.Settings(enabled={self.enabled}, url={self.url!r}, interval_s={self.interval_s}, token=***)"


# ----------------------------------------------------------------------------- helpers (pure)
def _secretish(key) -> bool:
    k = str(key).lower()
    return any(p in k for p in SECRET_PARTS)


def scrub(obj):
    """Drop secret-looking keys at every depth (dicts inside lists included). The v1 rule, applied recursively and by
    substring (token/auth/password/secret/webhook/url/login/account)."""
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if not _secretish(k)}
    if isinstance(obj, (list, tuple)):
        return [scrub(v) for v in obj]
    return obj


def event_id(rec: dict, offset: int) -> str:
    """Stable id: sha1(t|event|symbol|ticket|bar|offset) — offset is the line's byte offset in journal.jsonl (append-only),
    so the same row always gets the same id, and two identical rows in the same second still differ."""
    parts = [rec.get("t"), rec.get("event"), rec.get("symbol"), rec.get("ticket"), rec.get("bar"), offset]
    return hashlib.sha1("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f              # NaN → None (JSON has no NaN)


def read_journal(path: str, offset: int, *, max_bytes: int = READ_CHUNK) -> tuple[list[tuple[int, int, dict]], int]:
    """Complete lines from `offset`: [(line_offset, line_end, record)] for every parseable dict row, and the offset after
    the last complete line read. A file shorter than `offset` (rotated / replaced) restarts at 0."""
    if not os.path.exists(path):
        return [], 0
    size = os.path.getsize(path)
    if offset > size:
        offset = 0
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(max_bytes)
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], offset
    data = data[: cut + 1]
    out, pos = [], offset
    for raw in data.split(b"\n")[:-1]:
        start, pos = pos, pos + len(raw) + 1
        try:
            r = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append((start, pos, r))
    return out, offset + cut + 1


def _sig_kind(rec: dict) -> str | None:
    ev = rec.get("event")
    if ev == "signal":
        k = str(rec.get("signal_kind") or "signal")
        return "P" if k.lower() in ("p", "pre", "pb", "ps") else k
    return {"protected": "protected", "secured": "secured", "exit": "exit", "closed_by_rule": "closed",
            "error": "error", "agent_crash": "error", "cross_confirmed": "cross_confirmed", "pre_cross": "pre_cross",
            "partial": "partial", "locked": "locked"}.get(ev)


def _where(title: str, action: str = "", key: str = "") -> str:
    s = f"{title} {action}".lower()
    if "guardian" in s:
        return "guardian"
    if "discord" in s or "webhook" in s:
        return "discord"
    parts = str(key or "").split(":")
    if len(parts) >= 2 and parts[1] and parts[1].upper() == parts[1] and parts[1].isalnum():
        return f"agent:{parts[1]}"
    return "main"


def _tb(t, where, typ, message, text) -> dict:
    tid = hashlib.sha1(f"{t}|{where}|{typ}|{message}".encode()).hexdigest()
    return {"id": tid, "t": t, "where": where, "type": typ, "message": str(message or "")[:500],
            "traceback": str(text or "")[-MAX_TB_TEXT:]}


# ----------------------------------------------------------------------------- push-only sections
def _session(a: dict, ag) -> str | None:
    """asia / london / ny / off for now (the agents' own session table), else the agent's last seen session."""
    try:
        from .common.news import session_of
        off = float(a.get("offset_h"))
        return session_of(int(_time.time() + off * 3600), off)
    except Exception:  # noqa: BLE001
        return getattr(ag, "last_session", None)


def health_section(doc: dict, agents: dict, notify=None, *, stale_tick_s: float = 120.0) -> dict:
    ag = doc.get("agents") or {}
    conns = [a.get("connected") for a in ag.values() if isinstance(a, dict)]
    terminals = [a.get("mt5") for a in ag.values() if isinstance(a, dict) and a.get("mt5")]
    connected = all(c is not False for c in conns) if conns else None
    mt5_err = None
    for s, a in ag.items():
        st = str(((a or {}).get("health") or {}).get("status") or "")
        if st.startswith("error"):
            mt5_err = f"{s}: {st}"[:300]
    discord_ok, d_err, d_at = True, None, None
    if notify is not None:
        discord_ok = bool(getattr(notify, "last_ok", True))
    try:
        from . import netguard
        st = getattr(netguard, "STATE", {}) or {}
        streak = int(st.get("webhook_fail_streak") or 0)
        if streak:
            discord_ok = False
            d_err = f"webhook failed {streak}x in a row"
        if st.get("webhook_down_since"):
            d_at = sx._utc(st["webhook_down_since"])
    except Exception:  # noqa: BLE001
        pass
    if not discord_ok and d_err is None:
        d_err = "last webhook post failed"
    market = {}
    stale = False
    for s, a in ag.items():
        if not isinstance(a, dict):
            continue
        tick = _num(a.get("tick_age"))
        market[s] = {"open": a.get("market_open"), "tick_age_s": tick, "session": _session(a, (agents or {}).get(s))}
        if a.get("market_open") and tick is not None and tick > stale_tick_s:
            stale = True
    dead = any(isinstance(a, dict) and a.get("alive") is False for a in ag.values())
    if connected is False or dead:
        status = "error"
    elif not discord_ok or stale or mt5_err or doc.get("errors"):
        status = "degraded"
    else:
        status = "ok"
    return {"status": status,
            "mt5": {"connected": connected, "terminal": terminals[0] if terminals else None, "last_error": mt5_err},
            "discord": {"ok": discord_ok, "last_error": d_err, "last_error_at": d_at},
            "market": market}


def guardian_section(doc: dict, agents: dict) -> dict:
    """One row per guarded ticket from the agent's cached snapshot (positions + guardian state). Levels are prices:
    secured_level = entry ± secured points, peak = entry ± peak points (the raw points are kept as *_pts)."""
    out: dict[str, list] = {}
    for sym, ag in (agents or {}).items():
        snap = dict(getattr(ag, "snapshot", None) or {})
        states = snap.get("state") or {}
        g = getattr(ag, "g", None)
        rows = []
        for p in snap.get("positions") or []:
            t = p.get("ticket")
            st = states.get(t) or states.get(str(t)) or {}
            if not isinstance(st, dict) or not st:
                continue
            s = 1 if str(p.get("direction")) == "long" else -1
            entry = _num(p.get("price_open")) if p.get("price_open") is not None else _num(st.get("entry"))
            secured = _num(st.get("secured")) or 0.0
            peak = _num(st.get("peak"))
            prof = st.get("guardian_profile") or getattr(ag, "_resolved_profile", None)
            pre = bool(st.get("pre"))
            if prof == "leg":
                phase = "ride" if st.get("leg_phase") == "B" else ("pre" if pre else "protect")
            elif pre:
                phase = "pre"
            elif g is not None and secured > 0 and secured >= float(getattr(g, "secure_level", 0) or 0):
                phase = "ride" if secured > float(getattr(g, "secure_level", 0) or 0) else "secured"
            elif secured > 0 and g is None:
                phase = "secured"
            else:
                phase = "protect"
            rows.append({
                "ticket": t, "phase": phase, "leg_phase": st.get("leg_phase"), "profile": prof,
                "sl": _num(p.get("sl")) or None,
                "secured_level": round(entry + s * secured, 5) if (entry is not None and secured > 0) else None,
                "peak": round(entry + s * peak, 5) if (entry is not None and peak is not None) else None,
                "secured_pts": secured, "peak_pts": peak, "direction": p.get("direction"), "entry": entry,
                "bars": st.get("bars"), "frozen": bool(st.get("frozen")), "updated_at": sx._utc(snap.get("at")),
            })
        if rows:
            out[sym] = rows
    return out


def strategy_section(cfg, agents: dict) -> dict:
    exec_tf = str(getattr(cfg, "exec_tf", "") or "M5").upper()
    tfs = [exec_tf]
    if str(getattr(cfg, "htf_align", "off") or "off").lower() != "off":
        for tf in getattr(cfg, "htf_confirm_tfs", ()) or ():
            if str(tf).upper() not in tfs:
                tfs.append(str(tf).upper())
    ema, guardians, profiles = {}, {}, {}
    for sym, ag in (agents or {}).items():
        S = getattr(ag, "S", None)
        if S is not None and not ema:
            ema = {"fast": getattr(S, "fast", None), "slow": getattr(S, "slow", None)}
        g = getattr(ag, "g", None)
        if g is not None:
            try:
                import dataclasses
                guardians[sym] = dataclasses.asdict(g) if dataclasses.is_dataclass(g) else dict(vars(g))
            except Exception:  # noqa: BLE001
                pass
        profiles[sym] = getattr(ag, "_resolved_profile", None)
    params = {k: getattr(cfg, k, None) for k in ("poll_seconds", "bars", "htf_align", "htf_align_min", "giveback_pct",
                                                  "giveback_arm", "setup_stop", "dry")}
    params["htf_confirm_tfs"] = list(getattr(cfg, "htf_confirm_tfs", ()) or ())
    params["guardian"] = guardians
    params["guardian_profile"] = profiles
    return {"mode": getattr(cfg, "mode", None), "symbols": list(getattr(cfg, "symbols", []) or []), "exec_tf": exec_tf,
            "timeframes": tfs, "ema": ema, "params": params}


def _frame_rows(df, n: int, cols: tuple, periods: tuple) -> list[dict]:
    if df is None or n <= 0:
        return []
    d = df.tail(n)
    names = list(d.columns)
    out = []
    for r in d.itertuples(index=False):
        row = dict(zip(names, r))
        e = {str(p): _num(row.get(c)) for c, p in zip(cols, periods) if c in row and p}
        out.append({"t": int(row["time"]), "o": _num(row.get("open")), "h": _num(row.get("high")), "l": _num(row.get("low")),
                    "c": _num(row.get("close")), "v": _num(row.get("volume")), "ema": e})
    return out


def candles_section(cfg, agents: dict, n: int) -> dict:
    """The bars the agents already hold: `last_df` (exec TF, EMAs added) and the HTF confirm frames they cached."""
    out: dict[str, dict] = {}
    if n <= 0:
        return out
    for sym, ag in (agents or {}).items():
        S = getattr(ag, "S", None)
        cols = tuple(getattr(S, "ema_cols", ("ema_fast", "ema_slow")) or ())
        periods = (getattr(S, "fast", None), getattr(S, "slow", None))
        tfmap = {}
        exec_tf = str(getattr(ag, "exec_tf", None) or getattr(cfg, "exec_tf", "M5") or "M5").upper()
        rows = _frame_rows(getattr(ag, "last_df", None), n, cols, periods)
        if rows:
            tfmap[exec_tf] = rows
        cache = getattr(ag, "_htf_frames_cache", None)
        if isinstance(cache, tuple) and len(cache) == 2 and isinstance(cache[1], dict):
            for tf, df in cache[1].items():
                rows = _frame_rows(df, n, cols, periods)
                if rows:
                    tfmap[str(tf).upper()] = rows
        if tfmap:
            out[sym] = tfmap
    return out


def _server_offset(doc: dict) -> float:
    """Broker server time − UTC in hours, as the agents measured it (status/v1 agents.*.offset_h)."""
    for a in (doc.get("agents") or {}).values():
        off = _num((a or {}).get("offset_h")) if isinstance(a, dict) else None
        if off is not None:
            return off
    return 0.0


def _touched(pend, guardian) -> set:
    """Tickets the guardian acted on: guardian journal rows in this push, and tickets it holds state for right now."""
    out = set()
    for _, ev, _ in pend or []:
        if ev.get("event") in GUARDIAN_EVENTS and ev.get("ticket") is not None:
            out.add(ev["ticket"])
    for rows in (guardian or {}).values():
        for r in rows or []:
            if r.get("ticket") is not None:
                out.add(r["ticket"])
    return out


# ----------------------------------------------------------------------------- the bridge
class PushBridge:
    """Daemon thread: build → trim → POST → advance the cursor on 2xx. stop() ends it."""

    def __init__(self, settings: Settings, build_base, *, cfg=None, agents=None, notify=None, log=None, opener=None,
                 clock=_time.time, trades=None):
        self.settings, self.build_base = settings, build_base
        self.cfg, self.agents, self.notify = cfg, agents if agents is not None else {}, notify
        self.log = log or (lambda msg: None)
        self.opener = opener or urllib.request.build_opener()
        self.clock = clock
        self.seq = 0
        self.journal_path = os.path.join(settings.log_dir, "journal.jsonl")
        cur = self._load_cursor()
        self.trades = trades                       # trade_history.Collector (injected by main.py: this module never reads MT5)
        if trades is not None:
            trades.restore(cur.get("trades") or {})
        self.cursor: int | None = cur.get("journal_offset")
        self.tb_acked_t: float = float(cur.get("tb_t") or clock())
        self.min_t: int | None = None if self.cursor is not None else int(clock()) - settings.journal_hours * 3600
        self.scan_off: int | None = None
        self.latest: dict[str, dict] = {}
        self.last_status: int | None = None
        self.last_ok: float | None = None
        self._last_err_log = 0.0
        self._failing = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- cursor (logs/push_cursor.json — the only file this module writes)
    def _load_cursor(self) -> dict:
        try:
            with open(self.settings.cursor_path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_cursor(self) -> None:
        try:
            doc = {"journal_offset": self.cursor, "tb_t": self.tb_acked_t}
            if self.trades is not None:
                doc["trades"] = self.trades.state()
            sx.write_atomic(self.settings.cursor_path, doc)
        except OSError:
            pass

    def _initial_offset(self) -> int:
        try:
            size = os.path.getsize(self.journal_path)
        except OSError:
            return 0
        return max(0, size - READ_CHUNK)

    # ---- sections fed by the journal
    def _update_latest(self) -> None:
        if self.scan_off is None:
            self.scan_off = self._initial_offset()
        while True:
            rows, end = read_journal(self.journal_path, self.scan_off)
            for _, _, r in rows:
                k = _sig_kind(r)
                if k:
                    sym = str(r.get("symbol") or "_")
                    self.latest.setdefault(sym, {})[k] = {kk: v for kk, v in r.items() if kk != "trace"}
            moved = end != self.scan_off
            self.scan_off = end
            if not moved or not rows:
                break

    def _pending_events(self) -> tuple[list[tuple[int, dict, dict | None]], int]:
        """[(line_end, event, crash_traceback|None)] since the acked cursor, oldest first, and the end offset of what was
        read (used as the cursor when every event fits)."""
        start = self.cursor if self.cursor is not None else self._initial_offset()
        rows, end = read_journal(self.journal_path, start)
        out = []
        for off, line_end, r in rows:
            if r.get("event") not in EVENTS:
                continue
            try:
                t = int(r.get("t", 0))
            except (TypeError, ValueError):
                continue
            if self.min_t is not None and t < self.min_t:
                continue
            tb = None
            if r.get("event") == "agent_crash":
                exc = str(r.get("exc") or "")
                typ = exc.split(":", 1)[0] if ":" in exc else "Exception"
                tb = _tb(t, f"agent:{r.get('symbol')}", typ, exc, r.get("trace"))
            ev = {k: v for k, v in r.items() if k != "trace"}
            ev["id"] = event_id(r, off)
            out.append((line_end, ev, tb))
            if len(out) >= MAX_EVENTS:
                return out, line_end
        return out, end

    def _recent_tracebacks(self) -> list[dict]:
        try:
            from . import telemetry
            recent = list(telemetry.RECENT)
        except Exception:  # noqa: BLE001
            return []
        out = []
        for r in recent:
            t = float(r.get("t") or 0)
            if t <= self.tb_acked_t:
                continue
            out.append(_tb(int(t), _where(r.get("title") or "", r.get("action") or "", r.get("key") or ""),
                           r.get("exc_type") or "Error", r.get("exc") or r.get("title") or "", r.get("tb") or ""))
            out[-1]["_t"] = t
        return out

    # ---- build
    def build(self) -> tuple[dict, int, float]:
        """(document, cursor to commit on 2xx, traceback time to commit on 2xx)."""
        doc = self.build_base()
        errors = doc.setdefault("errors", {})

        def section(name, fn, default=None):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                errors[name] = f"{type(e).__name__}: {e}"[:200]
                return default

        self.seq += 1
        doc["schema"] = SCHEMA
        doc["push_id"] = str(uuid.uuid4())
        doc["seq"] = self.seq
        doc["health"] = section("health", lambda: health_section(doc, self.agents, self.notify), {"status": "error"})
        doc["guardian"] = section("guardian", lambda: guardian_section(doc, self.agents), {})
        section("signals_latest", self._update_latest)
        doc["signals_latest"] = {s: dict(v) for s, v in self.latest.items()}
        pend, end = section("events", self._pending_events, ([], self.cursor or 0))
        crash_tbs = [tb for _, _, tb in pend if tb]
        recent = section("tracebacks", self._recent_tracebacks, [])
        doc["strategy"] = section("strategy", lambda: strategy_section(self.cfg, self.agents), None)
        doc["candles"] = section("candles", lambda: candles_section(self.cfg, self.agents, self.settings.candles), {})
        if self.trades is not None:
            doc["trades"] = section("trades", lambda: self.trades.collect(
                off_h=_server_offset(doc), touched=_touched(pend, doc.get("guardian"))), None)

        # size cap: candles first, then the newest events (the cursor stops before them, so they go next time)
        recent_tbs = [{k: v for k, v in r.items() if k != "_t"} for r in recent]
        tb_t = max([r["_t"] for r in recent], default=self.tb_acked_t)

        def fill(keep: int) -> None:
            doc["events"] = scrub([e for _, e, _ in pend[:keep]])
            doc["tracebacks"] = scrub(([tb for _, _, tb in pend[:keep] if tb] + recent_tbs)[-MAX_TRACEBACKS:])

        fill(len(pend))
        doc = scrub(doc)
        cut = end
        n = self.settings.candles
        while doc.get("candles") and self._size(doc) > MAX_BODY:
            n //= 2
            doc["candles"] = scrub(candles_section(self.cfg, self.agents, n)) if n >= 10 else {}
            doc["errors"]["candles"] = f"trimmed to {n if n >= 10 else 0} bars: body over the size cap"
        if self._size(doc) > MAX_BODY:
            keep = len(pend)
            while keep > 0 and self._size(doc) > MAX_BODY:
                keep = keep // 2 if keep > 8 else keep - 1
                fill(keep)
            cut = pend[keep - 1][0] if keep > 0 else (self.cursor if self.cursor is not None else self._initial_offset())
            doc["errors"]["events"] = f"trimmed: {len(pend) - keep} events left for the next push"
        return doc, cut, tb_t

    @staticmethod
    def _size(doc) -> int:
        return len(json.dumps(doc, default=str, separators=(",", ":")).encode())

    # ---- send
    def _post(self, doc: dict) -> int:
        body = json.dumps(doc, default=str, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + self.settings.token,
                   "X-Aureon-Push-Id": doc["push_id"], "User-Agent": "aureon-mt5-push/1"}
        if self.settings.gzip:
            body = gzip.compress(body, mtime=0)
            headers["Content-Encoding"] = "gzip"
        req = urllib.request.Request(self.settings.url, data=body, headers=headers, method="POST")
        try:
            with self.opener.open(req, timeout=TIMEOUT_S) as r:
                r.read(4096)
                return int(r.status)
        except urllib.error.HTTPError as e:
            return int(e.code)

    def _redact(self, text: str) -> str:
        tok = self.settings.token
        return text.replace(tok, "***") if tok else text

    def tick(self) -> bool:
        """One push. True on 2xx (the cursor moved). Never raises."""
        try:
            doc, cut, tb_t = self.build()
            code = self._post(doc)
            self.last_status = code
            if 200 <= code < 300:
                self.cursor, self.tb_acked_t, self.min_t = cut, tb_t, None
                if self.trades is not None:
                    self.trades.commit()
                self._save_cursor()
                self.last_ok = self.clock()
                if self._failing:
                    self._failing = False
                    self.log(f"aureon push: delivered again (HTTP {code})")
                return True
            self._fail(f"HTTP {code}")
            return False
        except Exception as e:  # noqa: BLE001 — never raise into the bot
            self._fail(self._redact(f"{type(e).__name__}: {getattr(e, 'reason', None) or e}")[:200])
            return False

    def _fail(self, why: str) -> None:
        self._failing = True
        now = self.clock()
        if now - self._last_err_log >= 60:
            self._last_err_log = now
            self.log(f"aureon push failed: {why} (events kept for the next push)")

    def start(self) -> None:
        def loop():
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(self.settings.interval_s)
        self._thread = threading.Thread(target=loop, daemon=True, name="aureon-push-bridge")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def start(cfg, agents: dict, *, notify=None, state_fn=None, ema921e_fn=None, started=None, log=print, env=None, opener=None,
          trades=None):
    """main.py's one call. Returns the running PushBridge, or None when AUREON_PUSH_ENABLED is not 1 or it cannot start."""
    log_dir = getattr(cfg, "log_dir", "logs") or "logs"
    settings = Settings(env, log_dir=log_dir)
    if not settings.enabled:
        return None
    why = settings.problem()
    if why:
        log(why)
        return None
    st = sx.Settings(env, log_dir=log_dir)
    st.max_events = 0                     # events come from the cursor, not the 48 h window
    bt = sx.BacktestCache(st.backtest_dir)
    bridge = PushBridge(settings, lambda: sx.build_status(cfg, agents, settings=st, state_fn=state_fn, ema921e_fn=ema921e_fn,
                                                          backtests=bt, started=started),
                        cfg=cfg, agents=agents, notify=notify, log=log, opener=opener, trades=trades)
    bridge.start()
    log(f"aureon push: POST every {settings.interval_s} s to {settings.url}" + (" (gzip)" if settings.gzip else ""))
    return bridge
