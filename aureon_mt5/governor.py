"""v2.3.12 — enforced risk governor. No override flag — the governor is not bypassable.

Every button, /take, /override and (later) the agent passes through Governor.check() BEFORE broker.place_market.
Rules are code-level: a blocked entry never reaches MT5."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class Decision:
    allowed: bool
    rule: str       # stable machine-readable name
    detail: str     # human-readable


class Governor:
    def __init__(self, cfg, clock=None):
        self.cfg = cfg
        self.clock = clock or (lambda: datetime.now(IST))

    def check(self, symbol: str, side: str, lots: float, context: dict) -> Decision:
        """Check all rules. Returns the FIRST violation or an allowed decision.
        context keys: trades_today, losses_today, consecutive_losses, realised_pts_today,
        realised_money_today, equity, ist_now, news_block, open_positions."""
        ist_now = context.get("ist_now") or self.clock()
        ist_h = ist_now.hour + ist_now.minute / 60

        # halted (v2.3.13)
        if context.get("halted"):
            return Decision(False, "halted", "HALT is on — /unhalt to resume")

        # session_window: 23:00–05:30 IST
        if ist_h >= 23 or ist_h < 5.5:
            return Decision(False, "session_window", f"no entries 23:00–05:30 IST (now {ist_now:%H:%M})")

        # news_block
        if context.get("news_block"):
            return Decision(False, "news_block", "news blackout active")

        # max_trades_day
        trades = context.get("trades_today", 0)
        if trades >= self.cfg.max_trades_day:
            return Decision(False, "max_trades_day", f"{trades} trades today (limit {self.cfg.max_trades_day})")

        # daily_loss_cap
        equity = context.get("equity", 0)
        realised = context.get("realised_money_today", 0)
        if equity > 0 and self.cfg.daily_loss_limit_pct > 0:
            cap = equity * self.cfg.daily_loss_limit_pct / 100
            if realised <= -cap:
                return Decision(False, "daily_loss_cap",
                                f"day loss {realised:,.0f} >= cap {cap:,.0f} ({self.cfg.daily_loss_limit_pct:g}% of {equity:,.0f})")

        # loss_multiple (2x rule): block when today's realised loss >= multiple × per-trade risk
        if self.cfg.loss_multiple > 0 and equity > 0:
            per_trade_risk = equity * self.cfg.risk_pct / 100
            threshold = self.cfg.loss_multiple * per_trade_risk
            if realised <= -threshold:
                return Decision(False, "loss_multiple",
                                f"day loss {realised:,.0f} >= {self.cfg.loss_multiple:g}x per-trade risk {per_trade_risk:,.0f}")

        # one_position_per_direction
        open_pos = context.get("open_positions", [])
        direction = "long" if side.upper() == "LONG" else "short"
        if any(p.get("direction") == direction and p.get("symbol", "").upper() == symbol.upper() for p in open_pos):
            return Decision(False, "one_position_per_direction",
                            f"already {direction} {symbol}")

        return Decision(True, "ok", "all rules pass")

    def status(self, context: dict) -> str:
        """One-line status for /status."""
        trades = context.get("trades_today", 0)
        equity = context.get("equity", 0)
        realised = context.get("realised_money_today", 0)
        cap = equity * self.cfg.daily_loss_limit_pct / 100 if equity > 0 else 0
        pct = abs(realised) / cap * 100 if cap > 0 else 0
        return (f"{trades}/{self.cfg.max_trades_day} trades · "
                f"loss {pct:.0f}%/{self.cfg.daily_loss_limit_pct:g}% · OK")

    def rules_table(self, context: dict) -> list[dict]:
        """All rules with current values, for /governor."""
        ist_now = context.get("ist_now") or self.clock()
        ist_h = ist_now.hour + ist_now.minute / 60
        trades = context.get("trades_today", 0)
        equity = context.get("equity", 0)
        realised = context.get("realised_money_today", 0)
        cap = equity * self.cfg.daily_loss_limit_pct / 100 if equity > 0 else 0
        per_trade = equity * self.cfg.risk_pct / 100 if equity > 0 else 0
        loss_thresh = self.cfg.loss_multiple * per_trade
        open_pos = context.get("open_positions", [])
        return [
            {"rule": "session_window", "value": f"{ist_now:%H:%M} IST", "limit": "05:30–23:00",
             "ok": not (ist_h >= 23 or ist_h < 5.5)},
            {"rule": "news_block", "value": "active" if context.get("news_block") else "clear",
             "limit": "clear", "ok": not context.get("news_block")},
            {"rule": "max_trades_day", "value": str(trades), "limit": str(self.cfg.max_trades_day),
             "ok": trades < self.cfg.max_trades_day},
            {"rule": "daily_loss_cap", "value": f"{realised:,.0f}", "limit": f"-{cap:,.0f} ({self.cfg.daily_loss_limit_pct:g}%)",
             "ok": realised > -cap if cap > 0 else True},
            {"rule": "loss_multiple", "value": f"{realised:,.0f}", "limit": f"-{loss_thresh:,.0f} ({self.cfg.loss_multiple:g}x risk)",
             "ok": realised > -loss_thresh if loss_thresh > 0 else True},
            {"rule": "one_position_per_direction", "value": f"{len(open_pos)} open",
             "limit": "1 per symbol per direction", "ok": True},
        ]
