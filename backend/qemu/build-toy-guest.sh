#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
OUT_DIR=${1:-$ROOT_DIR/artifacts/qemu-hub75-toy}
GCC=${XTENSA_ESP32S3_GCC:-}

if [[ -z "$GCC" ]] && command -v xtensa-esp32s3-elf-gcc >/dev/null 2>&1; then
  GCC=$(command -v xtensa-esp32s3-elf-gcc)
fi
if [[ -z "$GCC" && -x /home/jung/.platformio/packages/toolchain-xtensa-esp-elf/bin/xtensa-esp32s3-elf-gcc ]]; then
  GCC=/home/jung/.platformio/packages/toolchain-xtensa-esp-elf/bin/xtensa-esp32s3-elf-gcc
fi
if [[ -z "$GCC" || ! -x "$GCC" ]]; then
  echo "ERROR: xtensa-esp32s3-elf-gcc is missing; set XTENSA_ESP32S3_GCC" >&2
  exit 2
fi

mkdir -p "$OUT_DIR"
OBJ="$OUT_DIR/toy_guest.o"
ELF="$OUT_DIR/toy_guest.elf"

echo "+ $GCC -c toy_guest.S"
"$GCC" -c -x assembler-with-cpp -mlongcalls -o "$OBJ" \
  "$ROOT_DIR/backend/qemu/toy-guest/toy_guest.S"
echo "+ $GCC -nostdlib -Wl,-T,linker.ld"
"$GCC" -nostdlib -Wl,-T,"$ROOT_DIR/backend/qemu/toy-guest/linker.ld" \
  -Wl,--gc-sections -Wl,--build-id=none -o "$ELF" "$OBJ"
"${GCC%gcc}readelf" -h "$ELF" | sed -n '1,18p'
sha256sum "$ELF" | tee "$OUT_DIR/toy_guest.elf.sha256"
echo "Built: $ELF"
