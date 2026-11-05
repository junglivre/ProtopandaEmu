# M5 manual walkthrough evidence

Fresh desktop run on 2026-11-05 using:

```bash
python3 -m protopandaemu.launcher \
  --hostdir /tmp/protopandaemu-m5-proof-b7gaee6z/host \
  --timeout 30
```

The four PNGs are direct captures of the Tk window's X11 window (`xwd` by
window ID, then ImageMagick conversion), not synthetic previews. The first
capture is the pre-session folder-picker/wiring step, so it correctly shows no
session or profile identity yet. Captures 02–04 keep the same profile hash and
session identity visible. The same mixed wiring selection is visible in every
capture:

```json
{
  "flip_left": false,
  "flip_right": true,
  "color_scheme_left": "bgr",
  "color_scheme_right": "grb"
}
```

The profile hash is `4175c0a186e1597e47a5d6dd935d5b59c0a0d30ccd366f1ab30eed64198f2c7c`.
The real session identity is `e73a6bb5-c1c5-4c93-9830-9f1d72368aaa`.

`03-running-real-preview.png` shows the latest real OLED GDDRAM dump and the
latest real HUB75 RGB888 dump. The HUB75 panel is visibly black and the GUI
labels it as the known M4 `clearScreen()` state. `04-stopped-report.png` shows
the same identity after the GUI Stop callback reports `stopped`.

The copied `manual-qemu.stderr.log` and `manual-qemu.uart.log` are the source
logs for the preview and live UART panel.
