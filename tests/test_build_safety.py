"""Build-runner data safety and artifact integrity, without compiler downloads."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BuildSafetyTests(unittest.TestCase):
    def test_outside_output_is_rejected_without_destroying_data(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "keep.txt"
            sentinel.write_text("user data", encoding="utf-8")
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/build-firmware.sh"), "--out", directory],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "user data")

    def verify_fixture(self, artifact_hash):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            artifacts = fixture / "artifacts"
            normal = artifacts / "build-normal"
            clean = artifacts / "build-clean"
            for build in (normal, clean):
                (build / "release").mkdir(parents=True)
                (build / "release/firmware.bin").write_bytes(b"integrity-test-data")
            lock = fixture / "lock.md"
            lock.write_text("test lock", encoding="utf-8")
            manifest = artifacts / "manifest.json"
            manifest.write_text(json.dumps({"artifacts": [{
                "path": "artifacts/build-normal/release/firmware.bin",
                "sha256": artifact_hash,
            }]}), encoding="utf-8")
            return subprocess.run(
                ["bash", str(ROOT / "scripts/verify-manifest.sh"), str(manifest),
                 str(lock), str(normal), str(clean)],
                cwd=ROOT, text=True, capture_output=True,
            )

    def test_changed_bytes_cannot_pass_hash_verification(self):
        result = self.verify_fixture("0" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA-256 mismatch", result.stderr)

    def test_partial_artifact_set_cannot_pass(self):
        result = self.verify_fixture(hashlib.sha256(b"integrity-test-data").hexdigest())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mandatory artifact set is incomplete", result.stderr)


if __name__ == "__main__":
    unittest.main()
