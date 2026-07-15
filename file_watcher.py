"""Event-driven file watching on top of macOS's native FSEvents API.

Uses the `watchdog` package (pip install watchdog), which on macOS talks
directly to the CoreServices FSEvents framework -- no polling loop.

Kept free of OS-daemon concerns (forking, signals, pidfiles) so it stays easy
to unit test. See daemon_watcher.py for the CLI/daemon wrapper.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Callable, Dict, List, Optional, Sequence, Set, Union

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

Command = Union[str, Sequence[str]]

# Where install.sh deploys the runtime scripts (daemon_watcher.py,
# file_watcher.py, and any command scripts like remove-browser-extension.js).
# Used as the triggered command's default working directory, so a --command
# that references a script by a bare relative name finds it there.
DEFAULT_COMMAND_DIR = "/Library/Application Support/Glow"


def resolve_watch_targets(files: Sequence[str]) -> Dict[str, Set[str]]:
    """Group watched files by parent directory.

    FSEvents (via watchdog) watches directories, not individual files, so we
    watch each file's parent directory and filter events down to the files
    we actually care about.
    """
    targets: Dict[str, Set[str]] = {}
    for path in files:
        real_path = os.path.realpath(path)
        parent_dir = os.path.dirname(real_path)
        targets.setdefault(parent_dir, set()).add(real_path)
    return targets


class PatchEventHandler(FileSystemEventHandler):
    """Filters FSEvents callbacks down to a set of watched paths and debounces them."""

    def __init__(
        self,
        watched_paths: Set[str],
        on_change: Callable[[str], None],
        debounce_seconds: float = 0.5,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.watched_paths = watched_paths
        self.on_change = on_change
        self.debounce_seconds = debounce_seconds
        self.time_fn = time_fn
        self._last_triggered: Optional[float] = None

    def resolve_event_path(self, event) -> str:
        dest_path = getattr(event, "dest_path", None)
        return os.path.realpath(dest_path or event.src_path)

    def is_relevant(self, event) -> bool:
        if event.is_directory:
            return False
        return self.resolve_event_path(event) in self.watched_paths

    def should_trigger(self) -> bool:
        now = self.time_fn()
        if self._last_triggered is None:
            return True
        return (now - self._last_triggered) >= self.debounce_seconds

    def mark_triggered(self) -> None:
        self._last_triggered = self.time_fn()

    def on_any_event(self, event) -> None:
        """Single entry point watchdog calls for every filesystem event."""
        if not self.is_relevant(event):
            return
        if not self.should_trigger():
            return
        # Marked *after* on_change (which runs the command, and can take
        # seconds) rather than before it, so the debounce window covers
        # events the command's own file writes cause -- e.g. a script that
        # edits the very file being watched, which would otherwise
        # re-trigger itself in a loop once its own edit lands outside a
        # debounce window measured from trigger-time instead of
        # completion-time.
        self.on_change(self.resolve_event_path(event))
        self.mark_triggered()


class FileWatcherDaemon:
    """Watches a list of files via native FSEvents and runs a command on change."""

    def __init__(
        self,
        files: Sequence[str],
        command: Command,
        debounce_seconds: float = 0.5,
        observer_factory: Callable[[], object] = Observer,
        command_dir: Optional[str] = DEFAULT_COMMAND_DIR,
        run_on_start: bool = True,
    ) -> None:
        if not files:
            raise ValueError("files must contain at least one path")
        if not command:
            raise ValueError("command must not be empty")
        self.files: List[str] = list(files)
        self.command: Command = command
        self.debounce_seconds: float = debounce_seconds
        self.observer_factory = observer_factory
        self.command_dir = command_dir
        self.run_on_start = run_on_start
        self._observer = None

    def build_subprocess_kwargs(self) -> Dict:
        if isinstance(self.command, str):
            kwargs: Dict = {"args": self.command, "shell": True}
        else:
            kwargs = {"args": list(self.command), "shell": False}
        if self.command_dir and os.path.isdir(self.command_dir):
            kwargs["cwd"] = self.command_dir
        return kwargs

    def execute_command(self) -> subprocess.CompletedProcess:
        kwargs = self.build_subprocess_kwargs()
        logger.info("Running command: %s", self.command)
        result = subprocess.run(**kwargs, check=False, stdout=subprocess.PIPE, text=True)
        self.log_command_output(result)
        return result

    def log_command_output(self, result: subprocess.CompletedProcess) -> None:
        if result.stdout:
            logger.info("Command stdout: %s", result.stdout.rstrip())

    def handle_change(self, changed_path: str) -> None:
        logger.info("File changed: %s", changed_path)
        self.execute_command()

    def start(self) -> None:
        logger.info("Starting watcher for %d file(s): %s", len(self.files), self.files)

        # Run before the observer exists, not after: if this command writes
        # to a watched file itself (see handle_change's docstring on the
        # same issue), doing it before we're actually watching means those
        # writes can't be picked up as a spurious first change event.
        if self.run_on_start:
            logger.info("Running command on startup")
            self.execute_command()

        targets = resolve_watch_targets(self.files)
        watched_paths = {path for paths in targets.values() for path in paths}
        handler = PatchEventHandler(
            watched_paths=watched_paths,
            on_change=self.handle_change,
            debounce_seconds=self.debounce_seconds,
        )
        self._observer = self.observer_factory()
        for directory in targets:
            self._observer.schedule(handler, directory, recursive=False)
        self._observer.start()

    def stop(self) -> None:
        if self._observer is None:
            return
        self._observer.stop()
        self._observer.join()
        self._observer = None
