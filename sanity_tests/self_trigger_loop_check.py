"""Sanity check: does a --command that writes to the watched file itself
cause daemon_watcher.py to re-trigger in a loop?

Not part of the pytest suite -- exercises the real daemon_watcher.py CLI
against a real file with real FSEvents, mirroring the
remove-browser-extension.js scenario where the triggered script edits the
very managed-preferences plist it's reacting to.
"""
import os
import subprocess
import sys
import time

WATCHED = "/tmp/self_trigger_loop_watched.txt"
COUNTER = "/tmp/self_trigger_loop_counter.txt"
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    process = None
    try:
        with open(WATCHED, "w") as handle:
            handle.write("original\n")
        with open(COUNTER, "w") as handle:
            handle.write("0")

        # The triggered command increments a counter *and* writes to the
        # watched file itself -- exactly like remove-browser-extension.js
        # editing the plist it's reacting to.
        command = (
            f'count=$(cat "{COUNTER}"); '
            f'echo $((count + 1)) > "{COUNTER}"; '
            f'echo "edited by command" >> "{WATCHED}"'
        )

        process = subprocess.Popen([
            sys.executable, os.path.join(PROJECT_DIR, "daemon_watcher.py"),
            "--file", WATCHED,
            "--command", command,
            "--debounce", "0.5",
            # Isolate this check to the self-trigger-loop behavior; the
            # automatic run-on-start is a separate feature covered by
            # run_on_start_check.py.
            "--skip-initial-run",
        ])

        time.sleep(1)
        with open(WATCHED, "a") as handle:
            handle.write("external change\n")

        # Give it plenty of time to (mis)fire repeatedly if the self-trigger
        # loop bug were still present.
        time.sleep(4)

        with open(COUNTER) as handle:
            count = int(handle.read().strip())

        print(f"Command ran {count} time(s).")
        if count != 1:
            print(
                f"FAILED: expected exactly 1 run, got {count} "
                "(self-triggered feedback loop?)"
            )
            sys.exit(1)
        print("OK: command ran exactly once, no self-trigger loop.")
    finally:
        if process is not None:
            process.terminate()
            process.wait()
        for path in (WATCHED, COUNTER):
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    main()
