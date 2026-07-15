import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from file_watcher import (
    DEFAULT_COMMAND_DIR,
    FileWatcherDaemon,
    PatchEventHandler,
    resolve_watch_targets,
)


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


def test_on_any_event_marks_triggered_after_on_change_completes():
    # Regression test: mark_triggered() must fire *after* on_change (which
    # runs the triggered command, possibly for several seconds) returns, not
    # before -- otherwise the debounce window is measured from when we
    # decided to trigger rather than when the command actually finished, and
    # a command that writes to the watched file itself (e.g.
    # remove-browser-extension.js editing the plist it's reacting to) can
    # re-trigger itself once that window has already elapsed mid-command.
    call_order = []
    handler = PatchEventHandler(
        watched_paths={os.path.realpath("/tmp/a.txt")},
        on_change=lambda path: call_order.append("on_change"),
    )
    original_mark_triggered = handler.mark_triggered
    handler.mark_triggered = lambda: (call_order.append("mark_triggered"), original_mark_triggered())
    handler.on_any_event(make_event("/tmp/a.txt"))
    assert call_order == ["on_change", "mark_triggered"]


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
    watcher = FileWatcherDaemon(files=["a"], command="echo hi", command_dir=None)
    assert watcher.build_subprocess_kwargs() == {"args": "echo hi", "shell": True}


def test_build_subprocess_kwargs_for_list_command():
    watcher = FileWatcherDaemon(files=["a"], command=["echo", "hi"], command_dir=None)
    assert watcher.build_subprocess_kwargs() == {"args": ["echo", "hi"], "shell": False}


def test_build_subprocess_kwargs_defaults_cwd_to_glow_when_it_exists(tmp_path):
    watcher = FileWatcherDaemon(files=["a"], command="echo hi", command_dir=str(tmp_path))
    assert watcher.build_subprocess_kwargs() == {
        "args": "echo hi", "shell": True, "cwd": str(tmp_path),
    }


def test_build_subprocess_kwargs_omits_cwd_when_command_dir_does_not_exist(tmp_path):
    missing_dir = str(tmp_path / "does_not_exist")
    watcher = FileWatcherDaemon(files=["a"], command="echo hi", command_dir=missing_dir)
    assert watcher.build_subprocess_kwargs() == {"args": "echo hi", "shell": True}


def test_build_subprocess_kwargs_uses_glow_by_default():
    # DEFAULT_COMMAND_DIR is a real, fixed path; just confirm the default
    # wiring (no explicit command_dir passed) matches whatever that constant
    # currently is, without hardcoding "/Library/Application Support/Glow"
    # twice.
    watcher = FileWatcherDaemon(files=["a"], command="echo hi")
    assert watcher.command_dir == DEFAULT_COMMAND_DIR


@patch("file_watcher.subprocess.run")
def test_execute_command_runs_subprocess(mock_run):
    watcher = FileWatcherDaemon(files=["a"], command="echo hi", command_dir=None)
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
    watcher = FileWatcherDaemon(
        files=[str(f1)], command="echo hi", observer_factory=FakeObserver, run_on_start=False,
    )
    watcher.start()
    observer = FakeObserver.instances[0]
    assert observer.started is True
    assert len(observer.scheduled) == 1
    handler, directory, recursive = observer.scheduled[0]
    assert isinstance(handler, PatchEventHandler)
    assert directory == str(tmp_path)
    assert recursive is False
    assert handler.watched_paths == {str(f1)}


def test_start_shares_a_single_handler_across_multiple_watched_directories(tmp_path):
    # The debounce state (PatchEventHandler._last_triggered) must be tied to
    # the command as a whole, not to any one watched path: start() builds
    # exactly one PatchEventHandler -- covering the union of every watched
    # file -- and schedules that *same* instance against each parent
    # directory, rather than creating a handler per path/directory.
    FakeObserver.instances = []
    dir1 = tmp_path / "d1"
    dir2 = tmp_path / "d2"
    dir1.mkdir()
    dir2.mkdir()
    f1 = dir1 / "a.txt"
    f2 = dir2 / "b.txt"
    watcher = FileWatcherDaemon(
        files=[str(f1), str(f2)], command="echo hi", observer_factory=FakeObserver,
        run_on_start=False,
    )
    watcher.start()
    observer = FakeObserver.instances[0]
    assert len(observer.scheduled) == 2
    handler_a, directory_a, _ = observer.scheduled[0]
    handler_b, directory_b, _ = observer.scheduled[1]
    assert handler_a is handler_b
    assert {directory_a, directory_b} == {str(dir1), str(dir2)}
    assert handler_a.watched_paths == {str(f1), str(f2)}


def test_stop_stops_and_joins_observer(tmp_path):
    f1 = tmp_path / "a.txt"
    watcher = FileWatcherDaemon(
        files=[str(f1)], command="echo hi", observer_factory=FakeObserver, run_on_start=False,
    )
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


def test_run_on_start_defaults_to_true():
    watcher = FileWatcherDaemon(files=["a"], command="echo hi")
    assert watcher.run_on_start is True


def test_start_logs_a_startup_message(tmp_path, caplog):
    f1 = tmp_path / "a.txt"
    watcher = FileWatcherDaemon(
        files=[str(f1)], command="echo hi", observer_factory=FakeObserver, run_on_start=False,
    )
    with caplog.at_level("INFO", logger="file_watcher"):
        watcher.start()
    assert "Starting watcher" in caplog.text


