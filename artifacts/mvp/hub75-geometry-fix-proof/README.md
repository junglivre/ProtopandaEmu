# HUB75 decoder: sentinel row-address doubled decoded height

## Bug

`hub75_decode_and_emit()` in `backend/qemu/new-files/hw/display/esp32s3_toy_hub75.c`
computed `rows_per_frame` as `max(row_addr) + 1` across every descriptor bucket
observed in one DMA ring lap. The real pinned ESP32-HUB75-MatrixPanel-I2S-DMA
library's descriptor ring carries one fixed extra descriptor every single lap
whose embedded row address reads as `HUB75_ADDR_MASK` (31, the all-ones 5-bit
value) — unrelated to real scan content. Confirmed via a temporary diagnostic
trace (`[protopandaemu.hub75.diag]`, since removed) against the user's real
`~/ThunarSDCard` boot: **every lap, across 16 consecutive frames**, showed
`row_addr` 0..15 densely populated with real per-row descriptor data (7
descriptors each), plus exactly one extra bucket at `row_addr=31`, every time.

Trusting `max(row_addr)+1` let that one sentinel double the computed scan
depth (16 → 32 for this 1/16-scan 64x32 panel), which doubled the decoded
frame height (32 → 64) and duplicated/mirrored every real row into a spurious
second copy — the quadrant-duplication artifact the user reported (uno-reverse
icon rendering as 4 mirrored copies instead of 1).

## Fix

Detect the sentinel structurally instead of by content heuristics: it is the
one bucket sitting at the maximum representable address (`HUB75_ADDR_MASK`),
outside the dense `0..N-1` run every real panel config fills contiguously
from zero. Exclude it from both the `rows_per_frame` computation and the
pixel-painting loop (bounds-checked: `row_addr >= rows_per_frame` is always
skipped, so a stray high address can never write past the allocated frame
buffer either).

## Evidence

`frame-seq11-after-fix.png`: `FRAME_DUMP seq=11` from a fresh real boot
session after the fix, decoded straight from the `[protopandaemu.hub75]`
trace. `width=128 height=32 rows_per_frame=16` (previously `height=64
rows_per_frame=32` for the same panel). Visually shows the real mirrored
text content once each, correctly positioned top-to-bottom, no duplicate
quadrants, no cut lines.

Before the fix (see `artifacts/mvp/hub75-real-content-proof/`, now
superseded for dense/asymmetric content): the same panel's real content
decoded as 4 mirrored copies because of the doubled height.
