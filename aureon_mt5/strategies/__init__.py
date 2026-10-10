"""Strategy registry. Each mode is an isolated package; nothing is shared between ema5080 and ema2050 except this interface.

    get_strategy("ema5080")   -> Strategy
    available()               -> ["ema5080", "ema2050"] (installed ones)
Aliases: 5080 -> ema5080, 2050 -> ema2050.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

ALIASES = {"5080": "ema5080", "ema5080": "ema5080", "ema50/80": "ema5080",
           "2050": "ema2050", "ema2050": "ema2050", "ema20/50": "ema2050"}
DEFAULT_MODE = "ema5080"


class UnknownMode(ValueError):
    pass


@dataclass
class Guardian:
    secure_at: float
    secure_level: float
    pre_stop: float
    ride_step: float
    pre_timeout_bars: int = 12
    news_flat_min: int = 15
    ema_slow_sl_buffer: float = 0.0
    enabled: bool = True
    note: str = ""
    # ---- v2.0.0 additions. Defaults keep every ema5080 profile byte-identical (a test enforces it).
    early_at: float | None = None       # MFE at which the early lock arms (None = no early lock)
    early_level: float = 0.0            # SL = entry + early_level once early_at is seen (never loosened)
    p_phase: bool = True                # False = no P phase at all: pre-stop / timeout / separation-abort paths are skipped
    slow_ema_sl: bool = True            # post-cross follow-SL on the slow EMA and the close on a bar through it (False = off)
    exit_on_confirmed_cross: bool = False   # close the position on the next CONFIRMED opposite cross (same confirm rule)


class Strategy:
    name: str = ""
    display_name: str = ""
    fast: int = 0
    slow: int = 0

    def guardian_for(self, symbol: str) -> Guardian | None: ...
    def analyse(self, bars, symbol: str, server_offset_h: float, news, display_from=None, trade_from=None) -> dict: ...
    def add_emas(self, df): ...
    ema_cols: tuple = ("ema_fast", "ema_slow")   # column names of the fast/slow EMA in this strategy's dataframe
    day_end_exit: bool = False                    # analyse() accepts day_end= and closes open journeys there (replay)
    def render_png(self, *a, **k): ...
    def render_html(self, *a, **k): ...
    def describe(self) -> str: return self.display_name


def resolve_mode(cli: str | None) -> str:
    raw = (cli or os.environ.get("AUREON_MODE") or DEFAULT_MODE).strip().lower()
    if raw not in ALIASES:
        raise UnknownMode(raw)
    return ALIASES[raw]


def installed() -> dict[str, bool]:
    out = {}
    for name in ("ema5080", "ema2050"):
        try:
            __import__(f"aureon_mt5.strategies.{name}.strategy")
            out[name] = True
        except Exception:
            out[name] = False
    return out


def available() -> list[str]:
    return [n for n, ok in installed().items() if ok]


def get_strategy(mode: str) -> Strategy:
    name = resolve_mode(mode)
    if not installed().get(name):
        raise UnknownMode(f"{name} is not installed in this build")
    mod = __import__(f"aureon_mt5.strategies.{name}.strategy", fromlist=["STRATEGY"])
    return mod.STRATEGY


def mode_help(bad: str | None = None) -> str:
    inst = installed()
    lines = ([f"Unknown Aureon mode: {bad}", ""] if bad else []) + ["Available:"] + [f"- {n}" for n, ok in inst.items() if ok]
    missing = [n for n, ok in inst.items() if not ok]
    if missing:
        lines += ["Installed but unavailable:"] + [f"- {n}" for n in missing]
    return "\n".join(lines)
