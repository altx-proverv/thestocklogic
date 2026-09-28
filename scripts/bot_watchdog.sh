#!/bin/bash
# ATLAS bot watchdog — keeps bot_listener alive AND current.
#
# WHY THIS CHANGED. The old version was one line:
#
#     pgrep -f "atlas/reporting/bot_listener.py" > /dev/null && exit 0
#
# It restarted the listener only when it was ABSENT. The deploy is
# `git reset --hard origin/main` every five minutes, which rewrites files a
# long-running Python process has already imported -- so a healthy listener kept
# serving the code it loaded at start, for as long as it stayed healthy. Three of
# four stale operator-facing messages survived that way: the fix was committed,
# deployed, and never reached the process answering the operator. A deploy that
# cannot reach the running process is not a deploy.
#
# So the listener is also restarted when the DEPLOYED SHA CHANGES. The SHA it was
# started at is recorded next to its state; if HEAD has moved, it is replaced.
#
# SIGTERM, not SIGKILL: bot_listener traps it and leaves the loop cleanly. Its
# Telegram offset is persisted per update, so the restart cannot replay a
# directive -- that guarantee is what makes restarting safe at all, and this
# script must not be pointed at a listener that lacks it.
set -u

# ATLAS_REPO exists so this script is testable against a scratch repo;
# the box never sets it.
REPO="${ATLAS_REPO:-/home/ubuntu/thestocklogic}"
STATE_DIR="${ATLAS_STATE_DIR:-/var/lib/atlas}"
SHA_FILE="$STATE_DIR/bot_listener.sha"
PATTERN="atlas/reporting/bot_listener.py"

cd "$REPO" || exit 0

CUR="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
PID="$(pgrep -f "$PATTERN" | head -1)"

start() {
    mkdir -p "$STATE_DIR" 2>/dev/null
    nohup "${ATLAS_PYTHON:-$REPO/venv/bin/python3}" "$REPO/$PATTERN" >> "$REPO/reports/bot.log" 2>&1 &
    # Record the SHA only after a successful spawn, so a failed start is retried
    # on the next tick rather than being remembered as done.
    sleep 2
    if pgrep -f "$PATTERN" > /dev/null; then
        echo "$CUR" > "$SHA_FILE" 2>/dev/null
        echo "$(date +%Y-%m-%dT%H:%M:%S%z) started bot_listener at $CUR" >> "$REPO/reports/bot.log"
    else
        echo "$(date +%Y-%m-%dT%H:%M:%S%z) bot_listener FAILED to start at $CUR" >> "$REPO/reports/bot.log"
    fi
}

# Absent: start it.
if [ -z "$PID" ]; then
    start
    exit 0
fi

# Present: restart only if the deployed SHA has moved since it started.
WAS="$(cat "$SHA_FILE" 2>/dev/null || echo "")"
if [ "$CUR" != "unknown" ] && [ "$WAS" != "$CUR" ]; then
    echo "$(date +%Y-%m-%dT%H:%M:%S%z) deploy moved ${WAS:-<unknown>} -> $CUR, restarting bot_listener" \
        >> "$REPO/reports/bot.log"
    kill -TERM "$PID" 2>/dev/null
    # getUpdates long-polls for 30s, so allow the loop to notice and exit.
    for _ in $(seq 1 35); do
        pgrep -f "$PATTERN" > /dev/null || break
        sleep 1
    done
    pgrep -f "$PATTERN" > /dev/null && kill -KILL "$PID" 2>/dev/null
    start
fi
exit 0
