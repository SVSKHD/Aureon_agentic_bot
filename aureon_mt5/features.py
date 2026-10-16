"""v2.10.0 — feature registry: every env-gated feature, live toggles from Discord (/feature-set), one source of truth.

Resolution: discord (logs/features.json) > env (.env / environment at boot) > default (the Config default).
apply() writes the resolved value onto the matching Config attribute IN PLACE, so every caller that already reads
`cfg.<attr>` sees the change on its next read (the guardian on its next poll) — no caller changes.
Same atomic-write style as tuning.py. Nothing here changes a trade by itself: defaults reproduce the previous release."""
from __future__ import annotations

import json
import os
import time as _time
from dataclasses import dataclass, field

IMAGE_EVENT_CHOICES = ("P", "CROSS", "RE", "ENTER", "ALERT", "READ")
MENTION_EVENT_CHOICES = ("ASK", "FILLED", "CLOSED", "STOP", "FLIP_HOLD", "POSITION_ASK", "CLOSED_BY_YOU")
PUSH_CHOICES = ("NTFY", "PUSHOVER", "TELEGRAM")
TF_LADDER_CHOICES = ("M5", "M10", "M15", "M20", "M30", "M45", "H1", "H2", "H3", "H4", "H6", "H8", "H12", "D1")
TRUE_WORDS = ("1", "true", "on", "yes", "y")
FALSE_WORDS = ("0", "false", "off", "no", "n")


@dataclass(frozen=True)
class Feature:
    name: str
    env: str
    type: str                 # bool | float | int | enum | enum-set
    default: object
    attr: str                 # the Config attribute this feature drives
    group: str
    desc: str
    hot: bool = True          # safe to change with a position open
    loud: bool = False        # a change posts a FEATURE CHANGED card
    choices: tuple = ()
    min: float | None = None
    max: float | None = None
    loud_values: tuple = ()   # loud only when switched TO one of these (empty = any change)
    consequence: str = ""     # one line for the FEATURE CHANGED card
    unit: str = ""
    extra: dict = field(default_factory=dict)


