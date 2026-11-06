#!/usr/bin/env bash
set -euo pipefail

# Builds the pinned Espressif QEMU fork with ONE additive research device
# (a minimal ESP32-S3 SENS one-shot ADC model) applied as a patch on top of
# the verified, unmodified upstream source. This is experimental backend
# work for the ProtopandaEmu M1 spike (see docs/qemu-adc-spike.md); it does
# NOT replace the stock .rf/backends/qemu pinned in scripts/spike.py, and it
# NEVER touches the Rock 1 Proto-panda firmware artifacts.
#
# Usage: backend/qemu/build-experimental-qemu.sh [--out DIR]

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
SOURCE_TAR="$ROOT_DIR/.work/qemu-build-inputs/qemu-source.tar.xz"
SOURCE_TAR_URL="https://github.com/espressif/qemu/releases/download/esp-develop-9.2.2-20260417/qemu-esp_develop_9.2.2_20260417-src.tar.xz"
SOURCE_SHA256="66015182274b9fda7d3c7f19e8f160e6e23947217cc149c7aa97e1586dfc6198"
EXTRACT_DIR="$ROOT_DIR/.work/qemu-source-unpacked"
SOURCE_DIR="$EXTRACT_DIR/qemu-esp_develop_9.2.2_20260417-src"
DEPS_DIR="$ROOT_DIR/.work/qemu-deps"
OUT_DIR="$ROOT_DIR/artifacts/qemu-adc"

while (($#)); do
  case "$1" in
    --out) OUT_DIR=$2; shift 2 ;;
    -h|--help) echo "Usage: $0 [--out DIR]"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
OUT_DIR=$(realpath -m "$OUT_DIR")
mkdir -p "$OUT_DIR"

if [[ ! -f "$SOURCE_TAR" ]]; then
  echo "+ curl -L -o $SOURCE_TAR $SOURCE_TAR_URL"
  mkdir -p "$(dirname "$SOURCE_TAR")"
  curl --fail --location --connect-timeout 15 --max-time 240 -o "$SOURCE_TAR" "$SOURCE_TAR_URL"
fi
ACTUAL_SHA256=$(sha256sum "$SOURCE_TAR" | cut -d' ' -f1)
[[ "$ACTUAL_SHA256" = "$SOURCE_SHA256" ]] || {
  echo "ERROR: source tarball hash mismatch: $ACTUAL_SHA256 != $SOURCE_SHA256" >&2
  exit 2
}

if [[ ! -d "$SOURCE_DIR" ]]; then
  mkdir -p "$EXTRACT_DIR"
  tar -xJf "$SOURCE_TAR" -C "$EXTRACT_DIR"
fi

# Isolated build dependencies (no system install). Each .deb is fetched via
# `apt-get download` and extracted with dpkg-deb -x; see docs/qemu-adc-spike.md
# for the exact package/version list if $DEPS_DIR does not exist yet.
if [[ ! -d "$DEPS_DIR/usr/include" ]]; then
  echo "ERROR: isolated build dependencies are missing: $DEPS_DIR" >&2
  echo "See docs/qemu-adc-spike.md 'Build dependencies' for the exact .deb list." >&2
  exit 2
fi

APPLY_MARKER="$SOURCE_DIR/.protopandaemu-sens-patch-applied"
if [[ ! -f "$APPLY_MARKER" ]]; then
  if grep -q 'hw/misc/esp32s3_sens.h' "$SOURCE_DIR/hw/xtensa/esp32s3.c" 2>/dev/null; then
    echo "SENS patch effects already present; recording idempotent marker"
  else
    echo "+ applying SENS device patches"
    patch -p1 -d "$SOURCE_DIR" < "$ROOT_DIR/backend/qemu/patches/0001-esp32s3.c.patch"
    patch -p1 -d "$SOURCE_DIR" < "$ROOT_DIR/backend/qemu/patches/0002-hw-misc-meson.build.patch"
  fi
  mkdir -p "$SOURCE_DIR/hw/misc" "$SOURCE_DIR/include/hw/misc"
  cp "$ROOT_DIR/backend/qemu/new-files/hw/misc/esp32s3_sens.c" "$SOURCE_DIR/hw/misc/esp32s3_sens.c"
  cp "$ROOT_DIR/backend/qemu/new-files/include/hw/misc/esp32s3_sens.h" "$SOURCE_DIR/include/hw/misc/esp32s3_sens.h"
  touch "$APPLY_MARKER"
