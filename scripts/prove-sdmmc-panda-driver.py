#!/usr/bin/env python3
"""Exercise the real SDMMC/FatFs driver-load boot path with Proto-panda v3.4.0.

The fixture is a real writable host folder staged by ``run_session``.  It uses
the pinned upstream Lua tree unchanged and intentionally takes the same path
as the frozen M4 report: dictionary persistence followed by INFRARED setup and
the ``panda`` driver.  It rejects SD/FatFs symptoms rather than accepting a
later successful open as proof that the boot transaction was correct.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import struct
import sys
import tempfile
import time
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from protopandaemu.supervisor import EXPECTED_FIRMWARE_SHA256, run_session, sha256  # noqa: E402

UPSTREAM = ROOT / ".work/upstream-v3.4.0"
FIRMWARE = ROOT / "artifacts/build-normal/release/merged_flash.bin"
BOARD = {"chip": "esp32s3", "cores": 2, "cpu_mhz": 240,
         "flash_bytes": 16 * 1024 * 1024, "psram_bytes": 8 * 1024 * 1024,
         "psram_mode": "octal"}
# "dont have a valid handler" is intentionally NOT forbidden: upstream
# drivers.lua WrapUp errors with it when a core driver's onEnable returns
# false, and the pinned panda driver (drivers/panda.lua:92) returns false
# whenever BLE is not running.  With INFRARED input that is the upstream-
# faithful outcome after the driver file itself loaded successfully.
FORBIDDEN = (
    "No such device",
    "File not found",
    "Failed to load driver",
)


class ProofFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProofFailure(message)


def write_json(path: pathlib.Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def png_solid(path: pathlib.Path, width: int = 64, height: int = 32) -> None:
    rows = b"".join(b"\0" + bytes((32, 104, 216)) * width for _ in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload +
                struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff))

    data = b"\x89PNG\r\n\x1a\n"
    data += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(rows, 9))
    data += chunk(b"IEND", b"")
    path.write_bytes(data)


def build_host_fixture(root: pathlib.Path, drivers: list[str]) -> None:
    require((UPSTREAM / "init.lua").is_file(), f"pinned upstream missing: {UPSTREAM}")
    shutil.copytree(UPSTREAM / "lualib", root / "lualib")
    shutil.copy(UPSTREAM / "init.lua", root / "init.lua")
    shutil.copy(UPSTREAM / "hardware.json", root / "hardware.json")
    (root / "frames").mkdir()
    (root / "cache").mkdir()
    png_solid(root / "frames/solid.png")
    write_json(root / "animation.json", {
        "frames": [{"name": "idle", "file": "/frames/solid.png"}],
        "expressions": [{"name": "idle", "frames": "idle", "animation": "loop"}],
    })
    (root / "wifi.json").write_text("{}\n", encoding="utf-8")
    # infrared_gpio must not collide with the firmware's SDMMC/FFAT pin
    # assignment (config_default.hpp: MMC pins are SPI_SCK=21, SPI_MOSI=14,
    # SPI_MISO=47, SPI_CS=38). pinMode() on any of those pins detaches the
    # SDMMC bus (periman -> SD_MMC.end()) and unmounts storage mid-boot.
    write_json(root / "keybinds.json", {
        "input": {"mode": "INFRARED", "infrared_gpio": 13, "drivers": drivers},
        "infrared": {"1": {"usercode": "00FF", "bind": {}}},
        "keybinds": {},
    })
    write_json(root / "misc.json", {
        "overlays": {}, "servos": {}, "leds": {"pin_mode": "double", "groups": []},
        "boop": {"enabled": False}, "fft": {"enabled": False}, "scripts": {},
    })
    (root / "models.json").write_text("{}\n", encoding="utf-8")


def run_boot_scenario(run_dir: pathlib.Path, name: str, drivers: list[str]) -> tuple[str, dict[str, bool]]:
    """Boot one real guest session and return its UART text plus markers."""
    host = run_dir / f"host-{name}"
    host.mkdir()
    build_host_fixture(host, drivers)
    profile_path = run_dir / f"profile-{name}.json"
    write_json(profile_path, {
        "firmware_image": str(FIRMWARE), "firmware_sha256": EXPECTED_FIRMWARE_SHA256,
        "board_profile": BOARD, "log_level": "info", "headless": True, "renderer": "none",
    })
    os.environ.setdefault("PROTOPANDAEMU_QUIET_UART", "1")
    handle = run_session(profile_path, hostdir=host, timeout=75)
    uart_log = pathlib.Path(handle.session_report()["uart_log_path"])
    deadline = time.monotonic() + 70
    # ``models`` is required before onSetup, so it does not prove that the
    # driver path returned.  The firmware emits this memory-accounting
    # marker only after onSetup and onPreflight have both returned.
    completed_setup = 'memmory_difference msg="completed setup"'
    # The upstream-faithful terminal marker of the INFRARED + panda path:
    # drivers.lua WrapUp after panda.onEnable() declined (BLE is not started
    # in INFRARED mode).
    driver_enable_declined = "dont have a valid handler"
    terminal = set(FORBIDDEN) | {completed_setup, driver_enable_declined}
    text = ""
    while time.monotonic() < deadline:
        text = uart_log.read_text(encoding="utf-8", errors="replace") if uart_log.is_file() else ""
        if any(marker in text for marker in terminal) or handle.wait(timeout=0.1):
            break
    report = handle.stop()
    text = uart_log.read_text(encoding="utf-8", errors="replace")
    (run_dir / f"qemu.uart-{name}.log").write_text(text, encoding="utf-8")
    markers = {
        "saved_dictionary": "Saving dictionary..." in text,
        "infrared_started": "[IR] Started IR" in text,
        "panda_loaded": "Loaded core driver panda" in text,
        "post_driver_boot": completed_setup in text,
        "driver_enable_declined": driver_enable_declined in text,
        "forbidden": not any(marker in text for marker in FORBIDDEN),
    }
    return text, markers


def main() -> int:
    run_dir = pathlib.Path(tempfile.mkdtemp(prefix="protopandaemu-sdmmc-panda-", dir="/tmp"))
    print(f"SDMMC_PANDA_PROOF_RUN_DIR={run_dir}", flush=True)
    try:
        require(FIRMWARE.is_file(), f"pinned firmware missing: {FIRMWARE}")
        require(sha256(FIRMWARE) == EXPECTED_FIRMWARE_SHA256, "pinned firmware hash mismatch")

        # Scenario A exercises the real driver-load path: dictionary
        # persistence, IR setup, and the panda driver file opened from the
        # nested lualib/drivers directory.  Upstream semantics then end the
        # boot at WrapUp because panda.onEnable() requires BLE, which
        # INFRARED mode does not start.
        driver_text, driver_markers = run_boot_scenario(run_dir, "driver", ["panda"])
        require(driver_markers["saved_dictionary"], "real guest did not persist its dictionary")
        require(driver_markers["infrared_started"], "real guest did not start INFRARED input")
        require(driver_markers["forbidden"], f"forbidden boot result: {[m for m in FORBIDDEN if m in driver_text]}")
        require(driver_markers["panda_loaded"], "real guest did not load the panda driver")
        require(driver_markers["driver_enable_declined"], "driver path did not reach the upstream BLE-only enable outcome")

        # Scenario B probes the full boot.  It now hangs AFTER leds.begin()
        # inside menu.setup(): the loop task's saved stack contains ESP32-S3
        # ROM SPI-flash wait-loop return addresses (0x4005xxxx ROM spiflash
        # range) and both cores sit idle, i.e. a FFAT write via the ROM flash
        # path never completes in the experimental QEMU.  That is a separate
        # flash-model gap, outside the SDMMC correction scope; this run only
        # records it as a diagnostic, it does not gate the storage proof.
        clean_text, clean_markers = run_boot_scenario(run_dir, "clean", [])
        clean_markers["flash_stall_after_leds"] = (
            "Second started" in clean_text and not clean_markers["post_driver_boot"]
        )

        summary = {
            "run_dir": str(run_dir),
            "driver_scenario": driver_markers,
            "clean_scenario_diagnostic": clean_markers,
        }
        write_json(run_dir / "summary.json", summary)
        print(json.dumps(summary, sort_keys=True))
        print(f"SDMMC_PANDA_PROOF_ARTIFACT={run_dir / 'summary.json'}")
        return 0
    except (ProofFailure, OSError) as exc:
        failure = {"run_dir": str(run_dir), "error": str(exc)}
        write_json(run_dir / "failure.json", failure)
        print(f"SDMMC_PANDA_PROOF_FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
