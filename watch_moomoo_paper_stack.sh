#!/bin/zsh
set -u

OPEN_D_HOST="127.0.0.1"
OPEN_D_PORT="11111"
BOT_LABEL="com.local.nasdaq-paper-bot"
REQUIRED_ACCOUNT_ID="COMPETITION_ACCOUNT_ID"

if ! /usr/bin/nc -z -G 2 "$OPEN_D_HOST" "$OPEN_D_PORT" >/dev/null 2>&1; then
  print -u2 "Moomoo OpenD is not reachable on ${OPEN_D_HOST}:${OPEN_D_PORT}"
  exit 78
fi

if ! /usr/bin/nc -z -G 2 127.0.0.1 8788 >/dev/null 2>&1; then
  print -u2 "bundled AI gateway is not reachable on 127.0.0.1:8788"
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

PLIST="$HOME/Library/LaunchAgents/${BOT_LABEL}.plist"
if ! /usr/bin/plutil -extract ProgramArguments xml1 -o - "$PLIST" 2>/dev/null \
  | /usr/bin/grep -q "${REQUIRED_ACCOUNT_ID}"; then
  print -u2 "paper bot is not pinned to SIMULATE account ${REQUIRED_ACCOUNT_ID}"
  exit 78
fi

if ! /usr/bin/plutil -extract ProgramArguments xml1 -o - "$PLIST" 2>/dev/null \
  | /usr/bin/grep -q -- "--live-paper"; then
  print -u2 "paper bot is not configured for paper execution"
  exit 78
fi

exit 0
