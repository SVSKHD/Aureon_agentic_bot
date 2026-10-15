"""v2.10.0 Part A — feature registry, live toggles, /feature-set gates, boot card."""
import json, os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import Mock
import pytest
from aureon_mt5 import broker, features as F
from aureon_mt5.bot import feature_set_action, features_text
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from tests.test_v181 import harness  # noqa: F401
from tests.test_claude_advisor import rig, frame  # noqa: F401
from tests.test_v280_flip_giveback import mkdf, step, exits

ADMIN = types.SimpleNamespace(roles=[types.SimpleNamespace(id=7)], __str__=lambda self: "op")
NOBODY = types.SimpleNamespace(roles=[])


def cfg_at(tmp_path):
    c = Config(); c.log_dir = str(tmp_path); return c


# ------------------------------------------------------------------ resolution + validation
def test_resolution_order_discord_env_default():
    assert F.resolve("giveback_pct", None, {}, environ={}) == (0.0, "default")
    assert F.resolve("giveback_pct", None, {}, environ={"AUREON_GIVEBACK_PCT": "40"}) == (40.0, "env")
    assert F.resolve("giveback_pct", None, {"giveback_pct": 55.0}, environ={"AUREON_GIVEBACK_PCT": "40"}) == (55.0, "discord")


@pytest.mark.parametrize("name,raw,want", [
    ("setup_stop", "on", True), ("setup_stop", "No", False), ("giveback_pct", "50", 50.0), ("max_calls", "100", 100),
    ("claude_mode", "Manage", "manage"), ("regime_gate", "show", "show"), ("image_events", "p, read", ("P", "READ")),
    ("image_events", "none", ()),
])
def test_parse_ok(name, raw, want):
    assert F.parse(F.get(name), raw) == want


@pytest.mark.parametrize("name,raw", [("setup_stop", "maybe"), ("giveback_pct", "95"), ("setup_max_dd", "1"), ("max_calls", "2.5"),
                                      ("claude_mode", "auto"), ("image_events", "P,FOO"), ("nope", "1")])
def test_parse_rejects(name, raw):
    with pytest.raises(ValueError):
        F.parse(F.get(name), raw)


def test_registry_defaults_equal_config_defaults():
    c = Config()
    for f in F.registry():
        if f.attr == "claude_open_read":
            continue                                              # tests/conftest.py turns the open read off in tests
        assert getattr(c, f.attr) == f.default, f.name


# ------------------------------------------------------------------ apply in place → the guardian sees it next bar
def test_giveback_set_live_closes_on_next_bar(harness, tmp_path):
    fake, ag, _ = harness(price=4200.0)
    fake.price = 4220.0; step(ag, fake, mkdf(4220.0, 4190.0, 4180.0))      # peak +20
    old, new = F.set_override(ag.cfg.log_dir, ag.cfg, "giveback_pct", "50", "op")
    assert (old, new) == (0.0, 50.0) and ag.cfg.giveback_pct == 50.0
    fake.price = 4208.0; step(ag, fake, mkdf(4208.0, 4190.0, 4180.0, t0=300))   # gave back 60 %
    assert fake.closed and exits(ag)[-1]["reason"] == "giveback"


def test_overrides_survive_reload_and_reset(tmp_path):
    c = cfg_at(tmp_path)
    F.set_override(c.log_dir, c, "regime_gate", "show", "op")
    c2 = cfg_at(tmp_path); F.apply(c2, F.load_overrides(c2.log_dir))
    assert c2.regime_gate == "show" and F.source("regime_gate", F.load_overrides(c2.log_dir), environ={}) == "discord"
    F.clear_override(c2.log_dir, c2, "regime_gate", "op", environ={})
    assert c2.regime_gate == "off" and F.load_overrides(c2.log_dir) == {}


