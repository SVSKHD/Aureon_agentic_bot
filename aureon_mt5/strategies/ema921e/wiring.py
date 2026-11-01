"""v3.0 ema921e — how main.py runs the mode next to the existing ones (their agents and behaviour unchanged).

    requested(cli_mode, flag, env)  → (start ema921e?, the agents' mode). ema921e is an ADD-ON runner, never a SymbolAgent:
        --ema921e  or  AUREON_EMA921E=1      ema921e next to whatever --mode / AUREON_MODE runs
        --mode ema921e  (or 921e)            the same, the agents in AUREON_E_COMPANION_MODE (default ema5080 — asks only,
                                             no automatic entries)
    Default: off (not started). Started, its NEW ENTRIES are still off until /e on (persisted in logs/ema921e_state.json).

    conflicts(cfg)   the startup warning: the agents in ema921 on XAUUSD would compete for the one-position cap
    start(...)       the runner thread (MT5Broker, or SimBroker on --dry / AUREON_PAPER=1) + its cards; a holder dict
    supervise(...)   a dead runner thread is restarted (a fresh one: warmup + reconcile adopts the open position)
"""
from __future__ import annotations

import os
import time as _time

RESPAWN_S = 60                       # a runner that keeps dying (no bars yet, MT5 down) is retried at most once a minute

E_NAMES = ("ema921e", "921e", "e921e", "e")
CONFIG_PATH = os.path.join("config", "ema921e.yaml")


def requested(cli_mode: str | None, flag: bool = False, env: dict | None = None) -> tuple[bool, str | None]:
    env = os.environ if env is None else env
    raw = (cli_mode or env.get("AUREON_MODE") or "").strip().lower()
    on = bool(flag) or env.get("AUREON_EMA921E", "0") == "1"
    if raw in E_NAMES:
        return True, (env.get("AUREON_E_COMPANION_MODE") or "ema5080")
    return on, cli_mode


def conflicts(cfg, symbol: str = "XAUUSD") -> list[str]:
    out = []
    syms = [s.upper() for s in (getattr(cfg, "symbols", None) or [])]
    if getattr(cfg, "mode", "") == "ema921" and symbol in syms:
        auto = bool(getattr(cfg, "auto_mode", False))
        out.append(f"ema921 is also running on {symbol} (auto-trade {'ON' if auto else 'off'}): the one-position-per-symbol cap "
                   f"means whichever of ema921 / ema921e holds a position refuses the other's entries (journaled + SKIP card). "
                   f"For November {symbol} belongs to ema921e — run ema921 without {symbol} (AUREON_SYMBOLS) or another --mode.")
    return out


def make_broker(cfg, symbol: str, tf: str, off: float):
    from .brokers import LiveFeed, MT5Broker, SimBroker
    if getattr(cfg, "dry", False) or getattr(cfg, "paper", False):
        b = SimBroker(symbol, LiveFeed(symbol, tf, cfg, off), cfg.log_dir)
    else:
        b = MT5Broker(symbol, tf, cfg, off)
    b.off = off
    return b


def start(cfg, journal, notify, *, news=(), health_notify=None, config_path: str = CONFIG_PATH) -> dict:
    from . import config as C
    from .cards import Cards
    from .mode import EMode
    settings, problems = C.load(config_path)
    if problems:
        (health_notify or notify).card("AUREON · MT5 · EMA921E CONFIG", "\n".join(problems)[:1900],
                                       footer=f"Aureon MT5 · {config_path} · defaults used for these keys")
        journal.log("ema921e_config_problems", problems=problems)
    holder = {"cfg": cfg, "journal": journal, "notify": notify, "news": tuple(news or ()), "config_path": config_path,
              "settings": settings, "runner": None, "restarts": 0}
    _spawn(holder)
    for w in conflicts(cfg, settings["symbol"]):
        print("⚠️  " + w)
        journal.log("ema921e_conflict_warning", text=w)
        (health_notify or notify).card("AUREON · MT5 · EMA921E · XAUUSD CONFLICT", w, footer="Aureon MT5 · ema921e")
    return holder


def _spawn(holder):
    from .cards import Cards
    from .mode import EMode
    cfg, s = holder["cfg"], holder["settings"]
    off = float(getattr(cfg, "server_utc_offset", 3.0) or 0.0)
    r = EMode(cfg, s, make_broker(cfg, s["symbol"], s["timeframe"], off), journal=holder["journal"], log_dir=cfg.log_dir,
              config_path=holder["config_path"])
    r.news = holder["news"]
    Cards(r, holder["notify"])
    r.start()
    holder["runner"] = r; holder["spawned_at"] = _time.monotonic()
    holder["journal"].log("ema921e_runner_start", broker=r.broker.name, enabled=r.enabled, restarts=holder["restarts"])
    print(f"ema921e: {s['symbol']} {s['timeframe']} · broker {r.broker.name} · lot {s['lot']:g} · "
          f"{'PAPER (demo only)' if s.get('paper', True) else 'LIVE'} · entries {'ON' if r.enabled else 'OFF (/e on)'}")
    return r


def supervise(holder: dict | None, now: float | None = None) -> bool:
    """Main loop: a runner thread that died (startup failed / an exception escaped run()) is replaced, at most once per
    RESPAWN_S. True when restarted."""
    if not holder or holder.get("runner") is None or holder["runner"].is_alive():
        return False
    now = _time.monotonic() if now is None else now
    if now - float(holder.get("spawned_at") or 0.0) < RESPAWN_S:
        return False
    holder["restarts"] += 1
    from . import config as C
    holder["settings"], _ = C.load(holder["config_path"])
    holder["notify"].send(f"ema921e:RESTART:{holder['restarts']}", "E RESTARTED · warmup + reconcile (an open position is adopted)", [])
    _spawn(holder)
    return True


def get(holder: dict | None):
    return holder.get("runner") if holder else None
