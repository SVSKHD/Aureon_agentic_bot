#!/usr/bin/env bash
# Linux twin of the Windows launcher's hang check (scripts/aureon_service.ps1): when the bot has
# been up longer than the start grace and logs/heartbeat.txt is older than HANG_SEC, restart it.
# Run as root by aureon-bot-watch.timer (it needs systemctl restart). Reads only file ages.
set -euo pipefail
BOT_DIR="${AUREON_BOT_DIR:-/srv/projects/Aureon_agentic_bot}"
UNIT="${AUREON_BOT_UNIT:-aureon-bot.service}"
HANG_SEC="${AUREON_HANG_SEC:-300}"
START_GRACE_SEC="${AUREON_START_GRACE_SEC:-300}"

systemctl is-active --quiet "$UNIT" || exit 0          # stopped on purpose, or systemd is restarting it
started="$(systemctl show "$UNIT" -p ActiveEnterTimestampMonotonic --value)"
now_us="$(awk '{printf "%d", $1 * 1000000}' /proc/uptime)"
up=$(( (now_us - started) / 1000000 ))
(( up > START_GRACE_SEC )) || exit 0

hb="$BOT_DIR/logs/heartbeat.txt"
if [[ -f "$hb" ]]; then age=$(( $(date +%s) - $(stat -c %Y "$hb") )); else age=999999; fi
if (( age > HANG_SEC )); then
  echo "HANG: heartbeat.txt is ${age} s old (up ${up} s) — restarting $UNIT"
  systemctl restart "$UNIT"
fi
