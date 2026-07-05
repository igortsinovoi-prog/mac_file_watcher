#!/bin/bash
# Installs dependencies into a local venv and registers the file watcher as a
# launchd LaunchAgent, so it starts automatically at login and is supervised
# (restarted if it crashes) for as long as the user is logged in.
#
# A LaunchAgent (not a LaunchDaemon) is used deliberately: LaunchDaemons run
# as root before login with no access to the user's GUI session, so a command
# like an AppleScript dialog popup would silently fail to display.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.codestation.filewatcher"
DEBOUNCE="0.5"
FILES=()
COMMAND=""

usage() {
  cat <<EOF
Usage: $0 -f FILE [-f FILE ...] -c COMMAND [--debounce SECONDS] [--label LABEL]

  -f, --file      A file to watch. Repeat for multiple files. Required.
  -c, --command   Shell command to run when a watched file changes. Required.
      --debounce  Seconds to collapse rapid duplicate events (default: $DEBOUNCE).
      --label     launchd job label / plist filename (default: $LABEL).

Example:
  $0 -f ~/notes/todo.txt -c 'osascript -e "display dialog \\"patch\\""'
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--file) FILES+=("$2"); shift 2 ;;
    -c|--command) COMMAND="$2"; shift 2 ;;
    --debounce) DEBOUNCE="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

if [[ ${#FILES[@]} -eq 0 || -z "$COMMAND" ]]; then
  echo "Error: at least one --file and a --command are required." >&2
  usage
fi

echo "==> Setting up Python virtual environment"
if [[ ! -d "$DIR/.venv" ]]; then
  python3 -m venv "$DIR/.venv"
fi
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

LOG_DIR="$DIR/logs"
mkdir -p "$LOG_DIR"
mkdir -p "$HOME/Library/LaunchAgents"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "==> Generating launchd plist at $PLIST_PATH"
FILE_ARGS=()
for f in "${FILES[@]}"; do
  FILE_ARGS+=(--file "$f")
done

"$DIR/.venv/bin/python" "$DIR/generate_plist.py" \
  "${FILE_ARGS[@]}" \
  --command "$COMMAND" \
  --debounce "$DEBOUNCE" \
  --label "$LABEL" \
  --python "$DIR/.venv/bin/python" \
  --daemon-script "$DIR/daemon_watcher.py" \
  --log-dir "$LOG_DIR" \
  --output "$PLIST_PATH"

echo "==> Registering with launchd"
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load -w "$PLIST_PATH"

cat <<EOF

Installed and started.
  Watching: ${FILES[*]}
  Command:  $COMMAND
  Label:    $LABEL
  Plist:    $PLIST_PATH
  Logs:     $LOG_DIR

The daemon will now also start automatically at every login.
To remove it, run: ./uninstall.sh --label $LABEL
EOF
