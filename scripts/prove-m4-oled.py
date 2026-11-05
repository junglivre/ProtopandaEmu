#!/usr/bin/env python3
"""Rock 3/M4 OLED acceptance proof: real SSD1306 GDDRAM, independently verified.

Boots the unmodified firmware with a real folder (display enabled), captures
the SSD1306 model's raw GDDRAM dumps and transfer/addressing events from the
backend's own stderr, and checks the splash-icon frame against an oracle this
script derives independently from the firmware's own compiled-in icon bitmap
(`icon_protopanda` in icons.hpp) using Adafruit_GFX's documented row-major
MSB-first bitmap unpacking and Adafruit_SSD1306's documented page/column
LSB-first GDDRAM packing -- not the renderer under test, and not a byte copy
of the source array (the two layouts differ; this script performs the real
transform by hand from source). `OLED_SCREEN_ROTATION=2` is applied the same
way `Adafruit_SSD1306::drawPixel` does for its rotation-2 case.

The icon frame also carries the firmware's own version text, which this
oracle does not render; only the non-text-overlay bytes are required to be
byte-exact, and the text-overlay bytes are required to be non-blank evidence
that real pixels were written there too.
"""
from __future__ import annotations

import json
import pathlib
import re
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
         "flash_bytes": 16 * 1024 * 1024, "psram_bytes": 8 * 1024 * 1024, "psram_mode": "octal"}
WIDTH, HEIGHT, PAGES = 128, 64, 8
ROTATION = 2  # OLED_SCREEN_ROTATION, config_default.hpp
REQUIRED_EVENTS = ("DISPLAY_OFF", "DISPLAY_ON", "COLUMNADDR", "PAGEADDR", "GDDRAM_DUMP")


class ProofFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProofFailure(message)


def write_json(path: pathlib.Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def png_solid(path: pathlib.Path, width: int = 64, height: int = 32) -> None:
    rows = b"".join(b"\0" + bytes((220, 40, 90)) * width for _ in range(height))
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload +
                struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff))
    data = b"\x89PNG\r\n\x1a\n"
    data += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(rows, 9))
    data += chunk(b"IEND", b"")
    path.write_bytes(data)


def build_host_fixture(root: pathlib.Path) -> None:
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
    write_json(root / "keybinds.json", {"input": {"mode": "NONE", "drivers": ["generic"]}, "keybinds": {}})
    write_json(root / "misc.json", {"overlays": {}, "servos": {}, "leds": {"pin_mode": "double", "groups": []},
                                    "boop": {"enabled": False}, "fft": {"enabled": False}, "scripts": {}})
    (root / "models.json").write_text("{}\n", encoding="utf-8")


def load_icon_bitmap() -> bytes:
    header = (UPSTREAM / "include/drawing/icons/icons.hpp").read_text(encoding="utf-8")
    block = header.split("icon_protopanda", 1)[1].split("{", 1)[1].split("};", 1)[0]
    values = bytes(int(h, 16) for h in re.findall(r"0x([0-9a-fA-F]{2})", block))
    require(len(values) == 1024, f"icon_protopanda is not 1024 bytes: {len(values)}")
    return values


