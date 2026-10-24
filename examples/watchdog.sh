#!/bin/sh
# Minimal health check: is the brain still syncing, is the service alive?
# Alerts through any webhook that accepts {"text": "..."} (Slack, Discord,
# Feishu/Lark custom bot, ...). Set WEBHOOK=... in the environment.
set -u
HERMES="$HOME/.hermes"
OK="$HERMES/brain/.state/last-ok"
STALE="$(( 5400 ))"          # 90 minutes
problems=""

if [ -f "$OK" ]; then
  age=$(( $(date +%s) - $(stat -c %Y "$OK" 2>/dev/null || stat -f %m "$OK") ))
  [ "$age" -gt "$STALE" ] && problems="$problems
  brain has not synced for $((age/60)) min"
else
  problems="$problems
  brain never recorded a successful sync"
fi

if python3 "$HERMES/brain/sync/chain.py" verify >/dev/null 2>&1; then :; else
  problems="$problems
  fact-log history was rewritten or truncated"
fi

[ -z "$problems" ] && exit 0
[ -z "${WEBHOOK:-}" ] && { echo "$problems"; exit 1; }
python3 - "$WEBHOOK" "$problems" <<'PY'
import json, sys, urllib.request
hook, msg = sys.argv[1], sys.argv[2]
req = urllib.request.Request(hook, data=json.dumps({"text": "shared-brain: " + msg}).encode(),
                             headers={"Content-Type": "application/json"})
urllib.request.urlopen(req, timeout=15).read()
PY
