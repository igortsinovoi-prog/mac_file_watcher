import logging
import os
import signal
import threading
from unittest.mock import MagicMock, call, patch

import pytest

import daemon_watcher


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------

def test_parse_args_defaults():
    args = daemon_watcher.parse_args(["-f", "a.txt", "-c", "echo hi"])
    assert args.files == ["a.txt"]
    assert args.command == "echo hi"
    assert args.debounce == 0.1
    assert args.skip_initial_run is False
    assert args.daemon is False
    assert args.pidfile == daemon_watcher.DEFAULT_PIDFILE
    assert args.log_file is None


def test_parse_args_multiple_files_and_overrides():
    args = daemon_watcher.parse_args([
        "-f", "a.txt", "-f", "b.txt", "-c", "echo hi",
        "--debounce", "1.5", "--skip-initial-run",
        "--daemon", "--pidfile", "/tmp/x.pid", "--log-file", "/tmp/x.log",
    ])
    assert args.files == ["a.txt", "b.txt"]
    assert args.debounce == 1.5
    assert args.skip_initial_run is True
    assert args.daemon is True
    assert args.pidfile == "/tmp/x.pid"
    assert args.log_file == "/tmp/x.log"


# ---------------------------------------------------------------------------
# configure_logging
# ---------------------------------------------------------------------------

def test_configure_logging_without_log_file():
    with patch("daemon_watcher.logging.basicConfig") as mock_config, \
         patch("daemon_watcher.logging.StreamHandler") as mock_stream:
        daemon_watcher.configure_logging(None)
    mock_stream.assert_called_once_with(daemon_watcher.sys.stdout)
    mock_config.assert_called_once()


def test_configure_logging_with_log_file(tmp_path):
    log_file = str(tmp_path / "out.log")
    with patch("daemon_watcher.logging.basicConfig") as mock_config, \
         patch("daemon_watcher.logging.FileHandler") as mock_file_handler:
        daemon_watcher.configure_logging(log_file)
    mock_file_handler.assert_called_once_with(log_file)
    mock_config.assert_called_once()


# ---------------------------------------------------------------------------
# pidfile helpers
# ---------------------------------------------------------------------------

def test_write_pidfile_writes_current_pid(tmp_path):
    pidfile = str(tmp_path / "test.pid")
    daemon_watcher.write_pidfile(pidfile)
    with open(pidfile) as handle:
        assert handle.read() == str(os.getpid())


def test_remove_pidfile_removes_existing_file(tmp_path):
    pidfile = str(tmp_path / "test.pid")
    with open(pidfile, "w") as handle:
        handle.write("123")
    daemon_watcher.remove_pidfile(pidfile)
    assert not os.path.exists(pidfile)


def test_remove_pidfile_swallows_missing_file(tmp_path):
    pidfile = str(tmp_path / "does_not_exist.pid")
    daemon_watcher.remove_pidfile(pidfile)  # must not raise


# ---------------------------------------------------------------------------
# daemonize
# ---------------------------------------------------------------------------

def test_daemonize_first_parent_exits():
    with patch("daemon_watcher.os.fork", return_value=123) as mock_fork, \
         patch("daemon_watcher.sys.exit", side_effect=SystemExit) as mock_exit:
        with pytest.raises(SystemExit):
            daemon_watcher.daemonize("/tmp/whatever.pid")
    mock_fork.assert_called_once()
    mock_exit.assert_called_once_with(0)


def test_daemonize_second_parent_exits():
    with patch("daemon_watcher.os.fork", side_effect=[0, 456]) as mock_fork, \
         patch("daemon_watcher.os.setsid") as mock_setsid, \
         patch("daemon_watcher.sys.exit", side_effect=SystemExit) as mock_exit:
        with pytest.raises(SystemExit):
            daemon_watcher.daemonize("/tmp/whatever.pid")
    assert mock_fork.call_count == 2
    mock_setsid.assert_called_once()
    mock_exit.assert_called_once_with(0)


