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
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
import uuid

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
    for key in ["wall_clock_to_first_uart_byte_ms", "wall_clock_to_onLoop_ms", "approx_guest_instructions_per_sec"]:
        if key not in result["benchmarks"]:
            raise RuntimeError(f"benchmark missing: {key}")
    if result["mode"] == "hostdir-probe" and result["status"] == "PASS":
        read = result.get("unpatched_guest_read", {})
        if not read.get("confirmed") or read.get("sentinel_in_guest_log") is not True:
            raise RuntimeError("hostdir PASS without confirmed unpatched guest read and guest log sentinel")
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


def inspect_hub75(args, backend: str, firmware: dict, binfo: dict, run_id: str, started: str, paths: dict) -> dict:
    paths["stdout"].parent.mkdir(parents=True, exist_ok=True)
    result = base_result(args, backend, "hub75-probe", run_id, started, firmware, binfo, paths)
    result["phase_a_status"] = "NOT_EXECUTED"; result["phase_b_status"] = "NOT_EXECUTED"; result["confirmed_scope"] = "none"
    result["phase_a"] = {}; result["phase_b"] = {}
    commands = []
    if backend == "esp-emulator":
        commands = [[str(binfo["path"]), "--help"]]
        archive_members = []
        with tarfile.open(binfo["archive_path"], "r:gz") as archive:
            archive_members = archive.getnames()
        result["phase_a"] = {"api_documented": "--i2c-dev register-file ACK/readback only; no SSD1306/HUB75 plugin API in the pinned binary/archive",
                              "toy_device_executed": False, "traffic_observed": False,
                              "reason": "release archive contains only the executable; the public distribution does not include a Rust core/device SDK",
                              "archive_members": archive_members,
                              "source_audit": source_audit(["https://raw.githubusercontent.com/espressif/esp-emulator/v0.51.0/README.md", "https://raw.githubusercontent.com/espressif/esp-emulator/v0.51.0/LICENSE"])}
    else:
        commands = [[str(binfo["path"]), "-machine", "help"], [str(binfo["path"]), "-device", "help"], [str(binfo["path"]), "-help"]]
        result["phase_a"] = {"api_documented": "QEMU C device model source and TCG plugin API are available; the plugin API is instrumentation, not a peripheral model registration API",
                              "toy_device_executed": False, "traffic_observed": False,
                              "source_paths": ["hw/i2c/esp32_i2c.c", "hw/dma/esp32s3_gdma.c", "hw/sd/dwc_sdmmc.c", "include/qemu/qemu-plugin.h"],
                              "reason": "no compiled toy device model or minimal guest was built/executed in this cycle",
                              "source_audit": source_audit([
                                  "https://raw.githubusercontent.com/espressif/qemu/40edccac415693c5130f91c01d84176ae6008566/hw/i2c/esp32_i2c.c",
                                  "https://raw.githubusercontent.com/espressif/qemu/40edccac415693c5130f91c01d84176ae6008566/hw/dma/esp32s3_gdma.c",
                                  "https://raw.githubusercontent.com/espressif/qemu/40edccac415693c5130f91c01d84176ae6008566/include/qemu/qemu-plugin.h"])}
    outputs = []
    for command in commands:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        outputs.append({"command": command, "return_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr})
    paths["stdout"].write_text(json.dumps(outputs, indent=2) + "\n", encoding="utf-8")
    paths["stderr"].write_text("API inspection command stderr is embedded in the structured stdout capture; no guest run was attempted.\n", encoding="utf-8")
    paths["combined"].write_bytes(paths["stdout"].read_bytes() + b"\n--- INSPECTION STDERR ---\n" + paths["stderr"].read_bytes())
    result["commands"] = commands; result["status"] = "NOT_EXECUTED"; result["blocker"] = {"kind": "toy device execution not attempted", "text": result["phase_a"]["reason"]}
    result["notes"].append("Phase B is NOT_EXECUTED because no toy device traffic was observed and the original firmware did not reach the required downstream peripheral path.")
    result["logs"] = {"stdout": log_meta(paths["stdout"]), "stderr": log_meta(paths["stderr"]), "combined": log_meta(paths["combined"])}
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
    args = parser.parse_args()
    if args.mode == "hostdir-probe" and not args.backend:
        parser.error("--backend is required for hostdir-probe")
    if args.mode != "hostdir-probe" and not args.backend:
        parser.error("--backend is required")
    if args.backend == "none" and args.mode != "hostdir-probe":
        parser.error("--backend=none is only valid for hostdir-probe")
    if args.mode == "hostdir-probe" and args.backend != "none":
        print("ERROR: hostdir-probe is unsupported for an engine backend in this cycle; use --backend=none to record unmet prerequisites", file=sys.stderr)
        return 2
    if args.mode == "hostdir-probe":
        canonical = ARTIFACTS / f"hostdir-spike-{args.backend}.json"
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
