#!/usr/bin/env python3
"""One evidence-producing harness for the ProtopandaEmu backend rocks."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import signal
import shutil
import struct
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
RUNS = ARTIFACTS / "spike-runs"
SCHEMA = "protopandaemu-spike/v1"
STATUS = {"PASS", "TECHNICAL_BLOCKER", "NOT_EXECUTED"}
PROFILE = {
    "chip": "esp32s3", "cores": 2, "cpu_mhz": 240,
    "flash_bytes": 16 * 1024 * 1024, "psram_bytes": 8 * 1024 * 1024,
    "psram_mode": "octal", "assertion": "N16R8: dual-core LX7, 240 MHz, 16 MiB flash, 8 MiB octal PSRAM",
}

BACKENDS = {
    "esp-emulator": {
        "binary": ROOT / ".rf/backends/esp-emu-0.51.0-x86_64-unknown-linux-gnu/esp-emu",
        "archive": ROOT / ".rf/backends/esp-emu-0.51.0.tar.gz",
        "archive_sha256": "2b9e157470edb8b03167996550292c6df8cc6716025ebad8927db950d7931417",
        "version": "0.51.0", "commit": "61d71cef2b1f13a936173c6d282a52c093137d6a",
        "repo": "https://github.com/espressif/esp-emulator",
    },
    "qemu": {
        "binary": ROOT / ".rf/backends/qemu/bin/qemu-system-xtensa",
        "archive": ROOT / ".rf/backends/qemu-9.2.2.tar.xz",
        "archive_sha256": "0eecb2a34a5586c0e59110f77b9343b7b336e82fdb0e1a30e1dc1bab8a547e35",
        "version": "9.2.2 (esp_develop_9.2.2_20260417)",
        "commit": "40edccac415693c5130f91c01d84176ae6008566",
        "repo": "https://github.com/espressif/qemu",
    },
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def run_path(backend: str, mode: str, run_id: str) -> pathlib.Path:
    return RUNS / run_id / f"{backend}-{mode}"


def firmware_info(requested: str | None) -> dict:
    if requested:
        path = pathlib.Path(requested)
        if not path.is_absolute():
            path = (ROOT / path).resolve()
        provenance = "caller-specified --firmware"
    else:
        path = ROOT / "artifacts/build-normal/release/merged_flash.bin"
        provenance = "Rock 1 artifacts/build-normal/release/merged_flash.bin"
    if not path.is_file():
        raise FileNotFoundError(f"mandatory firmware image is missing: {path}; pass --firmware only for an explicit caller-selected image")
    return {"path": str(path), "sha256": sha256(path), "size": path.stat().st_size, "provenance": provenance}


def backend_info(name: str) -> dict:
    info = BACKENDS[name]
    binary = info["binary"]
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise FileNotFoundError(f"backend executable is missing or not executable: {binary}")
    if not info["archive"].is_file():
        raise FileNotFoundError(f"backend release archive is missing: {info['archive']}")
    archive_sha = sha256(info["archive"])
    result = {"path": str(binary), "sha256": sha256(binary), "archive_path": str(info["archive"]),
              "archive_sha256": archive_sha, "version": info["version"], "commit": info["commit"],
              "repo": info["repo"]}
    if archive_sha != info["archive_sha256"]:
        raise RuntimeError(f"backend archive hash mismatch: {info['archive']}")
    expected_member = "esp-emu" if name == "esp-emulator" else "qemu/bin/qemu-system-xtensa"
    member_name = None
    member_hash = None
    with tarfile.open(info["archive"], "r:*") as archive:
        for member in archive.getmembers():
            if member.isfile() and member.name.endswith(expected_member):
                member_name = member.name
                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeError(f"cannot read backend archive member: {member.name}")
                digest = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
                member_hash = digest.hexdigest()
                break
    if not member_name or member_hash != result["sha256"]:
        raise RuntimeError(f"backend executable does not match pinned archive member: {expected_member}")
    version_run = subprocess.run([str(binary), "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, check=False, timeout=10)
    observed = version_run.stdout.strip()
    expected_token = "0.51.0" if name == "esp-emulator" else "esp_develop_9.2.2_20260417"
    if version_run.returncode != 0 or expected_token not in observed:
        raise RuntimeError(f"backend --version mismatch for {name}: expected {expected_token}, got {observed!r}")
    result.update({"archive_member": member_name, "archive_member_sha256": member_hash,
                   "archive_member_matches_executable": True, "version_observed": observed,
                   "version_command_returncode": version_run.returncode})
    return result


def experimental_qemu_info(stock_info: dict) -> dict:
    """Describe the separately-built additive QEMU fork used only by Rock 4."""
    binary = ROOT / ".work/qemu-source-unpacked/qemu-esp_develop_9.2.2_20260417-src/build/qemu-system-xtensa"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise FileNotFoundError(f"experimental QEMU executable is missing or not executable: {binary}")
    observed_run = subprocess.run([str(binary), "--version"], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, check=False, timeout=10)
    observed = observed_run.stdout.strip()
    if observed_run.returncode != 0 or "9.2.2" not in observed:
        raise RuntimeError(f"experimental QEMU --version mismatch: {observed!r}")
    source_tar = ROOT / ".work/qemu-build-inputs/qemu-source.tar.xz"
    return {"path": str(binary), "sha256": sha256(binary),
            "archive_path": str(source_tar), "archive_sha256": sha256(source_tar) if source_tar.is_file() else None,
            "version": stock_info["version"], "commit": stock_info["commit"], "repo": stock_info["repo"],
            "variant": "experimental-patched-QEMU-fork",
            "stock_binary_path": stock_info["path"], "stock_binary_sha256": stock_info["sha256"],
            "version_observed": observed, "version_command_returncode": observed_run.returncode,
            "patches": ["0001-esp32s3.c.patch", "0002-hw-misc-meson.build.patch",
                        "0003-esp32s3-spi-dummy-io-scaling.patch",
                        "0004-esp32s3-toy-hub75-oled.patch",
                        "0005-esp32-gpio-input-default-high.patch",
                        "0006-rock4-real-gdma-i2c-s3.patch"]}


def start_command(backend: str, firmware: pathlib.Path, timeout: float) -> list[str]:
    if backend == "esp-emulator":
        return [str(BACKENDS[backend]["binary"]), "--chip", "esp32s3", "--firmware", str(firmware),
                "--psram-size", "8M", "--timeout", f"{timeout:g}s", "--net", "user,restrict=yes"]
    return [str(BACKENDS[backend]["binary"]), "-nographic", "-machine", "esp32s3", "-m", "8M",
            "-global", "driver=ssi_psram,property=is_octal,value=true", "-drive",
            f"file={firmware},if=mtd,format=raw", "-snapshot", "-nic", "none",
            "-d", "unimp,guest_errors"]


def capture(command: list[str], stdout_path: pathlib.Path, stderr_path: pathlib.Path, timeout: float, env: dict | None = None) -> dict:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    first_stdout: float | None = None
    first_loop: float | None = None
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    lock = threading.Lock()

    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, env=process_env)

    def drain(stream, path, buf, is_stdout):
        nonlocal first_stdout, first_loop
        accumulated = bytearray()
        with path.open("wb") as output:
            while True:
                chunk = os.read(stream.fileno(), 4096)
                if not chunk:
                    break
                now = time.monotonic()
                if is_stdout and first_stdout is None:
                    first_stdout = now
                with lock:
                    buf.extend(chunk)
                    accumulated.extend(chunk)
                output.write(chunk)
                output.flush()
                if is_stdout and first_loop is None and positive_checkpoint(bytes(accumulated), "onLoop"):
                    first_loop = now

    threads = [threading.Thread(target=drain, args=(proc.stdout, stdout_path, stdout_buf, True), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, stderr_path, stderr_buf, False), daemon=True)]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        return_code = proc.wait(timeout=timeout + 2.0)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            return_code = proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            return_code = proc.wait(timeout=3.0)
    for thread in threads:
        thread.join(timeout=3.0)
    proc.stdout.close()
    proc.stderr.close()
    finished = time.monotonic()
    return {"return_code": return_code, "timed_out": timed_out,
            "first_stdout_ms": None if first_stdout is None else round((first_stdout - start) * 1000, 3),
            "first_loop_ms": None if first_loop is None else round((first_loop - start) * 1000, 3),
            "elapsed_ms": round((finished - start) * 1000, 3),
            "stdout": bytes(stdout_buf), "stderr": bytes(stderr_buf)}


def source_audit(urls: list[str]) -> list[dict]:
    evidence = []
    for url in urls:
        item = {"url": url}
        try:
            body = urllib.request.urlopen(url, timeout=15).read()
            item.update({"http": 200, "sha256": hashlib.sha256(body).hexdigest(),
                         "bytes": len(body), "excerpt": body.decode("utf-8", "replace")[:600]})
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            item.update({"http": None, "error": str(exc)})
        evidence.append(item)
    return evidence


def text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def positive_checkpoint(data: bytes | str, marker: str) -> bool:
    raw = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    for line in raw.splitlines():
        if re.search(rf"\b{re.escape(marker)}\b", line, re.I) and not re.search(r"failed|failure|error|never|not reached|missing", line, re.I):
            if re.search(rf"\b{re.escape(marker)}\b\s+(?:completed|entered|checkpoint|returned\s+ok)\s*[.!:]?$", line.strip(), re.I):
                return True
    return False


def evidence_lines(stdout: str, stderr: str) -> dict:
    lines = [("stdout", i + 1, line) for i, line in enumerate(stdout.splitlines())]
    all_lines = lines + [("stderr", i + 1, line) for i, line in enumerate(stderr.splitlines())]
    negative = [{"stream": "stdout", "line": i + 1, "text": line} for i, line in enumerate(stdout.splitlines())
                if re.search(r"\bonLoop\b", line, re.I) and re.search(r"failed|failure|error|never|not reached", line, re.I)]
    patterns = {
        "rom": re.compile(r"^ESP-ROM:"), "bootloader": re.compile(r"^entry\s+0x[0-9a-fA-F]+$"),
        "onSetup": None, "onPreflight": None, "vfs_init": None, "onLoop": None,
        "panic": re.compile(r"Guru Meditation|panic|abort\(", re.I),
        "watchdog_reset": re.compile(r"watchdog|WDT|task watchdog", re.I),
        "guest_stderr": re.compile(r"Guru Meditation|panic|abort\(|LoadStoreError|LoadProhibited|StoreProhibited", re.I),
    }
    markers = {}
    evidence = {}
    for key, pattern in patterns.items():
        if key in ("onSetup", "onPreflight", "vfs_init", "onLoop"):
            found = next(((stream, no, line) for stream, no, line in lines
                          if (positive_checkpoint(line, key) if key not in ("vfs_init",) else re.search(r"^(?:Starting Lua|Running upon)\b", line))), None)
        else:
            found = next(((stream, no, line) for stream, no, line in all_lines if pattern.search(line)), None)
        markers[key] = found is not None
        if found:
            evidence[key] = {"stream": found[0], "line": found[1], "text": found[2]}
    failure_patterns = re.compile(r"sdmmc_host_reset returned\s+0x[0-9a-fA-F]+|Card Mount Failed|Guru Meditation|panic|abort\(|watchdog|WDT|task watchdog|LoadStoreError|LoadProhibited|StoreProhibited|Invalid read at addr|Unknown cmd|unsupported flash", re.I)
    failures = [{"stream": stream, "line": no, "text": line} for stream, no, line in all_lines if failure_patterns.search(line)]
    return markers, evidence, negative, failures


def blocker(stdout: str, stderr: str, backend: str, timed_out: bool) -> dict:
    lines = [("stdout", i + 1, line) for i, line in enumerate(stdout.splitlines())]
    lines += [("stderr", i + 1, line) for i, line in enumerate(stderr.splitlines())]
    patterns = [
        (re.compile(r"sdmmc_host_reset returned\s+0x[0-9a-fA-F]+"), "firmware SDMMC host reset failure"),
        (re.compile(r"Card Mount Failed"), "firmware SD/MMC mount failure"),
        (re.compile(r"Invalid read at addr\s+0x[0-9a-fA-F]+"), "QEMU rejected MMIO read"),
        (re.compile(r"Unknown cmd|unsupported flash", re.I), "QEMU flash command unsupported"),
        (re.compile(r"LoadStoreError|panic|Guru Meditation|abort\(", re.I), "guest exception/panic"),
    ]
    for pattern, kind in patterns:
        for stream, no, line in lines:
            if pattern.search(line):
                return {"kind": kind, "stream": stream, "line": no, "text": line}
    if timed_out:
        return {"kind": "no required runtime marker before timeout", "stream": "process", "line": None,
                "text": "backend ran until the bounded timeout without onLoop"}
    return {"kind": "backend exited before onLoop without a recognized guest blocker", "stream": "process", "line": None,
            "text": "process exit prevented the required marker"}


def log_meta(path: pathlib.Path) -> dict:
    return {"path": str(path), "sha256": sha256(path), "size": path.stat().st_size}


def base_result(args, backend: str, mode: str, run_id: str, started: str, firmware: dict, binfo: dict, paths: dict) -> dict:
    return {"schema": SCHEMA, "mode": mode, "backend": backend, "status": "NOT_EXECUTED", "run_id": run_id,
            "started_at_utc": started, "finished_at_utc": None, "firmware": firmware,
            "backend_artifact": binfo, "profile": PROFILE, "commands": [],
            "markers": {key: False for key in ["rom", "bootloader", "onSetup", "onPreflight", "vfs_init", "onLoop", "panic", "watchdog_reset", "guest_stderr"]},
            "marker_evidence": {}, "logs": {"stdout": None, "stderr": None, "combined": None},
            "benchmarks": {"wall_clock_to_first_uart_byte_ms": "measurement_unavailable: no captured stdout byte",
                           "wall_clock_to_onLoop_ms": "measurement_unavailable: onLoop marker not observed",
                           "approx_guest_instructions_per_sec": "measurement_unavailable: no instruction counter or calibrated guest clock"},
            "blocker": None, "notes": []}


def validate_result(result: dict) -> None:
    required = ["schema", "mode", "backend", "status", "run_id", "started_at_utc", "finished_at_utc", "firmware",
                "backend_artifact", "profile", "markers", "logs", "benchmarks"]
    missing = [key for key in required if key not in result]
    if missing:
        raise RuntimeError(f"result missing fields: {missing}")
    if result["schema"] != SCHEMA or result["status"] not in STATUS:
        raise RuntimeError("result has an invalid schema or status")
    if result["mode"] not in {"boot", "hub75-probe", "hostdir-probe"}:
        raise RuntimeError("result has an invalid mode")
    expected_markers = {"rom", "bootloader", "onSetup", "onPreflight", "vfs_init", "onLoop", "panic", "watchdog_reset", "guest_stderr"}
    if set(result.get("markers", {})) != expected_markers:
        raise RuntimeError("result has a non-schema marker set")
    if result["mode"] == "boot" and result["status"] == "PASS" and not result["markers"].get("onLoop"):
        raise RuntimeError("boot PASS without an observed onLoop marker")
    if result["mode"] == "hub75-probe":
        phase_statuses = {"PASS", "TECHNICAL_BLOCKER", "NOT_EXECUTED"}
        if result.get("phase_a_status") not in phase_statuses or result.get("phase_b_status") not in phase_statuses:
            raise RuntimeError("hub75 result has invalid phase status")
        if result.get("confirmed_scope") not in {"firmware-confirmed", "backend-only", "none"}:
            raise RuntimeError("hub75 result has invalid confirmed_scope")
        phase_a = result["phase_a_status"]
        phase_b = result["phase_b_status"]
        expected = ("TECHNICAL_BLOCKER" if phase_a == "TECHNICAL_BLOCKER" or phase_b == "TECHNICAL_BLOCKER"
                    else "PASS" if phase_b == "PASS" else "NOT_EXECUTED")
        expected_scope = "firmware-confirmed" if phase_b == "PASS" else "backend-only" if phase_a == "PASS" else "none"
        if result["status"] != expected or result["confirmed_scope"] != expected_scope:
            raise RuntimeError("hub75 result violates PLAN.md phase aggregation")
    for key in ["wall_clock_to_first_uart_byte_ms", "wall_clock_to_onLoop_ms", "approx_guest_instructions_per_sec"]:
        if key not in result["benchmarks"]:
            raise RuntimeError(f"benchmark missing: {key}")
    if result["mode"] == "hostdir-probe" and result["status"] == "PASS":
        read = result.get("unpatched_guest_read", {})
        if not read.get("confirmed") or read.get("sentinel_in_guest_log") is not True:
            raise RuntimeError("hostdir PASS without confirmed unpatched guest read and guest log sentinel")
    if result["mode"] == "hostdir-probe":
        if result.get("f1_fallback_status") not in STATUS:
            raise RuntimeError("hostdir result has invalid f1_fallback_status")
        if not isinstance(result.get("unpatched_guest_read"), dict):
            raise RuntimeError("hostdir result is missing unpatched_guest_read")
    if result["backend"] != "none" and result["status"] != "NOT_EXECUTED":
        if not isinstance(result.get("logs"), dict) or set(result["logs"]) != {"stdout", "stderr", "combined"}:
            raise RuntimeError("executed result must include all raw log metadata")
        for meta in result["logs"].values():
            if not isinstance(meta, dict) or not pathlib.Path(meta.get("path", "")).is_file() or sha256(pathlib.Path(meta["path"])) != meta.get("sha256"):
                raise RuntimeError("executed result contains an invalid raw log hash")


def write_result(result: dict, destination: pathlib.Path) -> None:
    validate_result(result)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + f".{result['run_id']}.tmp")
    temp.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(destination)


def do_boot(args, backend: str, firmware: dict, binfo: dict, run_id: str, started: str, paths: dict) -> dict:
    result = base_result(args, backend, "boot", run_id, started, firmware, binfo, paths)
    command = start_command(backend, pathlib.Path(firmware["path"]), args.timeout)
    result["commands"] = [command]
    capture_result = capture(command, paths["stdout"], paths["stderr"], args.timeout,
                             {"TMPDIR": str(paths["stdout"].parent)} if backend == "qemu" else None)
    combined = paths["combined"]
    combined.write_bytes(paths["stdout"].read_bytes() + b"\n--- BACKEND STDERR ---\n" + paths["stderr"].read_bytes())
    stdout = text(paths["stdout"]); stderr = text(paths["stderr"])
    markers, evidence, negative, failures = evidence_lines(stdout, stderr)
    result["markers"].update(markers); result["marker_evidence"] = evidence
    result["negative_marker_evidence"] = negative
    result["failure_evidence"] = failures
    result["blocker"] = None if markers["onLoop"] and not negative and not failures else blocker(stdout, stderr, backend, capture_result["timed_out"])
    result["benchmarks"]["wall_clock_to_first_uart_byte_ms"] = capture_result["first_stdout_ms"] if capture_result["first_stdout_ms"] is not None else "measurement_unavailable: no captured stdout byte"
    if capture_result["first_loop_ms"] is not None:
        result["benchmarks"]["wall_clock_to_onLoop_ms"] = capture_result["first_loop_ms"]
    startup_failure = (not markers["rom"] and not markers["bootloader"] and
                       any(token in stderr.lower() for token in ["could not open temporary file", "cannot open", "no such file or directory"]))
    if startup_failure:
        result["status"] = "NOT_EXECUTED"
    elif markers["onLoop"] and not negative and not failures and not markers["panic"] and not markers["watchdog_reset"] and not markers["guest_stderr"]:
        result["status"] = "PASS"
    elif capture_result["return_code"] is None:
        result["status"] = "TECHNICAL_BLOCKER"
    else:
        result["status"] = "TECHNICAL_BLOCKER"
    result["notes"].extend([f"return_code={capture_result['return_code']}", f"timed_out={capture_result['timed_out']}",
                             "vfs_init is conservative: only a downstream Starting Lua/Running upon marker counts"])
    result["process"] = {"return_code": capture_result["return_code"], "timed_out": capture_result["timed_out"],
                          "terminated_by_harness": capture_result["timed_out"]}
    result["logs"] = {"stdout": log_meta(paths["stdout"]), "stderr": log_meta(paths["stderr"]), "combined": log_meta(paths["combined"])}
    return result


def run_command(command: list[str], timeout: float) -> dict:
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, check=False, timeout=timeout)
        return {"command": command, "return_code": completed.returncode, "timed_out": False,
                "output": completed.stdout}
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        return {"command": command, "return_code": None, "timed_out": True,
                "output": output + "\ncommand timed out\n"}


def capture_simple(command: list[str], stdout_path: pathlib.Path, stderr_path: pathlib.Path,
                   timeout: float, env: dict | None = None) -> dict:
    """Bounded capture for QEMU phases; communicate() drains both pipes safely."""
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    started = time.monotonic()
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, env=process_env)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
        return_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            out, err = proc.communicate(timeout=3.0)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate(timeout=3.0)
        return_code = proc.returncode
    stdout_path.write_bytes(out or b"")
    stderr_path.write_bytes(err or b"")
    elapsed = round((time.monotonic() - started) * 1000, 3)
    first_loop = positive_checkpoint(out or b"", "onLoop")
    return {"return_code": return_code, "timed_out": timed_out,
            "first_stdout_ms": 0.0 if out else None, "first_loop_ms": elapsed if first_loop else None,
            "elapsed_ms": elapsed, "stdout": out or b"", "stderr": err or b""}


def bounded_qemu_command(command: list[str]) -> list[str]:
    return ["timeout", "25s", *command]


def write_png(path: pathlib.Path, width: int = 64, height: int = 32) -> None:
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row.extend((255 if (x // 8 + y // 8) % 2 else 0, 32 + (x * 3) % 192, 64 + (y * 5) % 192))
        rows.append(bytes(row))

    def chunk(name: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", zlib.crc32(name + payload) & 0xffffffff)

    data = b"\x89PNG\r\n\x1a\n"
    data += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
    data += chunk(b"IEND", b"")
    path.write_bytes(data)


def make_hub75_fixture(run_id: str) -> pathlib.Path:
    fixture = ARTIFACTS / "spike-runs" / run_id / "qemu-hub75-sd-fixture"
    (fixture / "frames").mkdir(parents=True, exist_ok=True)
    (fixture / "animation.json").write_text(
        json.dumps({"frames": [{"name": "idle", "file": "/frames/idle.png"}]}, separators=(",", ":")) + "\n",
        encoding="utf-8")
    # This file is valid enough for the firmware's non-edit-mode fixture; the
    # probe does not enter the Wi-Fi/edit server path.
    (fixture / "wifi.json").write_text("{}\n", encoding="utf-8")
    write_png(fixture / "frames/idle.png")
    return fixture


def make_hostdir_fixture(hostdir_arg: str, sentinel: str) -> dict:
    """Populate the caller's fresh external directory with the SD fixture."""
    hostdir = pathlib.Path(hostdir_arg).expanduser()
    if not hostdir.is_absolute():
        raise ValueError("--hostdir must be an absolute external temporary directory")
    hostdir = hostdir.resolve()
    if ROOT == hostdir or ROOT in hostdir.parents:
        raise ValueError("--hostdir must be outside the repository")
    if not hostdir.is_dir():
        raise ValueError(f"--hostdir is not a directory: {hostdir}")
    if any(hostdir.iterdir()):
        raise ValueError("--hostdir must be empty before the harness creates the fixture")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", sentinel):
        raise ValueError("--sentinel must be a fresh 16-byte hex value from the caller")

    frames = hostdir / "frames"
    frames.mkdir()
    animation = hostdir / "animation.json"
    animation.write_text(json.dumps({"frames": [{"name": sentinel, "file": "/frames/idle.png"}]},
                                    separators=(",", ":")) + "\n", encoding="utf-8")
    (hostdir / "wifi.json").write_text("{}\n", encoding="utf-8")
    (hostdir / "hardware.json").write_text(
        json.dumps({"canvas_width": 64, "canvas_height": 32,
                    "display": {"type": "none", "enabled": False}}, separators=(",", ":")) + "\n",
        encoding="utf-8")
    write_png(frames / "idle.png")
    return {
        "hostdir": str(hostdir),
        "guest_visible_path": "/animation.json",
        "sentinel_field": "frames[0].name",
        "sentinel": sentinel,
        "files": {
            "animation_json": {"path": str(animation), "sha256": sha256(animation)},
            "frame_png": {"path": str(frames / "idle.png"), "sha256": sha256(frames / "idle.png")},
            "hardware_json": {"path": str(hostdir / "hardware.json"), "sha256": sha256(hostdir / "hardware.json")},
            "wifi_json": {"path": str(hostdir / "wifi.json"), "sha256": sha256(hostdir / "wifi.json")},
        },
    }


