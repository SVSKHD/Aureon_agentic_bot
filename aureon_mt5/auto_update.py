"""v2.17.0 — auto-update on merge: poll origin/master every AUREON_UPDATE_POLL_MIN (2) minutes, apply when flat.

    master moved, flat (no live / paper position, no pending order)
        → git (the v2.2.2 path: pull --ff-only; local edits → logs/local_changes_<sha>_<ts>.patch, then discarded;
          .env / logs/ / out/ / news_blackout.txt never touched) → the suite → restart (exit 75 when AUREON_SUPERVISED=1,
          in-process re-exec otherwise). Card `UPDATE APPLIED · <short sha> · <PR title from the merge commit>`.
    master moved, a position or pending order is open
        → update_pending, card `UPDATE PENDING · will apply when flat`; applied at the first flat moment (after the
          guardian's own close — nothing is ever closed for an update) or in the maintenance window, whichever first.
    suite red → back to the running commit, no restart, card `UPDATE HELD · tests failed · <first failure line>`; that sha
          is not retried — the next merge is.

/update (admin) forces the check · /update hold | resume pauses / resumes the automatic path · AUREON_AUTO_UPDATE=0 turns
the automatic path off (/update still works). State: logs/auto_update.json.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time as _time
from datetime import datetime, timedelta, timezone

from .common.gitinfo import ROOT

IST = timezone(timedelta(hours=5, minutes=30))


def _git(sh, root, *args, timeout=30) -> tuple[bool, str]:
    try:
        r = sh(["git", *args], capture_output=True, text=True, timeout=timeout, cwd=root)
        return r.returncode == 0, (r.stdout or "").strip()
    except Exception as e:
        return False, repr(e)


def pr_title(subject: str, body: str) -> str:
    """'Merge pull request #31 from x/y' + body 'resilience: …' → the body's first line (the PR title)."""
    if subject.startswith("Merge pull request") and body.strip():
        return body.strip().splitlines()[0].strip()
    return subject.strip()


def first_failure(output: str) -> str:
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith(("FAILED ", "ERROR ")):
            return ln[:200]
    for ln in lines:
        if "Error" in ln or "error" in ln:
            return ln[:200]
    return (lines[-1] if lines else "no output")[:200]


def restart(agents, *, exit_hook=None, execv=None, environ=None, argv=None):
    """exit 75 under the launcher (AUREON_SUPERVISED=1), else re-exec this process in place on the new code."""
    from . import ops
    from .selfheal import supervised
    if supervised(environ):
        return (exit_hook or (lambda code: ops.hard_exit(code, agents)))(ops.EXIT_RESTART)
    ops.STOP.set()
    for ag in list((agents or {}).values()):
        try:
            ag.stop(); ag.join(timeout=5)
        except Exception:
            pass
    try:
        from . import broker
        broker.close_connection()
    except Exception:
        pass
    args = [sys.executable] + list(sys.argv if argv is None else argv)
    return (execv or os.execv)(sys.executable, args)


CURRENT: dict = {}                                   # {"updater": AutoUpdater} for /update (set in main)


def dns_down() -> bool:
    """netguard's view: discord.com (or github.com, when checked) did not resolve on the last DNS check."""
    try:
        from . import netguard
        st = netguard.STATE
        return bool(st.get("dns_fail")) or any((st.get("dns") or {}).get(k, {}).get("ok") is False for k in ("discord", "github"))
    except Exception:
        return False


