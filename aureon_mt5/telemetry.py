"""Failures with full context → logs/aureon.log (traceback) and Discord (deduped, backoff). Journals an "error" event."""
from __future__ import annotations

import logging
import os
import time as _time
import traceback

_log = logging.getLogger("aureon")
_last: dict[str, float] = {}
BACKOFF_S = 300   # the same failure key is posted at most every 5 minutes


def setup(log_dir: str):
    os.makedirs(log_dir, exist_ok=True)
    if not _log.handlers:
        h = logging.FileHandler(os.path.join(log_dir, "aureon.log"), encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _log.addHandler(h); _log.setLevel(logging.INFO)


def info(msg: str): _log.info(msg)


def failure(notify, journal, *, title: str, key: str, mode: str = "", symbol: str = "", ticket=None, action: str = "",
            requested=None, current=None, bid=None, ask=None, retcode=None, comment: str = "", last_error: str = "",
            exc: BaseException | None = None, retry: str = "retry on next poll"):
    tb = traceback.format_exc() if exc else ""
    rec = dict(mode=mode, symbol=symbol, ticket=ticket, action=action, requested=requested, current=current, bid=bid, ask=ask,
               retcode=retcode, comment=comment, last_error=last_error, exc=repr(exc) if exc else None)
    _log.error(f"{title} · {rec}\n{tb}")
    if journal is not None:
        journal.log("error", title=title, **{k: v for k, v in rec.items() if v not in (None, "")})
    now = _time.time()
    if now - _last.get(key, 0) < BACKOFF_S:
        return
    _last[key] = now
    lines = [f"Mode: {mode} · Symbol: {symbol}" + (f" · Ticket: #{ticket}" if ticket else "")]
    if action: lines.append(f"Action: {action}")
    if requested is not None: lines.append(f"Requested: {requested} · Current: {current}")
    if bid is not None: lines.append(f"Bid: {bid} · Ask: {ask}")
    if retcode is not None or comment or last_error: lines.append(f"MT5: {retcode} {comment} {last_error}".strip())
    if exc: lines.append((tb.strip().splitlines() or [repr(exc)])[-1][:180])
    lines.append(f"Guardian state unchanged. {retry}.")
    notify.raw(f"**❌ {title}**\n" + "\n".join(lines))