def icon_oracle_gddram() -> bytearray:
    """Independently re-derive the expected GDDRAM bytes for the splash icon.

    `icon_protopanda` is row-major, MSB-first, byte-width 16 (128/8) -- the
    exact unpacking Adafruit_GFX::drawBitmap performs (`bitmap[j*byteWidth +
    i/8]`, bit tested with 0x80 >> (i%8)). Adafruit_SSD1306::drawPixel then
    repacks each set bit as `buffer[x + (y/8)*WIDTH] |= 1 << (y & 7)`, after
    applying its own rotation-2 coordinate flip (`x'=WIDTH-x-1,
    y'=HEIGHT-y-1`). Reproducing both steps by hand is what makes this an
    independent oracle: it is not a copy of the renderer under test, and not
    a trivial byte-for-byte reinterpretation of the source array (the two
    layouts are genuinely different serializations of the same image).
    """
    icon = load_icon_bitmap()
    byte_width = (WIDTH + 7) // 8
    oracle = bytearray(WIDTH * PAGES)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            source_byte = icon[y * byte_width + x // 8]
            if not (source_byte & (0x80 >> (x % 8))):
                continue
            rx, ry = WIDTH - x - 1, HEIGHT - y - 1  # rotation=2
            oracle[rx + (ry // 8) * WIDTH] |= 1 << (ry & 7)
    return oracle


def text_overlay_region() -> set[int]:
    """The byte indices the oracle does not model (the version-text overlay).

    `OledScreen::Start()` draws `setCursor((WIDTH/2)-8, HEIGHT-8)` then prints
    the version string on top of the icon, in the SAME `display()` call this
    proof captures. After the rotation-2 flip that region lands in GDDRAM
    page 0 (`y'=HEIGHT-1-y` maps the original bottom row to page 0). Scope
    the exemption to that one page rather than the whole frame, so a real
    regression anywhere else in the icon still fails this proof.
    """
    return {i for i in range(WIDTH * PAGES) if i // WIDTH == 0}


def extract_events(stderr_log: pathlib.Path) -> tuple[list[str], list[bytes]]:
    text = stderr_log.read_text(encoding="utf-8", errors="replace")
    events = re.findall(r"\[protopandaemu\.ssd1306\]\s+(\S+)", text)
    frames = [bytes.fromhex(h) for h in re.findall(
        r"GDDRAM_DUMP seq=\d+ width=128 height=64 bytes=1024 hex=([0-9a-f]+)", text)]
    return events, frames


def main() -> int:
    run_dir = pathlib.Path(tempfile.mkdtemp(prefix="protopandaemu-m4-oled-", dir="/tmp"))
    print(f"M4_OLED_PROOF_RUN_DIR={run_dir}", flush=True)
    try:
        require(FIRMWARE.is_file(), f"pinned firmware missing: {FIRMWARE}")
        require(sha256(FIRMWARE) == EXPECTED_FIRMWARE_SHA256, "pinned firmware hash mismatch")
        host = run_dir / "host"; host.mkdir()
        build_host_fixture(host)
        profile_path = run_dir / "profile.json"
        write_json(profile_path, {
            "firmware_image": str(FIRMWARE), "firmware_sha256": EXPECTED_FIRMWARE_SHA256,
            "board_profile": BOARD, "log_level": "info", "headless": True, "renderer": "none",
        })
        handle = run_session(profile_path, hostdir=host, timeout=30)
        # OledScreen::Start() runs at the very beginning of setup(), before
        # hardware.json's hub75 display is ever parsed; "Loading display
        # info" is logged once that whole call (icon + version text
        # display()) has already completed, well before the separate,
        # already-documented HUB75 GDMA streaming limitation would otherwise
        # hang the rest of boot. This proof is scoped to the OLED path only
        # and does not depend on reaching onLoop. Poll the real raw UART log
        # for that line instead of a fixed wall-clock sleep, since TCG
        # emulated-time-per-wall-second is not constant across runs.
        uart_log = pathlib.Path(handle.session_report()["uart_log_path"])
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if uart_log.is_file() and "Loading display info" in uart_log.read_text(errors="replace"):
                break
            if handle.wait(timeout=0.2):
                break
        else:
            raise ProofFailure("bounded wait did not observe real 'Loading display info' UART")
        report = handle.stop()
        require(report["exit_reason"] == "stopped", f"expected clean stop, got {report['exit_reason']!r}")
        stderr_log = pathlib.Path(report["stderr_log_path"])
        shutil.copyfile(stderr_log, run_dir / "qemu.stderr.log")
        events, frames = extract_events(run_dir / "qemu.stderr.log")
        for required in REQUIRED_EVENTS:
            require(required in events, f"missing required SSD1306 event: {required}")
        require(len(frames) >= 2, f"expected at least 2 real GDDRAM frames, got {len(frames)}")
        for index, frame in enumerate(frames):
            require(len(frame) == WIDTH * PAGES, f"frame {index}: wrong GDDRAM size {len(frame)}")
        oracle = icon_oracle_gddram()
        overlay = text_overlay_region()
        # TCG keeps executing guest code in real wall-clock time between the
        # UART line appearing and this script's own next poll tick, so later
        # frames (e.g. the start of the still-hanging HUB75 init) can also
        # land in the capture. Find the one real frame that is byte-exact
        # with the independent oracle outside the text overlay, rather than
        # guessing an index or "the densest frame".
        best_frame, best_mismatches = None, None
        for frame in frames:
            mismatches = [i for i in range(len(oracle)) if i not in overlay and oracle[i] != frame[i]]
            if best_mismatches is None or len(mismatches) < len(best_mismatches):
                best_frame, best_mismatches = frame, mismatches
        icon_frame, mismatches = best_frame, best_mismatches
        require(not mismatches,
                f"no captured frame is byte-exact with the independent oracle outside the known "
                f"text-overlay page; closest had {len(mismatches)} mismatch(es): {mismatches[:10]}")
        overlay_nonzero = sum(1 for i in overlay if icon_frame[i])
        require(overlay_nonzero > 0, "version-text overlay region is blank; no real text was rendered")
        write_json(run_dir / "oracle.json", {"gddram_hex": bytes(oracle).hex()})
        write_json(run_dir / "icon_frame.json", {"gddram_hex": icon_frame.hex()})
        summary = {
            "run_dir": str(run_dir), "events_observed": sorted(set(events)),
            "frames_captured": len(frames), "icon_frame_oracle_matched_bytes": len(oracle) - len(overlay),
            "icon_frame_total_bytes": len(oracle), "text_overlay_nonzero_bytes": overlay_nonzero,
        }
        write_json(run_dir / "summary.json", summary)
        print(json.dumps(summary, sort_keys=True))
        print(f"M4_OLED_PROOF_ARTIFACT={run_dir / 'summary.json'}")
        return 0
    except (ProofFailure, OSError) as exc:
        write_json(run_dir / "failure.json", {"run_dir": str(run_dir), "error": str(exc)})
        print(f"M4_OLED_PROOF_FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
