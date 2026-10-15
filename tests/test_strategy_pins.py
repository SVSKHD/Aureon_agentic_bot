"""v2.10.0 — STRATEGY PINS (see VERSIONING.md "pins rule").

The ema5080 analysis (signals, entries, exit reasons) and the live guardian (`_guard`, every feature at its default) are run
over a fixed 2-day fixture (tests/fixtures/xauusd_2day_m5.json: the synthetic 15-Oct bars + one synthetic day) and must
produce EXACTLY this sequence. Any patch — human or repair agent — that changes a pin fails the suite, and /repair-apply
refuses a patch whose tests fail. A deliberate strategy change updates these pins in the same commit, with the CHANGELOG
"Strategy rules changed: YES" line."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import pytest
from aureon_mt5.common.source import Bars
from aureon_mt5.strategies import get_strategy
from tools.guardian_sim import p_signals, simulate
from tests.fixtures.replay_20261015 import replay

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "xauusd_2day_m5.json")

PIN_P_SIGNALS = {1792056300: "SHORT", 1792115400: "LONG"}
PIN_JOURNEYS = [("short", 257, 4145.72, "pre_timeout", 1.76),
                ("short", 286, 4132.80, "stop_ema80", -6.66),
                ("long", 338, 4147.29, "floor", 10.0)]
PIN_GUARDIAN = [(4151.72, 270, "sl_hit", 0.5),
                (4138.80, 308, "sl_hit", -6.0),
                (4141.29, 361, "sl_hit", 15.0)]


@pytest.fixture(scope="module")
def bars():
    return pd.DataFrame(json.load(open(FIX, encoding="utf-8"))["bars"])


@pytest.fixture(scope="module")
def journeys(bars):
    df = bars[["time", "open", "high", "low", "close"]].copy(); df["volume"] = 1
    return get_strategy("ema5080").analyse({"M5": Bars("XAUUSD", "M5", df)}, "XAUUSD", 3.0, ())["M5"]["journeys"]


def test_pin_p_signals(bars):
    assert p_signals(bars) == PIN_P_SIGNALS


def test_pin_analysis_journeys(journeys):
    got = [(j.direction, j.entry_index, round(j.entry_price, 2), j.exit_reason, round(j.result, 2)) for j in journeys]
    assert got == PIN_JOURNEYS


def test_pin_guardian_defaults(bars, journeys):
    sig = p_signals(bars)
    got = []
    for j in journeys:
        r = simulate(bars, j.entry_index, j.direction, j.entry_price, signals=sig, m15="BULLISH")
        got.append((r["initial_sl"], r["exit"]["i"], r["exit"]["reason"], r["exit"]["points"]))
    assert got == PIN_GUARDIAN


def test_pin_15_oct_trade_at_defaults():
    r = replay()
    assert (r["initial_sl"], r["exit"]["time"], r["exit"]["reason"], r["exit"]["points"]) == (4097.5, "02:05", "p_flip", -1.2)
