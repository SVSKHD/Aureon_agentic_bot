"""Aureon MT5 v1.11.0 — runtime configuration. Strategy thresholds and guardian profiles live inside each strategy package."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv
load_dotenv()   # load .env if present

VERSION = "1.11.0"


@dataclass
class Config:
    mode: str = "ema5080"
    symbols: list[str] = field(default_factory=lambda: [s.strip().upper() for s in os.environ.get("AUREON_SYMBOLS", "XAUUSD").split(",")])
    server_utc_offset: float = float(os.environ.get("AUREON_SERVER_OFFSET", "3"))   # fallback; measured from MT5 at start
    poll_seconds: int = 10
    bars: int = 500
    webhook: str | None = os.environ.get("DISCORD_WEBHOOK")
    bot_token: str | None = os.environ.get("DISCORD_TOKEN")
    health_webhook: str | None = os.environ.get("DISCORD_HEALTH_WEBHOOK") or None   # bot/Claude health cards; falls back to DISCORD_WEBHOOK
    channel_id: int | None = int(os.environ["DISCORD_CHANNEL"]) if os.environ.get("DISCORD_CHANNEL", "").isdigit() else None   # for TAKE/SKIP buttons
    ask_ttl_min: int = 20          # an ask expires after this many minutes
    risk_pct: float = float(os.environ.get("AUREON_RISK_PCT", "1.0"))   # AUREON-005: lot helper on ask cards (display only)
    heartbeat_stale_min: int = 15  # AUREON-004: red card if no closed bar processed for this long while the market is open
    daily_report_ist: str = "23:00"   # AUREON-003
    calendar_feed: bool = os.environ.get("AUREON_CALENDAR", "1") != "0"   # AUREON-006
    # AUREON-007 daily limits (advisory: cards say 'sit out', nothing is blocked)
    daily_loss_limit_pct: float = float(os.environ.get("AUREON_DAILY_LOSS_PCT", "2.0"))
    daily_max_losses: int = int(os.environ.get("AUREON_DAILY_MAX_LOSSES", "3"))
    # AUREON-009 spread / volatility warning
    spread_warn: dict = field(default_factory=lambda: {"XAU": 0.50, "XAG": 0.05})
    vol_spike_mult: float = 3.0
    # AUREON-011 one-tap order placement (OFF unless explicitly enabled; demo accounts only unless AUREON_ALLOW_LIVE=1)
    execution_enabled: bool = os.environ.get("AUREON_EXECUTION", "0") == "1"
    allow_live: bool = os.environ.get("AUREON_ALLOW_LIVE", "0") == "1"
    max_lots: float = float(os.environ.get("AUREON_MAX_LOTS", "1.0"))
    # AUREON-012 partial close at secure (0 = off)
    partial_close_pct: float = float(os.environ.get("AUREON_PARTIAL_PCT", "0"))
    # AUREON-013 weekly store: SUPABASE_URL + SUPABASE_SERVICE_KEY in .env (read by supabase_sync.py)
    weekly_report_ist: str = os.environ.get("AUREON_WEEKLY_IST", "10:00")   # every Saturday at this IST time
    # Supabase write policy — "weekly" (default, free plan): ONE batch every Saturday (+ monthly roll-up on the first Saturday);
    # "realtime": also push signals/decisions as they happen and sync nightly. Reports are always computed locally on demand.
    supabase_mode: str = os.environ.get("AUREON_SUPABASE_MODE", "weekly")
    watchdog_ping: bool = os.environ.get("AUREON_WATCHDOG", "0") == "1"     # minute heartbeat to Supabase — off on the free plan
    # your manual SL rules: if you placed an SL wider than the guardian's P-phase stop, the guardian does not close at −pre_stop
    respect_manual_sl: bool = os.environ.get("AUREON_RESPECT_MANUAL_SL", "1") == "1"
    # v1.10.0 Claude add-on (local `claude -p`, your Claude Code login — no API key, no SDK). off | review | advisory | manage
    claude_mode: str = os.environ.get("AUREON_CLAUDE", "off").strip().lower()
    claude_bin: str = os.environ.get("AUREON_CLAUDE_BIN", "claude")
    claude_entry_model: str = os.environ.get("AUREON_CLAUDE_ENTRY_MODEL", "opus")
    claude_pullback_model: str = os.environ.get("AUREON_CLAUDE_PULLBACK_MODEL", "sonnet")
    claude_review_model: str = os.environ.get("AUREON_CLAUDE_REVIEW_MODEL", "haiku")
    claude_max_calls: int = int(os.environ.get("AUREON_CLAUDE_MAX_CALLS", "30"))      # per IST day, all symbols
    claude_timeout: float = float(os.environ.get("AUREON_CLAUDE_TIMEOUT", "90"))      # seconds per call
    claude_workdir: str = os.environ.get("AUREON_CLAUDE_WORKDIR", "") or os.path.join(os.path.expanduser("~"), "aureon_claude")
    # v1.11.0 weekly compare: optional Claude self-review of its wrong calls after the Saturday COMPARE card (counts toward the budget)
    compare_claude_review: bool = os.environ.get("AUREON_COMPARE_CLAUDE_REVIEW", "0") == "1"
    news_file: str | None = "news_blackout.txt"
    log_dir: str = "logs"
    source: str = os.environ.get("AUREON_SOURCE", "mt5")
    enable_silver: bool = False
    dry: bool = False
