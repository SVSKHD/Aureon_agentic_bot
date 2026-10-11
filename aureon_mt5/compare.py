"""v1.11.0 — weekly compare: Detector vs Claude vs Me on the SAME signals, plus Claude's pullback calls vs the guardian.

Measurement only: nothing here changes detection, the guardian, Claude calls or trades.
Grading is the scorecard's (reports.graded_signals → reports.scorecard); this module never grades a signal itself.
Computed locally from logs/journal.jsonl. No network calls (the Saturday batch reads latest_summary())."""
from __future__ import annotations

import hashlib
import json
import os
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .common.news import session_of

IST = timezone(timedelta(hours=5, minutes=30))
BAR_S = 300
MATCH_BARS = 6                    # a trade opened within 6 bars (30 min) after the signal bar closed = taken
MIN_N = 30                        # below this, the only verdict is "not enough data"
STRATEGY = "ema5080"                                   # dedupe-key prefix (kept)
KIND_OF_SIGNAL = {"P": "P", "cross": "CROSS", "reentry": "RE"}                     # ema5080 entry signals
KINDS_BY_MODE = {"ema5080": KIND_OF_SIGNAL, "ema2050": {"pullback": "ENTER", "no-pullback": "ENTER"}}   # v2.0.0: ema2050 ENTER bars
ENTRY_EVENTS = ("P", "CROSS", "RE", "ENTER")
WINS = ("WIN20", "WIN10")
SESSIONS = {"asia": "Asia", "london": "London", "ny": "New York", "off": "Off-hours"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_PATH = os.path.join(ROOT, "prompts", "claude_rules.md")

ROWS = [("detector", "Detector · all"), ("claude_take", "Claude · TAKE"), ("claude_skip", "Claude · SKIP (avoided)"),
        ("me_taken", "Me · TAKEN"), ("me_skipped", "Me · SKIPPED"),
        ("dis_ct_ms", "Claude TAKE · me SKIP"), ("dis_cs_mt", "Claude SKIP · me TAKE")]


# ============================================================================= rows (one per detector signal)
@dataclass
class Row:
    symbol: str
    kind: str                     # P | CROSS | RE
    side: str                     # LONG | SHORT
    bar: int                      # MT5 server epoch of the signal bar
    session: str
    status: str = "UNGRADED"      # GRADED | OPEN | UNGRADED
    grade: str | None = None
    points: float | None = None
    money: float | None = None
    claude: str | None = None     # TAKE | SKIP | None (= no verdict)
    me: str = "SKIPPED"           # TAKEN | SKIPPED
    me_via: str = ""              # button | mt5


def _valid_entry_verdict(v: dict) -> bool:
    return v.get("decision") in ("TAKE", "SKIP") and not v.get("stale") and v.get("claude_event") in ENTRY_EVENTS


def build_rows(records: list[dict], graded: list, since: int, until: int, off: float) -> list[Row]:
    """records: journal rows (may extend past `until` so late verdicts/decisions/trades still join).
    graded: reports.GradedSignal list. Signals are those journaled in [since, until)."""
    sigs = [r for r in records if r.get("event") == "signal" and r.get("mode", STRATEGY) in KINDS_BY_MODE
            and r.get("signal_kind") in KINDS_BY_MODE[r.get("mode", STRATEGY)] and since <= r["t"] < until and r.get("bar") is not None]
    grades = {(g.symbol, g.kind, g.side, int(g.bar)): g for g in graded}
    verdicts = {}
    for v in records:
        if v.get("event") == "claude_verdict" and v.get("claude_event") in ENTRY_EVENTS:
            verdicts[(v.get("symbol"), v.get("claude_event"), v.get("bar"))] = v          # latest wins
    decisions = {}
    for d in records:
        if d.get("event") == "decision" and d.get("decision") in ("TAKE", "SKIP", "EXPIRED"):
            decisions[(d.get("symbol"), d.get("bar"))] = d["decision"]
    rows, seen = [], set()
    for s in sorted(sigs, key=lambda r: r["bar"]):
        kind = KINDS_BY_MODE[s.get("mode", STRATEGY)][s["signal_kind"]]; side = str(s.get("side", "")).upper()
        key = (s.get("symbol"), kind, side, int(s["bar"]))
        if key in seen:
            continue
        seen.add(key)
        row = Row(s.get("symbol"), kind, side, int(s["bar"]), SESSIONS.get(session_of(int(s["bar"]), off), "Off-hours"))
        g = grades.get(key)
        if g is not None:
            row.grade = g.grade; row.money = g.money
            if g.session:
                row.session = SESSIONS.get(str(g.session).lower(), str(g.session))
            if g.grade == "OPEN":
                row.status = "OPEN"
            elif g.points is not None:
                row.status = "GRADED"; row.points = float(g.points)
        v = verdicts.get((row.symbol, kind, row.bar))
        if v is not None and _valid_entry_verdict(v):
            row.claude = v["decision"]
        if decisions.get((row.symbol, row.bar)) == "TAKE":
            row.me, row.me_via = "TAKEN", "button"
        rows.append(row)
    # trades seen in MT5: assign each to the most recent same-side signal whose window contains its open time
    for p in records:
        if p.get("event") != "position_seen" or p.get("open_time") is None:
            continue
        ot = int(p["open_time"])
        cands = [r for r in rows if r.symbol == p.get("symbol") and r.side == p.get("side")
                 and r.bar <= ot <= r.bar + (MATCH_BARS + 1) * BAR_S]
        if cands:
            r = max(cands, key=lambda x: x.bar)
            if r.me != "TAKEN":
                r.me, r.me_via = "TAKEN", "mt5"
    return rows


# ============================================================================= metrics
@dataclass
class Metrics:
    n: int = 0
    wins: int = 0
    win_rate: float | None = None
    expectancy: float | None = None
    total: float = 0.0
    stops: int = 0
    worst_streak: int = 0
    biggest_loss: float = 0.0
    money: float | None = None
    open: int = 0
    ungraded: int = 0


def metrics(rows: list[Row]) -> Metrics:
    m = Metrics()
    g = sorted([r for r in rows if r.status == "GRADED"], key=lambda r: r.bar)
    m.open = sum(r.status == "OPEN" for r in rows); m.ungraded = sum(r.status == "UNGRADED" for r in rows)
    m.n = len(g)
    if not g:
        return m
    m.wins = sum(r.grade in WINS for r in g); m.win_rate = m.wins / m.n
    m.total = round(sum(r.points for r in g), 2); m.expectancy = round(m.total / m.n, 2)
    m.stops = sum(r.grade == "STOP" for r in g)
    run = 0
    for r in g:
        run = run + 1 if r.points < 0 else 0
        m.worst_streak = max(m.worst_streak, run)
    m.biggest_loss = round(min(0.0, min(r.points for r in g)), 2)
    if all(r.money is not None for r in g):
        m.money = round(sum(r.money for r in g), 2)
    return m


def groups(rows: list[Row]) -> dict[str, list[Row]]:
    return {
        "detector": rows,
        "claude_take": [r for r in rows if r.claude == "TAKE"],
        "claude_skip": [r for r in rows if r.claude == "SKIP"],
        "me_taken": [r for r in rows if r.me == "TAKEN"],
        "me_skipped": [r for r in rows if r.me == "SKIPPED"],
        "dis_ct_ms": [r for r in rows if r.claude == "TAKE" and r.me == "SKIPPED"],
        "dis_cs_mt": [r for r in rows if r.claude == "SKIP" and r.me == "TAKEN"],
        "no_verdict": [r for r in rows if r.claude is None],
    }


# ============================================================================= verdict lines
def verdict(m: dict[str, Metrics]) -> list[str]:
    det, take, skip, me = m["detector"], m["claude_take"], m["claude_skip"], m["me_taken"]
    claude_n = take.n + skip.n
    if claude_n < MIN_N or det.n < MIN_N:
        return [f"Not enough data yet — {min(claude_n, det.n)}/{MIN_N} signals. Keep advisory running."]
    c1 = skip.n > 0 and skip.expectancy < 0
    c2 = take.n > 0 and take.expectancy > det.expectancy
    c3 = take.total >= 0.8 * det.total
    if c1 and c2 and c3:
        line = "✅ Claude helps — its skips lost, its takes beat the detector and kept ≥80% of the points."
    elif skip.n > 0 and not c1:
        line = "⚠️ Claude is skipping good trades — keep it as reviewer only."
    else:
        line = "➖ Mixed — no clear edge yet."
    out = [line]
    if me.n:
        d = round(me.expectancy - det.expectancy, 2)
        out.append(f"You: your filtering {'added' if d >= 0 else 'cost'} {abs(d):.1f} pts per signal "
                   f"(you {me.expectancy:+.1f} vs detector {det.expectancy:+.1f}).")
    else:
        out.append("You: none of the graded signals were taken.")
    return out


def disagreements(g: dict[str, list[Row]]) -> dict:
    """Right = the side whose choice caught a gain (points > 0 → the taker) or avoided a loss (points < 0 → the skipper)."""
    c = y = t = 0
    for r in g["dis_ct_ms"]:                       # Claude TAKE, me SKIP
        if r.status != "GRADED":
            continue
        if r.points > 0: c += 1
        elif r.points < 0: y += 1
        else: t += 1
    for r in g["dis_cs_mt"]:                       # Claude SKIP, me TAKE
        if r.status != "GRADED":
            continue
        if r.points > 0: y += 1
        elif r.points < 0: c += 1
        else: t += 1
    return {"n": c + y + t, "claude": c, "you": y, "tie": t}


# ============================================================================= pullbacks
@dataclass
class PullbackScore:
    n: int = 0
    right: int = 0
    diff_points: float = 0.0          # Σ (points following Claude − actual final), not-applied verdicts
    applied: int = 0                  # manage mode: acted on, counterfactual unknown
    ungraded: int = 0
    stale: int = 0
    by_decision: dict = field(default_factory=dict)


def _tighten_hit(bars, side: str, level: float, after: int, before: int | None) -> bool | None:
    if bars is None or len(bars) == 0:
        return None
    seg = bars[(bars["time"] > after) & ((bars["time"] <= before) if before is not None else True)]
    if len(seg) == 0:
        return None
    return bool((seg["low"] <= level).any()) if side == "long" else bool((seg["high"] >= level).any())


def pullbacks(records: list[dict], since: int, until: int, off: float, bars=None) -> PullbackScore:
    closes = {r.get("ticket"): r for r in records if r.get("event") == "closed"}
    exits = {r.get("ticket"): r for r in records if r.get("event") == "exit"}
    ps = PullbackScore()
    for v in records:
        if v.get("event") != "claude_verdict" or v.get("claude_event") != "pullback" or not (since <= v["t"] < until):
            continue
        dec = v.get("decision")
        if dec not in ("HOLD", "TIGHTEN", "CLOSE"):
            continue
        if v.get("stale"):
            ps.stale += 1; continue
        if v.get("acted"):
            ps.applied += 1; continue
        c = closes.get(v.get("ticket")); at = v.get("open_profit")
        final = (c or {}).get("final_points")
        if final is None and v.get("ticket") in exits:
            final = exits[v["ticket"]].get("points")
        if c is None or final is None or at is None:
            ps.ungraded += 1; continue
        final = float(final); at = float(at)
        if dec == "HOLD":
            right, implied = final >= at, final
        elif dec == "CLOSE":
            right, implied = final < at, at
        else:
            side = c.get("direction"); entry = c.get("entry"); tt = v.get("tighten_to")
            close_srv = int(c["t"] + off * 3600)
            b = bars.get(v.get("symbol")) if isinstance(bars, dict) else bars
            hit = _tighten_hit(b, side, float(tt), int(v.get("bar") or 0), close_srv) if (tt is not None and entry is not None) else None
            if hit is None:
                ps.ungraded += 1; continue
            implied = round((float(tt) - float(entry)) if side == "long" else (float(entry) - float(tt)), 2) if hit else final
            right = (implied >= final) if hit else True          # not hit = the trade continued, no harm
        ps.n += 1; ps.right += int(right); ps.diff_points = round(ps.diff_points + (implied - final), 2)
        b = ps.by_decision.setdefault(dec, {"n": 0, "right": 0}); b["n"] += 1; b["right"] += int(right)
    return ps


# ============================================================================= breakdowns
def breakdown(rows: list[Row]) -> dict[str, dict[str, Metrics]]:
    out = {}
    for label, pick in [(k, lambda r, k=k: r.kind == k) for k in ("P", "CROSS", "RE")] + \
                       [(s, lambda r, s=s: r.session == s) for s in ("Asia", "London", "New York")]:
        sub = [r for r in rows if pick(r)]
        g = groups(sub)
        out[label] = {k: metrics(g[k]) for k in ("detector", "claude_take", "me_taken")}
    return out


# ============================================================================= chart
def chart(rows: list[Row], path: str, off: float, title: str = "", end_ts: int | None = None) -> str | None:
    g = groups(rows)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except Exception:
        return None
    series = [("detector", "Detector · all", "#95A5A6"), ("claude_take", "Claude · TAKE", "#5865F2"), ("me_taken", "Me · TAKEN", "#57F287")]
    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=110)
    fig.patch.set_facecolor("#1e1f22"); ax.set_facecolor("#1e1f22")
    drew = False
    for key, label, colour in series:
        pts = sorted([r for r in g[key] if r.status == "GRADED"], key=lambda r: r.bar)
        if not pts:
            continue
        xs = [datetime.fromtimestamp(r.bar - off * 3600, tz=IST) for r in pts]
        ys, acc = [], 0.0
        for r in pts:
            acc += r.points; ys.append(acc)
        last_x = datetime.fromtimestamp(end_ts, tz=IST) if end_ts else xs[-1]
        xs = [xs[0]] + xs + [max(last_x, xs[-1])]; ys = [0.0] + ys + [acc]       # start at 0, carry the last level to the end
        ax.step(xs, ys, where="post", label=f"{label} ({acc:+.1f})", color=colour, linewidth=2, alpha=0.95); drew = True
    ax.axhline(0, color="#4e5058", linewidth=0.8)
    loc = mdates.AutoDateLocator(tz=IST); ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc, tz=IST))
    for s in ax.spines.values():
        s.set_color("#4e5058")
    ax.tick_params(colors="#dbdee1", labelsize=8)
    ax.set_ylabel("cumulative points (if taken)", color="#dbdee1", fontsize=9)
    ax.set_xlabel("IST", color="#dbdee1", fontsize=8)
    ax.set_title(title or "Detector vs Claude vs Me", color="#f2f3f5", fontsize=11)
    if drew:
        ax.legend(facecolor="#2b2d31", edgecolor="#4e5058", labelcolor="#dbdee1", fontsize=8, loc="upper left")
    else:
        ax.text(0.5, 0.5, "no graded signals yet", transform=ax.transAxes, ha="center", color="#dbdee1")
    fig.tight_layout()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fig.savefig(path, facecolor=fig.get_facecolor()); plt.close(fig)
    return path


