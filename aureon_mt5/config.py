"""Aureon MT5 v1.8.3 — runtime configuration. Strategy thresholds and guardian profiles live inside each strategy package."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

VERSION = "1.8.3"


@dataclass
class Config:
    mode: str = "ema5080"
    symbols: list[str] = field(default_factory=lambda: [s.strip().upper() for s in os.environ.get("AUREON_SYMBOLS", "XAUUSD").split(",")])
    server_utc_offset: float = float(os.environ.get("AUREON_SERVER_OFFSET", "3"))   # fallback; measured from MT5 at start
    poll_seconds: int = 10
    bars: int = 500
    webhook: str | None = os.environ.get("DISCORD_WEBHOOK")
    bot_token: str | None = os.environ.get("DISCORD_TOKEN")
    channel_id: int | None = int(os.environ["DISCORD_CHANNEL"]) if os.environ.get("DISCORD_CHANNEL", "").isdigit() else None   # for TAKE/SKIP buttons
    ask_ttl_min: int = 20          # an ask expires after this many minutes
    news_file: str | None = "news_blackout.txt"
    log_dir: str = "logs"
    source: str = os.environ.get("AUREON_SOURCE", "mt5")
    enable_silver: bool = False
    dry: bool = False
