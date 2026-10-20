"""v2.10.1 — the box belongs to Aureon: OS-level operations, every call through ONE seam (`run_ps`) that tests fake.

Everything here is PowerShell / subprocess with a timeout, journaled and announced; any failure degrades to "unknown" or a
card, never a crash. Nothing in this module places, modifies or closes a trade; reboots and restarts happen only when the
account is flat (ops.positions_block_mode_change) and outside the session unless a documented force rule applies.

  cold_start()          COLD START card on a boot without a restart marker (boot time, last shutdown reason, down time,
                        positions re-adopted across the gap)
  boot_info()           Windows boot time + last System-log shutdown events (1074 reason, 6006 clean, 6008 unexpected)
  Maintenance           hourly: pending Windows reboot / git update / log rotation / pip / MT5 restart — only flat and outside
                        the session (or force_after_h pending, still flat). 60 s reboot countdown, aborted if a position opens
  Mt5Manager            terminal64 hang → restart, re-verify login + symbols (cards on every action)
  clock_drift()         w32tm against time.windows.com; > 2 s → resync + card
  NetworkWatch          Discord AND dead-man failing while MT5 ticks → network_degraded (no new entries; guardian untouched)
  server_card()         /server
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time as _time
from datetime import datetime, timedelta, timezone

from . import telemetry

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIFT_LIMIT_S = 2.0
STATE = {"network_degraded": False, "drift": None, "updates_pending": None, "boot": None, "mt5_age": None,
         "disk_free_gb": None, "last_check": {}}


def run_ps(cmd: str, timeout: float = 5.0) -> tuple[bool, str]:
    """THE OS seam: one PowerShell command with a timeout → (ok, stdout). Never raises; not Windows → (False, ...)."""
    if os.name != "nt":
        return False, "not windows"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode == 0, (r.stdout or r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout:g}s"
    except Exception as e:
        return False, f"{e!r}"


def network_degraded() -> bool:
    return bool(STATE["network_degraded"])


def ist_hhmm(dt: datetime | None) -> str:
    return dt.astimezone(IST).strftime("%H:%M IST") if dt else "unknown"


def _parse_dt(s: str) -> datetime | None:
    s = (s or "").strip().strip('"')
    m = re.search(r"/Date\((\d+)\)/", s)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc)
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ============================================================================= E1 cold start
SHUTDOWN_IDS = {1074: "planned", 6006: "clean shutdown", 6008: "unexpected shutdown"}


def boot_info(run=run_ps) -> dict:
    out = {"boot_time": None, "shutdown": None}
    ok, txt = run("(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')", 5)
    if ok:
        out["boot_time"] = _parse_dt(txt.splitlines()[0] if txt else "")
    ok, txt = run("Get-WinEvent -FilterHashtable @{LogName='System'; Id=1074,6006,6008} -MaxEvents 3 | "
                  "Select-Object Id,TimeCreated,Message | ConvertTo-Json -Compress", 5)
    if ok and txt:
        try:
            ev = json.loads(txt)
            ev = ev if isinstance(ev, list) else [ev]
            ev = sorted(ev, key=lambda e: _parse_dt(str(e.get("TimeCreated"))) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            e = ev[0]
            eid = int(e.get("Id")); msg = str(e.get("Message") or "")
            reason = SHUTDOWN_IDS.get(eid, str(eid))
            if eid == 1074:
                m = re.search(r"(?:reason|Reason Code)[^:]*:\s*([^\r\n]+)", msg) or re.search(r"for the following reason:\s*([^\r\n]+)", msg)
                if "Windows Update" in msg or "update" in msg.lower():
                    reason = "Windows Update"
                elif m:
                    reason = m.group(1).strip()[:80]
            out["shutdown"] = {"id": eid, "time": _parse_dt(str(e.get("TimeCreated"))), "reason": reason}
        except Exception:
            pass
    return out


def last_heartbeat(log_dir: str) -> tuple[str | None, float | None]:
    path = os.path.join(log_dir, "heartbeat.txt")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None, os.path.getmtime(path)
    except OSError:
        return None, None


def cold_start(cfg, journal, notify, positions_by_symbol: dict, *, run=run_ps, now: float | None = None,
               last_exit: dict | None = None, heartbeat: tuple | None = None) -> dict:
    """Card + journal on a boot with NO restart marker. positions_by_symbol: {sym: [live positions]} (after MT5 connect)."""
    now = _time.time() if now is None else now
    info = boot_info(run)
    hb_line, hb_t = heartbeat if heartbeat is not None else last_heartbeat(cfg.log_dir)   # v2.15.0: read before the pre-flight
    down_s = int(now - hb_t) if hb_t else None
    sd = info["shutdown"]
    reason = f"{sd['reason']} ({sd['id']})" if sd else "unknown"
    adopted = []
    for sym, pos in (positions_by_symbol or {}).items():
        try:
            state = journal.load_state(sym)
        except Exception:
            state = {}
        for p in pos or []:
            st = state.get(p["ticket"], state.get(str(p["ticket"]), {}))
            adopted.append({"symbol": sym, "ticket": p["ticket"], "side": p["direction"], "sl": p.get("sl") or None,
                            "secured": st.get("secured"), "state": "re-adopted" if st else "new (no saved state)"})
    head = (f"rebooted {ist_hhmm(info['boot_time'])} · down {down_s // 60 if down_s is not None else '?'} min · reason: {reason}")
    lines = [head, f"last heartbeat: {hb_line or 'none'}"]
    if last_exit:                                            # v2.11.2: logs/exit_marker.json from the previous process
        from .netguard import last_exit_text
        lines.append(last_exit_text(last_exit))
    if adopted:
        lines.append("positions across the gap: " + "; ".join(
            f"#{a['ticket']} {a['side']} SL {a['sl'] or '—'} secured {a['secured'] if a['secured'] is not None else '—'} ({a['state']})"
            for a in adopted))
    else:
        lines.append("no open positions across the gap")
    rec = {"boot_time": info["boot_time"].isoformat() if info["boot_time"] else None, "down_s": down_s,
           "reason": reason, "last_heartbeat": hb_line, "positions": adopted,
           "last_exit": (last_exit or {}).get("reason")}
    try:
        journal.log("cold_start", **rec)
    except Exception:
        pass
    try:
        notify.card("AUREON · MT5 · COLD START", "\n".join(lines), footer="Aureon MT5 · unattended")
    except Exception:
        pass
    STATE["boot"] = rec
    return {"lines": lines, **rec}


def down_text(marker: dict | None, now: float | None = None) -> str:
    """'down 42 s' for the BACK ONLINE card (marker written just before the exit)."""
    now = _time.time() if now is None else now
    if not marker or not marker.get("t"):
        return "down ? s"
    out = f"down {int(max(0, now - float(marker['t'])))} s"
    if marker.get("exit"):                                   # v2.11.2: why the previous process ended
        from .netguard import last_exit_text
        out += " · " + last_exit_text(marker["exit"])
    return out


# ============================================================================= E2 MT5 before the bot
def mt5_path_file(log_dir: str) -> str:
    return os.path.join(log_dir, "mt5_path.txt")


def remember_mt5_path(log_dir: str, terminal_info) -> str | None:
    """Cache the terminal64.exe path MT5 reports (aureon_service.ps1 starts it before the bot next time)."""
    try:
        path = getattr(terminal_info, "path", None)
        if not path:
            return None
        exe = os.path.join(path, "terminal64.exe")
        os.makedirs(log_dir, exist_ok=True)
        with open(mt5_path_file(log_dir), "w", encoding="utf-8") as f:
            f.write(exe)
        return exe
    except Exception:
        return None


def connect_mt5(connect, *, boot_wait: float, notify=None, sleep=_time.sleep, clock=_time.monotonic, reset=None) -> bool:
    """broker.connect() with 5 s → 30 s backoff for up to boot_wait seconds (MT5 may still be starting after a reboot).
    After 5 min of failure posts one MT5 NOT UP card (webhook — no gateway needed). False when it never came up."""
    t0 = clock(); delay = 5.0; carded = False
    while True:
        if reset is not None:
            reset()
        if connect():
            return True
        waited = clock() - t0
        if waited >= 300 and not carded and notify is not None:
            carded = True
            try:
                notify.card("AUREON · MT5 · MT5 NOT UP", f"MetaTrader5.initialize() failing for {int(waited)} s — is terminal64 "
                            "running and logged in as the service user? The launcher restarts the bot (exit 75).",
                            footer="Aureon MT5 · unattended")
            except Exception:
                pass
        if waited >= boot_wait:
            return False
        sleep(min(delay, max(0.0, boot_wait - waited)))
        delay = min(30.0, delay * 2 if delay < 30 else 30.0)


# ============================================================================= F1 maintenance window
REBOOT_KEYS = (r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
               r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending")
CHECK_EVERY_S = 3600
COUNTDOWN_S = 60


def _ist(now: float) -> datetime:
    return datetime.fromtimestamp(now, tz=IST)


def in_session(cfg, now: float) -> bool:
    from .repair import in_session as _in
    return _in(cfg, _ist(now))


def pending_reboot(run=run_ps) -> tuple[bool | None, int | None]:
    """(reboot pending?, updates waiting) — None when unknown."""
    ok, out = run("(" + " -or ".join(f"(Test-Path '{k}')" for k in REBOOT_KEYS) + ")", 10)
    pending = (out.strip().lower() == "true") if ok else None
    ok2, out2 = run("(New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher().Search('IsInstalled=0').Updates.Count", 60)
    try:
        n = int(out2.strip().splitlines()[-1]) if ok2 else None
    except (ValueError, IndexError):
        n = None
    return pending, n


class Maintenance:
    """Hourly. Reboots / updates / housekeeping only when FLAT and OUTSIDE the session; a reboot pending longer than
    maint_force_after_h may happen inside the session, still only when flat. Never with a position open."""

    def __init__(self, cfg, journal, notify, agents, *, run=run_ps, clock=_time.time, positions_block=None, sh=subprocess.run,
                 exit_hook=None, root: str = ROOT, python: str = sys.executable):
        self.cfg, self.journal, self.notify, self.agents = cfg, journal, notify, agents
        self.run, self.clock, self.sh, self.exit_hook, self.root, self.python = run, clock, sh, exit_hook, root, python
        from . import ops
        self.positions_block = positions_block or ops.positions_block_mode_change
        self.state = self._load()

    # -- persistence (logs/maintenance.json)
    def _path(self):
        return os.path.join(self.cfg.log_dir, "maintenance.json")

    def _load(self) -> dict:
        try:
            with open(self._path(), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"pending_since": None, "reboot_at": None, "last_check": 0, "housekeeping_day": None, "bad_sha": None,
                    "updates": None}

    def _save(self):
        os.makedirs(self.cfg.log_dir, exist_ok=True)
        tmp = self._path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        os.replace(tmp, self._path())

    def _card(self, title, text):
        try:
            self.notify.card(f"AUREON · MT5 · {title}", text, footer="Aureon MT5 · server")
        except Exception:
            pass

    # -- the gate
    def window(self, now: float) -> tuple[bool, str]:
        flat = not self.positions_block(self.agents)
        if not flat:
            return False, "a position is open"
        if not in_session(self.cfg, now):
            return True, "flat, outside the session"
        ps = self.state.get("pending_since")
        if ps and now - ps >= float(self.cfg.maint_force_after_h) * 3600:
            return True, f"flat, reboot pending ≥ {self.cfg.maint_force_after_h:g} h"
        return False, f"inside the session ({self.cfg.session_ist} IST)"

    def tick(self, now: float | None = None) -> str | None:
        now = self.clock() if now is None else now
        if self.state.get("reboot_at"):
            return self._countdown(now)
        if now - float(self.state.get("last_check") or 0) < CHECK_EVERY_S:
            return None
        self.state["last_check"] = now
        pending, n = pending_reboot(self.run)
        STATE["updates_pending"] = n
        self.state["updates"] = n
        if pending and not self.state.get("pending_since"):
            self.state["pending_since"] = now
            self.journal.log("maintenance_pending", updates=n)
        if pending is False:
            self.state["pending_since"] = None
        self._save()
        ok, why = self.window(now)
        STATE["maintenance_window"] = why
        if not ok:
            return None
        if pending:
            return self._schedule_reboot(now, n)
        return self.housekeeping(now)

    def _schedule_reboot(self, now: float, n: int | None) -> str:
        from . import ops
        self._card("MAINTENANCE", f"rebooting for Windows Update in {COUNTDOWN_S} s (flat · {STATE.get('maintenance_window')}) · "
                                  f"updates: {n if n is not None else '?'}")
        ops.write_restart_marker(self.cfg.log_dir, "maintenance", f"updates applied: {n if n is not None else '?'}")
        ok, out = self.run(f'shutdown /r /t {COUNTDOWN_S} /c "Aureon maintenance: Windows Update"', 10)
        self.journal.log("maintenance_reboot", updates=n, issued=ok, out=out[:120])
        if not ok:
            self._card("MAINTENANCE FAILED", f"shutdown /r failed: {out[:200]}")
            return "reboot failed"
        self.state["reboot_at"] = now + COUNTDOWN_S
        self._save()
        return "reboot scheduled"

    def _countdown(self, now: float) -> str:
        if self.positions_block(self.agents):                       # a position opened during the 60 s: abort, reschedule
            self.run("shutdown /a", 10)
            self.state["reboot_at"] = None; self.state["last_check"] = 0
            self._save()
            try:
                from . import ops
                ops.pop_restart_marker(self.cfg.log_dir)
            except Exception:
                pass
            self.journal.log("maintenance_aborted", reason="position opened")
            self._card("MAINTENANCE ABORTED", "a position opened during the countdown — reboot cancelled, rescheduled for the next window")
            return "aborted"
        return "counting down"

    # -- housekeeping in the window (once per IST day)
    def housekeeping(self, now: float) -> str | None:
        day = _ist(now).strftime("%Y-%m-%d")
        if self.state.get("housekeeping_day") == day:
            return None
        self.state["housekeeping_day"] = day; self._save()
        removed = rotate_logs(self.cfg.log_dir, float(self.cfg.log_keep_days), now)
        pip = self.pip_if_changed()
        self.journal.log("maintenance_housekeeping", removed=removed, pip=pip)
        res = self.update_code()
        return f"housekeeping: removed {removed} · pip {pip} · update {res}"

    def pip_if_changed(self) -> str:
        import hashlib
        req = os.path.join(self.root, "requirements.txt")
        try:
            digest = hashlib.sha256(open(req, "rb").read()).hexdigest()
        except OSError:
            return "no requirements.txt"
        mark = os.path.join(self.cfg.log_dir, "requirements.sha")
        try:
            if open(mark, encoding="utf-8").read().strip() == digest:
                return "unchanged"
        except OSError:
            pass
        try:
            r = self.sh([self.python, "-m", "pip", "install", "-r", req], capture_output=True, text=True, timeout=600)
            ok = r.returncode == 0
        except Exception as e:
            ok = False; r = types_ns(stderr=repr(e))
        if ok:
            with open(mark, "w", encoding="utf-8") as f:
                f.write(digest)
        self._card("MAINTENANCE · pip", "requirements installed" if ok else f"pip install failed: {str(getattr(r, 'stderr', '') or '')[-300:]}")
        return "installed" if ok else "failed"

    def update_code(self) -> str:
        """git → origin/master, then the suite; green → exit 75 (the launcher restarts on the new code); red → back to the
        old commit and that sha is skipped until origin/master moves again."""
        from . import ops
        from .common.updater import update_to_origin
        res = update_to_origin(self.cfg.log_dir, run=self.sh, cwd=self.root)
        if res["status"] != "updated":
            return res["status"]
        if res["new"] == self.state.get("bad_sha"):
            self.sh(["git", "reset", "--hard", res["old"]], cwd=self.root, capture_output=True, text=True, timeout=60)
            return "skipped (known red)"
        try:
            r = self.sh([self.python, "-m", "pytest", "-q", "tests"], cwd=self.root, capture_output=True, text=True, timeout=1800)
            green = r.returncode == 0; summary = ((r.stdout or "").strip().splitlines() or ["?"])[-1]
        except Exception as e:
            green, summary = False, repr(e)
        if not green:
            self.sh(["git", "reset", "--hard", res["old"]], cwd=self.root, capture_output=True, text=True, timeout=60)
            self.state["bad_sha"] = res["new"]; self._save()
            self._card("UPDATE FAILED TESTS", f"origin/master `{res['new'][:8]}`: {summary} — staying on `{res['old'][:8]}`")
            self.journal.log("maintenance_update_red", new=res["new"], summary=summary)
            return "red"
        ops.write_restart_marker(self.cfg.log_dir, "auto-update", f"{res['old'][:8]} -> {res['new'][:8]} · {summary}")
        self._card("MAINTENANCE · update", f"`{res['old'][:8]}` → `{res['new'][:8]}` · {summary} · restarting")
        self.journal.log("maintenance_update", old=res["old"], new=res["new"], summary=summary)
        (self.exit_hook or ops.hard_exit)(ops.EXIT_RESTART)
        return "updated"


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


def rotate_logs(log_dir: str, keep_days: float, now: float | None = None) -> int:
    """Delete only disposable files: logs/charts/*.png > 7 days, stall dumps, replay summaries, rotated service logs and saved
    local-change patches older than keep_days. Never the journal, state, incidents, features or markers."""
    now = _time.time() if now is None else now
    removed = 0
    rules = [(os.path.join(log_dir, "charts"), (".png",), 7.0)]
    for d, exts, days in rules + [(log_dir, None, keep_days), (os.path.join(log_dir, "replay"), (".json",), keep_days)]:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            disposable = (exts and name.endswith(exts)) or (exts is None and (
                name.startswith("stall-") or name.startswith("local_changes_") or re.match(r"service\.log\.\d+$", name)
                or re.match(r"supervisor.*\.log$", name)))
            if disposable and now - os.path.getmtime(p) > days * 86400:
                try:
                    os.remove(p); removed += 1
                except OSError:
                    pass
    return removed


# ============================================================================= F2 MT5 as a managed dependency
class Mt5Manager:
    """terminal64 hang (broker not connected for 60 s while the process exists) → restart it, re-verify login + symbols."""

    def __init__(self, cfg, journal, notify, broker, *, run=run_ps, clock=_time.time, sleep=_time.sleep):
        self.cfg, self.journal, self.notify, self.broker, self.run, self.clock, self.sleep = cfg, journal, notify, broker, run, clock, sleep
        self.down_since = None

    def exe(self) -> str | None:
        if self.cfg.mt5_path:
            return self.cfg.mt5_path
        try:
            return open(mt5_path_file(self.cfg.log_dir), encoding="utf-8").read().strip() or None
        except OSError:
            return None

    def tick(self, now: float | None = None) -> str | None:
        if self.cfg.dry:
            return None
        now = self.clock() if now is None else now
        if self.broker.connected():
            self.down_since = None
            return None
        if self.down_since is None:
            self.down_since = now
        if now - self.down_since < 60:
            return None
        ok, out = self.run("(Get-Process terminal64 -ErrorAction SilentlyContinue | Measure-Object).Count", 5)
        running = ok and out.strip().isdigit() and int(out.strip()) > 0
        return self.restart(reason="hang (not connected 60 s, process alive)" if running else "terminal not running")

    def restart(self, reason: str) -> str:
        exe = self.exe()
        self.journal.log("mt5_restart", reason=reason, exe=exe)
        self.run("Stop-Process -Name terminal64 -Force -ErrorAction SilentlyContinue", 15)
        if exe:
            self.run(f"Start-Process -FilePath '{exe}'", 15)
        self.sleep(15)
        self.broker.reset()
        up = self.broker.connect()
        acct = self.broker.account() if up else None
        missing = []
        for sym in self.cfg.symbols:
            try:
                if not self.broker.mt5().symbol_info(sym):
                    missing.append(sym)
            except Exception:
                missing.append(sym)
        self.down_since = None if up else self.clock()
        text = (f"{reason} → terminal restarted · " + (f"login {acct['login_masked']} ({acct['trade_mode']})" if acct else "LOGIN NOT VERIFIED")
                + (f" · symbols missing: {', '.join(missing)}" if missing else " · symbols visible"))
        try:
            self.notify.card("AUREON · MT5 · MT5 RESTARTED" if up else "AUREON · MT5 · MT5 NOT UP", text, footer="Aureon MT5 · server")
        except Exception:
            pass
        return "up" if up else "down"


# ============================================================================= F3 clock + network
def clock_drift(run=run_ps) -> float | None:
    ok, out = run("w32tm /stripchart /computer:time.windows.com /samples:1 /dataonly", 5)
    if not ok:
        return None
    m = re.findall(r"([+-]\d+(?:\.\d+)?)s", out)
    return float(m[-1]) if m else None


def check_clock(journal, notify, run=run_ps) -> float | None:
    d = clock_drift(run)
    STATE["drift"] = d
    if d is not None and abs(d) > DRIFT_LIMIT_S:
        ok, out = run("w32tm /resync /force", 15)
        journal.log("clock_drift", drift=d, resynced=ok)
        try:
            notify.card(f"AUREON · MT5 · CLOCK DRIFT {abs(d):.1f} s · {'resynced' if ok else 'RESYNC FAILED'}",
                        "session gates and news windows depend on the clock", footer="Aureon MT5 · server")
        except Exception:
            pass
    return d


class NetworkWatch:
    """Discord AND the dead-man ping failing while MT5 still ticks → network_degraded: keep polling, the guardian keeps its
    stops, NO new entries; cards queue in the Notifier and one summary goes out on reconnect. Never flattens."""

    def __init__(self, journal):
        self.journal = journal

    def tick(self, discord_ok: bool, deadman_ok: bool | None, mt5_ok: bool) -> bool:
        degraded = (not discord_ok) and (deadman_ok is not True) and mt5_ok
        if degraded != STATE["network_degraded"]:
            STATE["network_degraded"] = degraded
            try:
                self.journal.log("network_degraded" if degraded else "network_restored")
            except Exception:
                pass
        return degraded


# ============================================================================= F4 /server + heartbeat fields
def _try(run, cmd, timeout=5, default="unknown"):
    ok, out = run(cmd, timeout)
    return out.strip() if ok and out.strip() else default


def server_card(cfg, agents, journal, *, run=run_ps, started: float | None = None, git=subprocess.run, broker=None) -> dict:
    """Every field degrades to 'unknown' when its OS call fails."""
    now = _time.time()
    up = f"{int((now - started) // 3600)} h {int((now - started) % 3600 // 60)} min" if started else "unknown"
    boot = STATE.get("boot") or {}
    try:
        du = shutil.disk_usage(cfg.log_dir if os.path.exists(cfg.log_dir) else ".")
        disk = f"{du.free / 1e9:.1f} GB free"
    except Exception:
        disk = "unknown"
    ram = _try(run, "$o=Get-CimInstance Win32_OperatingSystem; '{0:N1} / {1:N1} GB free' -f ($o.FreePhysicalMemory/1MB),($o.TotalVisibleMemorySize/1MB)")
    cpu = _try(run, "(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average")
    pid = _try(run, "(Get-Process terminal64 -ErrorAction SilentlyContinue | Select-Object -First 1).Id")
    try:
        acct = broker.account() if broker is not None else None
        login = f"{acct['login_masked']} ({acct['trade_mode']})" if acct else "unknown"
    except Exception:
        login = "unknown"
    ages = []
    for sym, ag in (agents or {}).items():
        snap = getattr(ag, "snapshot", None) or {}
        if snap.get("tick_age") is not None:
            ages.append(f"{sym} {snap['tick_age']}s")
    def g(*args):
        try:
            r = git(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10)
            return (r.stdout or "").strip() or "unknown"
        except Exception:
            return "unknown"
    head, origin = g("rev-parse", "--short", "HEAD"), g("rev-parse", "--short", "origin/master")
    restarts = [r for r in journal.read(int(now) - 30 * 86400)
                if r.get("event") in ("manual_restart", "self_restart", "cold_start", "repair_auto_applied", "maintenance_reboot",
                                      "repair_rolled_back")][-3:]
    def rline(r):
        return f"{datetime.fromtimestamp(r['t'], tz=IST):%d %b %H:%M} {r['event']} {r.get('reason') or r.get('fingerprint') or ''}".strip()
    try:
        from .repair import listing
        inc = listing(cfg.log_dir)
    except Exception:
        inc = "unknown"
    drift = STATE.get("drift")
    upd = STATE.get("updates_pending")
    fields = [
        {"name": "Uptime", "value": up, "inline": True},
        {"name": "Last boot", "value": f"{boot.get('reason', 'unknown')} · down {boot.get('down_s', '?')} s" if boot else "unknown", "inline": True},
        {"name": "Updates", "value": f"{upd if upd is not None else 'unknown'} pending · window: {STATE.get('maintenance_window') or 'unknown'}", "inline": False},
        {"name": "MT5", "value": f"pid {pid} · login {login} · tick age {', '.join(ages) or 'unknown'}", "inline": False},
        {"name": "Clock drift", "value": f"{drift:+.2f} s" if drift is not None else "unknown", "inline": True},
        {"name": "Disk", "value": disk, "inline": True},
        {"name": "RAM", "value": ram, "inline": True},
        {"name": "CPU", "value": f"{cpu} %" if cpu != "unknown" else "unknown", "inline": True},
        {"name": "Repo", "value": f"`{head}` vs origin/master `{origin}`" + (" ✅" if head == origin and head != "unknown" else ""), "inline": False},
        {"name": "Last restarts", "value": "\n".join(rline(r) for r in restarts) or "none in 30 days", "inline": False},
        {"name": "Incidents", "value": inc[:1000], "inline": False},
        {"name": "Network", "value": "DEGRADED — no new entries" if STATE["network_degraded"] else "ok", "inline": True},
    ]
    return {"title": "AUREON · MT5 · SERVER", "fields": fields}


def heartbeat_line() -> str:
    d = STATE.get("disk_free_gb"); drift = STATE.get("drift"); upd = STATE.get("updates_pending"); age = STATE.get("mt5_age")
    return (f"disk {f'{d:.0f} GB' if d is not None else '?'} · drift {f'{drift:+.1f} s' if drift is not None else '?'} · "
            f"updates pending {upd if upd is not None else '?'} · MT5 age {f'{age}s' if age is not None else '?'}")


def harden(plan: bool = True, run=run_ps, root: str = ROOT) -> str:
    """scripts/aureon_server.ps1 -Action plan|harden through the OS seam (harden needs an elevated session)."""
    script = os.path.join(root, "scripts", "aureon_server.ps1")
    ok, out = run(f"& '{script}' -Action {'plan' if plan else 'harden'}", 120)
    return out if ok else f"harden unavailable: {out}"
