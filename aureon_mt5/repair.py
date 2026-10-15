"""v2.10.0 — repair agent: DETECT → PROPOSE → a human APPLIES.

A live trading process must not rewrite itself and restart on its own. This module turns a detected plumbing problem into
a tested patch on a branch plus a Discord card; applying it is an admin command (/repair-apply), and ops.restart_process
does the restart. Strategy and guardian code is off-limits to the agent entirely (FORBIDDEN below): those changes go
through tuning.propose or a human, and tests/test_strategy_pins.py fails any patch that changes trading behaviour.

  Detector  — on the HealthGuard tick (no new thread): unhandled exceptions, a command failing twice in 10 min, Claude
              verdicts failing to parse >= 3/day, unreadable journal lines, chart render failures >= 3/day, a stale frame
              (>= 3 missing bars while the market is open and MT5 connected). NOT incidents: broker rejects, MT5
              disconnects, rate limits, losing trades. One open incident per fingerprint → logs/incidents.json + card.
  Repairer  — /repair n (or AUREON_REPAIR_AGENT=propose): git worktree from origin/master → REPAIR_BRIEF.md → the fixer
              (`claude -p` with Read/Edit/Write/pytest, or `codex exec`) → pytest → forbidden-path check → patch file →
              worktree reset → REPAIR card. /repair-apply n: git apply --check → apply → pytest → commit → push → restart.
"""
from __future__ import annotations

import ast
import fnmatch
import json
import os
import re
import subprocess
import sys
import time as _time
from datetime import datetime, timedelta, timezone

from . import telemetry

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICK_S = 30.0
CMD_WINDOW_S = 600
FORBIDDEN_GLOBS = ("aureon_mt5/strategies/*", "aureon_mt5/strategies/**", "aureon_mt5/broker.py", "*journeys.py",
                   "aureon_mt5/leg_guardian.py", "aureon_mt5/setup.py", ".env*", "VERSIONING.md")
GUARDIAN_FUNCS_FILE = "aureon_mt5/agent.py"
GUARDIAN_FUNC_RE = re.compile(r"^(_guard.*|_close|_move_sl)$")
PARSE_ERRORS = ("invalid verdict", "not json", "no json", "result missing", "no result")
BROKER_TITLES = ("GUARDIAN FAILURE", "PARTIAL CLOSE", "ORDER", "EXECUTION")


# ============================================================================= incident store (logs/incidents.json)
def _store_path(log_dir: str) -> str:
    return os.path.join(log_dir, "incidents.json")


def load(log_dir: str) -> dict:
    try:
        with open(_store_path(log_dir), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and "incidents" in d:
            return d
    except Exception:
        pass
    return {"next_id": 1, "incidents": []}


def save(log_dir: str, data: dict):
    os.makedirs(log_dir, exist_ok=True)
    tmp = _store_path(log_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, default=str)
    os.replace(tmp, _store_path(log_dir))


def get(log_dir: str, n: int) -> dict | None:
    return next((i for i in load(log_dir)["incidents"] if i["id"] == int(n)), None)


def update(log_dir: str, n: int, **fields) -> dict | None:
    d = load(log_dir)
    inc = next((i for i in d["incidents"] if i["id"] == int(n)), None)
    if inc is not None:
        inc.update(fields); save(log_dir, d)
    return inc


def top_frame(tb: str) -> str:
    frames = re.findall(r'File "([^"]+)", line \d+, in (\S+)', tb or "")
    if not frames:
        return "?"
    path, func = frames[-1]
    return f"{os.path.basename(path)}:{func}"


def tail(path: str, n: int = 200) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-n:]
    except OSError:
        return []


