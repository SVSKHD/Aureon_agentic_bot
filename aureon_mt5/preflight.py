"""v2.15.0 restart pre-flight: a Windows reboot or a supervisor restart recovers with no human.

Runs in main.py before any agent exists (and before the health thread writes logs/heartbeat.txt):

    1. terminal   terminal64.exe running? If not, start it from AUREON_MT5_PATH (else the path cached in
                  logs/mt5_path.txt, the launcher's rule); neither → a warning and the step is skipped.
    2. connect    MetaTrader5.initialize() AND account_info() — every 5 s for up to 90 s. initialize() alone can
                  succeed against a terminal that is not logged in yet. logs/heartbeat.txt is touched on every attempt,
                  so the supervisor's hang check (5 min) never kills a process that is only waiting for MT5.
                  Still down after 90 s → exit 75; the launcher starts the terminal and us again. The first failure
                  time is kept in logs/preflight.json across those restarts: MT5 NOT UP is carded ONCE when the outage
                  passes AUREON_MT5_BOOT_WAIT (300 s), and BACK ONLINE says how long it took.
    3. market     closed → nothing exits: the agents start and sleep until the next open (they keep the position
                  snapshot fresh, which the /mode and restart gates read); the card says so.
    4. card       BACK ONLINE after restart · MT5 build · account · open positions re-adopted.

Every step goes to logs/aureon.log (`preflight: …`)."""
from __future__ import annotations

import json
import os
import time as _time

from . import telemetry

WAIT_S = 90.0
EVERY_S = 5.0
STATE = "preflight.json"


def log(msg: str) -> None:
    telemetry.info(f"preflight: {msg}")
    print(f"preflight: {msg}")


def _ps(cmd: str, timeout: float = 5.0):
    from .server import run_ps
    return run_ps(cmd, timeout)


def terminal_exe(cfg) -> str | None:
    if getattr(cfg, "mt5_path", ""):
        return cfg.mt5_path
    from .server import mt5_path_file
    try:
        return open(mt5_path_file(cfg.log_dir), encoding="utf-8").read().strip() or None
    except OSError:
        return None


def ensure_terminal(cfg, *, run=_ps, windows: bool | None = None, exists=os.path.exists) -> str:
    """'running' | 'started <exe>' | 'skipped: …'. Started through Start-Process (not as our child), so a supervisor
    `taskkill /T` of the bot never takes the terminal down with it."""
    if not (os.name == "nt" if windows is None else windows):
        return "skipped: not Windows"
    ok, out = run("(Get-Process terminal64 -ErrorAction SilentlyContinue | Measure-Object).Count", 5)
    if ok and out.strip().isdigit() and int(out.strip()) > 0:
        return "running"
    exe = terminal_exe(cfg)
    if not exe or not exists(exe):
        return "skipped: terminal64.exe not running and AUREON_MT5_PATH is not set (or does not exist)"
    exe_q = exe.replace("'", "''")
    run(f"Start-Process -FilePath '{exe_q}' -WorkingDirectory '{os.path.dirname(exe).replace(chr(39), chr(39) * 2)}'", 15)
    return f"started {exe}"


def touch_heartbeat(log_dir: str) -> None:
    """mtime only (the content stays the last real heartbeat line, which the cold-start card reports)."""
    path = os.path.join(log_dir, "heartbeat.txt")
    try:
        os.makedirs(log_dir, exist_ok=True)
        if os.path.exists(path):
            os.utime(path, None)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write("preflight: waiting for MT5\n")
    except OSError:
        pass


def _load(log_dir: str) -> dict:
    try:
        with open(os.path.join(log_dir, STATE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(log_dir: str, st: dict) -> None:
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, STATE), "w", encoding="utf-8") as f:
            json.dump(st, f)
    except OSError:
        pass