# ============================================================================= the whole compare
@dataclass
class CompareResult:
    since: int
    until: int
    rows: list
    groups: dict
    metrics: dict
    verdict: list
    disagree: dict
    pullback: PullbackScore
    grading_note: str | None
    models: list
    rules_hash: str
    png: str | None = None
    history: list = field(default_factory=list)
    alerts: dict | None = None           # v2.0.0: alerts.stats() of the window (fired, taken, agreed, win/loss)


def rules_hash(path: str = RULES_PATH) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:8]
    except Exception:
        return "—"


def compute(records: list[dict], graded: list, since: int, until: int, off: float, *, grading_note: str | None = None,
            bars=None, png_path: str | None = None, history: list | None = None) -> CompareResult:
    rows = build_rows(records, graded, since, until, off)
    g = groups(rows)
    m = {k: metrics(v) for k, v in g.items()}
    if graded and rows and not any(r.grade for r in rows) and grading_note is None:
        grading_note = f"scorecard graded {len(graded)} signal(s) but none matched a journal signal bar — check the bar-time field"
    models = sorted({str(v.get("model")) for v in records if v.get("event") == "claude_verdict" and v.get("model")
                     and since <= v["t"] < until})
    from . import alerts as _alerts
    res = CompareResult(since, until, rows, g, m, verdict(m), disagreements(g), pullbacks(records, since, until, off, bars),
                        grading_note, models, rules_hash(), history=history or [],
                        alerts=_alerts.stats([r for r in records if since <= r.get("t", 0) < until]))
    if png_path:
        last_bar = max([r.bar for r in rows], default=None)
        res.png = chart(rows, png_path, off, f"Detector vs Claude vs Me · {_fmt_day(since)} → {_fmt_day(until - 1)}",
                        end_ts=int(last_bar - off * 3600 + BAR_S) if last_bar else None)
    return res


