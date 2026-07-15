"""Sanity check: does daemon_watcher.py run --command once automatically on
startup, and does --skip-initial-run correctly disable that?

Not part of the pytest suite -- exercises the real daemon_watcher.py CLI,
with no file changes involved at all: the only thing that could cause the
counter to move is the startup-run behavior itself.
"""
import os
import subprocess
import sys
import time

WATCHED = "/tmp/run_on_start_watched.txt"
COUNTER = "/tmp/run_on_start_counter.txt"
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_daemon(extra_args):
    return subprocess.Popen([
        sys.executable, os.path.join(PROJECT_DIR, "daemon_watcher.py"),
        "--file", WATCHED,
        "--command", f'count=$(cat "{COUNTER}"); echo $((count + 1)) > "{COUNTER}"',
        "--debounce", "0.5",
        *extra_args,
    ])


def read_counter():
    with open(COUNTER) as handle:
        return int(handle.read().strip())


def main():
    process = None
    failed = False
    try:
        with open(WATCHED, "w") as handle:
            handle.write("original\n")

        # Default behavior: no file changes at all, just start and wait --
        # the command should still run once, automatically.
        with open(COUNTER, "w") as handle:
            handle.write("0")
        process = run_daemon([])
        time.sleep(2)
        count = read_counter()
        print(f"Default (no --skip-initial-run): command ran {count} time(s).")
        if count != 1:
            print(f"FAILED: expected exactly 1 run on startup, got {count}.")
            failed = True
        else:
            print("OK: command ran once automatically on startup.")
        process.terminate()
        process.wait()
        process = None

        # --skip-initial-run: same setup, but must NOT run automatically.
        with open(COUNTER, "w") as handle:
            handle.write("0")
        process = run_daemon(["--skip-initial-run"])
        time.sleep(2)
        count = read_counter()
        print(f"With --skip-initial-run: command ran {count} time(s).")
        if count != 0:
            print(f"FAILED: expected 0 runs with --skip-initial-run, got {count}.")
            failed = True
        else:
            print("OK: --skip-initial-run correctly suppressed the startup run.")

        if failed:
            sys.exit(1)
    finally:
        if process is not None:
            process.terminate()
            process.wait()
        for path in (WATCHED, COUNTER):
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    main()
