"""v2.1.0 — run.py --claude: SHADOW replay. Claude is asked at every ENTER bar the replay produced (and, with entry+pullback,
at every pullback event inside a replayed trade) exactly as live: the same build_snapshot on ONLY the bars up to that bar
(no look-ahead — asserted), the same rules prompt, the same CLI (`claude_cli.call`, ANTHROPIC_API_KEY stripped, AUREON_CLAUDE_BIN
honoured) and the same reply contract (TAKE/SKIP, p_win, evidence). Verdicts are RECORDED, never applied to the replayed trades;
a second scored column "Claude-filtered" removes Claude's SKIPs (and, in the pullback variant, applies TIGHTEN / CLOSE on M5 bars).

Cache: out/claude_replay_<mode>_<symbol>.jsonl keyed by (bar_t, event, model, rules hash) → a re-run with the same inputs makes
zero calls; --claude-refresh forces new calls. --claude-max caps the calls per run; unscored bars are counted and continue
from the cache on the next run. Strategy rules changed: NO — this measures, it never decides."""
from __future__ import annotations

import json
import os
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from .common import claude_cli
from .compare import Metrics, Row, metrics as compare_metrics, rules_hash
from .strategies.ema5080.trend import state_of

IST = timezone(timedelta(hours=5, minutes=30))
BAR_S = 300
TREND_PULLBACK = ("PULLBACK", "WEAKENING", "CHALLENGED")
P_BUCKETS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01))


# ============================================================================= shadow agent (what build_snapshot reads)
class _NoJournal:
    def read(self, since, until=None):
        return []


class ShadowAgent:
    """Stands in for SymbolAgent in build_snapshot: strategy, offset, symbol, news, per-bar analysis of the CUT frame only."""

    def __init__(self, S, symbol: str, off: float, news: tuple, cfg):
        self.S, self.symbol, self.off, self.news, self.cfg = S, symbol, off, news, cfg
        self.journal = _NoJournal()
        self.last_res = None
        self.trend_state = None
        self.g = S.guardian_for(symbol)

    def _warnings(self, df):
        return []


def cut_frame(df, bar_t: int):
    """df.iloc[:bar+1] for the bar whose time is bar_t — the ONLY data Claude may see for that bar."""
    t = df["time"].to_numpy()
    idx = np.searchsorted(t, bar_t)
    if idx >= len(t) or int(t[idx]) != int(bar_t):
        raise ValueError(f"bar {bar_t} not in frame")
    cut = df.iloc[: int(idx) + 1].reset_index(drop=True)
    assert int(cut["time"].iloc[-1]) == int(bar_t), "no-lookahead: the cut frame must end at the signal bar"
    return cut


def snapshot_for_bar(shadow: ShadowAgent, df, bar_t: int, event: str, side: str, *, position=None, st=None, build=None,
                     journeys_before: list | None = None) -> dict:
    """build_snapshot on the cut frame; crosses_today / day_pnl_pts / trades_today recomputed for the BAR's server day
    (live uses the wall clock, which is wrong in a replay)."""
    from .claude_advisor import build_snapshot
    cut = cut_frame(df, bar_t)
    if shadow.S.name == "ema2050":
        from .strategies.ema2050.journeys import build as build_2050
        res = build_2050(shadow.S.add_emas(cut[["time", "open", "high", "low", "close"]].assign(volume=0)) if "ema20" not in cut else cut,
                         shadow.S.rules_for(shadow.symbol), shadow.off)
        shadow.last_res = res
    snap = (build or build_snapshot)(shadow, event, side, bar_t, cut, position=position, st=st)
    assert len(cut) == int(np.searchsorted(df["time"].to_numpy(), bar_t)) + 1
    cf, cs = shadow.S.ema_cols
    day_start = int(bar_t) - (int(bar_t) % 86400)
    t = cut["time"].to_numpy(); sg = np.sign(cut[cf].to_numpy() - cut[cs].to_numpy())
    snap["crosses_today"] = int(sum(1 for k in range(1, len(cut)) if t[k] >= day_start and sg[k] != 0 and sg[k - 1] != 0 and sg[k] != sg[k - 1]))
    done = [j for j in (journeys_before or []) if j.exit_time is not None and j.exit_time <= bar_t and j.exit_reason != "open" and j.entry_time >= day_start]
    snap["day_pnl_pts"] = round(sum(j.result for j in done), 2); snap["trades_today"] = len(done)
    snap["replay"] = True
    return snap


