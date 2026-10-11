"""Read-only Codex CLI bridge. Never imports or calls MT5.
All invocations run outside the trading poll/guardian threads.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .claude_cli import CliResult, _is_auth, clean_env, validate

VERDICT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["TAKE", "SKIP"]},
        "side": {"type": ["string", "null"], "enum": ["BUY", "SELL", None]},
        "tighten_to": {"type": "null"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "reason": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "p_win": {"type": ["number", "null"]},
    },
    "required": ["decision", "side", "tighten_to", "confidence", "reason", "evidence", "p_win"]
}

def executable(binary="codex"):
    return shutil.which(binary) or binary

def version(binary="codex", timeout=10):
    try:
        r = subprocess.run([executable(binary), "--version"], capture_output=True,
                           text=True, timeout=timeout, env=clean_env())
        return (r.stdout or r.stderr).strip()[:120] if r.returncode == 0 else "unavailable"
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"

def login_status(binary="codex", timeout=10):
    try:
        r = subprocess.run([executable(binary), "login", "status"], capture_output=True,
                           text=True, timeout=timeout, env=clean_env())
        return "authenticated" if r.returncode == 0 else "not authenticated / unavailable"
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"

PULLBACK_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["HOLD", "TIGHTEN", "CLOSE"]},
        "side": {"type": ["string", "null"], "enum": ["BUY", "SELL", None]},
        "tighten_to": {"type": ["number", "null"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "reason": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "p_win": {"type": ["number", "null"]},
    },
    "required": ["decision", "side", "tighten_to", "confidence", "reason", "evidence", "p_win"]
}
USAGE_KEYS = {"input_tokens": "input_tokens", "output_tokens": "output_tokens",
              "cached_input_tokens": "cache_read_input_tokens", "cache_read_input_tokens": "cache_read_input_tokens"}


def schema_for(event: str) -> dict:
    """v2.3.0: the same reply contract as Claude, per event: pullback → HOLD/TIGHTEN/CLOSE, everything else → TAKE/SKIP."""
    return PULLBACK_SCHEMA if event == "pullback" else VERDICT_SCHEMA


def parse_usage(stdout: str) -> tuple[dict | None, float | None, str]:
    """`codex exec --json` prints one JSON event per line; the token counts sit in a `usage` dict (turn.completed / token_count).
    → (usage in the ledger's key names or None, CLI-reported cost or None, reported model or ''). Never raises."""
    usage, cost, model = None, None, ""
    def walk(o):
        nonlocal usage, cost, model
        if isinstance(o, dict):
            u = o.get("usage")
            if isinstance(u, dict) and any(k in u for k in USAGE_KEYS):
                usage = {USAGE_KEYS[k]: int(v or 0) for k, v in u.items() if k in USAGE_KEYS and isinstance(v, (int, float))}
            if isinstance(o.get("total_cost_usd"), (int, float)):
                cost = float(o["total_cost_usd"])
            if isinstance(o.get("model"), str) and o["model"]:
                model = o["model"]
            for v in o.values():
                if isinstance(v, (dict, list)): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            walk(json.loads(line))
        except ValueError:
            continue
    return usage, cost, model


def call(prompt, *, event="ALERT", binary="codex", workdir=None, timeout=90, model=None, free_text=False, run=None):
    """One `codex exec` (read-only sandbox, ChatGPT/CLI login only — OPENAI_API_KEY / CODEX_API_KEY are stripped).
    free_text=True (daily / Saturday review): no schema, the last message is returned as `text`."""
    run = run or subprocess.run
    started = time.monotonic()
    directory = Path(workdir or (Path.home() / "aureon_codex")).resolve()
    # Codex is never run in the repository, to prevent file modifications.
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as d:
            schema = Path(d) / "verdict.schema.json"
            output = Path(d) / "last-message.json"
            cmd = [executable(binary), "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--json",
                   "--output-last-message", str(output)]
            if not free_text:
                schema.write_text(json.dumps(schema_for(event)), encoding="utf-8")
                cmd[cmd.index("--json"):cmd.index("--json")] = ["--output-schema", str(schema)]
            if model:
                cmd.extend(["--model", model])
            cmd.append("-")
            # No Aureon/MT5/Discord credentials and no API key in the child environment: CLI login only.
            env = clean_env()
            for k in list(env):
                if k.upper().startswith(("OPENAI_API_KEY", "CODEX_API_KEY")):
                    env.pop(k)
            kw = dict(input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=directory,
                      env=env, timeout=timeout)
            r = run(cmd, **kw)
            if r.returncode != 0 and "--json" in (r.stderr or "") and not output.is_file():   # older CLI without --json
                cmd.remove("--json"); r = run(cmd, **kw)
            latency = time.monotonic() - started
            usage, cost, reported = parse_usage(r.stdout or "")
            if r.returncode != 0 or not output.is_file():
                err = ("Codex CLI exit " + str(r.returncode) + ": " + (r.stderr or "")[-160:])[:200]
                return CliResult(False, error=err, latency_s=latency, auth_error=_is_auth(r.stderr or ""),
                                 usage=usage, total_cost_usd=cost, model=reported)
            body = output.read_text(encoding="utf-8")
            if free_text:
                return CliResult(bool(body.strip()), text=body.strip(), raw=body[:500], latency_s=latency,
                                 error="" if body.strip() else "empty reply", usage=usage, total_cost_usd=cost, model=reported)
            try:
                checked = validate(json.loads(body), event)
            except Exception as exc:                          # fail closed: nothing is shown as a verdict
                return CliResult(False, error=f"invalid verdict: {exc}"[:200], raw=body[:500], latency_s=latency,
                                 usage=usage, total_cost_usd=cost, model=reported)
            return CliResult(True, verdict=checked, raw=body[:500], latency_s=latency, usage=usage, total_cost_usd=cost,
                             model=reported)
    except subprocess.TimeoutExpired:
        return CliResult(False, error="Codex timed out", timed_out=True,
                         latency_s=time.monotonic() - started)
    except FileNotFoundError:
        return CliResult(False, error=f"'{binary}' not found — install the Codex CLI or set AUREON_CODEX_BIN", auth_error=True,
                         latency_s=time.monotonic() - started)
    except Exception as exc:
        return CliResult(False, error="Codex failed: " + str(exc)[:150],
                         latency_s=time.monotonic() - started)
