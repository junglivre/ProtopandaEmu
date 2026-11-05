# Changelog

## Unreleased

- Pin Proto-panda v3.4.0, PlatformIO 6.2.0 and resolved build dependencies.
- Build original release/debug firmware and 16 MiB merged flash images from actual PlatformIO upload/partition metadata.
- Fix byte reproducibility with `SOURCE_DATE_EPOCH`; compare ELF, BIN, partitions and merged images between normal and clean builds.
- Preserve real boot evidence for esp-emu 0.51.0 and Espressif QEMU 9.2.2. Keep SDMMC and ADC2/MMIO blockers visible.
- Add evidence-integrity regression tests, raw stdout/stderr capture and fail-closed compatibility checks. An incomplete G0 returns exit code 3, not success.
- Add an experimental, source-level QEMU SENS/ADC device (`backend/qemu/`) that unblocks the ADC-calibration wait on the same unmodified firmware (`docs/qemu-adc-spike.md`). Stock backend pins are unchanged; G0 stays `INCOMPLETE`.
- Retract the timer-interrupt diagnosis with evidence (`-d int` trace shows ticks firing; the stall was TCG slowness plus the firmware's UART-silent edit-mode phase).
- Prove the QEMU path reaches storage: a virtual SD card (`scripts/make-sd-image.sh`, `-drive if=sd`) mounts under the unmodified firmware — first backend to pass SDMMC.
- Fix FFat mount (`FR 13`) in the experimental QEMU tree: root cause was SPI1 dummy-phase scaling for quad-I/O reads in `esp32s3_spi.c` (`(cycles+7)/8` bytes instead of line-width-aware conversion), which shifted every `esp_flash_read` payload +2 bytes after QIO mode init — truncating the FAT `55 AA` signature. Fixed by `backend/qemu/patches/0003-esp32s3-spi-dummy-io-scaling.patch`; evidence chain in `docs/qemu-spi-dummy-fix.md`. The Cycle 1 attribution of this blocker to the memory-mapped flash cache window was wrong and is retracted. Post-fix, unmodified firmware mounts FFat, rebuilds `frames.bulk` from SD PNG assets, writes `cache.json` and reaches edit mode (WiFi off, `-nic none`).
- No production emulator, GUI, guest host-directory bridge or faithful HUB75/SSD1306 implementation is released.
- Add Rock 1/M2's importable `protopandaemu.supervisor`, thin headless CLI,
  committed machine-profile schema, real UART/resource reports, clean-stop
  flash persistence, structured pre-spawn profile rejection, and bounded
  timeout cleanup around the additive experimental QEMU fork.
- Add `scripts/prove-m2.py`, which retains fresh acceptance evidence for FFat
  rebuild/store, clean-stop/restart cache reuse, incompatible-profile rejection,
  timeout cleanup, and interrupt cleanup using the unmodified pinned Proto-panda
  v3.4.0 image.
- Harden M2 boundaries: schema-first typed rejection, pre-spawn flash hashing and
  firmware-alias protection, serialized graceful stop, signal cleanup, finite
  timeout validation, and persisted SD cache evidence across clean restarts.
- Add Rock 2/M3's production host-folder bridge (`protopandaemu.hostdir`):
  no-follow directory-handle staging with source-tree-change detection,
  firmware JSON/PNG input validation scoped to the actual config files the
  firmware's `configloader.lua`/`models.lua` require, a durable fsync-ordered
  commit journal with idempotent crash recovery, mandatory backups, and
  unique per-session conflict siblings that never overwrite a concurrent
  host edit or an earlier conflict. Add `protopandaemu run --hostdir` wiring
  and a `protopandaemu fsck-hostdir` CLI command for out-of-band recovery.
- Add `scripts/prove-m3.py`, exercising two real firmware boots through a
  real folder (including the guest's own Lua `io.open` read/write reaching
  the host and surviving a restart), the full §5 error matrix, repeated
  conflicting host/guest edits, and fault injection at every durable commit
  boundary with recovery idempotency checks.
