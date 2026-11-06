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
- Fix a real QEMU model bug: the toy SSD1306 realization wired I2C0's
  interrupt to the wrong intmatrix source (`ETS_I2C_EXT1_INTR_SOURCE`
  instead of `ETS_I2C_EXT0_INTR_SOURCE`), which silently stalled every
  successful I2C address ACK on its own driver timeout while a NACK
  still resolved instantly -- the real cause of the unmodified
  firmware's "no device found" result and the later `display.begin()`
  hang. The firmware's own I2C scan now reports "I2C device found at
  address 60" immediately.
- Implement the real SSD1306 protocol in the experimental QEMU fork:
  command/data byte-stream parsing (including multi-byte commands the
  real driver can split across several I2C transfers), real
  `COLUMNADDR`/`PAGEADDR` horizontal-addressing windows and wraparound,
  and a real 128x64 GDDRAM buffer. Add `scripts/prove-m4-oled.py`,
  which boots the unmodified firmware with its real display
  configuration and checks the captured splash-icon frame byte-for-byte
  against an oracle independently re-derived from the firmware's own
  compiled-in icon bitmap (not the renderer under test).
- Fix two real toy HUB75 bugs: it only decoded once instead of
  continuously streaming like the real continuous DMA refresh, and it
  assumed one descriptor per bit-plane when the real pinned library
  packs a whole row's planes into the first descriptor. Decoded
  geometry is now derived from, and matches, real GDMA traffic
  (`docs/qemu-hub75-oled-spike.md`); capturing a colored/animated
  frame is still blocked by an unrelated Lua VFS gap
  (`models.json`/`io.open`, tracked in `ISSUES.md`).
- Capture the continuous-streaming GDMA descriptor-walk helper
  (`esp_gdma_walk_channel_until_eof`) as a durable patch
  (`backend/qemu/patches/0007-gdma-walk-channel-until-eof.patch`); it had
  only existed as a hand-edit in the unpacked working tree and a clean
  rebuild silently lost it.
- Add the M5 minimum desktop launcher (`protopandaemu/launcher.py`):
  independent left/right HUB75 wiring controls writing the real upstream
  `flip_left`/`flip_right`/`color_scheme_left`/`color_scheme_right` fields,
  a folder picker wired to the hostdir bridge, start/stop via the shared
  supervisor API, and a live preview/UART/resource view.
- Fix two real bugs found testing the launcher against a real user SD-card
  config: the live OLED/HUB75 preview re-read and re-regexed the entire
  growing session stderr log on every refresh tick (visible stutter on a
  long-running session); and writing wiring fields reformatted the user's
  whole `animation.json` (`sort_keys=True` reordering every object,
  turning a 4-field addition into a 1,316-line diff on a real hand-authored
  file). Both fixed and regression-tested (`tests/test_launcher.py`).
- Document that BLE input mode is not supported by this experimental QEMU
  backend (no BLE controller model; a real firmware config with
  `input.mode: "BLE"` boots into an infinite crash/reboot loop) and warn
  for it in the launcher before starting a session.
- Fix two real bugs in the experimental QEMU DWC SDMMC model: it returned
  zero for BMOD/CLOCK/STATUS/CDETECT/WRTPRT with no storage attached, and
  a guest register read-modify-write on BMOD could silently clear the
  IDMAC-enable bit mid-read, disabling the DMA descriptor engine. Also
  gate the descriptor walk on `BMOD.ENABLE` and `CMD.data_expected` so a
  stale `BYTCNT` from a prior command can't start a spurious walk
  (`backend/qemu/patches/0008-esp32s3-dwc-sdmmc-register-state.patch`).
  This resolves the long-standing M4 VFS/FatFs race (`ISSUES.md`); the
  user's real, unmodified SD card now boots to `completed setup` with
  zero FatFs errors and renders real animated HUB75 content.
- Add a minimal ESP32-S3 RMT TX device model
  (`backend/qemu/patches/0009-esp32s3-rmt-tx.patch`) so FastLED's
  `leds.begin()` RMT wait-loop no longer stalls boot indefinitely.
- Build an emu-specific firmware variant from the user's own
  `Proto-panda` `alt` fork, gated entirely behind `-DPROTOPANDAEMU_EMU=1`
  (`backend/firmware-emu/patches/`, `scripts/build-firmware-emu.sh`): the
  shared IR/SDMMC pin uses raw `gpio_config()` instead of `pinMode()` to
  avoid detaching the SD bus; `BleManager::begin()` runs with no radio so
  BLE-gated driver `onEnable()` calls pass under infrared input mode; a
  console command (`IR:<addr>:<cmd>\n` over UART) injects virtual IR
  commands; LED strip output is stubbed (HUB75 is unaffected). No
  firmware config, pin assignment, or SD data is modified to make this
  work.
- Add launcher virtual-controller buttons wired to the real IR codes from
  the user's `keybinds.json`, sending injected IR commands over the
  session's UART.
- Fix the launcher's live preview/log freeze: `refresh()`'s `after()`
  reschedule call was the LAST statement in the method, so any exception
  in the body (e.g. a dump hex payload truncated mid-write by the bounded
  tail read) killed the polling loop permanently — even starting a new
  session afterward left the GUI dead while the backend kept running.
  `refresh()` now reschedules itself first and the dump decoder returns
  `None` instead of raising on a truncated/mismatched-length payload,
  picking the newest fully-decodable dump instead
  (`tests/test_launcher.py::RefreshRescheduleTests`,
  `::ReadLatestDumpsSkipsTruncatedTailMatchTests`).
- Raise the launcher's default `--timeout` from 30s to 600s. A real
  session's first boot composes the firmware's `frames.bulk` on FFat
  (~100s wall-clock for ~800 frames); 30s silently killed the session
  mid-compose, which looked identical to a GUI freeze and left a partial
  bulk file for the next boot to choke on.
- Clean up GUI text: drop "M5"/AI-slop phrasing from labels and warnings;
  "Proto-panda" is reserved for the upstream repo URL, the product name
  in the UI is "Protopanda".