else
  echo "SENS patch already applied (idempotent): $APPLY_MARKER"
fi

APPLY_MARKER2="$SOURCE_DIR/.protopandaemu-spi-dummy-patch-applied"
if [[ ! -f "$APPLY_MARKER2" ]]; then
  echo "+ applying SPI dummy I/O scaling patch (FFat/WL read fix)"
  patch -p1 -d "$SOURCE_DIR" < "$ROOT_DIR/backend/qemu/patches/0003-esp32s3-spi-dummy-io-scaling.patch"
  touch "$APPLY_MARKER2"
else
  echo "SPI dummy patch already applied (idempotent): $APPLY_MARKER2"
fi

APPLY_MARKER3="$SOURCE_DIR/.protopandaemu-rock4-toy-devices-applied"
if [[ ! -f "$APPLY_MARKER3" ]]; then
  echo "+ applying Rock 4 SSD1306/LCD_CAM toy-device patches"
  patch -p1 -d "$SOURCE_DIR" < "$ROOT_DIR/backend/qemu/patches/0004-esp32s3-toy-hub75-oled.patch"
  mkdir -p "$SOURCE_DIR/hw/i2c" "$SOURCE_DIR/hw/display" \
           "$SOURCE_DIR/include/hw/i2c" "$SOURCE_DIR/include/hw/display"
  cp "$ROOT_DIR/backend/qemu/new-files/hw/i2c/esp32s3_toy_ssd1306.c" \
     "$SOURCE_DIR/hw/i2c/esp32s3_toy_ssd1306.c"
  cp "$ROOT_DIR/backend/qemu/new-files/include/hw/i2c/esp32s3_toy_ssd1306.h" \
     "$SOURCE_DIR/include/hw/i2c/esp32s3_toy_ssd1306.h"
  cp "$ROOT_DIR/backend/qemu/new-files/hw/display/esp32s3_toy_hub75.c" \
     "$SOURCE_DIR/hw/display/esp32s3_toy_hub75.c"
  cp "$ROOT_DIR/backend/qemu/new-files/include/hw/display/esp32s3_toy_hub75.h" \
     "$SOURCE_DIR/include/hw/display/esp32s3_toy_hub75.h"
  touch "$APPLY_MARKER3"
else
  echo "Rock 4 toy-device patch already applied (idempotent): $APPLY_MARKER3"
  if grep -q 'i2c_slave_create_simple(qdev_get_child_bus' "$SOURCE_DIR/hw/xtensa/esp32s3.c"; then
    sed -i 's/i2c_slave_create_simple(qdev_get_child_bus(DEVICE(\&ss->i2c0), "i2c")/i2c_slave_create_simple(I2C_BUS(qdev_get_child_bus(DEVICE(\&ss->i2c0), "i2c"))/g' \
      "$SOURCE_DIR/hw/xtensa/esp32s3.c"
  fi
  # Keep the source tree's additive new-files synchronized on incremental
  # rebuilds too; the numbered patch and marker remain unchanged.
  mkdir -p "$SOURCE_DIR/hw/i2c" "$SOURCE_DIR/hw/display" \
           "$SOURCE_DIR/include/hw/i2c" "$SOURCE_DIR/include/hw/display"
  cp "$ROOT_DIR/backend/qemu/new-files/hw/i2c/esp32s3_toy_ssd1306.c" \
     "$SOURCE_DIR/hw/i2c/esp32s3_toy_ssd1306.c"
  cp "$ROOT_DIR/backend/qemu/new-files/include/hw/i2c/esp32s3_toy_ssd1306.h" \
     "$SOURCE_DIR/include/hw/i2c/esp32s3_toy_ssd1306.h"
  cp "$ROOT_DIR/backend/qemu/new-files/hw/display/esp32s3_toy_hub75.c" \
     "$SOURCE_DIR/hw/display/esp32s3_toy_hub75.c"
  cp "$ROOT_DIR/backend/qemu/new-files/include/hw/display/esp32s3_toy_hub75.h" \
     "$SOURCE_DIR/include/hw/display/esp32s3_toy_hub75.h"
