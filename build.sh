#!/bin/bash
# Builds a "prod" deployable folder containing only what's needed to install
# and run the watcher: install.sh, uninstall.sh, and the runtime script
# files. Nothing dev-only (tests, README, etc.) is included.
#
# install.sh (run from prod/) copies the runtime scripts onward to
# /Library/Application Support/Glow at install time, so the running daemon
# is decoupled from wherever this source checkout happens to live.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROD_DIR="$DIR/prod"

if [[ -e "$PROD_DIR" ]]; then
  echo "Removing existing $PROD_DIR"
  rm -rf "$PROD_DIR"
fi

mkdir -p "$PROD_DIR"

cp "$DIR/install.sh" "$PROD_DIR/"
cp "$DIR/uninstall.sh" "$PROD_DIR/"
cp "$DIR/daemon_watcher.py" "$PROD_DIR/"
cp "$DIR/file_watcher.py" "$PROD_DIR/"
cp "$DIR/generate_plist.py" "$PROD_DIR/"
cp "$DIR/requirements.txt" "$PROD_DIR/"
cp "$DIR/js_scripts/remove-browser-extension.js" "$PROD_DIR/"
chmod +x "$PROD_DIR/install.sh" "$PROD_DIR/uninstall.sh"

echo "Built $PROD_DIR:"
ls -la "$PROD_DIR"