def registry() -> list[Feature]:
    return [
        # ---- claude
        Feature("claude_mode", "AUREON_CLAUDE", "enum", "off", "claude_mode", "claude",
                "Claude add-on: off · review (cards only) · advisory (shown) · manage (TIGHTEN / CLOSE-in-profit applied)",
                choices=("off", "review", "advisory", "manage"), loud=True, loud_values=("manage",),
                consequence="Claude can now close trades in profit and tighten SLs"),
        Feature("image_events", "AUREON_CLAUDE_IMAGE_EVENTS", "enum-set", ("P", "CROSS", "READ"), "claude_image_events", "claude",
                "Claude events that get the chart PNG (prompt only, never a trade)", choices=IMAGE_EVENT_CHOICES),
        Feature("open_read", "AUREON_CLAUDE_OPEN_READ", "bool", True, "claude_open_read", "claude",
                "one Claude READ when a position is first seen (shown only)"),
        Feature("dd_read_at", "AUREON_DD_READ_AT", "float", 3.0, "claude_dd_read_at", "claude",
                "Claude drawdown READ when a bar closes this many pts under water (0 = off)", min=0.0, max=30.0, unit="pts"),
        Feature("exit_losing", "AUREON_CLAUDE_EXIT_LOSING", "bool", False, "claude_exit_losing", "claude",
                "manage mode: a drawdown EXIT may close a LOSING trade", loud=True, loud_values=(True,),
                consequence="Claude may now cut a losing trade from a drawdown read"),
        Feature("max_calls", "AUREON_CLAUDE_MAX_CALLS", "int", 30, "claude_max_calls", "claude",
                "Claude calls per IST day, all symbols", min=0, max=1000),
        Feature("pre_exit_read", "AUREON_PRE_EXIT_READ", "enum", "off", "pre_exit_read", "claude",
                "before a close-based software exit (ema_fast_turn, giveback, setup_invalidated, pre_timeout, p_flip): off · "
                "show = read journaled next to the close · defer = Claude HOLD delays the close one bar (broker stop unchanged)",
                choices=("off", "show", "defer"), loud=True, loud_values=("defer",),
                consequence="close-based exits may now wait up to {pre_exit_max_defer} bars for Claude (broker stop unchanged)"),
        Feature("pre_exit_max_defer", "AUREON_PRE_EXIT_MAX_DEFER", "int", 2, "pre_exit_max_defer", "claude",
                "pre-exit HOLDs per trade before the rule closes without asking", min=0, max=5),
        Feature("pre_exit_wait_s", "AUREON_PRE_EXIT_WAIT_S", "float", 45.0, "pre_exit_wait_s", "claude",
                "seconds the guardian waits for the pre-exit verdict before closing by the rule", min=10.0, max=240.0, unit="s"),
        # ---- guardian
        Feature("flip_require_structure", "AUREON_FLIP_REQUIRE_STRUCTURE", "bool", False, "flip_require_structure", "guardian",
                "an opposite P closes the trade only when the structure confirms it (next bar)"),
        Feature("flip_use_zl", "AUREON_FLIP_USE_ZL", "bool", False, "flip_use_zl", "guardian",
                "adds 'ZL cross against' as a fifth flip-evidence item (only adds evidence)"),
        Feature("giveback_pct", "AUREON_GIVEBACK_PCT", "float", 0.0, "giveback_pct", "guardian",
                "close on a bar CLOSE that gave back this % of the peak (0 = off; next bar)", min=0.0, max=90.0, unit="%"),
        Feature("giveback_arm", "AUREON_GIVEBACK_ARM", "float", 10.0, "giveback_arm", "guardian",
                "giveback is armed once the peak reached this many pts", min=1.0, max=100.0, unit="pts"),
        Feature("setup_stop", "AUREON_SETUP_STOP", "bool", False, "setup_stop", "guardian",
                "classic: initial SL = swing structure capped at −MAX_DD; exit on a CLOSE that kills the setup",
                hot=False, loud=True, loud_values=(True,), consequence="initial stop is now the structure stop, cap −{setup_max_dd}"),
        Feature("setup_max_dd", "AUREON_SETUP_MAX_DD", "float", 10.0, "setup_max_dd", "guardian",
                "the setup stop's hard drawdown cap", hot=False, min=2.0, max=30.0, unit="pts"),
        # ---- follow (v2.13.0) — the guardian profile and its knobs; not hot (a ticket keeps the profile it opened with)
        Feature("guardian_profile", "AUREON_GUARDIAN_PROFILE", "enum", "classic", "guardian_profile", "follow",
                "guardian profile for new trades: classic · leg · follow (secure +3, lock every +10, exit only on the stop) · "
                "auto (leg on demo)", choices=("classic", "leg", "follow", "auto"), hot=False, loud=True, loud_values=("follow",),
                consequence="new trades are managed by the follow profile: stop {follow_stop} pts, secure +{follow_secure_at}, "
                            "lock every +{follow_step}; no software exits"),
        Feature("follow_stop", "AUREON_FOLLOW_STOP", "float", 25.0, "follow_stop", "follow",
                "follow: initial stop, points (the M15 study value; scale for M5)", hot=False, min=2.0, max=100.0, unit="pts"),
        Feature("follow_stop_x_range", "AUREON_FOLLOW_STOP_X_RANGE", "float", 0.0, "follow_stop_x_range", "follow",
                "follow: if > 0 the stop is max(FOLLOW_STOP, this × 20-bar average range)", hot=False, min=0.0, max=10.0),
        Feature("follow_secure_at", "AUREON_FOLLOW_SECURE_AT", "float", 3.0, "follow_secure_at", "follow",
                "follow: move the stop to entry + SECURE_LEVEL once the trade is this many points up", hot=False, min=0.5, max=50.0, unit="pts"),
        Feature("follow_secure_level", "AUREON_FOLLOW_SECURE_LEVEL", "float", 3.0, "follow_secure_level", "follow",
                "follow: the secured stop, points above entry", hot=False, min=0.0, max=50.0, unit="pts"),
        Feature("follow_step", "AUREON_FOLLOW_STEP", "float", 10.0, "follow_step", "follow",
                "follow: lock every STEP points (stop = previous step once STEP + CLEARANCE past it)", hot=False, min=1.0, max=100.0, unit="pts"),
        Feature("follow_clearance", "AUREON_FOLLOW_CLEARANCE", "float", 2.0, "follow_clearance", "follow",
                "follow: points past a step before the stop moves (wick protection)", hot=False, min=0.0, max=20.0, unit="pts"),
        Feature("follow_trail_mode", "AUREON_FOLLOW_TRAIL_MODE", "enum", "step", "follow_trail_mode", "follow",
                "follow: step (lock +10 behind each +10) · continuous (stop = peak − STEP, ratcheted)",
                choices=("step", "continuous"), hot=False),
        Feature("follow_exit_on_flip", "AUREON_FOLLOW_EXIT_ON_FLIP", "enum", "structure", "follow_exit_on_flip", "follow",
                "follow: off · structure (opposite CONFIRMED cross with >= FLIP_MIN_EVIDENCE closes; a plain P never does)",
                choices=("off", "structure"), hot=False),
        Feature("follow_news_flat", "AUREON_FOLLOW_NEWS_FLAT", "bool", True, "follow_news_flat", "follow",
                "follow: flat before high-impact news while not secured", hot=False),
        Feature("follow_max_hold_bars", "AUREON_FOLLOW_MAX_HOLD_BARS", "int", 0, "follow_max_hold_bars", "follow",
                "follow: 0 = no timeout; else close only when NOT secured after this many bars AND the setup is dead",
                hot=False, min=0, max=1000),
        Feature("follow_pre_exit_read", "AUREON_FOLLOW_PRE_EXIT_READ", "bool", True, "follow_pre_exit_read", "follow",
                "follow: the structure-flip exit goes through the pre-exit read when AUREON_PRE_EXIT_READ is on"),
        # ---- timeframe (v2.14.0)
        Feature("exec_tf", "AUREON_EXEC_TF", "enum", "m5", "exec_tf", "timeframe",
                "execution timeframe — every bar-count rule is converted from minutes (needs a restart: /decide-timeframe)",
                choices=("m5", "m10", "m15", "m20", "m30", "h1", "h2", "h4"), hot=False, loud=True,
                consequence="the bot now trades on {exec_tf} bars after the restart"),
        Feature("confirm_hold_bars", "AUREON_CONFIRM_HOLD_BARS", "int", 0, "confirm_hold_bars", "timeframe",
                "ema2050 cross confirmation: bars the order must hold (0 = the exec TF default: M5 3, M30 2)", hot=False, min=0, max=10),
        Feature("confirm_gap_x_range", "AUREON_CONFIRM_GAP_X_RANGE", "float", 0.0, "confirm_gap_x_range", "timeframe",
                "ema2050 cross confirmation: |gap| >= this × avg range (0 = the exec TF default: M5 1.5 pts, M30 0.25×)",
                hot=False, min=0.0, max=3.0),
        Feature("confirm_window_bars", "AUREON_CONFIRM_WINDOW_BARS", "int", 0, "confirm_window_bars", "timeframe",
                "ema2050 cross confirmation window in bars (0 = the exec TF default: M5 18, M30 6)", hot=False, min=0, max=60),
        Feature("htf_align", "AUREON_HTF_ALIGN", "enum", "off", "htf_align", "timeframe",
                "the two higher timeframes: off · show (HTF line on the card) · block (no ask unless every confirm TF is with/pre)",
                choices=("off", "show", "block"), loud=True, loud_values=("block",),
                consequence="entry asks are now blocked unless the higher timeframes are with the cross or about to cross"),
        Feature("htf_confirm_tfs", "AUREON_HTF_CONFIRM_TFS", "enum-set", (), "htf_confirm_tfs", "timeframe",
                "confirm timeframes (none = the next two rungs above the exec TF, e.g. M30 → M45,H1)", choices=TF_LADDER_CHOICES),
        Feature("htf_pre_max_gap", "AUREON_HTF_PRE_MAX_GAP", "float", 0.35, "htf_pre_max_gap", "timeframe",
                "`pre` = gap against but converging and |gap| <= this × that TF's avg range", min=0.0, max=3.0),
        Feature("htf_with_max_bars", "AUREON_HTF_WITH_MAX_BARS", "int", 8, "htf_with_max_bars", "timeframe",
                "`with` = crossed our way within this many bars of that TF (older = with_stale)", min=1, max=200),
        Feature("htf_require_fresh", "AUREON_HTF_REQUIRE_FRESH", "bool", False, "htf_require_fresh", "timeframe",
                "a stale same-direction cross (older than HTF_WITH_MAX_BARS) does not count as `with`"),
        Feature("htf_align_min", "AUREON_HTF_ALIGN_MIN", "enum", "all", "htf_align_min", "timeframe",
                "all = every confirm TF with/pre · any = at least one with and none against", choices=("all", "any")),
        # ---- plan
        Feature("daily_plan", "AUREON_DAILY_PLAN", "bool", True, "daily_plan", "plan", "post the daily plan card"),
        # ---- repair
        Feature("repair_agent", "AUREON_REPAIR_AGENT", "enum", "off", "repair_agent", "repair",
                "incidents: off = card only · propose = tested patch on a branch · auto = propose, then apply plumbing fixes "
                "when every guard holds (flat, outside session, suite green, daily cap)",
                choices=("off", "propose", "auto"), loud=True, loud_values=("propose", "auto"),
                consequence="incidents now get a repair proposal; with auto, plumbing fixes apply themselves when every guard holds"),
        # ---- context
        Feature("zl_context", "AUREON_ZL_CONTEXT", "bool", False, "zl_context", "context",
                "zero-lag EMAs, HMA, ADX, KAMA, VWAP in the Claude snapshot and the chart (no rule change)"),
        # ---- regime
        Feature("regime_gate", "AUREON_REGIME_GATE", "enum", "off", "regime_gate", "regime",
                "off = journal only · show = REGIME line on entry cards · block = no ask / no Claude entry read in CHOPPY/DEAD",
                choices=("off", "show", "block"), loud=True, loud_values=("block",),
                consequence="entries are now blocked while the regime is CHOPPY or DEAD (open trades unaffected)"),
        Feature("regime_choppy", "AUREON_REGIME_CHOPPY", "float", 0.45, "regime_choppy", "regime",
                "enter CHOPPY below this regime score", min=0.0, max=1.0),
        Feature("regime_dead", "AUREON_REGIME_DEAD", "float", 0.25, "regime_dead", "regime",
                "enter DEAD below this regime score", min=0.0, max=1.0),
        Feature("regime_clean", "AUREON_REGIME_CLEAN", "float", 0.55, "regime_clean", "regime",
                "a bar counts toward leaving CHOPPY at or above this score", min=0.0, max=1.0),
        Feature("regime_clean_bars", "AUREON_REGIME_CLEAN_BARS", "int", 4, "regime_clean_bars", "regime",
                "consecutive clean bars needed to leave CHOPPY", min=1, max=30),
        Feature("no_trade_day_frac", "AUREON_NO_TRADE_DAY_FRAC", "float", 0.6, "no_trade_day_frac", "regime",
                "NO-TRADE DAY card once CHOPPY/DEAD covered this share of the session (after NY open)", min=0.1, max=1.0),
        # ---- server (v2.10.1)
        Feature("tuning_auto", "AUREON_TUNING_AUTO", "enum", "off", "tuning_auto", "server",
                "strategy proposals: off · shadow (replay + graded shadow sessions, nothing applied) · apply (after a clean "
                "shadow, applied with /veto)", choices=("off", "shadow", "apply"), loud=True, loud_values=("apply",),
                consequence="evidence-gated strategy changes may now apply themselves (replay + shadow; /veto reverts)"),
        Feature("maint_force_after_h", "AUREON_SERVER_MAINT_FORCE_AFTER_H", "float", 48.0, "maint_force_after_h", "server",
                "a pending Windows reboot may happen inside the session after this many hours (still only when flat)",
                min=1.0, max=720.0, unit="h"),
        Feature("log_keep_days", "AUREON_LOG_KEEP_DAYS", "float", 30.0, "log_keep_days", "server",
                "rotated logs / stall dumps / replays older than this are deleted in the maintenance window", min=1.0, max=365.0),
        Feature("network_degraded_entries", "AUREON_NETWORK_DEGRADED_ENTRIES", "bool", False, "network_degraded_entries", "server",
                "allow new entry asks while the network is degraded (Discord / DNS down); off = asks are journaled, not shown",
                loud=True, loud_values=(True,),
                consequence="entry asks now fire while Discord is unreachable — you may not see them until it is back"),
        # ---- notify (v2.11.0) — presentation only, never a trade
        Feature("mention_events", "AUREON_DISCORD_MENTION_EVENTS", "enum-set", ("ASK", "FILLED", "CLOSED", "STOP"),
                "discord_mention_events", "notify",
                "trade-class events that @mention AUREON_DISCORD_USER_ID (and go to the asks channel)", choices=MENTION_EVENT_CHOICES),
        Feature("push", "AUREON_PUSH", "enum-set", (), "push", "notify",
                "phone push backends (none = off): ntfy · pushover · telegram — asks get TAKE / SKIP / READ actions",
                choices=PUSH_CHOICES, loud=True, extra={"loud_when_on": True},
                consequence="trade asks now reach the phone with TAKE / SKIP / READ actions ({push})"),
        Feature("push_images", "AUREON_PUSH_IMAGES", "bool", False, "push_images", "notify",
                "attach the chart PNG to pushes (ntfy upload / pushover attachment)"),
    ]


