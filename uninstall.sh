#!/bin/bash
# Unregisters the file watcher LaunchAgent installed by install.sh.
set -euo pipefail

LABEL="com.codestation.filewatcher"

usage() {
  echo "Usage: $0 [--label LABEL]"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label) LABEL="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ -f "$PLIST_PATH" ]]; then
  launchctl unload "$PLIST_PATH" 2>/dev/null || true
  rm -f "$PLIST_PATH"
  echo "Unregistered and removed $PLIST_PATH"
else
  echo "No plist found at $PLIST_PATH (nothing to do)"
fi
