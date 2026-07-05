#!/bin/bash
# Installs dependencies into the local (system/user) Python environment and
# registers the file watcher as a launchd LaunchAgent, so it starts
# automatically at login and is supervised (restarted if it crashes) for as
# long as the target user is logged in.
#
# A LaunchAgent (not a LaunchDaemon) is used deliberately: LaunchDaemons run
# as root before login with no access to the user's GUI session, so a command
# like an AppleScript dialog popup would silently fail to display.
#
# Can be run either as a normal user (installs for yourself) or via sudo
# (installs on behalf of $SUDO_USER, or --target-user). Either way, the
# daemon itself always ends up running as the target *user*, never as root:
# launchd's "gui/<uid>" domain runs jobs as that uid regardless of who
# bootstrapped them, so dependencies are installed for that user too.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.codestation.filewatcher"
DEBOUNCE="0.5"
FILES=()
COMMAND=""
TARGET_USER=""

usage() {
  cat <<EOF
Usage: $0 -f FILE [-f FILE ...] -c COMMAND [--debounce SECONDS] [--label LABEL] [--target-user USER]

  -f, --file         A file to watch. Repeat for multiple files. Required.
  -c, --command      Shell command to run when a watched file changes. Required.
      --debounce     Seconds to collapse rapid duplicate events (default: $DEBOUNCE).
      --label        launchd job label / plist filename (default: $LABEL).
      --target-user  When run via sudo, which user's session to install for
                      (default: \$SUDO_USER). Ignored when not run as root.

Example:
  $0 -f ~/notes/todo.txt -c 'osascript -e "display dialog \\"patch\\""'

Can be run with sudo to install on behalf of another logged-in user; the
plist then lives in /Library/LaunchAgents (root-owned, as launchd requires)
and is bootstrapped into that user's GUI session specifically.
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--file) FILES+=("$2"); shift 2 ;;
    -c|--command) COMMAND="$2"; shift 2 ;;
    --debounce) DEBOUNCE="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --target-user) TARGET_USER="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

if [[ ${#FILES[@]} -eq 0 || -z "$COMMAND" ]]; then
  echo "Error: at least one --file and a --command are required." >&2
  usage
fi

RUNNING_AS_ROOT=false
if [[ "$(id -u)" -eq 0 ]]; then
  RUNNING_AS_ROOT=true
  TARGET_USER="${TARGET_USER:-${SUDO_USER:-}}"
  if [[ -z "$TARGET_USER" || "$TARGET_USER" == "root" ]]; then
    echo "Error: running as root but no target user to install for." >&2
    echo "Pass --target-user USER, or run via 'sudo' (so \$SUDO_USER is set)." >&2
    exit 1
  fi
fi

run_as_target() {
  if [[ "$RUNNING_AS_ROOT" == true ]]; then
    sudo -u "$TARGET_USER" -H "$@"
  else
    "$@"
  fi
}

if [[ "$RUNNING_AS_ROOT" == true ]]; then
  TARGET_SHELL="$(dscl . -read "/Users/$TARGET_USER" UserShell | awk '{print $2}')"
  PYTHON_BIN="$(sudo -u "$TARGET_USER" -H "${TARGET_SHELL:-/bin/zsh}" -lc 'command -v python3' || true)"
  TARGET_UID="$(id -u "$TARGET_USER")"
  PLIST_DIR="/Library/LaunchAgents"
  TARGET_HOME="$(dscl . -read "/Users/$TARGET_USER" NFSHomeDirectory | awk '{print $2}')"
  LOG_DIR="$TARGET_HOME/Library/Logs/mac_file_watcher"
else
  PYTHON_BIN="$(command -v python3 || true)"
  TARGET_UID="$(id -u)"
  PLIST_DIR="$HOME/Library/LaunchAgents"
  LOG_DIR="$HOME/Library/Logs/mac_file_watcher"
fi

if [[ -z "$PYTHON_BIN" ]]; then
  echo "Error: python3 not found on PATH for ${TARGET_USER:-$(whoami)}." >&2
  exit 1
fi

echo "==> Installing dependencies for $PYTHON_BIN (user: ${TARGET_USER:-$(whoami)}, no virtual environment)"
PIP_ERR="$(mktemp)"
trap 'rm -f "$PIP_ERR"' EXIT
if ! run_as_target "$PYTHON_BIN" -m pip install -q --user -r "$DIR/requirements.txt" 2>"$PIP_ERR"; then
  if grep -q "externally-managed-environment" "$PIP_ERR"; then
    echo "    (Homebrew Python blocks system-wide pip installs by default;"
    echo "     retrying with --break-system-packages, scoped to --user so it"
    echo "     only touches the user's site-packages, not Homebrew's files.)"
    run_as_target "$PYTHON_BIN" -m pip install -q --user --break-system-packages -r "$DIR/requirements.txt"
  else
    cat "$PIP_ERR" >&2
    exit 1
  fi
fi

mkdir -p "$LOG_DIR"
if [[ "$RUNNING_AS_ROOT" == true ]]; then
  chown "$TARGET_USER" "$LOG_DIR"
fi
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
  --daemon-script "$DIR/daemon_watcher.py" \
  --log-dir "$LOG_DIR" \
  --output "$PLIST_PATH"
chmod 644 "$PLIST_PATH"

echo "==> Registering with launchd"
DOMAIN="gui/$TARGET_UID"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST_PATH"
launchctl enable "$DOMAIN/$LABEL"

cat <<EOF

Installed and started.
  Watching:    ${FILES[*]}
  Command:     $COMMAND
  Label:       $LABEL
  Target user: ${TARGET_USER:-$(whoami)}
  Python:      $PYTHON_BIN
  Plist:       $PLIST_PATH
  Logs:        $LOG_DIR/$LABEL.out.log
               $LOG_DIR/$LABEL.err.log

The daemon will now also start automatically at every login.
To remove it, run: ./uninstall.sh --label $LABEL${TARGET_USER:+ --target-user $TARGET_USER}
EOF
