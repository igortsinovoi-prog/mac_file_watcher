#!/bin/bash
# Unregisters the file watcher LaunchDaemon installed by install.sh.
#
# Must be run as root (via sudo), like install.sh. Removing the plist from
# /Library/LaunchDaemons is the authoritative fix -- it stops launchd from
# loading this at any future boot. As a best-effort nicety, this also stops
# it running right now.
set -euo pipefail

RUNTIME_DIR="/Library/Application Support/Glow"
LABEL="com.codestation.filewatcher"
PURGE=false

usage() {
  cat <<EOF
Usage: sudo $0 [--label LABEL] [--purge]

  --label   launchd job label / plist filename (default: $LABEL).
  --purge   Also remove the runtime scripts install.sh copied into
            $RUNTIME_DIR (daemon_watcher.py, file_watcher.py,
            remove-browser-extension.js).
            WARNING: that runtime is shared by every installed label --
            only pass this if you're removing the last one.
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label) LABEL="$2"; shift 2 ;;
    --purge) PURGE=true; shift ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Error: run this with sudo." >&2
  echo "It needs root to remove /Library/LaunchDaemons/$LABEL.plist." >&2
  exit 1
fi

PLIST_PATH="/Library/LaunchDaemons/$LABEL.plist"

launchctl bootout "system/$LABEL" 2>/dev/null || true

if [[ -f "$PLIST_PATH" ]]; then
  rm -f "$PLIST_PATH"
  echo "Unregistered and removed $PLIST_PATH"
else
  echo "No plist found at $PLIST_PATH (nothing to do)"
fi

if [[ "$PURGE" == true ]]; then
  rm -f "$RUNTIME_DIR/daemon_watcher.py" "$RUNTIME_DIR/file_watcher.py" "$RUNTIME_DIR/remove-browser-extension.js"
  echo "Purged runtime scripts from $RUNTIME_DIR"
fi