# ============================================================================= cache
def cache_path(out_dir: str, mode: str, symbol: str) -> str:
    return os.path.join(out_dir, f"claude_replay_{mode}_{symbol}.jsonl")


def cache_key(bar_t: int, event: str, model: str, rules_h: str) -> str:
    return f"{int(bar_t)}|{event}|{model}|{rules_h}"


def load_cache(path: str) -> dict:
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("key"):
                    out[r["key"]] = r                   # last row for a key wins
            except Exception:
                continue
    return out


def append_cache(path: str, row: dict):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


# ============================================================================= the shadow run
@dataclass
class ShadowResult:
    rows: list = field(default_factory=list)          # one per ENTER bar (detector trade)
    pullbacks: list = field(default_factory=list)     # one per pullback event asked
    calls: int = 0
    cache_hits: int = 0
    unscored: int = 0
    errors: int = 0
    model: str = ""
    rules_hash: str = ""
    variant: str = "entry"
    months: dict = field(default_factory=dict)


def _event_for(S, jn) -> str:
    if S.name == "ema2050":
        return "ENTER"
    if getattr(jn, "pre", False):
        return "P"
    if getattr(jn, "reentry", False):
        return "RE"
    return "CROSS"


class ShadowReplay:
    """One run: ask (or read from the cache), record, score. `caller` defaults to claude_cli.call (mockable in tests)."""

    def __init__(self, cfg, S, symbol: str, off: float, news: tuple, *, out_dir: str = "out", model: str | None = None,
                 max_calls: int = 60, sleep_s: float = 2.0, refresh: bool = False, variant: str = "entry", caller=None,
                 sleep=_time.sleep, rules_path: str | None = None):
        from .claude_advisor import ClaudeAdvisor, RULES_PATH
        self.cfg, self.S, self.symbol, self.off, self.news = cfg, S, symbol, off, news
        self.out_dir = out_dir; self.model = model or cfg.claude_entry_model
        self.max_calls, self.sleep_s, self.refresh, self.variant = int(max_calls), float(sleep_s), bool(refresh), variant
        self.caller = caller or claude_cli.call; self.sleep = sleep
        self.rules_path = rules_path or RULES_PATH
        self.rules_h = rules_hash(self.rules_path)
        self.adv = ClaudeAdvisor(cfg, _Quiet(), _NoJournal(), caller=self.caller, start=False)     # prompt_for only; no worker, no budget
        try:
            with open(self.rules_path, encoding="utf-8") as f:
                self.adv.rules = f.read()
        except Exception:
            pass
        self.path = cache_path(out_dir, S.name, symbol)
        self.cache = load_cache(self.path)
        self.res = ShadowResult(model=self.model, rules_hash=self.rules_h, variant=variant)

    # ------------------------------------------------------------------ one call (cache first)
    def ask(self, snapshot: dict, bar_t: int, event: str) -> dict:
        key = cache_key(bar_t, event, self.model, self.rules_h)
        hit = None if self.refresh else self.cache.get(key)
        if hit is not None:
            self.res.cache_hits += 1
            return hit
        if self.res.calls >= self.max_calls:
            self.res.unscored += 1
            return {"key": key, "verdict": None, "error": "budget (--claude-max) reached", "unscored": True}
        if self.res.calls > 0 and self.sleep_s > 0:
            self.sleep(self.sleep_s)
        self.res.calls += 1
        r = self.caller(self.adv.prompt_for(snapshot), self.model, event=event, bin=self.cfg.claude_bin, workdir=self.cfg.claude_workdir,
                        timeout=self.cfg.claude_timeout)
        row = {"key": key, "bar_t": int(bar_t), "event": event, "model": self.model, "rules_hash": self.rules_h,
               "verdict": r.verdict if r.ok else None, "error": "" if r.ok else r.error, "latency_s": round(r.latency_s, 1),
               "reported_model": getattr(r, "model", ""), "asked_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")}
        if not r.ok:
            self.res.errors += 1
        self.cache[key] = row; append_cache(self.path, row)
        return row

    # ------------------------------------------------------------------ entries
    def score_day(self, day, df, journeys: list):
        """df: the day's analysed frame (EMAs present); journeys: the replayed trades of that day (NOT modified)."""
        shadow = ShadowAgent(self.S, self.symbol, self.off, self.news, self.cfg)
        done: list = []
        for jn in sorted(journeys, key=lambda j: j.entry_time):
            side = "LONG" if jn.direction == "long" else "SHORT"; event = _event_for(self.S, jn)
            snap = snapshot_for_bar(shadow, df, jn.entry_time, event, side, journeys_before=done)
            snap["ema_names"] = snap.get("ema_names")
            v = self.ask(snap, jn.entry_time, event)
            verdict = v.get("verdict")
            row = {"day": str(day), "bar_t": int(jn.entry_time), "ist": datetime.fromtimestamp(jn.entry_time - self.off * 3600, tz=IST).strftime("%Y-%m-%d %H:%M"),
                   "side": side, "kind": event, "points": float(jn.result if jn.exit_reason != "open" else 0.0), "exit_reason": jn.exit_reason,
                   "open": jn.exit_reason == "open", "stop": jn.exit_reason in ("stop", "pre_stop", "stop_ema80"),
                   "claude": (verdict or {}).get("decision"), "p_win": (verdict or {}).get("p_win"), "reason": (verdict or {}).get("reason"),
                   "evidence": (verdict or {}).get("evidence") or [], "error": v.get("error") or "", "unscored": bool(v.get("unscored")),
                   "month": datetime.fromtimestamp(jn.entry_time, tz=timezone.utc).strftime("%b %Y"), "filtered_points": None}
            if self.variant == "entry+pullback" and jn.exit_reason != "open" and row["claude"] != "SKIP":
                row["filtered_points"] = self._pullbacks(shadow, df, jn, side, done)
            self.res.rows.append(row)
            done.append(jn)

    # ------------------------------------------------------------------ pullbacks inside a replayed trade (variant entry+pullback)
    def _pullbacks(self, shadow, df, jn, side: str, done: list) -> float | None:
        """Walk the trade bar by bar with the live trigger (trend PULLBACK/WEAKENING/CHALLENGED or ≤60% of a ≥5 peak, once per
        pullback, re-armed after a new peak); ask Claude; apply TIGHTEN (tighter SL) / CLOSE (in profit) on the M5 bars for the
        Claude-filtered column only. Returns the filtered result for the trade (None = unchanged)."""
        cf, cs = self.S.ema_cols
        t = df["time"].to_numpy(); c = df["close"].to_numpy(); h = df["high"].to_numpy(); l = df["low"].to_numpy()
        ef = df[cf].to_numpy(); es = df[cs].to_numpy()
        s = 1 if side == "LONG" else -1
        i0 = int(np.searchsorted(t, jn.entry_time)); i1 = int(np.searchsorted(t, jn.exit_time))
        g = self.S.guardian_for(self.symbol)
        peak = 0.0; mae = 0.0; fired_peak = None; sl = None; peak_t = jn.entry_time
        filtered = None
        for k in range(i0 + 1, i1 + 1):
            pts = s * (c[k] - jn.entry_price)
            fav = s * ((h[k] if s > 0 else l[k]) - jn.entry_price); adv = s * ((l[k] if s > 0 else h[k]) - jn.entry_price)
            if fav > peak:
                peak, peak_t = fav, int(t[k])
            mae = min(mae, adv)
            if sl is not None and adv <= s * (sl - jn.entry_price):              # a Claude TIGHTEN stop was hit
                filtered = round(s * (sl - jn.entry_price), 2); break
            shadow.trend_state = state_of(float(c[k]), float(ef[k]), float(es[k]))
            p = {"direction": jn.direction, "price_open": jn.entry_price, "points": float(pts), "current": float(c[k]), "sl": sl}
            st = {"peak": peak, "secured": 0.0, "bars": k - i0, "mae": mae, "peak_t": peak_t}
            why = self.adv.pullback_reason(shadow, p, st, ())
            if not why or (fired_peak is not None and peak <= fired_peak + 1e-9):
                continue
            fired_peak = peak
            snap = snapshot_for_bar(shadow, df, int(t[k]), "pullback", side, position=p, st=st, journeys_before=done)
            snap["trigger"] = why
            v = self.ask(snap, int(t[k]), "pullback"); verdict = v.get("verdict")
            self.res.pullbacks.append({"bar_t": int(t[k]), "entry_t": int(jn.entry_time), "side": side, "trigger": why, "points_at": round(pts, 2),
                                       "peak": round(peak, 2), "claude": (verdict or {}).get("decision"), "tighten_to": (verdict or {}).get("tighten_to"),
                                       "final": jn.result, "unscored": bool(v.get("unscored"))})
            if not verdict:
                continue
            if verdict["decision"] == "CLOSE" and pts > 0:
                filtered = round(float(pts), 2); break
            if verdict["decision"] == "TIGHTEN" and verdict.get("tighten_to") is not None:
                tt = float(verdict["tighten_to"])
                tighter = sl is None or (tt > sl if s > 0 else tt < sl)
                sane = (tt < c[k]) if s > 0 else (tt > c[k])
                if tighter and sane:
                    sl = tt
        return filtered

    # ------------------------------------------------------------------ scoring
    def rows_as_compare(self, rows=None) -> list[Row]:
        out = []
        for r in (rows if rows is not None else self.res.rows):
            if r["open"]:
                out.append(Row(self.symbol, r["kind"], r["side"], r["bar_t"], "", status="OPEN", claude=r["claude"])); continue
            grade = "STOP" if r["stop"] else ("WIN10" if r["points"] >= 10 else ("FLAT" if r["points"] > 0 else "STOP" if r["points"] < 0 else "FLAT"))
            pts = r["points"] if r.get("filtered_points") is None else r["filtered_points"]
            out.append(Row(self.symbol, r["kind"], r["side"], r["bar_t"], "", status="GRADED", grade=grade, points=float(pts), claude=r["claude"]))
        return out

    def score(self) -> dict:
        rows = self.res.rows
        det = [r for r in rows if not r["open"]]
        base = [dict(r, filtered_points=None) for r in det]                  # detector column: the replayed trades as they were
        m = {"detector": compare_metrics(self.rows_as_compare(base)),
             "claude_take": compare_metrics(self.rows_as_compare([r for r in base if r["claude"] == "TAKE"])),
             "claude_skip": compare_metrics(self.rows_as_compare([r for r in base if r["claude"] == "SKIP"])),
             "filtered": compare_metrics(self.rows_as_compare([r for r in det if r["claude"] != "SKIP"]))}
        m["filtered"].stops = sum(1 for r in det if r["claude"] != "SKIP" and r["stop"] and r.get("filtered_points") is None)
        skips = [r for r in det if r["claude"] == "SKIP"]
        skip_precision = (sum(1 for r in skips if r["points"] < 0) / len(skips)) if skips else None
        calib = calibration(det)
        dis = [{"date": r["ist"], "side": r["side"], "points": r["points"], "claude": r["claude"], "p_win": r["p_win"], "reason": r["reason"],
                "evidence": r["evidence"]} for r in det if r["claude"] == "SKIP" or (r["claude"] == "TAKE" and r["points"] < 0)]
        months: dict = {}
        for r in det:
            mo = months.setdefault(r["month"], {"det_n": 0, "det_pts": 0.0, "filt_n": 0, "filt_pts": 0.0})
            mo["det_n"] += 1; mo["det_pts"] += r["points"]
            if r["claude"] != "SKIP":
                mo["filt_n"] += 1; mo["filt_pts"] += r["points"] if r.get("filtered_points") is None else r["filtered_points"]
        self.res.months = months
        return {"metrics": m, "skip_precision": skip_precision, "skips": len(skips), "calibration": calib, "disagreements": dis, "months": months,
                "no_verdict": sum(1 for r in det if r["claude"] is None), "unscored": self.res.unscored, "calls": self.res.calls,
                "cache_hits": self.res.cache_hits, "errors": self.res.errors, "pullbacks": self.res.pullbacks}


