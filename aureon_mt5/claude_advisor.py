"""v1.10.0 — Claude add-on: a second opinion at three moments. Claude NEVER places a trade.

  signal  (P / CROSS / RE at bar close)          → entry model    → TAKE / SKIP shown on the card
  pullback in a trade you placed                  → pullback model → HOLD / TIGHTEN / CLOSE
  23:00 IST                                       → review model   → CLAUDE REVIEW card

Modes (AUREON_CLAUDE): off (no calls) · review · advisory (shown, nothing applied) · manage (TIGHTEN, CLOSE-in-profit applied).
Safety:
  * Never blocks the poll: agents enqueue; ONE worker thread runs `claude -p`, one call at a time, with a timeout.
  * Fail closed: timeout / bad JSON / wrong decision / auth error / budget used / STALE → nothing acted on (v1.9.7 behaviour).
  * Actions run on the agent thread, after the guardian: TIGHTEN only through the guardian's `_move_sl` (never loosens,
    announced after MT5 confirms); CLOSE only in `manage`, only in profit, through the guardian's `_close`.
  * v2.0.0: both modes. ema2050 is asked at the ENTER bar (confirmed cross + pullback touch) and on pullbacks in a trade only —
    never at a raw CROSS or a MULTI CROSS. The /alert card gets the add-on verdict in review/advisory/manage (event ALERT).
    No account number, balance, login, webhook or token is ever put in a snapshot.
This module does not import broker.py and has no order placement (a test enforces it)."""
from __future__ import annotations

import json
import os
import queue
import threading
import time as _time
from collections import deque
from datetime import datetime, timedelta

import numpy as np

from . import advisor_board, telemetry
from .common import claude_cli
from .common.claude_cli import Budget, IST, ist_day
from .common.news import session_of
from .common.timeutil import bar_key, ist_str, now_server, server_str
from .notify import EVENT_COLOURS, field, footer as cfooter, position_fields, title as ctitle

MODES = ("off", "review", "advisory", "manage")
ADVISE_MODES = ("advisory", "manage")
STRATEGY = "ema5080"                       # key prefix for the ALL-symbol cards (kept for dedupe compatibility)
STRATEGIES = ("ema5080", "ema2050")        # v2.0.0: the add-on advises both modes
UNAVAILABLE_BACKOFF_S = 300
BAR_S = 300
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from .common.rules import RULES_PATH, read_rules   # v2.3.0: prompts/advisor_rules.md, shared with Codex
KIND_TO_EVENT = {"P": "P", "cross": "CROSS", "reentry": "RE", "enter": "ENTER", "pullback": "ENTER", "no-pullback": "ENTER"}
EMA_NAMES = {"ema5080": ("EMA 50", "EMA 80"), "ema2050": ("EMA 20", "EMA 50")}
TREND_PULLBACK = ("PULLBACK", "WEAKENING", "CHALLENGED")

READ_RULES = ("OPERATOR READ (/claude-read): the operator asks what to do RIGHT NOW on the latest closed bar. Nothing you say is "
              "applied — the guardian owns the SL and the operator places or closes trades. Answer with one JSON object and nothing "
              "else: {\"decision\": \"HOLD|EXIT|ENTER|SKIP\", \"side\": \"BUY|SELL|null\", \"confidence\": \"low|medium|high\", "
              "\"rationale\": \"<= 20 words\", \"risks\": [...], \"evidence\": [...]}. If the SNAPSHOT has a position "
              "(entry/sl/open_profit present): decide HOLD or EXIT for THAT position only (side = its side). Never TIGHTEN or move the "
              "SL. If there is no position: decide ENTER (side required, only when this mode's rules are met on this bar and "
              "news/session/governor/chop allow) or SKIP. SKIP is the default when in doubt; a flat day is not a mistake.")


ZL_RULES = ("ZL block = lower-lag view of the same EMAs; use it to judge whether the P is early or late and whether a flip is "
            "real (ADX rising with DI in the trade's favour = alive). It does not override the mode's rules.")
REGIME_RULES = "REGIME block = the bot's own read of whipsaw; in CHOPPY/DEAD default to SKIP and say why."
OPEN_RULES = ("OPEN READ: the operator just took this trade on the signal in the SNAPSHOT. Rate it: decision HOLD or EXIT, "
              "p_win 0–1, risks. HOLD is the default; EXIT only if the entry breaks a rule of this mode.")
DD_RULES = ("DRAWDOWN READ: the trade is under water but the setup is still alive by the rules. Is this a pullback or a failure? "
            "HOLD unless you see structure the rules do not. EXIT means take this loss now rather than wait for the stop at −{cap}.")
FLIP_RULES = ("FLIP HELD: an opposite P formed but the structure did not confirm it, so the guardian kept the trade "
              "(flip_evidence lists what did confirm). Decide HOLD or EXIT for this position.")
GUARDIAN_RULES = ("GUARDIAN block = what the bot will do on its own: `next_exit` is the closest exit rule and its distance. HOLD "
                  "means hold THROUGH that rule; EXIT means agree with it or go earlier. Never ask for the SL to be moved away.")
PRE_EXIT_RULES = ("PRE-EXIT READ: the guardian is about to close this trade by rule `{rule}` ({detail}). The broker stop stays where it "
                  "is regardless. Answer HOLD only if the structure says the rule is reacting to noise (cite evidence); otherwise "
                  "EXIT. A deferral is one bar; at most {max_defer}. Deferrals used so far: {used}.")
READ_TRIGGERS = ("open", "drawdown", "opposite P held", "pre_exit")


def read_prompt_text(base: str, extra: str = "") -> str:
    return base + "\n\n" + READ_RULES + (("\n\n" + extra) if extra else "")


REVIEW_PROMPT = ("You are reviewing one trading day of Aureon, an XAUUSD M5 EMA 50/80 assistant. A human placed every trade.\n"
                 "Below are the day's journal events (signals, decisions, guardian actions, exits, Claude verdicts).\n"
                 "Write a plain-text review, max 150 words, no code fences: 1) what worked, 2) what cost points, "
                 "3) one concrete thing to watch tomorrow. Never invent data not in the events.\n\nEVENTS:\n")


def normalise_mode(m: str | None) -> str:
    m = (m or "off").strip().lower()
    return m if m in MODES else "off"


def _side_word(side: str | None) -> str | None:
    return {"LONG": "BUY", "SHORT": "SELL", "BUY": "BUY", "SELL": "SELL"}.get((side or "").upper())


