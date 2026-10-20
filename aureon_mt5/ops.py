"""Safe admin restart and mode switching; no strategy-rule changes."""
from __future__ import annotations
import os
import sys
import time
import threading
from pathlib import Path
from dotenv import set_key

def admin_allowed(user, role_id):
    if not str(role_id).isdigit(): return False
    return any(str(getattr(r, "id", "")) == str(role_id) for r in getattr(user, "roles", ()))

def positions_block_mode_change(agents):
    """Fail closed: an unreadable/absent snapshot cannot prove flat exposure."""
    for symbol, agent in agents.items():
        snap = getattr(agent, "snapshot", None) or {}
        if not snap.get("at") or abs(time.time() - float(snap["at"])) > 30: return True
        if snap.get("positions"): return True
        if not snap.get("connected", False): return True
    return False

def write_mode(env_path, mode, symbols=None):
    if mode not in ("ema2050", "ema5080", "ema9200"): raise ValueError("unsupported mode")
    path = Path(env_path).resolve()
    if not path.is_file(): raise FileNotFoundError("existing .env file required")
    set_key(str(path), "AUREON_MODE", mode, quote_mode="never")
    if symbols is not None:
        names = [x.strip().upper() for x in symbols.split(",") if x.strip()]
        if not names or any(not x.replace("_", "").isalnum() for x in names):
            raise ValueError("invalid symbols")
        set_key(str(path), "AUREON_SYMBOLS", ",".join(names), quote_mode="never")
        return names
    return None

EXIT_RESTART = 75     # v2.7.2: "restart me" — the supervisor (scripts/aureon_service.ps1 / NSSM) brings the process back


def restart_marker_path(log_dir):
    return os.path.join(log_dir, "restart_marker.json")


def write_restart_marker(log_dir, kind, reason=""):
    """v2.7.2: the BACK ONLINE reason survives the process (env vars do not, now that a supervisor restarts us)."""
    import json
    os.makedirs(log_dir, exist_ok=True)
    with open(restart_marker_path(log_dir), "w", encoding="utf-8") as f:
        json.dump({"kind": kind, "reason": str(reason)[:200], "t": time.time()}, f)


def pop_restart_marker(log_dir, max_age=3600):
    """Read and delete the marker. None when absent, unreadable or older than `max_age` s."""
    import json
    path = restart_marker_path(log_dir)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        os.remove(path)
    except OSError:
        pass
    if time.time() - float(data.get("t", 0)) > max_age:
        return None
    return data


STOP = threading.Event()   # v2.11.2: a planned stop (/restart, /mode, maintenance, repair-apply, Ctrl-C) — the only thing
                           # that ends the Discord supervisor; everything network-related reconnects forever


def stop_requested() -> bool:
    return STOP.is_set()


def hard_exit(code=EXIT_RESTART, agents=None, join_timeout=5.0, _exit=None, reason: str | None = None):
    """Stop agents (best effort, bounded), flush logs and leave with `code`. os._exit because this runs on a Timer /
    health thread where sys.exit would only end that thread. The supervisor restarts the process.
    v2.11.2: sets STOP and records process_exit (reason from the restart marker) before leaving."""
    STOP.set()
    try:
        from . import netguard
        if reason is None:
            import json
            try:
                with open(restart_marker_path(netguard.EXIT_CTX.get("log_dir") or "logs"), encoding="utf-8") as f:
                    m = json.load(f)
                reason = f"restart · {m.get('kind')} · {m.get('reason', '')}".strip(" ·")
            except (OSError, ValueError):
                reason = f"exit {code}"
        netguard.record_exit(reason, planned=True, code=code, where="ops.hard_exit")
    except Exception:
        pass
    for ag in list((agents or {}).values()):
        try:
            ag.stop()
        except Exception:
            pass
    for ag in list((agents or {}).values()):
        try:
            ag.join(timeout=join_timeout)
        except Exception:
            pass
    try:
        from . import broker
        broker.close_connection()
    except Exception:
        pass
    try:
        import logging
        logging.shutdown()
        sys.stdout.flush(); sys.stderr.flush()
    except Exception:
        pass
    (_exit or os._exit)(code)


def cli_pins_mode(argv, symbols=None):
    """True when the process command line carries --mode (or --symbols when symbols change): the supervisor would restart
    with the same flags, which override .env, so a /mode change would silently not apply."""
    flags = ("--mode", "--symbols") if symbols is not None else ("--mode",)
    return any(a in flags or any(a.startswith(f + "=") for f in flags) for a in argv)


def restart_process(agents, journal, notify, user, reason="", mode=None, symbols=None, argv=None, exit_hook=None,
                    log_dir="logs", update=None, marker_kind="manual"):
    """Only after permission/position gates passed by the invoking command.
    Brings the checkout to origin/master first (same rules as auto-update); a failed update aborts the restart
    before any agent is stopped, so the current build keeps running.
    v2.7.2: no re-exec — stops agents, writes the restart marker and exits with code 75; the supervisor restarts the
    process (same command line; mode/symbols come from .env)."""
    reason = (reason or "requested").strip()[:120]
    args = list(argv if argv is not None else sys.argv)
    if mode and cli_pins_mode(args, symbols):
        notify.card("AUREON · MT5 · RESTART ABORTED",
                    "The process was started with --mode/--symbols; the supervisor would restart with the old flags.\n"
                    f".env already says {mode}. Remove the flags from the service command (docs/SERVICE.md) and /restart.")
        journal.log("manual_restart_aborted", by=str(user), reason=reason, error="cli flags pin the mode")
        return False
    from .common.updater import discarded_title, update_to_origin
    if update is None:
        update = lambda: update_to_origin(log_dir)
    res = update()
    if res["status"] == "failed":
        notify.card("AUREON · MT5 · RESTART ABORTED", f"Update failed: {res['error'][:200]}\nAgents still running on the current build."
                    + (f"\n.env already says {mode}; it takes effect on the next restart." if mode else ""))
        journal.log("manual_restart_aborted", by=str(user), reason=reason, error=res["error"][:200])
        return False
    if res["discarded"]:
        notify.card(discarded_title(res), f"Checkout reset to origin/master. Restore: `git apply {res['patch']}`")
    notify.card("AUREON · MT5 · RESTARTING", f"Requested by {user} · {reason}")
    journal.log("manual_restart", by=str(user), reason=reason, mode=mode, exit_code=EXIT_RESTART)
    for ag in list(agents.values()):
        ag.stop()
    for ag in list(agents.values()):
        ag.join(timeout=5)
    write_restart_marker(log_dir, marker_kind, reason)
    (exit_hook or hard_exit)(EXIT_RESTART)
    return True
