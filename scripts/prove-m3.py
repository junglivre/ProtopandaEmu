#!/usr/bin/env python3
"""Single-owner M3 acceptance proof.

This is deliberately an executable behavioral proof.  The positive cases use
the unchanged firmware through the shared supervisor; the filesystem cases
exercise the same hostdir implementation and real mtools FAT images.  A
failure stops the driver and returns nonzero; no later case can mask it.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from protopandaemu.hostdir import (  # noqa: E402
    HostDirError,
    HostDirSession,
    InjectedCrash,
    manifest,
    recover_hostdir,
)
from protopandaemu.supervisor import (  # noqa: E402
    EXPECTED_FIRMWARE_SHA256,
    run_session,
    sha256,
)


FIRMWARE = ROOT / "artifacts/build-normal/release/merged_flash.bin"
UPSTREAM = ROOT / ".work/upstream-v3.4.0"
BOARD = {
    "chip": "esp32s3", "cores": 2, "cpu_mhz": 240,
    "flash_bytes": 16 * 1024 * 1024, "psram_bytes": 8 * 1024 * 1024,
    "psram_mode": "octal",
}
REQUIRED_LOGS = (
    "Loading display info",
    "memmory_difference msg=\"init.lua\"",
    "Frame 0 was compressed to just",
    "Generated cache.json",
)


class ProofFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProofFailure(message)


def write_json(path: pathlib.Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def png_solid(path: pathlib.Path, width: int = 64, height: int = 32) -> None:
    # A real RGB PNG with long same-color runs.  The firmware therefore logs
    # its own PNG decode/RLE conversion as compressed, rather than using the
    # raw fixture observed in the M2 proof.
    import zlib

    rows = b"".join(b"\0" + bytes((220, 40, 90)) * width for _ in range(height))
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload +
                struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff))
    data = b"\x89PNG\r\n\x1a\n"
    data += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(rows, 9))
    data += chunk(b"IEND", b"")
    path.write_bytes(data)


def fixture(root: pathlib.Path, sentinel: str, *, conflict: bool = False) -> None:
    require((UPSTREAM / "init.lua").is_file(), f"pinned upstream missing: {UPSTREAM}")
    shutil.copytree(UPSTREAM / "lualib", root / "lualib")
    (root / "frames").mkdir()
    (root / "cache").mkdir()
    (root / "hardware.json").write_text(
        '{"canvas_width":64,"canvas_height":32,"display":{"type":"none","enabled":false}}\n',
        encoding="utf-8")
    (root / "animation.json").write_text(
        '{"frames":[{"name":"m3-solid","file":"/frames/solid.png"}],'
        '"expressions":[{"name":"m3-solid","frames":"m3-solid","animation":"loop"}]}\n',
        encoding="utf-8")
    (root / "wifi.json").write_text("{}\n", encoding="utf-8")
    (root / "keybinds.json").write_text(
        '{"input":{"mode":"NONE","drivers":["generic"]},"keybinds":{}}\n', encoding="utf-8")
    (root / "misc.json").write_text(
        '{"overlays":{},"servos":{},"leds":{"pin_mode":"double","groups":[]},'
        '"boop":{"enabled":false},"fft":{"enabled":false},"scripts":{}}\n', encoding="utf-8")
    (root / "models.json").write_text("{}\n", encoding="utf-8")
    png_solid(root / "frames/solid.png")
    if conflict:
        (root / "conflict.json").write_text("host-base\n", encoding="utf-8")
    original = (UPSTREAM / "init.lua").read_text(encoding="utf-8")
    conflict_code = """
local m3_conflict = io.open("/conflict.json", "w")
if m3_conflict then
    m3_conflict:write("guest-version-%s\\n")
    m3_conflict:close()
end
""" % sentinel if conflict else ""
    marker_code = """
local m3_sentinel_file = io.open("/cache/m3-sentinel.txt", "r")
if m3_sentinel_file then
    local m3_value = m3_sentinel_file:read("*a")
    m3_sentinel_file:close()
    print("M3_GUEST_READ_SENTINEL " .. m3_value)
else
    m3_sentinel_file = io.open("/cache/m3-sentinel.txt", "w")
    m3_sentinel_file:write("%s")
    m3_sentinel_file:close()
    print("M3_GUEST_CREATED_SENTINEL %s")
