# mac_file_watcher

Watches a list of files on macOS using the native **FSEvents** framework and
runs a shell command whenever any of them change. No polling — it's
event-driven, via the `watchdog` package, which talks to FSEvents directly.

A natural use case: watch a managed-preferences plist for tampering/policy
pushes and react to it, e.g. watching
`/Library/Managed Preferences/com.google.Chrome.plist` and, on change, running
a script that removes a specific Chrome extension.

## Directory contents

| File | Purpose |
|---|---|
| `file_watcher.py` | Core, unit-tested logic: `resolve_watch_targets`, `PatchEventHandler` (FSEvents callback filtering + debounce), `FileWatcherDaemon` (command execution, start/stop). No OS-daemon concerns live here. |
| `daemon_watcher.py` | Thin CLI wrapper around `FileWatcherDaemon`: argument parsing, logging setup, pidfile handling, signal handling, and an optional classic double-fork `daemonize()` for running detached from a terminal. |
| `generate_plist.py` | Generates a `launchd` LaunchAgent `.plist` from CLI args, using `plistlib` for correct XML escaping. Used by `install.sh`; not meant to be run by hand. |
| `install.sh` | Installer: creates a venv, installs dependencies, generates a plist via `generate_plist.py`, and registers + starts it with `launchctl` so the watcher runs automatically at every login. Takes the file list and command as its own arguments and passes them straight through. |
| `uninstall.sh` | Unregisters and removes the LaunchAgent installed by `install.sh`. |
| `run_all_tests.sh` | Runs the pytest suite (with coverage) and both smoke tests in one shot. Pass `--with-install-test` to also exercise `install.sh`/`uninstall.sh` end to end. |
| `verify_daemon.sh` | Smoke test: runs `daemon_watcher.py` against a real file in `/tmp`, makes real changes to it, and confirms the command fires. Uses a harmless `echo`-based command (see note below) and self-cleans on exit. |
| `sanity_tests/bare_watchdog_check.py` | Smoke test: exercises the `watchdog`/FSEvents stack directly (no code of ours involved), to isolate "is FSEvents seeing changes on this machine" from "is our code correct". Self-cleaning. |
| `test_file_watcher.py` | Unit tests for `file_watcher.py` — 100% line coverage. |
| `test_daemon_watcher.py` | Unit tests for `daemon_watcher.py` (including the double-fork `daemonize()`, mocked) — 100% line coverage. |
| `test_generate_plist.py` | Unit tests for `generate_plist.py` — 100% line coverage. |
| `requirements.txt` | `watchdog`, `pytest`, `pytest-cov`. |
| `.venv/` | Local virtualenv (created by `install.sh` or manually; see below). Not checked in. |
| `logs/` | Created by `install.sh`; holds the daemon's stdout/stderr when running under `launchd`. |

## Setup (without installing as a startup daemon)

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Run it directly in the foreground:

```bash
./.venv/bin/python daemon_watcher.py \
  -f "/Library/Managed Preferences/com.google.Chrome.plist" \
  -c /path/to/remove_extension.sh
```

Flags: `-f/--file` (repeatable), `-c/--command` (required), `--debounce SECONDS`
(default 0.5 — collapses rapid duplicate FSEvents into one command run),
`--daemon` (detach via double-fork instead of running in the foreground),
`--pidfile PATH`, `--log-file PATH`.

`--command` can be any shell command — including a path to a wrapper script,
which is the cleanest way to run something with nontrivial quoting. For
example, `remove_extension.sh`:

```bash
#!/bin/bash
PAYLOAD='{"params":{"extension_id":"<32-char-id>"},"dry_run":false}'
INPUT=$(printf '%s' "$PAYLOAD" | base64)
osascript -l JavaScript remove-browser-extension.js "$INPUT"
```

## Installing as a startup daemon (launchd)

`install.sh` sets up the venv, installs dependencies, and registers a
**LaunchAgent** (not a LaunchDaemon — LaunchDaemons run as root before login
with no GUI session, so anything needing the user's session would silently
fail; LaunchAgents run in the user's own login session).

```bash
./install.sh \
  -f "/Library/Managed Preferences/com.google.Chrome.plist" \
  -c /path/to/remove_extension.sh
```

Options: `-f/--file` (repeatable, required), `-c/--command` (required),
`--debounce SECONDS`, `--label LABEL` (defaults to
`com.codestation.filewatcher`; use a different label to run more than one
instance watching different files).

This writes `~/Library/LaunchAgents/<label>.plist`, and calls
`launchctl load -w` so it starts immediately and again at every future login.
`RunAtLoad` and `KeepAlive` are both set, so `launchd` also restarts it if it
ever crashes.

To remove it:

```bash
./uninstall.sh --label com.codestation.filewatcher   # label optional if using the default
```

## Testing

```bash
./run_all_tests.sh                      # unit tests + smoke tests
./run_all_tests.sh --with-install-test  # ...plus a full install/uninstall round trip
```

The `--with-install-test` run registers a throwaway LaunchAgent under a
distinct `*.selftest` label, confirms it actually fires on a real file
change, then unregisters it — it won't touch a LaunchAgent you've installed
under the default label. It's opt-in because, unlike the other smoke tests,
it briefly touches real `launchd` state.

Note the smoke tests (`verify_daemon.sh`, the install self-test) use a plain
`echo ... >> file` command rather than the real extension-removal example
above — they need a command with an observable, harmless side effect to
assert against, and `remove-browser-extension.js` isn't part of this repo.

Or run pieces individually:

```bash
./.venv/bin/python -m pytest --cov=file_watcher --cov=daemon_watcher --cov=generate_plist --cov-report=term-missing
./.venv/bin/python sanity_tests/bare_watchdog_check.py
./verify_daemon.sh
```

## Notes / gotchas

- **Symlinks**: macOS resolves `/tmp` to `/private/tmp` (and `/var`
  similarly). FSEvents reports the *resolved* path, so all path comparisons
  in `file_watcher.py` use `os.path.realpath`, not `os.path.abspath` —
  otherwise watching a file under `/tmp` would never match. Covered by
  dedicated symlink tests in `test_file_watcher.py`.
- **Debounce**: editors (and policy-push mechanisms) often generate more than
  one filesystem event for a single logical write. `PatchEventHandler`
  collapses events within `--debounce` seconds of the last trigger into a
  single command run.
- **Watching individual files**: FSEvents/`watchdog` watch directories, not
  individual files, so `resolve_watch_targets` schedules a watch on each
  file's parent directory and filters events down to the files you asked for.
