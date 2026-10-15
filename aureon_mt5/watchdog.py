"""v2.3.10 — Discord watchdog: stale-bar / MT5-disconnect / dead-agent incidents + periodic heartbeat.

Called from the main/supervisor thread (~30 s). Never runs on the Discord event loop.
Incidents open after 2 consecutive failing checks, clear after 2 passing. Persisted in logs/watchdog.json
so a restart mid-incident does not re-post the open card but does post RECOVERED."""
from __future__ import annotations

import json
import os
import time as _time
from datetime import datetime, timedelta, timezone

from . import broker as _broker_mod
from .config import VERSION

IST = timezone(timedelta(hours=5, minutes=30))
RED = 0xED4245
GREEN = 0x57F287
GREY = 0x95A5A6


class Watchdog:
    def __init__(self, notify, journal, cfg, agents: dict, clock=_time.time, broker=_broker_mod):
        self.notify = notify
        self.journal = journal
        self.cfg = cfg
        self.agents = agents
        self.clock = clock
        self.broker = broker
        self.started = clock()
        # {incident_key: {"fail_count", "pass_count", "open", "open_t", "card_posted"}}
        self._incidents: dict[str, dict] = {}
        self._heartbeat_posted: set[str] = set()
        self._discord_down_logged = False
        self.monitor = None                         # v2.7.2: LoopMonitor (set by main) — gateway reconnects on the heartbeat
        self._load()

    # ---- persistence
    def _path(self) -> str:
        return os.path.join(self.cfg.log_dir, "watchdog.json")

    def _load(self):
        try:
            with open(self._path(), encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.get("incidents", {}).items():
                self._incidents[k] = v
            self._heartbeat_posted = set(data.get("heartbeats", []))
        except Exception:
            pass
        # Startup reconciliation: incidents left from a previous process that are now clear
        for key, inc in list(self._incidents.items()):
            if inc.get("open") and not inc.get("recovered_at_startup"):
                inc["check_on_startup"] = True  # will be evaluated on first tick

    def _save(self):
        os.makedirs(self.cfg.log_dir, exist_ok=True)
        tmp = self._path() + ".tmp"
        data = {"incidents": self._incidents, "heartbeats": sorted(self._heartbeat_posted)[-50:]}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self._path())

    # ---- main entry point
    def tick(self):
        if not self.cfg.watchdog:
            return
        now = self.clock()
        mt5_down = not self.broker.connected() and not self.cfg.dry
        # Per-symbol checks
        for sym, ag in self.agents.items():
            market_open = self.broker.market_open(sym, getattr(ag, "off", self.cfg.server_utc_offset), self.cfg.dry)
            # STALE
            stale_key = f"stale:{sym}"
            bar_wall = getattr(ag, "last_bar_wall", None)
            stale_threshold = self.cfg.heartbeat_stale_min * 60
            is_stale = market_open and (
                (bar_wall is not None and now - bar_wall > stale_threshold) or
                (bar_wall is None and now - self.started > stale_threshold)
            )
            if mt5_down:
                # suppress per-symbol STALE cards when MT5 is down (root cause), but still track
                self._track(stale_key, is_stale, now, suppress_card=True,
                            title=f"{sym} · NO NEW M5 BAR {int((now - (bar_wall or self.started)) // 60)} min",
                            detail=self._stale_detail(sym, ag, now))
            elif not market_open:
                # never fire STALE while market closed
                self._track(stale_key, False, now, title="", detail="")
            else:
                mins = int((now - (bar_wall or self.started)) // 60)
                self._track(stale_key, is_stale, now,
                            title=f"{sym} · NO NEW M5 BAR {mins} min",
                            detail=self._stale_detail(sym, ag, now))
            # DEAD agent
            dead_key = f"dead:{sym}"
            is_dead = not ag.is_alive()
            self._track(dead_key, is_dead, now,
                        title=f"{sym} · AGENT THREAD DEAD",
                        detail=f"status: {ag.health.get('status', '?')}")

        # Global MT5 disconnect
        self._track("mt5", mt5_down, now,
                     title="MT5 DISCONNECTED",
                     detail="broker.connected() = False")

        self._heartbeat(now)
        self._save()

    def _stale_detail(self, sym, ag, now) -> str:
        h = ag.health
        bar_wall = getattr(ag, "last_bar_wall", None)
        return (f"last bar {h.get('last_bar') or '—'} IST · "
                f"tick age {ag.snapshot.get('tick_age', '—') if hasattr(ag, 'snapshot') else '—'}s · "
                f"MT5 {'connected' if self.broker.connected() else 'DISCONNECTED'} · "
                f"market {'open' if self.broker.market_open(sym, getattr(ag, 'off', 3), self.cfg.dry) else 'closed'} · "
                f"uptime {int((now - self.started) // 60)} min")

    # ---- incident state machine
    def _track(self, key: str, failing: bool, now: float, title: str = "", detail: str = "", suppress_card: bool = False):
        inc = self._incidents.setdefault(key, {"fail_count": 0, "pass_count": 0, "open": False,
                                                "open_t": None, "card_posted": False})
        # Startup reconciliation: check if a persisted open incident is now clear
        if inc.pop("check_on_startup", False):
            if not failing:
                dur = int((now - (inc.get("open_t") or now)) // 60)
                self._post_recovered(key, dur, "cleared while Aureon was down", now)
                inc["open"] = False; inc["card_posted"] = False
                inc["fail_count"] = 0; inc["pass_count"] = 2
                return

        if failing:
            inc["fail_count"] = inc.get("fail_count", 0) + 1
            inc["pass_count"] = 0
            if inc["fail_count"] >= 2 and not inc["open"]:
                inc["open"] = True; inc["open_t"] = now
                if not suppress_card:
                    inc["card_posted"] = True
                    self._post_open(key, title, detail, now)
                self.journal.log("watchdog_open", incident=key, detail=detail[:200])
        else:
            inc["pass_count"] = inc.get("pass_count", 0) + 1
            inc["fail_count"] = 0
            if inc["pass_count"] >= 2 and inc["open"]:
                dur = int((now - (inc.get("open_t") or now)) // 60)
                self._post_recovered(key, dur, "", now)
                self.journal.log("watchdog_clear", incident=key, duration_min=dur)
                inc["open"] = False; inc["card_posted"] = False

    def _post_open(self, key: str, title: str, detail: str, now: float):
        self.notify.send(f"watchdog:{key}:open:{int(now)}", f"⚠️ WATCHDOG · {title}",
                         [detail], color=RED, footer=f"Aureon MT5 · watchdog · {datetime.now(IST):%H:%M} IST")

    def _post_recovered(self, key: str, dur: int, note: str, now: float):
        msg = f"down {dur} min" + (f" · {note}" if note else "")
        self.notify.send(f"watchdog:{key}:clear:{int(now)}", f"✅ RECOVERED · {key}",
                         [msg], color=GREEN, footer=f"Aureon MT5 · watchdog · {datetime.now(IST):%H:%M} IST")

    # ---- heartbeat
    def _heartbeat(self, now: float):
        hours = self.cfg.heartbeat_hours
        if hours <= 0:
            return
        ist_now = datetime.fromtimestamp(now, tz=IST)
        # Aligned slot: 00, 04, 08, 12, 16, 20 for hours=4
        slot_h = int(ist_now.hour // hours) * int(hours)
        slot_key = f"heartbeat:{ist_now.strftime('%Y-%m-%d')} {slot_h:02d}"
        if slot_key in self._heartbeat_posted:
            return
        # Only post if we're past the slot start
        slot_start = ist_now.replace(hour=slot_h, minute=0, second=0, microsecond=0)
        if ist_now < slot_start:
            return
        # Skip on a quiet weekend (market closed, no incidents)
        is_weekend = ist_now.weekday() >= 5
        has_incidents = any(i.get("open") for i in self._incidents.values())
        if is_weekend and not has_incidents:
            market_open = any(self.broker.market_open(s, getattr(a, "off", 3), self.cfg.dry) for s, a in self.agents.items())
            if not market_open:
                return
        self._heartbeat_posted.add(slot_key)
        lines = [f"v{VERSION} · uptime {int((now - self.started) // 60)} min"]
        if self.monitor is not None:
            try:
                lines.append(f"gateway {self.monitor.gateway_state()} · reconnects last 4 h: {self.monitor.reconnects_since(4 * 3600)}")
            except Exception:
                pass
        try:                                                          # v2.10.1: disk · drift · updates pending · MT5 age
            from .server import heartbeat_line
            lines.append(heartbeat_line())
            try:                                             # v2.11.2: DNS self-check on the 4-hourly heartbeat
                from . import netguard
                lines.append(netguard.dns_line(netguard.dns_check(netguard.dns_hosts(self.cfg))))
            except Exception:
                pass
        except Exception:
            pass
        for sym, ag in self.agents.items():
            h = ag.health
            pos_n = len((getattr(ag, "snapshot", {}) or {}).get("positions", []))
            armed = len(ag.alerts.armed(sym)) if hasattr(ag, "alerts") and ag.alerts else 0
            chop_s = " · CHOP" if h.get("chop") else ""
            inc_keys = [k for k, v in self._incidents.items() if v.get("open") and sym in k]
            inc_s = f" · incidents: {', '.join(inc_keys)}" if inc_keys else ""
            lines.append(f"**{sym}** · {ag.S.display_name} · bar {h.get('last_bar') or '—'} · "
                         f"MT5 {'ok' if self.broker.connected() else 'DOWN'} · "
                         f"pos {pos_n} · alerts {armed}{chop_s}{inc_s}")
        global_inc = [k for k, v in self._incidents.items() if v.get("open") and ":" not in k]
        if global_inc:
            lines.append(f"Open incidents: {', '.join(global_inc)}")
        self.notify.send(slot_key, "💓 Aureon alive", lines, color=GREY,
                         footer=f"Aureon MT5 · heartbeat · {ist_now:%H:%M} IST")
        self.journal.log("heartbeat", slot=slot_key)

    # ---- queries
    def open_incidents(self) -> list[dict]:
        now = self.clock()
        out = []
        for k, v in self._incidents.items():
            if v.get("open"):
                dur = int((now - (v.get("open_t") or now)) // 60)
                out.append({"key": k, "duration_min": dur, "since": v.get("open_t")})
        return out

    def next_heartbeat_ist(self) -> str:
        hours = self.cfg.heartbeat_hours
        if hours <= 0:
            return "off"
        ist_now = datetime.fromtimestamp(self.clock(), tz=IST)
        slot_h = int(ist_now.hour // hours) * int(hours)
        nxt = ist_now.replace(hour=slot_h, minute=0, second=0, microsecond=0) + timedelta(hours=hours)
        return nxt.strftime("%H:%M") + " IST"
