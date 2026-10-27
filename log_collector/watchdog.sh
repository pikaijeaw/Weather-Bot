#!/bin/bash
# Restart weatherbot if its process is gone (reboot, OOM, crash).
# Prints nothing when healthy, so the cron job only sends a message when it acted.
cd "$(dirname "$0")/.." || exit 1
PIDF=data/bot.pid
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null \
   && tr '\0' ' ' < "/proc/$(cat "$PIDF")/cmdline" | grep -q "weatherbot.py run"; then
  exit 0
fi
# respect a deliberate stop from the dashboard/stop.sh: they remove the pid file
[ -f data/.manual_stop ] && exit 0
./start.sh >/dev/null 2>&1
sleep 5
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  echo "♻️ weatherbot was not running — watchdog restarted it (PID $(cat $PIDF))."
else
  echo "🛑 weatherbot is down and the watchdog could not restart it. Check data/weatherbot.log."
fi
