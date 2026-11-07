# Protopanda Emulator

<p align="center">
  <img src="https://github.com/mockthebear/proto-panda/raw/main/doc/logoprotopanda.png" alt="Protopanda logo">
</p>

**🇺🇸 English** | [🇧🇷 Português](README.pt-BR.md)

Run [Proto-panda](https://github.com/mockthebear/Proto-panda) firmware on your computer, without the physical board.

Protopanda Emulator is being built for testing animations, checking panel wiring and inspecting firmware behavior before deploying to an ESP32-S3. The goal is to run the original firmware, including its Lua scripts and storage, rather than recreate the application on the host.

## Target hardware

- **ESP32-S3 N16R8:** dual-core LX7, 16 MiB flash and 8 MiB octal PSRAM.
- **Two 64×32 HUB75 panels:** independent channel order and orientation per panel.
- **SSD1306 OLED:** 128×64 display over I²C.
- **Virtual SD card:** a local folder for configuration, Lua scripts and PNG assets.

## Current status

**The "run your own firmware fork and SD card as-is" workflow (see
below) is the primary supported path and is solid day to day:** real
QEMU-model bugs (I2C logging stall, a HUB75 decoder quadrant-duplication
bug, an SDMMC race that blocked all real content) are fixed, the desktop
launcher is a translated dark-theme GUI with a device-accurate
three-panel preview, a firmware picker, a virtual IR controller and a
boop proximity-sensor toggle. The separate, spec-driven M2-M5 milestone
track (pinned Proto-panda v3.4.0, `prove-m2.py`/`prove-m3.py`/
`prove-m4-oled.py`) also works end to end, but a formal six-permutation
golden-frame comparator for that track's own Rock 3/M4 acceptance gate
was never built — that gap doesn't block the as-is workflow, which
relies on the same already-verified HUB75 decoder.

| Component | Status |
| --- | --- |
| Firmware build | Pinned Proto-panda v3.4.0 build and artifact verification. An `-DPROTOPANDAEMU_EMU=1` variant also builds from a user's own firmware fork as-is (`scripts/build-firmware-emu.sh`), gated entirely behind that flag. |
| Headless runner | Profile validation, live UART, resource reports and bounded sessions. |
| Persistent state | Flash and private SD state survive a clean stop and restart. |
| Folder-backed storage | A chosen folder becomes the firmware's SD card: safe staging, crash-recoverable commit, backups and conflict retention. |
| OLED | Real SSD1306 I2C protocol, real 128x64 GDDRAM; byte-exact against an independently derived oracle. |
| HUB75 | Decoder verified against real GDMA traffic (continuous streaming, real multi-plane descriptors); a sentinel-descriptor bug that duplicated dense frames into mirrored quadrants is fixed. Real animated content is captured end to end (`artifacts/mvp/hub75-real-content-proof/`). The decoded color data is always full-brightness (brightness is a separate OE/blanking signal this decoder can't see); the launcher scales the preview to the real configured brightness separately, see below. |
| Desktop launcher | Translated, dark-theme Tkinter GUI: a three-panel preview (OLED + both HUB75 halves) at native aspect, aspect-locked resize, independent left/right wiring controls, a firmware picker (explicit choice overrides auto-detection), real hostdir sessions, live diagnostics, a virtual IR controller, a boop proximity-sensor toggle for SD cards using `trigger_mode: "gpio"`, an opt-in live LED strip preview (firmware-buffer capture, see below), and an opt-in Mic VU meter that captures the host microphone and injects its level as the firmware's FFT speech-energy reading, see below. |

**Known gaps** (unemulated or unverified hardware, not backend bugs):

- **Raw microphone ADC sampling is not modeled**, so voice-reactive
  behavior driven straight from the hardware never triggers on its own.
  This backend only models the single-shot SAR ADC peripheral
  (`backend/qemu/new-files/hw/misc/esp32s3_sens.c`, `docs/qemu-adc-spike.md`)
  that a different firmware subsystem polls during boot calibration;
  there is no model for the continuous/DMA-mode ADC `fft.cpp`'s
  microphone sampling requires (`adc_continuous_read`), and
  `readAndAnalyse()` silently no-ops on every `ESP_ERR_TIMEOUT` with no
  error logged. Instead, the launcher's optional Mic feature captures
  the host's own microphone and injects the level directly at the one
  point downstream code actually reads (`FFT::getBandValue()`, which
  only ever carries an overall loudness number, not real spectral
  content) — see "Run your own firmware fork and SD card as-is" below.
- **Servo output has no visual feedback.** The firmware writes a real
  PWM duty cycle; the launcher has no way to show the resulting angle.
- **BLE input mode is not supported** (no NimBLE/Bluedroid radio model);
  only `input.mode: "infrared"` boots reliably in this backend.

## Try the headless runner

Development currently targets Linux. You need Python 3, `mtools`, the pinned firmware build and the experimental QEMU executable. The Python runner itself uses the standard library.

In a prepared workspace, run the end-to-end check:

```bash
python3 scripts/prove-m2.py
```

This boots the real firmware twice, checks persisted state, rejects an incompatible profile and exercises timeout and interrupt cleanup. Each run prints the path to its logs and JSON reports.

The check uses a display-disabled SD fixture to isolate boot and storage from unfinished rendering. Its progress markers come from Lua running inside the guest.

### Run a session

Save this as `profile.json` at the repository root. Relative firmware paths resolve from the profile's directory.

```json
{
  "firmware_image": "artifacts/build-normal/release/merged_flash.bin",
  "firmware_sha256": "811774774e8a9dbfd9d7a8278efc03b8bb6cd1a028c18ba68e7c7cee26f04bf3",
  "board_profile": {
    "chip": "esp32s3",
    "cores": 2,
    "cpu_mhz": 240,
    "flash_bytes": 16777216,
    "psram_bytes": 8388608,
    "psram_mode": "octal"
  },
  "log_level": "info"
}
```

```bash
python3 -m protopandaemu run \
  --profile profile.json \
  --report run.report.json \
  --uart-log run.uart.log \
  --timeout 90
```

UART streams to the terminal. The JSON report records the firmware and profile hashes, boot markers, available guest memory metrics and the exit reason. `Ctrl+C` stops the backend; reaching the timeout returns exit code `124`. Guest networking is disabled.

The importable API is `protopandaemu.supervisor.run_session(...)`. Call `SessionHandle.stop()` for a clean stop. Flash state and the folder-backed SD domain are kept separate, as described next.

### Point the firmware at a folder

Add `--hostdir` to use a real folder instead of the generated sample SD card. The folder must already contain `hardware.json`, `animation.json` and any Lua/PNG assets the firmware reads; see the pinned `.work/upstream-v3.4.0` checkout for the stock layout.

```bash
python3 -m protopandaemu run \
  --profile profile.json \
  --hostdir /path/to/your/protopanda-data \
  --report run.report.json \
  --timeout 90
```

The folder is staged into a private image before boot and never passed to QEMU directly. On a clean stop, guest writes are committed back through a durable journal: new and changed files are written, backed up and renamed as one recoverable unit. A file the firmware changed while you also edited it on the host is never silently overwritten — the guest version is kept at a `<name>.protopandaemu-conflict-...` sibling instead, and the session report lists every conflict and backup. If a run is interrupted mid-commit, the next session (or `python3 -m protopandaemu fsck-hostdir /path/to/your/protopanda-data`) finishes or rolls back the same commit before anything else touches the folder.

Run the end-to-end check for this path with:

```bash
python3 scripts/prove-m3.py
```

This stages a real folder, boots the firmware, confirms the guest's own Lua read/write reached the host, and exercises the full error, conflict and crash-recovery matrix with real filesystem faults.

### Run the desktop launcher (M5)

The local launcher uses the same in-process supervisor API as the headless
runner. It writes the upstream per-frame `flip_left`, `flip_right`,
`color_scheme_left` and `color_scheme_right` fields to the selected folder's
`animation.json`, then starts and stops the real experimental QEMU session.

```bash
python3 -m protopandaemu.launcher --hostdir /path/to/your/protopanda-data
```

The default `--timeout` is 600s: a real folder's first boot composes the
firmware's `frames.bulk` on FFat (seconds to ~100s depending on asset
count) before `--hostdir`'s normal boot progress continues; a shorter
timeout kills the session mid-compose and leaves a partial bulk file for
the next boot. The compose only happens once per flash-state file.

The preview decodes the latest `GDDRAM_DUMP` and `FRAME_DUMP` lines from
the session report's stderr log into a three-panel layout matching the
physical device: OLED centered above a left/right HUB75 pair, each at its
exact native aspect. Window resizing is locked to the launcher's own
aspect ratio, so dragging a corner scales every panel proportionally
instead of distorting one axis. **This backend has no BLE radio model**:
a real SD card's `keybinds.json` with `input.mode: "BLE"` boots straight
into a crash/reboot loop against the stock pinned firmware (confirmed) —
the launcher warns before starting if it detects this, and the
workaround is switching that file's `input.mode` to `"infrared"` with a
numeric `infrared_gpio`. A local desktop is required. In a headless Linux
environment with the repository's extracted Xvfb, start a real X display first
and run the same command with `DISPLAY` set:


```bash
.work/mvp-display/root/usr/bin/Xvfb :99 -screen 0 1400x1000x24 -nolisten tcp &
DISPLAY=:99 python3 -m protopandaemu.launcher --hostdir /path/to/your/protopanda-data
```

The automated M5 proof drives the actual Tk widget callbacks and a real
supervisor session:

```bash
DISPLAY=:99 python3 scripts/prove-m5-launcher.py
```

### Run your own firmware fork and SD card as-is

`scripts/build-firmware-emu.sh` builds the launcher's firmware from a
local firmware source tree (default `PROTO_PANDA_FIRMWARE_SRC`, override
via that env var) with `-DPROTOPANDAEMU_EMU=1`, applying
`backend/firmware-emu/patches/` to a disposable copy — never to your
source tree. Those patches only touch what this backend cannot otherwise
support: the shared IR/SDMMC pin uses `gpio_config()` instead of
`pinMode()` so claiming it doesn't detach the SD bus, `BleManager::begin()`
runs with no radio so BLE-gated driver `onEnable()` calls pass under
`input.mode: "infrared"`, a console command (`IR:<addr>:<cmd>\n` over
UART) injects virtual IR button presses, a second console command
(`GPIO:<pin>:<0|1>\n`) overrides any digital input pin's read value (used
for the boop proximity-sensor toggle below), a third console command
(`MIC:<energy>\n`) overrides `FFT::getBandValue()`'s return for every
band -- the same loudness-only override the Mic VU meter below drives
from the host's own microphone, since this backend has no model for the
continuous/DMA ADC `fft.cpp`'s real sampling needs -- the real
`FastLED.show()` WS2812 write stays stubbed out (QEMU's RMT model
completes the transaction but doesn't decode signal timing, so there's
no wire to light). An opt-in build flag (`PROTOPANDAEMU_EMU_LED_DUMP`,
off by default -- even throttled, a long animated session adds up to
meaningful UART log volume) additionally dumps `LedStrip::Display()`'s
real computed color buffer to the console, for the live LED preview
below; set `PLATFORMIO_BUILD_FLAGS="-DPROTOPANDAEMU_EMU_LED_DUMP"`
before running `build-firmware-emu.sh` to turn it on, no source tree
edits needed. A fourth, always-on console line
(`[EMU] HUB75_BRIGHTNESS value=<0-255>`) reports every real panel
brightness change (`Devices::SetMaxBrightness`, direct or the gentle
per-frame ramp), throttled and change-gated the same way; the launcher
scales its decoded (always full-brightness) preview by that value. No
firmware config, pin assignment, or SD data is edited to make any of
this work — board profile and GPIO assignments stay real.

