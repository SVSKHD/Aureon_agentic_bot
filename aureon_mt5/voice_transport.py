"""v2.3.16 — voice transport: Telegram bot + HTTP endpoint (both optional).

Telegram: TELEGRAM_BOT_TOKEN + AUREON_VOICE_CHATS (allowed chat ids).
HTTP: POST /voice with X-Aureon-Secret header, bound to localhost only.

Neither transport is started when AUREON_VOICE=0 (default).
Audio is never stored after transcription."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time as _time

from .voice import (ConfirmationManager, ParsedIntent, challenge_message,
                    execute_readonly, parse_intent, transcribe)


class VoiceHandler:
    """Handles voice commands from any transport. Stateless except for the confirmation manager."""

    def __init__(self, cfg, agents: dict, journal, notify, bot_functions: dict | None = None):
        self.cfg = cfg
        self.agents = agents
        self.journal = journal
        self.notify = notify
        self.bot_functions = bot_functions or {}  # {intent: callable} for order-affecting intents
        self.confirms = ConfirmationManager(ttl=60.0)

    def handle_audio(self, audio_path: str, chat_id: str) -> str:
        """Transcribe audio → parse intent → execute or create challenge. Returns response text."""
        transcript = transcribe(audio_path)
        if transcript is None:
            return "transcription unavailable (whisper not installed)"

        intent = parse_intent(transcript)
        self.journal.log("voice_command", chat_id=chat_id, transcript=transcript,
                         intent=intent.intent, args=intent.args, confidence=intent.confidence)

        if intent.intent == "unknown":
            return f"didn't understand: \"{transcript}\""

        if not intent.order_affecting:
            result = execute_readonly(intent, self.cfg, self.agents, self.journal)
            self.journal.log("voice_result", chat_id=chat_id, intent=intent.intent, result="executed")
            return result

        # Order-affecting: create confirmation challenge
        ch = self.confirms.create(chat_id, intent)
        self.journal.log("voice_challenge", chat_id=chat_id, intent=intent.intent, code=ch.code)
        return challenge_message(ch)

    def handle_text(self, text: str, chat_id: str) -> str | None:
        """Handle a typed message — check for confirmation codes. Returns response or None if not a confirm."""
        text = text.strip()
        if not text.upper().startswith("CONFIRM"):
            return None

        ch = self.confirms.confirm(chat_id, text)
        if ch is None:
            return "no pending challenge, or wrong code / expired"

        intent = ch.intent
        self.journal.log("voice_confirmed", chat_id=chat_id, intent=intent.intent, code=ch.code)

        # Execute the confirmed intent through the same bot functions
        fn = self.bot_functions.get(intent.intent)
        if fn is not None:
            try:
                result = fn(intent)
                self.journal.log("voice_result", chat_id=chat_id, intent=intent.intent, result="confirmed_executed")
                return f"confirmed — {result}"
            except Exception as e:
                return f"confirmed but failed: {e}"

        return f"confirmed {intent.intent} — but no handler registered"

    def is_allowed_chat(self, chat_id: str) -> bool:
        if not self.cfg.voice_chats:
            return True  # no restriction when empty
        return str(chat_id) in self.cfg.voice_chats
