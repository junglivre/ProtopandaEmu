#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/build-firmware-emu.sh [--clean]

Builds the ProtopandaEmu compat firmware ("emu" variant) from the pinned
Proto-panda v3.4.0 source plus the guarded emulator patches in
backend/firmware-emu/patches/, and writes the merged flash image to
artifacts/build-emu/release/merged_flash.bin.

The emu variant is opt-in (PROTOPANDAEMU_EMU=1 build flag). Changes, all
guarded by that flag in source:
  - IR interrupt pin is configured without pinMode(), so a keybinds
    infrared_gpio that shares an SDMMC GPIO (v3.4.0 ships infrared_gpio=21 ==
    MMC_PIN_CLK) no longer unmounts storage mid-boot.
  - The BLE manager is registered without a radio so core drivers whose
    onEnable() requires BLE work under INFRARED input.
  - "IR:<addr>:<cmd>" lines on the console inject virtual NEC commands into
    the real IR queue (virtual controller channel).
The pinned build-normal/build-clean artifacts are untouched.
EOF
}

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# The emu variant builds the user's own firmware fork (the one the real
# device runs; it understands the expressions/ collections layout on the SD
# card), never the pinned v3.4.0 tree.
SOURCE_PROJECT=${PROTO_PANDA_FIRMWARE_SRC:-/home/jung/Projects/Proto/Proto-panda}
EMU_DIR=$ROOT_DIR/.work/firmware-emu
export PLATFORMIO_CORE_DIR=${PLATFORMIO_CORE_DIR:-$ROOT_DIR/.work/platformio-core}
PATCH_DIR=$ROOT_DIR/backend/firmware-emu/patches
OUT_DIR=$ROOT_DIR/artifacts/build-emu

