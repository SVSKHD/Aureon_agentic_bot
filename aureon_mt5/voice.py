"""v2.3.16 — voice command scope.

Deterministic keyword/grammar intent parser + typed confirmation challenges.
No LLM for intent parsing. Whisper/faster-whisper is an optional dependency for transcription.

docs/VOICE_SCOPE.md rule: NEVER execute an order, mode change, restart or any order-affecting action
solely from speech. Require explicit typed confirmation by an authenticated operator.
"""
from __future__ import annotations

import hashlib
import re
import time as _time
from dataclasses import dataclass, field


# ============================================================================= intent parser (deterministic)
INTENTS = {
    "status": {"words": {"status", "how", "position", "what's happening", "update"}, "order_affecting": False},
    "alerts": {"words": {"alerts", "list alerts", "show alerts", "what alerts"}, "order_affecting": False},
    "plan":   {"words": {"plan", "daily plan", "today's plan", "morning plan"}, "order_affecting": False},
    "take":   {"words": {"take", "take it", "go long", "go short", "enter", "buy", "sell"}, "order_affecting": True},
    "skip":   {"words": {"skip", "skip it", "pass", "no trade"}, "order_affecting": True},
    "alert":  {"words": {"set alert", "alert at", "alert"}, "order_affecting": True},
    "halt":   {"words": {"halt", "stop everything", "emergency", "kill"}, "order_affecting": True},
    "unhalt": {"words": {"unhalt", "resume", "resume trading"}, "order_affecting": True},
    "override": {"words": {"override", "override it"}, "order_affecting": True},
}


@dataclass
class ParsedIntent:
    intent: str           # one of INTENTS keys, or "unknown"
    args: dict            # parsed arguments (lots, price, symbol, etc.)
    confidence: float     # 0.0–1.0 (keyword match quality)
    transcript: str       # original transcript
    order_affecting: bool


def parse_intent(transcript: str) -> ParsedIntent:
    """Deterministic keyword/grammar parser. No LLM."""
    text = transcript.strip().lower()
    text = re.sub(r"[^\w\s.]+", " ", text)  # strip punctuation except dots (for prices)
    text = re.sub(r"\s+", " ", text).strip()

    best_intent = "unknown"; best_conf = 0.0; args = {}

    for name, spec in INTENTS.items():
        for phrase in spec["words"]:
            if phrase in text:
                conf = len(phrase) / max(len(text), 1)
                if conf > best_conf:
                    best_intent = name; best_conf = conf

    # Parse arguments for specific intents
    if best_intent == "take":
        lots_match = re.search(r"(\d+(?:\.\d+)?)\s*lots?", text)
        if lots_match:
            args["lots"] = float(lots_match.group(1))
        if "long" in text or "buy" in text:
            args["side"] = "LONG"
        elif "short" in text or "sell" in text:
            args["side"] = "SHORT"

    if best_intent == "alert":
        price_match = re.search(r"(?:at|price)\s*(\d{3,5}(?:\.\d+)?)", text)
        if price_match:
            args["price"] = float(price_match.group(1))
        if "above" in text:
            args["direction"] = "above"
        elif "below" in text:
            args["direction"] = "below"
        note_match = re.search(r"note\s+(.+)", text)
        if note_match:
            args["note"] = note_match.group(1).strip()[:120]

    order_affecting = INTENTS.get(best_intent, {}).get("order_affecting", True)
    return ParsedIntent(best_intent, args, round(min(best_conf * 2, 1.0), 2), transcript, order_affecting)


# ============================================================================= confirmation challenge
@dataclass
class Challenge:
    code: str
    intent: ParsedIntent
    created: float
    ttl: float = 60.0
    confirmed: bool = False
    expired: bool = False

    def is_valid(self, now: float | None = None) -> bool:
        now = now or _time.time()
        return not self.confirmed and not self.expired and (now - self.created) <= self.ttl


class ConfirmationManager:
    """Manages typed confirmation challenges for order-affecting voice commands."""

    def __init__(self, ttl: float = 60.0):
        self.ttl = ttl
        self._pending: dict[str, Challenge] = {}  # chat_id -> challenge

    def create(self, chat_id: str, intent: ParsedIntent) -> Challenge:
        """Create a confirmation challenge. Returns the challenge with the code to display."""
        code = hashlib.sha256(f"{chat_id}:{intent.intent}:{_time.time()}".encode()).hexdigest()[:3].upper()
        ch = Challenge(code=code, intent=intent, created=_time.time(), ttl=self.ttl)
        self._pending[chat_id] = ch
        return ch

    def confirm(self, chat_id: str, text: str) -> Challenge | None:
        """Try to confirm a pending challenge. Returns the challenge if the code matches, else None."""
        ch = self._pending.get(chat_id)
        if ch is None or not ch.is_valid():
            return None
        # Match "CONFIRM <code>" exactly
        match = re.match(r"CONFIRM\s+([A-F0-9]{3})", text.strip().upper())
        if match and match.group(1) == ch.code:
            ch.confirmed = True
            del self._pending[chat_id]
            return ch
        return None

    def expire_stale(self, now: float | None = None):
        now = now or _time.time()
        for cid in list(self._pending):
            ch = self._pending[cid]
            if not ch.is_valid(now):
                ch.expired = True
                del self._pending[cid]


# ============================================================================= transcription (optional dep)
def transcribe(audio_path: str, model: str = "base") -> str | None:
    """Transcribe an audio file using whisper (optional dependency). Returns the text or None if whisper is not installed."""
    try:
        import whisper  # type: ignore
        m = whisper.load_model(model)
        result = m.transcribe(audio_path)
        return result.get("text", "").strip()
    except ImportError:
        pass
    try:
        from faster_whisper import WhisperModel  # type: ignore
        m = WhisperModel(model)
        segments, _ = m.transcribe(audio_path)
        return " ".join(s.text for s in segments).strip()
    except ImportError:
        pass
    return None


# ============================================================================= execute read-only intents
def execute_readonly(intent: ParsedIntent, cfg, agents, journal) -> str:
    """Execute a read-only intent and return the text response."""
    if intent.intent == "status":
        lines = []
        for sym, ag in agents.items():
            h = ag.health
            lines.append(f"{sym}: {h.get('status', '?')} · bar {h.get('last_bar', '—')} · {h.get('close', '—')}")
        return "\n".join(lines) or "no agents running"

    if intent.intent == "alerts":
        lines = []
        for sym, ag in agents.items():
            armed = ag.alerts.armed(sym) if ag.alerts else []
            if armed:
                for a in armed:
                    lines.append(f"{a['id']} {a['symbol']} {a['price']:.2f} ({a['side_hint']})")
            else:
                lines.append(f"{sym}: no armed alerts")
        return "\n".join(lines)

    if intent.intent == "plan":
        from .daily_plan import plan_card
        card = plan_card(cfg, agents, journal)
        return card["title"] + "\n" + "\n".join(f["value"] for f in card["fields"])

    return f"unknown read-only intent: {intent.intent}"


# ============================================================================= format challenge message
def challenge_message(ch: Challenge) -> str:
    """Format the confirmation challenge for the user."""
    intent = ch.intent
    if intent.intent == "take":
        side = intent.args.get("side", "")
        lots = intent.args.get("lots", "")
        detail = f"take {side} {lots}" if side else "take"
    elif intent.intent == "halt":
        detail = "HALT all positions"
    elif intent.intent == "alert":
        detail = f"set alert at {intent.args.get('price', '?')}"
    else:
        detail = intent.intent
    return f"Reply CONFIRM {ch.code} within 60 s to {detail}"
