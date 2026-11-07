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
- Fix a HUB75 decoder bug that doubled decoded frame height: the ring's
  one fixed sentinel descriptor per lap (row address reads as the
  all-ones 5-bit `HUB75_ADDR_MASK`) was counted as real scan content,
  doubling the computed scan depth and rendering real content as
  duplicated/mirrored quadrants on dense, asymmetric frames. Now detected
  structurally (the lone bucket outside the dense `0..N-1` address run)
  and excluded from both geometry and painting. Evidence:
  `artifacts/mvp/hub75-geometry-fix-proof/`.
- Revamp the launcher GUI: full English translation (it was partly
  Portuguese); a dark theme applied uniformly via `ttk.Style` (frames,
  labels, buttons, combobox, including the combobox dropdown listbox,
  which isn't themeable through `ttk.Style` alone and needs the Tk
  option database); a device-accurate three-panel preview layout (one
  combined HUB75 FRAME_DUMP decodes into left/right eye-panel halves per
  `hardware.json`'s `horizontal_panel_count=2`, each on its own canvas,
  OLED's status display centered above between them), every panel sized
  at its exact native 2:1 aspect so the canvas fills its frame with no
  stretch-induced letterboxing; window resizing locked to the launcher's
  own aspect ratio (`wm_aspect`) so dragging a corner scales every panel
  proportionally instead of distorting one axis; a firmware picker next
  to the SD-card picker (explicit choice beats
  `PROTOPANDAEMU_EMU_FIRMWARE`/auto-detection); simulated controls
  restyled into an actual D-pad cross with a boop toggle (purple when
  active) next to Back/Aux, reading the configured pin from the loaded
  SD card's `misc.json`; start/stop session buttons laid out side by
  side and colored (green/grey/red) so the current state is obvious at a
  glance; the preview poll interval cut from 100ms to 50ms. Regression
  tests: `tests/test_launcher.py::DrawHub75SplitsLeftRightHalvesTests`,
  `ExplicitFirmwarePickOverridesAutoDetectionTests`, `BoopConfigLoadTests`,
  `ToggleBoopSendsGpioOverrideTests`.
- Fix a real emulator performance bug that looked like a GUI freeze: the
  ESP32 I2C model logged an unconditional `error_report()` on every
  tight-loop poll of an empty FIFO register (the ESP-IDF I2C driver's own
  transfer-wait pattern), and the launcher's QEMU invocation already
  passes `-d guest_errors`. One real boot session produced over 2.5
  million blocking stderr writes in 15 seconds, which throttled the TCG
  main loop badly enough that guest progress (UART output, HUB75/OLED
  content) nearly stalled, even though nothing was logically stuck. Now
  logged once per process lifetime instead
  (`backend/qemu/patches/0010-esp32-i2c-quiet-fifo-empty-poll.patch`).
- Cut the HUB75 backend's internal ring-sampling interval from 5ms to
  2ms (`HUB75_REFRESH_INTERVAL_NS`), so the guest can produce fresher
  frames for anything polling the FRAME_DUMP (the launcher preview,
  but also any other consumer of that channel).
- Emulate the proximity ("boop") sensor for SD cards configured with
  `misc.json`'s `boop.trigger_mode: "gpio"`: a new firmware-emu patch
  (`0012-emu-boop-gpio-override.patch`) rewires the Lua-visible
  `digitalRead` binding itself (not `boop.lua`, which this project's
  source doesn't control -- the firmware mounts the SD card as the Lua
  root, so `/lualib/boop.lua` always loads the SD card's own copy, never
  a patched one). A `GPIO:<pin>:<0|1>` line on the session console (the
  same channel the IR virtual controller already uses) overrides a pin's
  read for every caller transparently, real hardware or not, until
  cleared. Lidar-mode SD cards have no digital pin to override. Verified
  against a real boot on the user's own SD card: the console echoed both
  a near (1) and a far (0) override taking effect for the configured pin.
- Default the boop sensor to "far" as soon as a session's guest loop is
  confirmed running, instead of leaving it to whatever the real
  (unconnected) emulated pin happens to read. Without this, boop read as
  permanently triggered from boot for this user's wiring, unaffected by
  the launcher toggle until the first click. Sending the override right
  when the session starts was tried and discarded: the console parser
  only reads Serial once `loop()` is running, and bytes arriving during
  the ROM bootloader/`setup()` window were silently lost. `refresh()`
  now sends it once, gated on the "vfs_init" boot marker (a generic,
  SD-card-content-independent signal the guest loop is actually
  consuming console input). Regression tests:
  `tests/test_launcher.py::DeferredBoopDefaultOverrideTests`.
- Add a live LED strip preview to the launcher for SD cards that drive
  WS2812 output. Firmware-emu patch 0013 dumps `LedStrip::Display()`'s
  real computed color buffer -- exactly what the real strip would show
  -- as a throttled, change-gated `[EMU] LED_DUMP ...` console line,
  since QEMU's own RMT model (patch `0009-esp32s3-rmt-tx.patch`,
  backend/qemu) intentionally doesn't decode WS2812 signal timing (out
  of scope there), so there is no real wire-level traffic to capture the
  way HUB75/OLED's dumps do; this is a firmware-buffer capture instead,
  noted as such in the GUI. Off by default (gated behind
  `PROTOPANDAEMU_EMU_LED_DUMP`, not set by `build-firmware-emu.sh`):
  even throttled, a long animated session adds up to meaningful UART
  log volume, so the preview is opt-in rather than always-on. Regression
  tests: `tests/test_launcher.py::ReadLatestLedDumpTests`,
  `DrawLedStripRendersEachColorTests`. Verified against a real boot on
  the user's own SD card: a rotating two-segment behavior streamed live,
  distinct colors per dump, matching the boot log's own LED setup.