# ============================================================================= detection
class Detector:
    def __init__(self, cfg, journal, notify, agents: dict | None = None, *, on_new=None, clock=_time.time,
                 recent=None, chart_failures=None):
        self.cfg, self.journal, self.notify, self.agents = cfg, journal, notify, agents or {}
        self.on_new = on_new                              # called with a new incident when AUREON_REPAIR_AGENT=propose
        self.clock = clock
        self.recent = telemetry.RECENT if recent is None else recent
        if chart_failures is None:
            from .common import context_chart
            chart_failures = context_chart.FAILURES
        self.chart_failures = chart_failures
        self._last_tick = 0.0
        self._seen_failures = 0.0                         # telemetry records already examined (by time)
        self._journal_offset = 0
        self._bad_lines = 0
        self._last_parse_check = 0.0                      # the Claude-parse check reads today's journal: every 10 min only
        self.verifier = None                              # v2.10.1: Verifier after an auto-applied repair (set by main)

    def tick(self, now: float | None = None) -> list[dict]:
        now = self.clock() if now is None else now
        if now - self._last_tick < TICK_S:
            return []
        self._last_tick = now
        new = []
        if self.verifier is not None:
            try:
                self.verifier.tick(now)
            except Exception as e:
                telemetry.info(f"repair verifier failed: {e!r}")
        for check in (self._failures, self._claude_parse, self._journal_integrity, self._charts, self._stale_frames):
            try:
                new += [i for i in check(now) if i]
            except Exception as e:
                telemetry.info(f"repair detector {check.__name__} failed: {e!r}")
        return new

    # -- sources
    def _failures(self, now):
        out = []
        recs = [r for r in list(self.recent) if r["t"] > self._seen_failures]
        if recs:
            self._seen_failures = max(r["t"] for r in recs)
        for r in recs:
            if r.get("retcode") is not None or any(b in (r.get("title") or "") for b in BROKER_TITLES):
                continue                                   # broker rejects are the watchdog's / the operator's
            action = r.get("action") or ""
            if action.startswith("/"):
                same = [x for x in self.recent if (x.get("action") == action and now - x["t"] <= CMD_WINDOW_S)]
                if len(same) >= 2:
                    out.append(self.raise_incident(f"command {action}", "command",
                                                   f"{action} failed {len(same)}× in {CMD_WINDOW_S // 60} min: {r.get('exc')}", r.get("tb")))
                continue
            if r.get("exc_type"):
                out.append(self.raise_incident(f"{r['exc_type']} @ {top_frame(r.get('tb'))}", "exception",
                                               f"{r.get('title')} · {action} · {r.get('exc')}", r.get("tb")))
        return out

    def _today_rows(self, event_prefix: str, now):
        start = datetime.fromtimestamp(now, tz=IST).replace(hour=0, minute=0, second=0, microsecond=0)
        return [r for r in self.journal.read(int(start.timestamp())) if str(r.get("event", "")).startswith(event_prefix)]

    def _claude_parse(self, now):
        if now - self._last_parse_check < 600:
            return []
        self._last_parse_check = now
        bad = [r for r in self._today_rows("claude_verdict", now)
               if r.get("error") and any(m in str(r["error"]).lower() for m in PARSE_ERRORS) and not r.get("auth_error")]
        if len(bad) >= 3:
            return [self.raise_incident("claude verdict parse", "claude_parse",
                                        f"{len(bad)} Claude verdicts failed to parse today; last: {bad[-1]['error']}")]
        return []

    def _journal_integrity(self, now):
        path = getattr(self.journal, "path", None)
        if not path or not os.path.exists(path):
            return []
        size = os.path.getsize(path)
        if size < self._journal_offset:
            self._journal_offset = 0
        with open(path, encoding="utf-8", errors="replace") as f:
            f.seek(self._journal_offset)
            chunk = f.read()
            self._journal_offset = f.tell()
        bad = 0
        for line in chunk.splitlines():
            if not line.strip():
                continue
            try:
                json.loads(line)
            except Exception:
                bad += 1
        if bad:
            self._bad_lines += bad
            return [self.raise_incident("journal unreadable line", "journal",
                                        f"{self._bad_lines} unreadable line(s) in {os.path.basename(path)}")]
        return []

    def _charts(self, now):
        start = datetime.fromtimestamp(now, tz=IST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        n = sum(1 for t in list(self.chart_failures) if t >= start)
        if n >= 3:
            return [self.raise_incident("chart render", "chart", f"{n} chart render failures today")]
        return []

    def _stale_frames(self, now):
        out = []
        for sym, ag in list(self.agents.items()):
            snap = getattr(ag, "snapshot", None) or {}
            wall = getattr(ag, "last_bar_wall", None)
            if snap.get("market_open") and snap.get("connected") and wall and now - wall >= 4 * 300:
                out.append(self.raise_incident(f"stale frame {sym}", "stale_frame",
                                               f"{sym}: no new closed bar for {int((now - wall) // 60)} min with MT5 connected"))
        return out

    # -- one open incident per fingerprint
    def raise_incident(self, fingerprint: str, kind: str, detail: str, tb: str | None = None) -> dict | None:
        log_dir = self.cfg.log_dir
        data = load(log_dir)
        for inc in data["incidents"]:
            if inc["fingerprint"] == fingerprint and inc["status"] in ("open", "proposing", "proposed"):
                inc["count"] = inc.get("count", 1) + 1; inc["last_t"] = int(self.clock())
                save(log_dir, data)
                return None
        n = data["next_id"]; data["next_id"] = n + 1
        rows = self.journal.read(int(self.clock()) - 3600)[-30:]
        inc = {"id": n, "fingerprint": fingerprint, "kind": kind, "detail": detail[:600], "traceback": (tb or "")[-4000:],
               "log_tail": tail(os.path.join(log_dir, "aureon.log")), "journal_rows": rows, "t": int(self.clock()),
               "last_t": int(self.clock()), "count": 1, "status": "open"}
        data["incidents"].append(inc)
        save(log_dir, data)
        self.journal.log("incident", id=n, fingerprint=fingerprint, kind=kind)
        mode = getattr(self.cfg, "repair_agent", "off")
        try:
            self.notify.card(f"INCIDENT #{n} · {fingerprint}"[:256], f"{detail[:500]}\nNext: `/repair {n}` (admin) — proposes a tested patch; "
                             f"nothing is applied without `/repair-apply {n}`.", footer=f"Aureon MT5 · repair agent {mode}")
        except Exception:
            pass
        if mode in ("propose", "auto") and self.on_new is not None:
            try:
                self.on_new(inc)
            except Exception as e:
                telemetry.info(f"repair auto-propose failed: {e!r}")
        return inc


# ============================================================================= propose / apply
def fixer_prompt(inc: dict) -> str:
    return (f"You are fixing incident #{inc['id']} in Aureon (a live XAUUSD trading assistant). Read REPAIR_BRIEF.md first.\n"
            "RULES:\n"
            "- Fix the ROOT CAUSE in the smallest diff.\n"
            "- Add or extend a test under tests/ that FAILS before your fix and PASSES after.\n"
            "- Do NOT touch: aureon_mt5/strategies/**, the guardian functions in aureon_mt5/agent.py (_guard*, _close, _move_sl), "
            "aureon_mt5/broker.py, any journeys.py, aureon_mt5/leg_guardian.py, aureon_mt5/setup.py, .env*, VERSIONING.md.\n"
            "- Do NOT change any journal field name. Do NOT bump VERSION. Do NOT commit.\n"
            "- Run `python -m pytest -q tests` and make it pass (tests/test_strategy_pins.py must stay unchanged and green).\n"
            "Finish with ONE paragraph: what was wrong and what you changed.")


def brief(inc: dict) -> str:
    rows = "\n".join(json.dumps(r, default=str) for r in inc.get("journal_rows") or [])
    return (f"# REPAIR #{inc['id']} · {inc['fingerprint']}\n\nkind: {inc['kind']} · seen {inc.get('count', 1)}×\n\n## Detail\n{inc['detail']}\n\n"
            f"## Traceback\n```\n{inc.get('traceback') or '—'}\n```\n\n## logs/aureon.log (tail)\n```\n"
            + "\n".join(inc.get("log_tail") or []) + f"\n```\n\n## Journal rows\n```\n{rows}\n```\n\n## Rules\n" + fixer_prompt(inc) + "\n")


def forbidden(path: str) -> bool:
    p = path.replace("\\", "/").lstrip("./")
    return any(fnmatch.fnmatch(p, g) for g in FORBIDDEN_GLOBS)


def guardian_funcs(src: str) -> dict[str, str]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {"<unparseable>": src}
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and GUARDIAN_FUNC_RE.match(node.name):
            out[node.name] = ast.get_source_segment(src, node) or ""
    return out


def version_line(src: str) -> str:
    m = re.search(r'^VERSION\s*=.*$', src or "", re.M)
    return m.group(0) if m else ""


def pytest_summary(text: str) -> str:
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1][:200] if lines else "no output"


