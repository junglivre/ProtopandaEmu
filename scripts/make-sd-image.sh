#!/usr/bin/env bash
set -euo pipefail

# Builds a FAT32 SD card image with the pinned Proto-panda v3.4.0 SD payload
# (init.lua, hardware.json, lualib/) for use with the QEMU ESP32-S3 machine's
# DWC SDMMC controller (-drive if=sd). Experimental spike tooling; the stock
# spike harness does not attach any SD card.
#
# Requires: mtools (mformat, mcopy). Payload source: pinned upstream checkout
# at .work/upstream-v3.4.0 (created by scripts/build-firmware.sh).

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UPSTREAM="$REPO_ROOT/.work/upstream-v3.4.0"
OUT="${1:-$REPO_ROOT/.rf/sd-card.img}"

if [[ ! -f "$UPSTREAM/init.lua" || ! -f "$UPSTREAM/hardware.json" || ! -d "$UPSTREAM/lualib" ]]; then
    echo "error: pinned upstream payload not found at $UPSTREAM" >&2
    echo "run scripts/build-firmware.sh first (it fetches the pinned upstream)" >&2
    exit 1
fi

command -v mformat >/dev/null || { echo "error: mtools not installed" >&2; exit 1; }

dd if=/dev/zero of="$OUT" bs=1M count=64 status=none
mformat -i "$OUT" -F ::
mcopy -i "$OUT" -s "$UPSTREAM/init.lua" "$UPSTREAM/hardware.json" ::
mcopy -i "$OUT" -s "$UPSTREAM/lualib" ::

sha256sum "$OUT" | tee "$OUT.sha256"
echo "SD image written: $OUT"