```bash
PATH="$HOME/.platformio/penv/bin:$PATH" PROTO_PANDA_FIRMWARE_SRC=/path/to/your/fork \
  bash scripts/build-firmware-emu.sh
python3 -m protopandaemu.launcher --hostdir /path/to/your/protopanda-data
```

The launcher automatically picks up
`artifacts/build-emu/release/merged_flash.bin` when present — no env vars
needed; an explicit pick from the "Choose firmware..." button always
overrides auto-detection. The "Simulated IR controls" panel sends real IR
commands from your `keybinds.json` over the session's UART while it
runs. For SD cards with `misc.json`'s `boop.trigger_mode: "gpio"`, a
"Boop" toggle next to it overrides that pin's read value the same way
(purple when near, grey when far); it has no effect on lidar-mode boop
configs, which have no digital pin to override. Below the HUB75/OLED
preview, a LED strip row renders the real colors `LedStrip::Display()`
computed (not a hardware-traffic trace like HUB75/OLED, see Current
status above) for SD cards that drive WS2812 output -- it stays empty
unless the firmware was built with `PROTOPANDAEMU_EMU_LED_DUMP` (see
above). Below that, for SD cards with `misc.json`'s `fft.enabled: true`,
a "Mic" button starts capturing the host's own microphone through the
optional [`sounddevice`](https://python-sounddevice.readthedocs.io/)
package (`pip install sounddevice`; on Linux it also needs the
`libportaudio2` system library, e.g. `sudo apt install libportaudio2`)
and streams its level into the `MIC:<energy>\n` override above, scaled
into the SD card's own `fft.speech_min_energy`/`speech_max_energy`
range; the VU meter next to it shows the live level with a marker at
`fft.speech_frist_frame_threshold`, the same activation point the
firmware's own speech detection uses. This is the only launcher feature
outside the standard library -- the button stays disabled with install
instructions if `sounddevice` isn't present.

### Verify the OLED output

```bash
python3 scripts/prove-m4-oled.py
```

This boots the firmware with its real display configuration, captures the SSD1306's actual I2C command and pixel traffic, and checks the splash-icon frame byte-for-byte against an oracle this script derives independently from the firmware's own compiled-in icon bitmap. It stops the session right after the OLED work completes, by design, rather than waiting for the rest of boot.

## Build and inspect

- [`scripts/build-firmware.sh`](scripts/build-firmware.sh) builds the pinned release and debug firmware. It requires PlatformIO Core 6.2.0; Python dependency pins are in [`requirements-build.txt`](requirements-build.txt).
- [`backend/qemu/build-experimental-qemu.sh`](backend/qemu/build-experimental-qemu.sh) builds the patched backend. It currently expects a prepared dependency sysroot in `.work/qemu-deps`; there is no one-command installer yet.
- [`scripts/spike.py`](scripts/spike.py) runs bounded backend experiments. [`artifacts/`](artifacts/) holds captured results, logs and hashes.

```bash
bash scripts/build-firmware.sh --out artifacts/build-normal
bash backend/qemu/build-experimental-qemu.sh
```

Run individual backend probes with:

```bash
bash scripts/spike.sh --backend=qemu --mode=boot
bash scripts/spike.sh --backend=qemu --mode=hub75-probe
```

A probe can report a blocker or `NOT_EXECUTED`; neither means the firmware passed. Raw images, downloaded tools and scratch files stay under the ignored `.work/` and `.rf/` directories.

## Next steps

1. A formal, six-permutation golden-frame comparator for the pinned
   v3.4.0 track's own HUB75 acceptance check was never built; it isn't
   required for the as-is workflow, which already captures and verifies
   real animated content end to end.
2. BLE input and web/edit-mode tooling remain out of scope — no radio
   model exists for this backend.

See [`CHANGELOG.md`](CHANGELOG.md) for implemented changes.
