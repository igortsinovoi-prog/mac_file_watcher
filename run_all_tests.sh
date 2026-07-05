#!/bin/bash
# Runs the full pytest unit-test suite (with coverage) followed by the manual
# smoke tests, so a single command verifies both "the logic is correct" and
# "it actually works against a real file / real FSEvents".
#
# Pass --with-install-test to also exercise install.sh/uninstall.sh end to
# end (registers a throwaway launchd job under a distinct "selftest" label,
# confirms it fires on a real file change, then unregisters it). This is
# opt-in because it touches real launchd state, unlike the other smoke tests.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

WITH_INSTALL_TEST=false
for arg in "$@"; do
  case "$arg" in
    --with-install-test) WITH_INSTALL_TEST=true ;;
    -h|--help)
      echo "Usage: $0 [--with-install-test]"
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
  echo "=== Smoke test: install.sh / uninstall.sh round trip ==="

  local test_label="com.codestation.filewatcher.selftest"
  local test_watch_file="/tmp/installer_selftest_watch.txt"
  local test_out_file="/tmp/installer_selftest_out.txt"
  local plist_path="$HOME/Library/LaunchAgents/$test_label.plist"

  cleanup_install_test() {
    launchctl unload "$plist_path" 2>/dev/null || true
    rm -f "$plist_path" "$test_watch_file" "$test_out_file"
  }
  trap cleanup_install_test RETURN

  rm -f "$test_watch_file" "$test_out_file"
  touch "$test_watch_file"

  "$DIR/install.sh" \
    -f "$test_watch_file" \
    -c "echo triggered >> $test_out_file" \
    --label "$test_label"

  if [[ ! -f "$plist_path" ]]; then
    echo "FAILED: plist was not created at $plist_path" >&2
    return 1
  fi
  if ! launchctl list | grep -q "$test_label"; then
    echo "FAILED: launchd job $test_label is not registered" >&2
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
    return 1
  fi
  echo "OK: daemon ran the command after a file change (waited ${waited}s)."

  "$DIR/uninstall.sh" --label "$test_label"

  if [[ -f "$plist_path" ]]; then
    echo "FAILED: plist still exists after uninstall" >&2
    return 1
  fi
  if launchctl list | grep -q "$test_label"; then
    echo "FAILED: launchd job $test_label still registered after uninstall" >&2
    return 1
  fi
  echo "OK: uninstall removed the plist and unregistered the job."
}

echo "=== Unit tests (pytest, with coverage) ==="
"$DIR/.venv/bin/python" -m pytest \
  --cov=file_watcher --cov=daemon_watcher --cov=generate_plist \
  --cov-report=term-missing

echo
echo "=== Smoke test: bare watchdog/FSEvents sanity check ==="
"$DIR/.venv/bin/python" sanity_tests/bare_watchdog_check.py

echo
echo "=== Smoke test: full daemon against a real file ==="
"$DIR/verify_daemon.sh"

if [[ "$WITH_INSTALL_TEST" == true ]]; then
  run_install_uninstall_test
fi

echo
echo "All unit tests and smoke tests passed."
