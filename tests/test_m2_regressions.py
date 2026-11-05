"""Behavioral M2 boundary regressions that must reject before QEMU spawn."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class M2ProfileBoundaryTests(unittest.TestCase):
    def test_malformed_json_types_produce_structured_cli_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {
                "firmware_image": "not-consumed-after-schema-rejection.bin",
                "firmware_sha256": "0" * 64,
                "board_profile": {
                    "chip": "esp32s3", "cores": 2, "cpu_mhz": 240,
                    "flash_bytes": 16777216, "psram_bytes": 8388608, "psram_mode": "octal",
                },
                "log_level": [],
            }
            profile = root / "malformed.json"
            report = root / "report.json"
            profile.write_text(json.dumps(base), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "protopandaemu", "run", "--profile", str(profile),
                 "--report", str(report), "--timeout", "nan"],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertTrue(report.is_file())
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse(data["qemu_started"])
            self.assertEqual(data["error_code"], "profile_invalid")
            self.assertIn("log_level", data["error"])
            self.assertNotIn("TypeError", result.stderr)


if __name__ == "__main__":
    unittest.main()
