#!/usr/bin/env python3
"""Executable M2 acceptance proof against the real experimental QEMU."""
from __future__ import annotations

import json
import os
import pathlib
import re
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from protopandaemu.supervisor import (  # noqa: E402
    EXPECTED_FIRMWARE_SHA256,
    ProfileError,
    run_session,
    sha256,
)


FIRMWARE = ROOT / "artifacts/build-normal/release/merged_flash.bin"
BOARD = {
    "chip": "esp32s3",
    "cores": 2,
    "cpu_mhz": 240,
    "flash_bytes": 16 * 1024 * 1024,
    "psram_bytes": 8 * 1024 * 1024,
    "psram_mode": "octal",
}
REBUILD = "Frames.bulk is not present in flash, rebuilding it"
SECOND_BOOT_FORBIDDEN = (
    REBUILD,
    "There is no cache build",
    "Storing ",
    "Generated cache.json",
    "Finished writing data to internal storage",
    "Mismatched cached frames",
    "Mismatched stored frames",
    "Cache seems to be corrupted",
    "Cache structure is corrupted",
    "Version mismatched.",
    "Canvas dimensions changed",
)


class ProofFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProofFailure(message)


def backend_pids() -> set[int]:
    expected = str(ROOT / ".work/qemu-source-unpacked/qemu-esp_develop_9.2.2_20260417-src/build/qemu-system-xtensa")
    found: set[int] = set()
    for entry in pathlib.Path("/proc").glob("[0-9]*"):
        try:
            argv = entry.joinpath("cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if argv and argv[0].decode("utf-8", "replace") == expected:
            found.add(int(entry.name))
    return found


def write_json(path: pathlib.Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def profile_file(path: pathlib.Path, *, incompatible: bool = False,
                 firmware_path: pathlib.Path = FIRMWARE) -> pathlib.Path:
    board = dict(BOARD)
    if incompatible:
        board["chip"] = "esp32c3"
    profile = {
        "firmware_image": str(firmware_path),
        "firmware_sha256": EXPECTED_FIRMWARE_SHA256,
        "board_profile": board,
        "log_level": "info",
        "headless": True,
        "renderer": "none",
        "m2_proof_run_id": path.parent.name,
    }
    write_json(path, profile)
    return path


def copy_case_evidence(run_dir: pathlib.Path, case: str, report: dict) -> dict:
    source = pathlib.Path(report["uart_log_path"])
    require(source.is_file() and source.stat().st_size > 0, f"{case}: UART log missing or empty: {source}")
    destination = run_dir / f"{case}.uart.log"
    shutil.copyfile(source, destination)
    report["uart_log_path"] = str(destination)
    write_json(run_dir / f"{case}.report.json", report)
    return report


def wait_and_stop(handle, case: str, timeout: float) -> dict:
    try:
        reached = handle.wait_for_marker("onLoop", timeout)
        require(reached, f"{case}: bounded wait did not observe real M2_MARKER onLoop completed")
    finally:
        report = handle.stop()
    require(report["exit_reason"] == "stopped", f"{case}: clean stop returned {report['exit_reason']!r}")
    require(report["process_exited"] is True, f"{case}: backend process still alive after stop()")
    require(handle.process.poll() is not None, f"{case}: QEMU child survived stop()")
    return report


def assert_first_boot(run_dir: pathlib.Path, profile: pathlib.Path, flash: pathlib.Path) -> dict:
    require(not flash.exists(), "first boot: flash state unexpectedly existed before initialization")
    handle = run_session(profile, timeout=90, flash_state_path=flash)
    report = wait_and_stop(handle, "first boot", 90)
    require(report["qemu_started"] is True, "first boot: report did not record QEMU start")
    require(report["flash_initial_sha256"] == EXPECTED_FIRMWARE_SHA256,
            "first boot: initial flash was not initialized from the pinned firmware bytes")
    require(report["flash_final_sha256"] != report["flash_initial_sha256"],
            "first boot: clean-stop did not produce a changed flash state")
    require(report["resources"]["heap_free_pct"] != "unavailable: no firmware memory line yet",
            "first boot: resources were not parsed from the guest Memory UART line")
    report = copy_case_evidence(run_dir, "first-boot", report)
    log = pathlib.Path(report["uart_log_path"]).read_text(encoding="utf-8", errors="replace")
    require(REBUILD in log, "first boot: FFat cache miss/rebuild was not observed in raw guest UART")
    require(re.search(r"^Storing m2-idle as [0-9]+$", log, re.MULTILINE) is not None,
            "first boot: completed frame storage was not observed in raw guest UART")
    require("Generated cache.json" in log, "first boot: cache completion was not observed in raw guest UART")
    require("M2_MARKER onPreflight completed" in log and "M2_MARKER onLoop completed" in log,
            "first boot: downstream guest progress markers are incomplete")
    return report


def assert_second_boot(run_dir: pathlib.Path, profile: pathlib.Path, flash: pathlib.Path, first: dict) -> dict:
    require(flash.is_file(), "second boot: stopped flash state is missing")
    before = sha256(flash)
    require(before == first["flash_final_sha256"], "second boot: flash bytes changed after first clean stop")
    handle = run_session(profile, timeout=90, flash_state_path=flash)
    pre_spawn = handle.session_report()["flash_initial_sha256"]
    require(pre_spawn == before, "second boot: supervisor did not record the exact pre-spawn flash hash")
    report = wait_and_stop(handle, "second boot", 90)
    report = copy_case_evidence(run_dir, "second-boot", report)
    log = pathlib.Path(report["uart_log_path"]).read_text(encoding="utf-8", errors="replace")
    forbidden = [event for event in SECOND_BOOT_FORBIDDEN if event in log]
    require(not forbidden, f"second boot: cache was rebuilt or rewritten; observed {forbidden!r}")
    require("M2_MARKER onLoop completed" in log and "M2_MARKER onPreflight completed" in log,
            "second boot: cache-hit run lacked downstream real guest progress")
    return report


def run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PROTOPANDAEMU_QUIET_UART"] = "1"
    return subprocess.run([sys.executable, "-m", "protopandaemu", *args], cwd=ROOT, env=env,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35)


def assert_rejection(run_dir: pathlib.Path, valid_profile: pathlib.Path) -> dict:
    invalid = profile_file(run_dir / "incompatible-profile.json", incompatible=True)
    report_path = run_dir / "rejection.report.json"
    before = backend_pids()
    result = run_cli(["run", "--profile", str(invalid), "--report", str(report_path), "--timeout", "10"])
    after = backend_pids()
    require(result.returncode != 0, "rejection: incompatible profile unexpectedly returned zero")
    require(report_path.is_file(), "rejection: structured report is missing")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    require(report["qemu_started"] is False, "rejection: report says QEMU started")
    require(report["error_code"] == "incompatible_board_profile",
            f"rejection: wrong error code {report.get('error_code')!r}")
    require(not (after - before), f"rejection: backend process-start evidence found: {sorted(after - before)}")
    require("qemu_argv" not in report and "qemu_pid" not in report,
            "rejection: rejected session exposed a spawned backend")
    before_path = backend_pids()
    try:
        run_session(valid_profile, timeout=1, flash_state_path=run_dir / "bad,name.flash.bin")
    except ProfileError as exc:
        require(exc.code == "unsafe_qemu_path", f"path injection: wrong error code {exc.code!r}")
    else:
        raise ProofFailure("path injection: comma-containing flash path was accepted")
    require(not (backend_pids() - before_path), "path injection: backend spawned before rejection")
    private_firmware = run_dir / "private-pinned-firmware.bin"
    shutil.copyfile(FIRMWARE, private_firmware)
    alias_profile = profile_file(run_dir / "alias-profile.json", firmware_path=private_firmware)
    aliases = []
    hardlink = run_dir / "firmware-hardlink.bin"
    os.link(private_firmware, hardlink)
    aliases.append((hardlink, "flash_state_alias"))
    symlink = run_dir / "firmware-symlink.bin"
    symlink.symlink_to(private_firmware)
    aliases.append((symlink, "flash_state_alias"))
    for alias, expected_code in aliases:
        before_alias = backend_pids()
        try:
            run_session(alias_profile, timeout=1, flash_state_path=alias)
        except ProfileError as exc:
            require(exc.code == expected_code,
                    f"flash alias {alias.name}: wrong error code {exc.code!r}")
        else:
            raise ProofFailure(f"flash alias {alias.name}: alias was accepted")
        require(not (backend_pids() - before_alias), f"flash alias {alias.name}: backend spawned")
    return report


def assert_timeout(run_dir: pathlib.Path, valid_profile: pathlib.Path) -> dict:
    before_nonfinite = backend_pids()
    try:
        run_session(valid_profile, timeout=float("nan"), flash_state_path=run_dir / "nan.flash.bin")
    except ProfileError as exc:
        require(exc.code == "timeout_invalid", f"nonfinite timeout: wrong error code {exc.code!r}")
    else:
        raise ProofFailure("nonfinite timeout: NaN was accepted")
    require(not (backend_pids() - before_nonfinite), "nonfinite timeout: backend spawned")
    report_path = run_dir / "timeout.report.json"
    uart_path = run_dir / "timeout.uart.log"
    flash_path = run_dir / "timeout.flash.bin"
    before = backend_pids()
    result = run_cli(["run", "--profile", str(valid_profile), "--report", str(report_path),
                      "--uart-log", str(uart_path), "--flash-state", str(flash_path), "--timeout", "1"])
    after = backend_pids()
    require(result.returncode == 124, f"timeout: expected CLI exit 124, got {result.returncode}; stderr={result.stderr!r}")
    require(report_path.is_file() and uart_path.is_file(), "timeout: report or UART log missing")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    require(report["exit_reason"] == "timeout", f"timeout: wrong exit reason {report.get('exit_reason')!r}")
    require(report["qemu_started"] is True and report["process_exited"] is True,
            "timeout: report does not distinguish a started, terminated backend")
    require(not (after - before), f"timeout: surviving backend process found: {sorted(after - before)}")
    return report


def assert_cli_signal(run_dir: pathlib.Path, valid_profile: pathlib.Path) -> dict:
    report_path = run_dir / "signal.report.json"
    uart_path = run_dir / "signal.uart.log"
    flash_path = run_dir / "signal.flash.bin"
    env = os.environ.copy()
    env.pop("PROTOPANDAEMU_QUIET_UART", None)
    before = backend_pids()
    child = subprocess.Popen(
        [sys.executable, "-m", "protopandaemu", "run", "--profile", str(valid_profile),
         "--report", str(report_path), "--uart-log", str(uart_path),
         "--flash-state", str(flash_path), "--timeout", "90"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    deadline = time.monotonic() + 30
    live_stdout = bytearray()
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([child.stdout], [], [], 0.2)
            if ready:
                block = os.read(child.stdout.fileno(), 4096)
                if block:
                    live_stdout.extend(block)
                    if b"ESP-ROM:" in live_stdout:
                        break
            if child.poll() is not None:
                raise ProofFailure(f"signal: CLI exited before UART began: {child.returncode}")
        else:
            raise ProofFailure("signal: CLI did not produce initial UART before deadline")
        child.send_signal(signal.SIGINT)
        stdout, stderr = child.communicate(timeout=20)
    except BaseException:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
        raise
    after = backend_pids()
    stderr_text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    require(child.returncode == 130, f"signal: expected CLI exit 130, got {child.returncode}; stderr={stderr_text!r}")
    require(report_path.is_file() and uart_path.is_file(), "signal: report or UART log missing")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    require(report["qemu_started"] is True and report["process_exited"] is True,
            "signal: report does not prove backend cleanup")
    require(report["exit_reason"] == "stopped" and report.get("signal") == "SIGINT",
            f"signal: wrong report lifecycle fields: {report}")
    require(not (after - before), f"signal: surviving backend process found: {sorted(after - before)}")
    return report


def main() -> int:
    run_dir = pathlib.Path(tempfile.mkdtemp(prefix="protopandaemu-m2-", dir="/tmp"))
    print(f"M2_PROOF_RUN_DIR={run_dir}", flush=True)
    require(FIRMWARE.is_file(), f"pinned firmware missing: {FIRMWARE}")
    require(sha256(FIRMWARE) == EXPECTED_FIRMWARE_SHA256,
            "pinned firmware hash does not match the repository golden hash")
    valid = profile_file(run_dir / "valid-profile.json")
    flash = run_dir / "flash-state.bin"
    first = assert_first_boot(run_dir, valid, flash)
    second = assert_second_boot(run_dir, valid, flash, first)
    rejection = assert_rejection(run_dir, valid)
    timeout = assert_timeout(run_dir, valid)
    interrupted = assert_cli_signal(run_dir, valid)
    summary = {
        "run_dir": str(run_dir),
        "first_boot": {"exit_reason": first["exit_reason"], "flash_final_sha256": first["flash_final_sha256"]},
        "second_boot": {"exit_reason": second["exit_reason"], "flash_initial_sha256": second["flash_initial_sha256"]},
        "rejection": {"qemu_started": rejection["qemu_started"], "error_code": rejection["error_code"]},
        "timeout": {"exit_reason": timeout["exit_reason"], "qemu_started": timeout["qemu_started"]},
        "signal": {"exit_reason": interrupted["exit_reason"], "signal": interrupted.get("signal")},
    }
    write_json(run_dir / "summary.json", summary)
    print(json.dumps(summary, sort_keys=True))
    print(f"M2_PROOF_ARTIFACT={run_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ProofFailure, ProfileError, subprocess.SubprocessError, OSError) as exc:
        print(f"M2_PROOF_FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
