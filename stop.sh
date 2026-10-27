#!/bin/bash
cd "$(dirname "$0")"
PIDF=data/bot.pid
touch data/.manual_stop   # tells the watchdog not to restart it
if [ -f $PIDF ] && kill "$(cat $PIDF)" 2>/dev/null; then echo "stopped"; else echo "not running"; fi
rm -f $PIDF
