"""Aureon MT5 v2.1.0 — runtime configuration. Strategy thresholds and guardian profiles live inside each strategy package."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv
load_dotenv()   # load .env if present

VERSION = "2.7.2"


def _parse_lot_buttons(raw: str) -> list[float]:
    vals = []
    for x in raw.split(","):
        x = x.strip()
        if not x:
            continue
        f = float(x)
        if f > 0 and f not in vals:
            vals.append(f)
    return sorted(vals)[:5] or [0.5, 1.0, 1.5]


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
    guild_id: int | None = int(os.environ["DISCORD_GUILD"]) if os.environ.get("DISCORD_GUILD", "").isdigit() else None         # v2.0.2: command sync target
    ask_ttl_min: int = 20          # an ask expires after this many minutes
    risk_pct: float = float(os.environ.get("AUREON_RISK_PCT", "1.0"))   # AUREON-005: lot helper on ask cards (display only)
    heartbeat_stale_min: int = 15  # AUREON-004: red card if no closed bar processed for this long while the market is open
    daily_report_ist: str = "23:00"   # AUREON-003
    calendar_feed: bool = os.environ.get("AUREON_CALENDAR", "1") != "0"   # AUREON-006
    # AUREON-007 daily limits — enforced by the governor (v2.3.12)
    daily_loss_limit_pct: float = float(os.environ.get("AUREON_DAILY_LOSS_PCT", "2.0"))
    daily_max_losses: int = int(os.environ.get("AUREON_DAILY_MAX_LOSSES", "3"))
    max_trades_day: int = int(os.environ.get("AUREON_MAX_TRADES_DAY", "4"))
    loss_multiple: float = float(os.environ.get("AUREON_LOSS_MULTIPLE", "2.0"))
    # AUREON-009 spread / volatility warning
    spread_warn: dict = field(default_factory=lambda: {"XAU": 0.50, "XAG": 0.05})
    vol_spike_mult: float = 3.0
    # AUREON-011 one-tap order placement (OFF unless explicitly enabled; demo accounts only unless AUREON_ALLOW_LIVE=1)
    execution_enabled: bool = os.environ.get("AUREON_EXECUTION", "0") == "1"
    allow_live: bool = os.environ.get("AUREON_ALLOW_LIVE", "0") == "1"
    max_lots: float = float(os.environ.get("AUREON_MAX_LOTS", "1.0"))
    lot_buttons: list[float] = field(default_factory=lambda: _parse_lot_buttons(os.environ.get("AUREON_LOT_BUTTONS", "0.5,1.0,1.5")))
    # AUREON-012 partial close at secure (0 = off)
    partial_close_pct: float = float(os.environ.get("AUREON_PARTIAL_PCT", "0"))
    # AUREON-013 weekly store: SUPABASE_URL + SUPABASE_SERVICE_KEY in .env (read by supabase_sync.py)
    weekly_report_ist: str = os.environ.get("AUREON_WEEKLY_IST", "10:00")   # every Saturday at this IST time
    # Supabase write policy — "weekly" (default, free plan): ONE batch every Saturday (+ monthly roll-up on the first Saturday);
    # "realtime": also push signals/decisions as they happen and sync nightly. Reports are always computed locally on demand.
    supabase_mode: str = os.environ.get("AUREON_SUPABASE_MODE", "weekly")
    supabase_ping: bool = os.environ.get("AUREON_SUPABASE_PING", "0") == "1"   # minute heartbeat to Supabase — off on the free plan
    watchdog: bool = os.environ.get("AUREON_WATCHDOG", "1") == "1"
    heartbeat_hours: float = float(os.environ.get("AUREON_HEARTBEAT_HOURS", "4"))
    # v2.7.2 dead-man's switch: pinged every 60 s only while healthy (healthchecks.io or any URL); empty = off
    deadman_url: str | None = os.environ.get("AUREON_DEADMAN_URL") or None
    streak_half_after: int = int(os.environ.get("AUREON_STREAK_HALF_AFTER", "0"))
    chop_crosses: int = int(os.environ.get("AUREON_CHOP_CROSSES", "5"))
    missed_grace_min: int = int(os.environ.get("AUREON_MISSED_GRACE_MIN", "5"))
    override_window_min: int = int(os.environ.get("AUREON_OVERRIDE_WINDOW_MIN", "10"))
    override_max_drift: float = float(os.environ.get("AUREON_OVERRIDE_MAX_DRIFT", "3.0"))
    agent_default_min: int = int(os.environ.get("AUREON_AGENT_DEFAULT_MIN", "60"))
    agent_delay_s: float = float(os.environ.get("AUREON_AGENT_DELAY_S", "20"))
    daily_plan: bool = os.environ.get("AUREON_DAILY_PLAN", "1") == "1"
    daily_plan_at: str = os.environ.get("AUREON_DAILY_PLAN_AT", "05:00")
    week_goal_pts: float = float(os.environ.get("AUREON_WEEK_GOAL_PTS", "0"))
    early_lock_mode: str = os.environ.get("AUREON_EARLY_LOCK_MODE", "soft")   # soft | hard | off
    soft_fallback_min: int = int(os.environ.get("AUREON_SOFT_FALLBACK_MIN", "5"))
    # v2.7.0 guardian profile: "leg" (move-detector based) | "classic" (exact pre-v2.7.0 behaviour)
    guardian_profile: str = os.environ.get("AUREON_GUARDIAN_PROFILE", "classic")  # "auto" = leg on demo; "classic" | "leg" explicit
    guardian_profile_demo: str = os.environ.get("AUREON_GUARDIAN_PROFILE_DEMO", "leg")
    soft_giveback_pts: float = float(os.environ.get("AUREON_SOFT_GIVEBACK_PTS", "3.0"))
    soft_giveback_frac: float = float(os.environ.get("AUREON_SOFT_GIVEBACK_FRAC", "0.5"))
    soft_arm_pts: float = float(os.environ.get("AUREON_SOFT_ARM_PTS", "5"))
    ride_floor: float = float(os.environ.get("AUREON_RIDE_FLOOR", "4"))
    ride_swing_bars: int = int(os.environ.get("AUREON_RIDE_SWING_BARS", "6"))
    ride_buffer: float = float(os.environ.get("AUREON_RIDE_BUFFER", "1.0"))
    ride_atr_mult: float = float(os.environ.get("AUREON_RIDE_ATR_MULT", "3.0"))
    # your manual SL rules: if you placed an SL wider than the guardian's P-phase stop, the guardian does not close at −pre_stop
    respect_manual_sl: bool = os.environ.get("AUREON_RESPECT_MANUAL_SL", "1") == "1"
    # Codex read-only advisor (Discord on-demand; never blocks signals or guardian)
    codex_mode: str = os.environ.get("AUREON_CODEX", "off").strip().lower()
    codex_bin: str = os.environ.get("AUREON_CODEX_BIN", "codex")
    codex_model: str = os.environ.get("AUREON_CODEX_MODEL", "")
    codex_timeout: float = float(os.environ.get("AUREON_CODEX_TIMEOUT", "30"))
    codex_max_calls: int = int(os.environ.get("AUREON_CODEX_MAX_CALLS", "30"))
    codex_workdir: str = os.environ.get("AUREON_CODEX_WORKDIR", "") or os.path.join(os.path.expanduser("~"), "aureon_codex")

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
    calib_min_n: int = int(os.environ.get("AUREON_CALIB_MIN_N", "30"))
    pre_alert_2050: bool = os.environ.get("AUREON_PRE_ALERT_2050", "1") == "1"
    pwin_floor: float = float(os.environ.get("AUREON_PWIN_FLOOR", "0"))
    pwin_wait_s: float = float(os.environ.get("AUREON_PWIN_WAIT_S", "15"))
    eod_at: str = os.environ.get("AUREON_EOD_AT", "23:15")
    publish: bool = os.environ.get("AUREON_PUBLISH", "0") == "1"
    redis_url: str = os.environ.get("REDIS_URL", "")
    ctx_bars: int = int(os.environ.get("AUREON_CTX_BARS", "48"))
    ctx_max_chars: int = int(os.environ.get("AUREON_CTX_MAX_CHARS", "6000"))
    voice: bool = os.environ.get("AUREON_VOICE", "0") == "1"
    voice_chats: list[str] = field(default_factory=lambda: [c.strip() for c in os.environ.get("AUREON_VOICE_CHATS", "").split(",") if c.strip()])
    voice_secret: str = os.environ.get("AUREON_VOICE_SECRET", "")
    news_file: str | None = "news_blackout.txt"
    log_dir: str = "logs"
    source: str = os.environ.get("AUREON_SOURCE", "mt5")
    enable_silver: bool = False
    dry: bool = False
    _test_skip_session: bool = False   # set True in tests to skip governor session_window (never in production)

    def resolved_guardian_profile(self, is_demo: bool | None = None) -> str:
        """Resolve guardian profile: explicit value > auto (leg on demo, classic on live).
        is_demo=None (unknown) → classic (safe default)."""
        if self.guardian_profile not in ("auto", ""):
            return self.guardian_profile
        if is_demo is True:
            return self.guardian_profile_demo
        return "classic"