def test_set_mode_advisory_to_manage_applies_close(rig):
    from tests.test_claude_advisor import pullback_job, run_pullback, ok
    r = rig(mode="advisory", reply=ok("CLOSE"), price=4212.0)
    F.set_override(r.cfg.log_dir, r.cfg, "claude_mode", "manage", "op", advisors=[r.adv])
    assert r.adv.mode == "manage" and r.adv.manage
    assert [x for x in r.j.read(0) if x["event"] == "claude_mode_changed"][-1]["new"] == "manage"
    run_pullback(r, pullback_job(r))
    assert r.fake.closed


def test_max_calls_updates_budget(rig):
    r = rig(mode="advisory")
    F.set_override(r.cfg.log_dir, r.cfg, "max_calls", "100", "op", advisors=[r.adv])
    assert r.adv.budget.limit == 100


# ------------------------------------------------------------------ /feature-set gates
def agents_with_position(open_=True):
    snap = {"at": time.time(), "positions": [{"ticket": 1}] if open_ else [], "connected": True}
    return {"XAUUSD": types.SimpleNamespace(snapshot=snap)}


def test_feature_set_admin_only(tmp_path):
    c = cfg_at(tmp_path)
    out = feature_set_action(c, {}, Mock(), Mock(), None, NOBODY, "giveback_pct", "50", admin_role="7")
    assert out.startswith("Denied") and c.giveback_pct == 0.0


def test_non_hot_refused_with_open_position_unless_force(tmp_path):
    c = cfg_at(tmp_path); j = Journal(str(tmp_path)); n = Mock()
    out = feature_set_action(c, agents_with_position(), j, n, None, ADMIN, "setup_stop", "on", admin_role="7")
    assert "not hot" in out and c.setup_stop is False
    out = feature_set_action(c, agents_with_position(), j, n, None, ADMIN, "setup_stop", "on", force=True, admin_role="7")
    assert c.setup_stop is True and "forced" in out
    row = [x for x in j.read(0) if x["event"] == "feature_set"][-1]
    assert row["force"] is True and row["new"] == "on" and row["source"] == "discord"
    assert n.card.call_args.args[0] == "FEATURE CHANGED · setup_stop" and "cap −10" in n.card.call_args.args[1]


def test_hot_allowed_with_position_and_quiet(tmp_path):
    c = cfg_at(tmp_path); n = Mock()
    out = feature_set_action(c, agents_with_position(), Journal(str(tmp_path)), n, None, ADMIN, "giveback_pct", "50", admin_role="7")
    assert c.giveback_pct == 50.0 and "giveback_pct 50% (discord)" in out and not n.card.called


def test_bad_value_rejected(tmp_path):
    c = cfg_at(tmp_path)
    assert feature_set_action(c, {}, Mock(), Mock(), None, ADMIN, "giveback_pct", "120", admin_role="7").startswith("⚠️")


def test_feature_reset(tmp_path):
    c = cfg_at(tmp_path); j = Journal(str(tmp_path))
    feature_set_action(c, {}, j, Mock(), None, ADMIN, "regime_gate", "block", admin_role="7")
    assert c.regime_gate == "block"
    feature_set_action(c, {}, j, Mock(), None, ADMIN, "regime_gate", "", admin_role="7", reset=True)
    assert c.regime_gate == "off" and [x for x in j.read(0) if x["event"] == "feature_set"][-1]["reset"] is True


# ------------------------------------------------------------------ rendering / boot card
def test_summary_and_boot_card_text(tmp_path, monkeypatch):
    for f in F.registry():
        monkeypatch.delenv(f.env, raising=False)
    c = cfg_at(tmp_path)
    F.set_override(c.log_dir, c, "giveback_pct", "50", "op")
    txt = features_text(c)
    assert "✅ giveback_pct 50% (discord)" in txt and "⬜ setup_stop off (default)" in txt and "**guardian**" in txt
    import main
    field = main.features_field(c)
    assert "giveback_pct=50%*" in field and field.splitlines()[0].startswith("claude:")
