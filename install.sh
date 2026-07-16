#!/bin/bash
# Installs the watcher runtime and registers it as a launchd LaunchDaemon, so
# it runs as root starting at boot (before any login) and is supervised
# (restarted if it crashes) for as long as the machine is up.
#
# NOTE: a LaunchDaemon has no GUI session at all. If --command needs to talk
# to a running GUI app (e.g. osascript sending Apple Events, or anything
# gated by Automation/Accessibility permissions), it may behave differently
# here than it did as a LaunchAgent -- there is no logged-in user, no
# Aqua session, nothing on screen. Root-level filesystem/process work is
# unaffected.
#
# Must be run as root (via sudo). Everything it touches is a system-wide
# location -- /Library/Application Support/Glow for the runtime scripts,
# /Library/Logs/Glow for output, /Library/LaunchDaemons for the plist -- and
# dependencies are installed system-wide too.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="/Library/Application Support/Glow"
LOG_DIR="/Library/Logs/Glow"
PLIST_DIR="/Library/LaunchDaemons"
LABEL="com.codestation.filewatcher"
DEBOUNCE="0.1"
FILES=()
COMMAND=""

usage() {
  cat <<EOF
Usage: sudo $0 -f FILE [-f FILE ...] -c COMMAND [--debounce SECONDS] [--label LABEL]

  -f, --file      A file to watch. Repeat for multiple times. Required.
  -c, --command   Shell command to run when a watched file changes. Required.
      --debounce  Seconds to collapse rapid duplicate events (default: $DEBOUNCE).
      --label     launchd job label / plist filename (default: $LABEL).

Example:
  sudo $0 -f ~/notes/todo.txt -c 'osascript -e "display dialog \\"patch\\""'
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

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Error: run this with sudo." >&2
  echo "It needs root to write to $RUNTIME_DIR, $LOG_DIR, and $PLIST_DIR." >&2
  exit 1
fi

for f in "$DIR/daemon_watcher.py" "$DIR/file_watcher.py" "$DIR/generate_plist.py" "$DIR/requirements.txt" "$DIR/remove-browser-extension.js"; do
  if [[ ! -f "$f" ]]; then
    echo "Error: expected to find $f next to this script. Run build.sh first?" >&2
    exit 1
  fi
done

PYTHON_BIN="$(command -v python3 || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  echo "Error: python3 not found on PATH." >&2
  exit 1
fi

echo "==> Installing dependencies for $PYTHON_BIN (system-wide, no virtual environment)"
# Without --user, pip still installs to a *user* site-packages directory (not
# the true global one) when the target global directory isn't writable --
# and it resolves "user" via $HOME. sudo on macOS does NOT reset $HOME by
# default, so a plain "sudo ./install.sh" leaves $HOME as the invoking admin's
# home, and pip installs there -- e.g. /Users/abuyam/Library/Python/...
# The daemon then runs as root (this is a LaunchDaemon), with no $HOME set,
# so it resolves root's own home (/var/root) and looks in
# /var/root/Library/Python/... instead, finding nothing: ModuleNotFoundError.
# Forcing HOME=/var/root here makes pip's resolution match root's real home,
# consistent with how the daemon (also HOME-less, falling back to the same
# real home via the password database) will look for it.
PIP_ERR="$(mktemp)"
trap 'rm -f "$PIP_ERR"' EXIT
if ! HOME=/var/root "$PYTHON_BIN" -m pip install -q -r "$DIR/requirements.txt" 2>"$PIP_ERR"; then
  if grep -q "externally-managed-environment" "$PIP_ERR"; then
    echo "    (Homebrew Python blocks system-wide pip installs by default;"
    echo "     retrying with --break-system-packages.)"
    HOME=/var/root "$PYTHON_BIN" -m pip install -q --break-system-packages -r "$DIR/requirements.txt"
  else
    cat "$PIP_ERR" >&2
    exit 1
  fi
fi

echo "==> Deploying watcher runtime to $RUNTIME_DIR"
mkdir -p "$RUNTIME_DIR"
cp "$DIR/daemon_watcher.py" "$DIR/file_watcher.py" "$DIR/remove-browser-extension.js" "$RUNTIME_DIR/"
chmod 755 "$RUNTIME_DIR"
chmod 644 "$RUNTIME_DIR/daemon_watcher.py" "$RUNTIME_DIR/file_watcher.py" "$RUNTIME_DIR/remove-browser-extension.js"

# Unlike a gui/<uid> LaunchAgent (which opens StandardOutPath/StandardErrorPath
# as the target user), a LaunchDaemon always runs -- and opens its log files
# -- as root, so no special permissions are needed here.
mkdir -p "$LOG_DIR"
mkdir -p "$PLIST_DIR"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"

echo "==> Generating launchd plist at $PLIST_PATH"
FILE_ARGS=()
for f in "${FILES[@]}"; do
  FILE_ARGS+=(--file "$f")
done

"$PYTHON_BIN" "$DIR/generate_plist.py" \
  "${FILE_ARGS[@]}" \
  --command "$COMMAND" \
  --debounce "$DEBOUNCE" \
  --label "$LABEL" \
  --python "$PYTHON_BIN" \
  --daemon-script "$RUNTIME_DIR/daemon_watcher.py" \
  --log-dir "$LOG_DIR" \
  --output "$PLIST_PATH"
chmod 644 "$PLIST_PATH"

echo "==> Registering with launchd"
launchctl bootout "system/$LABEL" 2>/dev/null || true
# bootout can return before the job is fully deregistered; an immediate
# bootstrap can then race it ("Bootstrap failed: 37: Operation already in
# progress"). Wait for it to actually be gone first.
for _ in 1 2 3 4 5; do
  launchctl print "system/$LABEL" >/dev/null 2>&1 || break
  sleep 1
done
launchctl bootstrap system "$PLIST_PATH"
launchctl enable "system/$LABEL"

cat <<EOF

Installed and started.
  Watching: ${FILES[*]}
  Command:  $COMMAND
  Label:    $LABEL
  Python:   $PYTHON_BIN
  Runtime:  $RUNTIME_DIR
  Plist:    $PLIST_PATH
  Logs:     $LOG_DIR/$LABEL.out.log
            $LOG_DIR/$LABEL.err.log

Runs as root, starting at every boot from now on (no login required).
To remove it, run: sudo ./uninstall.sh --label $LABEL
EOF
