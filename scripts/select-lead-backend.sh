#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: scripts/select-lead-backend.sh

Validates each current boot result with validate-spike-result.py, then prints
backends whose verified result has markers.vfs_init=true.
EOF
  exit 0
fi
python3 - "$ROOT_DIR" <<'PY'
import json, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
for backend in ("esp-emulator", "qemu"):
    path = root / "artifacts" / f"{backend}-boot.json"
    if not path.is_file():
        continue
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        continue
    if result.get("backend") != backend or result.get("mode") != "boot":
        continue
    checked = subprocess.run([sys.executable, str(root / "scripts/validate-spike-result.py"), str(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if checked.returncode == 0 and result.get("markers", {}).get("vfs_init") is True:
        print(backend)
PY
