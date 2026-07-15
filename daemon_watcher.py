"""CLI / daemon wrapper around FileWatcherDaemon.

This module only wires the tested FileWatcherDaemon (file_watcher.py) into a
background macOS process: argument parsing, logging setup, pidfile handling,
signal handling, and an optional double-fork daemonize(). It's intentionally
thin -- almost all behavior lives in file_watcher.py where it's easy to test.
"""
from __future__ import annotations

import argparse
import atexit
import logging
import os
import signal
import sys
import threading
from typing import List, Optional

from file_watcher import FileWatcherDaemon

DEFAULT_PIDFILE = "/tmp/file_watcher.pid"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch files via macOS FSEvents and run a command on change."
    )
    parser.add_argument(
        "--file", "-f", dest="files", action="append", required=True,
        help='Path to watch. Can be given multiple times '
             '(e.g. "/Library/Managed Preferences/com.google.Chrome.plist").',
    )
    parser.add_argument(
        "--command", "-c", required=True,
        help='Shell command to run when a watched file changes '
             '(e.g. \'osascript -e "display dialog \\"patch\\""\').',
    )
    parser.add_argument(
        "--debounce", type=float, default=0.5,
        help="Seconds to collapse rapid duplicate events into one run (default: 0.5).",
    )
    parser.add_argument(
        "--skip-initial-run", action="store_true",
        help="Don't run --command once immediately on startup. By default it "
             "runs once at start (before watching begins), then again on "
             "every watched-file change.",
    )
    parser.add_argument("--daemon", action="store_true", help="Detach and run in the background.")
    parser.add_argument(
        "--pidfile", default=DEFAULT_PIDFILE,
        help="Where to write the daemon pid (default: %(default)s).",
    )
    parser.add_argument("--log-file", default=None, help="File to write logs to when daemonized.")
    return parser.parse_args(argv)


def configure_logging(log_file: Optional[str] = None) -> None:
    handlers = [logging.FileHandler(log_file)] if log_file else [logging.StreamHandler(sys.stdout)]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def write_pidfile(pidfile: str) -> None:
    with open(pidfile, "w") as handle:
        handle.write(str(os.getpid()))


def remove_pidfile(pidfile: str) -> None:
    try:
        os.remove(pidfile)
    except OSError:
        pass


def daemonize(pidfile: str) -> None:
    """Detach the process from the controlling terminal (classic double fork)."""
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)

    sys.stdout.flush()
    sys.stderr.flush()
    devnull_fd = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull_fd, sys.stdin.fileno())
    os.dup2(devnull_fd, sys.stdout.fileno())
    os.dup2(devnull_fd, sys.stderr.fileno())

    write_pidfile(pidfile)
    atexit.register(remove_pidfile, pidfile)


def install_signal_handlers(stop_event: threading.Event) -> None:
    def handle_stop(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)

    if args.daemon:
        daemonize(args.pidfile)

    configure_logging(args.log_file)

    watcher = FileWatcherDaemon(
        files=args.files,
        command=args.command,
        debounce_seconds=args.debounce,
        run_on_start=not args.skip_initial_run,
    )
    stop_event = threading.Event()
    install_signal_handlers(stop_event)

    watcher.start()
    try:
        stop_event.wait()
    finally:
        watcher.stop()


if __name__ == "__main__":  # pragma: no cover
    main()