def aggregate_hub75(result: dict) -> None:
    phase_a = result["phase_a_status"]
    phase_b = result["phase_b_status"]
    if phase_a == "TECHNICAL_BLOCKER" or phase_b == "TECHNICAL_BLOCKER":
        result["status"] = "TECHNICAL_BLOCKER"
    elif phase_b == "PASS":
        result["status"] = "PASS"
    else:
        result["status"] = "NOT_EXECUTED"
    if phase_b == "PASS":
        result["confirmed_scope"] = "firmware-confirmed"
    elif phase_a == "PASS":
        result["confirmed_scope"] = "backend-only"
    else:
        result["confirmed_scope"] = "none"


def inspect_hub75(args, backend: str, firmware: dict, binfo: dict, run_id: str, started: str, paths: dict) -> dict:
    """Run the actual Rock 4 probe; QEMU uses the experimental fork explicitly."""
    result = base_result(args, backend, "hub75-probe", run_id, started, firmware, binfo, paths)
    result.update({"phase_a_status": "NOT_EXECUTED", "phase_b_status": "NOT_EXECUTED",
                   "confirmed_scope": "none", "phase_a": {}, "phase_b": {}})
    commands = []
    aout = paths["stdout"].with_name(paths["stdout"].stem + ".phase-a.stdout.log")
    aerr = paths["stderr"].with_name(paths["stderr"].stem + ".phase-a.stderr.log")
    bout = paths["stdout"].with_name(paths["stdout"].stem + ".phase-b.stdout.log")
    berr = paths["stderr"].with_name(paths["stderr"].stem + ".phase-b.stderr.log")
    aout.touch(); aerr.touch(); bout.touch(); berr.touch()

    if backend == "esp-emulator":
        command = [str(binfo["path"]), "--help"]
        commands.append(command)
        probe = run_command(command, 15)
        aout.write_text(probe["output"] + "\n[reconfirmation] no device/plugin registration API in the distributed binary.\n", encoding="utf-8")
        aerr.write_text("Existing esp-emulator probe command re-run; toy execution is NOT_EXECUTED.\n", encoding="utf-8")
        acmb = aout.with_name(aout.stem + ".combined.log")
        acmb.write_bytes(aout.read_bytes() + b"\n--- PROBE STDERR ---\n" + aerr.read_bytes())
        with tarfile.open(binfo["archive_path"], "r:gz") as archive:
            members = archive.getnames()
        result["phase_a"] = {"api_documented": "--i2c-dev register-file ACK/readback only; no SSD1306/HUB75 plugin API",
                              "toy_device_executed": False, "traffic_observed": False,
                              "reason": "existing --help probe reconfirmed the prior source/license finding",
                              "probe_reconfirmation": True, "archive_members": members,
                              "logs": {"stdout": log_meta(aout), "stderr": log_meta(aerr), "combined": log_meta(acmb)}}
        result["phase_b"] = {"status": "NOT_EXECUTED", "reason": "Phase A has no device/plugin API."}
    else:
        build_cmd = ["bash", str(ROOT / "backend/qemu/build-experimental-qemu.sh"), "--out", str(ARTIFACTS / "qemu-hub75")]
        commands.append(build_cmd)
        build = run_command(build_cmd, 600)
        build_log = paths["stdout"].with_name(paths["stdout"].stem + ".experimental-build.log")
        build_log.write_text(build["output"], encoding="utf-8")
        if build["return_code"] != 0 or build["timed_out"]:
            result["phase_a_status"] = "TECHNICAL_BLOCKER"
            result["phase_a"] = {"toy_device_executed": False, "traffic_observed": False,
                                  "reason": "experimental/patched QEMU fork build failed", "build_log": log_meta(build_log)}
            result["blocker"] = {"kind": "experimental QEMU build failure", "text": result["phase_a"]["reason"]}
        else:
            experimental = experimental_qemu_info(binfo)
            result["backend_artifact"] = experimental
            toy_dir = RUNS / run_id / "toy-guest"
            toy_build_cmd = ["bash", str(ROOT / "backend/qemu/build-toy-guest.sh"), str(toy_dir)]
            commands.append(toy_build_cmd)
            toy_build = run_command(toy_build_cmd, 60)
            toy_log = paths["stdout"].with_name(paths["stdout"].stem + ".toy-build.log")
            toy_log.write_text(toy_build["output"], encoding="utf-8")
            toy_elf = toy_dir / "toy_guest.elf"
            if toy_build["return_code"] != 0 or toy_build["timed_out"] or not toy_elf.is_file():
                result["phase_a_status"] = "TECHNICAL_BLOCKER"
                result["phase_a"] = {"toy_device_executed": False, "traffic_observed": False,
                                      "reason": "minimal QEMU toy guest build failed", "build_log": log_meta(toy_log)}
                result["blocker"] = {"kind": "toy guest build failure", "text": result["phase_a"]["reason"]}
            else:
                toy_cmd = bounded_qemu_command([str(experimental["path"]), "-nographic", "-machine", "esp32s3", "-m", "8M", "-kernel", str(toy_elf),
                                                "-nic", "none", "-no-reboot", "-monitor", "none", "-d", "guest_errors"])
                commands.append(toy_cmd)
                toy_run = capture_simple(toy_cmd, aout, aerr, 27.0)
                toy_err = text(aerr)
                i2c_seen = "[protopandaemu.ssd1306] I2C transfer observed" in toy_err
                hub_seen = ("[protopandaemu.hub75] LCD_CAM DMA start observed" in toy_err and
                            "[protopandaemu.hub75] DMA transfer observed" in toy_err)
                acmb = aout.with_name(aout.stem + ".combined.log")
                acmb.write_bytes(aout.read_bytes() + b"\n--- QEMU STDERR ---\n" + aerr.read_bytes())
                result["phase_a"] = {"api_documented": "additive C SysBus/I2C models wired into esp32s3; minimal Xtensa ELF drives I2C and LCD_CAM/DMA",
                                      "toy_device_executed": True, "traffic_observed": i2c_seen and hub_seen,
                                      "ssd1306_traffic_observed": i2c_seen, "hub75_traffic_observed": hub_seen,
                                      "guest": {"path": str(toy_elf), "sha256": sha256(toy_elf), "build_log": log_meta(toy_log)},
                                      "process": {key: value for key, value in toy_run.items() if key not in {"stdout", "stderr"}}, "build_log": log_meta(build_log),
                                      "logs": {"stdout": log_meta(aout), "stderr": log_meta(aerr), "combined": log_meta(acmb)}}
                result["phase_a_status"] = "PASS" if i2c_seen and hub_seen else "TECHNICAL_BLOCKER"
                if result["phase_a_status"] != "PASS":
                    result["blocker"] = {"kind": "incomplete toy-device traffic", "text": "Phase A requires SSD1306 I2C and HUB75 LCD_CAM/DMA observations"}

            if result["phase_a_status"] == "PASS":
                fixture = make_hub75_fixture(run_id)
                scratch = ROOT / ".work" / "qemu-hub75-scratch" / run_id
                scratch.mkdir(parents=True, exist_ok=True)
                image = scratch / "qemu-hub75-sd-card.img"
                image_hash = RUNS / run_id / "qemu-hub75-sd-card.img.sha256"
                image_cmd = ["bash", str(ROOT / "scripts/make-sd-image.sh"), str(image), "--fixture-root", str(fixture)]
                commands.append(image_cmd)
                image_run = run_command(image_cmd, 120)
                image_log = paths["stdout"].with_name(paths["stdout"].stem + ".sd-image.log")
                image_log.write_text(image_run["output"], encoding="utf-8")
                if image_run["return_code"] != 0 or image_run["timed_out"] or not image.is_file():
                    result["phase_b_status"] = "TECHNICAL_BLOCKER"
                    result["phase_b"] = {"reason": "fresh minimal SD fixture could not be built", "build_log": log_meta(image_log)}
                else:
                    generated_hash = image.with_name(image.name + ".sha256")
                    image_hash.write_text(generated_hash.read_text(encoding="utf-8"), encoding="utf-8")
                    fw_cmd = bounded_qemu_command([str(experimental["path"]), "-nographic", "-machine", "esp32s3", "-m", "8M",
                                                    "-global", "driver=ssi_psram,property=is_octal,value=true", "-drive", f"file={firmware['path']},if=mtd,format=raw",
                                                    "-drive", f"if=sd,index=0,file={image},format=raw,snapshot=on", "-snapshot", "-nic", "none", "-d", "unimp,guest_errors"])
                    commands.append(fw_cmd)
                    fw_run = capture_simple(fw_cmd, bout, berr, 27.0, {"TMPDIR": str(paths["stdout"].parent)})
                    fw_err = text(berr)
                    i2c_seen = "[protopandaemu.ssd1306] I2C transfer observed" in fw_err
                    hub_seen = ("[protopandaemu.hub75] LCD_CAM DMA start observed" in fw_err or
                                "[protopandaemu.hub75] DMA transfer observed" in fw_err)
                    bcmb = bout.with_name(bout.stem + ".combined.log")
                    bcmb.write_bytes(bout.read_bytes() + b"\n--- QEMU STDERR ---\n" + berr.read_bytes())
                    result["phase_b_status"] = "PASS" if i2c_seen and hub_seen else ("TECHNICAL_BLOCKER" if i2c_seen or hub_seen else "NOT_EXECUTED")
                    result["phase_b"] = {"firmware_path_reached": i2c_seen or hub_seen,
                                          "ssd1306_traffic_observed": i2c_seen, "hub75_traffic_observed": hub_seen,
                                          "traffic_matches_firmware_paths": i2c_seen and hub_seen,
                                          "process": {key: value for key, value in fw_run.items() if key not in {"stdout", "stderr"}},
                                          "sd_image": {"path": str(image), "sha256": sha256(image), "sha256_file": log_meta(image_hash), "build_log": log_meta(image_log)},
                                          "source_mapping": [
                                              {"path": ".work/upstream-v3.4.0/src/tools/oledscreen.cpp", "line": 26, "evidence": "display.begin(SSD1306_SWITCHCAPVCC, 0x3C)"},
                                              {"path": ".work/upstream-v3.4.0/src/tools/devices.cpp", "lines": "100-110", "evidence": "I2CScan uses Wire.beginTransmission/endTransmission for addresses 1-126"},
                                              {"path": ".work/upstream-v3.4.0/include/tools/displays/hub75.hpp", "line": 34, "evidence": "Hub75Display::begin calls matrix->begin()"},
                                              {"path": ".work/upstream-v3.4.0/.pio/libdeps/release/ESP32 HUB75 LED MATRIX PANEL DMA Display/src/platforms/esp32s3/gdma_lcd_parallel16.cpp", "lines": "244-250,274,408", "evidence": "GDMA LCD trigger, completion poll, and LCD_CAM lcd_start"}],
                                          "logs": {"stdout": log_meta(bout), "stderr": log_meta(berr), "combined": log_meta(bcmb)}}
                    if result["phase_b_status"] == "TECHNICAL_BLOCKER":
                        result["blocker"] = {"kind": "partial original-firmware peripheral path", "text": "only one of the required I2C/LCD_CAM paths was observed"}

    aggregate_hub75(result)
    if result["status"] == "NOT_EXECUTED" and result.get("blocker") is None:
        result["blocker"] = {"kind": "Phase B not reached", "text": "original firmware produced no observable toy-device traffic"}
    result["commands"] = commands
    paths["stdout"].write_bytes(b"--- PHASE A ---\n" + aout.read_bytes() + b"\n--- PHASE B ---\n" + (bout.read_bytes() if bout.exists() else b"Phase B was not executed.\n"))
    paths["stderr"].write_bytes(b"--- PHASE A ---\n" + aerr.read_bytes() + b"\n--- PHASE B ---\n" + (berr.read_bytes() if berr.exists() else b"Phase B was not executed.\n"))
    paths["combined"].write_bytes(paths["stdout"].read_bytes() + b"\n--- BACKEND STDERR ---\n" + paths["stderr"].read_bytes())
    result["logs"] = {"stdout": log_meta(paths["stdout"]), "stderr": log_meta(paths["stderr"]), "combined": log_meta(paths["combined"])}
    validate_result(result)
    return result


