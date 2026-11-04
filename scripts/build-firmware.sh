#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/build-firmware.sh [--clean] --out <directory>

Builds the upstream Proto-panda v3.4.0 release and debug environments, copies
the real PlatformIO outputs and merged flash images into <directory>, and
refreshes artifacts/manifest.json when both build directories are present.
EOF
}

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
UPSTREAM_DIR=${PROTO_PANDA_UPSTREAM_DIR:-$ROOT_DIR/.work/upstream-v3.4.0}
export PLATFORMIO_CORE_DIR=${PLATFORMIO_CORE_DIR:-$ROOT_DIR/.work/platformio-core}
UPSTREAM_URL=https://github.com/mockthebear/Proto-panda
UPSTREAM_TAG=v3.4.0
UPSTREAM_COMMIT=97354713254a37691c250fd013bde108183174e5
OUT_DIR=
CLEAN=0

while (($#)); do
  case "$1" in
    --clean) CLEAN=1; shift ;;
    --out)
      (($# >= 2)) || { usage; exit 2; }
      OUT_DIR=$2
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

[[ -n "$OUT_DIR" ]] || { echo "--out is required" >&2; usage; exit 2; }
[[ "$OUT_DIR" = /* ]] || OUT_DIR="$ROOT_DIR/$OUT_DIR"

OUT_DIR=$(realpath -m "$OUT_DIR")
ARTIFACTS_DIR="$ROOT_DIR/artifacts"
EXPECTED_PARENT=$(realpath -m "$ARTIFACTS_DIR")
if [[ "$OUT_DIR" != "$EXPECTED_PARENT/build-normal" && "$OUT_DIR" != "$EXPECTED_PARENT/build-clean" ]]; then
  echo "ERROR: --out must be artifacts/build-normal or artifacts/build-clean; refusing unsafe path: $OUT_DIR" >&2
  exit 2
fi
if [[ -L "$ARTIFACTS_DIR" || -L "$OUT_DIR" ]]; then
  echo "ERROR: --out or its artifacts parent must not be a symlink" >&2
  exit 2
fi
if [[ -e "$OUT_DIR" && ! -d "$OUT_DIR" ]]; then
  echo "ERROR: --out exists but is not a directory: $OUT_DIR" >&2
  exit 2
fi
mkdir -p "$OUT_DIR"
for existing in "$OUT_DIR"/*; do
  [[ -e "$existing" ]] || continue
  case "$(basename "$existing")" in
    release|debug) [[ -d "$existing" ]] || { echo "ERROR: unexpected non-directory in output: $existing" >&2; exit 2; } ;;
    *) echo "ERROR: refusing to remove unrelated pre-existing output: $existing" >&2; exit 2 ;;
  esac
done

mkdir -p "$ROOT_DIR/.work" "$ARTIFACTS_DIR/logs"
LOG_NAME=$(basename "$OUT_DIR")
exec > >(tee -a "$ARTIFACTS_DIR/logs/${LOG_NAME}.log") 2>&1

if ! command -v pio >/dev/null 2>&1; then
  echo "ERROR: PlatformIO CLI 'pio' is not installed or not on PATH" >&2
  exit 127
fi
[[ "$(pio --version)" = "PlatformIO Core, version 6.2.0" ]] || {
  echo "ERROR: this build requires PlatformIO Core 6.2.0" >&2
  exit 2
}

if [[ ! -f "$UPSTREAM_DIR/platformio.ini" ]] || [[ ! -d "$UPSTREAM_DIR/.git" ]]; then
  mkdir -p "$(dirname "$UPSTREAM_DIR")"
  if [[ -e "$UPSTREAM_DIR" ]]; then
    echo "ERROR: upstream path exists but is not a valid git checkout: $UPSTREAM_DIR" >&2
    exit 2
  fi
  echo "+ sparse clone $UPSTREAM_URL@$UPSTREAM_TAG"
  git clone --filter=blob:none --no-checkout --branch "$UPSTREAM_TAG" --depth 1 "$UPSTREAM_URL" "$UPSTREAM_DIR"
  git -C "$UPSTREAM_DIR" sparse-checkout set src include lib
  git -C "$UPSTREAM_DIR" checkout "$UPSTREAM_COMMIT"
fi

ACTUAL_COMMIT=$(git -C "$UPSTREAM_DIR" rev-parse HEAD)
[[ "$ACTUAL_COMMIT" = "$UPSTREAM_COMMIT" ]] || {
  echo "ERROR: upstream checkout is $ACTUAL_COMMIT, expected $UPSTREAM_COMMIT ($UPSTREAM_TAG)" >&2
  exit 2
}

if [[ -n "$(git -C "$UPSTREAM_DIR" status --porcelain --untracked-files=all)" ]]; then
  echo "ERROR: upstream checkout has dirty firmware sources: $UPSTREAM_DIR" >&2
  git -C "$UPSTREAM_DIR" status --short >&2
  exit 2
fi

if [[ "$CLEAN" -eq 1 ]]; then
  echo "+ rm -rf $UPSTREAM_DIR/.pio/build (clean generated build cache only)"
  rm -rf "$UPSTREAM_DIR/.pio/build"
fi

# GCC honors SOURCE_DATE_EPOCH for __DATE__/__TIME__ in Arduino's diagnostics.
# Pin build metadata without modifying any upstream source or compiler flags.
export SOURCE_DATE_EPOCH
SOURCE_DATE_EPOCH=$(git -C "$UPSTREAM_DIR" show -s --format=%ct "$UPSTREAM_COMMIT")
export TZ=UTC

# Upstream leaves these transitive requirements open; lock them without saving
# changes to its platformio.ini.
(cd "$UPSTREAM_DIR" && pio pkg install -e release -e debug --no-save \
  --library "adafruit/Adafruit BusIO@1.17.4" --tool "tasmota/tool-mklittlefs@3.2.0")

echo "Upstream: $UPSTREAM_URL@$UPSTREAM_TAG ($ACTUAL_COMMIT)"
echo "PlatformIO: $(pio --version)"
echo "Build output: $OUT_DIR"

echo "+ pio run -e release -e debug"
(cd "$UPSTREAM_DIR" && pio run -e release -e debug)

copy_required() {
  local env=$1
  local source=$2
  local name=$3
  [[ -f "$source" ]] || { echo "ERROR: missing $env artifact: $source" >&2; exit 1; }
  local destination="$OUT_DIR/$env/$name"
  [[ ! -L "$OUT_DIR/$env" && ! -L "$destination" ]] || {
    echo "ERROR: refusing a symlink in generated output: $destination" >&2
    exit 2
  }
  mkdir -p "$(dirname "$destination")"
  cp -f "$source" "$destination"
}

merge_flash() {
  local env=$1
  local build_dir="$UPSTREAM_DIR/.pio/build/$env"
  local envdump="$ROOT_DIR/.work/envdump-$env.txt"
  local merged_tmp="$ROOT_DIR/.work/${env}-merged.bin"
  echo "+ pio run -e $env -t envdump > $envdump"
  (cd "$UPSTREAM_DIR" && pio run -e "$env" -t envdump > "$envdump")
  [[ -s "$envdump" ]] || { echo "ERROR: empty PlatformIO SCons envdump: $envdump" >&2; exit 1; }
  local uploader
  uploader=$(sed -n "s/^  'UPLOADER': '\(.*\)',/\1/p" "$envdump")
  [[ -f "$uploader" ]] || { echo "ERROR: SCons UPLOADER is missing: $uploader" >&2; exit 1; }
  mapfile -t args < <(python3 - "$envdump" "$UPSTREAM_DIR" "$build_dir" "$merged_tmp" "$OUT_DIR/$env" <<'PY'
import ast
import csv
import pathlib
import re
import sys

envdump = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
upstream = pathlib.Path(sys.argv[2])
build_dir = pathlib.Path(sys.argv[3])
merged = sys.argv[4]
output_dir = pathlib.Path(sys.argv[5])

def scalar(name):
    match = re.search(rf"['\"]{re.escape(name)}['\"]\s*:\s*([^,\n]+)", envdump)
    if not match:
        raise SystemExit(f"ERROR: {name} missing from PlatformIO SCons envdump")
    value = match.group(1).strip()
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return value.strip("'\"")

def list_value(name):
    marker = re.search(rf"['\"]{re.escape(name)}['\"]\s*:\s*\[", envdump)
    if not marker:
        raise SystemExit(f"ERROR: {name} missing from PlatformIO SCons envdump")
    start = marker.end() - 1
    depth = 0
    quote = None
    escaped = False
    for index in range(start, len(envdump)):
        char = envdump[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return ast.literal_eval(envdump[start:index + 1])
    raise SystemExit(f"ERROR: unterminated {name} in PlatformIO SCons envdump")

board = str(scalar("BOARD")).lower()
mcu = str(scalar("BOARD_MCU")).lower()
if "esp32-s3" not in board or mcu != "esp32s3":
    raise SystemExit(f"ERROR: SCons envdump is not ESP32-S3: BOARD={board!r}, MCU={mcu!r}")

config = (upstream / "platformio.ini").read_text(encoding="utf-8")
if not re.search(r"^\s*board\s*=\s*esp32-s3-devkitc-1\s*$", config, re.MULTILINE):
    raise SystemExit("ERROR: upstream board is not esp32-s3-devkitc-1")
if set(re.findall(r"^\s*board_upload\.flash_size\s*=\s*(\S+)\s*$", config, re.MULTILINE)) != {"16MB"}:
    raise SystemExit("ERROR: upstream board_upload.flash_size is not exactly 16MB")
if set(re.findall(r"^\s*board_build\.f_cpu\s*=\s*(\S+)\s*$", config, re.MULTILINE)) != {"240000000L"}:
    raise SystemExit("ERROR: upstream board_build.f_cpu is not exactly 240000000L")
if set(re.findall(r"^\s*board_build\.arduino\.memory_type\s*=\s*(\S+)\s*$", config, re.MULTILINE)) != {"qio_opi"}:
    raise SystemExit("ERROR: upstream Arduino memory type is not qio_opi")

partition_csv = pathlib.Path(str(scalar("PARTITIONS_TABLE_CSV")))
if not partition_csv.is_file() or partition_csv.name != "app3M_fat9M_16MB.csv":
    raise SystemExit(f"ERROR: unexpected PlatformIO partition CSV: {partition_csv}")
app_offset = None
with partition_csv.open(newline="", encoding="utf-8") as handle:
    for row in csv.reader(handle):
        fields = [field.strip() for field in row]
        if not fields or fields[0].startswith("#") or len(fields) < 5:
            continue
        if fields[0] == "app0" and fields[1] == "app":
            app_offset = int(fields[3], 0)
            if int(fields[4], 0) != 0x300000:
                raise SystemExit("ERROR: app0 size changed from 0x300000")
            break
if app_offset != 0x10000:
    raise SystemExit(f"ERROR: unexpected app0 offset: {app_offset!r}")

images = []
for item in list_value("FLASH_EXTRA_IMAGES"):
    if not isinstance(item, (list, tuple)) or len(item) != 2:
        continue
    address, source = str(item[0]), pathlib.Path(str(item[1]))
    if not source.is_absolute():
        source = build_dir / source
    images.append((address, source))
if [address for address, _ in images] != ["0x0000", "0x8000", "0xe000"]:
    raise SystemExit(f"ERROR: unexpected SCons flash image offsets: {images}")
firmware = build_dir / "firmware.bin"
if not firmware.is_file():
    raise SystemExit(f"ERROR: firmware image missing: {firmware}")
images = [(address, output_dir / source.name if (output_dir / source.name).is_file() else source)
          for address, source in images]
firmware = output_dir / "firmware.bin" if (output_dir / "firmware.bin").is_file() else firmware
images.append((hex(app_offset), firmware))

flash_clock = str(scalar("BOARD_F_FLASH")).rstrip("L")
try:
    flash_freq = f"{int(flash_clock) // 1000000}m"
except ValueError:
    raise SystemExit(f"ERROR: invalid BOARD_F_FLASH: {flash_clock!r}")
args = ["--chip", "esp32s3", "merge_bin", "--fill-flash-size", "16MB", "-o", merged,
        "--flash_mode", str(scalar("BOARD_FLASH_MODE")), "--flash_freq", flash_freq,
        "--flash_size", "16MB"]
for address, source in images:
    if not source.is_file():
        raise SystemExit(f"ERROR: SCons upload image missing: {source}")
    args.extend((address, str(source)))

for item in args:
    print(item)
print(f"# derived BOARD={board} MCU={mcu} FLASH_SIZE=16MB APP_OFFSET={hex(app_offset)}", file=sys.stderr)
PY
  )
  ((${#args[@]} > 0)) || { echo "ERROR: no merge arguments derived" >&2; exit 1; }
  echo "+ python3 $uploader ${args[*]}"
  python3 "$uploader" "${args[@]}"
  copy_required "$env" "$merged_tmp" merged_flash.bin
  rm -f "$merged_tmp"
}

for env in release debug; do
  mkdir -p "$OUT_DIR/$env"
  build_dir="$UPSTREAM_DIR/.pio/build/$env"
  copy_required "$env" "$build_dir/firmware.bin" firmware.bin
  copy_required "$env" "$build_dir/firmware.elf" firmware.elf
  copy_required "$env" "$build_dir/partitions.bin" partitions.bin
  copy_required "$env" "$build_dir/bootloader.bin" bootloader.bin
  if [[ -f "$build_dir/boot_app0.bin" ]]; then
    cp -f "$build_dir/boot_app0.bin" "$OUT_DIR/$env/boot_app0.bin"
  fi
  merge_flash "$env"
  partition_source=$(sed -n "s/^  'PARTITIONS_TABLE_CSV': '\(.*\)',/\1/p" "$ROOT_DIR/.work/envdump-$env.txt")
  copy_required "$env" "$partition_source" partition-source.csv
done

python3 - "$ROOT_DIR" "$UPSTREAM_DIR" "$OUT_DIR" "$UPSTREAM_URL" "$UPSTREAM_TAG" "$ACTUAL_COMMIT" "$PLATFORMIO_CORE_DIR" <<'PY'
import hashlib
import os
import json
import pathlib
import sys
from datetime import datetime, timezone

root = pathlib.Path(sys.argv[1])
upstream = pathlib.Path(sys.argv[2])
out = pathlib.Path(sys.argv[3])
url, tag, commit = sys.argv[4:7]
platformio_core = pathlib.Path(sys.argv[7])
manifest_path = root / "artifacts" / "manifest.json"
manifest_path.parent.mkdir(parents=True, exist_ok=True)

def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

entries = []
build_root = out
if not build_root.is_dir():
    raise SystemExit(f"ERROR: build output directory is missing: {build_root}")
for path in sorted(p for p in build_root.rglob("*") if p.is_file()):
    rel = path.relative_to(root).as_posix()
    entries.append({
        "path": rel,
        "sha256": sha256(path),
        "size": path.stat().st_size,
    })

for build_name in ("build-normal", "build-clean"):
    if build_name == out.name:
        continue
    other_root = root / "artifacts" / build_name
    if not other_root.is_dir():
        continue
    for path in sorted(p for p in other_root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        entries.append({
            "path": rel,
            "sha256": sha256(path),
            "size": path.stat().st_size,
        })

packages = {}
metadata_paths = [
    *list((platformio_core / "platforms").glob("*/platform.json")),
    *list((platformio_core / "packages").glob("*/package.json")),
]
for base in (upstream / ".pio" / "libdeps",):
    if not base.exists():
        continue
    metadata_paths.extend(base.rglob("package.json"))
    metadata_paths.extend(base.rglob(".piopm"))
for metadata in sorted(set(metadata_paths)):
        try:
            data = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = data.get("name")
        version = data.get("version")
        if name and version:
            packages[str(name)] = str(version)

payload = {
    "schema": 1,
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "upstream": {"url": url, "tag": tag, "commit": commit,
                 "checkout": str(upstream), "dirty": False},
    "resolved_packages": dict(sorted(packages.items())),
    "artifacts": entries,
    "source_date_epoch": int(os.environ["SOURCE_DATE_EPOCH"]),
}
manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
print(f"Manifest refreshed: {manifest_path} ({len(entries)} files)")
lock_path = root / "docs" / "upstream-lock.md"
marker = "\n## Generated artifact lock\n"
lock_text = lock_path.read_text(encoding="utf-8").split(marker, 1)[0].rstrip()
lines = [lock_text, "", "## Generated artifact lock", "",
         f"SOURCE_DATE_EPOCH: `{payload['source_date_epoch']}`. Generated by the build; "
         "byte equality is checked separately by verify-manifest.sh.", ""]
lines.extend(f"- `{entry['path']}`: `{entry['sha256']}`" for entry in sorted(entries, key=lambda e: e["path"]))
lines.extend(["", "### Resolved packages", ""])
lines.extend(f"- {name}: `{version}`" for name, version in sorted(packages.items()))
lock_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
