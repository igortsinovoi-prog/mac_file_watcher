#!/bin/bash
# Read-only, non-destructive real-world check of remove-browser-extension.js's
# _runCommand-dependent diagnostic functions on this machine - NOT a full
# exercise of the script (which would write managed-prefs policy, kill/relaunch
# Chrome, and delete extension files; too destructive/disruptive for a
# lightweight check and there is no existing automated harness for that flow).
#
# Specifically targets resolveConsoleUser(), serialNumber(), and
# getOSMajorVersion() - all three call the same _runCommand() that was found
# to silently return empty stdout despite exit code 0 (temp-file capture bug,
# fixed by switching to a single shared NSPipe read before waitUntilExit; see
# js_scripts/lib/set-vscode-extension-version-runner.js for the original find
# and js_scripts/remove-browser-extension.js for the matching fix applied
# here). resolveConsoleUser() in particular is the production analogue of the
# exact repro that motivated the fix (`id -u <user>`, a fast, simple command).
#
# Nothing here mutates system state, so there's nothing to restore - the
# self-cleaning convention still applies in spirit, there's just nothing to
# clean up.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"  # js_scripts/
SCRIPT="$DIR/remove-browser-extension.js"
# resolveConsoleUser() reports the GUI console user (stat -f%Su /dev/console),
# not whoever invoked this script - under sudo, $(whoami) is "root", which is
# never what resolveConsoleUser() returns (it explicitly excludes "root").
EXPECTED_USER="$(stat -f%Su /dev/console)"

PASS=0
FAIL=0

check() {
  # $1 = description, $2 = actual, $3 = expected
  if [[ "$2" == "$3" ]]; then
    echo "  OK: $1"
    PASS=$((PASS + 1))
  else
    echo "  FAILED: $1 (expected '$3', got '$2')"
    FAIL=$((FAIL + 1))
  fi
}

check_nonempty() {
  # $1 = description, $2 = actual
  if [[ -n "$2" ]]; then
    echo "  OK: $1 (got '$2')"
    PASS=$((PASS + 1))
  else
    echo "  FAILED: $1 (got empty string)"
    FAIL=$((FAIL + 1))
  fi
}

echo "==> Running read-only diagnostics from $SCRIPT"
result="$(osascript -l JavaScript - "$SCRIPT" <<'JXA'
ObjC.import('Foundation');

function readFile(path) {
  var s = $.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null);
  if (!s || s.isNil()) throw new Error('could not read ' + path);
  return s.js;
}

function run(argv) {
  // Load the functions defined by the target script into this scope WITHOUT
  // invoking its own run() (which performs the real, destructive removal work).
  eval(readFile(argv[0]));

  var consoleUser = resolveConsoleUser();
  var out = {
    console_user: consoleUser,
    serial_number: serialNumber(),
    os_major_version: getOSMajorVersion(),
  };
  return JSON.stringify(out);
}
JXA
)"
echo "  result: $result"

console_user="$(python3 -c 'import json,sys; v=json.loads(sys.argv[1]).get("console_user"); print(v.get("user") if v else "")' "$result")"
console_uid="$(python3 -c 'import json,sys; v=json.loads(sys.argv[1]).get("console_user"); print(v.get("uid") if v else "")' "$result")"
serial="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("serial_number"))' "$result")"
os_major="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("os_major_version"))' "$result")"

echo
check "resolveConsoleUser reports the real logged-in user ($EXPECTED_USER)" "$console_user" "$EXPECTED_USER"
check_nonempty "resolveConsoleUser reports a real numeric uid (previously NaN -> null on the empty-stdout bug)" "$console_uid"
check_nonempty "serialNumber is non-empty (previously empty on the empty-stdout bug)" "$serial"
if [[ "$os_major" != "0" ]]; then
  echo "  OK: getOSMajorVersion is non-zero (previously 0 on the empty-stdout bug, got $os_major)"
  PASS=$((PASS + 1))
else
  echo "  FAILED: getOSMajorVersion is 0 (would indicate the empty-stdout bug is still present)"
  FAIL=$((FAIL + 1))
fi

echo
echo "$PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