def calibration(rows: list[dict]) -> list[dict]:
    """p_win bucket → n, actual win share (points > 0), mean p_win. Rows without p_win are ignored."""
    out = []
    for lo, hi in P_BUCKETS:
        xs = [r for r in rows if r.get("p_win") is not None and lo <= float(r["p_win"]) < hi]
        if not xs:
            out.append({"bucket": f"{lo:.1f}–{min(hi, 1.0):.1f}", "n": 0, "win_rate": None, "mean_p": None}); continue
        out.append({"bucket": f"{lo:.1f}–{min(hi, 1.0):.1f}", "n": len(xs), "win_rate": round(sum(1 for r in xs if r["points"] > 0) / len(xs), 2),
                    "mean_p": round(sum(float(r["p_win"]) for r in xs) / len(xs), 2)})
    return out


# ============================================================================= report (same shape as compare.card)
def _f(x, fmt="{:+.1f}"):
    return "—" if x is None else fmt.format(x)


def report_text(sr: ShadowReplay, sc: dict, title: str) -> str:
    m = sc["metrics"]
    rows = [("Detector · all", m["detector"]), ("Claude TAKE", m["claude_take"]), ("Claude SKIP", m["claude_skip"]),
            ("Detector-filtered-by-Claude", m["filtered"])]
    head = f"{'group':<30}{'n':>5}{'win%':>7}{'exp':>8}{'total':>9}{'stops':>7}{'streak':>8}{'maxDD':>8}"
    L = [f"# CLAUDE SHADOW REPLAY · {title}",
         f"model `{sr.model}` · rules `{sr.rules_h}` · variant {sr.variant} · calls this run {sc['calls']} · cache hits {sc['cache_hits']} · "
         f"errors {sc['errors']} · no verdict {sc['no_verdict']} · **unscored ENTER bars {sc['unscored']}** (next run continues from the cache)",
         "", "```", head, "-" * len(head)]
    for name, mm in rows:
        L.append(f"{name:<30}{mm.n:>5}{_f(mm.win_rate, '{:.0%}'):>7}{_f(mm.expectancy):>8}{_f(mm.total):>9}{mm.stops:>7}{mm.worst_streak:>8}{_f(mm.biggest_loss):>8}")
    L.append("```")
    sp = sc["skip_precision"]
    L.append(f"SKIP precision (share of Claude SKIPs that lost): {_f(sp, '{:.0%}')} over {sc['skips']} SKIPs")
    L += ["", "## Calibration — p_win bucket vs actual win%", "```", f"{'p_win':<10}{'n':>5}{'actual win%':>13}{'mean p_win':>12}", "-" * 40]
    for c in sc["calibration"]:
        L.append(f"{c['bucket']:<10}{c['n']:>5}{_f(c['win_rate'], '{:.0%}'):>13}{_f(c['mean_p'], '{:.2f}'):>12}")
    L.append("```")
    L += ["", "## Per month — Detector vs Claude-filtered", "```", f"{'month':<10}{'det n':>7}{'det pts':>9}{'filt n':>8}{'filt pts':>10}{'diff':>8}", "-" * 52]
    for mo, v in sc["months"].items():
        L.append(f"{mo:<10}{v['det_n']:>7}{v['det_pts']:>+9.1f}{v['filt_n']:>8}{v['filt_pts']:>+10.1f}{v['filt_pts'] - v['det_pts']:>+8.1f}")
    L.append("```")
    if sc["pullbacks"]:
        pb = sc["pullbacks"]
        L += ["", f"## Pullback verdicts ({len(pb)}): " + " · ".join(f"{d} {sum(1 for p in pb if p['claude'] == d)}" for d in ("HOLD", "TIGHTEN", "CLOSE"))]
    L += ["", f"## Disagreements ({len(sc['disagreements'])}) — SKIPs and losing TAKEs: date · side · detector pts · Claude · reason · evidence"]
    for d in sc["disagreements"][:60]:
        L.append(f"- {d['date']} · {d['side']} · {d['points']:+.1f} · {d['claude']}" + (f" p_win {d['p_win']:.2f}" if d.get("p_win") is not None else "")
                 + f" · {d['reason'] or '—'} · {', '.join(d['evidence']) or '—'}")
    if len(sc["disagreements"]) > 60:
        L.append(f"- … {len(sc['disagreements']) - 60} more")
    return "\n".join(L)


def write_report(sr: ShadowReplay, sc: dict, title: str) -> tuple[str, str]:
    text = report_text(sr, sc, title)
    path = os.path.join(sr.out_dir, f"claude_replay_{sr.S.name}_{sr.symbol}.md")
    os.makedirs(sr.out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    return text, path


class _Quiet:
    """A Notifier stand-in: the shadow advisor never posts."""
    def send(self, *a, **k): return True
    def card(self, *a, **k): return True
    def raw(self, *a, **k): return True
    def edit_card(self, *a, **k): return False
    claude_edit_hook = None
    card_edit_hook = None
