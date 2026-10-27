#!/bin/bash
cd "$(dirname "$0")"
PIDF=data/bot.pid
rm -f data/.manual_stop
if [ -f $PIDF ] && kill -0 "$(cat $PIDF)" 2>/dev/null; then echo "already running (PID $(cat $PIDF))"; exit 1; fi
nohup venv/bin/python weatherbot.py run >/dev/null 2>&1 &
echo $! > $PIDF
echo "started PID $! — log: data/weatherbot.log"
