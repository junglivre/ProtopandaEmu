#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 1 ]] || { echo "Usage: $0 docs/firmware-build.md" >&2; exit 2; }
DOC=$1
[[ -s "$DOC" ]] || { echo "ERROR: documentation file is missing or empty: $DOC" >&2; exit 1; }

python3 - "$DOC" <<'PY'
import pathlib
import re
import sys

text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
required = {
    "Inputs": r"inputs?",
    "Commands": r"commands?",
    "Outputs": r"outputs?",
    "Reproduce from scratch": r"reproduce from scratch",
}
for title, pattern in required.items():
    match = re.search(rf"^#{{1,6}}\s+{pattern}\s*$", text, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        raise SystemExit(f"ERROR: required section missing: {title}")
    following = text[match.end():]
    next_heading = re.search(r"^#{1,6}\s+\S", following, flags=re.MULTILINE)
    body = following[:next_heading.start()] if next_heading else following
    if not body.strip():
        raise SystemExit(f"ERROR: required section is empty: {title}")
print("Firmware build documentation verified: Inputs, Commands, Outputs, Reproduce from scratch")
PY