def get(name: str) -> Feature:
    for f in registry():
        if f.name == name:
            return f
    raise ValueError(f"unknown feature {name!r} — /features lists them")


# ----------------------------------------------------------------------------- parsing / validation
def parse(f: Feature, raw):
    """Raw (string from Discord / env, or a typed value) → typed value. ValueError on anything invalid."""
    if f.type == "bool":
        if isinstance(raw, bool):
            return raw
        w = str(raw).strip().lower()
        if w in TRUE_WORDS:
            return True
        if w in FALSE_WORDS:
            return False
        raise ValueError(f"{f.name}: expected on/off, got {raw!r}")
    if f.type in ("float", "int"):
        try:
            v = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{f.name}: expected a number, got {raw!r}")
        if f.type == "int":
            if v != int(v):
                raise ValueError(f"{f.name}: expected a whole number, got {raw!r}")
            v = int(v)
        if (f.min is not None and v < f.min) or (f.max is not None and v > f.max):
            raise ValueError(f"{f.name}: {v:g} outside [{f.min:g}, {f.max:g}]")
        return v
    if f.type == "enum":
        w = str(raw).strip().lower()
        if w not in f.choices:
            raise ValueError(f"{f.name}: one of {'|'.join(f.choices)}, got {raw!r}")
        return w
    if f.type == "enum-set":
        items = raw if isinstance(raw, (list, tuple)) else [x for x in str(raw).replace(" ", ",").split(",")]
        out = []
        for x in items:
            x = str(x).strip().upper()
            if not x or x in ("NONE", "-", "OFF"):
                continue
            if x not in f.choices:
                raise ValueError(f"{f.name}: {x} not in {','.join(f.choices)}")
            if x not in out:
                out.append(x)
        return tuple(out)
    raise ValueError(f"{f.name}: unknown type {f.type}")


