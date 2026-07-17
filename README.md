# mac_file_watcher

Watches a list of files on macOS using the native **FSEvents** framework and
runs a shell command whenever any of them change. No polling — it's
event-driven, via the `watchdog` package, which talks to FSEvents directly.

A natural use case: watch a managed-preferences plist for tampering/policy
pushes and react to it, e.g. watching
`/Library/Managed Preferences/com.google.Chrome.plist` and, on change, running
a script that removes a specific Chrome extension.

When installed (see below), it runs as a `launchd` **LaunchDaemon** — root,
starting at boot, with no GUI session. If `--command` needs to interact with
a GUI app (Apple Events, Automation permissions, anything on screen), account
for the fact that there's no logged-in user or windowing session at all.

## Directory contents

| File | Purpose |
|---|---|
| `file_watcher.py` | Core, unit-tested logic: `resolve_watch_targets`, `PatchEventHandler` (FSEvents callback filtering + debounce), `FileWatcherDaemon` (command execution, start/stop). No OS-daemon concerns live here. |
| `daemon_watcher.py` | Thin CLI wrapper around `FileWatcherDaemon`: argument parsing, logging setup, pidfile handling, signal handling, and an optional classic double-fork `daemonize()` for running detached from a terminal. |
| `generate_plist.py` | Generates a `launchd` LaunchDaemon `.plist` from CLI args, using `plistlib` for correct XML escaping. Used by `install.sh`; not meant to be run by hand. |
| `build.sh` | Builds a deployable `prod/` folder containing only `install.sh`, `uninstall.sh`, and the runtime scripts (`daemon_watcher.py`, `file_watcher.py`, `generate_plist.py`, `requirements.txt`, `remove-browser-extension.js`) — no tests, no README. Removes any existing `prod/` first. |
| `install.sh` | Installer (run from `prod/`, as root): installs dependencies system-wide, copies the runtime scripts to `/Library/Application Support/Glow`, and writes/loads a LaunchDaemon plist so the watcher runs as root starting at every boot. Takes the file list and command as its own arguments and passes them straight through. |
| `uninstall.sh` | Unregisters and removes the LaunchDaemon installed by `install.sh` (also root). `--purge` also removes the runtime scripts from Glow. |
| `remove-browser-extension.js` | The extension-removal script used in the examples throughout this README (see `--command` examples below). Not part of the watcher's own logic — just a script `install.sh` happens to also deploy to Glow so a `--command` can reference it by relative name. |
| `run_all_tests.sh` | Runs the pytest suite (with coverage) and both smoke tests in one shot. Pass `--with-install-test` to also exercise `prod/install.sh`/`prod/uninstall.sh` end to end (requires `prod/` to exist and prompts for `sudo`). |
| `verify_daemon.sh` | Smoke test: runs `daemon_watcher.py` against a real file in `/tmp`, makes real changes to it, and confirms the command fires. Uses a harmless `echo`-based command (see note below) and self-cleans on exit. |
| `sanity_tests/bare_watchdog_check.py` | Smoke test: exercises the `watchdog`/FSEvents stack directly (no code of ours involved), to isolate "is FSEvents seeing changes on this machine" from "is our code correct". Self-cleaning. |
| `sanity_tests/self_trigger_loop_check.py` | Smoke test: runs the real CLI with a `--command` that edits the very file being watched, confirming it doesn't re-trigger itself in a loop. Self-cleaning. |
| `sanity_tests/multi_path_debounce_check.py` | Smoke test: runs the real CLI watching two files, changes both together, confirms the command ran exactly once (debounce is keyed to the command, not to any one path). Self-cleaning. |
| `sanity_tests/run_on_start_check.py` | Smoke test: confirms `--command` runs once automatically on startup with no file changes involved, and that `--skip-initial-run` disables that. Self-cleaning. |
| `test_file_watcher.py` | Unit tests for `file_watcher.py` — 100% line coverage. |
| `test_daemon_watcher.py` | Unit tests for `daemon_watcher.py` (including the double-fork `daemonize()`, mocked) — 100% line coverage. |
| `test_generate_plist.py` | Unit tests for `generate_plist.py` — 100% line coverage. |
| `requirements.txt` | `watchdog`, `pytest`, `pytest-cov` — installed into your system/user Python's site-packages, not a venv. |

