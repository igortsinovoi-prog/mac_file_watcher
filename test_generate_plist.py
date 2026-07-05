import plistlib

import generate_plist

COMMON_ARGS = [
    "--command", "echo hi",
    "--label", "com.test.watcher",
    "--python", "/venv/bin/python",
    "--daemon-script", "/x/daemon_watcher.py",
    "--log-dir", "/tmp/logs",
    "--output", "/tmp/out.plist",
]


def test_parse_args_collects_multiple_files_and_defaults():
    args = generate_plist.parse_args(["--file", "a.txt", "--file", "b.txt"] + COMMON_ARGS)
    assert args.files == ["a.txt", "b.txt"]
    assert args.debounce == 0.5
    assert args.label == "com.test.watcher"


def test_parse_args_accepts_custom_debounce():
    args = generate_plist.parse_args(
        ["--file", "a.txt", "--debounce", "1.5"] + COMMON_ARGS
    )
    assert args.debounce == 1.5


def test_build_program_arguments_orders_files_then_command_then_debounce():
    args = generate_plist.parse_args(
        ["--file", "a.txt", "--file", "b.txt", "--debounce", "1.5"] + COMMON_ARGS
    )
    result = generate_plist.build_program_arguments(args)
    assert result == [
        "/venv/bin/python", "/x/daemon_watcher.py",
        "--file", "a.txt", "--file", "b.txt",
        "--command", "echo hi", "--debounce", "1.5",
    ]


def test_build_plist_contains_expected_keys(tmp_path):
    args = generate_plist.parse_args([
        "--file", "a.txt",
        "--command", "echo hi",
        "--label", "com.test.watcher",
        "--python", "/venv/bin/python",
        "--daemon-script", "/x/daemon_watcher.py",
        "--log-dir", str(tmp_path),
        "--output", str(tmp_path / "out.plist"),
    ])
    plist_data = generate_plist.build_plist(args)
    assert plist_data["Label"] == "com.test.watcher"
    assert plist_data["RunAtLoad"] is True
    assert plist_data["KeepAlive"] is True
    assert plist_data["StandardOutPath"] == str(tmp_path / "com.test.watcher.out.log")
    assert plist_data["StandardErrorPath"] == str(tmp_path / "com.test.watcher.err.log")
    assert plist_data["ProgramArguments"][0] == "/venv/bin/python"


def test_write_plist_roundtrips_via_plistlib(tmp_path):
    output = tmp_path / "out.plist"
    data = {"Label": "x", "ProgramArguments": ["a", "b"], "RunAtLoad": True, "KeepAlive": True}
    generate_plist.write_plist(data, str(output))
    with open(output, "rb") as handle:
        loaded = plistlib.load(handle)
    assert loaded == data


def test_main_writes_plist_file(tmp_path):
    output = tmp_path / "out.plist"
    generate_plist.main([
        "--file", "watched.txt",
        "--command", "echo hi",
        "--label", "com.test.watcher",
        "--python", "/venv/bin/python",
        "--daemon-script", "/x/daemon_watcher.py",
        "--log-dir", str(tmp_path / "logs"),
        "--output", str(output),
    ])
    assert output.exists()
    with open(output, "rb") as handle:
        loaded = plistlib.load(handle)
    assert loaded["Label"] == "com.test.watcher"
    assert loaded["ProgramArguments"] == [
        "/venv/bin/python", "/x/daemon_watcher.py",
        "--file", "watched.txt",
        "--command", "echo hi", "--debounce", "0.5",
    ]