fi

APPLY_MARKER4="$SOURCE_DIR/.protopandaemu-rock4-gpio-input-applied"
if [[ ! -f "$APPLY_MARKER4" ]]; then
  echo "+ applying Rock 4 GPIO input default for the active-low edit strap"
  patch -p1 -d "$SOURCE_DIR" < "$ROOT_DIR/backend/qemu/patches/0005-esp32-gpio-input-default-high.patch"
  touch "$APPLY_MARKER4"
else
  echo "Rock 4 GPIO input default already applied (idempotent): $APPLY_MARKER4"
fi

APPLY_MARKER5="$SOURCE_DIR/.protopandaemu-rock4-gdma-i2c-s3-applied"
if [[ ! -f "$APPLY_MARKER5" ]]; then
  echo "+ applying Rock 4 real GDMA/LCD_CAM integration and ESP32-S3 I2C opcode patch"
  patch -p1 -d "$SOURCE_DIR" < "$ROOT_DIR/backend/qemu/patches/0006-rock4-real-gdma-i2c-s3.patch"
  touch "$APPLY_MARKER5"
else
  echo "Rock 4 real GDMA/I2C patch already applied (idempotent): $APPLY_MARKER5"
fi

APPLY_MARKER6="$SOURCE_DIR/.protopandaemu-gdma-walk-channel-applied"
if [[ ! -f "$APPLY_MARKER6" ]]; then
  echo "+ applying continuous-streaming GDMA descriptor-walk helper (M4 HUB75)"
  patch -p1 -d "$SOURCE_DIR" < "$ROOT_DIR/backend/qemu/patches/0007-gdma-walk-channel-until-eof.patch"
  touch "$APPLY_MARKER6"
else
  echo "GDMA walk-channel helper already applied (idempotent): $APPLY_MARKER6"
fi

APPLY_MARKER7="$SOURCE_DIR/.protopandaemu-sdmmc-register-state-applied"
if [[ ! -f "$APPLY_MARKER7" ]]; then
  echo "+ applying ESP32-S3 DWC SDMMC register-state patch"
  patch -p1 -d "$SOURCE_DIR" < "$ROOT_DIR/backend/qemu/patches/0008-esp32s3-dwc-sdmmc-register-state.patch"
  touch "$APPLY_MARKER7"
else
  echo "SDMMC register-state patch already applied (idempotent): $APPLY_MARKER7"
fi

APPLY_MARKER8="$SOURCE_DIR/.protopandaemu-rmt-tx-applied"
if [[ ! -f "$APPLY_MARKER8" ]]; then
  echo "+ applying ESP32-S3 RMT minimal TX model patch"
  patch -p1 -d "$SOURCE_DIR" < "$ROOT_DIR/backend/qemu/patches/0009-esp32s3-rmt-tx.patch"
  touch "$APPLY_MARKER8"
else
  echo "RMT TX model patch already applied (idempotent): $APPLY_MARKER8"
fi

cd "$SOURCE_DIR"
if [[ ! -f build/build.ninja ]]; then
  echo "+ configure"
  PKG_CONFIG_PATH="$DEPS_DIR/usr/lib/x86_64-linux-gnu/pkgconfig" \
  ./configure --target-list=xtensa-softmmu --enable-gcrypt --enable-plugins --enable-slirp \
    --disable-docs --disable-gtk --disable-sdl --disable-vnc --disable-werror \
    --extra-cflags="-I$DEPS_DIR/usr/include -I$DEPS_DIR/usr/include/x86_64-linux-gnu -I$DEPS_DIR/usr/include/slirp" \
    --extra-ldflags="-L$DEPS_DIR/usr/lib/x86_64-linux-gnu"
fi
echo "+ ninja -C build qemu-system-xtensa"
ninja -C build qemu-system-xtensa

BINARY="$SOURCE_DIR/build/qemu-system-xtensa"
sha256sum "$BINARY" > "$OUT_DIR/experimental-binary.sha256"
echo "Built: $BINARY"
echo "Hash recorded: $OUT_DIR/experimental-binary.sha256"
