# ProtopandaEmu spike runner

This repository contains the initial M0/M1 research harness only. It is not an
emulator product, renderer, firmware compatibility layer, or completed M1.
`scripts/spike.sh` runs bounded, headless experiments against pinned backend
executables and records raw stdout/stderr plus hashes. It never adds guest UART
markers and it does not implement a host-directory bridge.

Use `scripts/spike.sh --help` for the common entry point. The reproducible
current-image commands are:

```bash
bash scripts/spike.sh --backend=esp-emulator --mode=boot
bash scripts/spike.sh --backend=qemu --mode=boot
bash scripts/spike.sh --backend=esp-emulator --mode=hub75-probe
bash scripts/spike.sh --backend=qemu --mode=hub75-probe
bash scripts/select-lead-backend.sh
```

`NOT_EXECUTED` is a recorded research outcome, not a backend PASS or proof of
impossibility. `--backend=none --mode=hostdir-probe` only records that the VFS
prerequisite was not met; engine-specific hostdir probing is explicitly
unsupported and does not create a sentinel or claim a bridge.

## Pinned native backend inputs

The ignored `.rf/backends/` directory is expected to contain these exact
archives. Verify the digest before extracting; do not replace an executable
under an existing pinned path.

```bash
mkdir -p .rf/backends
curl -fL -o .rf/backends/esp-emu-0.51.0.tar.gz \
  https://github.com/espressif/esp-emulator/releases/download/v0.51.0/esp-emu-0.51.0-x86_64-unknown-linux-gnu.tar.gz
echo '2b9e157470edb8b03167996550292c6df8cc6716025ebad8927db950d7931417  .rf/backends/esp-emu-0.51.0.tar.gz' | sha256sum -c -
tar -tzf .rf/backends/esp-emu-0.51.0.tar.gz
tar -xzf .rf/backends/esp-emu-0.51.0.tar.gz -C .rf/backends

curl -fL -o .rf/backends/qemu-9.2.2.tar.xz \
  https://github.com/espressif/qemu/releases/download/esp-develop-9.2.2-20260417/qemu-xtensa-softmmu-esp_develop_9.2.2_20260417-x86_64-linux-gnu.tar.xz
echo '0eecb2a34a5586c0e59110f77b9343b7b336e82fdb0e1a30e1dc1bab8a547e35  .rf/backends/qemu-9.2.2.tar.xz' | sha256sum -c -
tar -tJf .rf/backends/qemu-9.2.2.tar.xz
tar -xJf .rf/backends/qemu-9.2.2.tar.xz -C .rf/backends
```

The firmware build inputs and reproducible commands are in
[docs/firmware-build.md](docs/firmware-build.md); Python/build dependencies
are listed in `requirements-build.txt`. Backend archives and extracted files
remain under `.rf/backends/`; transient build/setup state belongs under `.work/`.
This is still a research runner with incomplete M1/G0 evidence, not a hosted
emulator feature facade.

Run isolated integrity tests with `python3 tests/test_spike_integrity.py`; the
driver-owned `tests/test_build_safety.py` remains separate and unchanged.
