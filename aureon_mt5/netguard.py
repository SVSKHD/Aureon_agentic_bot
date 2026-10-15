"""v2.11.2 — a network loss never ends the process.

Every network failure (DNS, webhook 4xx/5xx/timeout, gateway loss, discord.py giving up, push backends, the console,
the dead-man ping) is caught at its own boundary, logged at most once a minute (rlog) and turned into STATE here.
Nothing in this module raises.

    degraded()          gateway down > 60 s  OR  2 webhook failures in a row  OR  discord.com does not resolve
                        → the poll loop and the guardian run unchanged on the existing stops; no new entries (asks are
                        journaled ask_suppressed_network unless the registry flag network_degraded_entries is on);
                        trade cards go to logs/card_queue.jsonl and are flushed (summary first) on reconnect.
    supervise_client()  the Discord client's outer loop: any exit of client.start() → back off 2 s → 60 s and start
                        again; only ops.STOP (/restart, /mode, maintenance, repair-apply, Ctrl-C) ends it.
    record_exit()       process_exit {reason, exc_type, where, uptime_s, positions_open} → journal + restart_marker.json
                        (when a restart was requested) or exit_marker.json.
    dns_check()         discord.com, the ntfy host, the MT5 host (AUREON_MT5_HOST) — on the heartbeat and on every flip."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time as _time
import traceback
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

GATEWAY_BACKOFF = (2.0, 60.0)
GATEWAY_DEGRADED_S = 60.0
WEBHOOK_FAILS_DEGRADED = 2
LOG_EVERY_S = 60.0
QUEUE_CAP = 500

_lock = threading.Lock()
_last_log: dict[str, float] = {}
STATE: dict = {"degraded": False, "since": None, "webhook_fail_streak": 0, "webhook_down_since": None,
               "dns_fail": False, "dns": {}, "dns_t": None, "suppressed": [], "reasons": []}
MONITOR = None                    # the bot's LoopMonitor (gateway_down_for); None without a bot
EXIT_CTX: dict = {"started": _time.time(), "journal": None, "log_dir": "logs", "positions": None, "recorded": False}


def configure(*, journal=None, log_dir: str | None = None, positions=None, started: float | None = None):
    """main(): where flips and process_exit are journaled, and how to count open positions at exit."""
    if journal is not None:
        EXIT_CTX["journal"] = journal
    if log_dir:
        EXIT_CTX["log_dir"] = log_dir
    if positions is not None:
        EXIT_CTX["positions"] = positions
    if started is not None:
        EXIT_CTX["started"] = started


def reset():
    """Tests: back to a healthy network."""
    global MONITOR
    with _lock:
        _last_log.clear()
        STATE.update(degraded=False, since=None, webhook_fail_streak=0, webhook_down_since=None, dns_fail=False, dns={},
                     dns_t=None, suppressed=[], reasons=[], client_starts=0, notified=False)
    MONITOR = None
    EXIT_CTX.update(recorded=False, journal=None, log_dir="logs", positions=None, started=_time.time())
    try:
        from . import server
        server.STATE["network_degraded"] = False
    except Exception:
        pass


# ----------------------------------------------------------------------------- once-a-minute logging
def rlog(key: str, msg: str, every: float = LOG_EVERY_S, clock=_time.monotonic) -> bool:
    """Print + aureon.log at most once per `every` s per key. True when it logged."""
    now = clock()
    with _lock:
        last = _last_log.get(key)
        if last is not None and now - last < every:
            return False
        _last_log[key] = now
    print(f"  ({msg})", flush=True)
    try:
        from . import telemetry
        telemetry.info(msg)
    except Exception:
        pass
    return True


# ----------------------------------------------------------------------------- state
def degraded() -> bool:
    return bool(STATE["degraded"])


def webhook_down() -> bool:
    return STATE["webhook_fail_streak"] >= WEBHOOK_FAILS_DEGRADED


def webhook_result(ok: bool, now: float | None = None):
    now = _time.time() if now is None else now
    with _lock:
        if ok:
            STATE["webhook_fail_streak"] = 0
            STATE["webhook_down_since"] = None
            STATE["dns_fail"] = False                      # a delivered POST proves discord.com resolves
        else:
            STATE["webhook_fail_streak"] += 1
            if STATE["webhook_down_since"] is None:
                STATE["webhook_down_since"] = now
    update(now=now)


def gateway_down_s() -> float:
    m = MONITOR
    if m is None:
        return 0.0
    try:
        return float(m.gateway_down_for())
    except Exception:
        return 0.0


def reasons() -> list[str]:
    out = []
    g = gateway_down_s()
    if g > GATEWAY_DEGRADED_S:
        out.append(f"gateway down {g:.0f} s")
    if webhook_down():
        out.append(f"webhook failed {STATE['webhook_fail_streak']}× in a row")
    if STATE["dns_fail"]:
        out.append("discord.com does not resolve")
    return out


def update(now: float | None = None, journal=None, on_flip=None) -> bool | None:
    """Recompute degraded. Returns the new value when it flipped, else None. Mirrors into server.STATE (entry gate)."""
    now = _time.time() if now is None else now
    why = reasons()
    new = bool(why)
    with _lock:
        flipped = new != STATE["degraded"]
        STATE["reasons"] = why
        if flipped:
            down_for = now - STATE["since"] if (STATE["since"] and not new) else None
            STATE["degraded"] = new
            STATE["since"] = now if new else None
    try:
        from . import server
        server.STATE["network_degraded"] = new
    except Exception:
        pass
    if on_flip is not None and STATE.get("notified", False) != new:    # the card / DNS check follow the state, whichever
        STATE["notified"] = new                                         # thread saw the flip first
        try:
            on_flip(new)
        except Exception as e:
            rlog("netflip-cb", f"network flip handler failed: {e!r}")
    if not flipped:
        return None
    try:
        j = journal or EXIT_CTX.get("journal")
        if j is not None:
            j.log("network_degraded" if new else "network_restored", reasons=why,
                  **({"down_s": round(down_for)} if down_for is not None else {}))
    except Exception:
        pass
    rlog("netflip", f"network {'DEGRADED: ' + '; '.join(why) if new else 'restored'}", every=0)
    return new


def suppress_ask(journal, symbol: str, key: str, title: str = ""):
    """An entry ask fired while degraded: journaled, kept for the console / pad, never auto-taken."""
    rec = {"t": _time.time(), "symbol": symbol, "key": key, "title": title, "reasons": list(STATE["reasons"])}
    with _lock:
        STATE["suppressed"] = (STATE["suppressed"] + [rec])[-20:]
    try:
        journal.log("ask_suppressed_network", symbol=symbol, key=key, title=title, reasons=rec["reasons"])
    except Exception:
        pass
    return rec


# ----------------------------------------------------------------------------- DNS self-check
def dns_hosts(cfg) -> list[tuple[str, str]]:
    """[(label, host)]: discord.com, the ntfy host (configured or api.ntfy.sh), the MT5 server host when known."""
    ntfy = urlparse(str(getattr(cfg, "ntfy_url", "") or "https://ntfy.sh")).hostname or "ntfy.sh"
    if ntfy == "ntfy.sh":
        ntfy = "api.ntfy.sh"
    out = [("discord", "discord.com"), ("ntfy", ntfy)]
    mt5 = str(getattr(cfg, "mt5_host", "") or "").strip()
    out.append(("mt5", mt5 or ""))
    return out


def dns_check(hosts, resolver=None, timeout: float = 5.0) -> dict:
    """{label: {"host", "ok", "error"}}. Each lookup bounded by `timeout` (a hung resolver cannot stall the caller long)."""
    resolve = resolver or socket.getaddrinfo
    out = {}
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="aureon-dns") as ex:
        futs = {label: (host, ex.submit(resolve, host, 443)) for label, host in hosts if host}
        for label, host in hosts:
            if not host:
                out[label] = {"host": "", "ok": None, "error": "not configured (AUREON_MT5_HOST)" if label == "mt5" else "not configured"}
                continue
            try:
                futs[label][1].result(timeout=timeout)
                out[label] = {"host": host, "ok": True, "error": ""}
            except Exception as e:
                out[label] = {"host": host, "ok": False, "error": (str(e) or type(e).__name__)[:80]}
    with _lock:
        STATE["dns"] = out
        STATE["dns_t"] = _time.time()
        if "discord" in out and out["discord"]["ok"] is not None:
            STATE["dns_fail"] = not out["discord"]["ok"]
    return out


def dns_line(res: dict | None = None) -> str:
    res = STATE["dns"] if res is None else res
    if not res:
        return "DNS: not checked yet"
    parts = []
    for label, r in res.items():
        if r["ok"] is None:
            parts.append(f"{label} —")
        elif r["ok"]:
            parts.append(f"{r['host']} ✅")
        else:
            parts.append(f"{r['host']} ❌ {r['error']}")
    return "DNS: " + " · ".join(parts)


# ----------------------------------------------------------------------------- the Discord client supervisor
async def supervise_client(start, *, stop_requested, close=None, on_exit=None, sleep=None, clock=_time.monotonic,
                           backoff=None) -> int:
    """Run `await start()` forever. Any exit — exception or clean return — → on_exit(why), close(), back off
    (2 s → 60 s, reset after 2 min of running) and start again. Returns (the number of starts) only when stop_requested()."""
    lo, hi = backoff or GATEWAY_BACKOFF
    delay, runs = lo, 0
    sleep = sleep or asyncio.sleep
    while not stop_requested():
        t0 = clock(); runs += 1
        STATE["client_starts"] = STATE.get("client_starts", 0) + 1
        try:
            await start()
            why = "client returned"
        except asyncio.CancelledError:
            why = "cancelled"
        except Exception as e:                                 # DNS, ClientConnectorError, ConnectionClosed, LoginFailure …
            why = f"{type(e).__name__}: {e}"[:200]
        if close is not None:
            try:
                await close()
            except Exception:
                pass
        if on_exit is not None:
            try:
                on_exit(why)
            except Exception:
                pass
        if stop_requested():
            break
        if clock() - t0 > 120:
            delay = lo
        rlog("gateway-supervisor", f"discord client exited ({why}) — reconnecting in {delay:.0f} s (forever)")
        try:
            await sleep(delay)
        except asyncio.CancelledError:
            pass
        delay = min(hi, delay * 2)
    return runs


# ----------------------------------------------------------------------------- the card queue (logs/card_queue.jsonl)
class CardQueue:
    def __init__(self, path: str, cap: int = QUEUE_CAP):
        self.path, self.cap = path, cap
        self._lock = threading.Lock()

    def append(self, item: dict) -> int:
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(item, default=str) + "\n")
                items = self._read()
                if len(items) > self.cap:
                    self._write(items[-self.cap:])
                    return self.cap
                return len(items)
            except OSError as e:
                rlog("cardqueue", f"card queue write failed: {e!r}")
                return 0

    def _read(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return [json.loads(x) for x in f if x.strip()]
        except (OSError, ValueError):
            return []

    def _write(self, items):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, default=str) + "\n")
        os.replace(tmp, self.path)

    def items(self) -> list[dict]:
        with self._lock:
            return self._read()

    def replace(self, items: list[dict]):
        with self._lock:
            try:
                if items:
                    self._write(items)
                elif os.path.exists(self.path):
                    os.remove(self.path)
            except OSError as e:
                rlog("cardqueue", f"card queue write failed: {e!r}")

    def __len__(self):
        return len(self.items())


# ----------------------------------------------------------------------------- exit_reason
def exit_marker_path(log_dir: str) -> str:
    return os.path.join(log_dir, "exit_marker.json")


def where_from(exc: BaseException | None) -> str:
    if exc is None or exc.__traceback__ is None:
        return ""
    fr = traceback.extract_tb(exc.__traceback__)[-1]
    return f"{os.path.basename(fr.filename)}:{fr.lineno} {fr.name}"


def record_exit(reason: str, *, exc: BaseException | None = None, where: str = "", planned: bool = False,
                code: int | None = None) -> dict:
    """Journal process_exit and write it into restart_marker.json (a restart was requested) or exit_marker.json. Once
    per process; never raises."""
    if EXIT_CTX.get("recorded"):
        return {}
    EXIT_CTX["recorded"] = True
    pos = None
    try:
        pos = EXIT_CTX["positions"]() if EXIT_CTX.get("positions") else None
    except Exception:
        pos = None
    rec = {"reason": str(reason)[:300], "exc_type": type(exc).__name__ if exc is not None else None,
           "where": where or where_from(exc), "uptime_s": round(_time.time() - EXIT_CTX.get("started", _time.time())),
           "positions_open": pos, "planned": bool(planned), "code": code, "t": _time.time()}
    try:
        j = EXIT_CTX.get("journal")
        if j is not None:
            j.log("process_exit", **{k: v for k, v in rec.items() if k != "t"})
    except Exception:
        pass
    log_dir = EXIT_CTX.get("log_dir") or "logs"
    try:
        from .ops import restart_marker_path
        rp = restart_marker_path(log_dir)
        if os.path.exists(rp):
            with open(rp, encoding="utf-8") as f:
                m = json.load(f)
            m["exit"] = rec
            with open(rp, "w", encoding="utf-8") as f:
                json.dump(m, f)
        else:
            os.makedirs(log_dir, exist_ok=True)
            with open(exit_marker_path(log_dir), "w", encoding="utf-8") as f:
                json.dump(rec, f)
    except Exception:
        pass
    return rec


def pop_exit_marker(log_dir: str) -> dict | None:
    p = exit_marker_path(log_dir)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        os.remove(p)
    except OSError:
        pass
    return d


def last_exit_text(rec: dict | None) -> str:
    if not rec:
        return ""
    bits = [rec.get("reason") or "unknown"]
    if rec.get("exc_type"):
        bits.append(rec["exc_type"])
    if rec.get("where"):
        bits.append(f"at {rec['where']}")
    return "last exit: " + " · ".join(bits)
