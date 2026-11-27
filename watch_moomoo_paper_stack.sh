#!/bin/zsh
set -u

OPEN_D_HOST="127.0.0.1"
OPEN_D_PORT="11111"
BOT_LABEL="com.local.us-ant-engine"
ENGINE="/Users/local.local/Projects/TradingSystem/ai_gateway/target/release/trading_engine"
PLIST="$HOME/Library/LaunchAgents/${BOT_LABEL}.plist"
REQUIRED_ACCOUNT_ID="${US_ANT_PAPER_ACCOUNT_ID:-$(/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:US_ANT_PAPER_ACCOUNT_ID' "$PLIST" 2>/dev/null)}"

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

if [[ -z "$REQUIRED_ACCOUNT_ID" ]] || ! /usr/bin/plutil -extract EnvironmentVariables xml1 -o - "$PLIST" 2>/dev/null \
  | /usr/bin/grep -q -- "${REQUIRED_ACCOUNT_ID}"; then
  print -u2 "paper bot is not pinned to the locally configured standard SIMULATE account"
  exit 78
fi

if ! "$ENGINE" status 2>/dev/null | /usr/bin/grep -q 'paper=enabled pid=[1-9]'; then
  print -u2 "paper bot worker is not running"
  exit 78
fi

exit 0
