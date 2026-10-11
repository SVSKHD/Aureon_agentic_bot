"""v2.2.2 — server-authoritative update: the checkout always ends exactly at origin/master, or is left untouched.

Local edits to tracked files are saved as a patch in the log dir, then discarded (git reset --hard origin/master).
Ignored paths (.env, logs/, out/, news_blackout.txt) are never touched by git; PRESERVE files are also restored
byte-for-byte after the update (covers a file that origin/master stops tracking). Never raises.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone

from .gitinfo import ROOT

IST = timezone(timedelta(hours=5, minutes=30))
PRESERVE = (".env", "news_blackout.txt")


def _git(run, cwd, *args, timeout=30):
    try:
        r = run(["git", *args], capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode == 0, (r.stdout or "").rstrip(), (r.stderr or r.stdout or "").strip()   # keep porcelain's leading space
    except Exception as e:                                   # git missing, timeout, …
        return False, "", f"{e!r}"


def discarded_title(res: dict) -> str:
    return f"AUREON · MT5 · UPDATE · LOCAL EDITS DISCARDED · {', '.join(res['discarded'][:10])} · patch saved {res['patch']}"[:256]


def update_to_origin(log_dir: str, run=subprocess.run, cwd: str = ROOT) -> dict:
    """→ {status: current|updated|failed, old, new, discarded: [files], patch, error}."""
    out = {"status": "failed", "old": "", "new": "", "discarded": [], "patch": "", "error": ""}
    ok, _, err = _git(run, cwd, "fetch", "origin", "master")
    if not ok:
        out["error"] = f"git fetch failed: {err[:200]}"; return out
    ok_l, local, _ = _git(run, cwd, "rev-parse", "HEAD", timeout=10)
    ok_r, remote, _ = _git(run, cwd, "rev-parse", "origin/master", timeout=10)
    if not (ok_l and ok_r and local and remote):
        out["error"] = "git rev-parse failed"; return out
    out.update(old=local, new=remote)
    if local == remote:
        out["status"] = "current"; return out
    ok, status, err = _git(run, cwd, "status", "--porcelain", "--untracked-files=no", timeout=10)
    if not ok:
        out["error"] = f"git status failed: {err[:200]}"; return out
    dirty = [line[3:] for line in status.splitlines() if line.strip()]
    kept = {}
    for name in PRESERVE:
        try:
            with open(os.path.join(cwd, name), "rb") as f: kept[name] = f.read()
        except OSError:
            pass
    if dirty:
        ok, diff, err = _git(run, cwd, "diff", "HEAD")
        if not ok:
            out["error"] = f"git diff failed: {err[:200]}"; return out
        try:                                                 # no patch on disk → nothing is discarded
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir, f"local_changes_{local[:8]}_{datetime.now(IST):%Y%m%d-%H%M%S}.patch")
            with open(path, "w", encoding="utf-8") as f: f.write(diff + "\n")
        except OSError as e:
            out["error"] = f"patch not saved: {e!r}"[:200]; return out
        out.update(discarded=dirty, patch=path)
        ok, _, err = _git(run, cwd, "reset", "--hard", "origin/master", timeout=60)
    else:
        ok, _, err = _git(run, cwd, "pull", "--ff-only", "origin", "master", timeout=60)
        if not ok:                                           # diverged / non-ff: origin/master wins
            ok, _, err = _git(run, cwd, "reset", "--hard", "origin/master", timeout=60)
    for name, data in kept.items():
        try:
            p = os.path.join(cwd, name)
            if not os.path.exists(p) or open(p, "rb").read() != data:
                with open(p, "wb") as f: f.write(data)
        except OSError:
            pass
    ok_h, head, _ = _git(run, cwd, "rev-parse", "HEAD", timeout=10)
    if not ok or not ok_h or head != remote:
        out["error"] = f"update incomplete: {err[:200]}" if not ok else f"HEAD {head[:8]} != origin/master {remote[:8]}"
        return out
    out["status"] = "updated"
    return out
