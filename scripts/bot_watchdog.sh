#!/bin/bash
# Bot watchdog — cron passes crontab header env vars to this script
pgrep -f "atlas/reporting/bot_listener.py" > /dev/null && exit 0
cd /home/ubuntu/thestocklogic
nohup ./venv/bin/python3 atlas/reporting/bot_listener.py >> reports/bot.log 2>&1 &
