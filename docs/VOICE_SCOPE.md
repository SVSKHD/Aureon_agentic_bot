# Voice command scope — NOT IMPLEMENTED

Voice is **not implemented** in v2.2.0. Candidate intents, mapped to existing or proposed slash commands:

| Voice intent | Slash command | Safety |
|---|---|---|
| status | /status, /agents | Read-only |
| alerts | /alerts, /alert | Setting an alert needs confirmation of symbol/price |
| take / skip | /take, /skip | **Typed confirmation required** for TAKE or any order-affecting action |
| mode | /mode | Admin + **typed confirmation** + position safety gate |
| restart | /restart | Admin + **typed confirmation** |
| Claude status | /claude, /claude-status | Read-only |

Transport considerations: Discord voice is not a practical command transport; a phone shortcut to authenticated webhook is simpler. Telegram voice note → speech-to-text → intent parser is an alternative.

Never execute an order, change mode, restart, or modify order-affecting state solely from speech recognition. Require explicit typed confirmation by an authenticated, authorized operator. Avoid exposing tokens in webhook URLs; use signed requests and replay protection.
