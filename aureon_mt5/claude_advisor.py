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

from . import telemetry
from .common import claude_cli
from .common.claude_cli import Budget, IST, ist_day
from .common.news import session_of
from .common.timeutil import bar_key, ist_str, now_server
from .notify import EVENT_COLOURS, field, footer as cfooter, position_fields, title as ctitle

MODES = ("off", "review", "advisory", "manage")
ADVISE_MODES = ("advisory", "manage")
STRATEGY = "ema5080"                       # key prefix for the ALL-symbol cards (kept for dedupe compatibility)
STRATEGIES = ("ema5080", "ema2050")        # v2.0.0: the add-on advises both modes
UNAVAILABLE_BACKOFF_S = 300
BAR_S = 300
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_PATH = os.path.join(ROOT, "prompts", "claude_rules.md")
KIND_TO_EVENT = {"P": "P", "cross": "CROSS", "reentry": "RE", "enter": "ENTER", "pullback": "ENTER", "no-pullback": "ENTER"}
EMA_NAMES = {"ema5080": ("EMA 50", "EMA 80"), "ema2050": ("EMA 20", "EMA 50")}
TREND_PULLBACK = ("PULLBACK", "WEAKENING", "CHALLENGED")

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
        confirm_state = res["bar_states"][-1]["confirm"]
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
        "bar_time_ist": ist_str(bar_t, off, "%Y-%m-%d %H:%M"), "close": round(close, 2),
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
                     "dist_to_sl": round(abs(float(position.get("current", close)) - sl), 2) if sl else None})
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
                 "retrace_from_peak_pts", "bars_since_peak", "mae_so_far", "closed_through_slow_ema", "dist_to_sl", "peak", "open_profit")


def _compact(snap: dict) -> dict:
    """The numeric/categorical snapshot fields journaled with every verdict (for the Saturday rule proposals). No bars, no prose."""
    out = {k: snap[k] for k in SNAPSHOT_KEEP if k in snap and snap[k] is not None}
    wz = snap.get("whipsaw") or {}
    if wz:
        out["whipsaw_flips"] = wz.get("ema50_side_flips"); out["whipsaw_zone"] = wz.get("zone")
    return out


