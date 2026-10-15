"""v2.10.1 — evidence-gated strategy autonomy (AUREON_TUNING_AUTO=off|shadow|apply, default off).

Owning the box is not owning the strategy. A tuning proposal (tuning.propose — from Claude's review, the board, or the
regime / ZL analysis) changes a strategy or guardian knob on its own ONLY with evidence:

  1. replay  tools/replay_guardian --compare over the last AUREON_TUNING_REPLAY_DAYS: must improve total points AND reduce
             losing (stopped-out) trades AND, for regime knobs, pass the Part D sign-off (blocked losers >= 3 x winners),
             AND the strategy pins must still pass.
  2. shadow  AUREON_TUNING_SHADOW_SESSIONS sessions: each session is replayed with and without the change; a card says what
             it WOULD have done; every session must agree (change >= default).
  3. apply   only with AUREON_TUNING_AUTO=apply, at most AUREON_TUNING_AUTO_PER_WEEK per week, through the feature
             registry, with a STRATEGY CHANGE APPLIED card and `/veto <id>` that reverts instantly.

NEVER auto-applied under any setting: lot size (max_lots, risk_pct, lot_buttons), loosening pre_stop / setup_max_dd,
claude_mode → manage, exit_losing, symbols, mode. Anything failing a gate stays a proposal for /approve.
Only registry features can be applied automatically (Guardian-dataclass knobs need a human)."""
from __future__ import annotations

import json
import os
import time as _time
from datetime import datetime, timedelta, timezone

from . import features as F
from . import tuning

IST = timezone(timedelta(hours=5, minutes=30))
NEVER = {"max_lots", "risk_pct", "lot_buttons", "exit_losing", "claude_exit_losing", "symbols", "mode"}
LOOSEN_NEVER = {"pre_stop", "setup_max_dd"}         # a LARGER value = a looser stop
LOSS_REASONS = None                                  # every losing trade counts as a stop-out


def never_reason(param: str, old, new) -> str | None:
    if param in NEVER:
        return f"{param} is never auto-applied"
    if param == "claude_mode" and str(new) == "manage":
        return "claude_mode → manage is never auto-applied"
    if param in LOOSEN_NEVER:
        try:
            if float(new) > float(old):
                return f"loosening {param} ({old} → {new}) is never auto-applied"
        except (TypeError, ValueError):
            return f"{param}: unreadable values"
    return None


def stopouts(res: dict) -> int:
    return sum(1 for t in res.get("trades_detail") or [] if float(t.get("points", 0)) < 0)


def _path(log_dir):
    return os.path.join(log_dir, "tuning_auto.json")


def load(log_dir) -> dict:
    try:
        with open(_path(log_dir), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"items": {}}


def save(log_dir, d):
    os.makedirs(log_dir, exist_ok=True)
    tmp = _path(log_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1, default=str)
    os.replace(tmp, _path(log_dir))