CLEAN=0
while (($#)); do
  case "$1" in
    --clean) CLEAN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

command -v pio >/dev/null 2>&1 || { echo "ERROR: PlatformIO CLI 'pio' not found" >&2; exit 127; }
PIO_BIN=${PIO_BIN:-pio}
"$PIO_BIN" --version >/dev/null || { echo "ERROR: '$PIO_BIN' is not usable" >&2; exit 127; }

mkdir -p "$ROOT_DIR/.work" "$OUT_DIR/release" "$ROOT_DIR/artifacts/logs"
exec > >(tee -a "$ROOT_DIR/artifacts/logs/build-emu.log") 2>&1

if [[ ! -d "$SOURCE_PROJECT/src" || ! -f "$SOURCE_PROJECT/platformio.ini" ]]; then
  echo "ERROR: firmware source not found: $SOURCE_PROJECT" >&2
  exit 2
fi

echo "+ fresh copy $SOURCE_PROJECT -> $EMU_DIR"
rm -rf "$EMU_DIR"
mkdir -p "$EMU_DIR"
cp -a "$SOURCE_PROJECT"/. "$EMU_DIR"/
# Keep the user's live project untouched; the emu copy gets the patches.
rm -rf "$EMU_DIR/.pio"

echo "+ applying emulator patches"
for patch_file in "$PATCH_DIR"/*.patch; do
  [[ -e "$patch_file" ]] || { echo "ERROR: no patches in $PATCH_DIR" >&2; exit 1; }
  if patch -d "$EMU_DIR" -p1 --forward --silent < "$patch_file"; then
    echo "  applied $(basename "$patch_file")"
  elif patch -d "$EMU_DIR" -p1 --dry-run --silent < "$patch_file" 2>/dev/null; then
    echo "ERROR: failed to apply $(basename "$patch_file")" >&2
    exit 2
  else
    echo "  already applied: $(basename "$patch_file")"
  fi
done
grep -q "PROTOPANDAEMU_EMU=1" "$EMU_DIR/platformio.ini" || {
  echo "ERROR: patches did not enable PROTOPANDAEMU_EMU" >&2
  exit 2
}

if [[ "$CLEAN" -eq 1 ]]; then
  echo "+ rm -rf $EMU_DIR/.pio/build"
  rm -rf "$EMU_DIR/.pio/build"
fi

export SOURCE_DATE_EPOCH
SOURCE_DATE_EPOCH=$(date -r "$EMU_DIR/platformio.ini" +%s)
export TZ=UTC

(cd "$EMU_DIR" && "$PIO_BIN" pkg install -e release --no-save \
  --library "adafruit/Adafruit BusIO@1.17.4" --tool "tasmota/tool-mklittlefs@3.2.0")

echo "Emu source: $SOURCE_PROJECT + emulator patches"
echo "+ pio run -e release"
(cd "$EMU_DIR" && "$PIO_BIN" run -e release)

build_dir="$EMU_DIR/.pio/build/release"
copy_required() {
  local source=$1
  local name=$2
  [[ -f "$source" ]] || { echo "ERROR: missing artifact: $source" >&2; exit 1; }
  cp -f "$source" "$OUT_DIR/release/$name"
}

echo "+ "$PIO_BIN" run -e release -t envdump > .work/envdump-emu.txt"
(cd "$EMU_DIR" && "$PIO_BIN" run -e release -t envdump > "$ROOT_DIR/.work/envdump-emu.txt")
[[ -s "$ROOT_DIR/.work/envdump-emu.txt" ]] || { echo "ERROR: empty envdump" >&2; exit 1; }
uploader=$(sed -n "s/^  'UPLOADER': '\(.*\)',/\1/p" "$ROOT_DIR/.work/envdump-emu.txt")
[[ -f "$uploader" ]] || { echo "ERROR: SCons UPLOADER missing" >&2; exit 1; }

merged_tmp=$ROOT_DIR/.work/emu-merged.bin
mapfile -t args < <(python3 - "$ROOT_DIR/.work/envdump-emu.txt" "$EMU_DIR" "$build_dir" "$merged_tmp" "$OUT_DIR/release" <<'PY'
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
        raise SystemExit(f"ERROR: {name} missing from envdump")
    value = match.group(1).strip()
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return value.strip("'\"")

def list_value(name):
    marker = re.search(rf"['\"]{re.escape(name)}['\"]\s*:\s*\[", envdump)
    if not marker:
        raise SystemExit(f"ERROR: {name} missing from envdump")
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
            continue
        if char in "'\"":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return ast.literal_eval(envdump[start:index + 1])
    raise SystemExit(f"ERROR: unterminated {name} in envdump")

partition_csv = pathlib.Path(str(scalar("PARTITIONS_TABLE_CSV")))
app_offset = None
with partition_csv.open(newline="", encoding="utf-8") as handle:
    for row in csv.reader(handle):
        fields = [field.strip() for field in row]
        if not fields or fields[0].startswith("#") or len(fields) < 5:
            continue
        if fields[0] == "app0" and fields[1] == "app":
            app_offset = int(fields[3], 0)
            break
if app_offset != 0x10000:
    raise SystemExit(f"ERROR: unexpected app0 offset {app_offset!r}")

images = []
for item in list_value("FLASH_EXTRA_IMAGES"):
    if not isinstance(item, (list, tuple)) or len(item) != 2:
        continue
    address, source = str(item[0]), pathlib.Path(str(item[1]))
    if not source.is_absolute():
        source = build_dir / source
    images.append((address, source))
if [address for address, _ in images] != ["0x0000", "0x8000", "0xe000"]:
    raise SystemExit(f"ERROR: unexpected flash image offsets: {images}")

firmware = build_dir / "firmware.bin"
if not firmware.is_file():
    raise SystemExit("ERROR: firmware.bin missing")

flash_clock = str(scalar("BOARD_F_FLASH")).rstrip("L")
flash_freq = f"{int(flash_clock) // 1000000}m"
args = ["--chip", "esp32s3", "merge_bin", "--fill-flash-size", "16MB", "-o", merged,
        "--flash_mode", str(scalar("BOARD_FLASH_MODE")), "--flash_freq", flash_freq,
        "--flash_size", "16MB"]
for address, source in images:
    if not source.is_file():
        raise SystemExit(f"ERROR: upload image missing: {source}")
    args.extend((address, str(source)))
args.extend((hex(app_offset), str(firmware)))
for item in args:
    print(item)
PY
)
((${#args[@]} > 0)) || { echo "ERROR: no merge arguments derived" >&2; exit 1; }
echo "+ python3 $uploader ${args[*]}"
python3 "$uploader" "${args[@]}"

copy_required "$merged_tmp" merged_flash.bin
copy_required "$build_dir/firmware.bin" firmware.bin
copy_required "$build_dir/firmware.elf" firmware.elf
rm -f "$merged_tmp"

sha256sum "$OUT_DIR/release/merged_flash.bin" | tee "$OUT_DIR/release/merged_flash.bin.sha256"
echo "EMU firmware written: $OUT_DIR/release/merged_flash.bin"
echo "Enable it with: export PROTOPANDAEMU_EXTRA_FIRMWARE_SHA256=\$(cut -d' ' -f1 $OUT_DIR/release/merged_flash.bin.sha256) PROTOPANDAEMU_EMU_FIRMWARE=$OUT_DIR/release/merged_flash.bin"
if [[ "${PLATFORMIO_BUILD_FLAGS:-}" == *PROTOPANDAEMU_EMU_LED_DUMP* ]]; then
  echo "LED strip preview: ENABLED (PROTOPANDAEMU_EMU_LED_DUMP set for this build)"
else
  echo "LED strip preview: disabled this build (opt-in, not persisted across"
  echo "  rebuilds) -- set PLATFORMIO_BUILD_FLAGS=\"-DPROTOPANDAEMU_EMU_LED_DUMP\""
  echo "  before running this script again to turn it back on"
fi