def wait_mt5(broker, *, log_dir: str, wait_s: float = WAIT_S, every_s: float = EVERY_S, sleep=_time.sleep,
             clock=_time.monotonic) -> tuple[dict | None, float, str]:
    """(account, seconds waited, last failure). account None = MT5 never answered within wait_s."""
    t0 = clock(); why = ""; n = 0
    while True:
        n += 1
        touch_heartbeat(log_dir)
        broker.reset()
        if broker.connect():
            acct = None
            try:
                acct = broker.account()
            except Exception as e:
                why = f"account_info() raised {e!r}"
            if acct:
                return acct, clock() - t0, ""
            why = why or "initialize() ok but account_info() is None (terminal not logged in yet)"
        else:
            why = "MetaTrader5.initialize() failed"
        waited = clock() - t0
        log(f"attempt {n}: {why} · {waited:.0f}/{wait_s:.0f} s")
        if waited >= wait_s:
            return None, waited, why
        sleep(min(every_s, max(0.0, wait_s - waited)))


def run(cfg, broker, notify=None, *, run_ps=_ps, windows: bool | None = None, sleep=_time.sleep, clock=_time.monotonic,
        wall=_time.time, exists=os.path.exists) -> dict:
    """Steps 1–2. {"ok", "terminal", "account", "waited", "why", "down_s", "build"}."""
    log("start")
    term = ensure_terminal(cfg, run=run_ps, windows=windows, exists=exists)
    log(f"terminal: {term}")
    if term.startswith("skipped") and "not Windows" not in term:
        log("WARNING: cannot start the terminal — set AUREON_MT5_PATH to terminal64.exe")
    acct, waited, why = wait_mt5(broker, log_dir=cfg.log_dir, sleep=sleep, clock=clock)
    st = _load(cfg.log_dir); now = wall()
    if acct is None:
        st.setdefault("first_fail", now)
        st["attempts"] = int(st.get("attempts", 0)) + 1
        out_s = now - float(st["first_fail"])
        if out_s >= float(getattr(cfg, "mt5_boot_wait", 300)) and not st.get("carded") and notify is not None:
            st["carded"] = True
            try:
                notify.card("AUREON · MT5 · MT5 NOT UP",
                            f"MT5 has not answered for {int(out_s)} s across {st['attempts']} start(s): {why}.\n"
                            f"terminal: {term}. The launcher keeps restarting the terminal and the bot every ~{int(WAIT_S)} s; "
                            "BACK ONLINE follows when it answers.", footer="Aureon MT5 · pre-flight")
            except Exception:
                pass
        _save(cfg.log_dir, st)
        log(f"MT5 not up after {waited:.0f} s ({why}) — exit 75, the launcher restarts terminal + bot")
        return {"ok": False, "terminal": term, "account": None, "waited": waited, "why": why, "down_s": out_s, "build": None}
    down_s = (now - float(st["first_fail"])) if st.get("first_fail") else None
    _save(cfg.log_dir, {})
    build = None
    try:
        v = broker.mt5().version()
        build = f"{v[0]} build {v[1]}" if v else None
    except Exception:
        pass
    log(f"MT5 up after {waited:.0f} s · {build or 'build ?'} · account {acct.get('login_masked')} ({acct.get('trade_mode')})")
    return {"ok": True, "terminal": term, "account": acct, "waited": waited, "why": "", "down_s": down_s, "build": build}


def back_online(notify, res: dict, positions: dict, *, closed: bool = False) -> str:
    """The one BACK ONLINE after restart card (step 4). positions: {symbol: [live positions]}."""
    a = res.get("account") or {}
    held = [f"#{p['ticket']} {sym} {str(p.get('direction', '')).upper()}" for sym, ps in (positions or {}).items() for p in ps or []]
    lines = [f"MT5 {res.get('build') or 'build ?'} · account {a.get('login_masked', '?')} ({a.get('trade_mode', '?')})",
             f"open positions re-adopted: {len(held)}" + (f" — {', '.join(held)}" if held else ""),
             f"terminal {res.get('terminal')} · MT5 answered after {res.get('waited', 0):.0f} s"
             + (f" · outage {int(res['down_s'] // 60)} min across restarts" if res.get("down_s") else "")]
    if closed:
        lines.append("market closed — agents sleeping until the next open")
    text = "\n".join(lines)
    log("BACK ONLINE · " + text.replace("\n", " · "))
    if notify is not None:
        try:
            notify.card("AUREON · MT5 · BACK ONLINE after restart", text, footer="Aureon MT5 · pre-flight")
        except Exception:
            pass
    return text
