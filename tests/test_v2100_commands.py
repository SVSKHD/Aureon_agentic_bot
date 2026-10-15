"""v2.10.0 — /commands documents every registered root command (the directory never shows an undocumented command)."""
import os, re, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aureon_mt5 import bot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def registered():
    src = open(os.path.join(ROOT, "aureon_mt5", "bot.py"), encoding="utf-8").read()
    return set(re.findall(r'tree\.command\(name="([^"]+)"', src)) | set(re.findall(r'app_commands\.Group\(name="([^"]+)"', src))


def test_every_registered_command_has_help():
    documented = {n for g in bot.HELP.values() for n in g}
    missing = sorted(registered() - documented)
    assert not missing, f"add these to bot.HELP: {missing}"


def test_new_commands_registered_and_documented():
    for name in ("features", "feature-set", "feature-reset", "feature-help", "repairs", "repair", "repair-apply", "repair-discard"):
        assert name in registered(), name


def test_directory_uses_descriptions_for_leftovers():
    cmd = lambda n, d: types.SimpleNamespace(name=n, description=d)
    tree = types.SimpleNamespace(get_commands=lambda: [cmd("status", "x"), cmd("brand-new", "Does a new thing")])
    out = bot.command_directory(tree, {"Monitoring": {"status": "snapshot"}})
    assert out["Monitoring"] == ["/status — snapshot"] and out["Other installed"] == ["/brand-new — Does a new thing"]
