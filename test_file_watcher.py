import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from file_watcher import FileWatcherDaemon, PatchEventHandler, resolve_watch_targets


def make_event(src_path, is_directory=False, dest_path=None):
    return SimpleNamespace(src_path=src_path, is_directory=is_directory, dest_path=dest_path)


# ---------------------------------------------------------------------------
# resolve_watch_targets
# ---------------------------------------------------------------------------

def test_resolve_watch_targets_groups_by_parent_dir(tmp_path):
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    targets = resolve_watch_targets([str(f1), str(f2)])
    assert set(targets.keys()) == {str(tmp_path)}
    assert targets[str(tmp_path)] == {str(f1), str(f2)}


def test_resolve_watch_targets_splits_different_dirs(tmp_path):
    dir1 = tmp_path / "d1"
    dir2 = tmp_path / "d2"
    dir1.mkdir()
    dir2.mkdir()
    f1 = dir1 / "a.txt"
    f2 = dir2 / "b.txt"
    targets = resolve_watch_targets([str(f1), str(f2)])
    assert set(targets.keys()) == {str(dir1), str(dir2)}
    assert targets[str(dir1)] == {str(f1)}
    assert targets[str(dir2)] == {str(f2)}


def test_resolve_watch_targets_resolves_relative_path_against_cwd():
    targets = resolve_watch_targets(["justafile.txt"])
    expected_dir = os.path.dirname(os.path.realpath("justafile.txt"))
    assert list(targets.keys()) == [expected_dir]
    assert targets[expected_dir] == {os.path.realpath("justafile.txt")}


def test_resolve_watch_targets_resolves_symlinked_file(tmp_path):
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    real_file = real_dir / "target.txt"
    real_file.write_text("hi")

    link_dir = tmp_path / "link_dir"
    link_dir.symlink_to(real_dir, target_is_directory=True)
    watched_via_symlink = link_dir / "target.txt"

    targets = resolve_watch_targets([str(watched_via_symlink)])

    # The parent dir key and the watched path must both be the *resolved*
    # (real) location, since that's what FSEvents reports for events under
    # a symlinked path (e.g. macOS's /tmp -> /private/tmp).
    assert list(targets.keys()) == [str(real_dir)]
    assert targets[str(real_dir)] == {str(real_file)}


def test_resolve_event_path_resolves_symlinked_src_path(tmp_path):
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    real_file = real_dir / "target.txt"
    real_file.write_text("hi")

    link_dir = tmp_path / "link_dir"
    link_dir.symlink_to(real_dir, target_is_directory=True)
    symlinked_path = str(link_dir / "target.txt")

    handler = PatchEventHandler(watched_paths=set(), on_change=lambda p: None)
    event = make_event(symlinked_path)

    assert handler.resolve_event_path(event) == str(real_file)


def test_is_relevant_true_when_event_path_is_symlinked_but_watched_path_is_real(tmp_path):
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    real_file = real_dir / "target.txt"
    real_file.write_text("hi")

    link_dir = tmp_path / "link_dir"
    link_dir.symlink_to(real_dir, target_is_directory=True)
    symlinked_path = str(link_dir / "target.txt")

    # Simulates watching a path under a symlinked directory (e.g. /tmp):
    # resolve_watch_targets would have stored the *real* path here.
    handler = PatchEventHandler(watched_paths={str(real_file)}, on_change=lambda p: None)
    event = make_event(symlinked_path)

    assert handler.is_relevant(event) is True


# ---------------------------------------------------------------------------
# PatchEventHandler
# ---------------------------------------------------------------------------

def test_resolve_event_path_uses_src_path_when_no_dest():
    handler = PatchEventHandler(watched_paths=set(), on_change=lambda p: None)
    event = make_event("/tmp/a.txt")
    assert handler.resolve_event_path(event) == os.path.realpath("/tmp/a.txt")


def test_resolve_event_path_prefers_dest_path_for_moves():
    handler = PatchEventHandler(watched_paths=set(), on_change=lambda p: None)
    event = make_event("/tmp/old.txt", dest_path="/tmp/new.txt")
    assert handler.resolve_event_path(event) == os.path.realpath("/tmp/new.txt")


