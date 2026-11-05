"""Minimal Tkinter desktop launcher for the real Proto-panda session API.

The launcher deliberately stays thin: it edits the upstream animation frame
fields, writes a machine profile, and calls ``protopandaemu.supervisor`` in
process.  Display previews are decoded only from the QEMU device reports
captured by the supervisor; there is no synthetic rendering path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

from . import supervisor


ROOT = pathlib.Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "artifacts/build-normal/release/merged_flash.bin"
WIRE_COLOR_SCHEMES = ("rgb", "rbg", "grb", "gbr", "brg", "bgr")
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

    def decode_oled(match: re.Match[str]) -> dict[str, Any]:
        seq, width, height, byte_count, hex_data = match.groups()
        return {"seq": int(seq), "width": int(width), "height": int(height),
                "bytes": int(byte_count), "data": bytes.fromhex(hex_data)}

    def decode_hub(match: re.Match[str]) -> dict[str, Any]:
        seq, width, height, rows, byte_count, descriptors, hex_data = match.groups()
        return {"seq": int(seq), "width": int(width), "height": int(height),
                "rows_per_frame": int(rows), "bytes": int(byte_count),
                "descriptors": int(descriptors), "data": bytes.fromhex(hex_data)}

    return (decode_oled(oled_matches[-1]) if oled_matches else None,
            decode_hub(hub_matches[-1]) if hub_matches else None)


class LauncherApp:
    """The small, testable desktop dashboard."""

    def __init__(self, root: tk.Misc, *, initial_hostdir: str | pathlib.Path | None = None,
                 timeout: float = 30.0):
        self.root = root
        self.timeout = timeout
        self.hostdir: pathlib.Path | None = None
        self.profile_path: pathlib.Path | None = None
        self.handle: supervisor.SessionHandle | None = None
        self.last_report: dict[str, Any] | None = None
        self._after_id: str | None = None

        self.hostdir_var = tk.StringVar(value="Nenhuma pasta selecionada")
        self.profile_hash_var = tk.StringVar(value="Perfil: —")
        self.session_var = tk.StringVar(value="Sessão: —")
        self.status_var = tk.StringVar(value="Pronto: selecione a pasta SD do firmware.")
        self.resource_var = tk.StringVar(value="Recursos: —")
        self.oled_var = tk.StringVar(value="OLED: aguardando dump GDDRAM real")
        self.hub75_var = tk.StringVar(value="HUB75: aguardando dump FRAME real")
        self.uart_var = tk.StringVar(value="UART: —")
        self.left_flip_var = tk.BooleanVar(value=True)
        self.right_flip_var = tk.BooleanVar(value=False)
        self.left_color_var = tk.StringVar(value="rgb")
        self.right_color_var = tk.StringVar(value="rgb")

        self._build_widgets()
        if initial_hostdir:
            self.set_hostdir(initial_hostdir)
        self._schedule_refresh()

    def _build_widgets(self) -> None:
        self.root.title("Proto-panda Emulator — M5 Launcher")
        self.root.minsize(1120, 760)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        controls = ttk.Frame(self.root, padding=12)
        controls.grid(row=0, column=0, sticky="nsew")
        controls.columnconfigure(0, weight=1)
        preview = ttk.Frame(self.root, padding=(0, 12, 12, 12))
        preview.grid(row=0, column=1, sticky="nsew")
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(2, weight=1)

        ttk.Label(controls, text="Proto-panda M5", font=("TkDefaultFont", 16, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 12))
        ttk.Label(controls, text="Hostdir / cartão SD").grid(row=1, column=0, sticky="w")
        ttk.Label(controls, textvariable=self.hostdir_var, wraplength=360).grid(
            row=2, column=0, sticky="w", pady=(2, 4))
        ttk.Button(controls, text="Escolher pasta…", command=self.choose_folder).grid(
            row=3, column=0, sticky="ew", pady=(0, 12))

        wiring = ttk.LabelFrame(controls, text="Wiring por metade — campos reais de animation.json", padding=8)
        wiring.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        wiring.columnconfigure(1, weight=1)
        ttk.Label(wiring, text="Metade esquerda", font=("TkDefaultFont", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w")
        self.left_flip_check = ttk.Checkbutton(wiring, text="flip_left", variable=self.left_flip_var,
                                               command=self._on_wiring_changed)
        self.left_flip_check.grid(row=1, column=0, sticky="w")
        self.left_color_combo = ttk.Combobox(wiring, textvariable=self.left_color_var,
                                             values=WIRE_COLOR_SCHEMES, state="readonly", width=9)
        self.left_color_combo.grid(row=1, column=1, sticky="ew", padx=(8, 0))
        self.left_color_combo.bind("<<ComboboxSelected>>", self._on_wiring_changed)
        ttk.Label(wiring, text="color_scheme_left").grid(row=2, column=1, sticky="w", padx=(8, 0))

        ttk.Separator(wiring).grid(row=3, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Label(wiring, text="Metade direita", font=("TkDefaultFont", 10, "bold")).grid(
            row=4, column=0, columnspan=2, sticky="w")
        self.right_flip_check = ttk.Checkbutton(wiring, text="flip_right", variable=self.right_flip_var,
                                                command=self._on_wiring_changed)
        self.right_flip_check.grid(row=5, column=0, sticky="w")
        self.right_color_combo = ttk.Combobox(wiring, textvariable=self.right_color_var,
                                              values=WIRE_COLOR_SCHEMES, state="readonly", width=9)
        self.right_color_combo.grid(row=5, column=1, sticky="ew", padx=(8, 0))
        self.right_color_combo.bind("<<ComboboxSelected>>", self._on_wiring_changed)
        ttk.Label(wiring, text="color_scheme_right").grid(row=6, column=1, sticky="w", padx=(8, 0))

        self.create_profile_button = ttk.Button(controls, text="Criar / atualizar perfil",
                                                command=self.create_profile)
        self.create_profile_button.grid(row=5, column=0, sticky="ew", pady=(0, 6))
        self.start_button = ttk.Button(controls, text="Iniciar sessão real", command=self.start_session)
        self.start_button.grid(row=6, column=0, sticky="ew", pady=3)
        self.stop_button = ttk.Button(controls, text="Parar sessão", command=self.stop_session, state="disabled")
        self.stop_button.grid(row=7, column=0, sticky="ew", pady=3)
        ttk.Label(controls, textvariable=self.status_var, wraplength=360).grid(
            row=8, column=0, sticky="w", pady=(12, 4))
        ttk.Label(controls, textvariable=self.profile_hash_var, wraplength=360).grid(
            row=9, column=0, sticky="w", pady=2)
        ttk.Label(controls, textvariable=self.session_var, wraplength=360).grid(
            row=10, column=0, sticky="w", pady=2)
        ttk.Label(controls, textvariable=self.resource_var, wraplength=360).grid(
            row=11, column=0, sticky="w", pady=2)

        ttk.Label(preview, text="Preview real do backend", font=("TkDefaultFont", 14, "bold")).grid(
            row=0, column=0, sticky="w")
        ttk.Label(preview, textvariable=self.hub75_var, wraplength=760).grid(
            row=1, column=0, sticky="w", pady=(2, 6))
        displays = ttk.Frame(preview)
        displays.grid(row=2, column=0, sticky="nsew")
        displays.columnconfigure(0, weight=1)
        displays.columnconfigure(1, weight=1)
        displays.rowconfigure(0, weight=1)
        oled_frame = ttk.LabelFrame(displays, text="OLED — GDDRAM 1bpp", padding=8)
        oled_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        hub_frame = ttk.LabelFrame(displays, text="HUB75 — RGB888", padding=8)
        hub_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.oled_canvas = tk.Canvas(oled_frame, width=384, height=192, bg="#202020",
                                     highlightthickness=1, highlightbackground="#808080")
        self.oled_canvas.pack(fill="both", expand=True)
        self.hub_canvas = tk.Canvas(hub_frame, width=384, height=192, bg="#202020",
                                    highlightthickness=1, highlightbackground="#808080")
        self.hub_canvas.pack(fill="both", expand=True)

        lower = ttk.Frame(preview)
        lower.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        lower.columnconfigure(0, weight=1)
        lower.rowconfigure(1, weight=1)
        ttk.Label(lower, textvariable=self.uart_var).grid(row=0, column=0, sticky="w")
        self.uart_text = tk.Text(lower, height=9, width=90, state="disabled", wrap="none")
        self.uart_text.grid(row=1, column=0, sticky="nsew")

    def _on_wiring_changed(self, _event: object | None = None) -> None:
        self.status_var.set("Wiring alterado: clique em ‘Criar / atualizar perfil’ para gravar animation.json.")

    def selected_wiring(self) -> dict[str, Any]:
        return selected_wiring(flip_left=self.left_flip_var.get(), flip_right=self.right_flip_var.get(),
                               color_scheme_left=self.left_color_var.get(),
                               color_scheme_right=self.right_color_var.get())

    def set_hostdir(self, value: str | pathlib.Path) -> None:
        root = pathlib.Path(value).expanduser().resolve()
        if not root.is_dir():
            raise LauncherError(f"hostdir não é uma pasta: {root}")
        self.hostdir = root
        self.hostdir_var.set(str(root))
        self._load_existing_wiring()
        self.status_var.set("Pasta selecionada. Confira as duas metades e crie o perfil.")

    def choose_folder(self) -> None:
        chosen = filedialog.askdirectory(parent=self.root, title="Escolha a pasta SD do Proto-panda",
                                         mustexist=True)
        if chosen:
            try:
                self.set_hostdir(chosen)
            except LauncherError as exc:
                messagebox.showerror("Pasta inválida", str(exc), parent=self.root)

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
            raise LauncherError("selecione primeiro a pasta SD")
        host_key = hashlib.sha256(str(self.hostdir).encode("utf-8")).hexdigest()[:16]
        return ROOT / ".work/mvp-profiles" / f"{host_key}.json"

    def create_profile(self) -> pathlib.Path | None:
        try:
            if self.hostdir is None:
                raise LauncherError("selecione primeiro a pasta SD")
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
            destination = self._profile_destination()
            _write_json(destination, profile)
            profile_hash = supervisor.canonical_hash(profile)
            self.profile_path = destination
            self.profile_hash_var.set(f"Perfil hash: {profile_hash}")
            self.status_var.set(f"Perfil criado; {frame_count} frame(s) receberam os quatro campos reais.")
            return destination
        except (LauncherError, OSError, KeyError) as exc:
            messagebox.showerror("Não foi possível criar o perfil", str(exc), parent=self.root)
            return None

    def _warn_if_unsupported_input_mode(self) -> None:
        """keybinds.json's 'BLE' input mode reaches startBLE()/startBLERadio(),
        which this experimental QEMU fork has no BLE controller model for; the
        real pinned firmware hits a Guru Meditation assert and reboots forever
        (confirmed: reproducible on every boot). Warn instead of silently
        bootlooping the user's real SD card config."""
        if self.hostdir is None:
            return
        try:
            keybinds = json.loads((self.hostdir / "keybinds.json").read_text(encoding="utf-8"))
            mode = str(keybinds.get("input", {}).get("mode", "")).upper()
        except (OSError, json.JSONDecodeError, AttributeError):
            return
        if mode == "BLE":
            messagebox.showwarning(
                "keybinds.json usa input mode BLE",
                "Este cartão usa input.mode=\"BLE\". O backend QEMU experimental não "
                "emula um controlador BLE: o firmware real trava com um assert ao "
                "chamar startBLE() e reinicia em loop infinito (reproduzido e "
                "confirmado). Para testar neste emulador, mude input.mode para "
                "\"none\" ou \"infrared\" no keybinds.json. A sessão vai iniciar "
                "mesmo assim, mas provavelmente vai bootloopar.",
                parent=self.root)

    def start_session(self) -> None:
        if self.handle is not None and not self.handle.wait(timeout=0):
            self.status_var.set("A sessão já está em execução.")
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
            self.status_var.set(f"Sessão rejeitada: {report.get('error_code')}: {report.get('error')}")
            messagebox.showerror("Sessão não iniciada", self.status_var.get(), parent=self.root)
            return
        report = self.handle.session_report()
        self.last_report = report
        self.session_var.set(f"Sessão: {report['session_id']}\nPerfil hash: {report['profile_hash']}")
        self.status_var.set("Sessão real iniciada pelo supervisor importado; aguardando dumps do backend.")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

    def stop_session(self) -> dict[str, Any] | None:
        if self.handle is None:
            return self.last_report
        try:
            report = self.handle.stop()
            self.last_report = report
            self.session_var.set(f"Sessão: {report.get('session_id')}\nPerfil hash: {report.get('profile_hash')}")
            self.status_var.set(f"Sessão finalizada: {report.get('exit_reason')}")
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            return report
        except Exception as exc:  # keep the dashboard alive for a reportable backend failure
            self.status_var.set(f"Erro ao parar sessão: {exc}")
            return None

    def _schedule_refresh(self) -> None:
        self._after_id = self.root.after(500, self.refresh)

    def refresh(self) -> None:
        if self.handle is not None:
            report = self.handle.session_report()
            self.last_report = report
            resources = report.get("resources") or {}
            self.resource_var.set("Recursos: " + ", ".join(f"{key}={value}" for key, value in resources.items()))
            stderr = report.get("stderr_log_path")
            if stderr:
                oled, hub = _read_latest_dumps(pathlib.Path(stderr))
                if oled:
                    self._draw_oled(oled)
                    self.oled_var.set(f"OLED: dump real seq={oled['seq']} {oled['width']}x{oled['height']}, {oled['bytes']} bytes")
                if hub:
                    self._draw_hub75(hub)
                    black = not any(hub["data"])
                    note = "all-black clearScreen state observed in M4" if black else "real RGB888 dump"
                    self.hub75_var.set(f"HUB75: dump real seq={hub['seq']} {hub['width']}x{hub['height']} "
                                       f"({hub['bytes']} bytes, {hub['descriptors']} descriptors); {note}")
            uart = report.get("uart_log_path")
            if uart:
                self._set_uart_tail(pathlib.Path(uart))
            if report.get("exit_reason") and self.handle is not None:
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
        self._schedule_refresh()

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

    def _draw_oled(self, dump: dict[str, Any]) -> None:
        canvas = self.oled_canvas
        canvas.delete("all")
        width, height, data = dump["width"], dump["height"], dump["data"]
        if len(data) != width * ((height + 7) // 8):
            return
        scale = max(1, min(canvas.winfo_width() // width, canvas.winfo_height() // height))
        for y in range(height):
            for x in range(width):
                on = bool(data[(y // 8) * width + x] & (1 << (y & 7)))
                if on:
                    canvas.create_rectangle(x * scale, y * scale, (x + 1) * scale, (y + 1) * scale,
                                            fill="#9cff57", outline="")

    def _draw_hub75(self, dump: dict[str, Any]) -> None:
        canvas = self.hub_canvas
        canvas.delete("all")
        width, height, data = dump["width"], dump["height"], dump["data"]
        if len(data) != width * height * 3:
            return
        scale = max(1, min(canvas.winfo_width() // width, canvas.winfo_height() // height))
        for index in range(width * height):
            red, green, blue = data[index * 3:index * 3 + 3]
            if red or green or blue:
                x, y = index % width, index // width
                canvas.create_rectangle(x * scale, y * scale, (x + 1) * scale, (y + 1) * scale,
                                        fill=f"#{red:02x}{green:02x}{blue:02x}", outline="")

    def close(self) -> None:
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
        if self.handle is not None and self.handle.session_report().get("exit_reason") is None:
            self.stop_session()
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tkinter desktop launcher for Proto-panda sessions")
    parser.add_argument("--hostdir", type=pathlib.Path, help="start with this firmware SD folder")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    root = tk.Tk()
    app = LauncherApp(root, initial_hostdir=args.hostdir, timeout=args.timeout)
    root.protocol("WM_DELETE_WINDOW", app.close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