def fmt(f: Feature, v) -> str:
    if f.type == "bool":
        return "on" if v else "off"
    if f.type == "enum-set":
        return ",".join(v) if v else "none"
    if isinstance(v, float):
        return f"{v:g}{f.unit if f.unit == '%' else ''}"
    return str(v)


def active(f: Feature, v) -> bool:
    if f.type == "bool":
        return bool(v)
    if f.type == "enum":
        return v not in ("off",)
    if f.type == "enum-set":
        return bool(v)
    if f.name in ("giveback_pct", "dd_read_at"):
        return float(v) > 0
    return True


# ----------------------------------------------------------------------------- persistence (logs/features.json)
def _path(log_dir: str) -> str:
    return os.path.join(log_dir, "features.json")


def load_overrides(log_dir: str) -> dict:
    """{name: value} set from Discord. Unknown or invalid entries are ignored (never block a boot)."""
    try:
        with open(_path(log_dir), encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return {}
    out = {}
    for name, val in (raw.get("overrides", raw) if isinstance(raw, dict) else {}).items():
        try:
            out[name] = parse(get(name), val)
        except ValueError:
            continue
    return out


def save_overrides(log_dir: str, overrides: dict, meta: dict | None = None):
    os.makedirs(log_dir, exist_ok=True)
    tmp = _path(log_dir) + ".tmp"
    data = {"overrides": {k: (list(v) if isinstance(v, tuple) else v) for k, v in overrides.items()}, "meta": meta or {}}
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, _path(log_dir))


