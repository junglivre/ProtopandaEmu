#!/usr/bin/env python3
"""Rock 4/M5 automated proof: real Tk callbacks plus a real supervisor session.

Run this under a real X display, for example the documented local Xvfb command.
The test does not replace ``run_session``: it creates a temporary real hostdir,
drives the launcher's bound widgets, boots the pinned backend and cleanly stops
it through the GUI's Stop callback.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import struct
import sys
import tempfile
import time
import tkinter as tk
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from protopandaemu import launcher  # noqa: E402


REQUIRED_REPORT_FIELDS = {
    "schema", "session_id", "firmware_sha256", "profile_hash", "started_at_utc",
    "finished_at_utc", "wall_clock_ms", "exit_reason", "qemu_started", "markers",
    "resources", "flash_state_path", "flash_initial_sha256", "flash_final_sha256",
    "uart_log_path", "stderr_log_path",
}
EXPECTED_WIRING = {
    "flip_left": False,
    "flip_right": True,
    "color_scheme_left": "bgr",
    "color_scheme_right": "grb",
}


class ProofFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProofFailure(message)


def write_json(path: pathlib.Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def png_solid(path: pathlib.Path, width: int = 64, height: int = 32) -> None:
    rows = b"".join(b"\0" + bytes((220, 40, 90)) * width for _ in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)

    data = b"\x89PNG\r\n\x1a\n"
    data += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(rows, 9))
    data += chunk(b"IEND", b"")
    path.write_bytes(data)


def build_host_fixture(root: pathlib.Path) -> None:
    upstream = ROOT / ".work/upstream-v3.4.0"
    shutil.copytree(upstream / "lualib", root / "lualib")
    shutil.copy(upstream / "init.lua", root / "init.lua")
    shutil.copy(upstream / "hardware.json", root / "hardware.json")
    (root / "frames").mkdir()
    (root / "cache").mkdir()
    png_solid(root / "frames/solid.png")
    write_json(root / "animation.json", {
        "frames": [{"name": "idle", "file": "/frames/solid.png"}],
        "expressions": [{"name": "idle", "frames": "idle", "animation": "loop"}],
    })
    (root / "wifi.json").write_text("{}\n", encoding="utf-8")
    write_json(root / "keybinds.json", {"input": {"mode": "NONE", "drivers": ["generic"]}, "keybinds": {}})
    write_json(root / "misc.json", {"overlays": {}, "servos": {}, "leds": {"pin_mode": "double", "groups": []},
                                     "boop": {"enabled": False}, "fft": {"enabled": False}, "scripts": {}})
    (root / "models.json").write_text("{}\n", encoding="utf-8")


def update_for(root: tk.Misc, seconds: float, predicate) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.05)
    root.update()
    return bool(predicate())


def main() -> int:
    run_dir = pathlib.Path(tempfile.mkdtemp(prefix="protopandaemu-m5-proof-", dir="/tmp"))
    evidence_dir = ROOT / "artifacts/mvp/m5-proof1"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    print(f"M5_PROOF_RUN_DIR={run_dir}", flush=True)
    os.environ.setdefault("PROTOPANDAEMU_QUIET_UART", "1")
    app = None
    root = None
    try:
        host = run_dir / "host"
        host.mkdir()
        build_host_fixture(host)

        root = tk.Tk()
        app = launcher.LauncherApp(root, timeout=30.0)

        # Exercise the actual folder-picker callback, with only the native
        # dialog provider replaced so the proof remains non-interactive.
        original_picker = launcher.filedialog.askdirectory
        launcher.filedialog.askdirectory = lambda **_kwargs: str(host)
        try:
            app.choose_folder()
        finally:
            launcher.filedialog.askdirectory = original_picker
        require(app.hostdir == host.resolve(), "folder picker callback did not select the fixture hostdir")

        # Drive the actual left/right widgets, including their bound callbacks.
        app.left_flip_check.invoke()
        app.right_flip_check.invoke()
        app.left_color_combo.set("bgr")
        app.left_color_combo.event_generate("<<ComboboxSelected>>")
        app.right_color_combo.set("grb")
        app.right_color_combo.event_generate("<<ComboboxSelected>>")
        root.update()
        require(app.selected_wiring() == EXPECTED_WIRING,
                f"GUI selection mismatch: {app.selected_wiring()!r}")

        # Invoke the real command= callback, not the implementation helper.
        app.create_profile_button.invoke()
        require(app.profile_path is not None and app.profile_path.is_file(), "profile callback wrote no profile")
        profile = json.loads(app.profile_path.read_text(encoding="utf-8"))
        require(profile["launcher_wiring"] == EXPECTED_WIRING,
                f"profile wiring mismatch: {profile.get('launcher_wiring')!r}")
        animation = json.loads((host / "animation.json").read_text(encoding="utf-8"))
        require(animation.get("frames") and isinstance(animation["frames"][0], dict),
                "animation frame missing")
        for index, frame in enumerate(animation["frames"]):
            require(all(frame.get(key) == value for key, value in EXPECTED_WIRING.items()),
                    f"animation frame {index} wiring mismatch: {frame!r}")

        app.start_button.invoke()
        require(app.handle is not None, "start callback did not create a SessionHandle")
        started = app.handle.session_report()
        require(started["qemu_started"] is True, "start callback did not start QEMU")
        require(started["profile_hash"] == supervisor_hash(profile), "profile hash mismatch before boot")

        saw_real_dump = update_for(root, 25.0, lambda: "dump real" in app.oled_var.get() or "dump real" in app.hub75_var.get())
        require(saw_real_dump, "GUI preview did not observe a real OLED/HUB75 backend dump")
        require("dump real" in app.oled_var.get() or "dump real" in app.hub75_var.get(),
                "preview labels do not identify the captured backend dump as real")

        app.stop_button.invoke()
        report = app.last_report or {}
        require(report.get("exit_reason") == "stopped", f"GUI stop did not cleanly stop session: {report}")
        require(REQUIRED_REPORT_FIELDS.issubset(report),
                f"session report missing Rock 1/M2 fields: {sorted(REQUIRED_REPORT_FIELDS - set(report))}")
        require(report["profile_hash"] == supervisor_hash(profile), "profile hash changed across GUI session")
        require(report["firmware_sha256"] == launcher.supervisor.EXPECTED_FIRMWARE_SHA256,
                "session used a non-pinned firmware hash")
        require(pathlib.Path(report["uart_log_path"]).is_file(), "UART log path in report is not a file")
        require(pathlib.Path(report["stderr_log_path"]).is_file(), "stderr log path in report is not a file")

        shutil.copyfile(report["uart_log_path"], evidence_dir / "qemu.uart.log")
        shutil.copyfile(report["stderr_log_path"], evidence_dir / "qemu.stderr.log")
        write_json(evidence_dir / "profile.json", profile)
        write_json(evidence_dir / "animation.json", animation)
        write_json(evidence_dir / "session-report.json", report)
        summary = {
            "proof": "M5 proof 1",
            "run_dir": str(run_dir),
            "hostdir": str(host),
            "profile_path": str(app.profile_path),
            "profile_hash": report["profile_hash"],
            "session_id": report["session_id"],
            "wiring": EXPECTED_WIRING,
            "preview_oled": app.oled_var.get(),
            "preview_hub75": app.hub75_var.get(),
            "exit_reason": report["exit_reason"],
            "report_fields_verified": sorted(REQUIRED_REPORT_FIELDS),
        }
        write_json(evidence_dir / "summary.json", summary)
        print(json.dumps(summary, sort_keys=True))
        print(f"M5_PROOF_ARTIFACT={evidence_dir / 'summary.json'}")
        return 0
    except (ProofFailure, OSError, tk.TclError) as exc:
        failure = {"run_dir": str(run_dir), "error": str(exc)}
        write_json(evidence_dir / "failure.json", failure)
        print(f"M5_PROOF_FAIL: {exc}", file=sys.stderr)
        return 1
    finally:
        if app is not None and app.handle is not None and app.handle.session_report().get("exit_reason") is None:
            app.stop_session()
        if root is not None:
            root.destroy()


def supervisor_hash(profile: dict[str, object]) -> str:
    return launcher.supervisor.canonical_hash(profile)


if __name__ == "__main__":
    raise SystemExit(main())
