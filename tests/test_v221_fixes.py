"""v2.2.1 fixes: CliResult usage fields, rules file sections (auto-update tests: test_v222_update.py)."""
from types import SimpleNamespace

from aureon_mt5 import claude_advisor
from aureon_mt5.claude_usage import append
from aureon_mt5.common.claude_cli import CliResult, parse_output


# ------------------------------------------------------------------ A1) CliResult carries usage / cost
def test_parse_output_keeps_usage_and_cost(tmp_path):
    out = ('{"type": "result", "result": "{\\"decision\\": \\"SKIP\\", \\"side\\": \\"BUY\\", \\"confidence\\": \\"low\\", '
           '\\"reason\\": \\"chop\\"}", "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.01}')
    res = parse_output(out, "ENTER")
    assert res.usage == {"input_tokens": 10, "output_tokens": 5} and res.total_cost_usd == 0.01
    assert CliResult(False).usage is None and CliResult(False).total_cost_usd is None
    row = append(str(tmp_path), "entry", SimpleNamespace(ok=False, latency_s=0, error="", timed_out=False, verdict=None))
    assert row["total_cost_usd"] is None and row["input_tokens"] == 0 and row["model"] == ""


# ------------------------------------------------------------------ A3) rules file loads with both mode sections
def test_rules_file_loads_with_both_mode_sections():
    text = open(claude_advisor.RULES_PATH, encoding="utf-8").read()
    assert "== MODE ema2050" in text and "== MODE ema5080" in text