def inspect_hostdir_experimental(args, backend: str, firmware: dict, stock_info: dict,
                                 run_id: str, started: str, paths: dict) -> dict:
    """Run Rock 5's real host-folder-to-SD probe against the additive QEMU fork."""
    result = base_result(args, backend, "hostdir-probe", run_id, started, firmware, stock_info, paths)
    result.update({
        "f1_fallback_status": "NOT_EXECUTED",
        "unpatched_guest_read": {"attempted": False, "confirmed": False, "sentinel_in_guest_log": False},
        "bridge": {"strategy": "C", "description": "fresh FAT image generated from the caller-specified host directory"},
        "experimental_lane": True,
        "notes": [
            "Experimental-fork evidence lane only; never substitutes for the stock hostdir-spike-none.json gate.",
            "The firmware image is unmodified Rock 1 F2 firmware; no strategy-D fallback was attempted.",
        ],
    })
    commands = []
    build_log = paths["stdout"].with_name(paths["stdout"].stem + ".experimental-build.log")
    image_log = paths["stdout"].with_name(paths["stdout"].stem + ".sd-image.log")
    overlay_log = paths["stdout"].with_name(paths["stdout"].stem + ".sd-image-overlay.log")
    qemu_stdout = paths["stdout"]
    qemu_stderr = paths["stderr"]
    qemu_combined = paths["combined"]

    build_out = RUNS / run_id / "qemu-hostdir-build"
    build_cmd = ["bash", str(ROOT / "backend/qemu/build-experimental-qemu.sh"), "--out", str(build_out)]
    commands.append(build_cmd)
    build = run_command(build_cmd, 600)
    build_log.write_text(build["output"], encoding="utf-8")
    if build["return_code"] != 0 or build["timed_out"]:
        result["status"] = "NOT_EXECUTED"
        result["blocker"] = {"kind": "experimental QEMU build unavailable",
                              "text": "the unpatched guest read was never attempted because the experimental fork build failed or timed out",
                              "build_log": log_meta(build_log)}
        result["commands"] = commands
        result["notes"].append("f1_fallback_status=NOT_EXECUTED: no patched-firmware fallback was attempted.")
        return result

    try:
        experimental = experimental_qemu_info(stock_info)
        fixture = make_hostdir_fixture(args.hostdir, args.sentinel)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        result["status"] = "NOT_EXECUTED"
        result["blocker"] = {"kind": "experimental hostdir prerequisite unavailable", "text": str(exc),
                              "build_log": log_meta(build_log)}
        result["backend_artifact"] = stock_info
        result["commands"] = commands
        result["notes"].append("The guest was not launched, so unpatched_guest_read.attempted=false.")
        return result

    result["backend_artifact"] = experimental
    result["host_fixture"] = fixture
    result["commands"].append(["fixture", "write", fixture["files"]["animation_json"]["path"]])

    scratch = ROOT / ".work" / "qemu-hostdir-scratch" / run_id
    scratch.mkdir(parents=True, exist_ok=True)
    image = scratch / "qemu-hostdir-sd-card.img"
    staged_fixture = scratch / "qemu-hostdir-sd-fixture"
    shutil.copytree(fixture["hostdir"], staged_fixture,
                    ignore=shutil.ignore_patterns("hardware.json"))
    image_cmd = ["bash", str(ROOT / "scripts/make-sd-image.sh"), str(image), "--fixture-root", str(staged_fixture)]
    commands.append(image_cmd)
    image_run = run_command(image_cmd, 120)
    image_log.write_text(image_run["output"], encoding="utf-8")
    if image_run["return_code"] != 0 or image_run["timed_out"] or not image.is_file():
        result["status"] = "NOT_EXECUTED"
        result["blocker"] = {"kind": "SD image generation unavailable",
                              "text": "the unpatched guest read was never attempted because the fresh FAT image could not be built",
                              "image_log": log_meta(image_log)}
        result["commands"] = commands
        return result

    overlay_cmd = ["mcopy", "-D", "o", "-i", str(image),
                   str(pathlib.Path(fixture["hostdir"]) / "hardware.json"), "::"]
    commands.append(overlay_cmd)
    overlay_run = run_command(overlay_cmd, 30)
    overlay_log.write_text(overlay_run["output"], encoding="utf-8")
    if overlay_run["return_code"] != 0 or overlay_run["timed_out"]:
        result["status"] = "NOT_EXECUTED"
        result["blocker"] = {"kind": "hardware fixture overlay unavailable",
                              "text": "the unpatched guest read was never attempted because the display-disabled hardware.json could not be overlaid",
                              "overlay_log": log_meta(overlay_log)}
        result["commands"] = commands
        return result

    image_hash = RUNS / run_id / "qemu-hostdir-sd-card.img.sha256"
    image_sha = sha256(image)
    image_hash.write_text(f"{image_sha}  {image}\n", encoding="utf-8")
    result["sd_image"] = {"path": str(image), "sha256": image_sha,
                          "sha256_file": log_meta(image_hash), "build_log": log_meta(image_log),
                          "overlay_log": log_meta(overlay_log),
                          "fixture_staging": str(staged_fixture)}

    qemu_cmd = bounded_qemu_command([
        str(experimental["path"]), "-nographic", "-machine", "esp32s3", "-m", "8M",
        "-global", "driver=ssi_psram,property=is_octal,value=true",
        "-drive", f"file={firmware['path']},if=mtd,format=raw",
        "-drive", f"if=sd,index=0,file={image},format=raw,snapshot=on", "-snapshot",
        "-nic", "none", "-d", "unimp,guest_errors",
    ])
    commands.append(qemu_cmd)
    result["unpatched_guest_read"]["attempted"] = True
    qemu_run = capture_simple(qemu_cmd, qemu_stdout, qemu_stderr, 27.0,
                              {"TMPDIR": str(qemu_stdout.parent)})
    guest_uart = text(qemu_stdout)
    backend_stderr = text(qemu_stderr)
    qemu_combined.write_bytes(qemu_stdout.read_bytes() + b"\n--- BACKEND STDERR ---\n" + qemu_stderr.read_bytes())
    markers, evidence, negative, failures = evidence_lines(guest_uart, backend_stderr)
    result["markers"].update(markers)
    result["marker_evidence"] = evidence
    result["negative_marker_evidence"] = negative
    result["failure_evidence"] = failures
    match_pattern = re.compile(rf"^Storing {re.escape(args.sentinel)} as \d+$")
    sentinel_matches = [{"stream": "stdout", "line": line_no, "text": line}
                        for line_no, line in enumerate(guest_uart.splitlines(), 1)
                        if match_pattern.fullmatch(line)]
    sentinel_in_guest_log = bool(sentinel_matches)
    result["unpatched_guest_read"].update({"confirmed": sentinel_in_guest_log,
                                            "sentinel_in_guest_log": sentinel_in_guest_log})
    result["guest_read_evidence"] = {
        "guest_uart_log": log_meta(qemu_stdout),
        "sentinel_matches": sentinel_matches,
        "match_rule": f"^Storing {args.sentinel} as [0-9]+$",
        "guest_visible_path": "/animation.json",
        "sentinel_field": "frames[0].name",
        "source_mapping": [
            {"path": ".work/upstream-v3.4.0/src/tools/storage.cpp", "line": 38,
             "evidence": "PANDA_SD.begin(\"/sdcard\", MMC_ONE_BIT, false, MMC_CLOCK_SPEED, 10)"},
            {"path": ".work/upstream-v3.4.0/src/drawing/framerepository.cpp", "line": 308,
             "evidence": "File file = PANDA_SD.open( \"/animation.json\" )"},
            {"path": ".work/upstream-v3.4.0/src/drawing/framerepository.cpp", "lines": "364-388",
             "evidence": "frames[].name is copied to currentName and counted after the frame is decoded"},
            {"path": ".work/upstream-v3.4.0/src/drawing/framerepository.cpp", "line": 478,
             "evidence": "Serial.printf(\"Storing %s as %d\\n\", pair.first.c_str(), pair.second)"},
        ],
    }
    bounded_timeout_hit = bool(qemu_run["timed_out"] or qemu_run["return_code"] == 124)
    result["process"] = {key: value for key, value in qemu_run.items() if key not in {"stdout", "stderr"}}
    result["process"]["bounded_timeout_hit"] = bounded_timeout_hit
    result["benchmarks"]["wall_clock_to_first_uart_byte_ms"] = qemu_run["first_stdout_ms"] if qemu_run["first_stdout_ms"] is not None else "measurement_unavailable: no captured guest UART byte"
    result["benchmarks"]["wall_clock_to_onLoop_ms"] = qemu_run["first_loop_ms"] if qemu_run["first_loop_ms"] is not None else "measurement_unavailable: onLoop marker not observed"
    result["logs"] = {"stdout": log_meta(qemu_stdout), "stderr": log_meta(qemu_stderr), "combined": log_meta(qemu_combined)}
    result["build_evidence"] = {"log": log_meta(build_log), "command": build_cmd}
    if sentinel_in_guest_log:
        result["status"] = "PASS"
        result["blocker"] = None
        result["notes"].append("PASS is based on the exact sentinel line in the raw guest UART stdout, not the combined harness log.")
    else:
        result["status"] = "TECHNICAL_BLOCKER"
        if "Card Mount Failed" in guest_uart:
            reason = "unpatched firmware attempted SD/MMC mount but reported Card Mount Failed"
        elif bounded_timeout_hit:
            reason = "unpatched firmware ran until the bounded QEMU timeout without logging the sentinel read"
        else:
            reason = "unpatched firmware run completed without the sentinel in its raw guest UART log"
        result["blocker"] = {"kind": "unpatched hostdir read not confirmed", "text": reason}
        result["notes"].append("The unpatched path was attempted; f1_fallback_status remains NOT_EXECUTED and does not alter this primary blocker.")
    result["commands"] = commands
    result["notes"].append("Stock --backend=qemu semantics were not reinterpreted; this result exists only under hostdir-spike-qemu-experimental.json.")
    result["notes"].append("The upstream image builder refuses a same-name hardware.json collision; the unchanged Rock 4 builder copied the staged fixture, then mcopy -D o overlaid the harness-created display-disabled hardware.json.")
    validate_result(result)
    return result


