"""v3.0 ema921e — config (step 3). Later steps add the runner, cards, logs and the two acceptance tests."""
import os
import shutil
import types

import pytest

from aureon_mt5.strategies.ema921e import config as C
from aureon_mt5.strategies.ema921e.mode import EMode, params_from

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YAML = os.path.join(REPO, "config", "ema921e.yaml")


def _copy(tmp_path):
    p = tmp_path / "ema921e.yaml"; shutil.copy(YAML, p); return str(p)


def test_shipped_yaml_is_the_brief():
    s, problems = C.load(YAML)
    assert problems == []
    assert s == {"symbol": "XAUUSD", "timeframe": "M30", "lot": 0.3, "spike": 0.15, "no_monday": True, "hours": [0, 24],
                 "atr_gate": 0.7, "month_stop_per_lot": 2000.0, "cut_bars": 2,
                 "ladder": [[0.25, 0.05], [0.5, 0.25], [1.0, 0.5]], "step": 0.5, "stop_atr": 3.0,
                 "news_pause_minutes": 30, "paper": True}
    p = params_from(s)
    assert (p.lot, p.atr_gate, p.month_stop_per_lot, p.stop_atr, p.spike, p.no_monday, p.cut_bars, p.step) == \
           (0.3, 0.7, 2000.0, 3.0, 0.15, True, 2, 0.5)
    assert p.ladder == ((0.25, 0.05), (0.5, 0.25), (1.0, 0.5)) and (p.hours_from, p.hours_to) == (0, 24)


def test_set_key_rewrites_one_line_and_keeps_comments(tmp_path):
    path = _copy(tmp_path)
    before = open(path).read().splitlines()
    assert C.set_key(path, "lot", "0.5") == ("lot", 0.5)
    assert C.set_key(path, "news_pause_minutes", "45") == ("news_pause_minutes", 45)
    after = open(path).read().splitlines()
    assert len(after) == len(before)
    assert "lot: 0.5" in after
    assert any(l.startswith("news_pause_minutes: 45") and "# optional" in l for l in after)
    assert C.load(path)[0]["lot"] == 0.5
    assert C.set_key(path, "ladder", "0.25:0.05,0.5:0.25,1:0.5")[1] == [[0.25, 0.05], [0.5, 0.25], [1.0, 0.5]]
    assert C.set_key(path, "hours", "1-23")[1] == [1, 23]


@pytest.mark.parametrize("key,value", [("lot", "0"), ("lot", "9"), ("lot", "abc"), ("cut_bars", "1.5"), ("hours", "5-3"),
                                       ("ladder", "0.5:0.6"), ("symbol", "XAGUSD"), ("timeframe", "M15"), ("paper", "false"),
                                       ("nope", "1")])
def test_set_key_refuses(tmp_path, key, value):
    path = _copy(tmp_path); before = open(path).read()
    with pytest.raises(C.ConfigError):
        C.set_key(path, key, value)
    assert open(path).read() == before


def test_bad_file_falls_back_loudly(tmp_path):
    p = tmp_path / "x.yaml"; p.write_text("lot: lots\nspike: 0.2\n")
    s, problems = C.load(str(p))
    assert s["lot"] == 0.3 and s["spike"] == 0.2
    assert any(x.startswith("lot:") for x in problems) and any("missing" in x for x in problems)


def test_hot_reload_swaps_engine_params(tmp_path):
    path = _copy(tmp_path)
    s, _ = C.load(path)
    cfg = types.SimpleNamespace(max_dd_usd=0, execution_enabled=False, allow_live=False)
    e = EMode(cfg, s, broker=types.SimpleNamespace(name="SIM"), log_dir=str(tmp_path), config_path=path)
    seen = []; e.listeners.append(lambda k, d: seen.append((k, d)))
    e.on_loop(); assert seen == []                                      # unchanged file: nothing
    txt = open(path).read().replace("atr_gate: 0.7", "atr_gate: 0.0")
    open(path, "w").write(txt); os.utime(path, ns=(1, os.stat(path).st_mtime_ns + 10_000_000))
    e.on_loop()
    assert e.eng.p.atr_gate == 0.0 and e.settings["atr_gate"] == 0.0
    assert seen[-1][0] == "config" and seen[-1][1]["changes"] == {"atr_gate": (0.7, 0.0)}
    k, v = e.set("lot", "0.4")
    assert (k, v) == ("lot", 0.4) and e.eng.p.lot == 0.4 and e.lot == 0.4 and C.load(path)[0]["lot"] == 0.4
    n = len(seen); e.on_loop(); assert len(seen) == n                   # our own write is not reloaded twice
    open(path, "w").write(open(path).read().replace("lot: 0.4", "lot: -1"))
    os.utime(path, ns=(1, os.stat(path).st_mtime_ns + 20_000_000))
    e.on_loop()
    assert e.lot == 0.4 and seen[-1][0] == "error" and "reload refused" in seen[-1][1]["text"]
