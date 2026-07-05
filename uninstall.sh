#!/bin/bash
# Unregisters the file watcher LaunchAgent installed by install.sh.
#
# Mirrors install.sh's root-awareness: if it was installed via sudo (plist in
# /Library/LaunchAgents, bootstrapped into a specific user's gui/<uid>
# session), removing it also needs sudo and the same --target-user.
set -euo pipefail

LABEL="com.codestation.filewatcher"
TARGET_USER=""

usage() {
  cat <<EOF
Usage: $0 [--label LABEL] [--target-user USER]

  --label        launchd job label / plist filename (default: $LABEL).
  --target-user  When run via sudo, which user's session it was installed
                 for (default: \$SUDO_USER). Ignored when not run as root.
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label) LABEL="$2"; shift 2 ;;
    --target-user) TARGET_USER="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

RUNNING_AS_ROOT=false
if [[ "$(id -u)" -eq 0 ]]; then
  RUNNING_AS_ROOT=true
  TARGET_USER="${TARGET_USER:-${SUDO_USER:-}}"
  if [[ -z "$TARGET_USER" || "$TARGET_USER" == "root" ]]; then
    echo "Error: running as root but no target user to uninstall for." >&2
    echo "Pass --target-user USER, or run via 'sudo' (so \$SUDO_USER is set)." >&2
    exit 1
  fi
  PLIST_PATH="/Library/LaunchAgents/$LABEL.plist"
  TARGET_UID="$(id -u "$TARGET_USER")"
else
  PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
  TARGET_UID="$(id -u)"
fi

DOMAIN="gui/$TARGET_UID"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true

if [[ -f "$PLIST_PATH" ]]; then
  rm -f "$PLIST_PATH"
  echo "Unregistered and removed $PLIST_PATH"
else
  echo "No plist found at $PLIST_PATH (nothing to do)"
fi
