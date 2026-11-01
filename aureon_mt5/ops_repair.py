"""v2.18.1 — the bounded local repair helper (`/repair`, `/repair go`, `/repair auto on|off`).

Diagnosis + an ALLOWLIST of named actions — never a free shell, never an order.

    inputs    the last 200 lines of logs/aureon.log, the updater's last error, `git status --porcelain`,
              `git rev-parse HEAD origin/master`, DNS for github.com / discord.com, `pip check`, the failing test's output
    actions   fetch_origin · reset_to_origin (the local patch saved first, as the updater does) · pip_install_requirements ·
              rerun_suite · restart_bot (exit 75 / re-exec) · restart_mt5_terminal (scripts/aureon_service.ps1) · flush_card_queue
    flow      /repair → `REPAIR · diagnosis` (the cause in one line + the actions it wants) → /repair go runs them in order,
              stops at the first failure → `REPAIR · done|failed` with what changed
    auto      /repair auto on: UPDATE FAILED / UPDATE HELD run diagnosis + go without the /repair go (never a trade card)
    limits    3 runs an hour · never touches .env, logs/, guardian_state.json · journal actor=repair for every action

The rules below decide first (DNS down · a locked git index · a failing test · behind origin). Only when they find nothing
does it ask Claude — through the same Claude Code CLI login the advisor uses (no API key path), AUREON_REPAIR_MODEL
(default the advisor's review model) — and its answer is held to the same allowlist: anything unknown is rejected
before a single action runs.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time as _time
from datetime import datetime, timedelta, timezone

from .common.gitinfo import ROOT

IST = timezone(timedelta(hours=5, minutes=30))
ACTIONS = ("fetch_origin", "reset_to_origin", "pip_install_requirements", "rerun_suite", "restart_bot", "restart_mt5_terminal",
           "flush_card_queue")
MAX_RUNS_PER_HOUR = 3
AUTO_KINDS = ("UPDATE FAILED", "UPDATE HELD")
PROTECTED = (".env", "logs", "guardian_state.json")
STALE_LOCK_S = 120.0
CURRENT: dict = {}                                   # {"repair": OpsRepair} (set in main)


class Rejected(ValueError):
    pass


def validate(actions) -> list[str]:
    """Every name must be on the allowlist — one unknown name rejects the whole list (nothing runs)."""
    out = []
    for a in actions or []:
        if not isinstance(a, str) or a not in ACTIONS:
            raise Rejected(f"action {a!r} is not on the allowlist ({', '.join(ACTIONS)}) — nothing was run")
        out.append(a)
    return out


# ----------------------------------------------------------------------------- inputs
def _run(sh, root, cmd, timeout=60) -> tuple[bool, str]:
    try:
        r = sh(cmd, capture_output=True, text=True, timeout=timeout, cwd=root)
        return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return False, repr(e)


def resolves(host: str, resolver=None) -> bool:
    try:
        (resolver or socket.getaddrinfo)(host, 443)
        return True
    except Exception:
        return False


def gather(log_dir: str, *, updater=None, sh=subprocess.run, root: str = ROOT, python: str = sys.executable, resolver=None,
           test_output: str = "") -> dict:
    """The read-only inputs, each bounded."""
    tail = []
    try:
        with open(os.path.join(log_dir, "aureon.log"), encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-200:]
    except OSError:
        pass
    st = getattr(updater, "state", None) or {}
    ok_s, status = _run(sh, root, ["git", "status", "--porcelain"], 20)
    ok_h, heads = _run(sh, root, ["git", "rev-parse", "HEAD", "origin/master"], 10)
    ok_p, pip = _run(sh, root, [python, "-m", "pip", "check"], 120)
    return {"log_tail": "".join(tail)[-20000:], "updater_error": str(st.get("last_error") or ""), "git_status": status,
            "git_status_ok": ok_s, "heads": heads.split() if ok_h else [], "heads_raw": heads,
            "dns": {"github.com": resolves("github.com", resolver), "discord.com": resolves("discord.com", resolver)},
            "pip_check": pip, "pip_ok": ok_p, "test_output": (test_output or str(st.get("last_error") or ""))[-4000:]}


# ----------------------------------------------------------------------------- diagnosis
def rules(ctx: dict) -> dict | None:
    """The deterministic diagnoses. → {cause, actions} or None (nothing recognised)."""
    text = "\n".join([ctx.get("updater_error", ""), ctx.get("log_tail", "")[-6000:], ctx.get("git_status", ""),
                      ctx.get("heads_raw", "")])
    dns = ctx.get("dns") or {}
    if dns.get("github.com") is False:
        both = dns.get("discord.com") is False
        return {"cause": "github.com does not resolve (DNS" + (" — discord.com neither" if both else "") + ") — nothing local to "
                         "repair; the updater retries on its next poll", "actions": []}
    if "index.lock" in text:
        return {"cause": "a stale .git/index.lock blocks git (an update was interrupted) — clear it, bring the checkout to "
                         "origin/master, prove the suite, restart", "actions": ["fetch_origin", "reset_to_origin", "rerun_suite", "restart_bot"]}
    tests = ctx.get("test_output", "") + "\n" + ctx.get("updater_error", "")
    if "ModuleNotFoundError" in tests or "ImportError" in tests or (ctx.get("pip_ok") is False and ctx.get("pip_check")):
        return {"cause": "the suite fails on a missing / broken package — install requirements.txt, rerun the suite, restart",
                "actions": ["pip_install_requirements", "rerun_suite", "restart_bot"]}
    if "FAILED " in tests or "tests failed" in tests or "AssertionError" in tests:
        import re
        m = re.search(r"(?:FAILED|ERROR) \S+(?: - [^\n]*)?", tests)
        line = m.group(0) if m else "a test fails"
        return {"cause": f"{line[:160]} — a code fault on origin/master; rerun once to rule out a flake (still held until the "
                         "next merge if it fails again)", "actions": ["rerun_suite"]}
    heads = ctx.get("heads") or []
    if len(heads) == 2 and heads[0] != heads[1] and ctx.get("updater_error"):
        return {"cause": "the checkout is behind origin/master after a failed update — fetch, reset to origin (local patch "
                         "saved), prove the suite, restart", "actions": ["fetch_origin", "reset_to_origin", "rerun_suite", "restart_bot"]}
    if "card queue" in text or "cards queue" in text:
        return {"cause": "cards are queued behind a Discord outage that has ended — flush the queue", "actions": ["flush_card_queue"]}
    return None


PROMPT = """You are Aureon's local repair helper. Diagnose the problem below and answer ONLY with JSON:
{{"cause": "<one line>", "actions": [<zero or more of: {actions}>]}}
Never invent an action outside that list, never suggest shell commands, never touch .env, logs/ or guardian_state.json,
never anything that places, changes or closes an order. Prefer no action over a guess.

