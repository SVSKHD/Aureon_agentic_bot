"""v3.0 ema921e — config/ema921e.yaml: load, validate, hot reload, /e set.

    load(path)              → (settings, problems). A bad / missing value falls back to DEFAULTS (loudly, in problems).
    ConfigWatcher(path)     .poll() → new settings when the file's mtime changed and it still validates, else None.
    set_key(path, k, v)     /e set: parse + validate one key, rewrite only that key's line (the comments stay), return the
                            new value. symbol / timeframe need a restart; paper flips only through /e live · /e demo.
"""
from __future__ import annotations

import os
import re

import yaml

DEFAULT_PATH = os.path.join("config", "ema921e.yaml")
DEFAULTS = {"symbol": "XAUUSD", "timeframe": "M30", "lot": 0.3, "spike": 0.15, "no_monday": True, "hours": [0, 24],
            "atr_gate": 0.7, "month_stop_per_lot": 2000.0, "cut_bars": 2, "ladder": [[0.25, 0.05], [0.5, 0.25], [1.0, 0.5]],
            "step": 0.5, "stop_atr": 3.0, "news_pause_minutes": 30, "paper": True}
RESTART_ONLY = ("symbol", "timeframe")
VIA_COMMAND = {"paper": "use /e live or /e demo"}
MAX_LOT = 5.0


class ConfigError(ValueError):
    pass


def _bool(v):
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"not a boolean: {v!r}")


def _num(lo=None, hi=None, kind=float):
    def f(v):
        try:
            x = kind(v)
        except (TypeError, ValueError):
            raise ConfigError(f"not a number: {v!r}")
        if kind is int and float(v) != x:
            raise ConfigError(f"not an integer: {v!r}")
        if (lo is not None and x < lo) or (hi is not None and x > hi):
            raise ConfigError(f"{x} outside [{lo}, {hi}]")
        return x
    return f


def _hours(v):
    if isinstance(v, str):
        v = [p for p in re.split(r"[\s,\-:]+", v.strip("[] ")) if p]
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise ConfigError(f"hours needs [from, to]: {v!r}")
    a, b = int(v[0]), int(v[1])
    if not (0 <= a < b <= 24):
        raise ConfigError(f"hours must be 0 ≤ from < to ≤ 24: {v!r}")
    return [a, b]


def _ladder(v):
    if isinstance(v, str):
        v = yaml.safe_load(v) if v.strip().startswith("[") else [p.split(":") for p in v.split(",")]   # "0.25:0.05,0.5:0.25"
    try:
        lad = [[float(t), float(l)] for t, l in v]
    except (TypeError, ValueError):
        raise ConfigError(f"ladder needs [[trigger, lock], …]: {v!r}")
    if not lad or any(l < 0 or l >= t for t, l in lad) or [t for t, _ in lad] != sorted({t for t, _ in lad}) \
            or [l for _, l in lad] != sorted(l for _, l in lad):
        raise ConfigError(f"ladder: triggers rising, locks rising, 0 ≤ lock < trigger: {v!r}")
    return lad


def _symbol(v):
    s = str(v).strip().upper()
    if s != "XAUUSD":
        raise ConfigError("v3.0 runs XAUUSD only")
    return s


def _tf(v):
    s = str(v).strip().upper()
    if s != "M30":
        raise ConfigError("v3.0 runs M30 only (the validated timeframe)")
    return s


PARSERS = {"symbol": _symbol, "timeframe": _tf, "lot": _num(0.01, MAX_LOT), "spike": _num(0, 5), "no_monday": _bool,
           "hours": _hours, "atr_gate": _num(0, 5), "month_stop_per_lot": _num(0, 1e7), "cut_bars": _num(1, 50, int),
           "ladder": _ladder, "step": _num(0, 10), "stop_atr": _num(0, 20), "news_pause_minutes": _num(0, 600, int),
           "paper": _bool}


def validate(raw: dict) -> tuple[dict, list[str]]:
    out, problems = {}, []
    raw = raw if isinstance(raw, dict) else {}
    for k, d in DEFAULTS.items():
        if k not in raw:
            out[k] = d; problems.append(f"{k}: missing → default {d!r}"); continue
        try:
            out[k] = PARSERS[k](raw[k])
        except ConfigError as e:
            out[k] = d; problems.append(f"{k}: {e} → default {d!r}")
    for k in raw:
        if k not in DEFAULTS:
            problems.append(f"{k}: unknown key, ignored")
    return out, problems


def load(path: str = DEFAULT_PATH) -> tuple[dict, list[str]]:
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return dict(DEFAULTS), [f"{path} missing → defaults"]
    except yaml.YAMLError as e:
        return dict(DEFAULTS), [f"{path} unreadable ({e}) → defaults"]
    return validate(raw)


class ConfigWatcher:
    """Hot reload: poll() returns the new settings when the file changed on disk and parses cleanly; a broken edit is
    reported once (last_problems) and the running settings are kept."""

    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path; self.mtime = self._mtime(); self.last_problems: list[str] = []

    def _mtime(self):
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return None

    def poll(self) -> dict | None:
        m = self._mtime()
        if m == self.mtime:
            return None
        self.mtime = m
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            self.last_problems = [f"reload failed: {e} — running settings kept"]; return None
        new, problems = validate(raw)
        bad = [p for p in problems if "unknown key" not in p]
        if bad:
            self.last_problems = [f"reload refused — {p}" for p in bad] + ["running settings kept"]; return None
        self.last_problems = problems
        return new


def _yaml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return yaml.safe_dump(v, default_flow_style=True).strip()
    return f"{v:g}" if isinstance(v, float) else str(v)


def parse_value(key: str, value):
    key = key.strip().lower()
    if key not in PARSERS:
        raise ConfigError(f"unknown key {key!r} — keys: {', '.join(DEFAULTS)}")
    if key in RESTART_ONLY:
        raise ConfigError(f"{key} needs a restart (edit {DEFAULT_PATH})")
    if isinstance(value, str) and key not in ("hours", "ladder"):
        try:
            value = yaml.safe_load(value)
        except yaml.YAMLError:
            pass
    return key, PARSERS[key](value)


def set_key(path: str, key: str, value, *, allow_command_keys: bool = False):
    """Validate and write one key in place. Returns (key, parsed value). Raises ConfigError."""
    key = key.strip().lower()
    if key in VIA_COMMAND and not allow_command_keys:
        raise ConfigError(f"{key}: {VIA_COMMAND[key]}")
    key, v = parse_value(key, value)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = [f"{k}: {_yaml_value(d)}" for k, d in DEFAULTS.items()]
    pat = re.compile(rf"^{re.escape(key)}\s*:\s*(.*?)(\s+#.*)?$")
    for i, ln in enumerate(lines):
        m = pat.match(ln)
        if m:
            new, comment = f"{key}: {_yaml_value(v)}", (m.group(2) or "").strip()
            lines[i] = f"{new:<29} {comment}" if comment else new
            break
    else:
        lines.append(f"{key}: {_yaml_value(v)}")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return key, v