Logs live under `/Library/Logs/Glow/` (see "Installing as a
startup daemon" below) — not inside this directory.

## Setup (without installing as a startup daemon)

This project runs directly against your system `python3` — no virtual
environment. Homebrew's Python refuses `pip install` outside a venv by
default (PEP 668); `--break-system-packages` overrides that guard, and
`--user` keeps the install scoped to your user site-packages rather than
Homebrew's own files:

```bash
python3 -m pip install --user -r requirements.txt
# If that fails with "externally-managed-environment":
python3 -m pip install --user --break-system-packages -r requirements.txt
```

Run it directly in the foreground:

```bash
python3 daemon_watcher.py \
  -f "/Library/Managed Preferences/com.google.Chrome.plist" \
  -c "osascript -l JavaScript $(pwd)/remove-browser-extension.js <base64-payload>"
```

Flags: `-f/--file` (repeatable), `-c/--command` (required), `--debounce SECONDS`
(default 0.1 — collapses rapid duplicate FSEvents into one command run),
`--skip-initial-run` (see below), `--daemon` (detach via double-fork instead
of running in the foreground), `--pidfile PATH`, `--log-file PATH`.

**Runs once on startup by default**: before it starts watching at all,
`FileWatcherDaemon.start()` runs `--command` once immediately (logged as
`Running command on startup`), then again on every subsequent watched-file
change — useful so the daemon reacts to state that may already be
out-of-policy at boot, not just to changes that happen after it starts. Pass
`--skip-initial-run` to disable this and only react to actual changes. The
initial run happens *before* the FSEvents observer is created, for the same
reason `on_any_event` marks its debounce timer after the command completes
(see "Notes / gotchas" below): if the command writes to a watched file
itself, that write must not be picked up as a spurious first change event.

`--command` can be any shell command — including one that references a
script by a relative name, like this repo's own `remove-browser-extension.js`:

```bash
osascript -l JavaScript remove-browser-extension.js eyJwYXJhbXMiOnsiZXh0ZW5zaW9uX2lkIjoiPDMyLWNoYXItaWQ+In0sImRyeV9ydW4iOmZhbHNlfQ==
```

That works because `--command` runs with its working directory defaulting to
`/Library/Application Support/Glow` (see "Notes / gotchas" below) — `install.sh`
deploys `remove-browser-extension.js` there alongside the runtime scripts, so
a bare relative filename resolves. When running `daemon_watcher.py` directly
(not installed), that directory won't exist yet unless you've already run
`install.sh` at least once, so a `--command` referencing a relative script
name would need Glow to exist, or you'd reference the script by full path
instead.

## Installing as a startup daemon (launchd)

Building and installing are separate steps: `build.sh` produces a minimal
`prod/` folder (install.sh, uninstall.sh, and just the runtime scripts —
nothing dev-only), and `install.sh` is what you actually run, as root, from
inside it:

```bash
./build.sh
sudo ./prod/install.sh \
  -f "/Library/Managed Preferences/com.google.Chrome.plist" \
  -c "osascript -l JavaScript remove-browser-extension.js <base64-payload>"
```

Options: `-f/--file` (repeatable, required), `-c/--command` (required),
`--debounce SECONDS`, `--label LABEL` (defaults to
`com.codestation.filewatcher`; use a different label to run more than one
instance watching different files).

`install.sh` must run as root — everything it touches is a system-wide
location, and the daemon itself runs as root too, as a **LaunchDaemon**:

- Dependencies are installed system-wide (`pip install --break-system-packages`,
  no venv, no per-user `--user` install).
- The runtime scripts (`daemon_watcher.py`, `file_watcher.py`,
  `remove-browser-extension.js`) are copied to
  `/Library/Application Support/Glow`, decoupled from wherever this checkout
  lives — this dev directory could later be moved or deleted without
  affecting the installed daemon.
- The plist goes to `/Library/LaunchDaemons/<label>.plist` and is bootstrapped
  into the `system` domain — it starts at boot, before any login, and keeps
  running (`KeepAlive`) regardless of who's logged in or whether anyone is.
- Logs go to `/Library/Logs/Glow/<label>.out.log` / `<label>.err.log`.

**No GUI session**: because this runs as a LaunchDaemon, `--command` executes
with no logged-in user, no Aqua session, nothing on screen. Root-level
filesystem/process work is fine; anything relying on Apple Events, GUI
scripting, or Automation/Accessibility permissions may not behave the way it
does when run interactively — test the actual `--command` after installing,
don't assume it carries over unchanged from a Terminal run.

To remove it:

```bash
sudo ./prod/uninstall.sh --label com.codestation.filewatcher   # label optional if using the default
```

Removing the plist is what actually stops it from ever loading again; as a
best-effort nicety `uninstall.sh` also stops it running right now. Add
`--purge` to also delete the runtime scripts from
`/Library/Application Support/Glow` — only do this if you're removing the
last installed label, since that runtime is shared.

## Testing

```bash
./run_all_tests.sh                      # unit tests + smoke tests
./build.sh && ./run_all_tests.sh --with-install-test  # ...plus a full install/uninstall round trip
```

The `--with-install-test` run drives `prod/install.sh`/`prod/uninstall.sh`
under `sudo` (so it prompts for a password interactively — run it yourself,
not through anything non-interactive), registering a throwaway LaunchDaemon
under a distinct `*.selftest` label, confirming it actually fires on a real
file change, then unregistering it — it won't touch a LaunchDaemon installed
under the default label. It requires `prod/` to already exist and errors
with a reminder to run `./build.sh` first if it doesn't. It's opt-in because,
unlike the other smoke tests, it touches real `launchd` state and needs root.

Note the smoke tests (`verify_daemon.sh`, the install self-test) use a plain
`echo ... >> file` command rather than the real extension-removal example
above — they need a command with an observable, harmless side effect to
assert against, and running `remove-browser-extension.js` for real would
actually attempt to remove a browser extension each time the tests run.

Or run pieces individually:

```bash
python3 -m pytest --cov=file_watcher --cov=daemon_watcher --cov=generate_plist --cov-report=term-missing
python3 sanity_tests/bare_watchdog_check.py
./verify_daemon.sh
```

## Notes / gotchas

- **Symlinks**: macOS resolves `/tmp` to `/private/tmp` (and `/var`
  similarly). FSEvents reports the *resolved* path, so all path comparisons
  in `file_watcher.py` use `os.path.realpath`, not `os.path.abspath` —
  otherwise watching a file under `/tmp` would never match. Covered by
  dedicated symlink tests in `test_file_watcher.py`.
- **Debounce is keyed to the command, not to any one watched path, and is
  measured from completion, not from trigger-time.** `FileWatcherDaemon.start()`
  builds exactly one `PatchEventHandler` — covering the union of every
  watched file — and schedules that *same* instance against every parent
  directory, so two different watched paths changing together (e.g. Chrome
  writing both the system-wide and per-user Managed Preferences plist as one
  policy push) run the command once, not once per path
  (`sanity_tests/multi_path_debounce_check.py`). Separately,
  `PatchEventHandler.mark_triggered()` fires *after* `on_change()` (which
  runs the command, possibly for several seconds) returns, not before — so
  the `--debounce` window covers events the command's own file writes cause.
  This matters concretely for `remove-browser-extension.js`: it edits the
  very plist the daemon is watching, and without this ordering, that edit
  would arrive outside a debounce window measured from when the command
  *started* and re-trigger itself in a loop
  (`sanity_tests/self_trigger_loop_check.py`). The same reasoning is why the
  automatic run-on-start (see above) executes before the FSEvents observer
  is even created, rather than after.
- **Watching individual files**: FSEvents/`watchdog` watch directories, not
  individual files, so `resolve_watch_targets` schedules a watch on each
  file's parent directory and filters events down to the files you asked for.
- **History: this was a LaunchAgent before it was a LaunchDaemon.** An
  earlier version of `install.sh` ran this as a `gui/<uid>` LaunchAgent
  (`/Library/LaunchAgents`, no fixed target user) specifically so `--command`
  would have GUI session access. That surfaced a real, easy-to-hit gotcha
  worth remembering if this ever moves back to a LaunchAgent: for `gui/<uid>`
  jobs, `launchd` opens `StandardOutPath`/`StandardErrorPath` as the *job's
  user*, not as root — a root-owned, `755` log directory silently blocks
  this, and the job fails to spawn at all (`launchctl print` shows
  `last exit code = 78: EX_CONFIG`, no log files are ever created, and
  nothing about the failure reaches Console/`log show` in an obvious way;
  the fix was `chmod 1777` on the log directory, like `/tmp`).
  `LimitLoadToSessionType: Aqua` was also set explicitly on the plist for the
  same reason. Now that this is a LaunchDaemon, `launchd` opens the log
  files as root, so neither of those applies anymore — but the tradeoff
  flipped: `--command` no longer has any GUI session at all (see "Installing
  as a startup daemon" above).
- **`--command`'s default working directory is Glow**: `FileWatcherDaemon`
  runs the triggered command with its working directory set to
  `/Library/Application Support/Glow` (`DEFAULT_COMMAND_DIR` in
  `file_watcher.py`) whenever that directory exists, falling back to the
  daemon's own working directory otherwise (e.g. when running
  `daemon_watcher.py` directly, outside of `install.sh`). This is why a
  `--command` can reference `remove-browser-extension.js` by a bare relative
  name instead of a full path — `install.sh` deploys it to Glow alongside
  `daemon_watcher.py`/`file_watcher.py` for exactly this reason. Override via
  `FileWatcherDaemon(..., command_dir=...)` (or `None` to disable) if needed.