updater error:
{updater_error}

git status --porcelain:
{git_status}

git rev-parse HEAD origin/master:
{heads_raw}

DNS: {dns}
pip check: {pip_check}

failing test output:
{test_output}

last log lines:
{log_tail}
"""


def ask_claude(ctx: dict, cfg, caller=None) -> dict | None:
    """Same CLI login as the advisor (claude -p, ANTHROPIC_API_KEY stripped). None when the CLI is not usable."""
    from .common import claude_cli
    call = caller or claude_cli.call
    model = os.environ.get("AUREON_REPAIR_MODEL") or getattr(cfg, "claude_review_model", "haiku")
    prompt = PROMPT.format(actions=", ".join(ACTIONS), **{k: str(ctx.get(k, ""))[-3000:] for k in
                                                         ("updater_error", "git_status", "heads_raw", "dns", "pip_check",
                                                          "test_output", "log_tail")})
    res = call(prompt, model, event="test", bin=getattr(cfg, "claude_bin", "claude"),
               workdir=getattr(cfg, "claude_workdir", ".") or ".", timeout=float(getattr(cfg, "claude_timeout", 90)))
    if not getattr(res, "ok", False):
        return None
    try:
        v = claude_cli.extract_json(res.text or res.raw)
    except Exception:
        return None
    return {"cause": str(v.get("cause", "")).strip()[:300] or "no cause given", "actions": list(v.get("actions") or [])}


def diagnose(ctx: dict, cfg=None, caller=None, use_claude: bool = True) -> dict:
    """→ {cause, actions (validated), source}. A Claude answer with an unknown action is rejected, never trimmed."""
    d = rules(ctx)
    if d is not None:
        return {**d, "actions": validate(d["actions"]), "source": "rules"}
    if use_claude and cfg is not None:
        c = ask_claude(ctx, cfg, caller)
        if c is not None:
            return {**c, "actions": validate(c["actions"]), "source": "claude"}
    return {"cause": "nothing recognised in the log / git / DNS / suite state — no action proposed", "actions": [], "source": "rules"}


# ----------------------------------------------------------------------------- the helper
class OpsRepair:
    def __init__(self, cfg, journal, notify, *, updater=None, agents=None, sh=subprocess.run, root: str = ROOT,
                 python: str = sys.executable, clock=_time.time, resolver=None, caller=None, restart=None, run_ps=None):
        self.cfg, self.journal, self.notify, self.updater, self.agents = cfg, journal, notify, updater, agents or {}
        self.sh, self.root, self.python, self.clock, self.resolver, self.caller = sh, root, python, clock, resolver, caller
        self.restart = restart
        self.run_ps = run_ps
        self.state = self._load()

    # -- persistence (logs/ops_repair.json)
    def _path(self):
        return os.path.join(self.cfg.log_dir, "ops_repair.json")

    def _load(self) -> dict:
        try:
            with open(self._path(), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"auto": False, "runs": [], "proposal": None}

    def _save(self):
        os.makedirs(self.cfg.log_dir, exist_ok=True)
        tmp = self._path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        os.replace(tmp, self._path())

    def _card(self, title, body, colour=None):
        try:
            self.notify.card(title, body[:3900], color=colour, footer="Aureon MT5 · repair")
        except Exception:
            pass

    # -- diagnosis
    def diagnose_now(self, trigger: str = "/repair", test_output: str = "") -> dict:
        ctx = gather(self.cfg.log_dir, updater=self.updater, sh=self.sh, root=self.root, python=self.python, resolver=self.resolver,
                     test_output=test_output)
        try:
            d = diagnose(ctx, self.cfg, self.caller, use_claude=getattr(self.cfg, "claude_mode", "off") != "off" or self.caller is not None)
        except Rejected as e:
            self.journal.decision("repair_rejected", actor="repair", rationale=str(e), trigger=trigger)
            self._card("REPAIR · rejected", str(e), 0xE74C3C)
            self.state["proposal"] = None; self._save()
            return {"cause": str(e), "actions": [], "source": "rejected"}
        self.state["proposal"] = {**d, "t": self.clock(), "trigger": trigger}; self._save()
        self.journal.decision("repair_diagnosis", actor="repair", rationale=d["cause"], actions=d["actions"], source=d["source"],
                              trigger=trigger)
        self._card("REPAIR · diagnosis", f"{d['cause']}\nactions: " + (" → ".join(d["actions"]) or "none")
                   + ("\n`/repair go` runs them" if d["actions"] else "") + f"\n(source: {d['source']} · trigger: {trigger})", 0xF1C40F)
        return d

    # -- execution
    def runs_last_hour(self) -> int:
        now = self.clock()
        self.state["runs"] = [t for t in self.state.get("runs", []) if now - float(t) < 3600]
        return len(self.state["runs"])

    def go(self, user="", actions=None) -> str:
        prop = self.state.get("proposal") or {}
        try:
            todo = validate(prop.get("actions", []) if actions is None else actions)
        except Rejected as e:
            self.journal.decision("repair_rejected", actor="repair", rationale=str(e), by=str(user))
            return f"⛔ {e}"
        if not todo:
            return "nothing to run — /repair first (the last diagnosis proposed no action)"
        if self.runs_last_hour() >= MAX_RUNS_PER_HOUR:
            return f"⛔ {MAX_RUNS_PER_HOUR} repair runs in the last hour — wait (hard limit)"
        self.state["runs"].append(self.clock()); self.state["proposal"] = None; self._save()
        done = []
        for name in todo:
            ok, what = self._do(name)
            self.journal.decision("repair_action", actor="repair", rationale=name, ok=ok, result=what[:300], by=str(user))
            done.append(f"{'✅' if ok else '❌'} {name}: {what[:200]}")
            if not ok:
                self._card("REPAIR · failed", "\n".join(done) + f"\nstopped at {name}", 0xE74C3C)
                return "REPAIR · failed\n" + "\n".join(done)
        self._card("REPAIR · done", "\n".join(done), 0x2ECC71)
        return "REPAIR · done\n" + "\n".join(done)

    def _do(self, name: str) -> tuple[bool, str]:
        return getattr(self, f"_a_{name}")()

    def _a_fetch_origin(self):
        return _run(self.sh, self.root, ["git", "fetch", "origin", "master"], 60)

    def _a_reset_to_origin(self):
        """The updater's own path (local edits → logs/local_changes_*.patch, then origin/master; .env / logs/ untouched), after
        clearing an index.lock no git process holds (older than 2 min)."""
        lock = os.path.join(self.root, ".git", "index.lock")
        if os.path.exists(lock) and self.clock() - os.path.getmtime(lock) > STALE_LOCK_S:
            os.remove(lock)
        from .common.updater import update_to_origin
        res = update_to_origin(self.cfg.log_dir, run=self.sh, cwd=self.root)
        if res["status"] in ("updated", "current"):
            return True, f"{res['status']} · HEAD {res.get('new', '')[:8]}" + (f" · patch {res['patch']}" if res.get("patch") else "")
        return False, res.get("error", "failed")

    def _a_pip_install_requirements(self):
        return _run(self.sh, self.root, [self.python, "-m", "pip", "install", "-r", "requirements.txt"], 900)

    def _a_rerun_suite(self):
        ok, out = _run(self.sh, self.root, [self.python, "-m", "pytest", "-q", "-x", "tests"], 1800)
        return ok, (out.strip().splitlines() or ["?"])[-1]

    def _a_restart_bot(self):
        from . import ops
        ops.write_restart_marker(self.cfg.log_dir, "ops-repair", "restart_bot")
        if self.restart is not None:
            self.restart()
        else:
            from .auto_update import restart
            restart(self.agents)
        return True, "restarting"

    def _a_restart_mt5_terminal(self):
        script = os.path.join(self.root, "scripts", "aureon_service.ps1")
        if self.run_ps is not None:
            return self.run_ps(f"& '{script}' -Action restart-mt5", 120)
        from .server import run_ps
        return run_ps(f"& '{script}' -Action restart-mt5", 120)

    def _a_flush_card_queue(self):
        try:
            self.notify.flush()
            return True, "card queue flushed"
        except Exception as e:
            return False, repr(e)

    # -- auto + the command
    def on_problem(self, kind: str, detail: str = ""):
        """The updater's hook. Auto only for UPDATE FAILED / UPDATE HELD — never a trade card."""
        if not self.state.get("auto") or kind not in AUTO_KINDS:
            return None
        if self.runs_last_hour() >= MAX_RUNS_PER_HOUR:
            self.journal.decision("repair_skipped", actor="repair", rationale="rate limit", trigger=kind)
            return None
        d = self.diagnose_now(trigger=f"auto · {kind}", test_output=detail)
        return self.go("auto") if d.get("actions") else None

    def command(self, arg: str = "", user="") -> str:
        a = (arg or "").strip().lower()
        if a in ("", "diagnose", "check"):
            d = self.diagnose_now(trigger=f"/repair by {user}")
            return (f"**REPAIR · diagnosis** ({d['source']})\n{d['cause']}\nactions: " + (" → ".join(d["actions"]) or "none")
                    + ("\n`/repair go` to run them" if d["actions"] else ""))
        if a == "go":
            return self.go(user)
        if a in ("auto on", "auto off"):
            self.state["auto"] = a.endswith("on"); self._save()
            self.journal.decision("repair_auto", actor="human", rationale=a, by=str(user))
            return (f"repair auto **{'on' if self.state['auto'] else 'off'}** — "
                    + ("UPDATE FAILED / UPDATE HELD now run diagnosis + go by themselves (3 runs an hour max)" if self.state["auto"]
                       else "every repair waits for /repair go"))
        if a == "status":
            p = self.state.get("proposal")
            return (f"repair auto {'on' if self.state.get('auto') else 'off'} · runs in the last hour {self.runs_last_hour()}/"
                    f"{MAX_RUNS_PER_HOUR} · pending proposal: " + (" → ".join(p['actions']) if p else "none"))
        return "⚠️ /repair · /repair go · /repair auto on|off · /repair status · /repair <n> (an incident's patch)"
