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
    if mode not in ("ema2050", "ema5080"): raise ValueError("unsupported mode")
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

def restart_process(agents, journal, notify, user, reason="", mode=None, symbols=None, argv=None, executor=os.execv):
    """Only after permission/position gates passed by the invoking command."""
    reason = (reason or "requested").strip()[:120]
    notify.card("AUREON · MT5 · RESTARTING", f"Requested by {user} · {reason}")
    journal.log("manual_restart", by=str(user), reason=reason, mode=mode)
    for ag in list(agents.values()):
        ag.stop()
    for ag in list(agents.values()):
        ag.join(timeout=5)
    os.environ["AUREON_RESTART_REASON"] = f"manual:{reason}"
    args = list(argv if argv is not None else sys.argv)
    # The process CLI overrides .env, so eliminate old --mode/--symbols flags.
    if mode:
        out = []; i = 0
        while i < len(args):
            item = args[i]
            if item in ("--mode", "--symbols"):
                i += 2; continue
            if item.startswith("--mode=") or item.startswith("--symbols="):
                i += 1; continue
            out.append(item); i += 1
        args = out + ["--mode", mode]
        if symbols is not None: args += ["--symbols", ",".join(symbols)]
    executor(sys.executable, [sys.executable] + args)