def test_daemonize_child_detaches_and_writes_pidfile(tmp_path, monkeypatch):
    pidfile = str(tmp_path / "daemon.pid")
    fake_fd = 99

    for name, fd in (("stdin", 0), ("stdout", 1), ("stderr", 2)):
        fake_stream = MagicMock()
        fake_stream.fileno.return_value = fd
        monkeypatch.setattr(daemon_watcher.sys, name, fake_stream)

    with patch("daemon_watcher.os.fork", side_effect=[0, 0]), \
         patch("daemon_watcher.os.setsid") as mock_setsid, \
         patch("daemon_watcher.os.open", return_value=fake_fd) as mock_open, \
         patch("daemon_watcher.os.dup2") as mock_dup2, \
         patch("daemon_watcher.atexit.register") as mock_atexit:
        daemon_watcher.daemonize(pidfile)

    mock_setsid.assert_called_once()
    mock_open.assert_called_once_with(os.devnull, os.O_RDWR)
    assert mock_dup2.call_count == 3
    with open(pidfile) as handle:
        assert handle.read() == str(os.getpid())
    mock_atexit.assert_called_once_with(daemon_watcher.remove_pidfile, pidfile)


# ---------------------------------------------------------------------------
# install_signal_handlers
# ---------------------------------------------------------------------------

def test_install_signal_handlers_sets_term_and_int():
    stop_event = threading.Event()
    with patch("daemon_watcher.signal.signal") as mock_signal:
        daemon_watcher.install_signal_handlers(stop_event)

    calls = mock_signal.call_args_list
    signals_registered = [c.args[0] for c in calls]
    assert signal.SIGTERM in signals_registered
    assert signal.SIGINT in signals_registered

    # Invoke one of the registered handlers and confirm it sets the event.
    handler = calls[0].args[1]
    assert stop_event.is_set() is False
    handler(signal.SIGTERM, None)
    assert stop_event.is_set() is True


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def test_main_without_daemon_starts_and_stops_watcher():
    fake_watcher = MagicMock()

    def fake_wait_sets_event(stop_event_holder):
        pass

    with patch("daemon_watcher.daemonize") as mock_daemonize, \
         patch("daemon_watcher.configure_logging") as mock_configure_logging, \
         patch("daemon_watcher.FileWatcherDaemon", return_value=fake_watcher) as mock_cls, \
         patch("daemon_watcher.threading.Event") as mock_event_cls:
        mock_event = MagicMock()
        mock_event_cls.return_value = mock_event
        daemon_watcher.main(["-f", "a.txt", "-c", "echo hi"])

    mock_daemonize.assert_not_called()
    mock_configure_logging.assert_called_once_with(None)
    mock_cls.assert_called_once_with(
        files=["a.txt"], command="echo hi", debounce_seconds=0.1, run_on_start=True,
    )
    fake_watcher.start.assert_called_once()
    mock_event.wait.assert_called_once()
    fake_watcher.stop.assert_called_once()


def test_main_passes_run_on_start_false_when_skip_initial_run_flag_set():
    fake_watcher = MagicMock()
    with patch("daemon_watcher.daemonize"), \
         patch("daemon_watcher.configure_logging"), \
         patch("daemon_watcher.FileWatcherDaemon", return_value=fake_watcher) as mock_cls, \
         patch("daemon_watcher.threading.Event") as mock_event_cls:
        mock_event_cls.return_value = MagicMock()
        daemon_watcher.main(["-f", "a.txt", "-c", "echo hi", "--skip-initial-run"])

    mock_cls.assert_called_once_with(
        files=["a.txt"], command="echo hi", debounce_seconds=0.1, run_on_start=False,
    )


def test_main_with_daemon_flag_calls_daemonize():
    fake_watcher = MagicMock()
    with patch("daemon_watcher.daemonize") as mock_daemonize, \
         patch("daemon_watcher.configure_logging"), \
         patch("daemon_watcher.FileWatcherDaemon", return_value=fake_watcher), \
         patch("daemon_watcher.threading.Event") as mock_event_cls:
        mock_event_cls.return_value = MagicMock()
        daemon_watcher.main(["-f", "a.txt", "-c", "echo hi", "--daemon", "--pidfile", "/tmp/x.pid"])

    mock_daemonize.assert_called_once_with("/tmp/x.pid")


def test_main_stops_watcher_even_if_wait_raises():
    fake_watcher = MagicMock()
    mock_event = MagicMock()
    mock_event.wait.side_effect = KeyboardInterrupt

    with patch("daemon_watcher.daemonize"), \
         patch("daemon_watcher.configure_logging"), \
         patch("daemon_watcher.FileWatcherDaemon", return_value=fake_watcher), \
         patch("daemon_watcher.threading.Event", return_value=mock_event):
        with pytest.raises(KeyboardInterrupt):
            daemon_watcher.main(["-f", "a.txt", "-c", "echo hi"])

    fake_watcher.stop.assert_called_once()
