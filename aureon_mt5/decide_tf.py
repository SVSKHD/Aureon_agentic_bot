"""v2.14.0 — /decide-timeframe: one command sets the whole ladder.

    plan(cfg, tf, avg_range)   exec TF → its two confirm TFs, the confirmation rule for that TF (TF_RULES), the follow knobs
                               rescaled from the exec TF's average range (step ≈ 1.5 × avg range rounded to 5, floor 10;
                               secure ≈ 0.5 × avg range rounded to 1, floor 3; stop = max(FOLLOW_STOP, 1.5 × avg range)), the
                               regime windows (2 h / 1 h of exec bars).
    compare(cfg, tf, days)     the replay of the last `days` with that trio (HTF gate block, follow) AND the same exec TF with
                               the gate off; cached per (tf, days, date) in logs/decide_tf_cache.json. No Claude, no budget.
    decide(...)                apply:false → the plan + the comparison, nothing changes. apply:true → the overrides through
                               features.set_override (source discord), journal `timeframe_decided`, a loud card, and the
                               /mode-style restart (exec_tf is not hot) — refused with an open / unverified position unless
                               force. The gate goes in as `show`, never `block`: promoting it stays a separate /feature-set."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from .common import tf_synth

IST = timezone(timedelta(hours=5, minutes=30))
CACHE = "decide_tf_cache.json"


def _round_to(x: float, base: float) -> float:
    return float(base * round(float(x) / base))


def rescale(avg_range: float) -> dict:
    step = max(10.0, _round_to(1.5 * avg_range, 5))
    secure = max(3.0, float(round(0.5 * avg_range)))
    return {"follow_step": step, "follow_secure_at": secure, "follow_stop_x_range": 1.5}


def plan(cfg, tf: str, avg_range: float, days: int = 30) -> dict:
    tf = tf.upper()
    if tf not in tf_synth.EXEC_CHOICES:
        raise ValueError(f"exec timeframe must be one of {', '.join(tf_synth.EXEC_CHOICES)}")
    confirm = list(tf_synth.confirm_tfs(tf))
    r = tf_synth.TF_RULES.get(tf, tf_synth.TF_RULES["M30"])
    k = rescale(avg_range)
    stop_floor = float(getattr(cfg, "follow_stop", 25.0))
    return {"tf": tf, "confirm": confirm, "synthetic": [c for c in confirm if c in tf_synth.SYNTHETIC], "days": days,
            "avg_range": round(float(avg_range), 2), "hold": r["hold"], "window": r["window"], "gap_x": r["gap_x"],
            "stop_floor": stop_floor, "stop_value": round(max(stop_floor, 1.5 * float(avg_range)), 1), **k,
            "regime_window_bars": tf_synth.bars_for(120, tf), "flat_window_bars": tf_synth.bars_for(60, tf),
            "pre_timeout_bars": tf_synth.bars_for(60, tf)}


def plan_text(p: dict) -> str:
    conf = ", ".join(c + ("*" if c in p["synthetic"] else "") for c in p["confirm"])
    gap = f"gap {p['gap_x']:g}× range" if p["gap_x"] else "gap 1.5 pts"
    return (f"exec {p['tf']} · confirm {conf} · hold {p['hold']} bars · {gap} · window {p['window']} bars · "
            f"follow stop = max(FOLLOW_STOP {p['stop_floor']:g}, 1.5× avg range of {p['tf']} over {p['days']} d = "
            f"{1.5 * p['avg_range']:.1f}) = {p['stop_value']:g} · step {p['follow_step']:g} · secure at +{p['follow_secure_at']:g} · "
            f"regime windows {p['regime_window_bars']}/{p['flat_window_bars']} bars · P timeout {p['pre_timeout_bars']} bars"
            + (" · * = synthetic from M15" if p["synthetic"] else ""))


def knobs(p: dict) -> dict:
    """Exactly the overrides apply:true writes (feature name → value as Discord would type it)."""
    return {"exec_tf": p["tf"].lower(), "htf_confirm_tfs": ",".join(p["confirm"]), "htf_align": "show",
            "follow_step": f"{p['follow_step']:g}", "follow_secure_at": f"{p['follow_secure_at']:g}",
            "follow_stop_x_range": f"{p['follow_stop_x_range']:g}", "confirm_hold_bars": str(p["hold"]),
            "confirm_gap_x_range": f"{p['gap_x']:g}", "confirm_window_bars": str(p["window"])}


def replay_sets(p: dict, gate: str) -> list[str]:
    return [f"exec_tf={p['tf']}", f"htf_align={gate}", f"htf_confirm_tfs={','.join(p['confirm'])}", "guardian_profile=follow",
            f"follow_stop_x_range={p['follow_stop_x_range']:g}", f"follow_step={p['follow_step']:g}",
            f"follow_secure_at={p['follow_secure_at']:g}", f"confirm_hold_bars={p['hold']}",
            f"confirm_gap_x_range={p['gap_x']:g}", f"confirm_window_bars={p['window']}"]


def default_runner(cfg, p: dict, sets: list[str], days: int) -> dict:
    """tools/replay_guardian.py on the last `days` (MT5 history, synthetic M45 from M15)."""
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(root, "tools"))
    import replay_guardian as RG
    to = datetime.now(timezone.utc).date(); fr = to - timedelta(days=days)
    argv = ["--symbol", cfg.symbols[0], "--mode", cfg.mode, "--from", f"{fr}", "--to", f"{to}", "--out",
            os.path.join(cfg.log_dir, "replay")]
    for s_ in sets:
        argv += ["--set", s_]
    return RG.main(argv)["combo"]


def _summary(res: dict) -> dict:
    tr = res.get("trades_detail") or []
    out = {"trades": res.get("trades", 0), "net": res.get("total_points", 0.0), "max_dd": res.get("max_drawdown", 0.0),
           "stop_outs": sum(1 for t in tr if t.get("points", 0) <= 0 and t.get("reason") in ("stop", "sl_hit"))}
    if "asks_blocked_by_htf" in res:
        out.update(blocked=res["asks_blocked_by_htf"], blocked_won=res["htf_would_have_won"], blocked_lost=res["htf_would_have_lost"],
                   pre={k: res["by_htf_entry"]["pre"][k] for k in ("trades", "total_points")},
                   with_={k: res["by_htf_entry"]["with"][k] for k in ("trades", "total_points")})
    return out


def compare(cfg, p: dict, *, runner=None, now=None) -> dict:
    """{"block": summary, "off": summary, "cached": bool}. Cached per (tf, days, IST date)."""
    day = (now or datetime.now(IST)).strftime("%Y-%m-%d")
    key = f"{p['tf']}|{p['days']}|{day}"
    path = os.path.join(cfg.log_dir, CACHE)
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return {**cache[key], "cached": True}
    run = runner or (lambda sets: default_runner(cfg, p, sets, p["days"]))
    res = {"block": _summary(run(replay_sets(p, "block"))), "off": _summary(run(replay_sets(p, "off")))}
    cache[key] = res
    os.makedirs(cfg.log_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1)
    return {**res, "cached": False}


def compare_text(c: dict) -> str:
    b, o = c["block"], c["off"]
    lines = [f"HTF block: {b['trades']} trades · net {b['net']:+.1f} · max DD {b['max_dd']:.1f} · stop-outs {b['stop_outs']}",
             f"HTF off  : {o['trades']} trades · net {o['net']:+.1f} · max DD {o['max_dd']:.1f} · stop-outs {o['stop_outs']}"]
    if "blocked" in b:
        lines.append(f"blocked by HTF: {b['blocked']} (would have won {b['blocked_won']}, lost {b['blocked_lost']}) · "
                     f"entered on pre: {b['pre']['trades']} ({b['pre']['total_points']:+.1f}) · on with: {b['with_']['trades']} "
                     f"({b['with_']['total_points']:+.1f})")
    return "\n".join(lines) + ("\n(cached)" if c.get("cached") else "")


def current_text(cfg, journal=None) -> str:
    from .htf_align import confirm_tfs_for
    ex = str(getattr(cfg, "exec_tf", "M5")).upper()
    rows = ["**Timeframe ladder** (exec → confirm)"]
    for tf in tf_synth.EXEC_CHOICES:
        try:
            c = tf_synth.confirm_tfs(tf)
            rows.append(f"{'▶ ' if tf == ex else '  '}{tf} → {', '.join(x + ('*' if x in tf_synth.SYNTHETIC else '') for x in c)}")
        except ValueError:
            pass
    rows.append(f"now: exec {ex} · confirm {', '.join(confirm_tfs_for(cfg))} · HTF gate {getattr(cfg, 'htf_align', 'off')}")
    last = None
    try:
        last = next((r for r in reversed(journal.read(0)) if r.get("event") == "timeframe_decided"), None) if journal else None
    except Exception:
        last = None
    rows.append(f"last decision: {last['tf']} → {', '.join(last['confirm'])} by {last.get('by')} "
                f"({datetime.fromtimestamp(last['t'], tz=IST):%d %b %H:%M} IST)" if last else "last decision: none")
    return "\n".join(rows) + "\n* = synthetic from M15"


def decide(cfg, agents, journal, notify, user, tf: str, *, apply: bool = False, days: int = 30, force: bool = False,
           admin_role: str | None = None, runner=None, avg_range_fn=None, restart=None, advisors=()) -> str:
    from . import features as F, ops
    role = os.environ.get("AUREON_ADMIN_ROLE_ID", "") if admin_role is None else admin_role
    if not ops.admin_allowed(user, role):
        return "Denied: configured administrator role required"
    if not tf:
        return current_text(cfg, journal)
    try:
        avg = float((avg_range_fn or (lambda t, d: default_avg_range(cfg, t, d)))(tf.upper(), days))
        p = plan(cfg, tf, avg, days)
    except ValueError as e:
        return f"⚠️ {e}"
    c = compare(cfg, p, runner=runner)
    text = f"**/decide-timeframe {p['tf']}**\n{plan_text(p)}\n{compare_text(c)}"
    if not apply:
        return text + "\napply:false — nothing changed"
    if not force and ops.positions_block_mode_change(agents):
        return text + "\n⛔ a position is open (or the snapshot cannot prove flat) — exec_tf needs a restart; retry with force:true"
    ks = knobs(p)
    for name, value in ks.items():
        F.set_override(cfg.log_dir, cfg, name, value, str(user), advisors=list(advisors))
    journal.log("timeframe_decided", tf=p["tf"], confirm=p["confirm"], knobs=ks, replay_summary={k: v for k, v in c.items() if k != "cached"},
                by=str(user))
    notify.card(f"AUREON · MT5 · TIMEFRAME DECIDED · {p['tf']} → {', '.join(p['confirm'])}",
                f"{plan_text(p)}\n{compare_text(c)}\nHTF gate set to `show` (promote to block with /feature-set htf_align block)\n"
                "restarting — exec_tf is not hot",
                footer=f"by {user} · {datetime.now(IST):%H:%M} IST")
    if restart is not None:
        restart(user, f"decide-timeframe {p['tf']}")
    return text + f"\n✅ applied {len(ks)} overrides · restarting"


def default_avg_range(cfg, tf: str, days: int) -> float:
    """The exec TF's average bar range over `days` (MT5)."""
    from . import broker
    per_day = max(1, int(24 * 60 / tf_synth.tf_minutes(tf)))
    d = broker.bars_tf(cfg.symbols[0], tf, min(20000, per_day * days + 50), cfg.source, cfg.server_utc_offset)
    return float((d["high"] - d["low"]).tail(per_day * days).mean())
