"""Sanity check: when two different watched files change together, does the
triggered command run once, or once per path?

Not part of the pytest suite -- exercises the real daemon_watcher.py CLI
against two real files with real FSEvents, mirroring watching both
/Library/Managed Preferences/com.google.Chrome.plist and its per-user
counterpart, which a single policy push can write to together.
"""
import os
import subprocess
import sys
import time

WATCHED_A = "/tmp/multi_path_debounce_a.txt"
WATCHED_B = "/tmp/multi_path_debounce_b.txt"
COUNTER = "/tmp/multi_path_debounce_counter.txt"
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    process = None
    try:
        for path in (WATCHED_A, WATCHED_B):
            with open(path, "w") as handle:
                handle.write("original\n")
        with open(COUNTER, "w") as handle:
            handle.write("0")

        command = f'count=$(cat "{COUNTER}"); echo $((count + 1)) > "{COUNTER}"'

        process = subprocess.Popen([
            sys.executable, os.path.join(PROJECT_DIR, "daemon_watcher.py"),
            "--file", WATCHED_A,
            "--file", WATCHED_B,
            "--command", command,
            "--debounce", "0.5",
            # Isolate this check to the multi-path debounce behavior; the
            # automatic run-on-start is a separate feature covered by
            # run_on_start_check.py.
            "--skip-initial-run",
        ])

        time.sleep(1)
        # Both files change together, as if from one policy push.
        for path in (WATCHED_A, WATCHED_B):
            with open(path, "a") as handle:
                handle.write("external change\n")

        time.sleep(3)

        with open(COUNTER) as handle:
            count = int(handle.read().strip())

        print(f"Command ran {count} time(s).")
        if count != 1:
            print(
                f"FAILED: expected exactly 1 run for a change to both watched "
                f"paths, got {count} (debounce keyed to path, not command?)"
            )
            sys.exit(1)
        print("OK: command ran exactly once for both paths changing together.")
    finally:
        if process is not None:
            process.terminate()
            process.wait()
        for path in (WATCHED_A, WATCHED_B, COUNTER):
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    main()
