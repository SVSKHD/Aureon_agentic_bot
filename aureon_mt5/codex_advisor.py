"""v2.3.0 — Codex add-on: the same second opinion as Claude, through `codex exec` (CLI login only; no API key path).

Same as ClaudeAdvisor (it IS ClaudeAdvisor with a different CLI): same triggers (ema2050: ENTER bar + pullback-in-trade;
ema5080: P / CROSS / RE + pullback), same build_snapshot (no account data), same prompts/advisor_rules.md, same reply contract
validated by claude_cli.validate, same fail-closed and STALE handling, one job per pullback, own worker thread, own daily budget
(AUREON_CODEX_MAX_CALLS), own usage ledger logs/codex_usage.jsonl.

Modes (AUREON_CODEX): off · review · advisory. In BOTH review and advisory a Codex verdict is JOURNALED AND SHOWN ONLY — it is
never applied: apply_pullback below has no path to the guardian's _move_sl / _close (a test walks every verdict kind).
Fast exit is identical: the guardian runs first, a closed trade turns the queued verdict into "position no longer open",
a verdict that arrives after the next bar closed is STALE."""
from __future__ import annotations

from .claude_advisor import BAR_S, ClaudeAdvisor
from .common import codex_cli
from .common.timeutil import now_server

MODES = ("off", "review", "advisory")
ADVISE_MODES = ("advisory",)
REVIEW_TIMEOUT_S = 120.0                       # free-text reviews take longer than a verdict


def normalise_mode(m: str | None) -> str:
    m = (m or "off").strip().lower()
    return m if m in MODES else "off"


class CodexAdvisor(ClaudeAdvisor):
    SOURCE = "codex"
    LABEL = "Codex"
    TAG = "CODEX"
    ATTR = "codex"
    MODES = MODES
    ADVISE_MODES = ADVISE_MODES
    ENV = "AUREON_CODEX"

    def __init__(self, cfg, notify, journal, *, health_notify=None, caller=codex_cli.call, start: bool = True):
        super().__init__(cfg, notify, journal, health_notify=health_notify, caller=caller, start=start)

    @classmethod
    def mode_of(cls, cfg) -> str:
        return normalise_mode(getattr(cfg, "codex_mode", "off"))

    @property
    def manage(self) -> bool:                  # there is no manage mode for Codex
        return False

    @property
    def max_calls(self) -> int:
        return int(self.cfg.codex_max_calls)

    @property
    def entry_model(self) -> str:
        return self.cfg.codex_model or ""      # "" = the CLI's default model

    @property
    def pullback_model(self) -> str:
        return self.cfg.codex_model or ""

    @property
    def review_model(self) -> str:
        return ""                              # daily / Saturday reviews: CLI default

    def _resolve_bin(self) -> str:
        return codex_cli.executable(self.cfg.codex_bin)

    def _bin_name(self) -> str:
        return self.cfg.codex_bin

    def _version(self) -> dict:
        v = codex_cli.version(self.cfg.codex_bin)
        ok = bool(v) and v != "unavailable"
        return {"ok": ok, "version": v if ok else "", "path": self._resolve_bin(),
                "error": "" if ok else f"'{self.cfg.codex_bin}' not found or not runnable — install the Codex CLI or set AUREON_CODEX_BIN"}

    def _invoke(self, prompt: str, model: str, event: str, *, free_text: bool = False, image=None):
        # v2.8.0: Codex gets no chart — `image` is accepted and dropped
        timeout = max(float(self.cfg.codex_timeout), REVIEW_TIMEOUT_S) if free_text else float(self.cfg.codex_timeout)
        return self.caller(prompt, event=event, binary=self.cfg.codex_bin, workdir=self.cfg.codex_workdir, timeout=timeout,
                           model=model or None, free_text=free_text)

    def _login_hint(self) -> str:
        return f"Run `codex login` (ChatGPT sign-in; no API key) and `codex login status` in {self.cfg.codex_workdir}."

    def login_status(self) -> str:
        return codex_cli.login_status(self.cfg.codex_bin)

    # ------------------------------------------------------------------ verdict on a position (AGENT thread) — shown, never applied
    def apply_pullback(self, agent, job: dict, p: dict | None, st: dict) -> bool:
        """Always returns False. No call into the guardian, the broker or MT5 exists on this path."""
        v = job["verdict"]; dec = v["decision"]
        stale = now_server(agent.off) >= job["bar_t"] + 2 * BAR_S
        if p is None:
            note = "position no longer open — nothing to do"
            self.stats["fast_exit_cancels"] += 1
            self.journal.log("codex_fast_exit_cancel", symbol=agent.symbol)
        elif stale:
            note = "STALE — arrived after the next bar closed; not acted"
        elif dec == "HOLD":
            note = "hold — guardian keeps managing by its rules"
        else:
            note = f"Codex {self.mode} — {dec} shown, never applied"
        self._journal(job, v, latency=job.get("latency", 0.0), acted=False, stale=stale, note=note)
        self._pullback_card(agent, job, v, p, st, note, False, stale)
        return False