class AutoUpdater:
    def __init__(self, cfg, journal, notify, agents, *, sh=subprocess.run, root: str = ROOT, python: str = sys.executable,
                 clock=_time.time, flat=None, restart_fn=None, update=None):
        self.cfg, self.journal, self.notify, self.agents = cfg, journal, notify, agents
        self.sh, self.root, self.python, self.clock = sh, root, python, clock
        self.flat_fn = flat or self._flat
        self.restart_fn = restart_fn or (lambda: restart(self.agents))
        self.update = update                          # injectable update_to_origin (tests)
        self.on_problem = None                        # v2.18.1: ops_repair's auto hook (UPDATE FAILED / UPDATE HELD)
        self.state = self._load()

    # -- persistence
    def _path(self):
        return os.path.join(self.cfg.log_dir, "auto_update.json")

    def _load(self) -> dict:
        try:
            with open(self._path(), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"hold": False, "update_pending": None, "held_sha": None, "last_sha": None, "last_at": None, "last_check": 0}

    def _save(self):
        os.makedirs(self.cfg.log_dir, exist_ok=True)
        tmp = self._path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        os.replace(tmp, self._path())

    def _card(self, title, text="", colour=None):
        try:
            self.notify.card(title, text, color=colour, footer="Aureon MT5 · auto-update")
        except Exception:
            pass

    # -- flat = no live position, no paper position, no pending order (an unreadable snapshot is NOT flat)
    def _flat(self) -> tuple[bool, str]:
        from . import broker, ops
        if ops.positions_block_mode_change(self.agents):
            return False, "a position is open (or the snapshot cannot prove flat)"
        for sym, ag in (self.agents or {}).items():
            try:
                if (self.cfg.paper or self.cfg.dry) and getattr(ag, "_paper_open", None) and ag._paper_open():
                    return False, f"{sym}: a paper position is open"
            except Exception:
                pass
            if not self.cfg.dry:
                orders = broker.pending_orders(sym)
                if orders is None:
                    return False, f"{sym}: pending orders unreadable"
                if orders:
                    return False, f"{sym}: a pending order is open (#{orders[0]['ticket']})"
        return True, "flat"

    # -- the check
    def remote(self) -> tuple[str | None, str | None]:
        """(HEAD, origin/master) after a fetch — the existing fetch path (git fetch origin master)."""
        ok, _ = _git(self.sh, self.root, "fetch", "origin", "master")
        if not ok:
            return None, None
        ok1, head = _git(self.sh, self.root, "rev-parse", "HEAD", timeout=10)
        ok2, origin = _git(self.sh, self.root, "rev-parse", "origin/master", timeout=10)
        return (head if ok1 else None), (origin if ok2 else None)

    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "auto_update", True)) and not self.state.get("hold")

    def tick(self, now: float | None = None) -> str | None:
        """The main loop calls this every pass: cheap unless a poll is due or an update is pending."""
        now = self.clock() if now is None else now
        if not self.enabled():
            return None
        if self.state.get("update_pending"):
            ok, _ = self.flat_fn()
            return self.apply(why="flat again") if ok else None
        if now - float(self.state.get("last_check") or 0) < float(getattr(self.cfg, "update_poll_min", 2.0)) * 60:
            return None
        return self.check(now)

    def check(self, now: float | None = None, *, force: bool = False, why: str = "") -> str:
        now = self.clock() if now is None else now
        self.state["last_check"] = now; self._save()
        head, origin = self.remote()
        if not head or not origin:
            self._error("git fetch origin master failed")
            return "fetch failed"
        if head == origin:
            if self.state.get("update_pending"):
                self.state["update_pending"] = None; self._save()
            return "current"
        if origin == self.state.get("held_sha") and not force:
            return "held (tests failed on this sha) — waiting for the next merge"
        ok, reason = self.flat_fn()
        if not ok:
            if self.state.get("update_pending") != origin:
                self.state["update_pending"] = origin; self._save()
                self.journal.log("update_pending", sha=origin, reason=reason)
                self._card(f"UPDATE PENDING · will apply when flat", f"origin/master `{origin[:8]}` · {self.title(origin)}\n{reason}",
                           0xF1C40F)
            return "pending"
        return self.apply(why=why or ("forced" if force else "merge"))

    def title(self, sha: str) -> str:
        ok, out = _git(self.sh, self.root, "log", "-1", "--format=%s%n%b", sha, timeout=10)
        if not ok or not out:
            return "?"
        subject, _, body = out.partition("\n")
        return pr_title(subject, body)

    def apply(self, why: str = "", *, forced: bool = False, skip_suite: bool = False) -> str:
        """git → suite → restart. Red → back to the running commit, no restart. forced (/update force): the flat gate was
        skipped — open positions are carried (the guardian is restart-safe), never closed. skip_suite: /update force notests."""
        from .common.updater import discarded_title, update_to_origin
        res = (self.update or (lambda: update_to_origin(self.cfg.log_dir, run=self.sh, cwd=self.root)))()
        if res["status"] == "current":
            self.state["update_pending"] = None; self._save()
            return "current"
        if res["status"] != "updated":
            self._card("UPDATE FAILED", res.get("error", "")[:300], 0xE74C3C)
            self.journal.log("update_failed", error=res.get("error", "")[:200])
            self._error(res.get("error", ""), kind="UPDATE FAILED")
            return "failed"
        if res.get("discarded"):
            self._card(discarded_title(res), f"restore: `git apply {res['patch']}`")
        new = res["new"]; old = res["old"]
        if skip_suite:
            green, out, summary = True, "", "SUITE SKIPPED"
        else:
            try:
                r = self.sh([self.python, "-m", "pytest", "-q", "-x", "tests"], cwd=self.root, capture_output=True, text=True, timeout=1800)
                green = r.returncode == 0
                out = (r.stdout or "") + "\n" + (r.stderr or "")
                summary = ((r.stdout or "").strip().splitlines() or ["?"])[-1]
            except Exception as e:
                green, out, summary = False, repr(e), repr(e)
        title = self.title(new)
        if not green:
            _git(self.sh, self.root, "reset", "--hard", old, timeout=60)        # the files on disk = the running build again
            line = first_failure(out)
            self.state.update(held_sha=new, update_pending=None); self._save()
            self.journal.log("update_held", sha=new, old=old, failure=line)
            self._card(f"UPDATE HELD · tests failed · {line}"[:256], f"origin/master `{new[:8]}` · {title}\nstill running `{old[:8]}` · "
                       "retried on the next merge", 0xE74C3C)
            self._error(f"tests failed on {new[:8]}: {line}\n{out[-1500:]}", kind="UPDATE HELD")
            return "held"
        from . import ops
        self.state.update(last_sha=new, last_at=self.clock(), update_pending=None, held_sha=None, last_error=None); self._save()
        self.journal.log("update_applied", sha=new, old=old, title=title, summary=summary, why=why, forced=forced, suite_skipped=skip_suite)
        if forced:
            n = self.positions_open()
            ops.write_restart_marker(self.cfg.log_dir, "update-force", f"{old[:8]} -> {new[:8]} · {n} position(s) carried"
                                     + (" · SUITE SKIPPED" if skip_suite else ""))
            self._card(f"UPDATE FORCED · {old[:7]} → {new[:7]} · {n} positions carried" + (" · SUITE SKIPPED" if skip_suite else ""),
                       f"{title} · {summary} · restarting ({why}) — nothing was closed; the guardian re-adopts the positions on boot",
                       0xE74C3C if skip_suite else 0xE67E22)
        else:
            ops.write_restart_marker(self.cfg.log_dir, "auto-update", f"{old[:8]} -> {new[:8]} · {title}")
            self._card(f"UPDATE APPLIED · {new[:7]} · {title}"[:256], f"`{old[:8]}` → `{new[:8]}` · {summary} · restarting ({why})",
                       0x2ECC71)
        self.restart_fn()
        return "applied"

    def positions_open(self) -> int:
        n = 0
        for ag in (self.agents or {}).values():
            n += len((getattr(ag, "snapshot", None) or {}).get("positions") or [])
            try:
                if getattr(ag, "_paper_open", None) and (self.cfg.paper or self.cfg.dry):
                    n += len(ag._paper_open())
            except Exception:
                pass
        return n

    def _error(self, text: str, kind: str = ""):
        """The last error (/update status) and, for UPDATE FAILED / UPDATE HELD, the repair helper's auto hook."""
        self.state["last_error"] = str(text)[:2000]; self.state["last_error_at"] = self.clock(); self._save()
        if kind and self.on_problem is not None:
            try:
                self.on_problem(kind, str(text))
            except Exception:
                pass

    def force(self, user="", *, skip_suite: bool = False) -> str:
        """/update force [notests]: pull now whatever is open (never closing anything), suite (unless skipped), restart."""
        if dns_down():
            self.journal.log("update_refused", by=str(user), why="network degraded")
            self._card("UPDATE REFUSED · network degraded", "DNS is down — a forced pull can only fail; retry when the network is back",
                       0xE74C3C)
            return "UPDATE REFUSED · network degraded"
        self.state["last_check"] = self.clock(); self._save()
        head, origin = self.remote()
        if not head or not origin:
            self._error("git fetch origin master failed")
            return "fetch failed — /update status shows the error"
        if head == origin:
            return "already at origin/master — " + self.boot_line()
        res = self.apply(why=f"/update force by {user}", forced=True, skip_suite=skip_suite)
        return {"applied": "UPDATE FORCED — restarting", "held": "UPDATE HELD · tests failed (card posted) — nothing restarted",
                "failed": "UPDATE FAILED (card posted)"}.get(res, res)

    # -- /update
    def command(self, arg: str = "", user="") -> str:
        a = (arg or "").strip().lower()
        if a == "hold":
            self.state["hold"] = True; self._save()
            self.journal.log("update_hold", by=str(user))
            return "auto-update **on hold** — merges are not applied until /update resume (/update still forces a check)"
        if a == "resume":
            self.state["hold"] = False; self._save()
            self.journal.log("update_resume", by=str(user))
            return "auto-update **resumed** — " + self.boot_line()
        if a == "status":
            return self.status_text()
        if a == "force":
            return self.force(user)
        if a == "force notests":
            self.state["notests_until"] = self.clock() + 60; self._save()
            return "⚠️ the suite will NOT run — confirm with `/update force notests confirm` within 60 s"
        if a == "force notests confirm":
            until = float(self.state.get("notests_until") or 0)
            self.state["notests_until"] = None; self._save()
            if self.clock() > until:
                return "⛔ no pending `/update force notests` (or it is older than 60 s) — start again"
            return self.force(user, skip_suite=True)
        if a not in ("", "check", "now"):
            return "⚠️ /update · /update status · /update force [notests] · /update hold · /update resume"
        res = self.check(force=True, why=f"/update by {user}")
        return {"current": "already at origin/master — " + self.boot_line(),
                "pending": "UPDATE PENDING · will apply when flat",
                "held": "UPDATE HELD · tests failed (card posted)",
                "applied": "UPDATE APPLIED — restarting"}.get(res, res)

    def status_text(self) -> str:
        head = _git(self.sh, self.root, "rev-parse", "HEAD", timeout=10)[1] or "?"
        origin = _git(self.sh, self.root, "rev-parse", "origin/master", timeout=10)[1] or "?"
        lc = self.state.get("last_check")
        when = datetime.fromtimestamp(float(lc), IST).strftime("%d %b %H:%M:%S IST") if lc else "never"
        pend, held = self.state.get("update_pending"), self.state.get("held_sha")
        err = (self.state.get("last_error") or "")[:300]
        return "\n".join([f"**/update status** · {self.boot_line()}",
                          f"last check {when} · HEAD `{head[:8]}` · origin/master `{origin[:8]}`"
                          + (" (behind)" if head != origin and "?" not in (head, origin) else ""),
                          f"pending: {pend[:8] if pend else '—'} · held: {held[:8] if held else '—'} · hold: {'on' if self.state.get('hold') else 'off'}",
                          f"last error: {err or '—'}"])

    def boot_line(self) -> str:
        if not getattr(self.cfg, "auto_update", True):
            state = "auto-update off (AUREON_AUTO_UPDATE=0)"
        else:
            state = "auto-update on" + (" · HOLD" if self.state.get("hold") else "")
        sha, at = self.state.get("last_sha"), self.state.get("last_at")
        if not sha:
            ok, out = _git(self.sh, self.root, "log", "-1", "--format=%H %ct", timeout=10)
            if ok and out:
                sha, _, ct = out.partition(" ")
                at = float(ct) if ct.strip().isdigit() else None
        when = datetime.fromtimestamp(at, IST).strftime("%d %b %H:%M IST") if at else "?"
        pend = self.state.get("update_pending")
        return f"{state} · last {sha[:7] if sha else '?'} {when}" + (f" · pending {pend[:7]}" if pend else "")