def _fmt_day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=IST).strftime("%d %b")


def window_days(days: int, now: float | None = None) -> tuple[int, int]:
    now = int(now if now is not None else _time.time())
    return now - int(days) * 86400, now + 1


def week_window(now: datetime) -> tuple[str, int, int]:
    """The Mon 00:00 → Sat 00:00 IST window of the week containing `now` (IST)."""
    d = now.astimezone(IST)
    mon = (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    y, w, _ = mon.isocalendar()
    return f"{y}-W{w:02d}", int(mon.timestamp()), int((mon + timedelta(days=5)).timestamp())


# ============================================================================= weekly history (+ Saturday batch summary)
def history_path(log_dir: str) -> str:
    return os.path.join(log_dir, "compare_weekly.jsonl")


def read_history(log_dir: str) -> list[dict]:
    out = []
    try:
        with open(history_path(log_dir), encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return out


def summary_for_batch(res: CompareResult, week: str | None = None) -> dict:
    """Compact `compare` field for the existing Saturday `aureon_weekly` row. No network call here."""
    def mm(k):
        x = res.metrics[k]
        return {"n": x.n, "win_rate": None if x.win_rate is None else round(x.win_rate, 3), "expectancy": x.expectancy,
                "total": x.total, "stops": x.stops, "worst_streak": x.worst_streak, "biggest_loss": x.biggest_loss}
    return {"week": week, "since": res.since, "until": res.until, "verdict": res.verdict[0],
            "rows": {k: mm(k) for k, _ in ROWS}, "no_verdict": len(res.groups["no_verdict"]),
            "open": res.metrics["detector"].open, "ungraded": res.metrics["detector"].ungraded,
            "disagree": res.disagree, "pullback": {"n": res.pullback.n, "right": res.pullback.right,
                                                   "diff_points": res.pullback.diff_points, "applied": res.pullback.applied},
            "models": res.models, "rules_hash": res.rules_hash, "grading_note": res.grading_note}


def append_history(log_dir: str, week: str, res: CompareResult):
    os.makedirs(log_dir, exist_ok=True)
    with open(history_path(log_dir), "a", encoding="utf-8") as f:
        f.write(json.dumps({"week": week, "posted_at": int(_time.time()), "compare": summary_for_batch(res, week)}, default=str) + "\n")


def latest_summary(log_dir: str) -> dict | None:
    """For the Saturday Supabase batch: the newest week's `compare` dict (add it to the aureon_weekly row)."""
    h = read_history(log_dir)
    return h[-1]["compare"] if h else None


def trend_line(history: list[dict], current: CompareResult | None = None, weeks: int = 4) -> str:
    pts = [(h.get("week", "?"), h.get("compare", {}).get("rows", {})) for h in history][-weeks:]
    if current is not None:
        pts = (pts + [("now", {k: {"expectancy": current.metrics[k].expectancy} for k in ("detector", "claude_take", "me_taken")})])[-weeks:]
    if not pts:
        return ""
    def seq(k):
        return " → ".join("—" if (r.get(k) or {}).get("expectancy") is None else f"{r[k]['expectancy']:+.1f}" for _, r in pts)
    return f"Detector {seq('detector')} · Claude TAKE {seq('claude_take')} · Me {seq('me_taken')}"


# ============================================================================= gathering (off the event loop; never holds the MT5 lock while grading)
_GRADE_CACHE: dict = {"at": 0.0, "since": None, "graded": [], "note": None}


def gather(cfg, agents, journal, since: int, until: int, *, lock_timeout: float = 2.0, png_dir: str | None = None,
           history: list | None = None) -> CompareResult:
    from . import broker, reports
    records = journal.read(since - 86400, until + 3 * 86400)          # late verdicts / decisions / closes still join
    with broker.try_lock(lock_timeout) as free:                         # probe only: grading itself runs unlocked
        pass
    if free:
        try:
            graded, note = reports.graded_signals(cfg, agents, journal, since - 86400)
        except Exception as e:
            graded, note = [], f"scorecard failed: {e!r}"[:200]
        if graded:
            _GRADE_CACHE.update(at=_time.time(), since=since - 86400, graded=graded, note=note)
    elif _GRADE_CACHE["graded"] and _GRADE_CACHE["since"] is not None and _GRADE_CACHE["since"] <= since - 86400:
        graded = _GRADE_CACHE["graded"]
        note = f"MT5 busy — grades from cache {int((_time.time() - _GRADE_CACHE['at']) / 60)} min ago"
    else:
        graded, note = [], "MT5 busy — grading skipped, try again in a minute"
    bars = None
    tight = {v.get("symbol") for v in records if v.get("event") == "claude_verdict" and v.get("decision") == "TIGHTEN"}
    if tight and free and not getattr(cfg, "dry", False):
        bars = {}
        n = min(20000, int((until - since) / BAR_S) + 600)
        for sym in tight:
            try:
                bars[sym] = broker.bars(sym, n, cfg.source, cfg.server_utc_offset).df
            except Exception:
                pass
    png = os.path.join(png_dir or os.path.join(cfg.log_dir, "charts"), f"compare_{since}_{until}.png")
    return compute(records, graded, since, until, cfg.server_utc_offset, grading_note=note, bars=bars, png_path=png,
                   history=history if history is not None else read_history(cfg.log_dir))


# ============================================================================= cards (house style, same as notify.py)
def _fmt(x, f="{:+.1f}", none="—"):
    return none if x is None else f.format(x)


def table(res: CompareResult) -> str:
    money = any(res.metrics[k].money is not None for k, _ in ROWS)
    head = f"{'Row':<24}{'n':>4}{'win':>6}{'exp':>7}{'total':>8}{'STOP':>5}{'strk':>5}{'worst':>7}" + (f"{'money':>9}" if money else "")
    lines = [head, "-" * len(head)]
    for k, label in ROWS:
        m = res.metrics[k]
        lines.append(f"{label:<24}{m.n:>4}{_fmt(m.win_rate, '{:.0%}'):>6}{_fmt(m.expectancy):>7}{_fmt(m.total if m.n else None):>8}"
                     f"{m.stops:>5}{m.worst_streak:>5}{_fmt(m.biggest_loss if m.n and m.biggest_loss < 0 else None):>7}"
                     + (f"{_fmt(m.money, '{:+,.0f}'):>9}" if money else ""))
    lines.append(f"{'No verdict (excluded)':<24}{len(res.groups['no_verdict']):>4}")
    return "```\n" + "\n".join(lines) + "\n```"


def card(res: CompareResult, *, title: str = "AUREON · MT5 · COMPARE", mode_display: str = "EMA 50/80") -> dict:
    """{title, description, fields, footer, png} — used by /compare and the Saturday post."""
    det = res.metrics["detector"]
    desc = [f"**{_fmt_day(res.since)} → {_fmt_day(res.until - 1)}** · same detector signals · points = if-taken result (scorecard)",
            table(res), "**Verdict:** " + res.verdict[0]] + res.verdict[1:]
    d = res.disagree
    fields = [{"name": "Disagreements", "value": (f"When we disagreed ({d['n']}): Claude right {d['claude']}, you right {d['you']}"
                                                   + (f", tie {d['tie']}" if d["tie"] else "")) if d["n"] else "none graded yet", "inline": False}]
    pb = res.pullback
    pbt = (f"{pb.n} scored · right {pb.right}/{pb.n} ({pb.right / pb.n:.0%}) · vs guardian-only {pb.diff_points:+.1f} pts"
           if pb.n else "no scored pullback verdicts")
    if pb.by_decision:
        pbt += "\n" + " · ".join(f"{k} {v['right']}/{v['n']}" for k, v in sorted(pb.by_decision.items()))
    extra = [x for x in (f"applied (manage) {pb.applied}" if pb.applied else "", f"ungraded {pb.ungraded}" if pb.ungraded else "",
                         f"stale {pb.stale}" if pb.stale else "") if x]
    if extra:
        pbt += "\n" + " · ".join(extra)
    fields.append({"name": "Pullbacks (Claude vs guardian)", "value": pbt, "inline": False})
    fields.append({"name": "Excluded", "value": f"OPEN {det.open} · UNGRADED {det.ungraded} · no verdict {len(res.groups['no_verdict'])}", "inline": False})
    al = res.alerts or {}
    if al.get("fired") or al.get("taken"):                               # v2.0.2 ALERTS table: fired · taken · agent✓ · claude✓ · pts taken
        from . import alerts as _alerts
        fields.append({"name": "ALERTS", "value": "```\n" + _alerts.stats_table(al) + "\n```" +
                       f"skipped {al['skipped']} · taken & closed {al['graded']}: win {al['wins']} / loss {al['losses']}", "inline": False})
    tl = trend_line(res.history, res)
    if tl:
        fields.append({"name": "Expectancy · last 4 weeks", "value": tl[:1024], "inline": False})
    if res.grading_note:
        fields.append({"name": "Grading", "value": "⚠️ " + res.grading_note[:1000], "inline": False})
    footer = f"{mode_display} · models {', '.join(res.models) or '—'} · rules {res.rules_hash} · measurement only"
    return {"title": title, "description": "\n".join(desc)[:4096], "fields": fields, "footer": footer, "png": res.png}


def breakdown_card(res: CompareResult, *, mode_display: str = "EMA 50/80") -> dict:
    bd = breakdown(res.rows)
    head = f"{'':<10}{'Det n':>6}{'win':>5}{'exp':>7}{'Cl n':>6}{'exp':>7}{'Me n':>6}{'exp':>7}"
    lines = [head, "-" * len(head)]
    for label, m in bd.items():
        d, c, me = m["detector"], m["claude_take"], m["me_taken"]
        lines.append(f"{label:<10}{d.n:>6}{_fmt(d.win_rate, '{:.0%}'):>5}{_fmt(d.expectancy):>7}{c.n:>6}{_fmt(c.expectancy):>7}{me.n:>6}{_fmt(me.expectancy):>7}")
        if label == "RE":
            lines.append("")
    desc = (f"**{_fmt_day(res.since)} → {_fmt_day(res.until - 1)}** · by event type and session · Cl = Claude TAKE\n```\n"
            + "\n".join(lines) + "\n```")
    fields = [{"name": "Grading", "value": "⚠️ " + res.grading_note[:1000], "inline": False}] if res.grading_note else []
    return {"title": "AUREON · MT5 · COMPARE · BREAKDOWN", "description": desc[:4096], "fields": fields,
            "footer": f"{mode_display} · rules {res.rules_hash} · measurement only", "png": None}


# ============================================================================= optional: Claude's weekly self-review (AUREON_COMPARE_CLAUDE_REVIEW=1)
SELF_REVIEW_PROMPT = ("You are the second opinion for Aureon (XAUUSD M5, EMA 50/80). Below are this week's signals where you were wrong "
                      "(TAKE that lost, SKIP that won) or where the trader disagreed with you, with the scorecard's if-taken result "
                      "in points. Write exactly 5 short plain-text lines: what you got wrong and why. Do not propose edits to any "
                      "file; the trader edits the rules himself. Never invent data not in the cases.\n\nCASES:\n")


def self_review_cases(res: CompareResult, limit: int = 40) -> list[dict]:
    out = []
    for r in res.rows:
        if r.status != "GRADED" or r.claude is None:
            continue
        wrong = (r.claude == "TAKE" and r.points < 0) or (r.claude == "SKIP" and r.points > 0)
        disagree = (r.claude == "TAKE" and r.me == "SKIPPED") or (r.claude == "SKIP" and r.me == "TAKEN")
        if wrong or disagree:
            out.append({"bar_ist": datetime.fromtimestamp(r.bar, tz=timezone.utc).strftime("%a %H:%M server"), "symbol": r.symbol,
                        "event": r.kind, "side": r.side, "session": r.session, "claude": r.claude, "trader": r.me,
                        "grade": r.grade, "points": r.points, "claude_wrong": wrong})
    return out[-limit:]


def self_review(claude, res: CompareResult, notify, week: str) -> str | None:
    """Runs in its own thread. Uses the advisor's normal call path (budget, one call at a time). Never writes claude_rules.md."""
    cases = self_review_cases(res)
    if not cases or claude is None:
        return None
    r = claude._call(SELF_REVIEW_PROMPT + json.dumps(cases, default=str), claude.cfg.claude_review_model, "review")
    if r is None or not r.ok or not r.text:
        return None
    text = "\n".join(r.text.strip().splitlines()[:5])[:1800]
    notify.send(f"{STRATEGY}:ALL:COMPARE_SELF_REVIEW:{week}", f"AUREON · MT5 · CLAUDE SELF-REVIEW · {week}", [text],
                fields=[{"name": "Cases", "value": str(len(cases)), "inline": True},
                        {"name": "Rules", "value": f"unchanged ({res.rules_hash}) — you edit claude_rules.md", "inline": True}],
                footer="Aureon MT5 · Claude · advisory only")
    return text


# ============================================================================= v2.0.0 CLAUDE RULE PROPOSALS (Saturday) — nothing changes without /claude-rules-approve
PROPOSAL_FIELDS = ("gap", "dist_to_fast_ema_pts", "swing_against_last6_pts", "atr20", "crosses_today", "bars_since_cross", "minutes_to_news",
                   "day_pnl_pts", "trades_today", "whipsaw_flips")
MIN_SUPPORT = 2


def _verdict_rows(records: list[dict], res: "CompareResult") -> list[dict]:
    """Entry verdicts of the window joined with their graded outcome: [{claude, points, fields, event, symbol, bar}]."""
    graded = {(r.symbol, r.bar): r for r in res.rows if r.status == "GRADED" and r.points is not None}
    out = []
    for v in records:
        if v.get("event") != "claude_verdict" or v.get("claude_event") not in ENTRY_EVENTS or v.get("decision") not in ("TAKE", "SKIP") or v.get("stale"):
            continue
        row = graded.get((v.get("symbol"), v.get("bar")))
        if row is None or not isinstance(v.get("snapshot_fields"), dict):
            continue
        out.append({"claude": v["decision"], "points": float(row.points), "fields": v["snapshot_fields"], "event": v["claude_event"],
                    "symbol": v.get("symbol"), "bar": v.get("bar"), "evidence": v.get("evidence") or []})
    return out


def rule_proposals(records: list[dict], res: "CompareResult", mode_display: str = "EMA 50/80", limit: int = 3) -> list[dict]:
    """Wrong calls (TAKE that lost, SKIP that won) + their snapshot fields → up to `limit` proposed rule lines with the supporting count.
    Deterministic: for each numeric field, every wrong value is tried as a >= / <= threshold; the best side (most wrong minus right)
    is proposed when it holds >= MIN_SUPPORT wrong calls and strictly more wrong than right calls. Nothing is written by this function."""
    rows = _verdict_rows(records, res)
    wrong = [r for r in rows if (r["claude"] == "TAKE" and r["points"] < 0) or (r["claude"] == "SKIP" and r["points"] > 0)]
    right = [r for r in rows if r not in wrong]
    props = []
    for dec in ("TAKE", "SKIP"):
        w = [r for r in wrong if r["claude"] == dec]; ok = [r for r in right if r["claude"] == dec]
        if len(w) < MIN_SUPPORT:
            continue
        for f in PROPOSAL_FIELDS:
            vals = [float(r["fields"][f]) for r in w if isinstance(r["fields"].get(f), (int, float)) and not isinstance(r["fields"].get(f), bool)]
            if len(vals) < MIN_SUPPORT:
                continue
            def num(r):
                x = r["fields"].get(f)
                return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None
            best = None
            for thr in sorted(set(vals)):                                   # every wrong value is a candidate threshold
                for op in (">=", "<="):
                    test = (lambda x, t=thr: x >= t) if op == ">=" else (lambda x, t=thr: x <= t)
                    nw = sum(1 for r in w if num(r) is not None and test(num(r)))
                    nr = sum(1 for r in ok if num(r) is not None and test(num(r)))
                    if nw >= MIN_SUPPORT and nw > nr and (best is None or (nw - nr, nw) > (best["score"], best["n_wrong"])):
                        action = "prefer SKIP" if dec == "TAKE" else "allow TAKE"
                        best = {"line": f"{action} when {f} {op} {thr:g} ({mode_display})", "field": f, "op": op, "value": thr, "decision": dec,
                                "n_wrong": nw, "n_right": nr, "support": f"{nw} wrong {dec} calls vs {nr} right", "score": nw - nr}
            if best is not None:
                props.append(best)
    props.sort(key=lambda p: (-p["score"], -p["n_wrong"], p["field"]))
    seen = set(); out = []
    for p in props:
        if (p["field"], p["decision"]) in seen:
            continue
        seen.add((p["field"], p["decision"])); out.append(p)
        if len(out) >= limit:
            break
    for i, p in enumerate(out, 1):
        p["n"] = i
    return out


def proposals_path(log_dir: str) -> str:
    return os.path.join(log_dir, "claude_rule_proposals.json")


def save_proposals(log_dir: str, week: str, props: list[dict]):
    os.makedirs(log_dir, exist_ok=True)
    tmp = proposals_path(log_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"week": week, "made": datetime.now(IST).strftime("%Y-%m-%d %H:%M"), "proposals": props}, f, indent=1)
    os.replace(tmp, proposals_path(log_dir))


def load_proposals(log_dir: str) -> dict:
    try:
        with open(proposals_path(log_dir), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def proposals_card(week: str, props: list[dict], n_wrong: int, rules_hash_: str) -> dict:
    if props:
        desc = "\n".join(f"**{p['n']}.** {p['line']} — {p['support']}" for p in props) + "\n\napprove one with `/claude-rules-approve <n>` — nothing changes until you do"
    else:
        desc = f"{n_wrong} wrong call(s) this week — no field separates them yet (need ≥ {MIN_SUPPORT} wrong calls on one side of a threshold)"
    return {"title": f"AUREON · MT5 · CLAUDE RULE PROPOSALS · {week}", "description": desc[:4096],
            "fields": [{"name": "Wrong calls", "value": str(n_wrong), "inline": True}, {"name": "Rules file", "value": f"unchanged ({rules_hash_})", "inline": True}],
            "footer": "Aureon MT5 · Claude · proposals only — approval appends a dated line to claude_rules.md", "png": None}


def approve_proposal(n: int, log_dir: str, rules_path: str = RULES_PATH) -> str:
    """/claude-rules-approve <n>: append the proposed line, dated, to claude_rules.md. Returns the appended line."""
    d = load_proposals(log_dir); props = d.get("proposals") or []
    p = next((x for x in props if int(x.get("n", 0)) == int(n)), None)
    if p is None:
        raise ValueError(f"no proposal {n} (latest card: {d.get('week') or 'none'})")
    if p.get("approved"):
        raise ValueError(f"proposal {n} already approved on {p['approved']}")
    line = f"- [approved {datetime.now(IST):%Y-%m-%d} · {d.get('week', '')}] {p['line']} — {p['support']}"
    with open(rules_path, "a", encoding="utf-8") as f:
        f.write(("\n" if not open(rules_path, encoding="utf-8").read().endswith("\n") else "") + line + "\n")
    p["approved"] = datetime.now(IST).strftime("%Y-%m-%d"); save_proposals(log_dir, d.get("week", ""), props)
    return line


def rule_proposals_tick(cfg, journal, notify, res: "CompareResult", week: str, mode_display: str = "EMA 50/80") -> list[dict]:
    """After the Saturday COMPARE card: build, save and post the proposals. No Claude call, no file edit."""
    records = journal.read(res.since - 86400, res.until + 3 * 86400)
    props = rule_proposals(records, res, mode_display)
    rows = _verdict_rows(records, res)
    n_wrong = sum(1 for r in rows if (r["claude"] == "TAKE" and r["points"] < 0) or (r["claude"] == "SKIP" and r["points"] > 0))
    save_proposals(cfg.log_dir, week, props)
    c = proposals_card(week, props, n_wrong, res.rules_hash)
    notify.send(f"{cfg.mode}:ALL:CLAUDE_RULE_PROPOSALS:{week}", c["title"], [c["description"]], fields=c["fields"], footer=c["footer"])
    return props
