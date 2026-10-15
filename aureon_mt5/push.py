"""v2.11.0 — the push layer: the trade reaches the phone when it happens, with actions.

    Push(cfg, journal).send(PushEvent)        one push per dedupe key, to every backend in AUREON_PUSH (ntfy, pushover, telegram)

Backends run on a small thread pool with a 10 s timeout; a failure is journaled (push_failed) and printed, never raised.
Mapping (map_event): ASK → urgent + TAKE / SKIP / READ · FILLED → high · CLOSED / STOP → normal (STOP losing more than
AUREON_PUSH_LOSS_ALERT_PTS → urgent) · FLIP_HOLD → high + READ · SHOULD WE CLOSE? → high + CLOSE / READ ·
MAINTENANCE / COLD START / MT5 NOT UP / REPAIR AUTO-APPLY → high. Everything else: no push.

Action buttons call the console (console.py) at AUREON_CONSOLE_URL/console/action. The Bearer they carry is NOT the console
token: it is a scoped, single-use action token (HMAC-SHA256 with the console token as key) valid for exactly that action on
that ask until the ask expires — ntfy.sh / Pushover / Telegram never see the console token. Telegram buttons are callbacks
that the long-poll thread (getUpdates, 25 s) turns into the same console actions in-process. Presentation only: nothing
here places or changes a trade by itself."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

BACKENDS = ("ntfy", "pushover", "telegram")
NTFY_PRIORITY = {"urgent": 5, "high": 4, "normal": 3}
PUSHOVER_PRIORITY = {"urgent": 2, "high": 1, "normal": 0}
TIMEOUT_S = 10.0
OPS_EVENTS = ("MAINTENANCE", "COLD_START", "MT5_NOT_UP", "REPAIR_AUTO")
TAGS = {"ASK": ["rotating_light"], "FILLED": ["white_check_mark"], "CLOSED": ["moneybag"], "STOP": ["octagonal_sign"],
        "FLIP_HOLD": ["warning"], "POSITION_ASK": ["question"], "READ_VERDICT": ["brain"]}


@dataclass
class PushAction:
    label: str
    action: str                    # take | skip | close | read
    symbol: str = ""
    ask_key: str = ""


@dataclass
class PushEvent:
    klass: str
    key: str
    title: str
    lines: list = field(default_factory=list)
    priority: str = "normal"       # urgent | high | normal
    png: str | None = None
    actions: list = field(default_factory=list)
    event: str = ""
    symbol: str = ""


# ----------------------------------------------------------------------------- scoped action tokens
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class ActionTokens:
    """`act.<payload>.<sig>`: payload = {a: action, s: symbol, k: ask key, x: expiry, n: nonce}. Single use."""

    def __init__(self, secret: str, clock=_time.time):
        self.secret = (secret or "").encode()
        self.clock = clock
        self.used: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self, action: str, symbol: str = "", ask_key: str = "", ttl_s: float = 1200.0) -> str:
        body = json.dumps({"a": action, "s": symbol, "k": ask_key, "x": int(self.clock() + ttl_s),
                           "n": _b64(os.urandom(9))}, separators=(",", ":")).encode()
        sig = hmac.new(self.secret, body, hashlib.sha256).digest()[:18]
        return f"act.{_b64(body)}.{_b64(sig)}"

    def verify(self, token: str, action: str, symbol: str = "", ask_key: str = "") -> str | None:
        """None when valid for exactly this request (and burns it); else the reason."""
        if not self.secret:
            return "console token not set"
        try:
            pre, body_s, sig_s = str(token).split(".")
            body = _unb64(body_s); sig = _unb64(sig_s)
        except Exception:
            return "malformed action token"
        if pre != "act" or not hmac.compare_digest(sig, hmac.new(self.secret, body, hashlib.sha256).digest()[:18]):
            return "bad action token"
        d = json.loads(body)
        if d["x"] < self.clock():
            return "action token expired"
        if d["a"] != action or (d["s"] and d["s"] != (symbol or "")) or (d["k"] and d["k"] != (ask_key or "")):
            return "action token is for a different action"
        with self._lock:
            if d["n"] in self.used:
                return "action token already used"
            now = self.clock()
            self.used = {n: t for n, t in self.used.items() if t > now}
            self.used[d["n"]] = d["x"]
        return None


# ----------------------------------------------------------------------------- mapping
def _plain(s) -> str:
    return re.sub(r"\*\*|__|`", "", str(s or "")).strip()


def map_event(cfg, *, key: str, title: str, lines=None, fields=None, png=None, symbol: str = "", event: str = "",
              klass: str = "info", points: float | None = None) -> PushEvent | None:
    ev = str(event or "").upper()
    body = [_plain(x) for x in (lines or []) if _plain(x)]
    if fields:
        body.append(" · ".join(f"{_plain(f.get('name'))} {_plain(f.get('value'))}" for f in fields[:6]))
    pe = PushEvent(klass=klass, key=key, title=_plain(title), lines=body, png=png, event=ev, symbol=symbol)
    if ev == "ASK":
        pe.priority = "urgent"
        pe.actions = [PushAction("TAKE", "take", symbol, key), PushAction("SKIP", "skip", symbol, key), PushAction("READ", "read", symbol, key)]
    elif ev == "FILLED":
        pe.priority = "high"
    elif ev in ("CLOSED", "STOP"):
        loss = -float(points) if points is not None else 0.0
        pe.priority = "urgent" if (ev == "STOP" and loss > float(getattr(cfg, "push_loss_alert_pts", 5.0))) else "normal"
    elif ev == "FLIP_HOLD":
        pe.priority = "high"; pe.actions = [PushAction("READ", "read", symbol, key)]
    elif ev == "POSITION_ASK":
        pe.priority = "high"; pe.actions = [PushAction("CLOSE", "close", symbol, key), PushAction("READ", "read", symbol, key)]
    elif ev in OPS_EVENTS:
        pe.priority = "high"
    elif ev == "READ_VERDICT":
        pe.priority = "high"
    else:
        return None
    return pe


# ----------------------------------------------------------------------------- request builders (pure, testable)
def console_ready(cfg) -> bool:
    tok = str(getattr(cfg, "console_token", "") or "")
    return bool(getattr(cfg, "console_url", "")) and len(tok) >= 32


def click_url(cfg) -> str:
    u = str(getattr(cfg, "console_url", "") or "")
    return f"{u}/console/state" if u else ""


def _header(v: str) -> str:
    """HTTP header value: latin-1 safe, else RFC 2047 (ntfy decodes =?UTF-8?B?…?=)."""
    v = str(v).replace("\n", " ")
    try:
        v.encode("latin-1")
        return v
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(v.encode()).decode() + "?="


def ntfy_actions(cfg, pe: PushEvent, tokens: ActionTokens, ttl_s: float) -> list[dict]:
    if not (pe.actions and console_ready(cfg)):
        return []
    out = []
    for a in pe.actions[:3]:                                     # ntfy: at most 3 actions
        out.append({"action": "http", "label": a.label, "url": f"{cfg.console_url}/console/action", "method": "POST",
                    "headers": {"Authorization": f"Bearer {tokens.issue(a.action, a.symbol, a.ask_key, ttl_s)}",
                                "Content-Type": "application/json"},
                    "body": json.dumps({"action": a.action, "symbol": a.symbol, "ask_key": a.ask_key}, separators=(",", ":")),
                    "clear": True})
    return out


def ntfy_request(cfg, pe: PushEvent, tokens: ActionTokens, ttl_s: float = 1200.0) -> dict:
    """{method, url, headers, json | data}. JSON publish to the server root; with an image (AUREON_PUSH_IMAGES=1) a PUT upload
    to /<topic> with the same fields as X- headers."""
    base = str(getattr(cfg, "ntfy_url", "") or "https://ntfy.sh").rstrip("/")
    msg = "\n".join(pe.lines)[:3500] or pe.title
    tags = TAGS.get(pe.event, ["gear"] if pe.klass == "ops" else []) + ([pe.symbol] if pe.symbol else [])
    acts = ntfy_actions(cfg, pe, tokens, ttl_s)
    auth = {"Authorization": f"Bearer {cfg.ntfy_token}"} if getattr(cfg, "ntfy_token", "") else {}
    click = click_url(cfg)
    if getattr(cfg, "push_images", False) and pe.png and os.path.exists(pe.png):
        h = {**auth, "X-Title": _header(pe.title), "X-Message": _header(msg.replace("\n", "\\n")),
             "X-Priority": str(NTFY_PRIORITY[pe.priority]), "X-Filename": os.path.basename(pe.png)}
        if tags:
            h["X-Tags"] = ",".join(tags)
        if click:
            h["X-Click"] = click
        if acts:
            h["X-Actions"] = json.dumps(acts, separators=(",", ":"))
        with open(pe.png, "rb") as f:
            data = f.read()
        return {"method": "PUT", "url": f"{base}/{cfg.ntfy_topic}", "headers": h, "data": data}
    body = {"topic": cfg.ntfy_topic, "title": pe.title, "message": msg, "priority": NTFY_PRIORITY[pe.priority]}
    if tags:
        body["tags"] = tags
    if click:
        body["click"] = click
    if acts:
        body["actions"] = acts
    return {"method": "POST", "url": base, "headers": {**auth, "Content-Type": "application/json"}, "json": body}


def pushover_request(cfg, pe: PushEvent) -> dict:
    data = {"token": cfg.pushover_token, "user": cfg.pushover_user, "title": pe.title[:250],
            "message": ("\n".join(pe.lines) or pe.title)[:1024], "priority": PUSHOVER_PRIORITY[pe.priority]}
    if pe.priority == "urgent":
        data.update(retry=30, expire=600)
    if click_url(cfg):
        data.update(url=click_url(cfg), url_title="Aureon console")
    req = {"method": "POST", "url": "https://api.pushover.net/1/messages.json", "data": data}
    if getattr(cfg, "push_images", False) and pe.png and os.path.exists(pe.png):
        req["file"] = pe.png
    return req


def telegram_text(pe: PushEvent) -> str:
    return (f"{pe.title}\n" + "\n".join(pe.lines))[:4000]


# ----------------------------------------------------------------------------- the layer
class Push:
    def __init__(self, cfg, journal=None, *, http=None, clock=_time.time, tg_send=None):
        self.cfg, self.journal, self.clock = cfg, journal, clock
        self._http = http
        self._tg_send = tg_send
        self.tokens = ActionTokens(getattr(cfg, "console_token", "") or "", clock=clock)
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="aureon-push")
        self.sent_keys: dict[str, float] = {}
        self.tg_actions: dict[str, tuple] = {}     # short id → (action, symbol, ask_key, expiry)
        self.handler = None                         # set by main: actions.run(action, symbol=, ask_key=, user=)
        self._poller = None
        self._stop = threading.Event()
        self.last: list[dict] = []                  # last results (for /server and tests)

    @property
    def http(self):
        if self._http is None:
            import requests  # type: ignore
            self._http = requests
        return self._http

    def backends(self) -> list[str]:
        return [b.lower() for b in (getattr(self.cfg, "push", ()) or ()) if b.lower() in BACKENDS]

    def ttl_s(self) -> float:
        return float(getattr(self.cfg, "ask_ttl_min", 20)) * 60

    # -- from notify.py
    def notify(self, *, key, title, lines=None, fields=None, png=None, symbol="", event="", klass="info", points=None, meta=None):
        if not self.backends():
            return []
        pe = map_event(self.cfg, key=key, title=title, lines=lines, fields=fields, png=png, symbol=symbol, event=event,
                       klass=klass, points=points)
        return self.send(pe) if pe is not None else []

    def send(self, pe: PushEvent) -> list:
        """One push per dedupe key to every configured backend. Returns the futures. Never raises."""
        try:
            if pe.key in self.sent_keys:
                return []
            now = self.clock()
            self.sent_keys[pe.key] = now
            if len(self.sent_keys) > 5000:
                self.sent_keys = dict(list(self.sent_keys.items())[-1000:])
            return [self.pool.submit(self._run, b, pe) for b in self.backends()]
        except Exception as e:
            print("  (push failed:", e, ")")
            return []

    def _run(self, backend: str, pe: PushEvent) -> bool:
        ok, err = False, ""
        try:
            ok = bool(getattr(self, f"_send_{backend}")(pe))
        except Exception as e:
            err = repr(e)[:200]
        rec = {"t": self.clock(), "backend": backend, "key": pe.key, "event": pe.event, "ok": ok, "error": err}
        self.last = (self.last + [rec])[-20:]
        if not ok:
            print(f"  (push {backend} failed: {err or 'HTTP error'})")
        try:
            if self.journal is not None:
                self.journal.log("push_sent" if ok else "push_failed", backend=backend, key=pe.key, event=pe.event,
                                 priority=pe.priority, error=err or None)
        except Exception:
            pass
        return ok

    def _send_ntfy(self, pe: PushEvent) -> bool:
        req = ntfy_request(self.cfg, pe, self.tokens, self.ttl_s())
        if req["method"] == "PUT":
            r = self.http.put(req["url"], data=req["data"], headers=req["headers"], timeout=TIMEOUT_S)
        else:
            r = self.http.post(req["url"], json=req["json"], headers=req["headers"], timeout=TIMEOUT_S)
        return 200 <= r.status_code < 300

    def _send_pushover(self, pe: PushEvent) -> bool:
        req = pushover_request(self.cfg, pe)
        if req.get("file"):
            with open(req["file"], "rb") as f:
                r = self.http.post(req["url"], data=req["data"], files={"attachment": (os.path.basename(req["file"]), f, "image/png")},
                                   timeout=TIMEOUT_S)
        else:
            r = self.http.post(req["url"], data=req["data"], timeout=TIMEOUT_S)
        return 200 <= r.status_code < 300

    # -- telegram: reuse scripts/deadman_fallback.telegram_send; buttons are callback_data → console actions
    def telegram_keyboard(self, pe: PushEvent) -> dict | None:
        if not pe.actions:
            return None
        row = []
        for a in pe.actions:
            sid = _b64(os.urandom(6))
            self.tg_actions[sid] = (a.action, a.symbol, a.ask_key, self.clock() + self.ttl_s())
            row.append({"text": a.label, "callback_data": f"a:{sid}"})
        while len(self.tg_actions) > 300:
            self.tg_actions.pop(next(iter(self.tg_actions)))
        return {"inline_keyboard": [row]}

    def _send_telegram(self, pe: PushEvent) -> bool:
        send = self._tg_send or _telegram_send()
        return bool(send(self.cfg.telegram_token, self.cfg.telegram_chat, telegram_text(pe), timeout=TIMEOUT_S,
                         reply_markup=self.telegram_keyboard(pe)))

    def on_callback(self, cq: dict) -> str:
        """One Telegram callback_query → a console action. Only from the configured chat. Returns the answer text."""
        chat = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
        if chat != str(self.cfg.telegram_chat):
            return "not this chat"
        sid = str(cq.get("data", ""))[2:]
        hit = self.tg_actions.pop(sid, None)
        if hit is None:
            return "expired or already used"
        action, symbol, ask_key, exp = hit
        if exp < self.clock():
            return "expired"
        if self.handler is None:
            return "console actions not available"
        who = (cq.get("from") or {}).get("username") or (cq.get("from") or {}).get("id")
        r = self.handler(action, symbol=symbol, ask_key=ask_key, user=f"telegram:{who}")
        try:
            if self.journal is not None:
                self.journal.log("console_call", source="push", path="telegram", action=action, symbol=symbol, key=ask_key,
                                 ok=bool(r.get("ok")), by=f"telegram:{who}")
        except Exception:
            pass
        return r.get("text", "")[:190]

    def poll_once(self, offset: int = 0, timeout: int = 25) -> int:
        api = f"https://api.telegram.org/bot{self.cfg.telegram_token}"
        r = self.http.get(f"{api}/getUpdates", params={"offset": offset, "timeout": timeout,
                                                       "allowed_updates": json.dumps(["callback_query"])}, timeout=timeout + 10)
        for u in (r.json() or {}).get("result", []):
            offset = max(offset, int(u["update_id"]) + 1)
            cq = u.get("callback_query")
            if cq:
                text = self.on_callback(cq)
                try:
                    self.http.post(f"{api}/answerCallbackQuery", data={"callback_query_id": cq["id"], "text": text}, timeout=TIMEOUT_S)
                except Exception:
                    pass
        return offset

    def start(self):
        """The Telegram long-poll thread (only with telegram in AUREON_PUSH)."""
        if "telegram" not in self.backends() or self._poller is not None:
            return
        def loop():
            off = 0
            while not self._stop.is_set():
                try:
                    off = self.poll_once(off)
                except Exception as e:
                    print("  (telegram poll failed:", e, ")"); self._stop.wait(10)
        self._poller = threading.Thread(target=loop, name="aureon-telegram-poll", daemon=True)
        self._poller.start()

    def stop(self):
        self._stop.set()
        self.pool.shutdown(wait=False, cancel_futures=True)


def _telegram_send():
    """scripts/deadman_fallback.telegram_send (loaded by path; scripts/ is not a package)."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "deadman_fallback.py")
    spec = importlib.util.spec_from_file_location("aureon_deadman_fallback", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.telegram_send
