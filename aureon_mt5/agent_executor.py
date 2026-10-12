"""v2.4.2 — AGENT entry executor.

When a symbol is in AGENT mode and a confirmed entry signal posts its ask,
after AUREON_AGENT_DELAY_S (default 20 s, gives the human first refusal)
and if the ask is still unanswered: call execute_entry with actor="agent".

Uses the SAME path as buttons: governor, HALT, demo/live gate, order →
confirm retcode → guardian state → journal → Discord.
"""
from __future__ import annotations

import threading
import time as _time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def schedule_agent_entry(cfg, agents: dict, journal, notify, agent, ask: dict, sleep=_time.sleep):
    """Schedule a delayed agent entry for an open ask. Runs in a daemon thread.
    `ask` is the dict from agent.track_ask (card_key, side, kind, price, bar, lots, sl, posted_t)."""
    def _run():
        sleep(cfg.agent_delay_s)

        # Check if still unanswered
        for a in agent.open_asks:
            if a["card_key"] == ask["card_key"]:
                if a.get("decided"):
                    return  # human answered → nothing to do
                break
        else:
            return  # ask no longer tracked (consumed/expired)

        # Check control mode still AGENT
        from .control import current_mode
        ctrl = current_mode(journal, agent.symbol)
        if ctrl["mode"] != "AGENT":
            return

        # Build item for execute_entry
        from .bot import execute_entry
        item = {"meta": {"symbol": agent.symbol, "side": ask["side"], "kind": ask["kind"],
                         "price": ask["price"], "bar": ask["bar"], "mode": agent.S.name,
                         "sl": ask.get("sl"), "lots": ask.get("lots"),
                         "chop": agent._is_chop() if hasattr(agent, "_is_chop") else False},
                "key": ask["card_key"]}
        r = execute_entry(cfg, agents, journal, item, ask["side"], "agent",
                          notify=notify, card_key=ask["card_key"], lots=ask.get("lots"))

        # Mark ask as decided by agent
        agent.mark_ask_decided(ask["card_key"], "TAKE", placed=r.get("placed", False))

        # Journal with actor="agent"
        journal.decision("agent_entry", actor="agent",
                         rationale=f"rule: {ask['kind']} confirmed",
                         p_win=ask.get("p_win"),
                         symbol=agent.symbol, side=ask["side"], kind=ask["kind"],
                         card_key=ask["card_key"], placed=r.get("placed", False),
                         ticket=r.get("ticket"), lots=r.get("lots"))

        # Edit the card
        notify.edit_card(ask["card_key"], {
            "Decision": f"🤖 AGENT TOOK {ask['side']} {r.get('lots', ''):g} lots · {r.get('line', '')}"[:1024]
        })

    t = threading.Thread(target=_run, daemon=True, name=f"agent-entry-{ask['card_key']}")
    t.start()
    return t


def maybe_schedule(cfg, agents, journal, notify, agent, ask: dict, sleep=_time.sleep) -> bool:
    """Called after track_ask. If the symbol is in AGENT mode, schedule delayed execution. Returns True if scheduled."""
    from .control import current_mode
    ctrl = current_mode(journal, agent.symbol)
    if ctrl["mode"] != "AGENT":
        return False
    schedule_agent_entry(cfg, agents, journal, notify, agent, ask, sleep=sleep)
    return True
