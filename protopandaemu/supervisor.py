"""Run the real experimental ESP32-S3 QEMU backend.

The module deliberately owns the process lifetime, UART capture and flash
state.  The CLI is only a presentation layer around :func:`run_session`.
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from typing import Any

from .hostdir import HostDirError, HostDirSession


ROOT = pathlib.Path(__file__).resolve().parents[1]
QEMU_DEFAULT = ROOT / ".work/qemu-source-unpacked/qemu-esp_develop_9.2.2_20260417-src/build/qemu-system-xtensa"
SD_BUILDER = ROOT / "scripts/make-sd-image.sh"
SCHEMA_PATH = ROOT / "schemas/machine-profile.schema.json"
FLASH_ROOT = ROOT / ".work/protopandaemu-flash"
SD_ROOT = ROOT / ".work/protopandaemu-sd"
SESSION_ROOT = ROOT / ".work/protopandaemu-sessions"
EXPECTED_FIRMWARE_SHA256 = "811774774e8a9dbfd9d7a8278efc03b8bb6cd1a028c18ba68e7c7cee26f04bf3"
DEFAULT_TIMEOUT = 30.0
STOP_TIMEOUT = 8.0
MARKER_NAMES = (
    "rom", "bootloader", "onSetup", "onPreflight", "vfs_init", "onLoop",
    "panic", "watchdog_reset", "guest_stderr",
)


class ProfileError(ValueError):
    """A fail-closed profile or pre-spawn prerequisite error."""

    def __init__(self, message: str, *, code: str = "profile_invalid", profile_hash: str | None = None,
                 profile_path: pathlib.Path | None = None):
        super().__init__(message)
        self.code = code
        self.profile_hash = profile_hash
        self.profile_path = profile_path

    def report(self) -> dict[str, Any]:
        now = utc_now()
        return {
            "schema": "protopandaemu/session-report/v1",
            "session_id": str(uuid.uuid4()),
            "firmware_sha256": None,
            "profile_hash": self.profile_hash,
            "started_at_utc": now,
            "finished_at_utc": now,
            "wall_clock_ms": 0.0,
            "exit_reason": "crash",
            "qemu_started": False,
            "error_code": self.code,
            "error": str(self),
            "markers": {name: False for name in MARKER_NAMES},
            "resources": unavailable_resources("pre-spawn rejection"),
            "flash_state_path": None,
            "flash_initial_sha256": None,
            "flash_final_sha256": None,
            "uart_log_path": None,
        }


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable_sha256(path: pathlib.Path, label: str) -> str:
    """Hash one stable regular file before it is handed to QEMU."""
    before = os.stat(path, follow_symlinks=True)
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
            raise ProfileError(f"{label} changed while it was being opened", code="file_changed_during_validation")
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    after = os.stat(path, follow_symlinks=True)
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
        raise ProfileError(f"{label} changed during validation", code="file_changed_during_validation")
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def unavailable_resources(reason: str) -> dict[str, str]:
    return {name: f"unavailable: {reason}" for name in (
        "heap_free_pct", "psram_free_bytes", "psram_total_bytes")}


def _validate_profile_shape(profile: Any) -> None:
    if not isinstance(profile, dict):
        raise ProfileError("profile root must be an object")
    required = {"firmware_image", "firmware_sha256", "board_profile", "log_level"}
    missing = sorted(required - profile.keys())
    if missing:
        raise ProfileError(f"profile is missing required fields: {', '.join(missing)}")
    if not isinstance(profile["firmware_image"], str) or not profile["firmware_image"]:
        raise ProfileError("firmware_image must be a non-empty string")
    if not isinstance(profile["firmware_sha256"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", profile["firmware_sha256"]):
        raise ProfileError("firmware_sha256 must be a 64-character hexadecimal string")
    board = profile["board_profile"]
    if not isinstance(board, dict):
        raise ProfileError("board_profile must be an object")
    expected = {"chip": "esp32s3", "cores": 2, "cpu_mhz": 240,
                "flash_bytes": 16 * 1024 * 1024, "psram_bytes": 8 * 1024 * 1024,
                "psram_mode": "octal"}
    incompatible = {key: board.get(key) for key, value in expected.items() if board.get(key) != value}
    missing = sorted(key for key in expected if key not in board)
    if missing or incompatible:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if incompatible:
            details.append("incompatible " + ", ".join(f"{key}={value!r}" for key, value in incompatible.items()))
        raise ProfileError("board_profile is not the pinned N16R8 shape (" + "; ".join(details) + ")",
                           code="incompatible_board_profile")
    if profile["log_level"] not in {"debug", "info", "warning", "error"}:
        raise ProfileError("log_level must be debug, info, warning, or error")
    if profile.get("headless", True) is not True:
        raise ProfileError("M2 only supports headless=true")
    if profile.get("renderer", "none") != "none":
        raise ProfileError("M2 only supports renderer=none")


def _validate_schema_subset(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate the schema keywords used by the committed M2 schema.

    This small stdlib validator keeps the runtime dependency-free while still
    making the checked contract executable rather than merely documentary.
    """
    expected_type = schema.get("type")
    types = expected_type if isinstance(expected_type, list) else [expected_type]
    if expected_type:
        matches = []
        for type_name in types:
            matches.append({
                "object": isinstance(value, dict), "string": isinstance(value, str),
                "integer": isinstance(value, int) and not isinstance(value, bool),
                "number": isinstance(value, (int, float)) and not isinstance(value, bool),
                "null": value is None,
                "boolean": isinstance(value, bool),
            }.get(type_name, False))
        if not any(matches):
            raise ProfileError(f"{path} must have JSON type {expected_type}")
    if "const" in schema and value != schema["const"]:
        raise ProfileError(f"{path} must equal {schema['const']}")
    if "enum" in schema and value not in schema["enum"]:
        raise ProfileError(f"{path} must be one of {schema['enum']}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ProfileError(f"{path} must not be empty")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ProfileError(f"{path} does not match the committed schema pattern")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise ProfileError(f"{path} is missing required field {key!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unexpected = sorted(set(value) - set(properties))
            if unexpected:
                raise ProfileError(f"{path} has unexpected fields: {', '.join(unexpected)}")
        for key, item in value.items():
            if key in properties:
                _validate_schema_subset(item, properties[key], f"{path}.{key}")


def load_profile(profile_path: str | os.PathLike[str]) -> tuple[dict[str, Any], pathlib.Path, str]:
    path = pathlib.Path(profile_path).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
        profile = json.loads(raw)
    except FileNotFoundError as exc:
        raise ProfileError(f"profile does not exist: {path}", code="profile_missing", profile_path=path) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"cannot read profile {path}: {exc}", profile_path=path) from exc
    profile_hash = canonical_hash(profile)
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        _validate_schema_subset(profile, schema)
        _validate_profile_shape(profile)
    except FileNotFoundError as exc:
        raise ProfileError(f"committed profile schema is missing: {SCHEMA_PATH}", code="schema_missing",
                           profile_hash=profile_hash, profile_path=path) from exc
    except json.JSONDecodeError as exc:
        raise ProfileError(f"committed profile schema is invalid: {exc}", code="schema_invalid",
                           profile_hash=profile_hash, profile_path=path) from exc
    except ProfileError as exc:
        if str(exc).startswith("$.board_profile"):
            exc.code = "incompatible_board_profile"
        exc.profile_hash = profile_hash
        exc.profile_path = path
        raise
    return profile, path, profile_hash


def _safe_qemu_path(path: pathlib.Path, label: str) -> str:
    resolved = path.expanduser().resolve()
    raw = str(resolved)
    if not raw or any(char in raw for char in (",", "\x00", "\n", "\r")):
        raise ProfileError(f"{label} contains a QEMU option separator or control character",
                           code="unsafe_qemu_path")
    return raw


def _firmware_path(profile: dict[str, Any], profile_path: pathlib.Path) -> pathlib.Path:
    candidate = pathlib.Path(profile["firmware_image"]).expanduser()
    if not candidate.is_absolute():
        candidate = (profile_path.parent / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if not candidate.is_file():
        raise ProfileError(f"firmware image does not exist: {candidate}", code="firmware_missing")
    _safe_qemu_path(candidate, "firmware_image")
    observed = _stable_sha256(candidate, "firmware_image")
    declared = profile["firmware_sha256"].lower()
    if observed != declared:
        raise ProfileError(f"firmware_sha256 mismatch: declared {declared}, disk {observed}",
                           code="firmware_hash_mismatch")
    extra = os.environ.get("PROTOPANDAEMU_EXTRA_FIRMWARE_SHA256", "").strip().lower()
    if observed != EXPECTED_FIRMWARE_SHA256 and observed != extra:
        raise ProfileError(f"firmware is not the pinned Proto-panda v3.4.0 image: {observed}",
                           code="firmware_not_pinned")
    if candidate.stat().st_size != 16 * 1024 * 1024:
        raise ProfileError(f"firmware image must be a 16 MiB merged image: {candidate.stat().st_size} bytes",
                           code="firmware_size_mismatch")
    return candidate


def _write_png(path: pathlib.Path) -> None:
    # A tiny valid RGB PNG, used as a real SD input to the unchanged guest.
    import struct
    import zlib

    width, height = 64, 32
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row.extend((255 if (x // 8 + y // 8) % 2 else 0, 40 + (x * 3) % 160, 80 + (y * 5) % 160))
        rows.append(bytes(row))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)

    data = b"\x89PNG\r\n\x1a\n"
    data += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
    data += chunk(b"IEND", b"")
    path.write_bytes(data)


def _lua_fixture() -> str:
    original = (ROOT / ".work/upstream-v3.4.0/init.lua").read_text(encoding="utf-8")
    return original + r'''

-- M2 acceptance markers are emitted by the real guest Lua API, not by the host.
local m2_original_onSetup = onSetup
function onSetup()
    m2_original_onSetup()
    print("M2_MARKER onSetup completed")
end

function onPreflight()
    -- M2 deliberately isolates the post-storage callback from radio/display
    -- setup. The unmodified firmware still invokes this real Lua callback;
    -- M4 owns the peripheral-backed preflight path.
    print("M2_MARKER onPreflight completed")
end

local m2_loop_marked = false
function onLoop(dt)
    -- Keep the actual guest loop alive without requiring M4 peripherals.
    if not m2_loop_marked then
        print("M2_MARKER onLoop completed")
        m2_loop_marked = true
    end
end
'''


def _copy_generated_file(source: pathlib.Path, destination: pathlib.Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(source, temporary)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _make_sd_image(session_dir: pathlib.Path, sd_state: pathlib.Path) -> pathlib.Path:
    image = session_dir / "sd-card.img"
    if sd_state.exists():
        if not sd_state.is_file():
            raise ProfileError(f"SD state is not a file: {sd_state}", code="sd_state_invalid")
        if sd_state.stat().st_size != 64 * 1024 * 1024:
            raise ProfileError(f"SD state has the wrong size: {sd_state.stat().st_size} bytes",
                               code="sd_state_size_mismatch")
        _copy_generated_file(sd_state, image)
        return image
    if not SD_BUILDER.is_file():
        raise ProfileError(f"SD image builder is missing: {SD_BUILDER}", code="sd_builder_missing")
    if not (ROOT / ".work/upstream-v3.4.0/init.lua").is_file():
        raise ProfileError("pinned upstream-v3.4.0 checkout is missing", code="upstream_missing")
    fixture = session_dir / "sd-fixture"
    fixture.mkdir(parents=True, exist_ok=False)
    (fixture / "frames").mkdir()
    (fixture / "animation.json").write_text(
        '{"frames":[{"name":"m2-idle","file":"/frames/idle.png"}],'
        '"expressions":[{"name":"m2-idle","frames":"m2-idle","animation":"loop"}]}\n',
        encoding="utf-8")
    (fixture / "wifi.json").write_text("{}\n", encoding="utf-8")
    (fixture / "keybinds.json").write_text(
        '{"input":{"mode":"NONE","drivers":["generic"]},"keybinds":{}}\n', encoding="utf-8")
    (fixture / "misc.json").write_text(
        '{"overlays":{},"servos":{},"leds":{"pin_mode":"double","groups":[]},"boop":{"enabled":false},'
        '"fft":{"enabled":false},"scripts":{}}\n', encoding="utf-8")
    (fixture / "models.json").write_text("{}\n", encoding="utf-8")
    if not any((fixture / "frames").iterdir()):
        _write_png(fixture / "frames/idle.png")
    build = subprocess.run(["bash", str(SD_BUILDER), str(image), "--fixture-root", str(fixture)],
                           cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, check=False, timeout=120)
    if build.returncode != 0 or not image.is_file():
        raise ProfileError(f"SD image generation failed: {build.stdout[-1200:]}", code="sd_image_failed")
    # The builder intentionally starts from the pinned stock hardware/init
    # files. Overlay the M2-only display-disabled fixture and Lua markers.
    (session_dir / "m2-init.lua").write_text(_lua_fixture(), encoding="utf-8")
    (session_dir / "m2-hardware.json").write_text(
        '{"canvas_width":64,"canvas_height":32,"display":{"type":"none","enabled":false}}\n',
        encoding="utf-8")
    overlays = ((session_dir / "m2-init.lua", "::init.lua"),
                (session_dir / "m2-hardware.json", "::hardware.json"))
    for overlay, target in overlays:
        result = subprocess.run(["mcopy", "-D", "o", "-i", str(image), str(overlay), target],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, check=False, timeout=30)
        if result.returncode != 0:
            raise ProfileError(f"SD fixture overlay failed: {result.stdout[-800:]}", code="sd_overlay_failed")
    return image


def _copy_flash(source: pathlib.Path, destination: pathlib.Path, source_sha256: str) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file():
            raise ProfileError(f"flash_state_path is not a file: {destination}", code="flash_state_invalid")
        if destination.stat().st_size != source.stat().st_size:
            raise ProfileError(
                f"flash_state_path has the wrong size: {destination.stat().st_size} bytes",
                code="flash_state_size_mismatch")
        return _stable_sha256(destination, "flash_state_path")
    with source.open("rb") as input_stream, destination.open("xb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
        output_stream.flush()
        os.fsync(output_stream.fileno())
    copied_hash = _stable_sha256(destination, "flash_state_path")
    if copied_hash != source_sha256:
        raise ProfileError("flash state copy does not match the pinned firmware",
                           code="flash_copy_hash_mismatch")
    return copied_hash


def _reject_flash_alias(firmware: pathlib.Path, requested: pathlib.Path, resolved: pathlib.Path) -> None:
    if resolved.exists():
        try:
            if os.path.samefile(firmware, resolved):
                raise ProfileError("flash_state_path aliases firmware_image", code="flash_state_alias")
        except FileNotFoundError:
            pass
    if requested.is_symlink():
        raise ProfileError("flash_state_path must not be a symlink", code="flash_state_symlink")


def _monitor_resources(line: str, report: dict[str, Any]) -> None:
    match = re.search(r"\[Memory\]\s+([0-9]+(?:\.[0-9])?)% free .*?\(psram:\s*(\d+)\s*/\s*(\d+)", line)
    if match:
        report["resources"] = {
            "heap_free_pct": float(match.group(1)),
            "psram_total_bytes": int(match.group(2)),
            "psram_free_bytes": int(match.group(3)),
        }


class SessionHandle:
    """A running QEMU session with an explicit clean-stop lifecycle."""

    def __init__(self, *, profile: dict[str, Any], profile_hash: str, firmware: pathlib.Path,
                 flash_state: pathlib.Path, session_dir: pathlib.Path, sd_image: pathlib.Path,
                 sd_state: pathlib.Path | None, firmware_sha256: str, flash_initial_sha256: str,
                 host_session: HostDirSession | None,
                 timeout: float, process: subprocess.Popen[bytes], monitor_stdin: Any,
                 qemu_argv: list[str], uart_log: pathlib.Path):
        self._profile = profile
        self._profile_hash = profile_hash
        self._firmware = firmware
        self._flash_state = flash_state
        self._session_dir = session_dir
        self._sd_image = sd_image
        self._sd_state = sd_state
        self._host_session = host_session
        self._timeout = timeout
        self._process = process
        self._monitor_stdin = monitor_stdin
        self._monitor_lock = threading.Lock()
        self._started_mono = time.monotonic()
        self._started_at = utc_now()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._finished = threading.Event()
        self._stop_initiated = False
        self._graceful_quit_sent = False
        self._timeout_requested = False
        self._report: dict[str, Any] = {
            "schema": "protopandaemu/session-report/v1",
            "session_id": session_dir.name,
            "firmware_sha256": firmware_sha256,
            "profile_hash": profile_hash,
            "started_at_utc": self._started_at,
            "finished_at_utc": None,
            "wall_clock_ms": None,
            "exit_reason": None,
            "qemu_started": True,
            "qemu_pid": process.pid,
            "qemu_argv": qemu_argv,
            "error_code": None,
            "markers": {name: False for name in MARKER_NAMES},
            "resources": unavailable_resources("no firmware memory line yet"),
            "flash_state_path": str(flash_state),
            "flash_initial_sha256": flash_initial_sha256,
            "flash_final_sha256": None,
            "uart_log_path": str(uart_log),
            "stderr_log_path": str(session_dir / "qemu.stderr.log"),
            "sd_image_path": str(sd_image),
            "sd_state_path": str(sd_state) if sd_state is not None else None,
        }
        if host_session is not None:
            self._report["hostdir"] = {
                "root": str(host_session.root),
                "sync_model": "clean-stop-only",
                "start_manifest": copy.deepcopy(host_session.start_manifest),
                "current_manifest": copy.deepcopy(host_session.start_manifest),
                "guest_manifest": None,
                "start_tree_sha256": host_session.start_tree_sha256,
                "current_tree_sha256": host_session.start_tree_sha256,
                "conflicts": [],
                "backups": [],
                "sync_status": "pending",
            }
        self._uart_log = uart_log
        self._stderr_log = session_dir / "qemu.stderr.log"
        self._uart_pending = ""
        self._stderr_pending = ""
        self._stdout_thread = threading.Thread(target=self._drain_uart, args=(process.stdout,), daemon=True)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, args=(process.stderr,), daemon=True)
        self._stderr_thread.start()
        self._stdout_thread.start()
        self._monitor_thread = threading.Thread(target=self._monitor, daemon=True)
        self._monitor_thread.start()

    @property
    def process(self) -> subprocess.Popen[bytes]:
        return self._process

    def _observe(self, data: bytes, *, stdout: bool) -> None:
        text = data.decode("utf-8", "replace")
        with self._lock:
            if stdout:
                text = self._uart_pending + text
            else:
                text = self._stderr_pending + text
            lines = text.splitlines(keepends=True)
            pending = lines[-1] if lines and not lines[-1].endswith(("\n", "\r")) else ""
            if len(pending) > 8192:
                pending = pending[-8192:]
            if stdout:
                self._uart_pending = pending
            else:
                self._stderr_pending = pending
            complete = lines[:-1] if pending else lines
            for line in complete:
                clean = line.strip()
                if re.search(r"^ESP-ROM:", clean):
                    self._report["markers"]["rom"] = True
                if re.search(r"^entry\s+0x[0-9a-fA-F]+$", clean):
                    self._report["markers"]["bootloader"] = True
                if re.search(r"M(?:2|3)_MARKER\s+onSetup\s+completed", clean):
                    self._report["markers"]["onSetup"] = True
                if re.search(r"M(?:2|3)_MARKER\s+onPreflight\s+completed", clean):
                    self._report["markers"]["onPreflight"] = True
                if re.search(r"M(?:2|3)_MARKER\s+onLoop\s+completed", clean):
                    self._report["markers"]["onLoop"] = True
                if re.search(r"^(Starting Lua|Running upon)\b", clean):
                    self._report["markers"]["vfs_init"] = True
                if re.search(r"Guru Meditation|panic|abort\(|LoadStoreError|LoadProhibited|StoreProhibited", clean, re.I):
                    self._report["markers"]["panic"] = True
                    self._report["markers"]["guest_stderr"] = True
                if re.search(r"watchdog|WDT|task watchdog", clean, re.I):
                    self._report["markers"]["watchdog_reset"] = True
                if not stdout and clean:
                    self._report["markers"]["guest_stderr"] |= bool(
                        re.search(r"Guru Meditation|panic|abort\(|LoadStoreError|LoadProhibited|StoreProhibited", clean, re.I))
                _monitor_resources(clean, self._report)
            self._condition.notify_all()

    def _drain_uart(self, stream: Any) -> None:
        self._uart_log.parent.mkdir(parents=True, exist_ok=True)
        # Full silence is still available via PROTOPANDAEMU_QUIET_UART=1
        # (automated proof scripts use it to keep their own output
        # clean). Short of that, every byte still reaches the log file
        # and the terminal unfiltered -- except [EMU] LED_DUMP lines,
        # which carry a full per-frame hex color buffer and drown out
        # everything else on a real boot; those are held back from the
        # terminal echo only, line by line, since the firmware writes
        # them in 4096-byte blocks that don't align with line breaks.
        quiet = os.environ.get("PROTOPANDAEMU_QUIET_UART") == "1"
        pending = b""
        with self._uart_log.open("wb") as output:
            while True:
                block = stream.read1(4096)
                if not block:
                    break
                output.write(block)
                output.flush()
                self._observe(block, stdout=True)
                if quiet:
                    continue
                pending += block
                *complete, pending = pending.split(b"\n")
                for line in complete:
                    if not line.startswith(b"[EMU] LED_DUMP"):
                        self._echo_to_stdout(line + b"\n")
        if not quiet and pending and not pending.startswith(b"[EMU] LED_DUMP"):
            self._echo_to_stdout(pending)

    @staticmethod
    def _echo_to_stdout(data: bytes) -> None:
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except (AttributeError, OSError):
            sys.stdout.write(data.decode("utf-8", "replace"))
            sys.stdout.flush()

    def _drain_stderr(self, stream: Any) -> None:
        self._stderr_log.parent.mkdir(parents=True, exist_ok=True)
        with self._stderr_log.open("wb") as output:
            while True:
                block = stream.read1(4096)
                if not block:
                    break
                output.write(block)
                output.flush()
                self._observe(block, stdout=False)

    def _monitor_command(self, command: str) -> bool:
        with self._monitor_lock:
            try:
                # mon:stdio is a QEMU mux: Ctrl-A c selects the HMP monitor
                # from the guest serial stream before issuing the command.
                self._monitor_stdin.write(b"\x01c")
                self._monitor_stdin.write((command + "\n").encode())
                self._monitor_stdin.flush()
                if command == "quit":
                    with self._lock:
                        self._graceful_quit_sent = True
            except (OSError, BrokenPipeError):
                return False
            return True

    def send_serial_input(self, text: str) -> bool:
        """Write raw text to the guest serial console.

        The ``mon:stdio`` mux routes plain stdin bytes to the guest UART, so
        the emu firmware's virtual controller channel ("IR:<addr>:<cmd>"
        lines) receives them directly.
        """
        with self._monitor_lock:
            try:
                self._monitor_stdin.write(text.encode())
                self._monitor_stdin.flush()
                return True
            except (OSError, BrokenPipeError):
                return False

    def _terminate(self, *, force: bool) -> None:
        if self._process.poll() is not None:
            return
        try:
            if force:
                self._process.kill()
            else:
                self._process.terminate()
        except ProcessLookupError:
            return
        try:
            self._process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                self._process.kill()
                self._process.wait(timeout=3.0)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass

    def _monitor(self) -> None:
        try:
            self._process.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            with self._lock:
                self._timeout_requested = True
            self._terminate(force=False)
        finally:
            if self._process.poll() is None:
                self._terminate(force=True)
            self._stderr_thread.join(timeout=3.0)
            self._stdout_thread.join(timeout=3.0)
            with self._monitor_lock:
                with self._lock:
                    if self._timeout_requested:
                        reason = "timeout"
                    elif self._report["markers"]["panic"] or self._report["markers"]["watchdog_reset"]:
                        reason = "guest_panic"
                    elif self._graceful_quit_sent and self._process.returncode == 0:
                        reason = "stopped"
                    else:
                        reason = "crash"
                    if reason == "stopped":
                        if self._host_session is not None:
                            try:
                                sync = self._host_session.sync(self._sd_image)
                                self._report["hostdir"].update(sync)
                            except (HostDirError, OSError) as exc:
                                reason = "sync_failed"
                                self._report["error_code"] = getattr(exc, "code", "hostdir_sync_failed")
                                self._report["error"] = str(exc)
                        else:
                            try:
                                _copy_generated_file(self._sd_image, self._sd_state)
                            except (OSError, ProfileError) as exc:
                                reason = "crash"
                                self._report["error_code"] = "sd_state_persist_failed"
                                self._report["error"] = str(exc)
                    if self._host_session is not None and self._report.get("hostdir", {}).get("sync_status") == "pending":
                        self._report["hostdir"]["sync_status"] = "not_committed"
                    self._report["exit_reason"] = reason
                    self._report["finished_at_utc"] = utc_now()
                    self._report["wall_clock_ms"] = round((time.monotonic() - self._started_mono) * 1000, 3)
                    try:
                        self._report["flash_final_sha256"] = sha256(self._flash_state)
                    except OSError:
                        self._report["flash_final_sha256"] = None
                    self._finished.set()
                    self._condition.notify_all()
            if self._host_session is not None:
                self._host_session.release()
            try:
                self._monitor_stdin.close()
            except OSError:
                pass

    def session_report(self) -> dict[str, Any]:
        with self._lock:
            report = copy.deepcopy(self._report)
            report["process_exited"] = self._process.poll() is not None
            if report["process_exited"] and report["finished_at_utc"] is None:
                report["finished_at_utc"] = utc_now()
            return report

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for backend completion without changing its lifecycle state."""
        return self._finished.wait(timeout=timeout)

    def wait_for_marker(self, marker: str, timeout: float) -> bool:
        """Wait for a marker observed from real guest UART, bounded by deadline."""
        if marker not in MARKER_NAMES:
            raise ValueError(f"unknown session marker: {marker}")
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._report["markers"][marker] and not self._finished.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return bool(self._report["markers"][marker])

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._finished.is_set():
                return self.session_report()
            initiate = not self._stop_initiated
            self._stop_initiated = True
        # HMP quit is a backend-level graceful stop.  The bounded wait below
        # is separate from timeout handling and never labels a timeout stop as
        # clean persistence.
        if initiate:
            self._monitor_command("quit")
        if not self._finished.wait(timeout=STOP_TIMEOUT):
            with self._lock:
                self._timeout_requested = True
            self._terminate(force=False)
            self._finished.wait(timeout=STOP_TIMEOUT)
        return self.session_report()


def _qemu_argv(flash_state: pathlib.Path, sd_image: pathlib.Path) -> list[str]:
    # Keep all QEMU filesystem operands private and explicit. In particular,
    # hostdir is never interpolated into this list.
    flash = _safe_qemu_path(flash_state, "flash_state_path")
    sd = _safe_qemu_path(sd_image, "generated_sd_image")
    argv = [str(QEMU_DEFAULT), "-nographic", "-machine", "esp32s3", "-m", "8M",
            "-global", "driver=ssi_psram,property=is_octal,value=true",
            "-drive", f"file={flash},if=mtd,format=raw",
            "-drive", f"if=sd,index=0,file={sd},format=raw",
            "-nic", "none", "-d", "unimp,guest_errors", "-serial", "mon:stdio"]
    # Opt-in escape hatch for the emu firmware variant: extra QEMU arguments
    # (e.g. "-icount shift=4,sleep=off" to cut FFat wait-loop wall time).
    extra = os.environ.get("PROTOPANDAEMU_EXTRA_QEMU_ARGS", "").split()
    return argv + extra


def run_session(profile_path: str | os.PathLike[str], hostdir: str | os.PathLike[str] | None = None,
                timeout: float | None = None, flash_state_path: str | os.PathLike[str] | None = None) -> SessionHandle:
    """Validate a profile and start one real experimental-QEMU session.

    Validation and firmware provisioning complete before ``Popen``.  A caller
    that needs a structured rejection catches :class:`ProfileError` and uses
    its ``report()`` method; no backend child is created on that path.
    """
    profile, profile_file, profile_hash = load_profile(profile_path)
    firmware = _firmware_path(profile, profile_file)
    if not QEMU_DEFAULT.is_file() or not os.access(QEMU_DEFAULT, os.X_OK):
        raise ProfileError(f"experimental QEMU executable is missing: {QEMU_DEFAULT}", code="qemu_missing",
                           profile_hash=profile_hash, profile_path=profile_file)
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ProfileError("timeout must be a finite positive number", code="timeout_invalid",
                               profile_hash=profile_hash, profile_path=profile_file)
        if not math.isfinite(float(timeout)) or timeout <= 0:
            raise ProfileError("timeout must be a finite positive number", code="timeout_invalid",
                               profile_hash=profile_hash, profile_path=profile_file)
    duration = float(timeout if timeout is not None else DEFAULT_TIMEOUT)
    session_id = str(uuid.uuid4())
    session_dir = SESSION_ROOT / session_id
    session_dir.mkdir(parents=True, exist_ok=False)
    try:
        requested_flash = pathlib.Path(flash_state_path).expanduser() if flash_state_path else FLASH_ROOT / f"{profile_hash}.flash.bin"
        if not requested_flash.is_absolute():
            requested_flash = ROOT / requested_flash
        flash_state = requested_flash.resolve()
        _safe_qemu_path(flash_state, "flash_state_path")
        _reject_flash_alias(firmware, requested_flash, flash_state)
        sd_state: pathlib.Path | None
        host_session: HostDirSession | None = None
        if hostdir is not None:
            try:
                host_session = HostDirSession.open(hostdir, session_dir, session_id)
                sd_image = host_session.build_image(session_dir / "sd-card.img")
            except HostDirError as exc:
                if host_session is not None:
                    host_session.release()
                raise ProfileError(str(exc), code=exc.code, profile_hash=profile_hash,
                                   profile_path=profile_file) from exc
            sd_state = None
        else:
            sd_state = SD_ROOT / f"{profile_hash}.sd.img"
            _safe_qemu_path(sd_state, "sd_state_path")
            sd_image = _make_sd_image(session_dir, sd_state)
        firmware_sha256 = _stable_sha256(firmware, "firmware_image")
        extra_allowed = os.environ.get("PROTOPANDAEMU_EXTRA_FIRMWARE_SHA256", "").strip().lower()
        if firmware_sha256 != profile["firmware_sha256"].lower() or (
                firmware_sha256 != EXPECTED_FIRMWARE_SHA256 and firmware_sha256 != extra_allowed):
            raise ProfileError("firmware changed before spawn or is no longer pinned",
                               code="firmware_changed_before_spawn", profile_hash=profile_hash,
                               profile_path=profile_file)
        flash_initial_sha256 = _copy_flash(firmware, flash_state, firmware_sha256)
        if _stable_sha256(firmware, "firmware_image") != firmware_sha256:
            raise ProfileError("firmware changed while flash state was provisioned",
                               code="firmware_changed_before_spawn", profile_hash=profile_hash,
                               profile_path=profile_file)
        _reject_flash_alias(firmware, requested_flash, flash_state)
        flash_identity = os.stat(flash_state, follow_symlinks=False)
        argv = _qemu_argv(flash_state, sd_image)
        if requested_flash.is_symlink() or os.stat(flash_state, follow_symlinks=False).st_ino != flash_identity.st_ino:
            raise ProfileError("flash_state_path changed during provisioning", code="flash_state_changed",
                               profile_hash=profile_hash, profile_path=profile_file)
        env = os.environ.copy()
        env["TMPDIR"] = str(session_dir)
        process = subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env,
                                   start_new_session=True)
        return SessionHandle(profile=profile, profile_hash=profile_hash, firmware=firmware,
                             flash_state=flash_state, session_dir=session_dir, sd_image=sd_image,
                             sd_state=sd_state, firmware_sha256=firmware_sha256,
                             flash_initial_sha256=flash_initial_sha256,
                             host_session=host_session,
                             timeout=duration, process=process, monitor_stdin=process.stdin,
                             qemu_argv=argv,
                             uart_log=session_dir / "qemu.uart.log")
    except ProfileError:
        if 'host_session' in locals() and host_session is not None:
            host_session.release()
        shutil.rmtree(session_dir, ignore_errors=True)
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        if 'host_session' in locals() and host_session is not None:
            host_session.release()
        shutil.rmtree(session_dir, ignore_errors=True)
        raise ProfileError(f"could not start experimental QEMU: {exc}", code="qemu_spawn_failed",
                           profile_hash=profile_hash, profile_path=profile_file) from exc