def test_is_relevant_false_for_directories():
    handler = PatchEventHandler(
        watched_paths={os.path.realpath("/tmp/a.txt")}, on_change=lambda p: None
    )
    event = make_event("/tmp/a.txt", is_directory=True)
    assert handler.is_relevant(event) is False


def test_is_relevant_false_for_unwatched_path():
    handler = PatchEventHandler(
        watched_paths={os.path.realpath("/tmp/a.txt")}, on_change=lambda p: None
    )
    event = make_event("/tmp/other.txt")
    assert handler.is_relevant(event) is False


def test_is_relevant_true_for_watched_file():
    handler = PatchEventHandler(
        watched_paths={os.path.realpath("/tmp/a.txt")}, on_change=lambda p: None
    )
    event = make_event("/tmp/a.txt")
    assert handler.is_relevant(event) is True


def test_should_trigger_true_on_first_call():
    handler = PatchEventHandler(watched_paths=set(), on_change=lambda p: None)
    assert handler.should_trigger() is True


def test_should_trigger_false_within_debounce_window():
    clock = iter([100.0, 100.1])
    handler = PatchEventHandler(
        watched_paths=set(), on_change=lambda p: None,
        debounce_seconds=0.5, time_fn=lambda: next(clock),
    )
    handler.mark_triggered()  # consumes 100.0
    assert handler.should_trigger() is False  # 100.1 - 100.0 < 0.5


def test_should_trigger_true_after_debounce_window():
    clock = iter([100.0, 100.6])
    handler = PatchEventHandler(
        watched_paths=set(), on_change=lambda p: None,
        debounce_seconds=0.5, time_fn=lambda: next(clock),
    )
    handler.mark_triggered()  # consumes 100.0
    assert handler.should_trigger() is True  # 100.6 - 100.0 >= 0.5


def test_mark_triggered_records_time():
    handler = PatchEventHandler(watched_paths=set(), on_change=lambda p: None, time_fn=lambda: 42.0)
    handler.mark_triggered()
    assert handler._last_triggered == 42.0


def test_on_any_event_ignores_irrelevant_event():
    calls = []
    handler = PatchEventHandler(watched_paths={"/tmp/a.txt"}, on_change=calls.append)
    handler.on_any_event(make_event("/tmp/other.txt"))
    assert calls == []


def test_on_any_event_ignores_when_debounced():
    handler = PatchEventHandler(
        watched_paths={os.path.realpath("/tmp/a.txt")}, on_change=lambda p: None,
        debounce_seconds=0.5, time_fn=lambda: 100.0,
    )
    handler.mark_triggered()
    calls = []
    handler.on_change = calls.append
    handler.on_any_event(make_event("/tmp/a.txt"))
    assert calls == []


def test_on_any_event_triggers_callback_for_relevant_event():
    calls = []
    real_path = os.path.realpath("/tmp/a.txt")
    handler = PatchEventHandler(watched_paths={real_path}, on_change=calls.append)
    handler.on_any_event(make_event("/tmp/a.txt"))
    assert calls == [real_path]
    assert handler._last_triggered is not None


# ---------------------------------------------------------------------------
# FileWatcherDaemon
# ---------------------------------------------------------------------------

def test_init_rejects_empty_files():
    with pytest.raises(ValueError):
        FileWatcherDaemon(files=[], command="echo hi")


def test_init_rejects_empty_command():
    with pytest.raises(ValueError):
        FileWatcherDaemon(files=["a"], command="")


def test_build_subprocess_kwargs_for_string_command():
    watcher = FileWatcherDaemon(files=["a"], command="echo hi")
    assert watcher.build_subprocess_kwargs() == {"args": "echo hi", "shell": True}


def test_build_subprocess_kwargs_for_list_command():
    watcher = FileWatcherDaemon(files=["a"], command=["echo", "hi"])
    assert watcher.build_subprocess_kwargs() == {"args": ["echo", "hi"], "shell": False}


