#!/usr/bin/env python3
"""Fail-closed validation for one spike result and its raw evidence."""
import argparse, hashlib, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import spike
SCHEMA = "protopandaemu-spike/v1"
STATUSES = {"PASS", "TECHNICAL_BLOCKER", "NOT_EXECUTED"}
BACKENDS = {"esp-emulator", "qemu", "none"}
MODES = {"boot", "hub75-probe", "hostdir-probe"}
LIFECYCLE = {"rom", "bootloader", "onSetup", "onPreflight", "vfs_init", "onLoop"}

def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()

def fail(message):
    raise ValueError(message)

def checked_log(meta, run_id):
    if not isinstance(meta, dict): fail("missing log metadata")
    for key in ("path", "sha256", "size"):
        if key not in meta: fail(f"log metadata missing {key}")
    path = pathlib.Path(meta["path"])
    if run_id not in str(path): fail("log path is not bound to result run_id")
    if not path.is_file(): fail(f"log missing: {path}")
    if meta["size"] != path.stat().st_size or digest(path) != meta["sha256"]:
        fail(f"log hash/size mismatch: {path}")
    return path

def main():
    ap = argparse.ArgumentParser(description="Validate one ProtopandaEmu spike result")
    ap.add_argument("result")
    ap.add_argument("--current-firmware", default=str(ROOT / "artifacts/build-normal/release/merged_flash.bin"))
    args = ap.parse_args()
    data = json.loads(pathlib.Path(args.result).read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA: fail("invalid schema")
    if data.get("status") not in STATUSES: fail("invalid status")
    if data.get("mode") not in MODES: fail("invalid mode")
    if data.get("backend") not in BACKENDS: fail("invalid backend")
    if not isinstance(data.get("run_id"), str) or not data["run_id"]: fail("missing run_id")
    markers = data.get("markers")
    expected_markers = {"rom", "bootloader", "onSetup", "onPreflight", "vfs_init", "onLoop", "panic", "watchdog_reset", "guest_stderr"}
    if not isinstance(markers, dict) or set(markers) != expected_markers: fail("marker set is not schema-exact")
    benchmarks = data.get("benchmarks")
    if not isinstance(benchmarks, dict) or any(key not in benchmarks for key in ("wall_clock_to_first_uart_byte_ms", "wall_clock_to_onLoop_ms", "approx_guest_instructions_per_sec")):
        fail("benchmark set is incomplete")
    prerequisite_not_executed = data["status"] == "NOT_EXECUTED" and data.get("blocker", {}).get("kind") == "missing prerequisite"

    firmware = data.get("firmware")
    backend = data.get("backend_artifact")
    if prerequisite_not_executed:
        print("valid spike result: prerequisite not executed")
        return
    if data["backend"] == "none":
        if data["mode"] != "hostdir-probe": fail("backend none is only valid for hostdir-probe")
    else:
        if not isinstance(firmware, dict) or not pathlib.Path(firmware.get("path", "")).is_file(): fail("firmware missing")
        if digest(pathlib.Path(firmware["path"])) != firmware.get("sha256"): fail("firmware hash mismatch")
        current = pathlib.Path(args.current_firmware)
        if not current.is_file(): fail(f"current canonical firmware is missing: {current}")
        if pathlib.Path(firmware["path"]).resolve() != current.resolve() or firmware["sha256"] != digest(current):
            fail("result uses a firmware different from current canonical firmware")
        if not isinstance(backend, dict) or not pathlib.Path(backend.get("path", "")).is_file(): fail("backend missing")
        if digest(pathlib.Path(backend["path"])) != backend.get("sha256"): fail("backend hash mismatch")
        if backend.get("archive_member_matches_executable") is not True or backend.get("archive_member_sha256") != backend.get("sha256"):
            fail("backend archive/executable relation is not proven")
        if not backend.get("version_observed") or backend.get("version_command_returncode") != 0: fail("backend version was not observed successfully")

    logs = data.get("logs")
    if data["backend"] != "none":
        if not isinstance(logs, dict) or set(logs) != {"stdout", "stderr", "combined"}: fail("executed result must have stdout/stderr/combined metadata")
        stdout = checked_log(logs["stdout"], data["run_id"])
        stderr = checked_log(logs["stderr"], data["run_id"])
        combined = checked_log(logs["combined"], data["run_id"])
        if combined.stat().st_size == 0: fail("combined evidence log is empty")
    else:
        stdout = stderr = combined = None

    evidence = data.get("marker_evidence")
    if not isinstance(evidence, dict): fail("marker_evidence missing")
    for marker, present in markers.items():
        if present:
            item = evidence.get(marker)
            if not isinstance(item, dict) or item.get("stream") not in {"stdout", "stderr"} or not isinstance(item.get("line"), int) or not item.get("text"):
                fail(f"positive marker lacks evidence: {marker}")
            if marker in LIFECYCLE and item["stream"] != "stdout": fail(f"lifecycle marker is not guest stdout: {marker}")
            path = stdout if item["stream"] == "stdout" else stderr
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if item["line"] < 1 or item["line"] > len(lines) or lines[item["line"] - 1] != item["text"]:
                fail(f"marker evidence does not match raw log: {marker}")
    if markers["vfs_init"] and evidence["vfs_init"]["stream"] != "stdout": fail("vfs_init is not guest stdout evidence")

    if data["mode"] == "boot":
        derived_markers, derived_evidence, derived_negative, derived_failures = spike.evidence_lines(
            stdout.read_text(encoding="utf-8", errors="replace"), stderr.read_text(encoding="utf-8", errors="replace"))
        if markers != derived_markers: fail("recorded markers do not match shared stdout/stderr evidence derivation")
        if evidence != derived_evidence: fail("recorded marker evidence does not match shared derivation")
        if data.get("negative_marker_evidence", []) != derived_negative: fail("negative marker evidence does not match shared derivation")
        if data.get("failure_evidence", []) != derived_failures: fail("explicit failure evidence does not match shared derivation")

    if data["mode"] == "boot" and data["status"] == "PASS":
        if not markers["onLoop"] or markers["panic"] or markers["watchdog_reset"] or markers["guest_stderr"] or data.get("negative_marker_evidence") or data.get("failure_evidence"):
            fail("boot PASS has missing/negative/failure evidence")
    if data["mode"] == "hostdir-probe" and data["status"] == "PASS":
        read = data.get("unpatched_guest_read", {})
        if read.get("confirmed") is not True or read.get("sentinel_in_guest_log") is not True: fail("hostdir PASS is not a confirmed unpatched guest read")
        if data.get("f1_fallback_status") == "PASS" and read.get("confirmed") is not True: fail("F1 fallback cannot satisfy F2 PASS")
        sentinel = data.get("sentinel")
        if not sentinel or sentinel not in stdout.read_text(encoding="utf-8", errors="replace"): fail("hostdir sentinel is absent from guest stdout")
    print("valid spike result")

if __name__ == "__main__":
    try: main()
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print("INVALID: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
