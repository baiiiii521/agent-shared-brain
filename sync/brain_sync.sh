#!/bin/sh
# One full pass of the shared brain. Put this in cron / launchd.
#   1. pull            bring remote changes down
#   2. chain verify    judge them BEFORE they can touch memory
#   3. mailbox.py      messages / journal / hand-off plumbing
#   4. facts.py        three-way merge + render
#   5. chain update    advance the append-only anchors
# Only a fully successful pass writes .state/last-ok; point your watchdog at it.
set -u
BRAIN="${HERMES_HOME:-$HOME/.hermes}/brain"
LOG="$BRAIN/logs/sync.log"
FAILED="$BRAIN/.state/CHAIN-ALERT"
PY="${PYTHON:-python3}"
mkdir -p "$BRAIN/logs" "$BRAIN/.state"
cd "$BRAIN" || exit 1

git pull -q --rebase --autostash >/dev/null 2>&1 || true

if ! "$PY" "$BRAIN/sync/chain.py" verify >> "$LOG" 2>&1; then
  { date '+%F %T'; "$PY" "$BRAIN/sync/chain.py" verify; } >> "$FAILED" 2>&1
  echo "$(date '+%F %T') CHAIN TAMPERED — memory untouched, see $FAILED" >> "$LOG"
  exit 6
fi
rm -f "$FAILED"

fail=""
"$PY" "$BRAIN/sync/mailbox.py" inbox --unread >> "$LOG" 2>&1 || true
"$PY" "$BRAIN/sync/facts.py" sync          >> "$LOG" 2>&1 || fail="$fail facts"
"$PY" "$BRAIN/sync/chain.py" update        >> "$LOG" 2>&1

if [ -z "$fail" ]; then
  touch "$BRAIN/.state/last-ok"
else
  echo "$(date '+%F %T') SYNC FAILED:$fail" >> "$LOG"
fi
tail -n 800 "$LOG" > "$LOG.t" 2>/dev/null && mv "$LOG.t" "$LOG"
[ -z "$fail" ]
