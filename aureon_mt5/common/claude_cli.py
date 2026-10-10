"""v1.10.0 — the ONLY place Aureon talks to Claude: a local `claude -p … --output-format json` subprocess.

Uses your Claude Code login (subscription). No API key, no Anthropic SDK, no HTTP calls from Aureon.
Fail closed: timeout, non-JSON, unknown/wrong decision, auth error → CliResult(ok=False) and nobody acts on it.
This module has no access to MT5 and must never import broker.py (a test enforces it)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

ENTRY_EVENTS = ("P", "CROSS", "RE", "ENTER", "ALERT")   # ENTER = ema2050 pullback-entry bar · ALERT = /alert card add-on (v2.0.0)
MAX_EVIDENCE = 12
DECISIONS = {**{e: ("TAKE", "SKIP") for e in ENTRY_EVENTS}, "pullback": ("HOLD", "TIGHTEN", "CLOSE")}
CONFIDENCE = ("low", "medium", "high")
MAX_REASON_WORDS = 20
AUTH_MARKERS = ("not logged in", "please run /login", "/login", "login required", "authenticat", "unauthorized", "401",
                "invalid api key", "credit balance", "oauth", "subscription")
# never handed to the claude subprocess: an API key would switch billing off your subscription; the rest are Aureon secrets
_DROP_PREFIXES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DISCORD_", "SUPABASE_", "AUREON_", "MT5", "METATRADER")
_DROP_WORDS = ("WEBHOOK", "PASSWORD", "PASSWD", "SECRET", "LOGIN")


@dataclass
class CliResult:
    ok: bool
    verdict: dict | None = None
    error: str = ""
    latency_s: float = 0.0
    auth_error: bool = False
    timed_out: bool = False
    raw: str = ""
    text: str = ""                       # free text result (daily review / test)


# ----------------------------------------------------------------------------- environment
def clean_env(env: dict | None = None) -> dict:
    src = dict(os.environ if env is None else env)
    out = {}
    for k, v in src.items():
        ku = k.upper()
        if ku.startswith(_DROP_PREFIXES) or any(w in ku for w in _DROP_WORDS):
            continue
        out[k] = v
    return out


# ----------------------------------------------------------------------------- parsing / validation
_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")


def extract_json(text: str) -> dict:
    t = _FENCE.sub("", (text or "").strip()).strip()
    if not t.startswith("{"):
        a, b = t.find("{"), t.rfind("}")
        if a < 0 or b <= a:
            raise ValueError("no JSON object in reply")
        t = t[a:b + 1]
    obj = json.loads(t)
    if not isinstance(obj, dict):
        raise ValueError("reply is not a JSON object")
    return obj


def validate(v: dict, event: str) -> dict:
    """Shape check against the event. Raises ValueError on anything we must not act on."""
    allowed = DECISIONS.get(event)
    if not allowed:
        raise ValueError(f"unknown event {event!r}")
    dec = str(v.get("decision", "")).strip().upper()
    if dec not in allowed:
        raise ValueError(f"decision {dec or '∅'} not valid for {event}")
    side = v.get("side")
    side = None if side in (None, "", "null", "None") else str(side).strip().upper()
    if side not in (None, "BUY", "SELL"):
        raise ValueError(f"side {side!r} invalid")
    conf = str(v.get("confidence", "")).strip().lower()
    if conf not in CONFIDENCE:
        raise ValueError(f"confidence {conf!r} invalid")
    tt = v.get("tighten_to")
    if tt in ("", "null", "None"):
        tt = None
    if tt is not None:
        try:
            tt = float(tt)
        except (TypeError, ValueError):
            raise ValueError("tighten_to not a number")
    if dec == "TIGHTEN" and tt is None:
        raise ValueError("TIGHTEN without tighten_to")
    words = str(v.get("reason", "")).split()
    ev = v.get("evidence")                                    # v2.0.0: the snapshot field names the verdict used (journaled)
    if isinstance(ev, str):
        ev = [x.strip() for x in ev.split(",")]
    evidence = [str(x)[:40] for x in ev if str(x).strip()][:MAX_EVIDENCE] if isinstance(ev, (list, tuple)) else []
    return {"decision": dec, "side": side, "tighten_to": tt if dec == "TIGHTEN" else None, "confidence": conf,
            "reason": " ".join(words[:MAX_REASON_WORDS]), "evidence": evidence}


def _is_auth(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in AUTH_MARKERS)


def parse_output(stdout: str, event: str) -> CliResult:
    """`--output-format json` → outer object → `result` → strip fences → json.loads → validate."""
    try:
        outer = json.loads(stdout)
    except Exception:
        return CliResult(False, error="CLI output is not JSON", raw=(stdout or "")[:500])
    if isinstance(outer, list):                              # stream style: take the final result message
        outer = next((m for m in reversed(outer) if isinstance(m, dict) and m.get("type") == "result"), {})
    if not isinstance(outer, dict):
        return CliResult(False, error="CLI output has no result", raw=(stdout or "")[:500])
    result = outer.get("result")
    if outer.get("is_error") or outer.get("subtype", "success") not in ("success",):
        msg = str(result or outer.get("error") or outer.get("subtype") or "error")[:200]
        return CliResult(False, error=msg, auth_error=_is_auth(msg), raw=(stdout or "")[:500])
    if not isinstance(result, str):
        return CliResult(False, error="CLI result missing", raw=(stdout or "")[:500])
    if event in ("review", "test"):
        return CliResult(True, text=result.strip(), raw=result[:500])
    try:
        return CliResult(True, verdict=validate(extract_json(result), event), raw=result[:500])
    except Exception as e:
        return CliResult(False, error=f"invalid verdict: {e}", raw=result[:500])


# ----------------------------------------------------------------------------- the call
def resolve_bin(name: str) -> str:
    return shutil.which(name) or name                        # Windows: finds claude.cmd / claude.exe


def call(prompt: str, model: str, *, event: str, bin: str = "claude", workdir: str = ".", timeout: float = 90.0,
         run=subprocess.run) -> CliResult:
    """One `claude -p` call. The prompt goes in on stdin; cwd = an empty work folder outside the repo."""
    t0 = _time.monotonic()
    cmd = [resolve_bin(bin), "-p", "--output-format", "json", "--model", model]
    try:
        os.makedirs(workdir, exist_ok=True)
        r = run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=workdir, timeout=timeout, env=clean_env())
    except subprocess.TimeoutExpired:
        return CliResult(False, error=f"timeout after {timeout:g} s", timed_out=True, latency_s=_time.monotonic() - t0)
    except FileNotFoundError:
        return CliResult(False, error=f"'{bin}' not found — install Claude Code or set AUREON_CLAUDE_BIN", auth_error=True,
                         latency_s=_time.monotonic() - t0)
    except Exception as e:
        return CliResult(False, error=f"CLI failed: {e!r}"[:200], latency_s=_time.monotonic() - t0)
    lat = _time.monotonic() - t0
    out = r.stdout or ""
    if r.returncode != 0:
        res = parse_output(out, event) if out.strip().startswith("{") else CliResult(False)
        msg = res.error or (r.stderr or out or f"exit {r.returncode}").strip()[:200]
        return CliResult(False, error=msg, auth_error=res.auth_error or _is_auth(msg) or _is_auth(r.stderr or ""), latency_s=lat,
                         raw=out[:500])
    res = parse_output(out, event)
    res.latency_s = lat
    return res


# ----------------------------------------------------------------------------- daily budget
def ist_day(now: float | None = None) -> str:
    return datetime.fromtimestamp(now if now is not None else _time.time(), tz=IST).strftime("%Y-%m-%d")


class Budget:
    """Calls per IST day across all symbols, persisted in logs/ so a restart does not reset it."""

    def __init__(self, path: str, limit: int, clock=_time.time):
        self.path, self.limit, self.clock = path, int(limit), clock
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def used(self) -> int:
        with self._lock:
            d = self._load()
            return int(d.get("calls", 0)) if d.get("day") == ist_day(self.clock()) else 0

    def take(self) -> bool:
        """Reserve one call. False when today's limit is reached (nothing is called)."""
        with self._lock:
            day = ist_day(self.clock()); d = self._load()
            n = int(d.get("calls", 0)) if d.get("day") == day else 0
            if n >= self.limit:
                return False
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"day": day, "calls": n + 1}, f)
            os.replace(tmp, self.path)
            return True
