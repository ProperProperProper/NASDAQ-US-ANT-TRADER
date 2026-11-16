#!/bin/zsh
set -u

OPEN_D_HOST="127.0.0.1"
OPEN_D_PORT="11111"
BOT_LABEL="com.local.nasdaq-paper-bot"

if ! /usr/bin/nc -z -G 2 "$OPEN_D_HOST" "$OPEN_D_PORT" >/dev/null 2>&1; then
  print -u2 "Moomoo OpenD is not reachable on ${OPEN_D_HOST}:${OPEN_D_PORT}"
  exit 78
fi

if ! /bin/launchctl print "gui/$(/usr/bin/id -u)/${BOT_LABEL}" >/dev/null 2>&1; then
  print -u2 "${BOT_LABEL} is not loaded"
  exit 78
fi

if ! /bin/launchctl print "gui/$(/usr/bin/id -u)/${BOT_LABEL}" 2>/dev/null | /usr/bin/grep -q 'state = running'; then
  print -u2 "${BOT_LABEL} is loaded but not running"
  exit 78
fi

if ! /usr/bin/curl --fail --silent --max-time 3 http://127.0.0.1:8780/api/health >/dev/null; then
  print -u2 "trading dashboard health check failed"
  exit 78
fi

if ! /usr/bin/curl --fail --silent --max-time 3 http://127.0.0.1:8780/api/status \
  | /usr/bin/python3 -c 'import json,sys; value=json.load(sys.stdin); raise SystemExit(0 if value.get("active") and value.get("mode") == "SIMULATE" else 1)'; then
  print -u2 "paper bot status is not active SIMULATE"
  exit 78
fi

exit 0