end
print("M3_MARKER onSetup completed")
""" % (sentinel, sentinel)
    lua = original + "\n" + "local m3_original_onSetup = onSetup\n" \
        "function onSetup()\n" \
        "  m3_original_onSetup()\n" + marker_code + conflict_code + "end\n" \
        "function onPreflight() print(\"M3_MARKER onPreflight completed\") end\n" \
        "local m3_loop_marked = false\n" \
        "function onLoop(dt) if not m3_loop_marked then print(\"M3_MARKER onLoop completed\"); m3_loop_marked = true end end\n"
    (root / "init.lua").write_text(lua, encoding="utf-8")


def profile(path: pathlib.Path) -> pathlib.Path:
    write_json(path, {
        "firmware_image": str(FIRMWARE),
        "firmware_sha256": EXPECTED_FIRMWARE_SHA256,
        "board_profile": BOARD,
        "log_level": "info", "headless": True, "renderer": "none",
        "m3_proof_run_id": path.parent.name,
    })
    return path


def copy_report(run_dir: pathlib.Path, name: str, report: dict) -> tuple[dict, str]:
    source = pathlib.Path(report["uart_log_path"])
    require(source.is_file() and source.stat().st_size > 0, f"{name}: missing UART evidence")
    target = run_dir / f"{name}.uart.log"
    shutil.copyfile(source, target)
    report = dict(report)
    report["uart_log_path"] = str(target)
    write_json(run_dir / f"{name}.report.json", report)
    return report, target.read_text(encoding="utf-8", errors="replace")


def stop_clean(handle, name: str) -> dict:
    require(handle.wait_for_marker("onLoop", 120), f"{name}: bounded real guest onLoop marker missing")
    report = handle.stop()
    require(report["exit_reason"] == "stopped", f"{name}: expected clean stop, got {report['exit_reason']!r}")
    require(report["process_exited"] is True and handle.process.poll() is not None,
            f"{name}: QEMU child survived clean stop")
    require("-snapshot" not in report["qemu_argv"], f"{name}: snapshot mode was enabled")
    return report


def assert_positive_boot(run_dir: pathlib.Path, prof: pathlib.Path, host: pathlib.Path,
                         flash: pathlib.Path, sentinel: str, name: str,
                         require_conversion: bool = True) -> tuple[dict, str]:
    handle = run_session(prof, hostdir=host, timeout=120, flash_state_path=flash)
    report = stop_clean(handle, name)
    report, log = copy_report(run_dir, name, report)
    required_logs = REQUIRED_LOGS if require_conversion else REQUIRED_LOGS[:2]
    for required in required_logs:
        require(required in log, f"{name}: missing real UART evidence: {required}")
    require(f"M3_GUEST_CREATED_SENTINEL {sentinel}" in log or
            f"M3_GUEST_READ_SENTINEL {sentinel}" in log,
            f"{name}: Lua io.open/read/write sentinel evidence missing")
    require(report.get("hostdir", {}).get("sync_status") == "committed",
            f"{name}: hostdir sync did not commit: {report.get('hostdir')}")
    require((host / "cache/m3-sentinel.txt").read_text(encoding="utf-8") == sentinel,
            f"{name}: exact guest sentinel did not reach host")
    require(sha256(host / "hardware.json") == report["hostdir"]["start_manifest"]["hardware.json"],
            f"{name}: host hardware.json was changed")
    require(sha256(host / "init.lua") == report["hostdir"]["start_manifest"]["init.lua"],
            f"{name}: host init.lua was changed")
    require((host / "lualib").is_dir(), f"{name}: chosen lualib tree was not staged")
    return report, log


def expect_code(action, expected: str, label: str) -> None:
    try:
        action()
    except HostDirError as exc:
        require(exc.code == expected, f"{label}: expected {expected}, got {exc.code}: {exc}")
    else:
        raise ProofFailure(f"{label}: expected {expected} but operation succeeded")


def build_fixture_session(root: pathlib.Path, work: pathlib.Path, session_id: str) -> tuple[HostDirSession, pathlib.Path]:
    session_dir = work / session_id
    session_dir.mkdir()
    session = HostDirSession.open(root, session_dir, session_id)
    return session, session.build_image(session_dir / "sd-card.img")


def add_guest_file(image: pathlib.Path, work: pathlib.Path, name: str, content: str) -> None:
    source = work / ("guest-" + name.replace("/", "-"))
    source.write_text(content, encoding="utf-8")
    subprocess.run(["mcopy", "-D", "o", "-i", str(image), str(source), "::" + name],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def error_suite(run_dir: pathlib.Path) -> dict[str, str]:
    root = run_dir / "error-host"; root.mkdir(); fixture(root, "errors")
    missing = run_dir / "missing-host"
    expect_code(lambda: manifest(missing), "hostdir_missing", "missing folder")
    original_animation = (root / "animation.json").read_bytes()

    def build_expect(code: str, label: str) -> None:
        session_dir = run_dir / ("error-" + label.replace(" ", "-")); session_dir.mkdir()
        try:
            session = HostDirSession.open(root, session_dir, label.replace(" ", "-"))
            try:
                expect_code(lambda: session.build_image(session_dir / "image.img"), code, label)
            finally:
                session.release()
        finally:
            pass

    (root / "animation.json").write_text("{broken\n", encoding="utf-8")
    build_expect("malformed_json", "malformed json")
    (root / "animation.json").write_bytes(original_animation)
    (root / "animation.json").write_text(
        '{"frames":[{"name":"missing","file":"/frames/missing.png"}],"expressions":[]}\n', encoding="utf-8")
    build_expect("png_missing", "missing png")
    (root / "animation.json").write_bytes(original_animation)
    (root / "frames/solid.png").write_bytes(b"not-a-png")
    build_expect("png_invalid", "invalid png")
    (root / "frames/solid.png").unlink()
    png_solid(root / "frames/solid.png")

    session_dir = run_dir / "error-source-change"; session_dir.mkdir()
    session = HostDirSession.open(root, session_dir, "source-change")
    (root / "hardware.json").write_text('{"changed":true}\n', encoding="utf-8")
    expect_code(lambda: session.build_image(session_dir / "image.img"), "source_tree_changed", "source tree changed")
    session.release()
    (root / "hardware.json").write_text(
        '{"canvas_width":64,"canvas_height":32,"display":{"type":"none","enabled":false}}\n', encoding="utf-8")

    escape = root / "escape"; escape.symlink_to("/tmp")
    expect_code(lambda: HostDirSession.open(root, run_dir / "error-symlink", "symlink"),
                "symlink_escape", "symlink escape")
    escape.unlink()

    swap_dir = run_dir / "error-symlink-swap"; swap_dir.mkdir()
    swap = HostDirSession.open(root, swap_dir, "symlink-swap")
    saved = (root / "hardware.json").read_bytes(); (root / "hardware.json").unlink()
    (root / "hardware.json").symlink_to("/tmp")
    expect_code(lambda: swap.build_image(swap_dir / "image.img"), "symlink_escape", "symlink swap")
    swap.release(); (root / "hardware.json").unlink(); (root / "hardware.json").write_bytes(saved)

    permission_root = run_dir / "permission-host"; permission_root.mkdir(); fixture(permission_root, "permission")
    old_mode = permission_root.stat().st_mode & 0o777
    os.chmod(permission_root, 0)
    try:
        expect_code(lambda: manifest(permission_root), "hostdir_permission_denied", "permission denied")
    finally:
        os.chmod(permission_root, old_mode)

    # An actual kernel file-size boundary produces EFBIG while creating the
    # 64 MiB image; hostdir maps that filesystem failure to disk_full.
    import resource
    full_root = run_dir / "disk-full-host"; full_root.mkdir(); fixture(full_root, "disk-full")
    full_dir = run_dir / "error-disk-full"; full_dir.mkdir()
    full_session = HostDirSession.open(full_root, full_dir, "disk-full")
    old_limit = resource.getrlimit(resource.RLIMIT_FSIZE)
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (4096, old_limit[1]))
        expect_code(lambda: full_session.build_image(full_dir / "image.img"), "disk_full", "disk full")
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, (old_limit[0], old_limit[1]))
        full_session.release()
    return {"missing": "hostdir_missing", "permission": "hostdir_permission_denied",
            "malformed_json": "malformed_json", "missing_png": "png_missing",
            "invalid_png": "png_invalid", "source_tree_changed": "source_tree_changed",
            "symlink_escape": "symlink_escape", "symlink_swap": "symlink_escape", "disk_full": "disk_full"}


def conflict_suite(run_dir: pathlib.Path) -> dict[str, object]:
    root = run_dir / "conflict-host"; root.mkdir(); fixture(root, "conflict-one", conflict=True)
    prof = profile(run_dir / "conflict-profile.json")
    flash = run_dir / "conflict.flash.bin"
    retained: list[pathlib.Path] = []
    for index in (1, 2):
        handle = run_session(prof, hostdir=root, timeout=120, flash_state_path=flash)
        require(handle.wait_for_marker("rom", 60), f"conflict {index}: real guest never booted")
        (root / "conflict.json").write_text(f"host-edit-{index}\n", encoding="utf-8")
        report = stop_clean(handle, f"conflict-{index}")
        require((root / "conflict.json").read_text(encoding="utf-8") == f"host-edit-{index}\n",
                f"conflict {index}: host edit was overwritten")
        entries = report.get("hostdir", {}).get("conflicts", [])
        require(entries, f"conflict {index}: no retained guest version in report")
        retained.extend(pathlib.Path(item["retained_path"]) for item in entries)
    files = sorted(root.glob("conflict.json.protopandaemu-conflict-*"))
    require(len(files) >= 2, "conflict: repeated unique siblings were not retained")
    require(all("guest-version-conflict-one" in p.read_text(encoding="utf-8") for p in files),
            "conflict: retained sibling does not contain the guest version")
    return {"retained_paths": [str(p) for p in files], "host_value": (root / "conflict.json").read_text()}


def recovery_suite(run_dir: pathlib.Path) -> dict[str, object]:
    points = ("after_backup_fsync", "after_temp_fsync", "after_journal_fsync",
              "after_journal_publish", "after_journal_dir_fsync", "after_rename", "after_journal_remove")
    outcomes: dict[str, str] = {}
    for index, point in enumerate(points):
        root = run_dir / f"recovery-{index}"; root.mkdir(); fixture(root, f"recovery-{index}")
        session_id = f"recovery-{index}-session"
        (root / "guest-target.txt").write_text("host-old\n", encoding="utf-8")
        session, image = build_fixture_session(root, run_dir, session_id)
        add_guest_file(image, run_dir, "guest-target.txt", "guest-new\n")
        try:
            try:
                session.sync(image, fault_at=point)
            except InjectedCrash:
                pass
            else:
                raise ProofFailure(f"recovery {point}: fault injection did not interrupt sync")
        finally:
            session.release()
        durable = (root / ".protopandaemu-journal.json").is_file()
        recovered = recover_hostdir(root)
        recovered_again = recover_hostdir(root)
        expected = "guest-new\n" if durable or point in {"after_journal_remove"} else "host-old\n"
        require((root / "guest-target.txt").read_text(encoding="utf-8") == expected,
                f"recovery {point}: unexpected target after recovery")
        require(recovered_again is False, f"recovery {point}: repeated recovery changed state")
        require(list(root.glob("guest-target.txt.protopandaemu-backup-*")),
                f"recovery {point}: mandatory backup missing")
        outcomes[point] = "committed" if expected.startswith("guest") else "pre-journal-preserved"
    return outcomes


def main() -> int:
    run_dir = pathlib.Path(tempfile.mkdtemp(prefix="protopandaemu-m3-", dir="/tmp"))
    print(f"M3_PROOF_RUN_DIR={run_dir}", flush=True)
    try:
        require(FIRMWARE.is_file(), f"pinned firmware missing: {FIRMWARE}")
        require(sha256(FIRMWARE) == EXPECTED_FIRMWARE_SHA256, "pinned firmware hash mismatch")
        prof = profile(run_dir / "profile.json")
        sentinel = uuid.uuid4().hex
        host = run_dir / "host"; host.mkdir(); fixture(host, sentinel)
        flash = run_dir / "flash-state.bin"
        first, first_log = assert_positive_boot(run_dir, prof, host, flash, sentinel, "first-boot")
        second, second_log = assert_positive_boot(run_dir, prof, host, flash, sentinel, "second-boot",
                                                  require_conversion=False)
        require(f"M3_GUEST_READ_SENTINEL {sentinel}" in second_log,
                "second boot did not read the persisted host sentinel through Lua")
        errors = error_suite(run_dir)
        conflicts = conflict_suite(run_dir)
        recovery = recovery_suite(run_dir)
        summary = {
            "run_dir": str(run_dir), "firmware_sha256": EXPECTED_FIRMWARE_SHA256,
            "first_boot": {"exit_reason": first["exit_reason"], "sync": first["hostdir"]["sync_status"]},
            "second_boot": {"exit_reason": second["exit_reason"], "sync": second["hostdir"]["sync_status"]},
            "errors": errors, "conflicts": conflicts, "recovery": recovery,
            "deviations": [
                "The approved C/FAT fallback is used because the experimental QEMU SD controller has no documented direct host-VFS hook.",
                "Atomic visibility across multiple host files is not claimed; journal recovery makes the commit whole after recovery.",
                "OS process sandboxing and rendering/UI remain outside M3.",
            ],
        }
        write_json(run_dir / "summary.json", summary)
        print(json.dumps(summary, sort_keys=True))
        print(f"M3_PROOF_ARTIFACT={run_dir / 'summary.json'}")
        return 0
    except (ProofFailure, HostDirError, OSError, subprocess.SubprocessError) as exc:
        write_json(run_dir / "failure.json", {"run_dir": str(run_dir), "error": str(exc),
                                               "type": type(exc).__name__})
        print(f"M3_PROOF_FAIL: {exc}", file=sys.stderr)
        print(f"M3_PROOF_ARTIFACT={run_dir / 'failure.json'}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
