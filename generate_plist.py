"""Generate a launchd LaunchDaemon plist for the file watcher daemon.

Kept as a small, testable module: install.sh shells out to this script instead
of hand-rolling plist XML (and its escaping rules) in bash.
"""
from __future__ import annotations

import argparse
import os
import plistlib
from typing import Dict, List, Optional, Sequence


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", dest="files", action="append", required=True,
                         help="Path to watch. Can be given multiple times.")
    parser.add_argument("--command", required=True,
                         help="Shell command to run when a watched file changes.")
    parser.add_argument("--debounce", type=float, default=0.5)
    parser.add_argument("--label", required=True, help="launchd job label.")
    parser.add_argument("--python", required=True, help="Path to the python executable to run the daemon with.")
    parser.add_argument("--daemon-script", required=True, help="Path to daemon_watcher.py.")
    parser.add_argument("--log-dir", required=True, help="Directory for stdout/stderr logs.")
    parser.add_argument("--output", required=True, help="Where to write the .plist file.")
    return parser.parse_args(argv)


def build_program_arguments(args: argparse.Namespace) -> List[str]:
    program_arguments = [args.python, args.daemon_script]
    for path in args.files:
        program_arguments += ["--file", path]
    log_file = os.path.join(args.log_dir, f"{args.label}.log")
    program_arguments += [
        "--command", args.command,
        "--debounce", str(args.debounce),
        "--log-file", log_file,
    ]
    return program_arguments


def build_plist(args: argparse.Namespace) -> Dict:
    return {
        "Label": args.label,
        "ProgramArguments": build_program_arguments(args),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": os.path.join(args.log_dir, f"{args.label}.out.log"),
        "StandardErrorPath": os.path.join(args.log_dir, f"{args.label}.err.log"),
    }


def write_plist(plist_data: Dict, output_path: str) -> None:
    with open(output_path, "wb") as handle:
        plistlib.dump(plist_data, handle)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    write_plist(build_plist(args), args.output)


if __name__ == "__main__":  # pragma: no cover
    main()