- Document the real status of microphone/FFT input: it never produces a
  usable sample in this backend. `fft.cpp`'s `readAndAnalyse()` silently
  no-ops every time `adc_continuous_read()` returns `ESP_ERR_TIMEOUT`,
  with no error logged, and nothing in this backend's ADC model can
  ever satisfy that read -- the existing SENS device
  (`backend/qemu/new-files/hw/misc/esp32s3_sens.c`, added for a
  different firmware subsystem's one-shot calibration poll, see
  `docs/qemu-adc-spike.md`) only covers single-shot conversions, not the
  continuous/DMA-mode ADC the microphone path requires. No code change:
  firmware with `misc.json`'s `fft.enabled: true` boots and runs
  normally (confirmed against a real user SD card with FFT enabled, no
  hang or crash across multiple full sessions this round), it just never
  sees real audio. README/README.pt-BR.md updated from "unverified" to
  this definitive finding.
- Fix virtual D-pad/Back/Aux buttons only ever registering as a 250ms tap
  regardless of how long the mouse stayed down. A single injected IR
  command refreshes the firmware's own `button_timeout` to
  `now+250ms` (`input.lua`'s `press()`) and auto-releases once that
  elapses, exactly matching a real remote's single pulse; holding the
  mouse down never re-injected, so a held press always looked identical
  to a tap from the guest's point of view. Buttons now bind
  press/release and re-inject every 150ms while held, well inside the
  firmware's own window, the same way a real remote's repeat pulses
  would. Regression tests: `tests/test_launcher.py::ButtonHoldRepeatTests`.
- Measured, not fixed: the HUB75 preview "looking dark" is the real
  content, not a brightness bug. `setBrightness8()` (the library backing
  this firmware's configurable panel brightness) dims via the OE/blanking
  pin's duty cycle, a separate signal this backend's HUB75 decoder never
  reads -- it only decodes the color bit-plane DMA data, so the preview
  already always reconstructs full, undimmed per-pixel color regardless
  of the real panel's configured brightness. A real captured frame from
  the user's own SD card measured a full 0-255 dynamic range (max byte
  255, mean of nonzero bytes 170); the dark appearance is ~91% of the
  frame being zero (black background behind a small mascot icon), which
  is the actual content, not under-rendered brightness.
- Scale the HUB75 preview to the real configured panel brightness.
  Following up on the "measured, not fixed" entry above: the decoded
  color data is always full-brightness (this backend's decoder can't see
  the real OE/blanking dimming signal), but the firmware DOES pass
  through one C++ choke point for every brightness change, direct or the
  gentle per-frame ramp (`Devices::SetMaxBrightness`,
  `src/tools/devices.cpp`) -- firmware-emu patch 0014 dumps that value,
  throttled and change-gated the same way the LED dump is, and the
  launcher scales the already-decoded frame by it before rendering.
  Regression tests: `tests/test_launcher.py::ReadLatestHub75BrightnessTests`,
  `DrawHub75ScalesToConfiguredBrightnessTests`. Verified against a real
  boot on the user's own SD card: the startup ramp logged 0 -> 29 -> 70
  -> 118 -> 164 (throttled to 5 lines for the whole ramp, not one per
  frame), and the rendered preview's sampled max channel value tracked
  the decoded brightness exactly (164/255 of the raw 255 max).
- Add host-microphone-driven FFT level emulation. Supersedes the
  "microphone/FFT input never produces a real sample" entry above for
  the specific thing this firmware actually uses mic input for: an
  overall loudness number, not real spectral content. The earlier entry
  was right that there's no way to model the continuous/DMA ADC
  `fft.cpp`'s real sampling needs, but that's the wrong layer to fix --
  `FFT::getBandValue(int i)` is the one place every consumer
  (`getBandValueFft` in Lua, `lualib/fft.lua`'s `getSpeechLevel()`) ever
  reads a band's energy from, the same shape as the already-proven
  `digitalRead()` override used for the boop sensor. Firmware-emu patch
  `0015-emu-fft-mic-override.patch` adds a third console command
  (`MIC:<energy>\n`) that overrides every band's return with that value
  until a new one arrives. The launcher's new optional "Mic" button
  captures the host microphone via the `sounddevice` package (the one
  launcher dependency outside the standard library; button stays
  disabled with install instructions if it's missing), maps the level
  to dBFS (a standard, perceptually-linear VU-meter domain) and scales
  it onto the SD card's own `fft.speech_min_energy`/`max_energy` range,
  spread evenly over `speech_band_start..speech_band_end`. A VU meter
  next to the button shows the live level with a marker at
  `speech_frist_frame_threshold`, the firmware's own activation point.
  Regression tests: `tests/test_launcher.py::RmsToLevelMappingTests`,
  `MicSendLoopInjectsScaledEnergyTests`, `MicThresholdFractionTests`,
  `MicButtonReflectsAvailabilityTests`. Verified end to end against a
  real boot on the user's own SD card: a direct probe
  (`getBandValueFft(2)`) tracked every injected `MIC:<n>` value exactly;
  separately confirmed the console-command parser and the override
  itself add no UART log volume (no echo print, unlike IR/GPIO), since
  this channel streams continuously rather than firing once per action.
- Fix HUB75 brightness preview never updating after the startup ramp.
  `Devices::SetMaxBrightness()` (patch 0014's hook) only ever sees the
  one-time gentle ramp at boot; every runtime brightness change (the
  settings menu's brightness slider, `screentest.lua`'s calibration
  screen) goes through the separate `setPanelBrightness()` Lua binding
  (`src/lua/luafunctions/drawingfunctions.cpp`), which writes straight
  to the display driver and never calls `SetMaxBrightness()` -- so the
  launcher's preview was stuck at whatever the startup ramp last set,
  for the rest of the session. New patch
  `0016-emu-setpanelbrightness-dump.patch` adds the identical throttled,
  change-gated dump to that second call site too (independent static
  state; the two call sites are genuinely different code). Verified
  against a real boot: a direct Lua-side probe calling
  `setPanelBrightness(200)` 8s after the startup ramp finished (which
  had settled on 218) produced `[EMU] HUB75_BRIGHTNESS value=200`
  immediately, where before this fix nothing would have been logged at
  all for that call.
- Fix the Mic button being silently disabled instead of explaining why
  it can't start. A disabled `ttk.Button` never fires its `command`, so
  the explanatory dialog for a missing `sounddevice` install or an SD
  card without `fft.enabled: true` never actually showed -- the button
  just looked broken. It now stays clickable and shows the real reason
  on click; capture still doesn't start in either case. Also add an
  "Input" device dropdown (`sounddevice.query_devices()`'s input-capable
  devices, "Default input device" first) instead of always opening
  whatever the host's current default happens to be, and a visible
  label under the VU meter showing the exact
  `fft.speech_frist_frame_threshold`/`speech_min_energy`/`max_energy`
  values driving the marker, instead of leaving the user to guess what
  the line means. Regression tests:
  `tests/test_launcher.py::MicButtonExplainsWhyItCannotStartTests`.
- Add a reminder to `scripts/build-firmware-emu.sh`'s output stating
  whether `PROTOPANDAEMU_EMU_LED_DUMP` was set for that build (LED strip
  preview enabled/disabled) -- an easy thing to forget on a rebuild done
  for an unrelated reason, since it's a per-build env var, not a
  persisted setting, and nothing else surfaces its state.
- Make the LED strip dump always-on, matching HUB75_BRIGHTNESS.
  Supersedes both the earlier opt-in entry and the build-script reminder
  entry above: the opt-in `PROTOPANDAEMU_EMU_LED_DUMP` flag kept lapsing
  on rebuilds done for an unrelated reason (confirmed: a user rebuild to
  pick up the mic feature silently dropped it, with zero `LED_DUMP`
  strings in the resulting binary), and a build-time reminder message
  didn't fix that, it just explained it after the fact. Already
  throttled (100ms) and change-gated (memcmp against the last dumped
  buffer) from the start, unlike the original unthrottled I2C dump
  mistake earlier in this project that first justified opt-in dumps in
  general -- measured worst case (continuous animation, 64 LEDs) is
  ~4KB/s, smaller than ordinary boot logging. Patch
  `0013-emu-ledstrip-dump.patch` regenerated as a full replacement
  (still diffed from the true unpatched source, not layered on the old
  patch) with the flag removed.
- Fix the HUB75/OLED preview looking oversized relative to its own
  content, and the Mic row's gain being effectively fixed. The first
  was a window-height regression from the Mic feature: screenshotted
  the real GUI under Xvfb and measured the root window at 1600x1080,
  up from 1600x958 before that feature existed -- taller than many
  screens can actually show once window-manager chrome is accounted
  for, which is what "the square looks bigger than the real view"
  turned out to mean (the window partly off-screen, not a rendering
  bug in the OLED/HUB75 canvases themselves; confirmed by measuring
  their decoded content fills them correctly once a real session is
  running). Compacted the Mic row (dropped its own section header,
  combined the button/device-picker/threshold text onto one row, a
  slimmer VU meter) back down to 1600x1019. Second: added a "Gain"
  spinbox (0.5x-8x, default 3x) multiplying the captured RMS before
  the dBFS mapping, and lowered the meter's floor from -50 to -62
  dBFS -- a real user's own device-side FFT calibration bars reacted
  to their voice but only ever registered small levels, meaning the
  signal path worked end to end but a quiet, unboosted mic needed more
  headroom than the original floor gave it to reliably cross the
  firmware's speech activation threshold. Also prefer a device literally
  named "pulse" over PortAudio's own "default" when populating the
  input dropdown: on a PulseAudio desktop "default" can resolve to a
  raw ALSA device PulseAudio already holds exclusively, opening silent
  or failing outright, which is exactly what another user report
  described ("mic works when I pick pulse"). Regression tests:
  `tests/test_launcher.py::RmsToLevelMappingTests::test_gain_boosts_a_quiet_signal`,
  updated `test_floor_db_is_empty_meter` to isolate the floor boundary
  from the new gain multiplier.
- Add a per-band FFT equalizer popup, and switch the mic override
  protocol from one value shared across every band to
  `MIC:<band>:<value>\n`. A real user reported the Mic feature still
  not triggering the firmware's overlay despite its own device-side FFT
  calibration screen visibly reacting to their voice, and suspected the
  uniform level -- every band rising together -- might be the problem,
  since the firmware's own speech detection may key on band *shape*,
  not just total energy, in ways a flat injected level never produces.
  Firmware-emu patch `0015-emu-fft-mic-override.patch` regenerated: the
  override is now a `g_emuFFTBandOverride[64]` array (same pattern as
  the GPIO override) instead of one scalar, with `FFT::getBandValue(i)`
  reading index `i` instead of one shared value. The launcher's existing
  Mic capture now sends the same per-band protocol (one line per
  configured speech band, same uniform value as before -- its behavior
  is otherwise unchanged); a new "Equalizer..." popup button gives one
  vertical slider per FFT band plus a boost multiplier, for setting any
  band shape directly to test what actually crosses the firmware's
  activation threshold. Starting either one stops the other, since both
  drive the same per-band state; closing the Equalizer releases every
  band back to the real (always-zero) read. Regression tests:
  `tests/test_launcher.py::EqualizerTests`, updated
  `MicSendLoopInjectsScaledEnergyTests` for the new per-band wire
  format. Verified against a real boot: a direct Lua-side probe reading
  `getBandValueFft(0..4)` showed three injected values (5000, 10000,
  99999 on bands 1-3) exactly, with bands 0 and 4 unaffected.
- Fix Mic and the Equalizer stopping each other, and widen Mic's
  effective gain range. A real user turned push-to-talk off (the actual
  cause of their earlier "overlay never responds" report -- the
  firmware ships it ON by default, gating overlay *visibility*, not the
  underlying FFT level, on holding a button) and still saw no response,
  this time because the level reaching the firmware was genuinely too
  quiet to cross the activation threshold with only -62 dBFS of floor
  and up to 8x gain to work with. Also: opening the Equalizer used to
  call `_stop_mic_capture()` and vice versa (both drove the same
  per-band console state through two separate timers, so letting them
  run concurrently would have had them race and flicker), which meant
  the Equalizer was never actually a way to *inspect* what the Mic
  button was sending -- only an alternative to it.
  - Lowered the meter floor to -75 dBFS and widened the Gain spinbox
    from 0.5x-8x to 0.1x-50x; confirmed a quiet rms=0.0001 (very quiet
    speech on an unboosted built-in mic) now crosses a real 40000
    threshold at gain=10, where the old -62 dBFS floor/8x gain ceiling
    left it at 0.
  - Unified Mic and the Equalizer onto one shared `_injection_loop()`
    (replacing the separate `_mic_send_loop()`/`_eq_send_loop()`) that
    runs whenever either is active, so they can run together instead of
    the one mutexing the other.
  - Added a "Scale by live mic level" checkbox to the Equalizer: with it
    on and Mic also running, every slider is multiplied by the Mic
    button's captured level each tick, turning the Equalizer into a
    live per-band shape editor for the real captured sound (0 whenever
    Mic isn't running) instead of only a static test pattern -- lets
    real speech reshape across bands while watching exactly what's
    being sent, instead of guessing why nothing moved.
  - Added a live "Captured: <rms> RMS (<dB> dBFS raw, before gain)"
    readout next to the Mic row, so a quiet result can be told apart
    from "no signal reaching here at all" without needing a debugger.
  Regression tests: updated `MicSendLoopInjectsScaledEnergyTests` for
  `_injection_loop`/`_injection_job_id`; new `EqualizerTests` cases
  `test_scale_by_mic_uses_zero_when_mic_off`,
  `test_scale_by_mic_multiplies_slider_by_captured_level`,
  `test_mic_and_equalizer_can_run_together`.
- Fix the Equalizer resetting every slider to 0 on reopen. A real user
  set "All to max", closed the popup, and reopened it to find every
  slider back at 0 -- `_open_equalizer()` always built fresh
  `tk.IntVar(value=0)` widgets with no memory of what was there before,
  even though releasing the console override on close (so the firmware
  doesn't keep seeing stale test data) was always intentional. Added
  `self.eq_band_values: list[int]`, a plain list that survives the
  popup's own widgets being destroyed; each slider's `IntVar` now
  traces its own writes back into it immediately, and `_open_equalizer`
  seeds new sliders from it (resetting to zeros only if `fft_band_count`
  changed, e.g. a different SD card was loaded in between). Separately
  investigated and ruled out a second report ("only channels 2-9
  respond even with All to max") as a real bug: a direct 32-band probe
  on a real boot, injecting 50000 into every band via `MIC:<band>:
  50000\n`, confirmed the override reaches all 30 valid bands this
  session's `m_bandCount` actually has (indices 30-31 read 0, matching
  the firmware's own out-of-range bound check, not ours) -- the
  "channels 2-9" the user saw on the device's own calibration screen is
  that screen's speech-level readout, which by firmware design (not
  emulator behavior) only ever sums `speech_band_start`..`speech_band_end`
  (2-8 on this card); the full spectrum bars on that same screen were
  unaffected and would have shown every band. Regression tests:
  `EqualizerTests::test_slider_positions_survive_close_and_reopen`,
  `test_band_count_change_resets_stale_positions`.
- Filter `[EMU] LED_DUMP` lines out of the visible UART panel. Each
  line carries a full per-frame hex color buffer and was drowning out
  everything else in the panel. The strip preview itself is unaffected
  -- it reads the underlying log file directly, not this display copy.
- Set PROTOPANDAEMU_QUIET_UART by default when the GUI starts. The
  previous UART-panel filter only covered the GUI's own text widget;
  `supervisor._drain_uart` separately echoes every raw byte to the
  host terminal too (intentional for the headless CLI, which has no
  other live output), so the launcher still flooded a real terminal
  with LED_DUMP lines even after that fix. An explicit override still
  wins. Verified on a real boot: zero UART bytes reached stdout.