# ============================================================================= advisor
class ClaudeAdvisor:
    def __init__(self, cfg, notify, journal, *, health_notify=None, caller=claude_cli.call, start: bool = True):
        self.cfg, self.notify, self.journal = cfg, notify, journal
        self.health = health_notify or notify
        self.mode = normalise_mode(getattr(cfg, "claude_mode", "off"))
        self.caller = caller
        self.q: "queue.Queue[dict]" = queue.Queue(maxsize=20)
        self.budget = Budget(os.path.join(cfg.log_dir, "claude_budget.json"), cfg.claude_max_calls)
        self._call_lock = threading.Lock()                  # one `claude -p` at a time, worker and commands alike
        self.fired: dict[tuple, float] = {}                 # (symbol, ticket) -> peak when the pullback verdict was requested
        self._unavail_last: dict[str, float] = {}
        self._budget_day: str | None = None
        self.stats = {"login": None, "last_verdict": None, "last_error": "", "latencies": deque(maxlen=50), "dropped": 0}
        self._worker: threading.Thread | None = None
        try:
            with open(RULES_PATH, encoding="utf-8") as f:
                self.rules = f.read()
        except Exception:
            self.rules = "Reply with one JSON object only."
        if start and self.mode != "off":
            self.start()

    @classmethod
    def create(cls, cfg, notify, journal, **kw):
        """None when AUREON_CLAUDE=off → zero Claude calls, zero behaviour change."""
        return None if normalise_mode(getattr(cfg, "claude_mode", "off")) == "off" else cls(cfg, notify, journal, **kw)

    # ------------------------------------------------------------------ who gets hooks
    def advises(self, strategy) -> bool:
        return self.mode in ADVISE_MODES and getattr(strategy, "name", "") in STRATEGIES

    @property
    def manage(self) -> bool:
        return self.mode == "manage"

    # ------------------------------------------------------------------ worker
    def start(self):
        if self._worker is None:
            self._worker = threading.Thread(target=self._loop, daemon=True, name="claude-worker"); self._worker.start()

    def _loop(self):
        while True:
            job = self.q.get()
            try:
                self.process(job)
            except Exception as e:
                telemetry.info(f"claude worker error: {e!r}")

    def _enqueue(self, job: dict) -> bool:
        try:
            self.q.put_nowait(job); return True
        except queue.Full:
            self.stats["dropped"] += 1
            telemetry.info(f"claude queue full — dropped {job.get('event')} {job.get('symbol')}")
            return False

    # ------------------------------------------------------------------ triggers (agent thread; enqueue only)
    def request_entry(self, agent, kind: str, side: str, bar_t: int, df, card_key: str | None = None) -> bool:
        event = KIND_TO_EVENT.get(kind)
        if not event or not self.advises(agent.S):
            return False
        snap = build_snapshot(agent, event, side, bar_t, df, kind=kind)
        return self._enqueue({"type": "entry", "event": event, "symbol": agent.symbol, "side": side, "bar_t": bar_t,
                              "off": agent.off, "snapshot": snap, "card_key": card_key, "agent": agent,
                              "model": self.cfg.claude_entry_model, "mode_name": agent.S.name, "display": agent.S.display_name})

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
                              "open_profit": p["points"], "trigger": why, "model": self.cfg.claude_pullback_model,
                              "mode_name": agent.S.name, "display": agent.S.display_name})

    def request_alert(self, agent, alert: dict, card: dict, df, bar_t: int) -> bool:
        """v2.0.0: the /alert card's second line — any mode but off (review included). Uses the entry model and the daily budget;
        a used budget skips silently (no card). Enqueue only; the poll never waits."""
        if self.mode == "off":
            return False
        sug = (card.get("meta") or {}).get("suggested_side")
        snap = build_snapshot(agent, "ALERT", sug or "", bar_t, df)
        snap["alert"] = {"id": alert["id"], "price": alert["price"], "hit": alert.get("hit_price"), "side_hint": alert.get("side_hint"), "note": alert.get("note")}
        snap["bot_suggestion"] = {"decision": (card.get("meta") or {}).get("suggestion"), "side": sug, "reason": (card.get("meta") or {}).get("reason")}
        return self._enqueue({"type": "alert", "event": "ALERT", "symbol": agent.symbol, "side": sug, "bar_t": bar_t, "off": agent.off,
                              "snapshot": snap, "card_key": card.get("key"), "agent": agent, "alert_id": alert["id"],
                              "model": self.cfg.claude_entry_model, "mode_name": agent.S.name, "display": agent.S.display_name})

    def forget(self, symbol: str, ticket):
        self.fired.pop((symbol, ticket), None)

    # ------------------------------------------------------------------ the call (worker / command threads)
    def _call(self, prompt: str, model: str, event: str) -> claude_cli.CliResult | None:
        """None = budget used (no call made)."""
        if not self.budget.take():
            self._budget_card(); return None
        with self._call_lock:
            res = self.caller(prompt, model, event=event, bin=self.cfg.claude_bin, workdir=self.cfg.claude_workdir,
                              timeout=self.cfg.claude_timeout)
        self.stats["latencies"].append(res.latency_s)
        if res.auth_error:
            self.stats["login"] = "FAILED"
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
        return head + self.rules.strip() + "\n\nSNAPSHOT:\n" + json.dumps(snapshot, default=str)

    def process(self, job: dict):
        if job["type"] == "review":
            self.daily_review(job.get("day"), force=job.get("force", False)); return
        res = self._call(self.prompt_for(job["snapshot"]), job["model"], job["event"])
        if res is None:
            self._journal(job, None, latency=0.0, acted=False, stale=False, error="budget used"); return
        v = res.verdict if res.ok else None
        if job["type"] == "alert":
            self._journal(job, v, latency=res.latency_s, acted=False, stale=False, error=("" if v is not None else (res.error or "no verdict")))
            if v is not None:
                self.stats["last_verdict"] = f"{job['symbol']} ALERT {v['decision']} · {v['confidence']} · {datetime.now(IST):%H:%M} IST"
                self._alert_line(job, v)
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
        job["latency"] = res.latency_s
        if job["type"] == "entry":
            stale = now_server(job["off"]) >= job["bar_t"] + 2 * BAR_S
            self._journal(job, v, latency=res.latency_s, acted=False, stale=stale)
            self._entry_card(job, v, stale)
        else:
            job["verdict"] = v
            agent = job["agent"]
            inbox = getattr(agent, "claude_inbox", None)
            if inbox is not None:
                inbox.put(job)                           # applied/shown on the agent thread, after the guardian

    # ------------------------------------------------------------------ verdict on a position (AGENT thread)
    def drain(self, agent, positions: list[dict]):
        inbox = getattr(agent, "claude_inbox", None)
        while inbox is not None:
            try:
                job = inbox.get_nowait()
            except queue.Empty:
                return
            try:
                p = next((x for x in positions if x["ticket"] == job["ticket"]), None)
                if p is not None and p["ticket"] in getattr(agent, "closed_by_aureon", set()):
                    p = None                             # the guardian already closed it: hard exits win
                self.apply_pullback(agent, job, p, agent.state.get(job["ticket"], {}) if p else {})
            except Exception as e:
                telemetry.failure(self.health, self.journal, title="AUREON CLAUDE ERROR", key=f"{agent.S.name}:{agent.symbol}:CLAUDE_ERROR:apply",
                                  mode=agent.S.name, symbol=agent.symbol, action="claude pullback verdict", exc=e)

    def apply_pullback(self, agent, job: dict, p: dict | None, st: dict) -> bool:
        """Returns True when an action was applied in MT5. Order: MT5 → confirm → state → journal → Discord."""
        v = job["verdict"]; dec = v["decision"]
        stale = now_server(agent.off) >= job["bar_t"] + 2 * BAR_S
        acted = False; note = ""
        if p is None:
            note = "position no longer open — nothing to do"
        elif stale:
            note = "STALE — arrived after the next bar closed; not acted"
        elif dec == "HOLD":
            note = "hold — guardian keeps managing by its rules"
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
                agent._close(p, f"Claude: {v['reason'] or 'trend turning'}", "CLAUDE CLOSED",
                             agent.key("CLAUDE_CLOSED", f"{p['ticket']}-{bar_key(job['bar_t'], agent.off)}"), st, "claude_close", agent.last_bar)
                acted = p["ticket"] in getattr(agent, "closed_by_aureon", set())
                note = "closed in profit (MT5 confirmed)" if acted else "close failed — see the failure alert"
        self._journal(job, v, latency=job.get("latency", 0.0), acted=acted, stale=stale, note=note)
        self._pullback_card(agent, job, v, p, st, note, acted, stale)
        return acted

    # ------------------------------------------------------------------ daily review
    def review_events(self, day: str) -> list[dict]:
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=IST)
        recs = self.journal.read(int(start.timestamp()), int((start + timedelta(days=1)).timestamp()))
        keep = ("t", "event", "symbol", "side", "signal_kind", "decision", "reason", "points", "price", "secured", "peak", "level",
                "best", "against", "why", "claude_event", "confidence", "acted", "stale", "trigger")
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
        if not force and any(r.get("event") == "claude_review" and r.get("day") == day
                             for r in self.journal.read(int(_time.time()) - 2 * 86400)):
            return f"already reviewed {day}"
        events = self.review_events(day)
        if not events:
            text = f"no journal events on {day}"
            self.notify.card(f"AUREON · MT5 · CLAUDE REVIEW · {day}", text, color=EVENT_COLOURS["info"], footer="Aureon MT5 · Claude review")
            return text
        res = self._call(REVIEW_PROMPT + json.dumps(events, default=str), self.cfg.claude_review_model, "review")
        if res is None:
            return "budget used — no review today"
        if not res.ok or not res.text:
            return f"review unavailable: {res.error or 'empty reply'}"
        text = res.text[:3800]
        self.journal.log("claude_review", source="claude", day=day, model=self.cfg.claude_review_model, latency_s=round(res.latency_s, 1),
                         events=len(events))
        self.notify.card(f"AUREON · MT5 · CLAUDE REVIEW · {day}", text, color=EVENT_COLOURS["info"],
                         fields=[field("Events", len(events)), field("Model", self.cfg.claude_review_model), field("Latency", f"{res.latency_s:.0f} s")],
                         footer="Aureon MT5 · Claude review · advisory only")
        return text

    def request_review(self, day: str | None = None, force: bool = False) -> bool:
        return self._enqueue({"type": "review", "event": "review", "symbol": "ALL", "day": day, "force": force})

    def test_call(self) -> claude_cli.CliResult | None:
        return self._call("Reply only: OK", self.cfg.claude_review_model, "test")

    # ------------------------------------------------------------------ status for /claude
    def status(self) -> dict:
        lats = list(self.stats["latencies"])
        return {"mode": self.mode, "login": self.stats["login"] or "unknown — run /claude-test",
                "calls": self.budget.used(), "limit": self.cfg.claude_max_calls, "last_verdict": self.stats["last_verdict"] or "—",
                "avg_latency": (sum(lats) / len(lats)) if lats else None, "queue": self.q.qsize(),
                "last_error": self.stats["last_error"] or "—", "dropped": self.stats["dropped"],
                "models": f"entry {self.cfg.claude_entry_model} · pullback {self.cfg.claude_pullback_model} · review {self.cfg.claude_review_model}"}

    # ------------------------------------------------------------------ journal + cards
    def _journal(self, job, v, *, latency, acted, stale, error="", note=""):
        try:
            self.journal.log("claude_verdict", source="claude", claude_event=job["event"], symbol=job["symbol"], model=job.get("model"),
                             latency_s=round(latency, 1), decision=(v or {}).get("decision"), side=(v or {}).get("side"),
                             confidence=(v or {}).get("confidence"), reason=(v or {}).get("reason"),
                             tighten_to=(v or {}).get("tighten_to"), my_action=None, acted=bool(acted), stale=bool(stale),
                             bar=job.get("bar_t"), ticket=job.get("ticket"), open_profit=job.get("open_profit"),
                             trigger=job.get("trigger"), error=error or None, note=note or None,
                             evidence=(v or {}).get("evidence") or [], alert_id=job.get("alert_id"),
                             snapshot_fields=_compact(job.get("snapshot") or {}))
        except Exception as e:
            telemetry.info(f"claude journal failed: {e!r}")

    def _alert_line(self, job, v):
        """Second line on the ALERT REACHED card: 'Claude: TAKE · high · reason (evidence: ...)'. Falls back to a small card."""
        ev = ", ".join(v.get("evidence") or [])
        label = f"{v['decision']}" + (f" {v['side']}" if v.get("side") else "") + f" · {v['confidence']} · {v['reason']}" + (f" (evidence: {ev})" if ev else "")
        fld = field("Claude add-on", label, inline=False)
        hook = getattr(self.notify, "claude_edit_hook", None)
        if hook and job.get("card_key"):
            try:
                if hook(job["card_key"], fld):
                    return
            except Exception:
                pass
        self.notify.send(f"{job['mode_name']}:{job['symbol']}:CLAUDE_ALERT:{job.get('alert_id')}", ctitle(job["symbol"], "CLAUDE · ALERT", job.get("side")),
                         [f"Claude: **{label}**", "second opinion only — you decide; nothing is placed"],
                         fields=[field("Model", job["model"]), field("Latency", f"{job.get('latency', 0):.0f} s")],
                         color=EVENT_COLOURS["info"], footer=cfooter(job["display"], "Claude"))

    def _entry_card(self, job, v, stale):
        label = f"{'⌛ STALE · ' if stale else ''}{v['decision']} · {v['confidence']} · {v['reason']}"
        fld = field("Claude", label, inline=False)
        hook = getattr(self.notify, "claude_edit_hook", None)
        if hook and job.get("card_key") and not stale:
            try:
                if hook(job["card_key"], fld):           # added to the original ask card
                    return
            except Exception:
                pass
        title_ev = {"P": "P PRE-CROSS", "CROSS": "CONFIRMED CROSS", "RE": "RE-ENTRY", "ENTER": "ENTER"}.get(job["event"], job["event"])
        self.notify.send(f"{job['mode_name']}:{job['symbol']}:CLAUDE_{job['event']}:{bar_key(job['bar_t'], job['off'])}",
                         ctitle(job["symbol"], f"CLAUDE · {title_ev}", job["side"]),
                         [f"Claude: **{label}**", "second opinion only — you decide; nothing is placed"],
                         fields=[field("Model", job["model"]), field("Latency", f"{job.get('latency', 0):.0f} s"),
                                 field("Signal bar", f"{ist_str(job['bar_t'], job['off'])} IST")],
                         color=EVENT_COLOURS["info"], footer=cfooter(job["display"], "Claude"))

    def _pullback_card(self, agent, job, v, p, st, note, acted, stale):
        dec = v["decision"]
        if acted and dec in ("TIGHTEN", "CLOSE"):
            if dec == "CLOSE":
                return                                   # the guardian's _close already posted CLAUDE CLOSED after MT5 confirmed
            title = "CLAUDE TIGHTENED"; colour = EVENT_COLOURS["secured"]
        else:
            title = "CLAUDE PULLBACK VERDICT"; colour = EVENT_COLOURS["flip"] if dec != "HOLD" else EVENT_COLOURS["info"]
        lines = [f"Claude: **{'⌛ STALE · ' if stale else ''}{dec} · {v['confidence']} · {v['reason']}**", note]
        fields = []
        if p is not None:
            fields = position_fields(ticket=p["ticket"], direction=p["direction"], entry=p["price_open"], now=p["current"],
                                     points=p["points"], sl=(v["tighten_to"] if acted and dec == "TIGHTEN" else (p["sl"] or None)),
                                     secured=st.get("secured"), peak=st.get("peak"))
        fields.append(field("Trigger", job.get("trigger") or "—", inline=False))
        self.notify.send(agent.key(title.replace(" ", "_"), f"{job['ticket']}-{bar_key(job['bar_t'], agent.off)}"),
                         ctitle(agent.symbol, title, job["side"]), lines, fields=fields, color=colour,
                         footer=cfooter(agent.S.display_name, f"Claude · {self.mode}"))

    def _budget_card(self):
        day = ist_day()
        if self._budget_day == day:
            return
        self._budget_day = day
        self.health.send(f"{STRATEGY}:ALL:CLAUDE_BUDGET:{day}", "AUREON · MT5 · CLAUDE BUDGET USED",
                         [f"{self.cfg.claude_max_calls} calls used today (IST) — no more Claude calls until 00:00 IST",
                          "guardian and signals unaffected"], color=EVENT_COLOURS["news"], footer="Aureon MT5 · Claude")

    def _unavailable(self, event, error):
        key = f"{STRATEGY}:ALL:CLAUDE_UNAVAILABLE:{'auth'}"
        now = _time.time()
        if now - self._unavail_last.get(key, 0) < UNAVAILABLE_BACKOFF_S:
            return
        self._unavail_last[key] = now
        try:
            self.health.card("AUREON · MT5 · CLAUDE UNAVAILABLE",
                             f"{error[:300]}\nRun `claude -p \"Reply only: OK\"` in {self.cfg.claude_workdir} to check the login. "
                             "Aureon runs exactly as without Claude meanwhile.",
                             color=EVENT_COLOURS["error"], footer="Aureon MT5 · Claude")
        except Exception:
            pass
