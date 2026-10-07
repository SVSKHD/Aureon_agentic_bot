from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv
load_dotenv()

VERSION = "1.9.0"


@dataclass
class Config:
    mode: str = "ema5080"
    symbols: list[str] = field(default_factory=lambda: [s.strip().upper() for s in os.environ.get("AUREON_SYMBOLS", "XAUUSD").split(",")])
    server_utc_offset: float = float(os.environ.get("AUREON_SERVER_OFFSET", "3"))   # fallback; measured from MT5 at start
    poll_seconds: int = 3
    bars: int = 250
    webhook: str | None = os.environ.get("DISCORD_WEBHOOK")
    bot_token: str | None = os.environ.get("DISCORD_TOKEN")
    news_file: str | None = "news_blackout.txt"
    log_dir: str = "logs"
    source: str = os.environ.get("AUREON_SOURCE", "mt5")
    enable_silver: bool = False
    dry: bool = False
