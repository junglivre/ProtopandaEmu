"""Desktop Tkinter launcher for the Protopanda session API.

The launcher deliberately stays thin: it edits the upstream animation frame
fields, writes a machine profile, and calls ``protopandaemu.supervisor`` in
process.  Display previews are decoded only from the QEMU device reports
captured by the supervisor; there is no synthetic rendering path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

try:
    import sounddevice as _sounddevice  # type: ignore[import-untyped]
except Exception:
    # Optional dependency: the microphone VU meter/FFT-level override is
    # the only feature that needs it (sounddevice -> PortAudio -> an
    # actual host input device). Everything else in the launcher works
    # with the standard library alone; this just disables the Mic button
    # instead of crashing the whole launcher on import.
    _sounddevice = None

from . import supervisor


ROOT = pathlib.Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "artifacts/build-normal/release/merged_flash.bin"
# Built by scripts/build-firmware-emu.sh from the user's own firmware fork
# with -DPROTOPANDAEMU_EMU=1; used automatically when present so the common
# "run my own firmware + SD card as-is" workflow needs no env vars.
EMU_FIRMWARE = ROOT / "artifacts/build-emu/release/merged_flash.bin"
WIRE_COLOR_SCHEMES = ("rgb", "rbg", "grb", "gbr", "brg", "bgr")

# Dark theme palette. Chosen once, applied uniformly via ttk.Style so every
# widget (frames, labels, buttons, combobox) matches instead of leaving
# Tk's light-grey default theme showing through gaps.
DARK_BG = "#1b1d24"
DARK_PANEL = "#242733"
DARK_PANEL_ALT = "#2d3040"
DARK_FG = "#e8e8f0"
DARK_FG_MUTED = "#9a9db3"
DARK_ACCENT = "#8b5cf6"
DARK_ACCENT_HOVER = "#a78bfa"
DARK_BORDER = "#3a3d4d"
DARK_CANVAS_BG = "#0c0d12"
DARK_GREEN = "#22c55e"
DARK_GREEN_HOVER = "#4ade80"
DARK_RED = "#ef4444"
DARK_RED_HOVER = "#f87171"
BOARD_PROFILE = {
    "chip": "esp32s3",
    "cores": 2,
    "cpu_mhz": 240,
    "flash_bytes": 16 * 1024 * 1024,
    "psram_bytes": 8 * 1024 * 1024,
    "psram_mode": "octal",
}
OLED_RE = re.compile(
    r"\[protopandaemu\.ssd1306\]\s+GDDRAM_DUMP\s+seq=(\d+)\s+"
    r"width=(\d+)\s+height=(\d+)\s+bytes=(\d+)\s+hex=([0-9a-fA-F]+)"
)
HUB75_RE = re.compile(
    r"\[protopandaemu\.hub75\]\s+FRAME_DUMP\s+seq=(\d+)\s+"
    r"width=(\d+)\s+height=(\d+)\s+rows_per_frame=(\d+)\s+bytes=(\d+)\s+"
    r"descriptors=(\d+)\s+hex=([0-9a-fA-F]+)"
)
# The LED strip dump comes from firmware-emu patch 0013, a buffer capture
# (not a verified hardware-traffic trace like the two above): it reads
# straight off the real console/UART, not QEMU's own device-model stderr.
LED_RE = re.compile(
    r"\[EMU\]\s+LED_DUMP\s+count=(\d+)\s+hex=([0-9a-fA-F]+)"
)
# HUB75 panel brightness (Devices::SetMaxBrightness, src/tools/devices.cpp):
# the real panel dims via the OE/blanking pin's duty cycle, which never
# appears in the color bit-plane DMA data HUB75_RE decodes, so the preview
# always reconstructs full, undimmed color on its own. This separate dump
# reports the real configured brightness value so the launcher can scale
# the already-decoded frame to match what the panel would actually show.
HUB75_BRIGHTNESS_RE = re.compile(
    r"\[EMU\]\s+HUB75_BRIGHTNESS\s+value=(\d+)"
)


class LauncherError(RuntimeError):
    """A user-facing launcher validation or lifecycle error."""


def _write_json(path: pathlib.Path, value: Any, *, sort_keys: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=sort_keys) + "\n", encoding="utf-8")


def selected_wiring(*, flip_left: bool, flip_right: bool,
                   color_scheme_left: str, color_scheme_right: str) -> dict[str, Any]:
    """Return the exact upstream per-frame fields written to animation.json."""
    if color_scheme_left not in WIRE_COLOR_SCHEMES or color_scheme_right not in WIRE_COLOR_SCHEMES:
        raise LauncherError("color scheme must be one of rgb, rbg, grb, gbr, brg, bgr")
    return {
        "flip_left": bool(flip_left),
        "flip_right": bool(flip_right),
        "color_scheme_left": color_scheme_left,
        "color_scheme_right": color_scheme_right,
    }


def apply_wiring_to_animation(path: str | pathlib.Path, wiring: dict[str, Any]) -> int:
    """Write upstream's real per-frame wiring fields and return frame count.

    The firmware accepts frame elements as either strings or objects.  A string
    element has no place for per-half modes, so it is promoted to the equivalent
    ``{"file": ...}`` object before the four real fields are added.
    """
    animation_path = pathlib.Path(path)
    try:
        document = json.loads(animation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LauncherError(f"cannot read animation.json: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("frames"), list):
        raise LauncherError("animation.json must contain a frames array")
    modes = selected_wiring(
        flip_left=wiring["flip_left"],
        flip_right=wiring["flip_right"],
        color_scheme_left=wiring["color_scheme_left"],
        color_scheme_right=wiring["color_scheme_right"],
    )
    updated: list[dict[str, Any]] = []
    for index, element in enumerate(document["frames"]):
        if isinstance(element, str):
            element = {"file": element}
        elif isinstance(element, dict):
            element = dict(element)
        else:
            raise LauncherError(f"animation.json frames[{index}] is neither an object nor a string")
        element.update(modes)
        updated.append(element)
    document["frames"] = updated
    _write_json(animation_path, document, sort_keys=False)
    return len(updated)


# A HUB75 FRAME_DUMP line can carry up to width*height*3*2 hex characters
# (~49KB for the current 128x64 fixture); reading only the tail keeps each
# refresh tick O(1) instead of O(log size) as a running session's stderr
# log grows into the megabytes, which otherwise stutters the live preview.
_DUMP_TAIL_BYTES = 262144


def _read_latest_dumps(stderr_path: pathlib.Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        with stderr_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _DUMP_TAIL_BYTES))
            tail = handle.read()
    except OSError:
        return None, None
    text = tail.decode("utf-8", errors="replace")
    oled_matches = list(OLED_RE.finditer(text))
    hub_matches = list(HUB75_RE.finditer(text))

    def decode_oled(match: re.Match[str]) -> dict[str, Any] | None:
        seq, width, height, byte_count, hex_data = match.groups()
        try:
            data = bytes.fromhex(hex_data)
        except ValueError:
            # A tail read can land mid-dump; skip broken matches instead of
            # crashing the dashboard refresh loop.
            return None
        if len(data) != int(byte_count):
            return None
        return {"seq": int(seq), "width": int(width), "height": int(height),
                "bytes": int(byte_count), "data": data}

    def decode_hub(match: re.Match[str]) -> dict[str, Any] | None:
        seq, width, height, rows, byte_count, descriptors, hex_data = match.groups()
        try:
            data = bytes.fromhex(hex_data)
        except ValueError:
            return None
        if len(data) != int(byte_count):
            return None
        return {"seq": int(seq), "width": int(width), "height": int(height),
                "rows_per_frame": int(rows), "bytes": int(byte_count),
                "descriptors": int(descriptors), "data": data}

    # Newest first; the first fully-decodable dump wins.
    oled = None
    for match in reversed(oled_matches):
        oled = decode_oled(match)
        if oled is not None:
            break
    hub = None
    for match in reversed(hub_matches):
        hub = decode_hub(match)
        if hub is not None:
            break
    return oled, hub


def _read_latest_led_dump(uart_path: pathlib.Path) -> dict[str, Any] | None:
    """Decode the newest LED_DUMP line from the live UART log tail.

    Unlike OLED/HUB75 (QEMU device-model stderr), this comes straight from
    the guest console: firmware-emu patch 0013 captures LedStrip::Display()'s
    real computed color buffer, since this backend's RMT model doesn't
    decode WS2812 signal timing (see the patch and LED_RE for why).
    """
    try:
        with uart_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _DUMP_TAIL_BYTES))
            tail = handle.read()
    except OSError:
        return None
    text = tail.decode("utf-8", errors="replace")
    for match in reversed(list(LED_RE.finditer(text))):
        count_text, hex_data = match.groups()
        count = int(count_text)
        try:
            data = bytes.fromhex(hex_data)
        except ValueError:
            # A tail read can land mid-dump; skip it and try the previous one.
            continue
        if len(data) != count * 3:
            continue
        return {"count": count, "data": data}
    return None


def _read_latest_hub75_brightness(uart_path: pathlib.Path) -> int | None:
    """Decode the newest HUB75_BRIGHTNESS value from the live UART log tail.

    See HUB75_BRIGHTNESS_RE for why this exists: the real panel's
    brightness never shows up in the color bit-plane DMA data HUB75_RE
    already decodes, so this is a second, independent dump of the one
    value that actually controls it (Devices::SetMaxBrightness).
    """
    try:
        with uart_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _DUMP_TAIL_BYTES))
            tail = handle.read()
    except OSError:
        return None
    text = tail.decode("utf-8", errors="replace")
    matches = HUB75_BRIGHTNESS_RE.findall(text)
    if not matches:
        return None
    value = int(matches[-1])
    return max(0, min(255, value))


class LauncherApp:
    """The small, testable desktop dashboard."""

    def __init__(self, root: tk.Misc, *, initial_hostdir: str | pathlib.Path | None = None,
                 timeout: float = 30.0):
        self.root = root
        self.timeout = timeout
        self.hostdir: pathlib.Path | None = None
        self.firmware_path: pathlib.Path | None = None
        self.profile_path: pathlib.Path | None = None
        self.handle: supervisor.SessionHandle | None = None
        self.last_report: dict[str, Any] | None = None
        self._after_id: str | None = None
        self._oled_photo: tk.PhotoImage | None = None
        self._hub75_left_photo: tk.PhotoImage | None = None
        self._hub75_right_photo: tk.PhotoImage | None = None
        self._led_photo: tk.PhotoImage | None = None
        self._last_led_dump: dict[str, Any] | None = None
        self.hub75_brightness = 255  # no dimming known yet; see _draw_hub75
        self.boop_gpio: int | None = None
        self.boop_near = False
        self._boop_default_sent = True  # no session yet, nothing pending
        self._hold_repeat_id: str | None = None
        # Microphone -> FFT-level override. See _load_mic_config() for the
        # SD card's own speech_band_start/end/min_energy/max_energy and
        # _start_mic_capture() for how a host RMS level maps into that
        # same domain.
        self.fft_enabled = False
        self.fft_band_start = 2
        self.fft_band_end = 8
        self.fft_min_energy = 50000
        self.fft_max_energy = 200000
        self.fft_frist_frame_threshold = 40000
        self._mic_stream = None
        self._mic_active = False
        self._mic_level = 0.0
        self._mic_level_lock = threading.Lock()
        self._mic_send_job_id: str | None = None
        self.mic_device_indices: list[int | None] = [None]
        self.mic_threshold_var = tk.StringVar(value="")

        self.hostdir_var = tk.StringVar(value="No folder selected")
        self.firmware_var = tk.StringVar(value="Auto-detected")
        self.profile_hash_var = tk.StringVar(value="Profile: —")
        self.session_var = tk.StringVar(value="Session: —")
        self.status_var = tk.StringVar(value="Ready: select the firmware SD folder.")
        self.resource_var = tk.StringVar(value="Resources: —")
        self.oled_var = tk.StringVar(value="OLED: waiting for GDDRAM dump")
        self.hub75_var = tk.StringVar(value="HUB75: waiting for FRAME dump")
        self.uart_var = tk.StringVar(value="UART: —")
        self.left_flip_var = tk.BooleanVar(value=True)
        self.right_flip_var = tk.BooleanVar(value=False)
        self.left_color_var = tk.StringVar(value="rgb")
        self.right_color_var = tk.StringVar(value="rgb")
        self.oled_rotate_var = tk.BooleanVar(value=False)
        self._last_oled_dump: dict[str, Any] | None = None
        self._last_hub_dump: dict[str, Any] | None = None

        self._apply_dark_theme()
        self._build_widgets()
        self._lock_aspect_ratio()
        if initial_hostdir:
            self.set_hostdir(initial_hostdir)
        self._schedule_refresh()

    def _apply_dark_theme(self) -> None:
        self.root.configure(bg=DARK_BG)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=DARK_BG, foreground=DARK_FG,
                        fieldbackground=DARK_PANEL_ALT, bordercolor=DARK_BORDER,
                        lightcolor=DARK_PANEL, darkcolor=DARK_PANEL, troughcolor=DARK_PANEL,
                        focuscolor=DARK_ACCENT)
        style.configure("TFrame", background=DARK_BG)
        style.configure("TLabelframe", background=DARK_BG, bordercolor=DARK_BORDER, relief="solid")
        style.configure("TLabelframe.Label", background=DARK_BG, foreground=DARK_FG_MUTED,
                        font=("TkDefaultFont", 10, "bold"))
        style.configure("TLabel", background=DARK_BG, foreground=DARK_FG)
        style.configure("TCheckbutton", background=DARK_BG, foreground=DARK_FG)
        style.map("TCheckbutton", background=[("active", DARK_BG)],
                 indicatorcolor=[("selected", DARK_ACCENT), ("!selected", DARK_PANEL_ALT)])
        style.configure("TButton", background=DARK_PANEL_ALT, foreground=DARK_FG,
                        bordercolor=DARK_BORDER, focusthickness=0, padding=6)
        style.map("TButton",
                 background=[("active", DARK_BORDER), ("disabled", DARK_PANEL)],
                 foreground=[("disabled", DARK_FG_MUTED)])
        style.configure("Accent.TButton", background=DARK_ACCENT, foreground="#ffffff", padding=8)
        style.map("Accent.TButton",
                 background=[("active", DARK_ACCENT_HOVER), ("disabled", DARK_PANEL)],
                 foreground=[("disabled", DARK_FG_MUTED)])
        style.configure("Start.TButton", background=DARK_GREEN, foreground="#ffffff", padding=8)
        style.map("Start.TButton",
                 background=[("active", DARK_GREEN_HOVER), ("disabled", DARK_PANEL)],
                 foreground=[("disabled", DARK_FG_MUTED)])
        style.configure("Stop.TButton", background=DARK_RED, foreground="#ffffff", padding=8)
        style.map("Stop.TButton",
                 background=[("active", DARK_RED_HOVER), ("disabled", DARK_PANEL)],
                 foreground=[("disabled", DARK_FG_MUTED)])
        style.configure("BoopActive.TButton", background=DARK_ACCENT, foreground="#ffffff",
                        bordercolor=DARK_BORDER, focusthickness=0, padding=6)
        style.map("BoopActive.TButton", background=[("active", DARK_ACCENT_HOVER)])
        style.configure("Dpad.TButton", background=DARK_PANEL_ALT, padding=10,
                        font=("TkDefaultFont", 12, "bold"))
        style.map("Dpad.TButton", background=[("active", DARK_ACCENT)])
        style.configure("TCombobox", fieldbackground=DARK_PANEL_ALT, background=DARK_PANEL_ALT,
                        foreground=DARK_FG, arrowcolor=DARK_FG, bordercolor=DARK_BORDER)
        style.map("TCombobox", fieldbackground=[("readonly", DARK_PANEL_ALT)],
                 foreground=[("readonly", DARK_FG)])
        style.configure("TSeparator", background=DARK_BORDER)
        style.configure("Vertical.TScrollbar", background=DARK_PANEL_ALT, troughcolor=DARK_BG,
                        bordercolor=DARK_BORDER, arrowcolor=DARK_FG_MUTED)
        style.map("Vertical.TScrollbar", background=[("active", DARK_BORDER)])
        # The combobox dropdown list is a plain Tk Listbox, not themed by
        # ttk.Style; set it via the option database so it doesn't flash
        # light-grey against an otherwise all-dark window.
        self.root.option_add("*TCombobox*Listbox.background", DARK_PANEL_ALT)
        self.root.option_add("*TCombobox*Listbox.foreground", DARK_FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", DARK_ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    def _build_widgets(self) -> None:
        self.root.title("Protopanda Emulator")
        self.root.minsize(1580, 900)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        # The sidebar scrolls (Canvas + inner Frame + Scrollbar, the
        # standard Tk pattern -- ttk has no native scrollable container):
        # firmware/wiring/profile controls can exceed the window height on
        # a shorter screen, and there was previously no way to reach the
        # content below the cutoff.
        controls_outer = ttk.Frame(self.root)
        controls_outer.grid(row=0, column=0, sticky="nsew")
        controls_outer.columnconfigure(0, weight=1)
        controls_outer.rowconfigure(0, weight=1)
        controls_canvas = tk.Canvas(controls_outer, bg=DARK_BG, highlightthickness=0, width=380)
        controls_canvas.grid(row=0, column=0, sticky="nsew")
        # No visible scrollbar: mouse-wheel/trackpad scroll (bound below)
        # covers it, and an always-present track looked cluttered for a
        # sidebar that only needs scrolling on a shorter screen.
        controls_scrollbar = ttk.Scrollbar(controls_outer, orient="vertical", command=controls_canvas.yview)
        controls_canvas.configure(yscrollcommand=controls_scrollbar.set)
        controls = ttk.Frame(controls_canvas, padding=12)
        controls_window = controls_canvas.create_window((0, 0), window=controls, anchor="nw")
        controls.bind("<Configure>", lambda _e: controls_canvas.configure(
            scrollregion=controls_canvas.bbox("all")))
        controls_canvas.bind("<Configure>", lambda e: controls_canvas.itemconfig(controls_window, width=e.width))

        def _scroll_controls(event: tk.Event) -> None:
            delta = event.delta if event.delta else (120 if event.num == 4 else -120)
            controls_canvas.yview_scroll(int(-delta / 120), "units")
        controls_canvas.bind("<Enter>", lambda _e: (
            controls_canvas.bind_all("<MouseWheel>", _scroll_controls),
            controls_canvas.bind_all("<Button-4>", _scroll_controls),
            controls_canvas.bind_all("<Button-5>", _scroll_controls)))
        controls_canvas.bind("<Leave>", lambda _e: (
            controls_canvas.unbind_all("<MouseWheel>"),
            controls_canvas.unbind_all("<Button-4>"),
            controls_canvas.unbind_all("<Button-5>")))

        controls.columnconfigure(0, weight=1)
        preview = ttk.Frame(self.root, padding=(0, 12, 12, 12))
        preview.grid(row=0, column=1, sticky="nsew")
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(2, weight=3)
        preview.rowconfigure(3, weight=1)

        ttk.Label(controls, text="Protopanda", font=("TkDefaultFont", 18, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 14))
        ttk.Label(controls, text="SD card").grid(row=1, column=0, sticky="w")
        ttk.Label(controls, textvariable=self.hostdir_var, wraplength=340,
                 foreground=DARK_FG_MUTED).grid(row=2, column=0, sticky="w", pady=(2, 4))
        ttk.Button(controls, text="Choose folder…", command=self.choose_folder).grid(
            row=3, column=0, sticky="ew", pady=(0, 12))

        ttk.Label(controls, text="Firmware").grid(row=4, column=0, sticky="w")
        ttk.Label(controls, textvariable=self.firmware_var, wraplength=340,
                 foreground=DARK_FG_MUTED).grid(row=5, column=0, sticky="w", pady=(2, 4))
        ttk.Button(controls, text="Choose firmware…", command=self.choose_firmware).grid(
            row=6, column=0, sticky="ew", pady=(0, 12))

        wiring = ttk.LabelFrame(controls, text="Per-half wiring (animation.json)", padding=8)
        wiring.grid(row=7, column=0, sticky="ew", pady=(0, 12))
        wiring.columnconfigure(1, weight=1)
        ttk.Label(wiring, text="Left half", font=("TkDefaultFont", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w")
        self.left_flip_check = ttk.Checkbutton(wiring, text="flip_left", variable=self.left_flip_var,
                                               command=self._on_wiring_changed)
        self.left_flip_check.grid(row=1, column=0, sticky="w")
        self.left_color_combo = ttk.Combobox(wiring, textvariable=self.left_color_var,
                                             values=WIRE_COLOR_SCHEMES, state="readonly", width=9)
        self.left_color_combo.grid(row=1, column=1, sticky="ew", padx=(8, 0))
        self.left_color_combo.bind("<<ComboboxSelected>>", self._on_wiring_changed)
        ttk.Label(wiring, text="color_scheme_left", foreground=DARK_FG_MUTED).grid(
            row=2, column=1, sticky="w", padx=(8, 0))

        ttk.Separator(wiring).grid(row=3, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Label(wiring, text="Right half", font=("TkDefaultFont", 10, "bold")).grid(
            row=4, column=0, columnspan=2, sticky="w")
        self.right_flip_check = ttk.Checkbutton(wiring, text="flip_right", variable=self.right_flip_var,
                                                command=self._on_wiring_changed)
        self.right_flip_check.grid(row=5, column=0, sticky="w")
        self.right_color_combo = ttk.Combobox(wiring, textvariable=self.right_color_var,
                                              values=WIRE_COLOR_SCHEMES, state="readonly", width=9)
        self.right_color_combo.grid(row=5, column=1, sticky="ew", padx=(8, 0))
        self.right_color_combo.bind("<<ComboboxSelected>>", self._on_wiring_changed)
        ttk.Label(wiring, text="color_scheme_right", foreground=DARK_FG_MUTED).grid(
            row=6, column=1, sticky="w", padx=(8, 0))

        self.create_profile_button = ttk.Button(controls, text="Create / update profile",
                                                command=self.create_profile)
        self.create_profile_button.grid(row=8, column=0, sticky="ew", pady=(0, 6))
        session_buttons = ttk.Frame(controls)
        session_buttons.grid(row=9, column=0, sticky="ew", pady=3)
        session_buttons.columnconfigure(0, weight=1)
        session_buttons.columnconfigure(1, weight=1)
        self.start_button = ttk.Button(session_buttons, text="Start session", style="Start.TButton",
                                       command=self.start_session)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.stop_button = ttk.Button(session_buttons, text="Stop session", style="Stop.TButton",
                                      command=self.stop_session, state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(3, 0))

        ttk.Label(controls, textvariable=self.status_var, wraplength=340).grid(
            row=10, column=0, sticky="w", pady=(14, 4))
        ttk.Label(controls, textvariable=self.profile_hash_var, wraplength=340,
                 foreground=DARK_FG_MUTED).grid(row=11, column=0, sticky="w", pady=2)
        ttk.Label(controls, textvariable=self.session_var, wraplength=340,
                 foreground=DARK_FG_MUTED).grid(row=12, column=0, sticky="w", pady=2)
        ttk.Label(controls, textvariable=self.resource_var, wraplength=340,
                 foreground=DARK_FG_MUTED).grid(row=13, column=0, sticky="w", pady=2)

        ttk.Label(preview, text="Backend preview", font=("TkDefaultFont", 14, "bold")).grid(
            row=0, column=0, sticky="w")
        status_row = ttk.Frame(preview)
        status_row.grid(row=1, column=0, sticky="w", pady=(2, 8))
        ttk.Label(status_row, textvariable=self.oled_var, foreground=DARK_FG_MUTED).pack(side="left")
        ttk.Label(status_row, text="   ").pack(side="left")
        ttk.Label(status_row, textvariable=self.hub75_var, foreground=DARK_FG_MUTED).pack(side="left")

        # OLED above the HUB75 left panel, simulated controls directly
        # above the HUB75 right panel -- same 2-column grid as the hub75
        # pair below, no extra column pushing anything further right.
        # Both HUB75 halves decode from ONE combined FRAME_DUMP (cols 0-63
        # and 64-127 of the real 2-panel horizontal chain). The oled/hub75
        # panels stretch with the window (sticky="nsew" + row/column
        # weights); each canvas recomputes its integer zoom from its own
        # current pixel size on every redraw, and a <Configure> binding
        # forces a redraw-from-cache on resize so the preview visibly
        # rescales even with no new backend dump arriving. The window's
        # aspect ratio is locked (see _lock_aspect_ratio), so interactive
        # resizing only ever scales this whole block uniformly.
        displays = ttk.Frame(preview)
        displays.grid(row=2, column=0, sticky="nsew")
        displays.columnconfigure(0, weight=1)
        displays.columnconfigure(1, weight=1)
        displays.rowconfigure(0, weight=2)
        displays.rowconfigure(1, weight=3)

        oled_frame = ttk.LabelFrame(displays, text="OLED", padding=8)
        oled_frame.grid(row=0, column=0, sticky="n", padx=(0, 6), pady=(0, 8))
        controller = ttk.LabelFrame(displays, text="Simulated IR controls", padding=10)
        controller.grid(row=0, column=1, sticky="n", padx=(6, 0), pady=(0, 8))
        hub_left_frame = ttk.LabelFrame(displays, text="HUB75 left", padding=8)
        hub_left_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        hub_right_frame = ttk.LabelFrame(displays, text="HUB75 right", padding=8)
        hub_right_frame.grid(row=1, column=1, sticky="nsew", padx=(6, 0))

        self.oled_rotate_check = ttk.Checkbutton(
            oled_frame, text="Rotate 180°",
            variable=self.oled_rotate_var, command=self._redraw_oled_from_cache)
        self.oled_rotate_check.pack(anchor="w")
        self.oled_canvas = tk.Canvas(oled_frame, width=384, height=192, bg=DARK_CANVAS_BG,
                                     highlightthickness=1, highlightbackground=DARK_BORDER)
        self.oled_canvas.pack(fill="both", expand=True)
        self.oled_canvas.bind("<Configure>", lambda _e: self._redraw_oled_from_cache())
        self.hub_left_canvas = tk.Canvas(hub_left_frame, width=576, height=288, bg=DARK_CANVAS_BG,
                                         highlightthickness=1, highlightbackground=DARK_BORDER)
        self.hub_left_canvas.pack(fill="both", expand=True)
        self.hub_left_canvas.bind("<Configure>", lambda _e: self._redraw_hub75_from_cache())
        self.hub_right_canvas = tk.Canvas(hub_right_frame, width=576, height=288, bg=DARK_CANVAS_BG,
                                          highlightthickness=1, highlightbackground=DARK_BORDER)
        self.hub_right_canvas.pack(fill="both", expand=True)
        self.hub_right_canvas.bind("<Configure>", lambda _e: self._redraw_hub75_from_cache())

        # No row/column weight on the dpad's own grid: a weighted cell
        # stretches its button to fill whatever irregular space is left,
        # which is how the buttons stopped being square. Leaving weight at
        # the default (0) keeps every button at its natural, compact,
        # roughly 1:1 size regardless of how much room the column has.
        # D-pad/Back/Aux inject one virtual IR command per click, but the
        # firmware's own button_timeout auto-releases 250ms after the last
        # injected code (input.lua's press()) -- a single click only ever
        # held the button for 250ms regardless of how long the mouse stayed
        # down. Binding press/release instead of a one-shot command and
        # re-injecting every 150ms while held keeps the firmware's timeout
        # continuously refreshed, matching a real remote's repeat pulses.
        dpad = {"▲": "B9", "◀": "BB", "OK": "BF", "▶": "BC", "▼": "EA"}
        positions = {"▲": (0, 1), "◀": (1, 0), "OK": (1, 1), "▶": (1, 2), "▼": (2, 1)}
        for label, (row, col) in positions.items():
            code = dpad[label]
            btn = ttk.Button(controller, text=label, width=3, style="Dpad.TButton")
            btn.grid(row=row, column=col, padx=2, pady=2)
            btn.bind("<ButtonPress-1>", lambda _e, code=code: self._start_button_hold(code))
            btn.bind("<ButtonRelease-1>", lambda _e: self._stop_button_hold())
        self.boop_button = ttk.Button(controller, text="Boop", width=6,
                                      command=self._toggle_boop)
        self.boop_button.grid(row=3, column=0, padx=2, pady=(8, 0))
        back_btn = ttk.Button(controller, text="Back", width=4)
        back_btn.grid(row=3, column=1, padx=2, pady=(8, 0))
        back_btn.bind("<ButtonPress-1>", lambda _e: self._start_button_hold("E6"))
        back_btn.bind("<ButtonRelease-1>", lambda _e: self._stop_button_hold())
        aux_btn = ttk.Button(controller, text="Aux", width=6)
        aux_btn.grid(row=3, column=2, padx=2, pady=(8, 0))
        aux_btn.bind("<ButtonPress-1>", lambda _e: self._start_button_hold("BA"))
        aux_btn.bind("<ButtonRelease-1>", lambda _e: self._stop_button_hold())

        lower = ttk.Frame(preview)
        lower.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        lower.columnconfigure(0, weight=1)
        lower.rowconfigure(5, weight=1)
        ttk.Label(lower, text="LED strip (buffer capture, not a verified hardware trace)",
                 foreground=DARK_FG_MUTED).grid(row=0, column=0, sticky="w")
        self.led_canvas = tk.Canvas(lower, height=28, bg=DARK_CANVAS_BG,
                                    highlightthickness=1, highlightbackground=DARK_BORDER)
        self.led_canvas.grid(row=1, column=0, sticky="ew", pady=(2, 8))
        self.led_canvas.bind("<Configure>", lambda _e: self._redraw_led_strip_from_cache())
        ttk.Label(lower, text="Microphone (host capture -> FFT level override)",
                 foreground=DARK_FG_MUTED).grid(row=2, column=0, sticky="w")
        mic_row = ttk.Frame(lower)
        mic_row.grid(row=3, column=0, sticky="ew", pady=(2, 8))
        mic_row.columnconfigure(2, weight=1)
        self.mic_button = ttk.Button(mic_row, text="Mic", width=6, command=self._toggle_mic)
        self.mic_button.grid(row=0, column=0, padx=(0, 6))
        ttk.Label(mic_row, text="Input:", foreground=DARK_FG_MUTED).grid(row=0, column=1, padx=(0, 4))
        self.mic_device_combo = ttk.Combobox(mic_row, state="readonly", width=30)
        self.mic_device_combo.grid(row=0, column=2, sticky="ew")
        self.mic_canvas = tk.Canvas(mic_row, height=20, bg=DARK_CANVAS_BG,
                                    highlightthickness=1, highlightbackground=DARK_BORDER)
        self.mic_canvas.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self.mic_canvas.bind("<Configure>", lambda _e: self._draw_vu_meter())
        ttk.Label(mic_row, textvariable=self.mic_threshold_var, foreground=DARK_FG_MUTED).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(2, 0))
        self._populate_mic_devices()
        ttk.Label(lower, textvariable=self.uart_var, foreground=DARK_FG_MUTED).grid(
            row=4, column=0, sticky="w")
        self.uart_text = tk.Text(lower, height=9, width=149, state="disabled", wrap="none",
                                 bg=DARK_CANVAS_BG, fg=DARK_FG, insertbackground=DARK_FG,
                                 relief="solid", borderwidth=1, highlightthickness=1,
                                 highlightbackground=DARK_BORDER, padx=6, pady=4)
        self.uart_text.grid(row=5, column=0, sticky="nsew")

    def _lock_aspect_ratio(self) -> None:
        # "Diagonal-only" resize: lock the window's min and max interactive
        # resize aspect ratio to the same value, so dragging any edge/corner
        # only ever scales both dimensions together, preserving every
        # panel's on-screen proportions instead of squashing/stretching one
        # axis independently.
        self.root.update_idletasks()
        width, height = self.root.winfo_width(), self.root.winfo_height()
        if width > 1 and height > 1:
            self.root.wm_aspect(width, height, width, height)

    def _redraw_hub75_from_cache(self) -> None:
        if self._last_hub_dump is not None:
            self._draw_hub75(self._last_hub_dump)

    def _on_wiring_changed(self, _event: object | None = None) -> None:
        self.status_var.set("Wiring changed. Starting a session saves it automatically.")

    def selected_wiring(self) -> dict[str, Any]:
        return selected_wiring(flip_left=self.left_flip_var.get(), flip_right=self.right_flip_var.get(),
                               color_scheme_left=self.left_color_var.get(),
                               color_scheme_right=self.right_color_var.get())

    def set_hostdir(self, value: str | pathlib.Path) -> None:
        root = pathlib.Path(value).expanduser().resolve()
        if not root.is_dir():
            raise LauncherError(f"hostdir is not a folder: {root}")
        self.hostdir = root
        self.hostdir_var.set(str(root))
        self._load_existing_wiring()
        self._load_boop_config()
        self._load_mic_config()
        self.status_var.set("Folder selected.")

    def _load_boop_config(self) -> None:
        self.boop_gpio = None
        self.boop_near = False
        if self.hostdir is None:
            return
        try:
            misc = json.loads((self.hostdir / "misc.json").read_text(encoding="utf-8"))
            boop = misc.get("boop", {})
            if boop.get("trigger_mode") == "gpio" and isinstance(boop.get("gpio"), (int, float)):
                self.boop_gpio = int(boop["gpio"])
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        if hasattr(self, "boop_button"):
            self.boop_button.configure(style="TButton")

    def _load_mic_config(self) -> None:
        if self._mic_active:
            self._stop_mic_capture()
        self.fft_enabled = False
        self.fft_band_start = 2
        self.fft_band_end = 8
        self.fft_min_energy = 50000
        self.fft_max_energy = 200000
        self.fft_frist_frame_threshold = 40000
        if self.hostdir is not None:
            try:
                misc = json.loads((self.hostdir / "misc.json").read_text(encoding="utf-8"))
                fft = misc.get("fft", {})
                if fft.get("enabled"):
                    self.fft_enabled = True
                    self.fft_band_start = int(fft.get("speech_band_start", self.fft_band_start))
                    self.fft_band_end = int(fft.get("speech_band_end", self.fft_band_end))
                    self.fft_min_energy = int(fft.get("speech_min_energy", self.fft_min_energy))
                    self.fft_max_energy = int(fft.get("speech_max_energy", self.fft_max_energy))
                    self.fft_frist_frame_threshold = int(
                        fft.get("speech_frist_frame_threshold", self.fft_frist_frame_threshold))
            except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
                pass
        if hasattr(self, "mic_threshold_var"):
            if self.fft_enabled:
                self.mic_threshold_var.set(
                    f"Activation threshold: {self.fft_frist_frame_threshold} "
                    f"(speech range {self.fft_min_energy}-{self.fft_max_energy})")
            else:
                self.mic_threshold_var.set("This SD card's misc.json does not have fft.enabled set to true.")
        if hasattr(self, "mic_canvas"):
            self._draw_vu_meter()

    def choose_folder(self) -> None:
        chosen = filedialog.askdirectory(parent=self.root, title="Choose the Protopanda SD folder",
                                         mustexist=True)
        if chosen:
            try:
                self.set_hostdir(chosen)
            except LauncherError as exc:
                messagebox.showerror("Invalid folder", str(exc), parent=self.root)

    def choose_firmware(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self.root, title="Choose a firmware image",
            filetypes=[("Firmware image", "*.bin"), ("All files", "*.*")])
        if not chosen:
            return
        path = pathlib.Path(chosen).expanduser().resolve()
        if not path.is_file():
            messagebox.showerror("Invalid firmware", f"Not a file: {path}", parent=self.root)
            return
        self.firmware_path = path
        self.firmware_var.set(str(path))
        self.status_var.set("Firmware selected.")

    def _load_existing_wiring(self) -> None:
        if self.hostdir is None:
            return
        path = self.hostdir / "animation.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            element = next((item for item in data.get("frames", []) if isinstance(item, dict)), None)
        except (OSError, json.JSONDecodeError, AttributeError):
            return
        if not element:
            return
        if isinstance(element.get("flip_left"), bool):
            self.left_flip_var.set(element["flip_left"])
        if isinstance(element.get("flip_right"), bool):
            self.right_flip_var.set(element["flip_right"])
        if element.get("color_scheme_left") in WIRE_COLOR_SCHEMES:
            self.left_color_var.set(element["color_scheme_left"])
        if element.get("color_scheme_right") in WIRE_COLOR_SCHEMES:
            self.right_color_var.set(element["color_scheme_right"])

    def _profile_destination(self) -> pathlib.Path:
        if self.hostdir is None:
            raise LauncherError("select the SD folder first")
        host_key = hashlib.sha256(str(self.hostdir).encode("utf-8")).hexdigest()[:16]
        return ROOT / ".work/mvp-profiles" / f"{host_key}.json"

    def create_profile(self) -> pathlib.Path | None:
        try:
            if self.hostdir is None:
                raise LauncherError("select the SD folder first")
            animation = self.hostdir / "animation.json"
            wiring = self.selected_wiring()
            frame_count = apply_wiring_to_animation(animation, wiring)
            profile = {
                "firmware_image": str(FIRMWARE),
                "firmware_sha256": supervisor.EXPECTED_FIRMWARE_SHA256,
                "board_profile": dict(BOARD_PROFILE),
                "log_level": "info",
                "headless": True,
                "renderer": "none",
                "host_directory": str(self.hostdir),
                "launcher_wiring": wiring,
            }
            if self.firmware_path is not None:
                # Explicit GUI pick always wins over auto-detection.
                if not self.firmware_path.is_file():
                    raise LauncherError(f"selected firmware does not exist: {self.firmware_path}")
                emu_path = self.firmware_path
            else:
                emu_firmware = os.environ.get("PROTOPANDAEMU_EMU_FIRMWARE", "").strip()
                if emu_firmware:
                    emu_path = pathlib.Path(emu_firmware).expanduser().resolve()
                    if not emu_path.is_file():
                        raise LauncherError(f"PROTOPANDAEMU_EMU_FIRMWARE does not exist: {emu_path}")
                elif EMU_FIRMWARE.is_file():
                    # Default to the user's own firmware build when present;
                    # no env var needed for the common as-is workflow.
                    emu_path = EMU_FIRMWARE
                else:
                    emu_path = None
            if emu_path is not None:
                profile["firmware_image"] = str(emu_path)
                emu_hash = supervisor._stable_sha256(emu_path, "firmware_image")
                profile["firmware_sha256"] = emu_hash
                # The supervisor only boots the pinned image unless this is
                # set; the launcher just computed the real hash above, so
                # set it here instead of requiring a manual shell export.
                os.environ["PROTOPANDAEMU_EXTRA_FIRMWARE_SHA256"] = emu_hash
                self.firmware_var.set(str(emu_path))
            else:
                self.firmware_var.set("Pinned default")
            destination = self._profile_destination()
            _write_json(destination, profile)
            profile_hash = supervisor.canonical_hash(profile)
            self.profile_path = destination
            self.profile_hash_var.set(f"Profile hash: {profile_hash}")
            self.status_var.set(f"Profile created. {frame_count} frame(s) updated.")
            return destination
        except (LauncherError, OSError, KeyError) as exc:
            messagebox.showerror("Could not create profile", str(exc), parent=self.root)
            return None

    def _warn_if_unsupported_input_mode(self) -> None:
        """Warn about input modes the emulator doesn't support."""
        if self.hostdir is None:
            return
        try:
            keybinds = json.loads((self.hostdir / "keybinds.json").read_text(encoding="utf-8"))
            mode = str(keybinds.get("input", {}).get("mode") or "BLE").upper()
        except (OSError, json.JSONDecodeError, AttributeError):
            return
        if mode == "BLE":
            messagebox.showwarning(
                "keybinds.json uses input.mode=\"BLE\"",
                "The emulator has no BLE controller. Use input.mode=\"infrared\" "
                "with a numeric infrared_gpio. With \"BLE\" the session boot-loops.",
                parent=self.root)
        elif mode == "NONE":
            messagebox.showwarning(
                "keybinds.json uses input.mode=\"none\"",
                "input.Start() only accepts \"BLE\"/\"INFRARED\"; \"none\" errors "
                "in the firmware. Use \"infrared\" with a numeric infrared_gpio.",
                parent=self.root)
        elif mode == "INFRARED":
            gpio = keybinds.get("input", {}).get("infrared_gpio")
            if not isinstance(gpio, (int, float)) or isinstance(gpio, bool):
                messagebox.showwarning(
                    "keybinds.json: infrared_gpio missing",
                    "input.mode=\"infrared\" requires a numeric infrared_gpio in "
                    "keybinds.json. Add, for example, \"infrared_gpio\": 13.",
                    parent=self.root)

    def send_virtual_button(self, code: str) -> None:
        """Inject one virtual IR command into the live guest session."""
        if self.handle is None or self.handle.wait(timeout=0):
            self.status_var.set("No active session to inject controls.")
            return
        if not self.handle.send_serial_input(f"IR:FF00:{code}\n"):
            self.status_var.set("Failed to write to the session console.")

    def _start_button_hold(self, code: str) -> None:
        """Begin repeat-injecting a virtual button while the mouse is held.

        A single injected IR command only ever holds the firmware's button
        state for 250ms (input.lua's press() refreshes button_timeout to
        now+250ms on each decoded command, same as a real remote's repeat
        pulses would); re-injecting well inside that window for as long as
        the mouse button stays down is what makes a press-and-hold actually
        register as held instead of a 250ms tap regardless of hold length.
        """
        self._stop_button_hold()
        self.send_virtual_button(code)
        self._hold_repeat_id = self.root.after(150, lambda: self._start_button_hold(code))

    def _stop_button_hold(self) -> None:
        if self._hold_repeat_id is not None:
            self.root.after_cancel(self._hold_repeat_id)
            self._hold_repeat_id = None


    def _toggle_boop(self) -> None:
        """Flip the simulated proximity sensor between far (0) and near (1).

        Overrides the configured boop GPIO pin's read value over the
        session console (`GPIO:<pin>:<0|1>`), the same injection channel
        IR uses -- the firmware's own boop.lua trigger_mode="gpio" check
        reads this override instead of the real (unconnected) hardware pin.
        """
        if self.boop_gpio is None:
            self.status_var.set("No boop GPIO configured for this SD card (trigger_mode must be \"gpio\").")
            return
        if self.handle is None or self.handle.wait(timeout=0):
            self.status_var.set("No active session to inject controls.")
            return
        self.boop_near = not self.boop_near
        state = 1 if self.boop_near else 0
        if self.handle.send_serial_input(f"GPIO:{self.boop_gpio}:{state}\n"):
            self.boop_button.configure(style="BoopActive.TButton" if self.boop_near else "TButton")
        else:
            self.boop_near = not self.boop_near  # revert, the write failed
            self.status_var.set("Failed to write to the session console.")

    # Host RMS maps to dBFS (standard VU-meter domain, perceptually linear,
    # unlike raw amplitude) clamped to [MIC_DB_FLOOR, MIC_DB_CEIL], then
    # that 0..1 fraction scales onto [0, fft_max_energy * MIC_HEADROOM] --
    # the per-band value "MIC:<n>" injects. The headroom multiplier lets a
    # louder-than-"max" voice still read as full rather than clipping the
    # meter exactly at the configured max.
    MIC_DB_FLOOR = -50.0
    MIC_DB_CEIL = 0.0
    MIC_HEADROOM = 1.2
    MIC_SEND_INTERVAL_MS = 100

    def _toggle_mic(self) -> None:
        if self._mic_active:
            self._stop_mic_capture()
        else:
            self._start_mic_capture()

    def _populate_mic_devices(self) -> None:
        self.mic_device_indices = [None]
        names = ["Default input device"]
        if _sounddevice is not None:
            try:
                for index, dev in enumerate(_sounddevice.query_devices()):
                    if dev.get("max_input_channels", 0) > 0:
                        self.mic_device_indices.append(index)
                        names.append(dev.get("name") or f"Device {index}")
            except Exception:
                pass  # leave just "Default input device"; _start_mic_capture()
                      # reports any real open failure when the user clicks Mic
        self.mic_device_combo.configure(values=names)
        self.mic_device_combo.current(0)

    def _selected_mic_device(self) -> int | None:
        choice = self.mic_device_combo.current()
        if choice < 0 or choice >= len(self.mic_device_indices):
            return None
        return self.mic_device_indices[choice]

    def _start_mic_capture(self) -> None:
        if not self.fft_enabled:
            messagebox.showerror(
                "Microphone not usable for this SD card",
                "This SD card's misc.json does not have fft.enabled set to "
                "true, so the firmware never reads FFT band energy -- "
                "there is nothing for a captured level to drive.",
                parent=self.root)
            return
        if _sounddevice is None:
            messagebox.showerror(
                "Microphone unavailable",
                "sounddevice isn't installed. Install it, then restart the launcher:\n\n"
                "  sudo apt install libportaudio2\n"
                "  pip install sounddevice",
                parent=self.root)
            return
        if self._mic_stream is not None:
            return

        def audio_callback(indata, frames, time_info, status):  # noqa: ANN001 - sounddevice callback signature
            rms = float((indata.astype("float64") ** 2).mean() ** 0.5)
            with self._mic_level_lock:
                self._mic_level = rms

        try:
            self._mic_stream = _sounddevice.InputStream(
                channels=1, samplerate=16000, blocksize=800,
                device=self._selected_mic_device(), callback=audio_callback)
            self._mic_stream.start()
        except Exception as exc:
            self._mic_stream = None
            messagebox.showerror("Could not open microphone", str(exc), parent=self.root)
            return
        self._mic_active = True
        self.mic_button.configure(style="BoopActive.TButton")
        self._mic_send_loop()

    def _stop_mic_capture(self) -> None:
        if self._mic_send_job_id is not None:
            self.root.after_cancel(self._mic_send_job_id)
            self._mic_send_job_id = None
        if self._mic_stream is not None:
            self._mic_stream.stop()
            self._mic_stream.close()
            self._mic_stream = None
        self._mic_active = False
        with self._mic_level_lock:
            self._mic_level = 0.0
        if hasattr(self, "mic_button"):
            self.mic_button.configure(style="TButton")
        self._draw_vu_meter()
        if self.handle is not None and not self.handle.wait(timeout=0):
            self.handle.send_serial_input("MIC:0\n")

    def _rms_to_level(self, rms: float) -> float:
        db = 20.0 * math.log10(max(rms, 1e-6))
        level = (db - self.MIC_DB_FLOOR) / (self.MIC_DB_CEIL - self.MIC_DB_FLOOR)
        return max(0.0, min(1.0, level))

    def _mic_send_loop(self) -> None:
        if not self._mic_active:
            return
        with self._mic_level_lock:
            rms = self._mic_level
        level = self._rms_to_level(rms)
        if self.handle is not None and not self.handle.wait(timeout=0):
            num_bands = max(1, self.fft_band_end - self.fft_band_start + 1)
            total_energy = level * self.fft_max_energy * self.MIC_HEADROOM
            per_band = int(total_energy / num_bands)
            self.handle.send_serial_input(f"MIC:{per_band}\n")
        self._draw_vu_meter(level)
        self._mic_send_job_id = self.root.after(self.MIC_SEND_INTERVAL_MS, self._mic_send_loop)

    def _mic_threshold_fraction(self) -> float:
        """Where the activation threshold (speech_frist_frame_threshold)
        sits in the meter's 0..1 display domain, given the same headroom
        scaling _mic_send_loop() uses to turn a level into a per-band
        energy value. Kept separate from _draw_vu_meter so the mapping is
        testable without real Tk window geometry."""
        headroom_energy = self.fft_max_energy * self.MIC_HEADROOM
        if headroom_energy <= 0:
            return 0.0
        return max(0.0, min(1.0, self.fft_frist_frame_threshold / headroom_energy))

    def _draw_vu_meter(self, level: float = 0.0) -> None:
        canvas = self.mic_canvas
        canvas.delete("all")
        w = max(1, canvas.winfo_width())
        h = max(1, canvas.winfo_height())
        fill_w = int(w * max(0.0, min(1.0, level)))
        if fill_w > 0:
            color = DARK_RED if level >= 0.95 else DARK_ACCENT
            canvas.create_rectangle(0, 0, fill_w, h, fill=color, outline="")
        mx = int(w * self._mic_threshold_fraction())
        canvas.create_line(mx, 0, mx, h, fill=DARK_FG, width=2)

    def start_session(self) -> None:
        if self.handle is not None and not self.handle.wait(timeout=0):
            self.status_var.set("Session is already running.")
            return
        self._warn_if_unsupported_input_mode()
        profile = self.create_profile()
        if profile is None or self.hostdir is None:
            return
        try:
            self.handle = supervisor.run_session(profile, hostdir=self.hostdir, timeout=self.timeout)
        except supervisor.ProfileError as exc:
            report = exc.report()
            self.last_report = report
            self.status_var.set(f"Session rejected: {report.get('error_code')}: {report.get('error')}")
            messagebox.showerror("Session not started", self.status_var.get(), parent=self.root)
            return
        report = self.handle.session_report()
        self.last_report = report
        self.session_var.set(f"Session: {report['session_id']}\nProfile hash: {report['profile_hash']}")
        self.status_var.set("Session started. Waiting for backend dumps.")
        self.boop_near = False
        # Sending the override now would race the guest: the console
        # parser only reads Serial once loop() is running, and bytes
        # arriving during the ROM bootloader/setup() window can be lost.
        # refresh() sends it once the "vfs_init" marker (a generic,
        # SD-card-content-independent boot marker) confirms the guest
        # loop is actually consuming console input.
        self._boop_default_sent = self.boop_gpio is None
        if hasattr(self, "boop_button"):
            self.boop_button.configure(style="TButton")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

    def stop_session(self) -> dict[str, Any] | None:
        if self.handle is None:
            return self.last_report
        try:
            report = self.handle.stop()
            self.last_report = report
            self.session_var.set(f"Session: {report.get('session_id')}\nProfile hash: {report.get('profile_hash')}")
            self.status_var.set(f"Session finished: {report.get('exit_reason')}")
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            return report
        except Exception as exc:  # keep the dashboard alive for a reportable backend failure
            self.status_var.set(f"Error stopping session: {exc}")
            return None

    def _schedule_refresh(self) -> None:
        self._after_id = self.root.after(50, self.refresh)

    def refresh(self) -> None:
        # Reschedule first: nothing below may kill the refresh loop, or the
        # dashboard freezes permanently (truncated dumps in the stderr tail
        # used to raise inside the decoders and stop the after() chain).
        self._schedule_refresh()
        if self.handle is None:
            return
        if not self._boop_default_sent and self.handle.wait_for_marker("vfs_init", timeout=0):
            if self.handle.send_serial_input(f"GPIO:{self.boop_gpio}:0\n"):
                self._boop_default_sent = True
        try:
            report = self.handle.session_report()
            self.last_report = report
            resources = report.get("resources") or {}
            self.resource_var.set("Resources: " + ", ".join(f"{key}={value}" for key, value in resources.items()))
            stderr = report.get("stderr_log_path")
            if stderr:
                oled, hub = _read_latest_dumps(pathlib.Path(stderr))
                if oled:
                    self._last_oled_dump = oled
                    self._draw_oled(oled)
                    self.oled_var.set(f"OLED: seq={oled['seq']} {oled['width']}x{oled['height']}, {oled['bytes']} bytes")
                if hub:
                    self._last_hub_dump = hub
                    self._draw_hub75(hub)
                    note = " (black)" if not any(hub["data"]) else ""
                    self.hub75_var.set(f"HUB75: seq={hub['seq']} {hub['width']}x{hub['height']} "
                                       f"({hub['bytes']} bytes, {hub['descriptors']} descriptors){note}")
            uart = report.get("uart_log_path")
            if uart:
                uart_path = pathlib.Path(uart)
                self._set_uart_tail(uart_path)
                led = _read_latest_led_dump(uart_path)
                if led:
                    self._last_led_dump = led
                    self._draw_led_strip(led)
                brightness = _read_latest_hub75_brightness(uart_path)
                if brightness is not None and brightness != self.hub75_brightness:
                    self.hub75_brightness = brightness
                    self._redraw_hub75_from_cache()
            if report.get("exit_reason") and self.handle is not None:
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
        except Exception as exc:  # keep the dashboard alive for a reportable backend failure
            self.status_var.set(f"Preview error: {exc}")

    def _set_uart_tail(self, path: pathlib.Path) -> None:
        try:
            data = path.read_text(encoding="utf-8", errors="replace")[-8000:]
        except OSError:
            return
        self.uart_var.set(f"UART: {path}")
        self.uart_text.configure(state="normal")
        self.uart_text.delete("1.0", "end")
        self.uart_text.insert("1.0", data)
        self.uart_text.see("end")
        self.uart_text.configure(state="disabled")

    def _redraw_oled_from_cache(self) -> None:
        if self._last_oled_dump is not None:
            self._draw_oled(self._last_oled_dump)

    def _draw_oled(self, dump: dict[str, Any]) -> None:
        canvas = self.oled_canvas
        width, height, data = dump["width"], dump["height"], dump["data"]
        if len(data) != width * ((height + 7) // 8):
            return
        on, off = "#ffffff", DARK_CANVAS_BG
        rows = []
        for y in range(height):
            row_bytes = data[(y // 8) * width:(y // 8) * width + width]
            bit = 1 << (y & 7)
            rows.append(" ".join(on if byte & bit else off for byte in row_bytes))
        if self.oled_rotate_var.get():
            # Cosmetic only: the firmware's OLED_SCREEN_ROTATION=2 is baked
            # into the real GDDRAM bytes this reads (see ISSUES.md); this
            # flips the VIEW to match a physically-compensated enclosure,
            # it never changes the reported seq/width/height/bytes data.
            rows = [" ".join(reversed(row.split(" "))) for row in reversed(rows)]
        photo = tk.PhotoImage(width=width, height=height)
        photo.put(" ".join(f"{{{row}}}" for row in rows))
        scale = max(1, min(canvas.winfo_width() // width, canvas.winfo_height() // height))
        self._oled_photo = photo.zoom(scale, scale)
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=self._oled_photo)

    def _draw_hub75(self, dump: dict[str, Any]) -> None:
        # The real 2-panel horizontal chain packs both eyes into one
        # continuous frame (cols 0..half-1 = left panel, half..width-1 =
        # right panel, per hardware.json's horizontal_panel_count=2). Split
        # it here so each half renders in its own physically-positioned
        # canvas instead of one combined strip.
        width, height, data = dump["width"], dump["height"], dump["data"]
        if len(data) != width * height * 3:
            return
        half = width // 2
        self._draw_hub75_half(self.hub_left_canvas, data, width, height, 0, half, "left")
        self._draw_hub75_half(self.hub_right_canvas, data, width, height, half, width, "right")

    def _draw_hub75_half(self, canvas: tk.Canvas, data: bytes, full_width: int, height: int,
                         x0: int, x1: int, side: str) -> None:
        # Scale to the real configured panel brightness (see
        # HUB75_BRIGHTNESS_RE): the decoded color data itself is always
        # full/undimmed, since brightness is a separate OE/blanking signal
        # this backend's decoder can't see.
        br = self.hub75_brightness
        rows = []
        for y in range(height):
            base = y * full_width * 3
            pixels = (
                f"#{data[base + i * 3] * br // 255:02x}"
                f"{data[base + i * 3 + 1] * br // 255:02x}"
                f"{data[base + i * 3 + 2] * br // 255:02x}"
                for i in range(x0, x1))
            rows.append(" ".join(pixels))
        w = x1 - x0
        photo = tk.PhotoImage(width=w, height=height)
        photo.put(" ".join(f"{{{row}}}" for row in rows))
        scale = max(1, min(canvas.winfo_width() // w, canvas.winfo_height() // height))
        zoomed = photo.zoom(scale, scale)
        if side == "left":
            self._hub75_left_photo = zoomed
        else:
            self._hub75_right_photo = zoomed
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=zoomed)

    def _redraw_led_strip_from_cache(self) -> None:
        if self._last_led_dump is not None:
            self._draw_led_strip(self._last_led_dump)

    def _draw_led_strip(self, dump: dict[str, Any]) -> None:
        canvas = self.led_canvas
        count, data = dump["count"], dump["data"]
        if count <= 0 or len(data) != count * 3:
            return
        row = " ".join(
            f"#{data[i * 3]:02x}{data[i * 3 + 1]:02x}{data[i * 3 + 2]:02x}" for i in range(count))
        photo = tk.PhotoImage(width=count, height=1)
        photo.put(f"{{{row}}}")
        canvas_width = max(1, canvas.winfo_width())
        canvas_height = max(1, canvas.winfo_height())
        scale_x = max(1, canvas_width // count)
        self._led_photo = photo.zoom(scale_x, canvas_height)
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=self._led_photo)

    def close(self) -> None:
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
        self._stop_button_hold()
        if self._mic_active:
            self._stop_mic_capture()
        if self.handle is not None and self.handle.session_report().get("exit_reason") is None:
            self.stop_session()
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Desktop launcher for Protopanda sessions")
    parser.add_argument("--hostdir", type=pathlib.Path, help="start with this firmware SD folder")
    # The emu's first boot composes frames.bulk on FFAT via TCG (~100s of
    # wall time); 30s used to kill the session mid-compose (~70% progress).
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--geometry", help="Tk geometry, e.g. 1560x900+4020+292 (multi-monitor placement)")
    args = parser.parse_args(argv)
    root = tk.Tk()
    app = LauncherApp(root, initial_hostdir=args.hostdir, timeout=args.timeout)
    if args.geometry:
        root.geometry(args.geometry)
    root.protocol("WM_DELETE_WINDOW", app.close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
