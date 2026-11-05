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

**Early development. A headless runner works; the desktop emulator is not ready yet.**

| Component | Status |
| --- | --- |
| Firmware build | Pinned Proto-panda v3.4.0 build and artifact verification. |
| Headless runner | Profile validation, live UART, resource reports and bounded sessions. |
| Persistent state | Flash and private SD state survive a clean stop and restart. |
| Folder-backed storage | Read-path prototype verified; safe write-back is in progress. |
| HUB75 and OLED | Peripheral probes exist; complete rendering is not available yet. |
| Desktop launcher | Planned after storage and rendering. |

The runner uses an experimental [Espressif QEMU](https://github.com/espressif/qemu) fork with patches in [`backend/qemu/`](backend/qemu/). Stock backend probes still have boot blockers. Results from the patched fork do not imply support in stock QEMU.

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

The importable API is `protopandaemu.supervisor.run_session(...)`. Call `SessionHandle.stop()` for a clean stop. Flash and SD state are separate persistence domains; private SD persistence is not folder write-back.

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

## Next milestones

1. Complete folder-backed storage with safe write-back, backups and conflict recovery.
2. Render HUB75 and OLED output from the firmware's actual peripheral traffic.
3. Add a desktop launcher with per-panel wiring controls, previews and live diagnostics.

See [`CHANGELOG.md`](CHANGELOG.md) for implemented changes.
