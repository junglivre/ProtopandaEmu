"""Thin CLI wrapper for :mod:`protopandaemu.supervisor`."""
from __future__ import annotations

import argparse
import json
import pathlib
import signal
import shutil
import sys

from .hostdir import HostDirError, recover_hostdir
from .supervisor import ProfileError, run_session


def _write_json(path: pathlib.Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="protopandaemu")
    subparsers = parser.add_subparsers(dest="command")
    run = subparsers.add_parser("run", help="run the pinned Proto-panda firmware headlessly")
    run.add_argument("--profile", required=True, type=pathlib.Path)
    run.add_argument("--hostdir", type=pathlib.Path)
    run.add_argument("--timeout", type=float, default=30.0)
    run.add_argument("--flash-state", type=pathlib.Path)
    run.add_argument("--report", type=pathlib.Path, required=True)
    run.add_argument("--uart-log", type=pathlib.Path)
    fsck = subparsers.add_parser(
        "fsck-hostdir",
        help="complete or roll back an interrupted host-folder sync without starting a session")
    fsck.add_argument("hostdir", type=pathlib.Path)
    args = parser.parse_args(argv)
    if args.command == "fsck-hostdir":
        try:
            recovered = recover_hostdir(args.hostdir)
        except HostDirError as exc:
            print(json.dumps({"hostdir": str(args.hostdir), "recovered": False, "error_code": exc.code,
                              "error": str(exc)}))
            return 1
        print(json.dumps({"hostdir": str(args.hostdir), "recovered": recovered}))
        return 0
    if args.command != "run":
        parser.error("a command is required (use: protopandaemu run --profile PROFILE --report REPORT, "
                     "or protopandaemu fsck-hostdir HOSTDIR)")
    try:
        handle = run_session(args.profile, hostdir=args.hostdir, timeout=args.timeout,
                             flash_state_path=args.flash_state)
    except ProfileError as exc:
        report = exc.report()
        _write_json(args.report, report)
        print(json.dumps({"report": str(args.report), "exit_reason": report["exit_reason"],
                          "qemu_started": False, "error_code": report["error_code"]}))
        return 2
    signal_state: dict[str, int | None] = {"signum": None}

    def interrupt(signum: int, _frame: object) -> None:
        signal_state["signum"] = signum
        handle.stop()

    old_handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    try:
        if handle.wait(timeout=max(args.timeout + 10.0, 15.0)):
            report = handle.session_report()
        else:
            report = handle.stop()
    finally:
        for signum, previous in old_handlers.items():
            signal.signal(signum, previous)
    if signal_state["signum"] is not None:
        signal_name = signal.Signals(signal_state["signum"]).name
        report["signal"] = signal_name
        report["error_code"] = f"interrupted_{signal_name}"
    if args.uart_log:
        args.uart_log.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(report["uart_log_path"], args.uart_log)
        report["uart_log_path"] = str(args.uart_log)
    _write_json(args.report, report)
    print(json.dumps({"report": str(args.report), "exit_reason": report["exit_reason"],
                      "qemu_started": report["qemu_started"], "session_id": report["session_id"]}))
    if signal_state["signum"] is not None:
        return 128 + int(signal_state["signum"])
    return 0 if report["exit_reason"] == "stopped" else 124 if report["exit_reason"] == "timeout" else 1


if __name__ == "__main__":
    raise SystemExit(main())