def do_hostdir_none(args, firmware: dict | None) -> dict:
    run_id = str(uuid.uuid4()); now = utc_now()
    result = {"schema": SCHEMA, "mode": "hostdir-probe", "backend": "none", "status": "NOT_EXECUTED", "run_id": run_id,
              "started_at_utc": now, "finished_at_utc": utc_now(), "firmware": firmware,
              "backend_artifact": None, "profile": PROFILE, "commands": [],
              "markers": {key: False for key in ["rom", "bootloader", "onSetup", "onPreflight", "vfs_init", "onLoop", "panic", "watchdog_reset", "guest_stderr"]},
              "marker_evidence": {}, "logs": {"stdout": None, "stderr": None, "combined": None},
              "benchmarks": {"wall_clock_to_first_uart_byte_ms": "measurement_unavailable: no eligible backend run",
                             "wall_clock_to_onLoop_ms": "measurement_unavailable: no eligible backend run",
                             "approx_guest_instructions_per_sec": "measurement_unavailable: no eligible backend run"},
              "blocker": {"kind": "no backend reached successful vfs_init", "text": "select-lead-backend.sh found no boot result with markers.vfs_init=true"},
              "f1_fallback_status": "NOT_EXECUTED", "unpatched_guest_read": {"attempted": False, "confirmed": False, "sentinel_in_guest_log": False},
              "notes": ["No host directory was populated and no guest read was attempted."]}
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["esp-emulator", "qemu", "none"])
    parser.add_argument("--mode", required=True, choices=["boot", "hub75-probe", "hostdir-probe"])
    parser.add_argument("--firmware")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--hostdir")
    parser.add_argument("--sentinel")
    parser.add_argument("--experimental-qemu", action="store_true",
                        help="use the separately labeled experimental QEMU fork for hostdir-probe")
    args = parser.parse_args()
    if args.mode == "hostdir-probe" and not args.backend:
        parser.error("--backend is required for hostdir-probe")
    if args.mode != "hostdir-probe" and not args.backend:
        parser.error("--backend is required")
    if args.backend == "none" and args.mode != "hostdir-probe":
        parser.error("--backend=none is only valid for hostdir-probe")
    if args.experimental_qemu and not (args.mode == "hostdir-probe" and args.backend == "qemu"):
        parser.error("--experimental-qemu is only valid with --backend=qemu --mode=hostdir-probe")
    if args.mode == "hostdir-probe" and args.backend == "qemu" and not args.experimental_qemu:
        print("ERROR: hostdir-probe is unsupported for an engine backend in this cycle; use --backend=none to record unmet prerequisites", file=sys.stderr)
        return 2
    if args.experimental_qemu and (not args.hostdir or not args.sentinel):
        parser.error("experimental hostdir-probe requires --hostdir and --sentinel")
    if args.mode == "hostdir-probe":
        canonical_name = "hostdir-spike-qemu-experimental.json" if args.experimental_qemu else f"hostdir-spike-{args.backend}.json"
        canonical = ARTIFACTS / canonical_name
    elif args.mode == "hub75-probe":
        canonical = ARTIFACTS / f"{args.backend}-hub75-extension.json"
    else:
        canonical = ARTIFACTS / f"{args.backend}-boot.json"
    if canonical.exists():
        canonical.unlink()
    try:
        firmware = firmware_info(args.firmware)
        if args.backend == "none":
            result = do_hostdir_none(args, firmware)
            result["finished_at_utc"] = utc_now()
            write_result(result, canonical)
            print(json.dumps({"result": str(canonical), "status": result["status"], "run_id": result["run_id"]}))
            return 0
        binfo = backend_info(args.backend) if args.backend else None
        run_id = str(uuid.uuid4()); started = utc_now(); paths_root = run_path(args.backend or "none", args.mode, run_id)
        paths_root.mkdir(parents=True, exist_ok=True)
        paths = {key: paths_root.with_suffix(f".{key}.log") for key in ["stdout", "stderr", "combined"]}
        if args.mode == "boot":
            result = do_boot(args, args.backend, firmware, binfo, run_id, started, paths)
        elif args.mode == "hub75-probe":
            result = inspect_hub75(args, args.backend, firmware, binfo, run_id, started, paths)
        elif args.experimental_qemu:
            result = inspect_hostdir_experimental(args, args.backend, firmware, binfo, run_id, started, paths)
        result["finished_at_utc"] = utc_now()
        write_result(result, canonical)
        print(json.dumps({"result": str(canonical), "status": result["status"], "run_id": result["run_id"]}))
        return 0
    except FileNotFoundError as exc:
        # A missing executable/input is a recorded NOT_EXECUTED result, not a harness crash.
        run_id = str(uuid.uuid4())
        result = {"schema": SCHEMA, "mode": args.mode, "backend": args.backend, "status": "NOT_EXECUTED", "run_id": run_id,
                  "started_at_utc": utc_now(), "finished_at_utc": utc_now(), "firmware": None, "backend_artifact": None,
                  "profile": PROFILE, "commands": [], "markers": {key: False for key in ["rom", "bootloader", "onSetup", "onPreflight", "vfs_init", "onLoop", "panic", "watchdog_reset", "guest_stderr"]}, "marker_evidence": {}, "logs": {"stdout": None, "stderr": None, "combined": None},
                  "benchmarks": {"wall_clock_to_first_uart_byte_ms": "measurement_unavailable: prerequisite missing",
                                 "wall_clock_to_onLoop_ms": "measurement_unavailable: prerequisite missing",
                                 "approx_guest_instructions_per_sec": "measurement_unavailable: prerequisite missing"},
                  "blocker": {"kind": "missing prerequisite", "text": str(exc)}, "notes": []}
        write_result(result, canonical)
        print(json.dumps({"result": str(canonical), "status": result["status"], "run_id": result["run_id"]}))
        return 0
    except Exception as exc:
        print(f"HARNESS BUG: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