class Repairer:
    def __init__(self, cfg, journal, notify, *, run=subprocess.run, root: str = ROOT, python: str = sys.executable):
        self.cfg, self.journal, self.notify, self.run, self.root, self.python = cfg, journal, notify, run, root, python

    # -- helpers
    def _sh(self, cmd, cwd, timeout=120, input=None, env=None):
        try:
            r = self.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, input=input, env=env)
            return r.returncode, (r.stdout or ""), (r.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, "", f"timeout after {timeout}s"
        except Exception as e:
            return 1, "", f"{e!r}"

    def _git(self, cwd, *args, timeout=120):
        return self._sh(["git", *args], cwd, timeout)

    def _pytest(self, cwd):
        code, out, err = self._sh([self.python, "-m", "pytest", "-q", "tests"], cwd, timeout=1800)
        return code == 0, pytest_summary(out or err)

    def worktree(self, n: int) -> str:
        return os.path.join(os.path.abspath(self.cfg.log_dir), "repair", str(n))

    def patch_path(self, n: int) -> str:
        return os.path.join(os.path.abspath(self.cfg.log_dir), "repair", f"{n}.patch")

    def _fixer_cmd(self):
        if getattr(self.cfg, "repair_engine", "claude") == "codex":
            return [self.cfg.codex_bin, "exec", "--sandbox", "workspace-write", "--skip-git-repo-check", "-"]
        from .common.claude_cli import resolve_bin
        return [resolve_bin(self.cfg.claude_bin), "-p", "--output-format", "json", "--model", self.cfg.repair_model,
                "--allowedTools", "Read,Edit,Write,Bash(python -m pytest*)"]

    # -- propose
    def propose(self, n: int, *, lock=None, budget=None) -> dict:
        """Worktree → brief → fixer → tests → forbidden check → patch → reset. Never touches the live checkout."""
        inc = get(self.cfg.log_dir, n)
        if inc is None:
            return {"ok": False, "status": f"incident #{n} not found"}
        if budget is not None and not budget.take():
            return self._finish(n, {"ok": False, "status": "daily Claude budget used — no repair call made"})
        update(self.cfg.log_dir, n, status="proposing")
        wt = self.worktree(n); branch = f"repair/{n}"
        self._git(self.root, "fetch", "origin", "master")
        self._git(self.root, "worktree", "remove", "--force", wt)
        self._git(self.root, "branch", "-D", branch)
        os.makedirs(os.path.dirname(wt), exist_ok=True)
        code, _, err = self._git(self.root, "worktree", "add", wt, "-b", branch, "origin/master")
        if code != 0:
            return self._finish(n, {"ok": False, "status": f"git worktree add failed: {err[:200]}"})
        with open(os.path.join(wt, "REPAIR_BRIEF.md"), "w", encoding="utf-8") as f:
            f.write(brief(inc))
        before_ok, before = self._pytest(wt)
        _, base_agent, _ = self._git(wt, "show", f"HEAD:{GUARDIAN_FUNCS_FILE}")
        _, base_cfg, _ = self._git(wt, "show", "HEAD:aureon_mt5/config.py")
        from .common.claude_cli import clean_env
        t0 = _time.monotonic()
        if lock is not None:
            with lock:
                code, out, err = self._sh(self._fixer_cmd(), wt, timeout=float(self.cfg.repair_timeout), input=fixer_prompt(inc), env=clean_env())
        else:
            code, out, err = self._sh(self._fixer_cmd(), wt, timeout=float(self.cfg.repair_timeout), input=fixer_prompt(inc), env=clean_env())
        latency = _time.monotonic() - t0
        explanation = out
        try:
            explanation = str(json.loads(out).get("result") or out)
        except Exception:
            pass
        explanation = (explanation.strip().split("\n\n")[-1] if explanation.strip() else (err or "no explanation"))[:900]
        self._usage(code == 0, latency, err)
        if os.path.exists(os.path.join(wt, "REPAIR_BRIEF.md")):
            os.remove(os.path.join(wt, "REPAIR_BRIEF.md"))
        after_ok, after = self._pytest(wt)
        _, status, _ = self._git(wt, "status", "--porcelain")
        changed = [l[3:].strip().split(" -> ")[-1] for l in status.splitlines() if l.strip()]
        bad = [p for p in changed if forbidden(p)]
        try:
            with open(os.path.join(wt, GUARDIAN_FUNCS_FILE), encoding="utf-8") as f:
                now_agent = f.read()
            if guardian_funcs(now_agent) != guardian_funcs(base_agent):
                bad.append(f"{GUARDIAN_FUNCS_FILE} guardian functions")
            with open(os.path.join(wt, "aureon_mt5", "config.py"), encoding="utf-8") as f:
                if version_line(f.read()) != version_line(base_cfg):
                    bad.append("VERSION bumped")
        except OSError:
            pass
        self._git(wt, "add", "-A")
        _, stat, _ = self._git(wt, "diff", "--cached", "--stat")
        _, diff, _ = self._git(wt, "diff", "--cached")
        self._git(wt, "reset", "--hard")                                  # the branch stays clean; the patch is the product
        self._git(wt, "clean", "-fd")
        res = {"ok": False, "before": before, "after": after, "stat": stat.strip()[-900:], "explanation": explanation,
               "forbidden": bad, "changed": changed, "patch": None}
        if not changed:
            res["status"] = "no change proposed"
        elif bad:
            res["status"] = "REFUSED — forbidden paths: " + ", ".join(sorted(set(bad)))
        elif not after_ok:
            res["status"] = "tests failing after the fix"
        else:
            with open(self.patch_path(n), "w", encoding="utf-8") as f:
                f.write(diff if diff.endswith("\n") else diff + "\n")
            res.update(ok=True, patch=self.patch_path(n), status="ready")
        res["tests_before_ok"] = before_ok
        return self._finish(n, res)

    def _usage(self, ok, latency, err):
        try:
            from .claude_usage import append
            from .common.claude_cli import CliResult
            append(self.cfg.log_dir, "repair", CliResult(ok, error=(err or "")[:160], latency_s=latency),
                   source=("codex" if getattr(self.cfg, "repair_engine", "claude") == "codex" else "claude"))
        except Exception:
            pass

    def _finish(self, n, res: dict) -> dict:
        update(self.cfg.log_dir, n, status=("proposed" if res.get("ok") else "open"), proposal=res)
        self.journal.log("repair_proposed", id=n, status=res.get("status"), ok=bool(res.get("ok")), forbidden=res.get("forbidden") or [])
        lines = [f"tests before: {res.get('before', '—')}", f"tests after: {res.get('after', '—')}",
                 f"forbidden-path check: {'FAILED · ' + ', '.join(res['forbidden']) if res.get('forbidden') else 'clean'}"]
        if res.get("stat"):
            lines.append(f"```\n{res['stat'][-700:]}\n```")
        if res.get("explanation"):
            lines.append(res["explanation"][:700])
        lines.append(f"`/repair-apply {n}` · `/repair-discard {n}`" if res.get("ok") else f"`/repair-discard {n}` · or `/repair {n}` again")
        if res.get("patch"):
            lines.append(f"patch: `{res['patch']}`")
        try:
            self.notify.card(f"REPAIR #{n} · {res.get('status')}"[:256], "\n".join(lines)[:4000], footer="Aureon MT5 · repair agent")
        except Exception:
            pass
        return res

    # -- apply (admin; the live checkout)
    def apply(self, n: int, *, agents, user, force: bool = False, restart=None, positions_block=None, auto: bool = False) -> str:
        from . import ops
        inc = get(self.cfg.log_dir, n)
        if inc is None:
            return f"incident #{n} not found"
        prop = inc.get("proposal") or {}
        patch = prop.get("patch")
        if inc.get("status") != "proposed" or not patch or not os.path.exists(patch):
            return f"#{n} has no ready patch (status {inc.get('status')}) — run /repair {n} first"
        if (positions_block or ops.positions_block_mode_change)(agents) and not force:
            return "⛔ a position is open (or flat cannot be proven) — retry with force:true to apply and restart anyway"
        code, _, err = self._git(self.root, "apply", "--check", "--index", patch)
        if code != 0:
            return self._apply_failed(n, f"git apply --check failed: {err[:300]}")
        code, _, err = self._git(self.root, "apply", "--index", patch)
        if code != 0:
            return self._apply_failed(n, f"git apply failed: {err[:300]}")
        ok, summary = self._pytest(self.root)
        if not ok:
            self._git(self.root, "apply", "-R", "--index", patch)
            return self._apply_failed(n, f"tests failed on the live checkout — reverted: {summary}")
        code, _, err = self._git(self.root, "commit", "-m", f"repair #{n}: {inc['fingerprint']}")
        if code != 0:
            self._git(self.root, "apply", "-R", "--index", patch)
            return self._apply_failed(n, f"git commit failed — reverted: {err[:300]}")
        _, remotes, _ = self._git(self.root, "remote")
        if remotes.strip():
            code, _, err = self._git(self.root, "push", "origin", "HEAD:master", timeout=120)
            if code != 0:
                self._git(self.root, "reset", "--hard", "HEAD~1")
                return self._apply_failed(n, f"git push failed — commit reverted (auto-update would discard it): {err[:300]}")
        update(self.cfg.log_dir, n, status="applied", applied_by=str(user), applied_t=int(_time.time()), auto=bool(auto))
        self.journal.log("repair_auto_applied" if auto else "repair_applied", id=n, by=str(user), force=bool(force), tests=summary,
                         fingerprint=inc["fingerprint"])
        if auto:                                                     # the next process verifies (or rolls back) this commit
            _, sha, _ = self._git(self.root, "rev-parse", "HEAD")
            write_verify(self.cfg.log_dir, {"n": n, "fingerprint": inc["fingerprint"], "sha": sha.strip(), "pushed": bool(remotes.strip()),
                                            "t": int(_time.time())})
        update_fn = None if remotes.strip() else (lambda: {"status": "current", "discarded": []})
        (restart or ops.restart_process)(agents, self.journal, self.notify, user, f"repair #{n}", log_dir=self.cfg.log_dir,
                                         **({"update": update_fn} if update_fn else {}),
                                         **({"marker_kind": "repair-auto"} if auto else {}))
        return f"✅ repair #{n} applied · {summary} · restarting"

    def _apply_failed(self, n, why: str) -> str:
        self.journal.log("repair_apply_failed", id=n, error=why[:300])
        try:
            self.notify.card(f"REPAIR #{n} · APPLY FAILED", f"{why}\nIncident stays open; the live checkout is unchanged.",
                             footer="Aureon MT5 · repair agent")
        except Exception:
            pass
        return f"⚠️ {why}"

    def discard(self, n: int, user="") -> str:
        inc = get(self.cfg.log_dir, n)
        if inc is None:
            return f"incident #{n} not found"
        self._git(self.root, "worktree", "remove", "--force", self.worktree(n))
        self._git(self.root, "branch", "-D", f"repair/{n}")
        try:
            os.remove(self.patch_path(n))
        except OSError:
            pass
        update(self.cfg.log_dir, n, status="discarded", discarded_by=str(user))
        self.journal.log("repair_discarded", id=n, by=str(user))
        return f"🗑 incident #{n} closed · worktree and patch removed"


def listing(log_dir: str) -> str:
    rows = [i for i in load(log_dir)["incidents"] if i["status"] not in ("discarded", "applied")]
    if not rows:
        return "no open incidents"
    return "\n".join(f"#{i['id']} · {i['status']} · {i['fingerprint']} · seen {i.get('count', 1)}× · "
                     f"{datetime.fromtimestamp(i['t'], tz=IST):%d %b %H:%M} IST"
                     + (f" · patch ready" if (i.get('proposal') or {}).get('ok') else "") for i in rows)[:1900]


# ============================================================================= v2.10.1 auto tier (AUREON_REPAIR_AGENT=auto)
AUTO_UNHEALTHY_KINDS = ("journal",)                 # the bot is already unhealthy: may auto-apply inside the session
AUTO_UNHEALTHY_WORDS = ("heartbeat", "gateway", "journal")


def _verify_path(log_dir: str) -> str:
    return os.path.join(log_dir, "repair_verify.json")


def write_verify(log_dir: str, data: dict):
    os.makedirs(log_dir, exist_ok=True)
    with open(_verify_path(log_dir), "w", encoding="utf-8") as f:
        json.dump(data, f)


def read_verify(log_dir: str) -> dict | None:
    try:
        with open(_verify_path(log_dir), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def clear_verify(log_dir: str):
    try:
        os.remove(_verify_path(log_dir))
    except OSError:
        pass


def in_session(cfg, now: datetime | None = None) -> bool:
    """The governor's entry window (AUREON_SESSION_IST, default 05:30–23:00 IST)."""
    now = now or datetime.now(IST)
    try:
        a, b = cfg.session_ist.split("-")
        ah, am = (int(x) for x in a.split(":")); bh, bm = (int(x) for x in b.split(":"))
    except Exception:
        ah, am, bh, bm = 5, 30, 23, 0
    m = now.hour * 60 + now.minute
    return ah * 60 + am <= m < bh * 60 + bm


def auto_blocked(log_dir: str) -> set[str]:
    return set(load(log_dir).get("auto_blocked") or [])


def auto_guards(repairer, n: int, agents, *, now: datetime | None = None, positions_block=None) -> list[str]:
    """Every guard that FAILS for auto-applying incident n (empty = apply). Forbidden paths are refused before a patch is
    ever proposed, so a proposal that exists is plumbing-only by construction."""
    from . import ops
    cfg = repairer.cfg
    inc = get(cfg.log_dir, n) or {}
    prop = inc.get("proposal") or {}
    fails = []
    if not prop.get("ok") or prop.get("forbidden"):
        fails.append("no clean patch (forbidden paths or failing tests)")
    if inc.get("fingerprint") in auto_blocked(cfg.log_dir):
        fails.append("fingerprint rolled back before — needs a human")
    if (positions_block or ops.positions_block_mode_change)(agents):
        fails.append("a position is open (or flat cannot be proven)")
    unhealthy = inc.get("kind") in AUTO_UNHEALTHY_KINDS or any(w in (inc.get("fingerprint") or "").lower() for w in AUTO_UNHEALTHY_WORDS)
    if in_session(cfg, now) and not unhealthy:
        fails.append(f"inside the trading session ({cfg.session_ist} IST)")
    start = (now or datetime.now(IST)).replace(hour=0, minute=0, second=0, microsecond=0)
    done = [r for r in repairer.journal.read(int(start.timestamp())) if r.get("event") == "repair_auto_applied"]
    if len(done) >= int(cfg.repair_auto_per_day):
        fails.append(f"daily cap reached ({len(done)}/{cfg.repair_auto_per_day})")
    return fails


def maybe_auto_apply(repairer, n: int, agents, *, now=None, positions_block=None, restart=None) -> str:
    """AUREON_REPAIR_AGENT=auto: after a proposal, apply it only when every guard holds; otherwise it stays a proposal and the
    card names the guard. The live suite runs again after git apply (apply reverts on failure)."""
    if getattr(repairer.cfg, "repair_agent", "off") != "auto":
        return "not auto"
    fails = auto_guards(repairer, n, agents, now=now, positions_block=positions_block)
    if fails:
        update(repairer.cfg.log_dir, n, auto_held=fails)
        repairer.journal.log("repair_auto_held", id=n, guards=fails)
        try:
            repairer.notify.card(f"REPAIR #{n} · AUTO-APPLY HELD", "stays a proposal — " + "; ".join(fails)
                                 + f"\n`/repair-apply {n}` to apply by hand", footer="Aureon MT5 · repair agent auto")
        except Exception:
            pass
        return "held: " + "; ".join(fails)
    inc = get(repairer.cfg.log_dir, n) or {}
    try:
        repairer.notify.card(f"REPAIR AUTO-APPLY #{n}", f"{inc.get('fingerprint')} — patch applies, live suite runs, then restart. "
                             f"Verified after {int(repairer.cfg.repair_verify_s)} s of healthy running, else rolled back.",
                             footer="Aureon MT5 · repair agent auto")
    except Exception:
        pass
    return repairer.apply(n, agents=agents, user="repair-agent", restart=restart, positions_block=positions_block, auto=True)


class Verifier:
    """On the process that started after an auto-apply: REPAIR VERIFIED after verify_s of healthy running with no new
    incident of the same fingerprint; otherwise git revert + push + restart on the previous code (exactly once),
    REPAIR ROLLED BACK, incident needs_human, fingerprint never auto-applied again."""

    def __init__(self, cfg, journal, notify, *, health_ok=lambda: True, run=subprocess.run, root: str = ROOT, clock=_time.time,
                 restart=None, agents=None):
        self.cfg, self.journal, self.notify, self.health_ok, self.clock = cfg, journal, notify, health_ok, clock
        self.repairer = Repairer(cfg, journal, notify, run=run, root=root)
        self.restart, self.agents = restart, agents or {}
        self.boot = clock()
        self.done = False

    def tick(self, now: float | None = None) -> str | None:
        if self.done:
            return None
        v = read_verify(self.cfg.log_dir)
        if not v:
            self.done = True
            return None
        now = self.clock() if now is None else now
        same = [i for i in load(self.cfg.log_dir)["incidents"]
                if i["fingerprint"] == v["fingerprint"] and i["id"] != v["n"] and i["t"] >= self.boot]
        if same:
            return self.rollback(v, f"incident {v['fingerprint']} came back (#{same[-1]['id']})")
        if now - self.boot < float(self.cfg.repair_verify_s):
            return None
        if not self.health_ok():
            return self.rollback(v, "unhealthy after the verify window (heartbeat / gateway / agents)")
        clear_verify(self.cfg.log_dir); self.done = True
        update(self.cfg.log_dir, v["n"], status="verified", verified_t=int(now))
        self.journal.log("repair_verified", id=v["n"], fingerprint=v["fingerprint"], sha=v.get("sha"))
        try:
            self.notify.card(f"REPAIR VERIFIED #{v['n']}", f"{v['fingerprint']} · {int(now - self.boot)} s healthy, no recurrence.",
                             footer="Aureon MT5 · repair agent auto")
        except Exception:
            pass
        return "verified"

    def rollback(self, v: dict, why: str) -> str:
        from . import ops
        clear_verify(self.cfg.log_dir); self.done = True              # exactly once, whatever happens next
        rp = self.repairer
        code, _, err = rp._git(rp.root, "revert", "--no-edit", v.get("sha") or "HEAD")
        if code == 0 and v.get("pushed"):
            rp._git(rp.root, "push", "origin", "HEAD:master", timeout=120)
        data = load(self.cfg.log_dir)
        data["auto_blocked"] = sorted(set(data.get("auto_blocked") or []) | {v["fingerprint"]})
        for i in data["incidents"]:
            if i["id"] == v["n"]:
                i["status"] = "needs_human"
        save(self.cfg.log_dir, data)
        self.journal.log("repair_rolled_back", id=v["n"], fingerprint=v["fingerprint"], why=why, revert_ok=code == 0)
        try:
            self.notify.card(f"REPAIR ROLLED BACK #{v['n']}", f"{why}\n" + ("reverted the repair commit — restarting on the previous code"
                             if code == 0 else f"git revert failed: {err[:200]} — restarting anyway; needs a human"),
                             footer="Aureon MT5 · repair agent auto")
        except Exception:
            pass
        (self.restart or ops.restart_process)(self.agents, self.journal, self.notify, "repair-agent", f"rollback #{v['n']}",
                                              log_dir=self.cfg.log_dir, update=lambda: {"status": "current", "discarded": []},
                                              marker_kind="repair-rollback")
        return "rolled back"
