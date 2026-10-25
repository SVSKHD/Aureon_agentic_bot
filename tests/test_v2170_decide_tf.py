"""v2.16.2 — /decide-timeframe for every mode: the EMA column contract in the replay tools, the default = the running exec
TF, and the ema921 / ema9200 path through the mode's own replay."""
import os
import sys
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from aureon_mt5 import decide_tf as D, features as F
from aureon_mt5.common import tf_synth as T
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.strategies import get_strategy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import guardian_sim as GS  # noqa: E402
import replay_guardian as RG  # noqa: E402

MODES = {"ema5080": ("ema_fast", "ema_slow"), "ema2050": ("ema20", "ema50"), "ema921": ("ema9", "ema21"),
         "ema9200": ("ema9", "ema200")}


def walk(n=900, step=300, seed=4, t0=1_760_918_400):
    rng = np.random.default_rng(seed)
    close = 3300 + np.cumsum(np.repeat(rng.normal(0, 0.8, n // 12 + 1), 12)[:n] + rng.normal(0, 2.0, n))
    op = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"time": t0 + np.arange(n) * step, "open": op, "high": np.maximum(op, close) + 1.0,
                         "low": np.minimum(op, close) - 1.0, "close": close})


# ----------------------------------------------------------------------------- 1. the column contract
@pytest.mark.parametrize("mode", list(MODES))
def test_simulate_finds_the_modes_columns(mode):
    S = get_strategy(mode)
    assert S.ema_cols == MODES[mode]
    bars = S.add_emas(walk())
    assert set(S.ema_cols) <= set(bars.columns) and ("ema_fast" in bars.columns) == (mode == "ema5080")
    out = GS.simulate(bars, 400, "long", mode=mode, max_bars=60)
    assert out["entry"] == pytest.approx(float(bars["close"].iloc[400]))


@pytest.mark.parametrize("mode", list(MODES))
def test_run_combo_threads_the_mode(mode):
    """The 22 Oct 01:02 KeyError('ema_fast'): run_combo simulated with the default (ema5080) columns on another mode's frame."""
    bars = get_strategy(mode).add_emas(walk())
    res = RG.run_combo(bars, [{"i": 400, "direction": "long", "entry": None, "source": "trade"}], {}, None, mode=mode)
    assert res["trades"] == 1


def test_the_old_default_is_what_broke():
    bars = get_strategy("ema921").add_emas(walk())
    with pytest.raises(KeyError):
        GS.simulate(bars, 400, "long", mode="ema5080", max_bars=5)            # ema921 frame, 5080 column names


# ----------------------------------------------------------------------------- 2. default TF = the running exec TF
class Note:
    def __init__(self):
        self.cards = []

    def card(self, title, body, **kw):
        self.cards.append((title, body))


ADMIN = SimpleNamespace(roles=[SimpleNamespace(id=42)], __str__=lambda s: "koushik")


def flat_agents():
    return {"XAUUSD": SimpleNamespace(snapshot={"at": time.time(), "positions": [], "connected": True})}


def summary():
    return {"trades": 3, "total_points": 4.0, "max_drawdown": 2.0, "trades_detail": []}


def test_no_tf_uses_cfg_exec_tf(tmp_path):
    cfg = Config(); cfg.log_dir = str(tmp_path); cfg.exec_tf = "m30"; cfg.mode = "ema5080"
    out = D.decide(cfg, flat_agents(), Journal(str(tmp_path)), Note(), ADMIN, "", admin_role="42",
                   runner=lambda sets: summary(), avg_range_fn=lambda tf, d: 9.0)
    assert "current exec TF **M30**" in out and "▶ M30" in out
    assert "**/decide-timeframe M30**" in out and "exec M30 (current) · confirm M45*, H1" in out
    out2 = D.decide(cfg, flat_agents(), Journal(str(tmp_path)), Note(), ADMIN, "H1", admin_role="42",
                    runner=lambda sets: summary(), avg_range_fn=lambda tf, d: 15.0)
    assert "exec H1 (current: M30)" in out2


def test_replay_guardian_default_exec_tf_is_the_configured_one(tmp_path, monkeypatch):
    fixture = os.path.join(ROOT, "tests", "fixtures", "xauusd_2day_m5.json")
    monkeypatch.setenv("AUREON_EXEC_TF", "M30")
    r30 = RG.main(["--bars", fixture, "--out", str(tmp_path)])
    monkeypatch.setenv("AUREON_EXEC_TF", "M5")
    r5 = RG.main(["--bars", fixture, "--out", str(tmp_path)])
    assert r30["bars"] * 6 == pytest.approx(r5["bars"], abs=6)           # M30 bars, not M5, when --set exec_tf is omitted
    assert T.exec_tf() == "M5"


# ----------------------------------------------------------------------------- 3. ema921 / ema9200: the mode's own replay
def m15_history(days=40):
    df = walk(n=days * 96, step=900, seed=9)
    return df


def fetcher(calls):
    base = m15_history()
    def fetch(tf, days):
        calls.append(tf)
        return base if tf == "M15" else T.resample_tf(base, tf)
    return fetch


def rig(tmp_path, mode="ema921", positions=()):
    cfg = Config(); cfg.log_dir = str(tmp_path); cfg.exec_tf = "m30"; cfg.mode = mode; cfg.symbols = ["XAUUSD"]
    ag = {"XAUUSD": SimpleNamespace(snapshot={"at": time.time(), "positions": list(positions), "connected": True})}
    return cfg, ag, Journal(str(tmp_path)), Note()


def test_ema921_table_rows_marks_and_cache(tmp_path):
    cfg, ag, j, n = rig(tmp_path); calls = []
    out = D.decide(cfg, ag, j, n, ADMIN, "", admin_role="42", fetch=fetcher(calls))
    assert "the mode's own replay (strategies/ema921/journeys)" in out and "current exec TF **M30**" in out
    table = out.split("```")[1].splitlines()
    lines = [l for l in table if l[:3] in ("M15", "M30", "H1 ")]
    assert [l[:3] for l in lines] == ["M15", "M30", "H1 "]
    m30 = next(l for l in lines if l.startswith("M30"))
    assert "research" not in m30 and "◀ current" in m30
    m15 = next(l for l in lines if l.startswith("M15"))
    assert "research" in m15                                                # v2.18.0: the pullback study ran on M15
    assert "M15 is the research timeframe for ema921" in out and "apply:false — nothing changed" in out
    assert sorted(calls) == ["H1", "M15", "M30"]
    out2 = D.decide(cfg, ag, j, n, ADMIN, "H4", admin_role="42", fetch=fetcher(calls))
    assert sorted(calls) == ["H1", "H4", "M15", "M30"]                      # only H4 was new: the rest came from the cache
    assert "H4" in out2 and "requested" in out2


def test_ema921_stats_come_from_its_own_journeys(tmp_path):
    cfg, *_ = rig(tmp_path)
    df = T.resample_tf(m15_history(), "M30")
    st = D.mode_stats(cfg, "ema921", df, 30)
    from aureon_mt5.strategies.ema921 import journeys as J
    from aureon_mt5.strategies.ema921.analysis import add_emas
    since = int(df["time"].iloc[-1]) - 30 * 86400
    res = J.build(add_emas(df), J.Rules(), 3.0, trade_from=since)
    assert st["trades"] == res["summary"]["trades"] and st["net"] == pytest.approx(res["summary"]["net"], abs=0.1)


def test_ema921_apply_writes_exec_tf_only_and_restarts(tmp_path):
    cfg, ag, j, n = rig(tmp_path); restarts = []
    out = D.decide(cfg, ag, j, n, ADMIN, "H1", admin_role="42", apply=True, fetch=fetcher([]),
                   restart=lambda u, why: restarts.append(why))
    assert F.load_overrides(cfg.log_dir) == {"exec_tf": "h1"}                 # no follow knobs, no confirm rules
    assert "AUREON_EXEC_TF=H1 written (nothing else)" in out and restarts == ["decide-timeframe ema921 H1"]
    row = [r for r in j.read(0) if r["event"] == "timeframe_decided"][-1]
    assert row["mode"] == "ema921" and row["knobs"] == {"exec_tf": "h1"}


def test_ema921_apply_refused_with_a_position(tmp_path):
    cfg, ag, j, n = rig(tmp_path, positions=[{"ticket": 1}])
    out = D.decide(cfg, ag, j, n, ADMIN, "H1", admin_role="42", apply=True, fetch=fetcher([]))
    assert "a position is open" in out and F.load_overrides(cfg.log_dir) == {}


def test_ema9200_uses_its_own_replay(tmp_path):
    cfg, ag, j, n = rig(tmp_path, mode="ema9200")
    out = D.decide(cfg, ag, j, n, ADMIN, "M30", admin_role="42", fetch=fetcher([]))
    assert "strategies/ema9200/journeys" in out and "M30 is the research timeframe for ema9200" in out


def test_no_mt5_history_is_a_row_not_a_crash(tmp_path):
    cfg, ag, j, n = rig(tmp_path)
    def broken(tf, days):
        raise RuntimeError("copy_rates_from_pos empty")
    out = D.decide(cfg, ag, j, n, ADMIN, "M30", admin_role="42", fetch=broken)
    assert out.count("no data — RuntimeError") == 3


def test_command_acks_first_then_follows_up():
    """respond() defers before the work runs (Discord's 3 s), the table is the followup."""
    src = open(os.path.join(ROOT, "aureon_mt5", "bot.py"), encoding="utf-8").read()
    i = src.index('name="decide-timeframe"')
    assert 'await run(inter, "decide-timeframe", work, timeout=600)' in src[i:i + 1600]
    j = src.index("async def respond(")
    body = src[j:j + 2500]
    assert body.index("await inter.response.defer(") < body.index("await off_loop(work")
