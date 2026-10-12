"""v2.3.15 — daily plan card at 05:00 IST."""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from datetime import datetime, timedelta, timezone
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5 import daily_plan

IST = timezone(timedelta(hours=5, minutes=30))


class Cap(Notifier):
    def __init__(self): super().__init__(None); self.titles = []; self.bodies = []
    def raw(self, body, png=None, *, embed=None):
        self.titles.append(embed["title"] if embed else str(body).splitlines()[0])
        self.bodies.append(str(embed or body))
        return True


class FakeAgent:
    def __init__(self):
        self.chop = {"chop": False, "crosses": 2, "threshold": 5, "day": "2026-10-13"}
        self.health = {}; self.S = types.SimpleNamespace(display_name="EMA 20/50")


def _cfg(tmp_path, plan=True, goal=0):
    cfg = Config(); cfg.log_dir = str(tmp_path); cfg.dry = True; cfg.daily_plan = plan
    cfg.daily_plan_at = "05:00"; cfg.week_goal_pts = goal; cfg.news_file = None
    return cfg


# ================================================================== fires at 05:00 IST on weekday
def test_fires_at_0500_weekday(tmp_path):
    cfg = _cfg(tmp_path); n = Cap(); j = Journal(str(tmp_path))
    now = datetime(2026, 10, 13, 5, 0, tzinfo=IST)  # Tuesday 05:00
    ok = daily_plan.tick(cfg, {"XAUUSD": FakeAgent()}, j, n, now=now)
    assert ok
    assert len([t for t in n.titles if "DAILY PLAN" in t]) == 1
    assert any(e["event"] == "daily_plan" for e in j.read(0))


# ================================================================== not on weekends
def test_not_on_weekend(tmp_path):
    cfg = _cfg(tmp_path); n = Cap(); j = Journal(str(tmp_path))
    now = datetime(2026, 10, 17, 5, 0, tzinfo=IST)  # Saturday
    ok = daily_plan.tick(cfg, {"XAUUSD": FakeAgent()}, j, n, now=now)
    assert not ok


# ================================================================== restart doesn't double-post
def test_no_double_post(tmp_path):
    cfg = _cfg(tmp_path); n = Cap(); j = Journal(str(tmp_path))
    now = datetime(2026, 10, 13, 5, 0, tzinfo=IST)
    daily_plan.tick(cfg, {"XAUUSD": FakeAgent()}, j, n, now=now)
    daily_plan.tick(cfg, {"XAUUSD": FakeAgent()}, j, n, now=now)
    assert len([t for t in n.titles if "DAILY PLAN" in t]) == 1


# ================================================================== content: news, yesterday, week goal, chop
def test_card_content_fields(tmp_path):
    cfg = _cfg(tmp_path, goal=10.0); j = Journal(str(tmp_path))
    # Simulate yesterday's trade
    j.log("closed", final_points=5.0, symbol="XAUUSD")
    now = datetime(2026, 10, 13, 5, 0, tzinfo=IST)
    card = daily_plan.plan_card(cfg, {"XAUUSD": FakeAgent()}, j, now=now)
    field_names = [f["name"] for f in card["fields"]]
    assert "News" in field_names and "Sessions" in field_names and "Yesterday" in field_names
    assert "Week to date" in field_names and "Chop" in field_names and "Governor" in field_names
    week_field = next(f for f in card["fields"] if f["name"] == "Week to date")
    assert "goal" in week_field["value"]  # goal enabled


def test_card_no_goal_when_zero(tmp_path):
    cfg = _cfg(tmp_path, goal=0); j = Journal(str(tmp_path))
    now = datetime(2026, 10, 13, 5, 0, tzinfo=IST)
    card = daily_plan.plan_card(cfg, {"XAUUSD": FakeAgent()}, j, now=now)
    week_field = next(f for f in card["fields"] if f["name"] == "Week to date")
    assert "goal" not in week_field["value"]


def test_chop_set(tmp_path):
    cfg = _cfg(tmp_path); j = Journal(str(tmp_path))
    ag = FakeAgent(); ag.chop = {"chop": True, "crosses": 6, "threshold": 5, "day": "2026-10-13"}
    now = datetime(2026, 10, 13, 5, 0, tzinfo=IST)
    card = daily_plan.plan_card(cfg, {"XAUUSD": ag}, j, now=now)
    chop_field = next(f for f in card["fields"] if f["name"] == "Chop")
    assert "CHOP" in chop_field["value"]


# ================================================================== /plan works any time
def test_plan_card_any_time(tmp_path):
    cfg = _cfg(tmp_path); j = Journal(str(tmp_path))
    now = datetime(2026, 10, 13, 14, 30, tzinfo=IST)  # afternoon
    card = daily_plan.plan_card(cfg, {"XAUUSD": FakeAgent()}, j, now=now)
    assert "DAILY PLAN" in card["title"]


# ================================================================== disabled
def test_disabled(tmp_path):
    cfg = _cfg(tmp_path, plan=False); n = Cap(); j = Journal(str(tmp_path))
    now = datetime(2026, 10, 13, 5, 0, tzinfo=IST)
    assert not daily_plan.tick(cfg, {"XAUUSD": FakeAgent()}, j, n, now=now)