def _load_meta(log_dir: str) -> dict:
    try:
        with open(_path(log_dir), encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("meta") or {}
    except Exception:
        return {}


# ----------------------------------------------------------------------------- resolution / apply
def resolve(name: str, cfg=None, overrides: dict | None = None, environ=None) -> tuple[object, str]:
    """(value, source) with source discord > env > default."""
    f = get(name)
    if overrides and name in overrides:
        return overrides[name], "discord"
    env = os.environ if environ is None else environ
    if env.get(f.env) not in (None, ""):
        try:
            return parse(f, env[f.env]), "env"
        except ValueError:
            pass
    return f.default, "default"


def source(name: str, overrides: dict | None, environ=None) -> str:
    return resolve(name, None, overrides, environ)[1]


def apply(cfg, overrides: dict, advisors=(), environ=None) -> list[str]:
    """Write every override onto cfg in place. Returns the names whose cfg value changed.
    claude_mode also flips the Claude advisor via set_mode; max_calls updates its budget limit."""
    changed = []
    for name, val in (overrides or {}).items():
        try:
            f = get(name)
        except ValueError:
            continue
        if getattr(cfg, f.attr, None) != val:
            setattr(cfg, f.attr, val); changed.append(name)
    sync_advisors(cfg, advisors)
    return changed


def sync_advisors(cfg, advisors=()):
    for adv in advisors or ():
        if adv is None or getattr(adv, "SOURCE", "") != "claude":
            continue
        if adv.mode != cfg.claude_mode:
            adv.set_mode(cfg.claude_mode)
        adv.budget.limit = int(adv.max_calls)


def set_override(log_dir: str, cfg, name: str, raw_value, who: str = "", advisors=()) -> tuple[object, object]:
    """Validate, persist, apply live. Returns (old, new). ValueError on a bad name/value."""
    f = get(name)
    new = parse(f, raw_value)
    old = getattr(cfg, f.attr, f.default)
    ov = load_overrides(log_dir); ov[name] = new
    meta = _load_meta(log_dir); meta[name] = {"who": str(who), "t": int(_time.time())}
    save_overrides(log_dir, ov, meta)
    apply(cfg, ov, advisors)
    return old, new


def clear_override(log_dir: str, cfg, name: str, who: str = "", advisors=(), environ=None) -> tuple[object, object]:
    """Drop the Discord override; the env/default value applies again. Returns (old, new)."""
    f = get(name)
    old = getattr(cfg, f.attr, f.default)
    ov = load_overrides(log_dir); ov.pop(name, None)
    meta = _load_meta(log_dir); meta.pop(name, None)
    save_overrides(log_dir, ov, meta)
    val, _ = resolve(name, cfg, ov, environ)
    setattr(cfg, f.attr, val)
    sync_advisors(cfg, advisors)
    return old, val


def is_loud(f: Feature, new) -> bool:
    if f.extra.get("loud_when_on"):
        return f.loud and bool(new)
    return f.loud and (not f.loud_values or new in f.loud_values)


def consequence(f: Feature, cfg) -> str:
    try:
        return f.consequence.format(**{x.name: fmt(x, getattr(cfg, x.attr, x.default)) for x in registry()})
    except Exception:
        return f.consequence


# ----------------------------------------------------------------------------- rendering
def line(f: Feature, cfg, overrides: dict | None, environ=None) -> str:
    v = getattr(cfg, f.attr, f.default)
    return f"{'✅' if active(f, v) else '⬜'} {f.name} {fmt(f, v)} ({source(f.name, overrides, environ)})"


def summary(cfg, overrides: dict | None, group: str | None = None, environ=None) -> list[str]:
    """One line per feature, grouped ('**group**' header lines). e.g. '✅ giveback_pct 50% (discord)'."""
    out, last = [], None
    for f in registry():
        if group and f.group != group:
            continue
        if f.group != last:
            out.append(f"**{f.group}**"); last = f.group
        out.append(line(f, cfg, overrides, environ))
    return out


def summary_compact(cfg, overrides: dict | None, environ=None) -> list[str]:
    """One line per group for the boot / plan card: `guardian: giveback_pct=50%* · setup_stop=off` (* = set from Discord)."""
    groups: dict[str, list[str]] = {}
    for f in registry():
        mark = "*" if overrides and f.name in overrides else ""
        groups.setdefault(f.group, []).append(f"{f.name}={fmt(f, getattr(cfg, f.attr, f.default))}{mark}")
    return [f"{g}: " + " · ".join(items) for g, items in groups.items()]


def help_text(name: str, cfg, overrides: dict | None) -> str:
    f = get(name)
    rng = (f" · range {f.min:g}–{f.max:g}" if f.min is not None else "") + (f" · choices {'|'.join(f.choices)}" if f.choices else "")
    return (f"**{f.name}** (`{f.env}`, {f.type}{rng}) — {f.desc}\n"
            f"now: {line(f, cfg, overrides)} · default {fmt(f, f.default)} · "
            f"{'hot (safe with a position open)' if f.hot else 'NOT hot: refused with a position open unless force'}")
