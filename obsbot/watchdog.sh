#!/bin/bash
# Restart obsbot if it died (reboot/OOM/crash). Silent when healthy or deliberately stopped.
cd "$(dirname "$0")/.." || exit 1
PIDF=obsbot/data/bot.pid
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null \
   && tr '\0' ' ' < "/proc/$(cat "$PIDF")/cmdline" | grep -q "obsbot.py run"; then exit 0; fi
[ -f obsbot/data/.manual_stop ] && exit 0
mkdir -p obsbot/data
nohup venv/bin/python obsbot/obsbot.py run >/dev/null 2>&1 &
echo $! > "$PIDF"
sleep 5
if kill -0 "$(cat "$PIDF")" 2>/dev/null; then echo "♻️ obsbot was not running — watchdog restarted it (PID $(cat $PIDF))."
else echo "🛑 obsbot is down and could not be restarted. Check obsbot/data/obsbot.log."; fi
