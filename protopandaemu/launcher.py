"""Launcher desktop Tkinter para a API de sessão do Protopanda.

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
# Built by scripts/build-firmware-emu.sh from the user's own firmware fork
# with -DPROTOPANDAEMU_EMU=1; used automatically when present so the common
# "run my own firmware + SD card as-is" workflow needs no env vars.
EMU_FIRMWARE = ROOT / "artifacts/build-emu/release/merged_flash.bin"
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
        self._oled_photo: tk.PhotoImage | None = None
        self._hub75_photo: tk.PhotoImage | None = None

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
        self.oled_rotate_var = tk.BooleanVar(value=False)
        self._last_oled_dump: dict[str, Any] | None = None

        self._build_widgets()
        if initial_hostdir:
            self.set_hostdir(initial_hostdir)
        self._schedule_refresh()

    def _build_widgets(self) -> None:
        self.root.title("Protopanda Emulator")
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

        ttk.Label(controls, text="Protopanda", font=("TkDefaultFont", 16, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 12))
        ttk.Label(controls, text="Cartão SD").grid(row=1, column=0, sticky="w")
        ttk.Label(controls, textvariable=self.hostdir_var, wraplength=360).grid(
            row=2, column=0, sticky="w", pady=(2, 4))
        ttk.Button(controls, text="Escolher pasta…", command=self.choose_folder).grid(
            row=3, column=0, sticky="ew", pady=(0, 12))

        wiring = ttk.LabelFrame(controls, text="Wiring por metade (animation.json)", padding=8)
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
        self.start_button = ttk.Button(controls, text="Iniciar sessão", command=self.start_session)
        self.start_button.grid(row=6, column=0, sticky="ew", pady=3)
        self.stop_button = ttk.Button(controls, text="Parar sessão", command=self.stop_session, state="disabled")
        self.stop_button.grid(row=7, column=0, sticky="ew", pady=3)
        controller = ttk.LabelFrame(controls, text="Controles virtuais (injeta IR na sessão)")
        controller.grid(row=8, column=0, sticky="ew", pady=(8, 4))
        for column, (label, code) in enumerate(
                [("▲", "B9"), ("▼", "EA"), ("◀", "BB"), ("▶", "BC")]):
            ttk.Button(controller, text=label, width=4,
                       command=lambda code=code: self.send_virtual_button(code)).grid(
                row=0, column=column, padx=2)
        for column, (label, code) in enumerate(
                [("OK", "BF"), ("Voltar", "E6"), ("Aux", "BA")]):
            ttk.Button(controller, text=label,
                       command=lambda code=code: self.send_virtual_button(code)).grid(
                row=1, column=column, padx=2, pady=(2, 0))
        ttk.Label(controls, textvariable=self.status_var, wraplength=360).grid(
            row=9, column=0, sticky="w", pady=(12, 4))
        ttk.Label(controls, textvariable=self.profile_hash_var, wraplength=360).grid(
            row=10, column=0, sticky="w", pady=2)
        ttk.Label(controls, textvariable=self.session_var, wraplength=360).grid(
            row=11, column=0, sticky="w", pady=2)
        ttk.Label(controls, textvariable=self.resource_var, wraplength=360).grid(
            row=12, column=0, sticky="w", pady=2)

        ttk.Label(preview, text="Preview do backend", font=("TkDefaultFont", 14, "bold")).grid(
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
        self.oled_rotate_check = ttk.Checkbutton(
            oled_frame, text="Girar 180° (só visual)",
            variable=self.oled_rotate_var, command=self._redraw_oled_from_cache)
        self.oled_rotate_check.pack(anchor="w")
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
        self.status_var.set("Wiring alterado. Clique em 'Criar / atualizar perfil' para gravar.")

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
        self.status_var.set("Pasta selecionada. Crie o perfil.")

    def choose_folder(self) -> None:
        chosen = filedialog.askdirectory(parent=self.root, title="Escolha a pasta SD do Protopanda",
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
            emu_firmware = os.environ.get("PROTOPANDAEMU_EMU_FIRMWARE", "").strip()
            if emu_firmware:
                emu_path = pathlib.Path(emu_firmware).expanduser().resolve()
                if not emu_path.is_file():
                    raise LauncherError(f"PROTOPANDAEMU_EMU_FIRMWARE não existe: {emu_path}")
            elif EMU_FIRMWARE.is_file():
                # Default to the user's own firmware build when present; no
                # env var needed for the common as-is workflow.
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
            destination = self._profile_destination()
            _write_json(destination, profile)
            profile_hash = supervisor.canonical_hash(profile)
            self.profile_path = destination
            self.profile_hash_var.set(f"Perfil hash: {profile_hash}")
            self.status_var.set(f"Perfil criado. {frame_count} frame(s) atualizados.")
            return destination
        except (LauncherError, OSError, KeyError) as exc:
            messagebox.showerror("Não foi possível criar o perfil", str(exc), parent=self.root)
            return None

    def _warn_if_unsupported_input_mode(self) -> None:
        """Avisa sobre modos de input que o emulador não suporta."""
        if self.hostdir is None:
            return
        try:
            keybinds = json.loads((self.hostdir / "keybinds.json").read_text(encoding="utf-8"))
            mode = str(keybinds.get("input", {}).get("mode") or "BLE").upper()
        except (OSError, json.JSONDecodeError, AttributeError):
            return
        if mode == "BLE":
            messagebox.showwarning(
                "keybinds.json usa input.mode=\"BLE\"",
                "O emulador não tem controlador BLE. Use input.mode=\"infrared\" "
                "com infrared_gpio numérico. Com \"BLE\" a sessão bootloopa.",
                parent=self.root)
        elif mode == "NONE":
            messagebox.showwarning(
                "keybinds.json usa input.mode=\"none\"",
                "input.Start() só aceita \"BLE\"/\"INFRARED\"; \"none\" dá erro "
                "no firmware. Use \"infrared\" com infrared_gpio numérico.",
                parent=self.root)
        elif mode == "INFRARED":
            gpio = keybinds.get("input", {}).get("infrared_gpio")
            if not isinstance(gpio, (int, float)) or isinstance(gpio, bool):
                messagebox.showwarning(
                    "keybinds.json: infrared_gpio ausente",
                    "input.mode=\"infrared\" exige infrared_gpio numérico no "
                    "keybinds.json. Adicione por exemplo \"infrared_gpio\": 13.",
                    parent=self.root)

    def send_virtual_button(self, code: str) -> None:
        """Inject one virtual IR command into the live guest session."""
        if self.handle is None or self.handle.wait(timeout=0):
            self.status_var.set("Nenhuma sessão ativa para injetar controles.")
            return
        if not self.handle.send_serial_input(f"IR:FF00:{code}\n"):
            self.status_var.set("Falha ao escrever no console da sessão.")

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
        self.status_var.set("Sessão iniciada. Aguardando dumps do backend.")
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
        self._after_id = self.root.after(100, self.refresh)

    def refresh(self) -> None:
        # Reschedule first: nothing below may kill the refresh loop, or the
        # dashboard freezes permanently (truncated dumps in the stderr tail
        # used to raise inside the decoders and stop the after() chain).
        self._schedule_refresh()
        if self.handle is None:
            return
        try:
            report = self.handle.session_report()
            self.last_report = report
            resources = report.get("resources") or {}
            self.resource_var.set("Recursos: " + ", ".join(f"{key}={value}" for key, value in resources.items()))
            stderr = report.get("stderr_log_path")
            if stderr:
                oled, hub = _read_latest_dumps(pathlib.Path(stderr))
                if oled:
                    self._last_oled_dump = oled
                    self._draw_oled(oled)
                    self.oled_var.set(f"OLED: seq={oled['seq']} {oled['width']}x{oled['height']}, {oled['bytes']} bytes")
                if hub:
                    self._draw_hub75(hub)
                    note = " (preto)" if not any(hub["data"]) else ""
                    self.hub75_var.set(f"HUB75: seq={hub['seq']} {hub['width']}x{hub['height']} "
                                       f"({hub['bytes']} bytes, {hub['descriptors']} descriptors){note}")
            uart = report.get("uart_log_path")
            if uart:
                self._set_uart_tail(pathlib.Path(uart))
            if report.get("exit_reason") and self.handle is not None:
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
        except Exception as exc:  # keep the dashboard alive for a reportable backend failure
            self.status_var.set(f"Erro no preview: {exc}")

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
        on, off = "#ffffff", "#202020"
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
        canvas = self.hub_canvas
        width, height, data = dump["width"], dump["height"], dump["data"]
        if len(data) != width * height * 3:
            return
        rows = []
        for y in range(height):
            base = y * width * 3
            pixels = (f"#{data[base + i * 3]:02x}{data[base + i * 3 + 1]:02x}{data[base + i * 3 + 2]:02x}"
                     for i in range(width))
            rows.append(" ".join(pixels))
        photo = tk.PhotoImage(width=width, height=height)
        photo.put(" ".join(f"{{{row}}}" for row in rows))
        scale = max(1, min(canvas.winfo_width() // width, canvas.winfo_height() // height))
        self._hub75_photo = photo.zoom(scale, scale)
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=self._hub75_photo)

    def close(self) -> None:
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
        if self.handle is not None and self.handle.session_report().get("exit_reason") is None:
            self.stop_session()
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launcher desktop para sessões Protopanda")
    parser.add_argument("--hostdir", type=pathlib.Path, help="start with this firmware SD folder")
    # Primeiro boot do emu compõe o frames.bulk no FFAT via TCG (~100 s de
    # wall); 30 s matava a sessão no meio do compose (~70% da barra).
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(argv)
    root = tk.Tk()
    app = LauncherApp(root, initial_hostdir=args.hostdir, timeout=args.timeout)
    root.protocol("WM_DELETE_WINDOW", app.close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
