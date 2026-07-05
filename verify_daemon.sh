#!/bin/bash
# Manual smoke test: runs the daemon against a real file and confirms the
# command fires on change. Not part of the pytest suite.
set -euo pipefail

WATCH_FILE="/tmp/watch_target.txt"
OUT_FILE="/tmp/watch_out.txt"
LOG_FILE="/tmp/watch_daemon.log"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

rm -f "$WATCH_FILE" "$OUT_FILE" "$LOG_FILE"
touch "$WATCH_FILE"

"$DIR/.venv/bin/python" "$DIR/daemon_watcher.py" \
  -f "$WATCH_FILE" \
  -c "echo triggered >> $OUT_FILE" > "$LOG_FILE" 2>&1 &
WATCHER_PID=$!

cleanup() {
  kill "$WATCHER_PID" 2>/dev/null || true
  wait "$WATCHER_PID" 2>/dev/null || true
  rm -f "$WATCH_FILE" "$OUT_FILE" "$LOG_FILE"
}
trap cleanup EXIT

sleep 1
echo "change 1" >> "$WATCH_FILE"
sleep 1
echo "change 2" >> "$WATCH_FILE"
sleep 1

echo "--- output file contents ---"
cat "$OUT_FILE" 2>/dev/null || echo "(no output file created)"
echo "--- daemon log ---"
cat /tmp/watch_daemon.log 2>/dev/null || echo "(no log)"
