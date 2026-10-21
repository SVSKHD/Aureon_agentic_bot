"""EMA 9/21 — minimal HTML report: summary + candidate / trade tables (no interactive replay in this version)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _t(ts: int, off: float) -> str:
    return (datetime.fromtimestamp(ts, tz=timezone.utc) - timedelta(hours=off) + timedelta(hours=5.5)).strftime("%a %d %b %H:%M")


def render_html(res: dict, symbol: str, path: str, title: str = "", server_offset_h: float = 3.0, **_):
    s = res.get("summary", {})
    rows = "".join(f"<tr><td>{_t(e.time, server_offset_h)}</td><td>{e.label}</td><td>{e.direction}</td><td>{e.reason}</td>"
                   f"<td>{e.info.get('dist9', 0):+.1f}</td><td>{e.info.get('dist200', 0):+.1f}</td><td>{' '.join(e.info.get('last3', []))}</td></tr>"
                   for e in res.get("events", []) if e.label in ("EB", "ES"))
    trades = "".join(f"<tr><td>{_t(j.entry_time, server_offset_h)}</td><td>{j.direction}</td><td>{j.entry_price:.2f}</td>"
                     f"<td>{_t(j.exit_time, server_offset_h) if j.exit_time else '—'}</td><td>{j.exit_reason}</td><td>{j.result:+.1f}</td>"
                     f"<td>+{j.mfe:.1f}</td><td>−{j.mae:.1f}</td></tr>" for j in res.get("journeys", []))
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>{symbol} EMA 9/21 {title}</title>
<style>body{{font:14px system-ui;background:#121417;color:#e6e8eb;padding:16px}}table{{border-collapse:collapse;margin:12px 0}}
td,th{{border:1px solid #2a2f37;padding:4px 8px;font-family:ui-monospace,monospace;font-size:12px}}h2{{font-size:16px}}</style></head><body>
<h2>{symbol} · EMA 9/21 · {title}</h2>
<p>crosses {s.get("crosses", 0)} · pre-cross cards {s.get("pre", 0)} · trades {s.get('trades', 0)} ·
win {s.get('win_rate', 0) * 100:.0f}% · net {s.get('net', 0):+.1f} · stops {s.get('stops', 0)} · secured {s.get('secured', 0)} ·
EMA9 turn {s.get('ema9_turn', 0)} · day end {s.get('day_end', 0)}</p>
<h2>Crosses</h2><table><tr><th>IST</th><th>label</th><th>dir</th><th>reason</th><th>dist9</th><th>dist200</th><th>last 3</th></tr>{rows}</table>
<h2>Trades</h2><table><tr><th>entry</th><th>dir</th><th>price</th><th>exit</th><th>why</th><th>result</th><th>MFE</th><th>MAE</th></tr>{trades}</table>
</body></html>"""
    open(path, "w", encoding="utf-8").write(html)
    return path
