"""v3.0 ema921e — the /e commands as plain functions (bot.py wraps them; tests call them directly). Each returns the reply text.

    /e status            engine status + execution: side, ATR, ATR ratio, position, entry, bars in, next cut bar, MFE,
                         floor (price, pts, ATR), month P&L, today's skips by reason, regime / drawdown (show only)
    /e on | /e off       new entries on / off (an open position keeps being managed)
    /e flat              close now
    /e set <key> <value> hot config change, echoes the new value
    /e live <account>    paper → live: needs AUREON_ALLOW_LIVE=1 AND the account number typed in the same message
    /e demo              back to paper (demo account only)
"""
from __future__ import annotations

from collections import Counter

from .cards import ist_of


def _runner(get):
    r = get() if callable(get) else get
    if r is None:
        raise LookupError("ema921e is not running in this process (AUREON_EMA921E=1 / --e921e starts it)")
    return r


def status(get) -> str:
    r = _runner(get); s = r.status(); off = r.off
    side = {1: "9 > 21 (bull)", -1: "9 < 21 (bear)", 0: "flat lines"}.get(int(s.get("side") or 0))
    ratio = s.get("atr_ratio")
    lines = [f"**ema921e · {r.symbol} {r.tf}** · entries {'ON' if r.enabled else 'OFF'} · "
             f"{'PAPER (demo only)' if s['paper'] else 'LIVE'} · broker {s['broker']} · {r.health.get('status')}",
             f"EMA {side} · ATR {float(s['atr'] or 0):.2f} · ratio {('%.2f' % ratio) if ratio is not None else '—'}"
             f" vs norm (gate {r.settings.get('atr_gate')}) · last bar {s.get('last_bar')} srv"]
    if s.get("in_position"):
        ps = r.eng.pos
        cut_bar = int(r.times[ps.i]) + r.eng.p.cut_bars * r.bar_s if ps.i < len(r.times) else None
        cut = ("passed" if s["bars_in"] >= r.eng.p.cut_bars else
               f"bar {r.eng.p.cut_bars} closes {ist_of(cut_bar + r.bar_s, off):%H:%M} IST") if cut_bar is not None else "—"
        fl = (f"{ps.floor_price:.2f} (+{ps.floor_atr * ps.atr:.1f} pts · +{ps.floor_atr:.2f} ATR)" if ps.floor_atr is not None
              else "none yet (first rung +%.1f pts)" % (sorted(r.eng.p.ladder)[0][0] * ps.atr))
        fill = s.get("entry_fill")
        lines.append(f"IN {s['dir']} @ {s['entry']:.2f}" + (f" (fill {fill:.2f})" if fill and abs(fill - s['entry']) > 1e-9 else "")
                     + f" · ticket #{s.get('ticket') or '— (engine only)'} · bars in {s['bars_in']} · cut {cut} · "
                     f"MFE +{s['mfe_pts']:.1f} pts · floor {fl}"
                     + (f" · disaster stop {ps.entry - ps.side * r.eng.p.stop_atr * ps.atr:.2f}" if r.eng.p.stop_atr else ""))
    else:
        lines.append("no position")
    day = ist_of(r.broker.now_server(), off).strftime("%Y-%m-%d")
    sk = Counter(rs for t, rs in r.skip_log if ist_of(t, off).strftime("%Y-%m-%d") == day)
    lines.append(f"month P&L (engine) ${s['month_pnl']:+,.0f} · today's skips: "
                 + (", ".join(f"{k} {v}" for k, v in sk.items()) if sk else "none"))
    dd = s.get("dd") or {}
    lines.append(f"show only · regime {s.get('regime') or '—'} · drawdown "
                 + (f"${dd['dd']:,.0f} of ${dd['limit']:,.0f}{' (would block)' if dd.get('would_block') else ''}" if dd.get("limit")
                    else "guard off" if dd else "—"))
    return "\n".join(lines)


def on(get) -> str:
    r = _runner(get); r.set_enabled(True)
    return f"ema921e entries ON · lot {r.lot:g} · {'PAPER (demo only)' if r.settings.get('paper', True) else 'LIVE'}"


def off(get) -> str:
    r = _runner(get); r.set_enabled(False)
    return "ema921e entries OFF" + (" · the open position keeps being managed" if r.st.get("ticket") else "")


def flat(get) -> str:
    return "ema921e: " + _runner(get).flat("/e flat")


def set_(get, key: str, value: str) -> str:
    from .config import ConfigError
    r = _runner(get)
    try:
        k, v = r.set(key, value)
    except ConfigError as e:
        return f"refused — {e}"
    return f"ema921e {k} = {v!r}" + (" (from the next bar; the open trade keeps its entry ATR)" if r.eng.pos is not None else "")


def live(get, account: str, *, allow_live: bool) -> str:
    from .config import set_key
    r = _runner(get)
    if not allow_live:
        return "refused — /e live needs AUREON_ALLOW_LIVE=1 in the environment"
    login = r.broker.account_login() if hasattr(r.broker, "account_login") else None
    if login is None:
        return "refused — no account number from MT5"
    if str(account or "").strip() != str(login):
        return "refused — type the account number in the same message: /e live <account number>"
    if r.config_path:
        set_key(r.config_path, "paper", False, allow_command_keys=True)
        if r.watcher is not None:
            r.watcher.mtime = r.watcher._mtime()
    r.apply_settings({**r.settings, "paper": False}, source="/e live")
    r.log("ema921e_live", account_masked=f"***{str(login)[-3:]}")
    return f"ema921e LIVE on account ***{str(login)[-3:]} ({r.broker.account_kind() or '?'}) · /e demo to go back"


def demo(get) -> str:
    from .config import set_key
    r = _runner(get)
    if r.config_path:
        set_key(r.config_path, "paper", True, allow_command_keys=True)
        if r.watcher is not None:
            r.watcher.mtime = r.watcher._mtime()
    r.apply_settings({**r.settings, "paper": True}, source="/e demo")
    return "ema921e PAPER · orders only on a demo account"
