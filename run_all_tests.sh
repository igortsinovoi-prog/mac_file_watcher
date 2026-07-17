#!/bin/bash
# Runs the full pytest unit-test suite (with coverage) followed by the manual
# smoke tests, so a single command verifies both "the logic is correct" and
# "it actually works against a real file / real FSEvents".
#
# Pass --with-install-test to also exercise prod/install.sh/uninstall.sh end
# to end (registers a throwaway launchd job under a distinct "selftest"
# label, confirms it fires on a real file change, then unregisters it). This
# requires prod/ to already exist (run build.sh first) and requires sudo, so
# it's opt-in and prompts for a password interactively.
#
# Pass --with-js-diagnostics-test to also run
# js_scripts/tests/real_world_check_remove_browser_extension_diagnostics.sh,
# a read-only real-world check of remove-browser-extension.js's
# _runCommand-dependent diagnostic functions on this machine. Nothing it does
# mutates system state, so unlike --with-install-test it needs no sudo and no
# cleanup.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PYTHON_BIN="$(command -v python3)"

WITH_INSTALL_TEST=false
WITH_JS_DIAGNOSTICS_TEST=false
for arg in "$@"; do
  case "$arg" in
    --with-install-test) WITH_INSTALL_TEST=true ;;
    --with-js-diagnostics-test) WITH_JS_DIAGNOSTICS_TEST=true ;;
    -h|--help)
      echo "Usage: $0 [--with-install-test] [--with-js-diagnostics-test]"
      exit 1
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 1
      ;;
  esac
done

run_install_uninstall_test() {
  echo
  echo "=== Smoke test: prod/install.sh / prod/uninstall.sh round trip (sudo) ==="

  if [[ ! -d "$DIR/prod" ]]; then
    echo "Error: $DIR/prod does not exist. Run ./build.sh first." >&2
    return 1
  fi

  local test_label="com.codestation.filewatcher.selftest"
  local test_watch_file="/tmp/installer_selftest_watch.txt"
  local test_out_file="/tmp/installer_selftest_out.txt"
  local plist_path="/Library/LaunchDaemons/$test_label.plist"
  local domain="system/$test_label"

  cleanup_install_test() {
    sudo launchctl bootout "$domain" 2>/dev/null || true
    sudo rm -f "$plist_path"
    # test_out_file doesn't exist until the root-run daemon command creates it
    # via "echo >> ...", so it's root-owned - needs sudo to remove from /tmp's
    # sticky-bit directory.
    rm -f "$test_watch_file"
    sudo rm -f "$test_out_file"
  }
  trap cleanup_install_test RETURN

  rm -f "$test_watch_file"
  sudo rm -f "$test_out_file"
  touch "$test_watch_file"

  sudo "$DIR/prod/install.sh" \
    -f "$test_watch_file" \
    -c "echo triggered >> $test_out_file" \
    --label "$test_label"

  if [[ ! -f "$plist_path" ]]; then
    echo "FAILED: plist was not created at $plist_path" >&2
    return 1
  fi
  if ! sudo launchctl print "$domain" >/dev/null 2>&1; then
    echo "FAILED: launchd job $test_label is not registered in $domain" >&2
    return 1
  fi
  echo "OK: plist created and job registered with launchd."

  sleep 1
  echo "change" >> "$test_watch_file"

  local waited=0
  while [[ ! -f "$test_out_file" && "$waited" -lt 10 ]]; do
    sleep 1
    waited=$((waited + 1))
  done

  if [[ ! -f "$test_out_file" ]]; then
    echo "FAILED: daemon did not run the command after a file change (waited ${waited}s)" >&2
    echo "--- launchctl print $domain ---" >&2
    sudo launchctl print "$domain" >&2 2>&1 || true
    echo "--- /Library/Logs/Glow/$test_label.out.log ---" >&2
    sudo cat "/Library/Logs/Glow/$test_label.out.log" >&2 2>&1 || true
    echo "--- /Library/Logs/Glow/$test_label.err.log ---" >&2
    sudo cat "/Library/Logs/Glow/$test_label.err.log" >&2 2>&1 || true
    return 1
  fi
  echo "OK: daemon ran the command after a file change (waited ${waited}s)."

  sudo "$DIR/prod/uninstall.sh" --label "$test_label"

  if [[ -f "$plist_path" ]]; then
    echo "FAILED: plist still exists after uninstall" >&2
    return 1
  fi

  local unload_waited=0
  while sudo launchctl print "$domain" >/dev/null 2>&1 && [[ "$unload_waited" -lt 5 ]]; do
    sleep 1
    unload_waited=$((unload_waited + 1))
  done
  if sudo launchctl print "$domain" >/dev/null 2>&1; then
    echo "FAILED: launchd job $test_label still registered after uninstall (waited ${unload_waited}s)" >&2
    return 1
  fi
  echo "OK: uninstall removed the plist and unregistered the job (waited ${unload_waited}s)."
}

echo "=== Unit tests (pytest, with coverage) ==="
"$PYTHON_BIN" -m pytest \
  --cov=file_watcher --cov=daemon_watcher --cov=generate_plist \
  --cov-report=term-missing

echo
echo "=== Smoke test: bare watchdog/FSEvents sanity check ==="
"$PYTHON_BIN" sanity_tests/bare_watchdog_check.py

echo
echo "=== Smoke test: full daemon against a real file ==="
"$DIR/verify_daemon.sh"

echo
echo "=== Smoke test: self-trigger loop check ==="
"$PYTHON_BIN" sanity_tests/self_trigger_loop_check.py

echo
echo "=== Smoke test: multi-path debounce check ==="
"$PYTHON_BIN" sanity_tests/multi_path_debounce_check.py

echo
echo "=== Smoke test: run-on-start check ==="
"$PYTHON_BIN" sanity_tests/run_on_start_check.py

if [[ "$WITH_JS_DIAGNOSTICS_TEST" == true ]]; then
  echo
  echo "=== Real-world check: remove-browser-extension.js diagnostics ==="
  "$DIR/js_scripts/tests/real_world_check_remove_browser_extension_diagnostics.sh"
fi

if [[ "$WITH_INSTALL_TEST" == true ]]; then
  run_install_uninstall_test
fi

echo
echo "All unit tests and smoke tests passed."
