"""Aureon MT5 v2.1.0 — runtime configuration. Strategy thresholds and guardian profiles live inside each strategy package."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv
load_dotenv()   # load .env if present

VERSION = "2.17.0"


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



# v2.10.1: an unparsable number in .env never crashes the import — the default is used and the problem is recorded for
# validate() (BOOT CHECK card, /env-check).
PARSE_PROBLEMS: list[dict] = []


def _num(env: str, default: str, kind=float):
    raw = os.environ.get(env, default)
    try:
        if kind is int:
            v = float(raw)
            if v != int(v):
                raise ValueError(raw)
            return int(v)
        return float(raw)
    except (TypeError, ValueError):
        PARSE_PROBLEMS.append({"name": env.lower().replace("aureon_", ""), "env": env, "value": raw,
                               "issue": f"not a {'number' if kind is float else 'whole number'}", "fallback": default})
        return kind(default)


def _event_set(env: str, default: str) -> tuple:
    """v2.11.0 comma list → upper-case tuple; off / none / - → ()."""
    raw = os.environ.get(env, default)
    return tuple(dict.fromkeys(x.strip().upper() for x in raw.replace(" ", ",").split(",")
                               if x.strip() and x.strip().upper() not in ("OFF", "NONE", "-")))


@dataclass
class Config:
    mode: str = "ema5080"
    symbols: list[str] = field(default_factory=lambda: [s.strip().upper() for s in os.environ.get("AUREON_SYMBOLS", "XAUUSD").split(",")])
    server_utc_offset: float = _num("AUREON_SERVER_OFFSET", "3", float)   # fallback; measured from MT5 at start
    poll_seconds: int = 10
    bars: int = 500
    webhook: str | None = os.environ.get("DISCORD_WEBHOOK")
    bot_token: str | None = os.environ.get("DISCORD_TOKEN")
    health_webhook: str | None = os.environ.get("DISCORD_HEALTH_WEBHOOK") or None   # bot/Claude health cards; falls back to DISCORD_WEBHOOK
    channel_id: int | None = int(os.environ["DISCORD_CHANNEL"]) if os.environ.get("DISCORD_CHANNEL", "").isdigit() else None   # for TAKE/SKIP buttons
    guild_id: int | None = int(os.environ["DISCORD_GUILD"]) if os.environ.get("DISCORD_GUILD", "").isdigit() else None         # v2.0.2: command sync target
    ask_ttl_min: int = _num("AUREON_ASK_TTL_MIN", "20", int)   # an ask expires after this many minutes (wall clock; M30: 30)
    risk_pct: float = _num("AUREON_RISK_PCT", "1.0", float)   # AUREON-005: lot helper on ask cards (display only)
    heartbeat_stale_min: int = 15  # AUREON-004: red card if no closed bar processed for this long while the market is open
    daily_report_ist: str = "23:00"   # AUREON-003
    calendar_feed: bool = os.environ.get("AUREON_CALENDAR", "1") != "0"   # AUREON-006
    # AUREON-007 daily limits — enforced by the governor (v2.3.12)
    daily_loss_limit_pct: float = _num("AUREON_DAILY_LOSS_PCT", "2.0", float)
    daily_max_losses: int = _num("AUREON_DAILY_MAX_LOSSES", "3", int)
    max_trades_day: int = _num("AUREON_MAX_TRADES_DAY", "4", int)
    loss_multiple: float = _num("AUREON_LOSS_MULTIPLE", "2.0", float)
    # AUREON-009 spread / volatility warning
    spread_warn: dict = field(default_factory=lambda: {"XAU": 0.50, "XAG": 0.05})
    vol_spike_mult: float = 3.0
    # AUREON-011 one-tap order placement (OFF unless explicitly enabled; demo accounts only unless AUREON_ALLOW_LIVE=1)
    execution_enabled: bool = os.environ.get("AUREON_EXECUTION", "0") == "1"
    auto_mode: bool = os.environ.get("AUREON_AUTO", "0") == "1"                 # v2.16.0: ema921 places orders itself (needs execution too)
    auto_trade: bool = os.environ.get("AUREON_AUTO", "0") == "1"                # v2.17.0: the live switch (/auto-trade); auto_mode = the boot default
    paper: bool = os.environ.get("AUREON_PAPER", "0") == "1"                    # v2.16.0: real feed, real cards, fills only journaled
    max_dd_usd: float = _num("AUREON_MAX_DD_USD", "0", float)                   # v2.16.0: no new auto entries once equity is this far below its peak (0 = off)
    allow_live: bool = os.environ.get("AUREON_ALLOW_LIVE", "0") == "1"
    filling: str = os.environ.get("AUREON_FILLING", "ioc").strip().lower()      # v2.16.1: ioc | fok | return (fallback on 10030)
    max_lots: float = _num("AUREON_MAX_LOTS", "1.0", float)
    entry_921: str = os.environ.get("AUREON_921_ENTRY", "market").strip().lower()       # v2.17.0: market | limit (behind /auto-trade)
    limit_offset_921: float = _num("AUREON_921_LIMIT_OFFSET", "1.5", float)           # v2.17.0: points back from the cross close
    limit_ttl_bars_921: int = _num("AUREON_921_LIMIT_TTL_BARS", "2", int)             # v2.17.0: bars a pending order may fill
    lot_buttons: list[float] = field(default_factory=lambda: _parse_lot_buttons(os.environ.get("AUREON_LOT_BUTTONS", "0.5,1.0,1.5")))
    # AUREON-012 partial close at secure (0 = off)
    partial_close_pct: float = _num("AUREON_PARTIAL_PCT", "0", float)
    # AUREON-013 weekly store: SUPABASE_URL + SUPABASE_SERVICE_KEY in .env (read by supabase_sync.py)
    weekly_report_ist: str = os.environ.get("AUREON_WEEKLY_IST", "10:00")   # every Saturday at this IST time
    # Supabase write policy — "weekly" (default, free plan): ONE batch every Saturday (+ monthly roll-up on the first Saturday);
    # "realtime": also push signals/decisions as they happen and sync nightly. Reports are always computed locally on demand.
    supabase_mode: str = os.environ.get("AUREON_SUPABASE_MODE", "weekly")
    supabase_ping: bool = os.environ.get("AUREON_SUPABASE_PING", "0") == "1"   # minute heartbeat to Supabase — off on the free plan
    watchdog: bool = os.environ.get("AUREON_WATCHDOG", "1") == "1"
    heartbeat_hours: float = _num("AUREON_HEARTBEAT_HOURS", "4", float)
    # v2.7.2 dead-man's switch: pinged every 60 s only while healthy (healthchecks.io or any URL); empty = off
    deadman_url: str | None = os.environ.get("AUREON_DEADMAN_URL") or None
    streak_half_after: int = _num("AUREON_STREAK_HALF_AFTER", "0", int)
    chop_crosses: int = _num("AUREON_CHOP_CROSSES", "5", int)
    missed_grace_min: int = _num("AUREON_MISSED_GRACE_MIN", "5", int)
    override_window_min: int = _num("AUREON_OVERRIDE_WINDOW_MIN", "10", int)
    override_max_drift: float = _num("AUREON_OVERRIDE_MAX_DRIFT", "3.0", float)
    agent_default_min: int = _num("AUREON_AGENT_DEFAULT_MIN", "60", int)
    agent_delay_s: float = _num("AUREON_AGENT_DELAY_S", "20", float)
    daily_plan: bool = os.environ.get("AUREON_DAILY_PLAN", "1") == "1"
    daily_plan_at: str = os.environ.get("AUREON_DAILY_PLAN_AT", "05:00")
    week_goal_pts: float = _num("AUREON_WEEK_GOAL_PTS", "0", float)
    early_lock_mode: str = os.environ.get("AUREON_EARLY_LOCK_MODE", "soft")   # soft | hard | off
    soft_fallback_min: int = _num("AUREON_SOFT_FALLBACK_MIN", "5", int)
    # v2.7.0 guardian profile: "leg" (move-detector based) | "classic" (exact pre-v2.7.0 behaviour)
    guardian_profile: str = os.environ.get("AUREON_GUARDIAN_PROFILE", "classic")  # "auto" = leg on demo; "classic" | "leg" explicit
    guardian_profile_demo: str = os.environ.get("AUREON_GUARDIAN_PROFILE_DEMO", "leg")
    soft_giveback_pts: float = _num("AUREON_SOFT_GIVEBACK_PTS", "3.0", float)
    soft_giveback_frac: float = _num("AUREON_SOFT_GIVEBACK_FRAC", "0.5", float)
    soft_arm_pts: float = _num("AUREON_SOFT_ARM_PTS", "5", float)
    ride_floor: float = _num("AUREON_RIDE_FLOOR", "4", float)
    ride_swing_bars: int = _num("AUREON_RIDE_SWING_BARS", "6", int)
    ride_buffer: float = _num("AUREON_RIDE_BUFFER", "1.0", float)
    ride_atr_mult: float = _num("AUREON_RIDE_ATR_MULT", "3.0", float)
    # your manual SL rules: if you placed an SL wider than the guardian's P-phase stop, the guardian does not close at −pre_stop
    respect_manual_sl: bool = os.environ.get("AUREON_RESPECT_MANUAL_SL", "1") == "1"
    # Codex read-only advisor (Discord on-demand; never blocks signals or guardian)
    codex_mode: str = os.environ.get("AUREON_CODEX", "off").strip().lower()
    codex_bin: str = os.environ.get("AUREON_CODEX_BIN", "codex")
    codex_model: str = os.environ.get("AUREON_CODEX_MODEL", "")
    codex_timeout: float = _num("AUREON_CODEX_TIMEOUT", "30", float)
    codex_max_calls: int = _num("AUREON_CODEX_MAX_CALLS", "30", int)
    codex_workdir: str = os.environ.get("AUREON_CODEX_WORKDIR", "") or os.path.join(os.path.expanduser("~"), "aureon_codex")

    # v1.10.0 Claude add-on (local `claude -p`, your Claude Code login — no API key, no SDK). off | review | advisory | manage
    claude_mode: str = os.environ.get("AUREON_CLAUDE", "off").strip().lower()
    claude_bin: str = os.environ.get("AUREON_CLAUDE_BIN", "claude")
    claude_entry_model: str = os.environ.get("AUREON_CLAUDE_ENTRY_MODEL", "opus")
    claude_pullback_model: str = os.environ.get("AUREON_CLAUDE_PULLBACK_MODEL", "sonnet")
    claude_review_model: str = os.environ.get("AUREON_CLAUDE_REVIEW_MODEL", "haiku")
    claude_max_calls: int = _num("AUREON_CLAUDE_MAX_CALLS", "30", int)      # per IST day, all symbols
    claude_timeout: float = _num("AUREON_CLAUDE_TIMEOUT", "90", float)      # seconds per call
    claude_workdir: str = os.environ.get("AUREON_CLAUDE_WORKDIR", "") or os.path.join(os.path.expanduser("~"), "aureon_claude")
    # v2.8.0 the chart goes with these Claude events (P, CROSS, READ); "" = never. Prompt text only — never changes a trade.
    claude_image_events: tuple = tuple(e.strip().upper() for e in os.environ.get("AUREON_CLAUDE_IMAGE_EVENTS", "P,CROSS,READ").split(",")
                                       if e.strip())
    # v2.8.0 structure-gated flip (0 = v2.7.2: any opposite P closes) — evidence needed before an opposite P closes the trade
    flip_require_structure: bool = os.environ.get("AUREON_FLIP_REQUIRE_STRUCTURE", "0") == "1"
    flip_min_evidence: int = _num("AUREON_FLIP_MIN_EVIDENCE", "2", int)
    flip_gap_x_range: float = _num("AUREON_FLIP_GAP_X_RANGE", "0.5", float)
    # v2.8.0 giveback exit on a bar CLOSE once peak >= ARM (0 = off)
    giveback_pct: float = _num("AUREON_GIVEBACK_PCT", "0", float)
    giveback_arm: float = _num("AUREON_GIVEBACK_ARM", "10", float)
    # v2.8.0 setup-aware stop (classic profile; 0 = v2.7.2 fixed −pre_stop and 12-bar timeout)
    setup_stop: bool = os.environ.get("AUREON_SETUP_STOP", "0") == "1"
    setup_max_dd: float = _num("AUREON_SETUP_MAX_DD", "10", float)
    setup_max_dd_x_range: float = _num("AUREON_SETUP_MAX_DD_X_RANGE", "0", float)
    setup_swing_bars: int = _num("AUREON_SETUP_SWING_BARS", "6", int)
    setup_buffer: float = _num("AUREON_SETUP_BUFFER", "1.0", float)
    # v2.8.0 Claude votes: one READ at open (shown only) and event-driven READs in drawdown
    claude_open_read: bool = os.environ.get("AUREON_CLAUDE_OPEN_READ", "1") == "1"
    claude_dd_read_at: float = _num("AUREON_DD_READ_AT", "3", float)
    claude_dd_read_step: float = _num("AUREON_DD_READ_STEP", "2", float)
    claude_dd_reads_per_trade: int = _num("AUREON_DD_READS_PER_TRADE", "3", int)
    claude_exit_losing: bool = os.environ.get("AUREON_CLAUDE_EXIT_LOSING", "0") == "1"
    # v2.10.0 (aureon_mt5/features.py registers every toggle; /feature-set changes them live)
    flip_use_zl: bool = os.environ.get("AUREON_FLIP_USE_ZL", "0") == "1"
    zl_context: bool = os.environ.get("AUREON_ZL_CONTEXT", "0") == "1"
    repair_agent: str = os.environ.get("AUREON_REPAIR_AGENT", "off").strip().lower()
    repair_engine: str = os.environ.get("AUREON_REPAIR_ENGINE", "claude").strip().lower()
    repair_model: str = os.environ.get("AUREON_REPAIR_MODEL", "opus")
    repair_timeout: float = _num("AUREON_REPAIR_TIMEOUT", "900", float)
    regime_gate: str = os.environ.get("AUREON_REGIME_GATE", "off").strip().lower()
    regime_choppy: float = _num("AUREON_REGIME_CHOPPY", "0.45", float)
    regime_dead: float = _num("AUREON_REGIME_DEAD", "0.25", float)
    regime_clean: float = _num("AUREON_REGIME_CLEAN", "0.55", float)
    regime_clean_bars: int = _num("AUREON_REGIME_CLEAN_BARS", "4", int)
    no_trade_day_frac: float = _num("AUREON_NO_TRADE_DAY_FRAC", "0.6", float)
    # v2.10.1 unattended operation + server ownership
    repair_auto_per_day: int = _num("AUREON_REPAIR_AUTO_PER_DAY", "2", int)
    repair_verify_s: float = _num("AUREON_REPAIR_VERIFY_S", "600", float)
    mt5_path: str = os.environ.get("AUREON_MT5_PATH", "")
    mt5_boot_wait: float = _num("AUREON_MT5_BOOT_WAIT", "300", float)
    session_ist: str = os.environ.get("AUREON_SESSION_IST", "05:30-23:00")       # the governor's entry window
    server_active_hours: str = os.environ.get("AUREON_SERVER_ACTIVE_HOURS", "")
    server_autologon: bool = os.environ.get("AUREON_SERVER_AUTOLOGON", "1") == "1"
    maint_force_after_h: float = _num("AUREON_SERVER_MAINT_FORCE_AFTER_H", "48", float)
    auto_update: bool = os.environ.get("AUREON_AUTO_UPDATE", "1") == "1"         # v2.17.0: apply merges to master when flat
    update_poll_min: float = _num("AUREON_UPDATE_POLL_MIN", "2", float)          # v2.17.0: origin/master poll interval
    log_keep_days: float = _num("AUREON_LOG_KEEP_DAYS", "30", float)
    tuning_auto: str = os.environ.get("AUREON_TUNING_AUTO", "off").strip().lower()
    tuning_replay_days: int = _num("AUREON_TUNING_REPLAY_DAYS", "30", int)
    tuning_shadow_sessions: int = _num("AUREON_TUNING_SHADOW_SESSIONS", "3", int)
    tuning_auto_per_week: int = _num("AUREON_TUNING_AUTO_PER_WEEK", "1", int)
    # v2.11.0 notifications: Discord mentions, the asks channel, the push layer, the action console (no trading change)
    discord_user_id: str = os.environ.get("AUREON_DISCORD_USER_ID", "").strip()          # <@id> on trade-class cards only
    discord_asks_webhook: str = os.environ.get("AUREON_DISCORD_ASKS_WEBHOOK", "").strip() # trade-class copies (phone: All messages)
    discord_mention_events: tuple = field(default_factory=lambda: _event_set("AUREON_DISCORD_MENTION_EVENTS", "ASK,FILLED,CLOSED,STOP"))
    push: tuple = field(default_factory=lambda: _event_set("AUREON_PUSH", "off"))          # () | NTFY,PUSHOVER,TELEGRAM
    push_images: bool = os.environ.get("AUREON_PUSH_IMAGES", "0") == "1"
    push_loss_alert_pts: float = _num("AUREON_PUSH_LOSS_ALERT_PTS", "5", float)
    ntfy_url: str = os.environ.get("AUREON_NTFY_URL", "https://ntfy.sh").strip().rstrip("/")
    ntfy_topic: str = os.environ.get("AUREON_NTFY_TOPIC", "").strip()
    ntfy_token: str = os.environ.get("AUREON_NTFY_TOKEN", "").strip()
    pushover_token: str = os.environ.get("AUREON_PUSHOVER_TOKEN", "").strip()
    pushover_user: str = os.environ.get("AUREON_PUSHOVER_USER", "").strip()
    telegram_token: str = os.environ.get("AUREON_TELEGRAM_TOKEN", "").strip()
    telegram_chat: str = os.environ.get("AUREON_TELEGRAM_CHAT", "").strip()
    console_bind: str = os.environ.get("AUREON_CONSOLE_BIND", "127.0.0.1:8787").strip()
    console_token: str = os.environ.get("AUREON_CONSOLE_TOKEN", "").strip()
    console_allow_public: bool = os.environ.get("AUREON_CONSOLE_ALLOW_PUBLIC", "0") == "1"
    console_url: str = os.environ.get("AUREON_CONSOLE_URL", "").strip().rstrip("/")     # how the phone reaches it (Tailscale)
    # v2.11.2 network loss: entries stay off while degraded unless the operator allows them; optional MT5 host for the DNS check
    network_degraded_entries: bool = os.environ.get("AUREON_NETWORK_DEGRADED_ENTRIES", "0") == "1"
    mt5_host: str = os.environ.get("AUREON_MT5_HOST", "").strip()
    # v2.12.0 pre-exit read: before a close-based software exit Claude gets a vote (off = v2.11.2, show = journal only,
    # defer = HOLD delays the close by one bar, at most AUREON_PRE_EXIT_MAX_DEFER times; the broker stop never moves)
    pre_exit_read: str = os.environ.get("AUREON_PRE_EXIT_READ", "off").strip().lower()
    pre_exit_wait_s: float = _num("AUREON_PRE_EXIT_WAIT_S", "45", float)
    pre_exit_max_defer: int = _num("AUREON_PRE_EXIT_MAX_DEFER", "2", int)
    pre_exit_grade_bars: int = _num("AUREON_PRE_EXIT_GRADE_BARS", "6", int)
    # v2.13.0 follow guardian profile (AUREON_GUARDIAN_PROFILE=follow): secure early, lock every +STEP, exit only on the stop
    follow_stop: float = _num("AUREON_FOLLOW_STOP", "25", float)
    follow_stop_x_range: float = _num("AUREON_FOLLOW_STOP_X_RANGE", "0", float)
    follow_secure_at: float = _num("AUREON_FOLLOW_SECURE_AT", "3", float)
    follow_secure_level: float = _num("AUREON_FOLLOW_SECURE_LEVEL", "3", float)
    follow_step: float = _num("AUREON_FOLLOW_STEP", "10", float)
    follow_clearance: float = _num("AUREON_FOLLOW_CLEARANCE", "2", float)
    follow_trail_mode: str = os.environ.get("AUREON_FOLLOW_TRAIL_MODE", "step").strip().lower()
    follow_exit_on_flip: str = os.environ.get("AUREON_FOLLOW_EXIT_ON_FLIP", "structure").strip().lower()
    follow_news_flat: bool = os.environ.get("AUREON_FOLLOW_NEWS_FLAT", "1") == "1"
    follow_max_hold_bars: int = _num("AUREON_FOLLOW_MAX_HOLD_BARS", "0", int)
    follow_pre_exit_read: bool = os.environ.get("AUREON_FOLLOW_PRE_EXIT_READ", "1") == "1"   # the flip exit asks Claude when
                                                                                           # AUREON_PRE_EXIT_READ is on
    # v2.14.0 execution timeframe + the higher-timeframe alignment gate (defaults = v2.13.0: M5, gate off)
    exec_tf: str = os.environ.get("AUREON_EXEC_TF", "M5").strip().lower()        # m5 | m10 | … (readers upper() it)
    confirm_hold_bars: int = _num("AUREON_CONFIRM_HOLD_BARS", "0", int)          # 0 = the exec TF's default (M5 3, M30 2)
    confirm_gap_x_range: float = _num("AUREON_CONFIRM_GAP_X_RANGE", "0", float)  # 0 = the exec TF's default (M5 1.5 pts, M30 0.25×)
    confirm_window_bars: int = _num("AUREON_CONFIRM_WINDOW_BARS", "0", int)      # 0 = the exec TF's default (M5 18, M30 6)
    htf_align: str = os.environ.get("AUREON_HTF_ALIGN", "off").strip().lower()
    htf_confirm_tfs: tuple = field(default_factory=lambda: tuple(x.strip().upper() for x in
                                   os.environ.get("AUREON_HTF_CONFIRM_TFS", "").replace(" ", ",").split(",") if x.strip()))
    htf_pre_max_gap: float = _num("AUREON_HTF_PRE_MAX_GAP", "0.35", float)
    htf_with_max_bars: int = _num("AUREON_HTF_WITH_MAX_BARS", "8", int)
    htf_require_fresh: bool = os.environ.get("AUREON_HTF_REQUIRE_FRESH", "0") == "1"
    htf_align_min: str = os.environ.get("AUREON_HTF_ALIGN_MIN", "all").strip().lower()
    # regime score weights (sum need not be 1; the score is clipped to [0, 1])
    regime_weights: dict = field(default_factory=lambda: {"crosses": 0.30, "flat_gap": 0.20, "adx": 0.20, "di": 0.10,
                                                          "failed_p": 0.10, "compression": 0.10})
    # v1.11.0 weekly compare: optional Claude self-review of its wrong calls after the Saturday COMPARE card (counts toward the budget)
    compare_claude_review: bool = os.environ.get("AUREON_COMPARE_CLAUDE_REVIEW", "0") == "1"
    calib_min_n: int = _num("AUREON_CALIB_MIN_N", "30", int)
    pre_alert_2050: bool = os.environ.get("AUREON_PRE_ALERT_2050", "1") == "1"
    pwin_floor: float = _num("AUREON_PWIN_FLOOR", "0", float)
    pwin_wait_s: float = _num("AUREON_PWIN_WAIT_S", "15", float)
    eod_at: str = os.environ.get("AUREON_EOD_AT", "23:15")
    publish: bool = os.environ.get("AUREON_PUBLISH", "0") == "1"
    redis_url: str = os.environ.get("REDIS_URL", "")
    ctx_bars: int = _num("AUREON_CTX_BARS", "48", int)
    ctx_max_chars: int = _num("AUREON_CTX_MAX_CHARS", "6000", int)
    voice: bool = os.environ.get("AUREON_VOICE", "0") == "1"
    voice_chats: list[str] = field(default_factory=lambda: [c.strip() for c in os.environ.get("AUREON_VOICE_CHATS", "").split(",") if c.strip()])
    voice_secret: str = os.environ.get("AUREON_VOICE_SECRET", "")
    news_file: str | None = "news_blackout.txt"
    log_dir: str = "logs"
    source: str = os.environ.get("AUREON_SOURCE", "mt5")
    enable_silver: bool = False
    dry: bool = False
    _test_skip_session: bool = False   # set True in tests to skip governor session_window (never in production)

    def resolved_guardian_profile(self, is_demo: bool | None = None, symbol: str | None = None) -> str:
        """Resolve guardian profile: explicit value > auto (leg on demo, classic on live).
        is_demo=None (unknown) → classic (safe default)."""
        if symbol:                                    # v2.13.0: per symbol, e.g. AUREON_GUARDIAN_PROFILE_XAUUSD=follow
            per = os.environ.get(f"AUREON_GUARDIAN_PROFILE_{symbol.upper()}", "").strip().lower()
            if per in ("classic", "leg", "follow"):
                return per
        if self.guardian_profile not in ("auto", ""):
            return self.guardian_profile
        if is_demo is True:
            return self.guardian_profile_demo
        return "classic"


# ----------------------------------------------------------------------------- v2.10.1 env check (BOOT CHECK card, /env-check)
GUARDIAN_GROUPS = ("guardian", "regime")


def validate(cfg, environ=None, pre_stop: float = 6.0) -> list[dict]:
    """Every problem with the running configuration: {name, env, value, issue, fallback, group}. Pure; see apply_fallbacks()
    for the safe defaults. Rules: unparsable numbers · values outside the feature registry's range / choices · Discord
    plumbing missing · contradictory combinations."""
    from . import features as F
    env = os.environ if environ is None else environ
    out = [dict(p, group=_group_of(p["env"])) for p in PARSE_PROBLEMS]
    seen = {p["env"] for p in out}
    for f in F.registry():
        raw = env.get(f.env)
        if raw in (None, "") or f.env in seen:
            continue
        try:
            F.parse(f, raw)
        except ValueError as e:
            out.append({"name": f.name, "env": f.env, "value": raw, "issue": str(e).split(": ", 1)[-1],
                        "fallback": F.fmt(f, f.default), "group": f.group})
    if not (getattr(cfg, "webhook", None) or env.get("DISCORD_WEBHOOK")):
        out.append({"name": "discord_webhook", "env": "DISCORD_WEBHOOK", "value": "", "issue": "missing — no Discord cards",
                    "fallback": "console only", "group": "discord"})
    if not (getattr(cfg, "bot_token", None) or env.get("DISCORD_TOKEN")):
        out.append({"name": "discord_token", "env": "DISCORD_TOKEN", "value": "", "issue": "missing — slash commands off",
                    "fallback": "webhook cards only", "group": "discord"})
    role = env.get("AUREON_ADMIN_ROLE_ID", "")
    if role and not role.isdigit():
        out.append({"name": "admin_role", "env": "AUREON_ADMIN_ROLE_ID", "value": role, "issue": "not a role id (digits)",
                    "fallback": "admin commands denied", "group": "discord"})
    mode = str(getattr(cfg, "claude_mode", "off"))
    if getattr(cfg, "claude_exit_losing", False) and mode != "manage":
        out.append({"name": "exit_losing", "env": "AUREON_CLAUDE_EXIT_LOSING", "value": "1",
                    "issue": f"has no effect with AUREON_CLAUDE={mode} (cut-loss needs manage)", "fallback": "off", "group": "claude"})
    if getattr(cfg, "setup_stop", False) and float(getattr(cfg, "setup_max_dd", 10)) < pre_stop:
        out.append({"name": "setup_max_dd", "env": "AUREON_SETUP_MAX_DD", "value": f"{cfg.setup_max_dd:g}",
                    "issue": f"cap −{cfg.setup_max_dd:g} is tighter than pre_stop −{pre_stop:g} with SETUP_STOP=1",
                    "fallback": "10", "group": "guardian"})
    if not (cfg.regime_dead < cfg.regime_choppy <= cfg.regime_clean):
        out.append({"name": "regime_thresholds", "env": "AUREON_REGIME_DEAD/CHOPPY/CLEAN",
                    "value": f"{cfg.regime_dead:g}/{cfg.regime_choppy:g}/{cfg.regime_clean:g}",
                    "issue": "need DEAD < CHOPPY <= CLEAN", "fallback": "0.25/0.45/0.55", "group": "regime"})
    if str(getattr(cfg, "entry_921", "market")) not in ("market", "limit"):              # v2.17.0
        out.append({"name": "entry_921", "env": "AUREON_921_ENTRY", "value": str(cfg.entry_921), "issue": "market | limit",
                    "fallback": "market", "group": "trading"})
    out += _notify_problems(cfg, env)
    out += sync_folder_problems(cfg)
    return out


SYNC_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "my drive", "icloud drive", "iclouddrive")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sync_folder_problems(cfg, root: str | None = None) -> list[dict]:
    """v2.11.2: a repo or log_dir inside OneDrive / Dropbox / Google Drive / iCloud — the sync client locks and rewrites
    guardian_state.json, features.json, restart_marker.json and the journal while the bot writes them."""
    out = []
    for what, path in (("repo", root or REPO_ROOT), ("log_dir", getattr(cfg, "log_dir", "logs") or "logs")):
        try:
            real = os.path.realpath(path if os.path.isabs(path) else os.path.join(root or REPO_ROOT, path))
        except Exception:
            continue
        low = real.replace("\\", "/").lower()
        hit = next((m for m in SYNC_MARKERS if m in low), None)
        if hit:
            out.append({"name": "sync_folder", "env": f"{what} path", "value": real,
                        "issue": f"⚠️ inside a {hit} sync folder — it locks and syncs the journal / state files while the bot "
                                 "writes them; install at C:\\Aureon and run the NSSM service (docs/SERVICE.md §1)",
                        "fallback": "runs, but move it", "group": "server"})
    return out


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def console_host_port(bind: str) -> tuple[str, int]:
    """'127.0.0.1:8787' → ('127.0.0.1', 8787); '[::1]:8787' too. ValueError on garbage."""
    b = str(bind or "").strip()
    host, _, port = b.rpartition(":")
    host = host.strip("[]")
    if not host or not port.isdigit() or not (0 < int(port) < 65536):
        raise ValueError(f"not host:port: {bind!r}")
    return host, int(port)


def _notify_problems(cfg, env) -> list[dict]:
    """v2.11.0: mentions, push backends, the console (bind / token)."""
    out = []
    uid = str(getattr(cfg, "discord_user_id", "") or "")
    if uid and not uid.isdigit():
        out.append({"name": "discord_user_id", "env": "AUREON_DISCORD_USER_ID", "value": uid, "issue": "not a user id (digits)",
                    "fallback": "no mentions", "group": "notify"})
    hook = str(getattr(cfg, "discord_asks_webhook", "") or "")
    if hook and not hook.startswith("https://"):
        out.append({"name": "discord_asks_webhook", "env": "AUREON_DISCORD_ASKS_WEBHOOK", "value": hook[:40],
                    "issue": "not an https webhook URL", "fallback": "no asks channel", "group": "notify"})
    push = tuple(b.upper() for b in (getattr(cfg, "push", ()) or ()))
    need = {"NTFY": ("ntfy_topic",), "PUSHOVER": ("pushover_token", "pushover_user"), "TELEGRAM": ("telegram_token", "telegram_chat")}
    for b in push:
        miss = [a for a in need.get(b, ()) if not getattr(cfg, a, "")]
        if miss:
            out.append({"name": "push", "env": "AUREON_PUSH", "value": ",".join(push).lower(),
                        "issue": f"{b.lower()} needs " + ", ".join("AUREON_" + a.upper() for a in miss), "fallback": f"{b.lower()} off",
                        "group": "notify"})
    tok = str(getattr(cfg, "console_token", "") or "")
    if tok and len(tok) < 32:
        out.append({"name": "console_token", "env": "AUREON_CONSOLE_TOKEN", "value": f"<{len(tok)} chars>",
                    "issue": "shorter than 32 characters", "fallback": "console off", "group": "notify"})
    bind = str(getattr(cfg, "console_bind", "") or "")
    try:
        host, port = console_host_port(bind)
    except ValueError:
        out.append({"name": "console_bind", "env": "AUREON_CONSOLE_BIND", "value": bind, "issue": "not host:port",
                    "fallback": "127.0.0.1:8787", "group": "notify"})
    else:
        if host not in LOOPBACK:
            if getattr(cfg, "console_allow_public", False):
                out.append({"name": "console_public", "env": "AUREON_CONSOLE_ALLOW_PUBLIC", "value": "1",
                            "issue": f"⚠️ CONSOLE LISTENS ON {host}:{port} — anyone who can reach it and has the token can TAKE / CLOSE",
                            "fallback": "allowed (AUREON_CONSOLE_ALLOW_PUBLIC=1)", "group": "notify"})
            else:
                out.append({"name": "console_bind", "env": "AUREON_CONSOLE_BIND", "value": bind,
                            "issue": "non-loopback bind refused without AUREON_CONSOLE_ALLOW_PUBLIC=1",
                            "fallback": f"127.0.0.1:{port}", "group": "notify"})
    if push and not (tok and len(tok) >= 32 and getattr(cfg, "console_url", "")):
        out.append({"name": "push_actions", "env": "AUREON_CONSOLE_URL", "value": str(getattr(cfg, "console_url", "") or ""),
                    "issue": "push without a console URL + token has no TAKE / SKIP / READ buttons",
                    "fallback": "pushes without actions", "group": "notify"})
    return out


def _group_of(env_name: str) -> str:
    from . import features as F
    return next((f.group for f in F.registry() if f.env == env_name), "other")


def apply_fallbacks(cfg, problems: list[dict]) -> list[str]:
    """Set every problematic registry feature (and the contradictory guardian combos) back to its safe default.
    Returns the names changed. Never leaves a guardian knob unset."""
    from . import features as F
    changed = []
    for p in problems:
        if p["name"] == "regime_thresholds":
            cfg.regime_dead, cfg.regime_choppy, cfg.regime_clean = 0.25, 0.45, 0.55; changed.append(p["name"]); continue
        if p["name"] == "discord_user_id":
            cfg.discord_user_id = ""; changed.append(p["name"]); continue
        if p["name"] == "discord_asks_webhook":
            cfg.discord_asks_webhook = ""; changed.append(p["name"]); continue
        if p["name"] == "console_token":
            cfg.console_token = ""; changed.append(p["name"]); continue
        if p["name"] == "console_bind":
            cfg.console_bind = p["fallback"]; changed.append(p["name"]); continue
        if p["name"] == "push" and p["fallback"].endswith(" off"):
            off = p["fallback"].split()[0].upper()
            cfg.push = tuple(b for b in cfg.push if b.upper() != off); changed.append(f"push:{off.lower()}"); continue
        if p["name"] in ("console_public", "push_actions"):
            continue
        if p["name"] == "entry_921":
            cfg.entry_921 = "market"; changed.append(p["name"]); continue
        try:
            f = F.get(p["name"])
        except ValueError:
            continue
        setattr(cfg, f.attr, f.default); changed.append(f.name)
    return changed


def env_check_table(cfg, overrides: dict | None = None, problems: list[dict] | None = None, environ=None) -> str:
    """/env-check: what the guardian is actually running on (registry values + sources) and every problem."""
    from . import features as F
    lines = ["**running on**"] + F.summary(cfg, overrides or {}, environ=environ)
    probs = validate(cfg, environ) if problems is None else problems
    lines.append("**problems**" if probs else "**problems** · none")
    for p in probs:
        lines.append(f"⚠️ `{p['env']}`={p['value'] or '∅'} — {p['issue']} → {p['fallback']}")
    return "\n".join(lines)
