#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 4 ]] || { echo "Usage: $0 manifest.json docs/upstream-lock.md build-normal build-clean" >&2; exit 2; }
MANIFEST=$1
LOCK=$2
NORMAL=$3
CLEAN=$4
ROOT_DIR=$(cd "$(dirname "$MANIFEST")/.." && pwd)

[[ -f "$MANIFEST" ]] || { echo "ERROR: manifest missing: $MANIFEST" >&2; exit 1; }
[[ -f "$LOCK" ]] || { echo "ERROR: upstream lock missing: $LOCK" >&2; exit 1; }
[[ -d "$NORMAL" && -d "$CLEAN" ]] || { echo "ERROR: both build directories are required" >&2; exit 1; }

python3 - "$ROOT_DIR" "$MANIFEST" "$LOCK" "$NORMAL" "$CLEAN" <<'PY'
import hashlib
import json
import pathlib
import re
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
manifest = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
lock_path = pathlib.Path(sys.argv[3])
normal = pathlib.Path(sys.argv[4])
clean = pathlib.Path(sys.argv[5])
entries = manifest.get("artifacts")
if not isinstance(entries, list) or not entries:
    raise SystemExit("ERROR: manifest artifacts list is empty")

recorded = {}
required = {
    f"artifacts/{build}/{env}/{name}"
    for build in (normal.name, clean.name)
    for env in ("release", "debug")
    for name in ("firmware.bin", "firmware.elf", "partitions.bin",
                 "bootloader.bin", "merged_flash.bin")
}
for entry in entries:
    path = entry.get("path")
    expected = entry.get("sha256")
    if not isinstance(path, str) or not isinstance(expected, str):
        raise SystemExit(f"ERROR: malformed manifest entry: {entry!r}")
    target = root / path
    if not target.is_file():
        raise SystemExit(f"ERROR: manifest artifact missing: {path}")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != expected:
        raise SystemExit(f"ERROR: SHA-256 mismatch for {path}: {digest} != {expected}")
    if target.stat().st_size == 0:
        raise SystemExit(f"ERROR: manifest artifact is empty: {path}")
    recorded[path] = expected

missing = sorted(required - set(recorded))
if missing:
    raise SystemExit(f"ERROR: mandatory artifact set is incomplete: {missing}")

def hashes(directory):
    return {
        p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.rglob("*") if p.is_file()
    }

normal_hashes = hashes(normal)
clean_hashes = hashes(clean)
if set(normal_hashes) != set(clean_hashes):
    raise SystemExit(f"ERROR: normal/clean artifact name sets differ: {set(normal_hashes) ^ set(clean_hashes)}")
for name in sorted(normal_hashes):
    if normal_hashes[name] != clean_hashes[name]:
        raise SystemExit(f"ERROR: normal/clean SHA-256 differs for {name}")
    for build_name in (normal.name, clean.name):
        manifest_path = f"artifacts/{build_name}/{name}"
        if recorded.get(manifest_path) != normal_hashes[name]:
            raise SystemExit(f"ERROR: manifest does not record {manifest_path} correctly")

lock_text = lock_path.read_text(encoding="utf-8")
lock_hashes = dict(re.findall(
    r"^\s*-\s+`(artifacts/[^`]+)`:\s+`([0-9a-f]{64})`\s*$",
    lock_text,
    flags=re.MULTILINE,
))
if not lock_hashes:
    raise SystemExit("ERROR: upstream lock contains no artifact hashes")
for path, digest in recorded.items():
    if lock_hashes.get(path) != digest:
        raise SystemExit(f"ERROR: upstream lock hash missing or wrong for {path}")
if set(lock_hashes) != set(recorded):
    raise SystemExit("ERROR: upstream lock artifact hash set differs from manifest")

upstream = manifest.get("upstream", {})
checkout = pathlib.Path(upstream.get("checkout", "/tmp/protopanda-upstream-v3.4.0"))
if upstream.get("dirty") is not False:
    raise SystemExit("ERROR: manifest does not attest to a clean upstream checkout")
if not (checkout / ".git").exists():
    raise SystemExit(f"ERROR: upstream git checkout missing: {checkout}")
expected_commit = upstream.get("commit")
actual_commit = subprocess.check_output(
    ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
if actual_commit != expected_commit:
    raise SystemExit(f"ERROR: upstream commit changed: {actual_commit} != {expected_commit}")
dirty = subprocess.check_output(
    ["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=all"],
    text=True,
)
if dirty.strip():
    raise SystemExit(f"ERROR: upstream checkout is dirty:\n{dirty}")

print(f"Manifest verified: {len(entries)} files; normal and clean outputs are byte-identical")
PY