# ============================================================================= snapshot (built on the agent thread)
# v2.5.0: context_pack.pack is the canonical builder; build_snapshot is kept as a thin backwards-compat wrapper
# New callers should use context_pack.pack directly.
def build_snapshot(agent, event: str, side: str, bar_t: int, df, *, position: dict | None = None, st: dict | None = None,
                   kind: str | None = None) -> dict:
    """Only market/strategy data the bot already has. Never account, balance, login, webhook or token."""
    cf, cs = agent.S.ema_cols
    off = agent.off
    close = float(df["close"].iloc[-1]); e50 = float(df[cf].iloc[-1]); e80 = float(df[cs].iloc[-1])
    rng = (df["high"] - df["low"]).to_numpy()
    avg_range = float(rng[-21:-1].mean()) if len(rng) > 21 else float(rng.mean()) if len(rng) else 0.0
    atr20 = float(rng[-20:].mean()) if len(rng) else 0.0
    f_arr = df[cf].to_numpy(); s_arr = df[cs].to_numpy(); t_arr = df["time"].to_numpy(); sgn = np.sign(f_arr - s_arr)
    cross_bars = [k for k in range(1, len(df)) if sgn[k] != 0 and sgn[k - 1] != 0 and sgn[k] != sgn[k - 1]]
    day_start = int(datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + off * 3600)
    crosses_today = sum(1 for k in cross_bars if t_arr[k] >= day_start)
    bars_since_cross = (len(df) - 1 - cross_bars[-1]) if cross_bars else None
    confirm_state = None
    res = getattr(agent, "last_res", None)
    if agent.S.name == "ema2050" and res is not None and res.get("bar_states") and len(res["bar_states"]) == len(df):
        raw = str(res["bar_states"][-1]["confirm"])
        # v2.3.3: words only — the detector's own strings carry its thresholds ("bar 2 of 3 · gap 1.20 < 1.5")
        confirm_state = raw if raw.startswith("confirmed") else ("no cross" if raw == "no cross" else "not confirmed")
    s_dir = 1 if _side_word(side) == "BUY" else -1 if _side_word(side) == "SELL" else 0
    h6 = float(df["high"].iloc[-6:].max()); l6 = float(df["low"].iloc[-6:].min())
    swing_against = round((h6 - close) if s_dir > 0 else (close - l6) if s_dir < 0 else max(h6 - close, close - l6), 2)
    day_pnl, trades_today = _day_stats(agent)
    last = df.iloc[-30:]
    bars = [[ist_str(int(r.time), off, "%H:%M"), round(float(r.open), 2), round(float(r.high), 2), round(float(r.low), 2),
             round(float(r.close), 2)] for r in last.itertuples()]
    # whipsaw — same definition as the detector (read-only use of its constants)
    try:
        from .strategies.ema5080.journeys import rules_for
        rules = rules_for(agent.symbol); wz_bars, wz_flips = rules.wz_bars, rules.wz_flips
    except Exception:
        wz_bars, wz_flips = 12, 3
    side_arr = (df["close"] - df[cf]).to_numpy()[-(wz_bars + 1):]
    sg = [1 if x > 0 else -1 if x < 0 else 0 for x in side_arr]
    flips = sum(1 for a, b in zip(sg[:-1], sg[1:]) if a != b)
    now = int(_time.time())
    upcoming = [r for r in (agent.news or ()) if r >= now]
    fast_name, slow_name = EMA_NAMES.get(agent.S.name, (f"EMA {agent.S.fast}", f"EMA {agent.S.slow}"))
    snap = {
        "event": event, "symbol": agent.symbol, "mode": agent.S.name, "side": _side_word(side),
        "ema_names": {"fast": fast_name, "slow": slow_name},
        "bar_time_ist": ist_str(bar_t, off, "%Y-%m-%d %H:%M"), "bar_time_server": server_str(bar_t, "%H:%M"), "close": round(close, 2),
        "ema_fast": round(e50, 2), "ema_slow": round(e80, 2), "gap": round(e50 - e80, 2), "avg_range": round(avg_range, 2),
        # v2.0.0 quantified context (both modes)
        "crosses_today": crosses_today, "bars_since_cross": bars_since_cross, "confirm_state": confirm_state,
        "dist_to_fast_ema_pts": round(close - e50, 2), "swing_against_last6_pts": swing_against, "atr20": round(atr20, 2),
        "day_pnl_pts": day_pnl, "trades_today": trades_today,
        "bars_ohlc_ist": bars, "trend_state": getattr(agent, "trend_state", None),
        "whipsaw": {"ema50_side_flips": flips, "window_bars": wz_bars, "zone": flips >= wz_flips},
        "htf_context": (getattr(agent, "_htf_cache", None) or (None, None))[1],
        "spread_warning": None, "session": session_of(bar_t, off),
        "minutes_to_news": round((min(upcoming) - now) / 60) if upcoming else None,
        "streak": (getattr(agent, "_streak_cache", None) or (None, None))[1],
        "daily_limit_state": (getattr(agent, "_limit_cache", None) or (None, None))[1],
        "track_record": None,
    }
    try:
        snap["spread_warning"] = " · ".join(agent._warnings(df)) or None
    except Exception:
        pass
    if "zl50" in df:                                                   # v2.10.0 AUREON_ZL_CONTEXT
        from .common.zl import zl_block
        snap["zl"] = zl_block(df)
    reg = getattr(agent, "regime", None)                               # v2.10.0 regime: computed always, shown to Claude only
    if reg and getattr(agent.cfg, "regime_gate", "off") != "off":       # when the gate is show|block (defaults: prompt unchanged)
        snap["regime"] = {"state": reg.get("state"), "score": reg.get("score"), "reasons": list(reg.get("reasons") or [])[:5],
                          "since_bars": reg.get("since_bars"), "no_trade_day": bool(reg.get("no_trade_day"))}
    try:
        if kind:
            snap["track_record"] = agent._evidence_field(kind, side, bar_t)[0]["value"]
    except Exception:
        pass
    if agent.S.name == "ema5080":
        snap["ema50"], snap["ema80"] = snap["ema_fast"], snap["ema_slow"]        # v1.10 names kept for the 50/80 prompt
    else:
        snap["ema20"], snap["ema50"] = snap["ema_fast"], snap["ema_slow"]
    if position is not None:
        st = st or {}
        peak = float(st.get("peak", 0.0)); pts = float(position["points"]); sl = float(position["sl"]) if position.get("sl") else None
        long = position["direction"] == "long"
        peak_t = st.get("peak_t")
        snap.update({"entry": round(float(position["price_open"]), 2), "sl": sl,
                     "secured_level": float(st.get("secured", 0.0)), "peak": round(peak, 2),
                     "open_profit": round(pts, 2), "bars_held": int(st.get("bars", 0)),
                     # v2.0.0
                     "mae_so_far": round(float(st.get("mae", min(0.0, pts))), 2),
                     "retrace_from_peak_pts": round(max(0.0, peak - pts), 2),
                     "retrace_pct": round(100 * max(0.0, peak - pts) / peak, 1) if peak > 0 else None,
                     "bars_since_peak": int(max(0, (bar_t - int(peak_t)) // BAR_S)) if peak_t else None,
                     "closed_through_slow_ema": bool((close < e80) if long else (close > e80)),
                     "dist_to_sl": round(abs(float(position.get("current", close)) - sl), 2) if sl else None,
                     # v2.8.0 setup view (the guardian's per-ticket checks; None until computed)
                     "setup": st.get("setup"), "setup_alive": st.get("setup_alive"), "dist_to_cap": st.get("dist_to_cap")})
        intent = getattr(agent, "guardian_intent", None)                # v2.12.0 Part A: what the guardian is about to do
        if callable(intent):
            try:
                snap["guardian"] = intent(position, st, df, close, e50, e80, f_arr - s_arr)
            except Exception as e:
                telemetry.info(f"guardian_intent failed: {e!r}")
    return snap


def _day_stats(agent) -> tuple[float, int]:
    """(day_pnl_pts, trades_today) for the symbol from today's journal (IST day). Never raises."""
    try:
        start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        recs = [r for r in agent.journal.read(int(start.timestamp())) if r.get("symbol") == agent.symbol]
        pnl = {}; seen = set()
        for r in recs:
            if r.get("event") == "position_seen":
                seen.add(r.get("ticket"))
            if r.get("event") == "exit" and r.get("points") is not None:
                pnl[r.get("ticket")] = float(r["points"])
            if r.get("event") == "closed" and r.get("final_points") is not None:
                pnl[r.get("ticket")] = float(r["final_points"])
        return round(sum(pnl.values()), 2), len(seen | set(pnl))
    except Exception:
        return 0.0, 0


SNAPSHOT_KEEP = ("gap", "dist_to_fast_ema_pts", "swing_against_last6_pts", "atr20", "avg_range", "crosses_today", "bars_since_cross",
                 "confirm_state", "session", "trend_state", "minutes_to_news", "day_pnl_pts", "trades_today", "retrace_pct",
                 "retrace_from_peak_pts", "bars_since_peak", "mae_so_far", "closed_through_slow_ema", "dist_to_sl", "peak", "open_profit",
                 "setup", "setup_alive", "dist_to_cap", "trigger", "zl", "regime", "guardian", "pending_exit", "deferrals_used")


def _compact(snap: dict) -> dict:
    """The numeric/categorical snapshot fields journaled with every verdict (for the Saturday rule proposals). No bars, no prose."""
    out = {k: snap[k] for k in SNAPSHOT_KEEP if k in snap and snap[k] is not None}
    wz = snap.get("whipsaw") or {}
    if wz:
        out["whipsaw_flips"] = wz.get("ema50_side_flips"); out["whipsaw_zone"] = wz.get("zone")
    return out


# ============================================================================= advisor
class ClaudeAdvisor:
    # v2.3.0: one class, two advisors. CodexAdvisor (codex_advisor.py) overrides these and the CLI call; everything else —
    # triggers, snapshot, validation, fail-closed, STALE, fast-exit — is this code.
    SOURCE = "claude"                     # journal source · usage ledger logs/<SOURCE>_usage.jsonl · budget logs/<SOURCE>_budget.json
    LABEL = "Claude"
    TAG = "CLAUDE"
    ATTR = "claude"                       # agent.claude / agent.claude_any / agent.claude_inbox
    MODES = MODES
    ADVISE_MODES = ADVISE_MODES
    ENV = "AUREON_CLAUDE"

    def __init__(self, cfg, notify, journal, *, health_notify=None, caller=claude_cli.call, start: bool = True):
        self.cfg, self.notify, self.journal = cfg, notify, journal
        self.health = health_notify or notify
        self.mode = self.mode_of(cfg)
        self.caller = caller
        self.q: "queue.Queue[dict]" = queue.Queue(maxsize=20)
        self.budget = Budget(os.path.join(cfg.log_dir, f"{self.SOURCE}_budget.json"), self.max_calls)
        self._call_lock = threading.Lock()                  # one CLI call at a time, worker and commands alike
        self.fired: dict[tuple, float] = {}                 # (symbol, ticket) -> peak when the pullback verdict was requested
        self.dd_fired: dict[tuple, dict] = {}               # v2.8.0 (symbol, ticket) -> {"trough", "last_chance"} at the last drawdown read
        self._unavail_last: dict[str, float] = {}
        self._budget_day: str | None = None
        self.stats = {"login": None, "last_verdict": None, "last_error": "", "latencies": deque(maxlen=50), "dropped": 0,
                      # v2.0.1 /claude attachment card
                      "version": None, "bin_path": self._resolve_bin(), "version_error": "",
                      "calls_by_event": {"entry": 0, "pullback": 0, "alert": 0, "review": 0, "test": 0, "read": 0},
                      "fast_exit_cancels": 0, "detach_cancels": 0, "last_latency": None,
                      "reported_models": {}, "last_verdict_by_symbol": {}}
        self._worker: threading.Thread | None = None
        self.rules = read_rules()
        if start and self.mode != "off":
            self.start()

    @classmethod
    def mode_of(cls, cfg) -> str:
        return normalise_mode(getattr(cfg, "claude_mode", "off"))

    @classmethod
    def create(cls, cfg, notify, journal, **kw):
        """None when the mode is off → zero calls, zero behaviour change."""
        return None if cls.mode_of(cfg) == "off" else cls(cfg, notify, journal, **kw)

    # ------------------------------------------------------------------ per-advisor settings (CodexAdvisor overrides)
    @property
    def max_calls(self) -> int:
        return int(self.cfg.claude_max_calls)

    @property
    def entry_model(self) -> str:
        return self.cfg.claude_entry_model

    @property
    def pullback_model(self) -> str:
        return self.cfg.claude_pullback_model

    @property
    def review_model(self) -> str:
        return self.cfg.claude_review_model

    def _resolve_bin(self) -> str:
        return claude_cli.resolve_bin(self.cfg.claude_bin)

    def _version(self) -> dict:
        return claude_cli.version(self.cfg.claude_bin)

    def _invoke(self, prompt: str, model: str, event: str, *, free_text: bool = False, image=None):
        kw = {"image": image} if image is not None else {}       # v2.8.0: older fakes / callers take no image kwarg
        return self.caller(prompt, model, event=event, bin=self.cfg.claude_bin, workdir=self.cfg.claude_workdir,
                           timeout=self.cfg.claude_timeout, **kw)

    def wants_image(self, event: str) -> bool:
        """v2.8.0: attach the chart to this event's call (AUREON_CLAUDE_IMAGE_EVENTS). Codex never gets one."""
        return self.SOURCE == "claude" and str(event).upper() in tuple(getattr(self.cfg, "claude_image_events", ()) or ())

    def _inbox(self, agent):
        return getattr(agent, f"{self.ATTR}_inbox", None)

    def bound(self, agent) -> bool:
        return getattr(agent, self.ATTR, None) is self

    # ------------------------------------------------------------------ who gets hooks
    def advises(self, strategy) -> bool:
        return self.mode in self.ADVISE_MODES and getattr(strategy, "name", "") in STRATEGIES

    @property
    def manage(self) -> bool:
        return self.mode == "manage"

    # ------------------------------------------------------------------ v2.10.0 live mode change (/feature-set claude_mode)
    def set_mode(self, mode: str) -> str:
        """Switch off/review/advisory/manage at runtime (manage is derived from mode). Journals claude_mode_changed."""
        new = normalise_mode(mode) if self.SOURCE == "claude" else self.mode
        old = self.mode
        if new == old:
            return new
        self.mode = new
        if new != "off":
            self.start()
        try:
            self.journal.log(f"{self.SOURCE}_mode_changed", old=old, new=new)
        except Exception:
            pass
        return new

    def rebind(self, agents) -> None:
        """After a mode change: hooks on every agent this advisor now advises, none where it no longer does."""
        for ag in (agents.values() if isinstance(agents, dict) else agents or ()):
            setattr(ag, self.ATTR, self if self.advises(ag.S) else None)

    # ------------------------------------------------------------------ worker
    def start(self):
        if self._worker is None:
            self.refresh_version()
            self._worker = threading.Thread(target=self._loop, daemon=True, name=f"{self.SOURCE}-worker"); self._worker.start()

    # ------------------------------------------------------------------ v2.0.1 attachment: version, attach / detach, cancel
    def refresh_version(self) -> dict:
        """`<cli> --version` (cached; refreshed by /claude-test, /codex-test). Never raises."""
        try:
            v = self._version()
        except Exception as e:
            v = {"ok": False, "version": "", "path": self.stats["bin_path"], "error": f"{e!r}"[:160]}
        self.stats["version"] = v["version"] if v["ok"] else None
        self.stats["version_error"] = v["error"]; self.stats["bin_path"] = v["path"]
        if not v["ok"] and "not found" in v["error"]:
            self.stats["login"] = "NOT FOUND"
        return v

    def attachment(self, agent) -> dict:
        """{state, label, triggers} for one agent. state: ATTACHED | REVIEW | NOT_ATTACHED | DETACHED."""
        S = agent.S
        triggers = "ENTER bar, pullback-in-trade" if getattr(S, "name", "") == "ema2050" else "P / CROSS / RE, pullback-in-trade"
        if self.bound(agent) and self.mode in self.ADVISE_MODES:
            return {"state": "ATTACHED", "label": f"ATTACHED ({self.mode})", "triggers": triggers}
        if self.mode == "review":
            return {"state": "REVIEW", "label": "REVIEW (cards only, nothing applied)", "triggers": "23:00 IST review, /alert add-on line"}
        if self.mode == "off":
            return {"state": "DETACHED", "label": "DETACHED (mode=off)", "triggers": "—"}
        if not self.advises(S):
            return {"state": "NOT_ATTACHED", "label": "NOT ATTACHED (advisor.advises()=False)", "triggers": "—"}
        return {"state": "DETACHED", "label": f"DETACHED (/{self.SOURCE}-detach)", "triggers": "—"}

    def cancel_jobs(self, symbol: str, agent=None) -> int:
        """Drop queued jobs for `symbol` (and its inbox). Returns how many were cancelled. A call already in flight finishes;
        its result finds agent.<advisor> is None and is journaled only."""
        kept = []; n = 0
        while True:
            try:
                job = self.q.get_nowait()
            except queue.Empty:
                break
            if job.get("symbol") == symbol:
                n += 1
            else:
                kept.append(job)
        for job in kept:
            self.q.put_nowait(job)
        inbox = self._inbox(agent)
        while inbox is not None:
            try:
                inbox.get_nowait(); n += 1
            except queue.Empty:
                break
        self.stats["detach_cancels"] += n
        return n

    def detach(self, agent, by: str = "") -> int:
        n = self.cancel_jobs(agent.symbol, agent)
        setattr(agent, self.ATTR, None)
        self.journal.log(f"{self.SOURCE}_detach", symbol=agent.symbol, mode=agent.S.name, cancelled=n, by=by)
        return n

    def attach(self, agent, by: str = "") -> tuple[bool, str]:
        """Re-bind at runtime. False with the reason when the mode or advises() does not allow it."""
        if self.mode not in self.ADVISE_MODES:
            return False, (f"mode={self.mode} — {'review: cards only, nothing applied' if self.mode == 'review' else 'off'}; "
                           f"set {self.ENV}={'|'.join(self.ADVISE_MODES)} and restart")
        if not self.advises(agent.S):
            return False, f"advisor.advises()=False for {agent.S.name}"
        setattr(agent, self.ATTR, self)
        self.journal.log(f"{self.SOURCE}_attach", symbol=agent.symbol, mode=agent.S.name, by=by)
        return True, f"ATTACHED ({self.mode})"

    def _loop(self):
        while True:
            job = self.q.get()
            try:
                self.process(job)
            except Exception as e:
                telemetry.info(f"{self.SOURCE} worker error: {e!r}")

    def _enqueue(self, job: dict) -> bool:
        try:
            self.q.put_nowait(job); return True
        except queue.Full:
            self.stats["dropped"] += 1
            telemetry.info(f"{self.SOURCE} queue full — dropped {job.get('event')} {job.get('symbol')}")
            return False

    # ------------------------------------------------------------------ triggers (agent thread; enqueue only)
    def request_entry(self, agent, kind: str, side: str, bar_t: int, df, card_key: str | None = None, png: str | None = None) -> bool:
        event = KIND_TO_EVENT.get(kind)
        if not event or not self.advises(agent.S):
            return False
        snap = build_snapshot(agent, event, side, bar_t, df, kind=kind)
        job = {"type": "entry", "event": event, "symbol": agent.symbol, "side": side, "bar_t": bar_t,
               "off": agent.off, "snapshot": snap, "card_key": card_key, "agent": agent,
               "model": self.entry_model, "mode_name": agent.S.name, "display": agent.S.display_name}
        if png and self.wants_image(event):                       # v2.8.0: the card's chart goes with the call
            job["png"] = png
        return self._enqueue(job)

    def pullback_reason(self, agent, p: dict, st: dict, advisor_codes) -> str | None:
        long = p["direction"] == "long"
        ts = getattr(agent, "trend_state", None) or ""
        if ts.startswith("BULLISH" if long else "BEARISH") and ts.endswith(TREND_PULLBACK):
            return f"trend {ts}"
        peak = float(st.get("peak", 0.0))
        if peak >= 5 and p["points"] <= 0.6 * peak:
            return f"profit {p['points']:+.1f} ≤ 60% of peak +{peak:.1f}"
        if advisor_codes:
            return "advisor: SHOULD WE CLOSE? (" + ", ".join(advisor_codes) + ")"
        return None

    def maybe_pullback(self, agent, p: dict, st: dict, bar_t: int, df, advisor_codes=()) -> bool:
        """Once per pullback; re-armed only after a new peak."""
        if not self.advises(agent.S):
            return False
        key = (agent.symbol, p["ticket"]); peak = float(st.get("peak", 0.0))
        if key in self.fired and peak <= self.fired[key] + 1e-9:
            return False
        why = self.pullback_reason(agent, p, st, advisor_codes)
        if not why:
            return False
        self.fired[key] = peak
        snap = build_snapshot(agent, "pullback", p["direction"].upper(), bar_t, df, position=p, st=st)
        snap["trigger"] = why
        return self._enqueue({"type": "pullback", "event": "pullback", "symbol": agent.symbol, "side": p["direction"].upper(),
                              "ticket": p["ticket"], "bar_t": bar_t, "off": agent.off, "snapshot": snap, "agent": agent,
                              "open_profit": p["points"], "trigger": why, "model": self.pullback_model,
                              "mode_name": agent.S.name, "display": agent.S.display_name})

    def request_alert(self, agent, alert: dict, card: dict, df, bar_t: int) -> bool:
        """v2.0.0: the /alert card's second line — any mode but off (review included). Uses the entry model and the daily budget;
        a used budget skips silently (no card). Enqueue only; the poll never waits."""
        if self.mode == "off":
            return False
        meta = card.get("meta") or {}
        sug = meta.get("suggested_side")
        snap = build_snapshot(agent, "alert", sug or "", bar_t, df)                 # event="alert", side = the agent's side or None
        snap["alert"] = {"id": alert["id"], "price": alert["price"], "hit": alert.get("hit_price"), "side_hint": alert.get("side_hint"), "note": alert.get("note")}
        snap["agent_verdict"] = {"decision": meta.get("suggestion"), "side": sug, "reason": meta.get("reason")}
        return self._enqueue({"type": "alert", "event": "ALERT", "symbol": agent.symbol, "side": sug, "bar_t": bar_t, "off": agent.off,
                              "snapshot": snap, "card_key": card.get("key"), "agent": agent, "alert_id": alert["id"],
                              "agent_block": meta.get("agent_block") or meta.get("agent_verdict") or "", "agent_side": sug,
                              "title": card.get("title", ""), "model": self.entry_model, "mode_name": agent.S.name,
                              "display": agent.S.display_name})

    def forget(self, symbol: str, ticket):
        self.fired.pop((symbol, ticket), None)
        self.dd_fired.pop((symbol, ticket), None)

    # ------------------------------------------------------------------ v2.8.0 READs on a position (agent thread; enqueue only)
    def _request_read(self, agent, p: dict, st: dict, bar_t: int, df, *, trigger: str, extra: str, kind: str | None = None,
                      snap_extra: dict | None = None, card_key: str | None = None) -> bool:
        """One READ job on a live position. Claude only (Codex: no-op). The verdict is applied/shown on the agent thread by
        drain() → apply_pullback(), after the guardian ran for that poll."""
        if self.SOURCE != "claude" or not self.advises(agent.S):
            return False
        side = p["direction"].upper()
        snap = build_snapshot(agent, "READ", side, bar_t, df, position=p, st=st, kind=kind)
        snap["trigger"] = trigger
        snap.update(snap_extra or {})
        job = {"type": "read", "event": "READ", "symbol": agent.symbol, "side": side, "ticket": p["ticket"], "bar_t": bar_t,
               "off": agent.off, "snapshot": snap, "agent": agent, "open_profit": p["points"], "trigger": trigger, "extra": extra,
               "model": self.entry_model, "mode_name": agent.S.name, "display": agent.S.display_name, "card_key": card_key,
               "sl": p.get("sl") or None}
        if self.wants_image("READ"):
            job["df"] = df.copy()                               # the chart is rendered on the worker, never on the poll
        return self._enqueue(job)

    def request_flip_read(self, agent, p: dict, st: dict, bar_t: int, df, *, evidence) -> bool:
        """v2.8.0: the guardian held through an opposite P (structure did not confirm) — ask HOLD / EXIT."""
        return self._request_read(agent, p, st, bar_t, df, trigger="opposite P held", extra=FLIP_RULES,
                                  snap_extra={"flip_evidence": list(evidence or [])})

    def request_open_read(self, agent, p: dict, st: dict, bar_t: int, df, card_key: str | None = None) -> bool:
        """v2.8.0: one READ when a position is first seen. Shown only in every mode — never closes."""
        if not getattr(self.cfg, "claude_open_read", False):
            return False
        sig = getattr(agent, "latest_signal", None)
        kind = sig["kind"] if (sig and sig.get("direction") == p["direction"].upper() and sig.get("bar_time") == bar_t) else None
        return self._request_read(agent, p, st, bar_t, df, trigger="open", extra=OPEN_RULES, kind=kind, card_key=card_key)

    def request_pre_exit_read(self, agent, p: dict, st: dict, bar_t: int, df, *, rule: str, detail: str, show: bool = False,
                              job_id: str | None = None, deferral_n: int = 0) -> bool:
        """v2.12.0: the guardian is about to close by `rule`. show → journal only (the close already happened);
        defer → the verdict decides in drain(): EXIT / error → close now, HOLD → one more bar. Claude only."""
        if self.SOURCE != "claude" or not self.advises(agent.S):
            return False
        used = int(st.get("deferrals", 0))
        level = None
        try:
            g = agent.guardian_intent(p, st, df, float(df["close"].iloc[-1]), float(df[agent.S.ema_cols[0]].iloc[-1]),
                                      float(df[agent.S.ema_cols[1]].iloc[-1]))
            ne = g.get("next_exit") or {}
            level = ne.get("level") if ne.get("rule") == rule else None
        except Exception:
            pass
        extra_snap = {"pending_exit": {"rule": rule, "detail": detail, "level": level}, "deferrals_used": used}
        extra = PRE_EXIT_RULES.format(rule=rule, detail=detail, max_defer=int(getattr(self.cfg, "pre_exit_max_defer", 2)), used=used)
        side = p["direction"].upper()
        snap = build_snapshot(agent, "READ", side, bar_t, df, position=p, st=st)
        snap["trigger"] = "pre_exit"
        snap.update(extra_snap)
        job = {"type": "read", "event": "READ", "symbol": agent.symbol, "side": side, "ticket": p["ticket"], "bar_t": bar_t,
               "off": agent.off, "snapshot": snap, "agent": agent, "open_profit": p["points"], "trigger": "pre_exit", "extra": extra,
               "model": self.entry_model, "mode_name": agent.S.name, "display": agent.S.display_name, "card_key": None,
               "sl": p.get("sl") or None, "rule": rule, "deferral_n": deferral_n, "pre_exit_show": bool(show), "job_id": job_id}
        if self.wants_image("READ"):
            job["df"] = df.copy()
        return self._enqueue(job)

    def maybe_drawdown(self, agent, p: dict, st: dict, bar_t: int, df) -> bool:
        """v2.8.0: event-driven READs while the trade is under water on the CLOSE and the setup is still alive:
        first drawdown >= DD_READ_AT, each new trough >= DD_READ_STEP deeper, one last chance when <= 2 pts from the cap.
        At most DD_READS_PER_TRADE per ticket."""
        cfg = self.cfg
        if self.SOURCE != "claude" or not self.advises(agent.S) or float(getattr(cfg, "claude_dd_read_at", 0) or 0) <= 0:
            return False
        if st.get("setup_alive") is not True:
            return False
        s = 1 if p["direction"] == "long" else -1
        close = float(df["close"].iloc[-1])
        under = (float(p["price_open"]) - close) * s
        if under < cfg.claude_dd_read_at:
            return False
        if int(st.get("dd_reads", 0)) >= int(cfg.claude_dd_reads_per_trade):
            return False
        cap = float(st.get("cap_pts") or cfg.setup_max_dd)
        dist = float(st["dist_to_cap"]) if st.get("dist_to_cap") is not None else cap - under
        key = (agent.symbol, p["ticket"]); arm = self.dd_fired.get(key)
        last_chance = dist <= 2.0
        if arm is not None and not (under >= arm["trough"] + cfg.claude_dd_read_step or (last_chance and not arm["last_chance"])):
            return False
        self.dd_fired[key] = {"trough": max(under, arm["trough"] if arm else 0.0),
                              "last_chance": bool(last_chance or (arm or {}).get("last_chance"))}
        st["dd_reads"] = int(st.get("dd_reads", 0)) + 1
        extra = {"mae_so_far": round(float(st.get("mae", min(0.0, p["points"]))), 2), "bars_in_drawdown": int(st.get("bars_in_dd", 0)),
                 "dist_to_cap": round(dist, 2), "setup": st.get("setup"), "setup_alive": st.get("setup_alive"),
                 "drawdown_on_close": round(under, 2), "last_chance": last_chance}
        return self._request_read(agent, p, st, bar_t, df, trigger="drawdown", extra=DD_RULES.format(cap=f"{cap:g}"), snap_extra=extra)

    def _process_read(self, job: dict):
        """Worker: chart (optional) → READ call → keep HOLD/EXIT → agent inbox (applied after the guardian)."""
        png = None
        if job.get("df") is not None:
            png = self._render_chart(job["agent"], job["df"], job["side"], f"claude-{job['trigger']}".replace(" ", "-"), job.get("sl"))
        job.pop("df", None)
        if png:
            job["png"] = png
        res = self._call(self.read_prompt(job["snapshot"], job.get("extra", "")), job["model"], "READ", image=png)
        pre_exit = job.get("trigger") == "pre_exit" and not job.get("pre_exit_show")
        if res is None:
            self._journal(job, None, latency=0.0, acted=False, stale=False, error="budget used")
            if pre_exit:                                       # v2.12.0: the guardian closes by its rule now
                job["verdict"] = None; job["error"] = "budget used"
                inbox = self._inbox(job["agent"])
                if inbox is not None:
                    inbox.put(job)
            return
        v = res.verdict if res.ok else None
        err = res.error or "no verdict"
        if v is not None and v["decision"] not in ("HOLD", "EXIT"):
            err = f"{v['decision']} is not valid on an open position"; v = None
        if v is None:
            self._journal(job, None, latency=res.latency_s, acted=False, stale=False, error=err)
            if pre_exit:
                job["verdict"] = None; job["error"] = err
                inbox = self._inbox(job["agent"])
                if inbox is not None:
                    inbox.put(job)
            return
        v["side"] = _side_word(job["side"])
        self.stats["last_verdict"] = f"{job['symbol']} READ ({job['trigger']}) {v['decision']} · {v['confidence']} · {datetime.now(IST):%H:%M} IST"
        self.stats["last_verdict_by_symbol"][job["symbol"]] = f"{datetime.now(IST):%H:%M} IST {v['decision']}"
        job["latency"] = res.latency_s; job["verdict"] = v
        inbox = self._inbox(job["agent"])
        if inbox is not None:
            inbox.put(job)

    # ------------------------------------------------------------------ the call (worker / command threads)
    def _call(self, prompt: str, model: str, event: str, *, kind_override: str | None = None,
              free_text: bool = False, image: str | None = None) -> claude_cli.CliResult | None:
        """None = budget used (no call made)."""
        if not self.budget.take():
            self._budget_card(); return None
        with self._call_lock:
            res = self._invoke(prompt, model, event, free_text=free_text, image=image)
        from .claude_usage import append as append_usage
        try:
            append_usage(self.cfg.log_dir, kind_override or {"pullback": "pullback", "ALERT": "alert", "review": "review", "test": "test", "READ": "read"}.get(event, "entry"),
                         res, source=self.SOURCE)
        except OSError as exc:
            telemetry.info(f"{self.LABEL} usage ledger unavailable: {exc!r}")
        self.stats["latencies"].append(res.latency_s); self.stats["last_latency"] = res.latency_s
        kind = {"pullback": "pullback", "ALERT": "alert", "review": "review", "test": "test", "READ": "read"}.get(event, "entry")
        self.stats["calls_by_event"][kind] = self.stats["calls_by_event"].get(kind, 0) + 1
        if getattr(res, "model", ""):
            self.stats["reported_models"][kind] = res.model
        if res.auth_error:
            self.stats["login"] = "NOT FOUND" if "not found" in (res.error or "") else "FAILED"
        elif res.ok or res.raw:
            self.stats["login"] = "OK"
        if not res.ok:
            self.stats["last_error"] = res.error
            if res.auth_error:
                self._unavailable(event, res.error)
        return res

    def prompt_for(self, snapshot: dict) -> str:
        names = snapshot.get("ema_names") or EMA_NAMES.get(snapshot.get("mode", ""), ("fast EMA", "slow EMA"))
        fast, slow = (names["fast"], names["slow"]) if isinstance(names, dict) else names
        head = (f"MODE: {snapshot.get('mode', '?')} — fast EMA = {fast}, slow EMA = {slow}. Apply the section for this mode; "
                f"ema_fast/ema_slow in the snapshot are {fast}/{slow}.\n\n")
        extra = "".join(f"\n\n{t}" for k, t in (("zl", ZL_RULES), ("regime", REGIME_RULES), ("guardian", GUARDIAN_RULES))
                        if snapshot.get(k))
        return head + self.rules.strip() + extra + "\n\nSNAPSHOT:\n" + json.dumps(snapshot, default=str)

    def read_prompt(self, snapshot: dict, extra: str = "") -> str:
        """v2.8.0: the advisor prompt + READ_RULES (+ a trigger paragraph: OPEN_RULES / DD_RULES / flip-held)."""
        return read_prompt_text(self.prompt_for(snapshot), extra)

    def _render_chart(self, agent, df, side: str | None, title_extra: str, stop: float | None = None) -> str | None:
        """A chart of the frozen frame for a READ call. Any failure → None (never blocks the read)."""
        try:
            from .common.context_chart import context_chart
            return context_chart(df, agent.symbol, agent.S.display_name, agent.S.fast, agent.S.slow, agent.S.ema_cols, agent.off,
                                 agent.chart_dir, marker={"index": len(df) - 1, "side": (side or "LONG").upper(), "label": "READ",
                                                          "stop": stop}, bars=120, title_extra=title_extra)
        except Exception as e:
            telemetry.info(f"{self.SOURCE} read chart failed: {e!r}")
            return None

    # ------------------------------------------------------------------ v2.8.0 /claude-read (Discord command thread, blocking)
    def read_now(self, agent) -> dict:
        """What to do RIGHT NOW on the latest closed bar. Shown only: nothing is applied, no broker call (positions come from the
        agent's cached snapshot). Returns {ok, verdict, error, png, snapshot, position, latency, stale, budget_used}."""
        out = {"ok": False, "verdict": None, "error": "", "png": None, "snapshot": None, "position": None, "latency": 0.0,
               "stale": False, "budget_used": False}
        frame = getattr(agent, "last_df", None)
        if frame is None or len(frame) == 0:
            out["error"] = "no cached closed-bar frame yet"; return out
        df = frame.copy()
        bar_t = int(df["time"].iloc[-1])
        cache = getattr(agent, "snapshot", None) or {}
        position = next(iter(cache.get("positions") or []), None)
        st = None
        if position is not None:
            states = cache.get("state") or {}
            st = states.get(position["ticket"], states.get(str(position["ticket"]), {}))
        side = position["direction"].upper() if position else ""
        snap = build_snapshot(agent, "READ", side, bar_t, df, position=position, st=st)
        snap["position_open"] = position is not None
        snap["ticket"] = position["ticket"] if position else None
        out["snapshot"] = snap; out["position"] = position
        if self.budget.used() >= self.max_calls:
            out["budget_used"] = True; out["error"] = "daily budget used — no call made"; return out
        png = self._render_chart(agent, df, side or None, "claude-read", (position or {}).get("sl") or None) if self.wants_image("READ") else None
        out["png"] = png
        res = self._call(self.read_prompt(snap), self.entry_model, "READ", kind_override="read", image=png)
        if res is None:
            out["budget_used"] = True; out["error"] = "daily budget used — no call made"; return out
        out["latency"] = res.latency_s
        out["stale"] = now_server(agent.off) >= bar_t + 2 * BAR_S
        v = res.verdict if res.ok else None
        err = "" if v is not None else (res.error or "no verdict")
        if v is not None:
            allowed = ("HOLD", "EXIT") if position else ("ENTER", "SKIP")
            if v["decision"] not in allowed:
                err = (f"{v['decision']} is not valid " + ("with an open position" if position else "without a position"))
                v = None
            elif position:
                v["side"] = _side_word(side)                     # HOLD / EXIT are about THAT position
        out.update(ok=v is not None, verdict=v, error=err)
        try:
            self.journal.log(f"{self.SOURCE}_read", source=self.SOURCE, symbol=agent.symbol, bar=bar_t,
                             decision=(v or {}).get("decision"), side=(v or {}).get("side"), confidence=(v or {}).get("confidence"),
                             reason=(v or {}).get("reason"), risks=(v or {}).get("risks") or [], evidence=(v or {}).get("evidence") or [],
                             error=err or None, stale=bool(out["stale"]), image=bool(res.image or png), ticket=snap["ticket"],
                             open_profit=snap.get("open_profit"), acted=False, snapshot_fields=_compact(snap))
        except Exception as e:
            telemetry.info(f"{self.SOURCE} read journal failed: {e!r}")
        return out

    def process(self, job: dict):
        if job["type"] == "review":
            self.daily_review(job.get("day"), force=job.get("force", False)); return
        if job["type"] == "read":
            self._process_read(job); return
        if job["type"] == "repair":                             # v2.10.0: serialised with every other CLI call; daily budget
            res = job["repairer"].propose(job["n"], lock=self._call_lock, budget=self.budget)
            if res.get("ok") and job.get("after"):                  # v2.10.1: AUREON_REPAIR_AGENT=auto guards + apply
                job["after"](res)
            return
        res = self._call(self.prompt_for(job["snapshot"]), job["model"], job["event"], image=job.get("png"))
        if res is None:
            self._journal(job, None, latency=0.0, acted=False, stale=False, error="budget used")
            if job["type"] == "alert":
                self.notify.edit_card(job.get("card_key") or "", {"CLAUDE VERDICT": "— budget used"})   # silent: no card, no call
            return
        v = res.verdict if res.ok else None
        if job["type"] == "alert":
            self._journal(job, v, latency=res.latency_s, acted=False, stale=False, error=("" if v is not None else (res.error or "no verdict")))
            if v is not None:
                self.stats["last_verdict"] = f"{job['symbol']} ALERT {v['decision']} · {v['confidence']} · {datetime.now(IST):%H:%M} IST"
            job["latency"] = res.latency_s
            self._alert_line(job, v, error=(res.error or "unavailable") if v is None else "")
            return
        if v is not None and job["type"] == "entry":
            want = _side_word(job["side"])
            if v["side"] not in (None, want):
                v, res.error = None, f"side {v['side']} does not match the {job['side']} signal"
            else:
                v["side"] = want
        if v is None:                                    # fail closed: card unchanged, nothing acted on
            self._journal(job, None, latency=res.latency_s, acted=False, stale=False, error=res.error or "no verdict")
            return
        self.stats["last_verdict"] = f"{job['symbol']} {job['event']} {v['decision']} · {v['confidence']} · {datetime.now(IST):%H:%M} IST"
        self.stats["last_verdict_by_symbol"][job["symbol"]] = f"{datetime.now(IST):%H:%M} IST {v['decision']}"
        job["latency"] = res.latency_s
        if job["type"] == "entry":
            stale = now_server(job["off"]) >= job["bar_t"] + 2 * BAR_S
            self._journal(job, v, latency=res.latency_s, acted=False, stale=stale)
            self._entry_card(job, v, stale)
        else:
            job["verdict"] = v
            agent = job["agent"]
            inbox = self._inbox(agent)
            if inbox is not None:
                inbox.put(job)                           # applied/shown on the agent thread, after the guardian

    # ------------------------------------------------------------------ verdict on a position (AGENT thread)
    def drain(self, agent, positions: list[dict]):
        inbox = self._inbox(agent)
        while inbox is not None:
            try:
                job = inbox.get_nowait()
            except queue.Empty:
                return
            try:
                p = next((x for x in positions if x["ticket"] == job["ticket"]), None)
                if p is not None and p["ticket"] in getattr(agent, "closed_by_aureon", set()):
                    p = None                             # the guardian already closed it: hard exits win
                if job.get("trigger") == "pre_exit":
                    self.apply_pre_exit(agent, job, p, agent.state.get(job["ticket"], {}) if p else {})
                    continue
                self.apply_pullback(agent, job, p, agent.state.get(job["ticket"], {}) if p else {})
            except Exception as e:
                telemetry.failure(self.health, self.journal, title=f"AUREON {self.TAG} ERROR", key=f"{agent.S.name}:{agent.symbol}:{self.TAG}_ERROR:apply",
                                  mode=agent.S.name, symbol=agent.symbol, action=f"{self.SOURCE} pullback verdict", exc=e)

    def apply_pre_exit(self, agent, job: dict, p: dict | None, st: dict) -> str:
        """v2.12.0 (AGENT thread): EXIT / no verdict → close now by the original rule; HOLD → one more bar (deferrals += 1).
        show mode or a moot ask (closed / cancelled / superseded) → journaled only. Never moves the SL."""
        v = job.get("verdict")
        pe = st.get("pending_exit") if p is not None else None
        if job.get("pre_exit_show") or p is None or not pe or pe.get("job_id") != job.get("job_id"):
            if v is not None:
                self._journal(job, v, latency=job.get("latency", 0.0), acted=False, stale=False,
                              note="pre-exit read · shown with the close" if job.get("pre_exit_show") else "pre-exit ask no longer pending")
            return "moot"
        if v is None or v["decision"] == "EXIT":
            outcome = "exit" if v is not None else ("error" if job.get("error") else "timeout")
            agent.finish_pending_exit(p, st, outcome)
            if v is not None:
                self._journal(job, v, latency=job.get("latency", 0.0), acted=True, stale=False, note=f"closed by {job['rule']} (Claude EXIT)")
            return outcome
        st["deferrals"] = int(st.get("deferrals", 0)) + 1
        pe["state"] = "held"
        n, mx = st["deferrals"], int(getattr(self.cfg, "pre_exit_max_defer", 2))
        agent.journal.log("exit_deferred", symbol=agent.symbol, mode=agent.S.name, ticket=p["ticket"], rule=job["rule"],
                          reason=v.get("reason"), p_win=v.get("p_win"), deferral_n=n, points=p["points"], price=p.get("current"))
        agent.note_action(st, "exit_deferred", f"{job['rule']} · Claude HOLD {n}/{mx}")
        self._journal(job, v, latency=job.get("latency", 0.0), acted=True, stale=False, note=f"{job['rule']} deferred {n}/{mx}")
        try:
            agent._save_state_now()
        except Exception:
            pass
        self.notify.send(agent.key("EXIT_DEFERRED", f"{p['ticket']}-{n}"),
                         ctitle(agent.symbol, f"EXIT DEFERRED · {job['rule']} · Claude HOLD · {n}/{mx}", p["direction"]),
                         [f"\"{v.get('reason') or ''}\"", "the broker stop is unchanged · the rule is checked again on the next close"],
                         color=EVENT_COLOURS["flip"], footer=cfooter(agent.S.display_name, "pre-exit"))
        return "hold"

    def apply_pullback(self, agent, job: dict, p: dict | None, st: dict) -> bool:
        """Returns True when an action was applied in MT5. Order: MT5 → confirm → state → journal → Discord."""
        v = job["verdict"]; dec = v["decision"]
        stale = now_server(agent.off) >= job["bar_t"] + 2 * BAR_S
        acted = False; note = ""
        if p is None:
            note = "position no longer open — nothing to do"
            self.stats["fast_exit_cancels"] += 1                 # v2.0.1: the trade closed before the verdict arrived
            self.journal.log(f"{self.SOURCE}_fast_exit_cancel", symbol=agent.symbol)
        elif stale:
            note = "STALE — arrived after the next bar closed; not acted"
        elif dec == "HOLD":
            note = "hold — guardian keeps managing by its rules"
        elif job.get("trigger") == "open":
            note = f"open read — {dec} shown only, never applied"
        elif not self.manage:
            note = f"advisory mode — {dec} shown, not applied"
        elif dec == "TIGHTEN":
            tt = float(v["tighten_to"]); long = p["direction"] == "long"; sl = float(p.get("sl") or 0.0)
            tighter = (not sl or (tt > sl if long else tt < sl))
            sane = (tt < p["current"]) if long else (tt > p["current"])
            if not tighter:
                note = f"ignored — {tt:.2f} is not tighter than the live SL {sl:.2f}"
            elif not sane:
                note = f"ignored — {tt:.2f} is on the wrong side of price {p['current']:.2f}"
            elif agent._move_sl(p, tt, "CLAUDE TIGHTEN"):          # guardian's safe path: never loosens, retcode checked
                acted = True
                st["secured"] = max(st.get("secured", 0.0), agent._locked({**p, "sl": tt}))
                note = f"SL → {tt:.2f} (MT5 confirmed)"
            else:
                note = "MT5 rejected the SL — see the failure alert; nothing changed"
        elif dec == "CLOSE":
            if p["points"] <= 0:
                note = f"not in profit ({p['points']:+.2f}) — CLOSE shown, not acted"
            else:
                acted = self._guardian_close(agent, job, p, st, f"Claude: {v['reason'] or 'trend turning'}", "CLAUDE_CLOSED", "claude_close")
                note = "closed in profit (MT5 confirmed)" if acted else "close failed — see the failure alert"
        elif dec == "EXIT":                                              # v2.8.0 READ verdicts
            cap = float(st.get("cap_pts") or getattr(self.cfg, "setup_max_dd", 10.0))
            if p["points"] > 0:
                acted = self._guardian_close(agent, job, p, st, f"Claude: {v['reason'] or 'exit'}", "CLAUDE_CLOSED", "claude_close")
                note = "closed in profit (MT5 confirmed)" if acted else "close failed — see the failure alert"
            elif job.get("trigger") == "drawdown" and getattr(self.cfg, "claude_exit_losing", False) and p["points"] > -cap:
                acted = self._guardian_close(agent, job, p, st, f"Claude: cut loss {p['points']:+.1f} — {v['reason']}", "CLAUDE_CUT",
                                             "claude_cut_loss")
                note = f"loss cut at {p['points']:+.1f} (MT5 confirmed)" if acted else "close failed — see the failure alert"
            else:
                note = "EXIT shown, not acted (losing)"
        self._journal(job, v, latency=job.get("latency", 0.0), acted=acted, stale=stale, note=note)
        if job.get("trigger") == "open" and p is not None and job.get("card_key"):
            if self.notify.edit_card(job["card_key"], {f"{self.TAG} VERDICT": f"{advisor_board.verdict_line(self.SOURCE, v, stale)} · "
                                                                               f"p_win {v.get('p_win') if v.get('p_win') is not None else '—'}"}):
                return acted                                    # the ask card carries the open read
        self._pullback_card(agent, job, v, p, st, note, acted, stale)
        return acted

    def _guardian_close(self, agent, job, p, st, reason: str, key_name: str, journal_reason: str) -> bool:
        """The ONLY close path for a verdict: the guardian's own _close (MT5 → confirm → journal → card). True when closed."""
        title = {"claude_close": "CLAUDE CLOSED", "claude_cut_loss": "CLAUDE CUT LOSS"}.get(journal_reason, "CLAUDE CLOSED")
        agent._close(p, reason, title, agent.key(key_name, f"{p['ticket']}-{bar_key(job['bar_t'], agent.off)}"), st, journal_reason, agent.last_bar)
        return p["ticket"] in getattr(agent, "closed_by_aureon", set())

    # ------------------------------------------------------------------ daily review
    def review_events(self, day: str) -> list[dict]:
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=IST)
        recs = self.journal.read(int(start.timestamp()), int((start + timedelta(days=1)).timestamp()))
        keep = ("t", "event", "symbol", "side", "signal_kind", "decision", "reason", "points", "price", "secured", "peak", "level",
                "best", "against", "why", "claude_event", "codex_event", "source", "confidence", "acted", "stale", "trigger")
        out = []
        for r in recs:
            if r.get("event") in ("error",):
                continue
            row = {k: r[k] for k in keep if k in r and r[k] not in (None, "")}
            row["t"] = datetime.fromtimestamp(r["t"], tz=IST).strftime("%H:%M")
            out.append(row)
        return out[-300:]

    def daily_review(self, day: str | None = None, *, force: bool = True) -> str:
        """Runs on the worker or a command thread (never the event loop / poll). Returns the text it posted.
        force=False (the 23:00 schedule): skipped when the journal already has that day's review (restart-safe)."""
        day = day or ist_day()
        if not force and any(r.get("event") == f"{self.SOURCE}_review" and r.get("day") == day
                             for r in self.journal.read(int(_time.time()) - 2 * 86400)):
            return f"already reviewed {day}"
        events = self.review_events(day)
        if not events:
            text = f"no journal events on {day}"
            self.notify.card(f"AUREON · MT5 · {self.TAG} REVIEW · {day}", text, color=EVENT_COLOURS["info"], footer=f"Aureon MT5 · {self.LABEL} review")
            return text
        res = self._call(REVIEW_PROMPT + json.dumps(events, default=str), self.review_model, "review", free_text=True)
        if res is None:
            return "budget used — no review today"
        if not res.ok or not res.text:
            return f"review unavailable: {res.error or 'empty reply'}"
        text = res.text[:3800]
        self.journal.log(f"{self.SOURCE}_review", source=self.SOURCE, day=day, model=self.review_model or "CLI default",
                         latency_s=round(res.latency_s, 1), events=len(events))
        self.notify.card(f"AUREON · MT5 · {self.TAG} REVIEW · {day}", text, color=EVENT_COLOURS["info"],
                         fields=[field("Events", len(events)), field("Model", self.review_model or "CLI default"), field("Latency", f"{res.latency_s:.0f} s")],
                         footer=f"Aureon MT5 · {self.LABEL} review · advisory only")
        return text

    def request_review(self, day: str | None = None, force: bool = False) -> bool:
        return self._enqueue({"type": "review", "event": "review", "symbol": "ALL", "day": day, "force": force})

    def test_call(self) -> claude_cli.CliResult | None:
        self.refresh_version()
        return self._call("Reply only: OK", self.review_model, "test", free_text=True)

    # ------------------------------------------------------------------ status for /claude
    def status(self) -> dict:
        lats = list(self.stats["latencies"])
        return {"mode": self.mode, "login": self.stats["login"] or "unknown — run /claude-test",
                "calls": self.budget.used(), "limit": self.max_calls, "last_verdict": self.stats["last_verdict"] or "—",
                "avg_latency": (sum(lats) / len(lats)) if lats else None, "queue": self.q.qsize(),
                "last_error": self.stats["last_error"] or "—", "dropped": self.stats["dropped"],
                "models": f"entry {self.entry_model or 'CLI default'} · pullback {self.pullback_model or 'CLI default'} · review {self.review_model or 'CLI default'}",
                # v2.0.1
                "version": self.stats["version"], "version_error": self.stats["version_error"], "bin_path": self.stats["bin_path"],
                "bin": self._bin_name(), "calls_by_event": dict(self.stats["calls_by_event"]), "fast_exit_cancels": self.stats["fast_exit_cancels"],
                "detach_cancels": self.stats["detach_cancels"], "last_latency": self.stats["last_latency"],
                "reported_models": dict(self.stats["reported_models"]), "last_verdict_by_symbol": dict(self.stats["last_verdict_by_symbol"]),
                "model_ids": {"entry": self.entry_model, "pullback": self.pullback_model, "review": self.review_model}}

    def _bin_name(self) -> str:
        return self.cfg.claude_bin

    # ------------------------------------------------------------------ journal + cards
    def _journal(self, job, v, *, latency, acted, stale, error="", note=""):
        try:
            self.journal.log(f"{self.SOURCE}_verdict", source=self.SOURCE, **{f"{self.SOURCE}_event": job["event"]}, symbol=job["symbol"], model=job.get("model"),
                             latency_s=round(latency, 1), decision=(v or {}).get("decision"), side=(v or {}).get("side"),
                             confidence=(v or {}).get("confidence"), reason=(v or {}).get("reason"),
                             tighten_to=(v or {}).get("tighten_to"), my_action=None, acted=bool(acted), stale=bool(stale),
                             bar=job.get("bar_t"), ticket=job.get("ticket"), open_profit=job.get("open_profit"),
                             trigger=job.get("trigger"), error=error or None, note=note or None,
                             evidence=(v or {}).get("evidence") or [], alert_id=job.get("alert_id"),
                             image=bool(job.get("png")), p_win=(v or {}).get("p_win"),
                             snapshot_fields=_compact(job.get("snapshot") or {}),
                             **({"rule": job.get("rule"), "deferral_n": job.get("deferral_n")} if job.get("trigger") == "pre_exit" else {}))
        except Exception as e:
            telemetry.info(f"{self.SOURCE} journal failed: {e!r}")

    def _alert_line(self, job, v, error: str = ""):
        """v2.0.2: EDIT the ALERT REACHED card's <TAG> VERDICT block (AGREES / DISAGREES with the agent). If the card cannot be
        edited (webhook only, expired), post a follow-up 'ALERT VERDICT · <id>' card repeating the AGENT and advisor blocks.
        The agent block always exists first — the hit card carried it before this call was even queued.
        v2.3.0: with both advisors on the card, the second to answer adds ADVISORS: AGREE / SPLIT."""
        from .alerts import claude_verdict_block
        blk = claude_verdict_block(v, job.get("agent_side"), error=error, name=self.TAG)
        agent = job.get("agent"); store = getattr(agent, "alerts", None)
        if store is not None:
            try:
                getattr(store, f"set_{self.SOURCE}")(job.get("alert_id"), v, blk["side"], blk["agreed"])
            except Exception:
                pass
        others = advisor_board.record(f"alert:{job.get('alert_id')}", self.SOURCE, v, False) if v is not None else {}
        others = {k: o for k, o in others.items() if o.get("v")}
        t = advisor_board.tag(v, others) if v is not None else None
        updates = {f"{self.TAG} VERDICT": blk["text"]}
        if t:
            updates["ADVISORS"] = advisor_board.tag_text(t)
        if self.notify.edit_card(job.get("card_key") or "", updates):
            return
        fields = [field("AGENT VERDICT", job.get("agent_block") or "—", inline=False), field(f"{self.TAG} VERDICT", blk["text"], inline=False)]
        fields += [field(f"{advisor_board.TAGS.get(k, k.upper())} VERDICT", o["line"], inline=False) for k, o in others.items()]
        if t:
            fields.append(field("ADVISORS", advisor_board.tag_text(t), inline=False))
        self.notify.send(f"{job['mode_name']}:{job['symbol']}:{self.TAG}_ALERT:{job.get('alert_id')}",
                         ctitle(job["symbol"], f"ALERT VERDICT · {job.get('alert_id')}", blk["side"] or job.get("agent_side")),
                         ["second opinion only — you decide; nothing is placed"],
                         fields=fields + [field("Model", job["model"] or "CLI default"), field("Latency", f"{job.get('latency', 0):.0f} s")],
                         color=EVENT_COLOURS["info"], footer=cfooter(job["display"], self.LABEL))

    def _entry_card(self, job, v, stale):
        line = advisor_board.verdict_line(self.SOURCE, v, stale)
        key = job.get("card_key") or f"{job['mode_name']}:{job['symbol']}:{job['event']}:{job['bar_t']}"
        others = advisor_board.record(key, self.SOURCE, v, stale)
        t = advisor_board.tag(v, others)
        fld = field(self.LABEL, line, inline=False)
        hook = getattr(self.notify, "claude_edit_hook", None)
        if hook and job.get("card_key") and not stale:
            try:
                if hook(job["card_key"], fld):           # added to the original ask card
                    if t:
                        self.notify.edit_card(job["card_key"], {"Advisors": advisor_board.tag_text(t)})
                    return
            except Exception:
                pass
        title_ev = {"P": "P PRE-CROSS", "CROSS": "CONFIRMED CROSS", "RE": "RE-ENTRY", "ENTER": "ENTER"}.get(job["event"], job["event"])
        lines = [f"**{line}**"] + [o["line"] for o in others.values()] + ([f"Advisors: **{advisor_board.tag_text(t)}**"] if t else [])
        self.notify.send(f"{job['mode_name']}:{job['symbol']}:{self.TAG}_{job['event']}:{bar_key(job['bar_t'], job['off'])}",
                         ctitle(job["symbol"], f"{self.TAG} · {title_ev}", job["side"]),
                         lines + ["second opinion only — you decide; nothing is placed"],
                         fields=[field("Model", job["model"] or "CLI default"), field("Latency", f"{job.get('latency', 0):.0f} s"),
                                 field("Signal bar", f"{ist_str(job['bar_t'], job['off'])} IST")],
                         color=EVENT_COLOURS["info"], footer=cfooter(job["display"], self.LABEL))

    def _pullback_card(self, agent, job, v, p, st, note, acted, stale):
        dec = v["decision"]
        if acted and dec in ("TIGHTEN", "CLOSE", "EXIT"):
            if dec in ("CLOSE", "EXIT"):
                return                                   # the guardian's _close already posted CLAUDE CLOSED after MT5 confirmed
            title = f"{self.TAG} TIGHTENED"; colour = EVENT_COLOURS["secured"]
        elif job.get("event") == "READ":
            title = f"{self.TAG} READ · {str(job.get('trigger') or '').upper()}"
            colour = EVENT_COLOURS["flip"] if dec != "HOLD" else EVENT_COLOURS["info"]
        else:
            title = f"{self.TAG} PULLBACK VERDICT"; colour = EVENT_COLOURS["flip"] if dec != "HOLD" else EVENT_COLOURS["info"]
        others = advisor_board.record(f"pb:{agent.symbol}:{job['ticket']}:{job['bar_t']}", self.SOURCE, v, stale)
        t = advisor_board.tag(v, others)
        lines = [f"**{advisor_board.verdict_line(self.SOURCE, v, stale)}**"] + [o["line"] for o in others.values()]
        if t:
            lines.append(f"Advisors: **{advisor_board.tag_text(t)}**")
        lines.append(note)
        fields = []
        if p is not None:
            fields = position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"],
                                     points=p["points"], sl=(v["tighten_to"] if acted and dec == "TIGHTEN" else (p["sl"] or None)),
                                     secured=st.get("secured"), peak=st.get("peak"))
        fields.append(field("Trigger", job.get("trigger") or "—", inline=False))
        self.notify.send(agent.key(title.replace(" ", "_"), f"{job['ticket']}-{bar_key(job['bar_t'], agent.off)}"),
                         ctitle(agent.symbol, title, job["side"]), lines, fields=fields, color=colour,
                         footer=cfooter(agent.S.display_name, f"{self.LABEL} · {self.mode}"))

    def _budget_card(self):
        day = ist_day()
        if self._budget_day == day:
            return
        self._budget_day = day
        self.health.send(f"{STRATEGY}:ALL:{self.TAG}_BUDGET:{day}", f"AUREON · MT5 · {self.TAG} BUDGET USED",
                         [f"{self.max_calls} calls used today (IST) — no more {self.LABEL} calls until 00:00 IST",
                          "guardian and signals unaffected"], color=EVENT_COLOURS["news"], footer=f"Aureon MT5 · {self.LABEL}")

    def _unavailable(self, event, error):
        key = f"{STRATEGY}:ALL:{self.TAG}_UNAVAILABLE:{'auth'}"
        now = _time.time()
        if now - self._unavail_last.get(key, 0) < UNAVAILABLE_BACKOFF_S:
            return
        self._unavail_last[key] = now
        try:
            self.health.card(f"AUREON · MT5 · {self.TAG} UNAVAILABLE",
                             f"{error[:300]}\n{self._login_hint()} "
                             f"Aureon runs exactly as without {self.LABEL} meanwhile.",
                             color=EVENT_COLOURS["error"], footer=f"Aureon MT5 · {self.LABEL}")
        except Exception:
            pass

    def _login_hint(self) -> str:
        return f"Run `claude -p \"Reply only: OK\"` in {self.cfg.claude_workdir} to check the login."