@patch("file_watcher.subprocess.run")
def test_start_runs_command_once_by_default(mock_run, tmp_path):
    f1 = tmp_path / "a.txt"
    mock_run.return_value = subprocess.CompletedProcess(args="echo hi", returncode=0, stdout="")
    watcher = FileWatcherDaemon(files=[str(f1)], command="echo hi", observer_factory=FakeObserver)
    watcher.start()
    mock_run.assert_called_once()


@patch("file_watcher.subprocess.run")
def test_start_skips_initial_run_when_run_on_start_false(mock_run, tmp_path):
    f1 = tmp_path / "a.txt"
    watcher = FileWatcherDaemon(
        files=[str(f1)], command="echo hi", observer_factory=FakeObserver, run_on_start=False,
    )
    watcher.start()
    mock_run.assert_not_called()


@patch("file_watcher.subprocess.run")
def test_start_runs_initial_command_before_creating_the_observer(mock_run, tmp_path):
    # Regression guard for the same class of bug fixed by the self-trigger
    # debounce reordering: if the initial run's command writes to a watched
    # file (e.g. remove-browser-extension.js editing the plist it reacts to
    # later), that write must happen before we're actually watching, not
    # after -- otherwise it could be picked up as a spurious first change
    # event. Proven here by asserting watcher._observer is still None at the
    # moment execute_command() is invoked.
    f1 = tmp_path / "a.txt"
    mock_run.return_value = subprocess.CompletedProcess(args="echo hi", returncode=0, stdout="")
    watcher = FileWatcherDaemon(files=[str(f1)], command="echo hi", observer_factory=FakeObserver)

    observer_snapshots = []
    original_execute_command = watcher.execute_command

    def spy_execute_command():
        observer_snapshots.append(watcher._observer)
        return original_execute_command()

    watcher.execute_command = spy_execute_command
    watcher.start()

    assert observer_snapshots == [None]
    assert watcher._observer is not None


@patch("file_watcher.subprocess.run")
def test_end_to_end_handler_triggers_daemon_command(mock_run, tmp_path):
    """Wire a real PatchEventHandler (as start() builds it) to a fake event."""
    f1 = tmp_path / "a.txt"
    watcher = FileWatcherDaemon(
        files=[str(f1)], command="echo hi", observer_factory=FakeObserver, run_on_start=False,
    )
    watcher.start()
    handler, _directory, _recursive = watcher._observer.scheduled[0]
    handler.on_any_event(make_event(str(f1)))
    mock_run.assert_called_once()


@patch("file_watcher.subprocess.run")
def test_command_that_rewrites_watched_file_does_not_self_trigger_loop(mock_run, tmp_path):
    """End-to-end-ish regression test for the remove-browser-extension.js
    scenario: a --command that edits the very file being watched (e.g. the
    managed-preferences plist it's reacting to) must not cause an immediate
    re-trigger once it's done running, and must still respond normally to a
    later, genuinely new external change.
    """
    watched_file = tmp_path / "watched.txt"
    watched_file.write_text("original")
    mock_run.return_value = subprocess.CompletedProcess(args="edit-file", returncode=0, stdout="")

    watcher = FileWatcherDaemon(files=[str(watched_file)], command="edit-file", command_dir=None)
    clock = iter([100.0, 100.0, 100.2, 100.7, 100.7])
    handler = PatchEventHandler(
        watched_paths={os.path.realpath(str(watched_file))},
        on_change=watcher.handle_change,
        debounce_seconds=0.5,
        time_fn=lambda: next(clock),
    )

    # A real, external change -> triggers the command.
    handler.on_any_event(make_event(str(watched_file)))
    assert mock_run.call_count == 1

    # The command's own write to the watched file, arriving 0.2s after it
    # finished (well within the 0.5s debounce window measured from
    # completion). Must NOT re-trigger.
    handler.on_any_event(make_event(str(watched_file)))
    assert mock_run.call_count == 1

    # A later, genuinely new external change, arriving after the debounce
    # window has elapsed -- the fix must not permanently wedge triggering.
    handler.on_any_event(make_event(str(watched_file)))
    assert mock_run.call_count == 2


@patch("file_watcher.subprocess.run")
def test_two_different_watched_paths_changing_together_run_command_once(mock_run, tmp_path):
    """The debounce window is keyed to the command, not to any one watched
    path -- e.g. Chrome writing both the system-wide and per-user
    Managed Preferences plist as part of one policy push must run the
    reacting command once, not once per path.
    """
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("1")
    f2.write_text("2")
    mock_run.return_value = subprocess.CompletedProcess(args="echo hi", returncode=0, stdout="")

    watcher = FileWatcherDaemon(files=[str(f1), str(f2)], command="echo hi", command_dir=None)
    handler = PatchEventHandler(
        watched_paths={os.path.realpath(str(f1)), os.path.realpath(str(f2))},
        on_change=watcher.handle_change,
        debounce_seconds=0.5,
        time_fn=lambda: 100.0,
    )

    handler.on_any_event(make_event(str(f1)))
    handler.on_any_event(make_event(str(f2)))

    mock_run.assert_called_once()
