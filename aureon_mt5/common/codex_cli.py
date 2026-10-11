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

from .claude_cli import CliResult, clean_env, validate

VERDICT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["TAKE", "SKIP"]},
        "side": {"type": ["string", "null"], "enum": ["BUY", "SELL", None]},
        "tighten_to": {"type": "null"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "reason": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "side", "tighten_to", "confidence", "reason", "evidence"]
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

def call(prompt, *, event="ALERT", binary="codex", workdir=None, timeout=90, model=None):
    started = time.monotonic()
    directory = Path(workdir or (Path.home() / "aureon_codex")).resolve()
    # Codex is never run in the repository, to prevent file modifications.
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as d:
            schema = Path(d) / "verdict.schema.json"
            output = Path(d) / "last-message.json"
            schema.write_text(json.dumps(VERDICT_SCHEMA), encoding="utf-8")
            cmd = [executable(binary), "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                   "--output-schema", str(schema), "--output-last-message", str(output)]
            if model:
                cmd.extend(["--model", model])
            cmd.append("-")
            # No Aureon/MT5/Discord credentials in child environment.
            env = clean_env()
            for k in list(env):
                if k.upper().startswith(("OPENAI_API_KEY", "CODEX_API_KEY")):
                    env.pop(k)
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", cwd=directory,
                               env=env, timeout=timeout)
            latency = time.monotonic() - started
            if r.returncode != 0 or not output.is_file():
                return CliResult(False, error=("Codex CLI exit " + str(r.returncode) + ": " +
                                 (r.stderr or "")[-160:])[:200], latency_s=latency)
            reply = json.loads(output.read_text(encoding="utf-8"))
            checked = validate(reply, event)
            return CliResult(True, verdict=checked, latency_s=latency)
    except subprocess.TimeoutExpired:
        return CliResult(False, error="Codex timed out", timed_out=True,
                         latency_s=time.monotonic() - started)
    except Exception as exc:
        return CliResult(False, error="Codex failed: " + str(exc)[:150],
                         latency_s=time.monotonic() - started)
