#!/usr/bin/env python3
"""Isolated evidence-integrity tests; no backend process is invoked."""
import hashlib, json, pathlib, subprocess, sys, tempfile, time, unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import spike

VALIDATOR = ROOT / "scripts/validate-spike-result.py"

class SpikeIntegrityTests(unittest.TestCase):
    def make_result(self, root, mode="boot", status="PASS"):
        run_id = "isolated-run"
        firmware = root / "firmware.bin"; firmware.write_bytes(b"current-final-image")
        backend = root / "backend.bin"; backend.write_bytes(b"pinned-backend-binary")
        stdout = root / f"{run_id}.stdout.log"; stdout.write_text("ESP-ROM:fixture\nonLoop completed\nSENTINEL\n")
        stderr = root / f"{run_id}.stderr.log"; stderr.write_text("")
        combined = root / f"{run_id}.combined.log"; combined.write_bytes(stdout.read_bytes() + b"\n--- BACKEND STDERR ---\n")
        def meta(p): return {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "size": p.stat().st_size}
        markers = {k: False for k in ("rom", "bootloader", "onSetup", "onPreflight", "vfs_init", "onLoop", "panic", "watchdog_reset", "guest_stderr")}
        markers["rom"] = True; markers["onLoop"] = True
        return {
            "schema": "protopandaemu-spike/v1", "mode": mode, "backend": "qemu", "status": status,
            "run_id": run_id, "started_at_utc": "now", "finished_at_utc": "now",
            "firmware": {"path": str(firmware), "sha256": hashlib.sha256(firmware.read_bytes()).hexdigest()},
            "backend_artifact": {"path": str(backend), "sha256": hashlib.sha256(backend.read_bytes()).hexdigest(),
                                  "archive_member_matches_executable": True, "archive_member_sha256": hashlib.sha256(backend.read_bytes()).hexdigest(),
                                  "version_observed": "fixture", "version_command_returncode": 0},
            "profile": {}, "commands": [], "markers": markers,
            "marker_evidence": {"rom": {"stream":"stdout", "line":1, "text":"ESP-ROM:fixture"}, "onLoop":{"stream":"stdout", "line":2, "text":"onLoop completed"}},
            "logs": {"stdout": meta(stdout), "stderr": meta(stderr), "combined": meta(combined)},
            "benchmarks": {"wall_clock_to_first_uart_byte_ms": 1, "wall_clock_to_onLoop_ms": 2, "approx_guest_instructions_per_sec": "measurement_unavailable: fixture"},
            "negative_marker_evidence": []
        }

    def validate(self, result, root, current=None):
        path = root / "result.json"; path.write_text(json.dumps(result))
        current = current or root / "firmware.bin"
        return subprocess.run([sys.executable, str(VALIDATOR), str(path), "--current-firmware", str(current)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_first_byte_is_recorded_before_process_eof(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            command = [sys.executable, "-c", "import sys,time;sys.stdout.write('X');sys.stdout.flush();time.sleep(.5)"]
            result = spike.capture(command, root / "out", root / "err", 2)
            self.assertEqual(result["return_code"], 0)
            self.assertIsNotNone(result["first_stdout_ms"])
            self.assertGreater(result["elapsed_ms"] - result["first_stdout_ms"], 100)

    def test_missing_canonical_firmware_has_no_silent_smoke_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(spike, "ROOT", pathlib.Path(d)):
                with self.assertRaises(FileNotFoundError): spike.firmware_info(None)

    def test_stderr_onloop_and_negative_guest_text_are_not_positive_lifecycle(self):
        markers, _, negative, _ = spike.evidence_lines("onLoop failed\n", "onLoop completed\n")
        self.assertFalse(markers["onLoop"])
        self.assertTrue(negative)
        for token in ("onLoop", "not onLoop", "onLoop failed"):
            markers, _, _, _ = spike.evidence_lines(token + "\n", "")
            self.assertFalse(markers["onLoop"], token)

    def test_failure_line_cannot_be_relabelled_as_onloop_pass(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d); result = self.make_result(root)
            stdout = pathlib.Path(result["logs"]["stdout"]["path"])
            stdout.write_text("ESP-ROM:fixture\nCard Mount Failed\n")
            combined = pathlib.Path(result["logs"]["combined"]["path"])
            combined.write_bytes(stdout.read_bytes() + b"\n--- BACKEND STDERR ---\n")
            for name in ("stdout", "combined"):
                path = pathlib.Path(result["logs"][name]["path"])
                result["logs"][name]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                result["logs"][name]["size"] = path.stat().st_size
            result["marker_evidence"]["onLoop"] = {"stream": "stdout", "line": 2, "text": "Card Mount Failed"}
            self.assertNotEqual(self.validate(result, root).returncode, 0)

    def test_missing_or_tampered_logs_and_missing_logs_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d); result = self.make_result(root)
            good = self.validate(result, root); self.assertEqual(good.returncode, 0, good.stderr.decode())
            pathlib.Path(result["logs"]["stdout"]["path"]).write_text("tampered\n")
            self.assertNotEqual(self.validate(result, root).returncode, 0)
            result = self.make_result(root); result["logs"] = {}
            self.assertNotEqual(self.validate(result, root).returncode, 0)

    def test_prior_firmware_cannot_be_eligible(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d); result = self.make_result(root)
            old = root / "old.bin"; old.write_bytes(b"old-image")
            result["firmware"] = {"path": str(old), "sha256": hashlib.sha256(old.read_bytes()).hexdigest()}
            self.assertNotEqual(self.validate(result, root, root / "firmware.bin").returncode, 0)
            self.assertNotEqual(self.validate(result, root, root / "missing-current.bin").returncode, 0)

    def test_f1_fallback_cannot_satisfy_f2_pass(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d); result = self.make_result(root, mode="hostdir-probe")
            result["sentinel"] = "SENTINEL"; result["f1_fallback_status"] = "PASS"
            result["unpatched_guest_read"] = {"confirmed": False, "sentinel_in_guest_log": True}
            self.assertNotEqual(self.validate(result, root).returncode, 0)

if __name__ == "__main__": unittest.main()