@patch("file_watcher.subprocess.run")
def test_execute_command_runs_subprocess(mock_run):
    watcher = FileWatcherDaemon(files=["a"], command="echo hi")
    mock_run.return_value = subprocess.CompletedProcess(args="echo hi", returncode=0, stdout="")
    result = watcher.execute_command()
    mock_run.assert_called_once_with(
        args="echo hi", shell=True, check=False, stdout=subprocess.PIPE, text=True,
    )
    assert result.returncode == 0


@patch("file_watcher.subprocess.run")
def test_execute_command_logs_command_stdout(mock_run, caplog):
    watcher = FileWatcherDaemon(files=["a"], command="echo hi")
    mock_run.return_value = subprocess.CompletedProcess(args="echo hi", returncode=0, stdout="hello\n")
    with caplog.at_level("INFO", logger="file_watcher"):
        watcher.execute_command()
    assert "Command stdout: hello" in caplog.text


def test_log_command_output_logs_nonempty_stdout(caplog):
    watcher = FileWatcherDaemon(files=["a"], command="echo hi")
    result = subprocess.CompletedProcess(args="echo hi", returncode=0, stdout="hello\n")
    with caplog.at_level("INFO", logger="file_watcher"):
        watcher.log_command_output(result)
    assert "Command stdout: hello" in caplog.text


def test_log_command_output_skips_empty_stdout(caplog):
    watcher = FileWatcherDaemon(files=["a"], command="echo hi")
    result = subprocess.CompletedProcess(args="echo hi", returncode=0, stdout="")
    with caplog.at_level("INFO", logger="file_watcher"):
        watcher.log_command_output(result)
    assert "Command stdout" not in caplog.text


@patch("file_watcher.subprocess.run")
def test_handle_change_runs_command(mock_run):
    watcher = FileWatcherDaemon(files=["a"], command="echo hi")
    watcher.handle_change("/tmp/a.txt")
    mock_run.assert_called_once()


class FakeObserver:
    """Test double standing in for watchdog.observers.Observer."""

    instances = []

    def __init__(self):
        self.scheduled = []
        self.started = False
        self.stopped = False
        self.joined = False
        FakeObserver.instances.append(self)

    def schedule(self, handler, directory, recursive=False):
        self.scheduled.append((handler, directory, recursive))

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def join(self):
        self.joined = True


def test_start_schedules_handler_on_parent_dirs_and_starts_observer(tmp_path):
    FakeObserver.instances = []
    f1 = tmp_path / "a.txt"
    watcher = FileWatcherDaemon(files=[str(f1)], command="echo hi", observer_factory=FakeObserver)
    watcher.start()
    observer = FakeObserver.instances[0]
    assert observer.started is True
    assert len(observer.scheduled) == 1
    handler, directory, recursive = observer.scheduled[0]
    assert isinstance(handler, PatchEventHandler)
    assert directory == str(tmp_path)
    assert recursive is False
    assert handler.watched_paths == {str(f1)}


def test_stop_stops_and_joins_observer(tmp_path):
    f1 = tmp_path / "a.txt"
    watcher = FileWatcherDaemon(files=[str(f1)], command="echo hi", observer_factory=FakeObserver)
    watcher.start()
    observer = watcher._observer
    watcher.stop()
    assert observer.stopped is True
    assert observer.joined is True
    assert watcher._observer is None


def test_stop_is_noop_when_never_started():
    watcher = FileWatcherDaemon(files=["a"], command="echo hi", observer_factory=FakeObserver)
    watcher.stop()  # must not raise
    assert watcher._observer is None


@patch("file_watcher.subprocess.run")
def test_end_to_end_handler_triggers_daemon_command(mock_run, tmp_path):
    """Wire a real PatchEventHandler (as start() builds it) to a fake event."""
    f1 = tmp_path / "a.txt"
    watcher = FileWatcherDaemon(files=[str(f1)], command="echo hi", observer_factory=FakeObserver)
    watcher.start()
    handler, _directory, _recursive = watcher._observer.scheduled[0]
    handler.on_any_event(make_event(str(f1)))
    mock_run.assert_called_once()