class TuningAuto:
    def __init__(self, cfg, journal, notify, *, replay=None, pins=None, clock=_time.time, advisors=()):
        """replay(over: dict, days: int) -> {"defaults": {...}, "combo": {...}} (tools/replay_guardian.run_combo shape);
        pins() -> bool (tests/test_strategy_pins.py green)."""
        self.cfg, self.journal, self.notify, self.clock, self.advisors = cfg, journal, notify, clock, advisors
        self.replay = replay or default_replay(cfg)
        self.pins = pins or default_pins()

    def _card(self, title, text):
        try:
            self.notify.card(title, text, footer="Aureon MT5 · tuning auto")
        except Exception:
            pass

    # -- 1. replay gate
    def evaluate(self, prop: dict) -> dict:
        param, old, new = prop["param"], prop["old"], prop["new"]
        st = {"id": prop["id"], "param": param, "old": old, "new": new, "status": "proposal", "why": "", "shadow": []}
        nr = never_reason(param, old, new)
        if nr:
            st.update(status="never", why=nr); return st
        try:
            f = F.get(param)
        except ValueError:
            st.update(why="not a registry feature — Guardian knobs need a human (/approve)"); return st
        try:
            res = self.replay({f.attr: F.parse(f, new)}, int(self.cfg.tuning_replay_days))
        except Exception as e:
            st.update(why=f"replay failed: {e!r}"[:200]); return st
        d, c = res["defaults"], res["combo"]
        dp = round(c["total_points"] - d["total_points"], 2); ds = stopouts(c) - stopouts(d)
        st.update(replay_points=dp, replay_stopouts=ds)
        fails = []
        if dp <= 0:
            fails.append(f"points {dp:+g}")
        if ds >= 0:
            fails.append(f"stop-outs {ds:+d}")
        if f.group == "regime" and "gate_acceptable" in c and c.get("gate_acceptable") is False:
            fails.append("regime sign-off failed (blocked losers < 3 × winners)")
        if not self.pins():
            fails.append("strategy pins failed")
        if fails:
            st.update(why="replay gate: " + ", ".join(fails)); return st
        st.update(status="shadow", why=f"replay {dp:+g} pts, {ds:+d} stop-outs — shadowing {self.cfg.tuning_shadow_sessions} sessions")
        self._card(f"STRATEGY SHADOW · {param} {old}→{new}", st["why"] + " · nothing changes until the shadow is clean")
        return st

    # -- 2. shadow one session (called once per session end)
    def shadow_session(self, st: dict) -> dict:
        f = F.get(st["param"])
        res = self.replay({f.attr: F.parse(f, st["new"])}, 1)
        d, c = res["defaults"], res["combo"]
        delta = round(c["total_points"] - d["total_points"], 2)
        agree = delta >= 0 and stopouts(c) <= stopouts(d)
        st["shadow"].append({"day": datetime.fromtimestamp(self.clock(), tz=IST).strftime("%Y-%m-%d"), "delta": delta, "agree": agree})
        self.journal.log("tuning_shadow", id=st["id"], param=st["param"], delta=delta, agree=agree)
        self._card(f"SHADOW · {st['param']} {st['old']}→{st['new']}", f"would have made {delta:+g} pts vs the live setting today "
                   f"({'agrees' if agree else 'DISAGREES'}) · {len(st['shadow'])}/{self.cfg.tuning_shadow_sessions}")
        if not agree:
            st.update(status="proposal", why=f"shadow disagreed on {st['shadow'][-1]['day']} ({delta:+g} pts) — /approve to apply by hand")
        elif len(st["shadow"]) >= int(self.cfg.tuning_shadow_sessions):
            st.update(status="shadow_passed", why=f"shadow {len(st['shadow'])}/{self.cfg.tuning_shadow_sessions} agree")
        return st

    # -- 3. apply
    def applied_this_week(self, d: dict) -> int:
        week_ago = self.clock() - 7 * 86400
        return sum(1 for s in d["items"].values() if s.get("applied_t", 0) >= week_ago)

    def maybe_apply(self, st: dict, d: dict) -> dict:
        if st["status"] != "shadow_passed":
            return st
        if self.cfg.tuning_auto != "apply":
            st["why"] += " · shadow mode: not applied (/approve or AUREON_TUNING_AUTO=apply)"; return st
        if self.applied_this_week(d) >= int(self.cfg.tuning_auto_per_week):
            st["why"] += f" · weekly cap {self.cfg.tuning_auto_per_week} reached — waits"; return st
        nr = never_reason(st["param"], st["old"], st["new"])
        if nr:                                                    # re-checked at the last moment
            st.update(status="never", why=nr); return st
        old, new = F.set_override(self.cfg.log_dir, self.cfg, st["param"], st["new"], "tuning-auto", advisors=self.advisors)
        st.update(status="applied", applied_t=self.clock())
        self.journal.log("tuning_auto_applied", id=st["id"], param=st["param"], old=F.fmt(F.get(st["param"]), old), new=st["new"],
                         replay_points=st.get("replay_points"), replay_stopouts=st.get("replay_stopouts"))
        n = len(st["shadow"])
        self._card(f"STRATEGY CHANGE APPLIED · {st['param']} {st['old']}→{st['new']}",
                   f"replay {st.get('replay_points', 0):+g} pts, {st.get('replay_stopouts', 0):+d} stop-outs · shadow {n}/{n} agree\n"
                   f"`/veto {st['id']}` reverts instantly")
        return st

    def veto(self, pid: int, who: str = "") -> str:
        d = load(self.cfg.log_dir)
        st = d["items"].get(str(pid))
        if not st:
            return f"no auto-tuning item #{pid}"
        if st["status"] == "applied":
            F.clear_override(self.cfg.log_dir, self.cfg, st["param"], who, advisors=self.advisors)
        st.update(status="vetoed", vetoed_by=str(who))
        save(self.cfg.log_dir, d)
        self.journal.log("tuning_vetoed", id=pid, param=st["param"], by=str(who))
        return f"↩️ #{pid} {st['param']} vetoed — back to {F.fmt(F.get(st['param']), getattr(self.cfg, F.get(st['param']).attr))}" \
            if st["param"] in {f.name for f in F.registry()} else f"↩️ #{pid} vetoed"

    # -- the tick (main loop)
    def tick(self, session_ended: bool = False) -> list[dict]:
        if self.cfg.tuning_auto not in ("shadow", "apply"):
            return []
        d = load(self.cfg.log_dir); changed = []
        for prop in tuning.load_proposals(self.cfg.log_dir):
            if prop.get("status") != "pending" or str(prop["id"]) in d["items"]:
                continue
            d["items"][str(prop["id"])] = self.evaluate(prop); changed.append(d["items"][str(prop["id"])])
        if session_ended:
            for st in d["items"].values():
                if st["status"] == "shadow":
                    self.shadow_session(st); changed.append(st)
        for st in d["items"].values():
            if st["status"] == "shadow_passed":
                self.maybe_apply(st, d)
        save(self.cfg.log_dir, d)
        return changed


def default_replay(cfg):
    def run(over: dict, days: int) -> dict:
        from aureon_mt5 import broker
        import tools.replay_guardian as RG
        bars = broker.bars_tf(cfg.symbols[0], "M5", days * 288 + 300, "mt5", cfg.server_utc_offset)
        bars = RG.prepare(bars, zl=bool(over.get("zl_context") or over.get("flip_use_zl")))
        ents = RG.entries(bars, [], "ema5080", cfg.symbols[0])
        return {"defaults": RG.run_combo(bars, ents, {}), "combo": RG.run_combo(bars, ents, over)}
    return run


def default_pins():
    def run() -> bool:
        import subprocess, sys
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_strategy_pins.py"], cwd=root, capture_output=True,
                           text=True, timeout=600)
        return r.returncode == 0
    return run
