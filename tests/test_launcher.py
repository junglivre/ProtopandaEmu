"""Launcher regression tests for bugs found against a real user SD-card config.

Each covers a boundary/invariant bug a GUI user can hit in practice, not a
synthetic case: a growing session log must not force an O(size) read every
refresh tick, rewriting a user's hand-authored animation.json for four new
wiring fields must not silently reorder/reformat the rest of their file, a
dump landing mid-write in the log tail must not raise inside the refresh
loop, and nothing in refresh() may prevent it from rescheduling itself.
"""
import json
import os
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from protopandaemu import launcher  # noqa: E402


class ApplyWiringPreservesUserFileTests(unittest.TestCase):
    def test_top_level_and_frame_key_order_survive_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "animation.json"
            # Deliberately NOT alphabetical, matching real hand-authored files.
            path.write_text(json.dumps({
                "expression_collections": [{"name": "Mito", "path": "/expressions/collections/mito"}],
                "frames": [
                    {"pattern": "/expressions/sus/sus%d.png", "flip_left": True, "from": 1, "to": 2,
                     "name": "frames_amogus"},
                ],
                "expressions": [{"name": "normal", "frames": "frames_amogus"}],
            }, indent=2) + "\n", encoding="utf-8")
            original_text = path.read_text(encoding="utf-8")

            count = launcher.apply_wiring_to_animation(path, {
                "flip_left": False, "flip_right": True,
                "color_scheme_left": "bgr", "color_scheme_right": "grb",
            })

            self.assertEqual(count, 1)
            updated = json.loads(path.read_text(encoding="utf-8"))
            # Top-level keys keep their original order (sort_keys must be off).
            self.assertEqual(list(updated.keys()), list(json.loads(original_text).keys()))
            frame = updated["frames"][0]
            # Pre-existing keys keep their original order; new ones are appended.
            self.assertEqual(list(frame.keys())[:5],
                              ["pattern", "flip_left", "from", "to", "name"])
            self.assertEqual(frame["flip_left"], False)  # GUI selection wins
            self.assertEqual(frame["flip_right"], True)
            self.assertEqual(frame["color_scheme_left"], "bgr")
            self.assertEqual(frame["color_scheme_right"], "grb")
            # Untouched sections are byte-identical, not just logically equal.
            self.assertEqual(updated["expression_collections"],
                              json.loads(original_text)["expression_collections"])
            self.assertEqual(updated["expressions"], json.loads(original_text)["expressions"])


class ReadLatestDumpsBoundedReadTests(unittest.TestCase):
    def test_finds_dump_near_the_end_of_a_log_larger_than_the_tail_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qemu.stderr.log"
            # Pad past the tail window with unrelated lines, same as a long
            # real session's accumulated log, then one real dump at the end.
            padding = "qemu-system-xtensa: info: unrelated noise line\n" * 20000
            dump = ("qemu-system-xtensa: info: [protopandaemu.ssd1306] GDDRAM_DUMP "
                    "seq=3 width=128 height=64 bytes=4 hex=deadbeef\n")
            self.assertGreater(len(padding), launcher._DUMP_TAIL_BYTES)
            path.write_text(padding + dump, encoding="utf-8")

            oled, hub = launcher._read_latest_dumps(path)

            self.assertIsNone(hub)
            self.assertIsNotNone(oled)
            self.assertEqual(oled["seq"], 3)
            self.assertEqual(oled["data"], bytes.fromhex("deadbeef"))


class ReadLatestDumpsSkipsTruncatedTailMatchTests(unittest.TestCase):
    """A tail read can start mid-dump, truncating its hex payload. The
    decoder must skip that broken match and fall back to the previous,
    fully-written dump instead of raising (which used to kill the GUI
    refresh loop permanently -- see RefreshRescheduleTests below)."""

    def test_truncated_newest_dump_falls_back_to_previous_complete_one(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qemu.stderr.log"
            good = ("qemu-system-xtensa: info: [protopandaemu.ssd1306] GDDRAM_DUMP "
                    "seq=1 width=128 height=64 bytes=4 hex=deadbeef\n")
            # hex payload cut short mid-byte, as a tail-window seek would do.
            truncated = ("qemu-system-xtensa: info: [protopandaemu.ssd1306] GDDRAM_DUMP "
                         "seq=2 width=128 height=64 bytes=4 hex=dead\n")
            path.write_text(good + truncated, encoding="utf-8")

            oled, hub = launcher._read_latest_dumps(path)

            self.assertIsNone(hub)
            self.assertIsNotNone(oled)
            self.assertEqual(oled["seq"], 1)
            self.assertEqual(oled["data"], bytes.fromhex("deadbeef"))


class RefreshRescheduleTests(unittest.TestCase):
    """refresh() must reschedule itself via after() BEFORE doing any work
    that can raise. A session_report() crash (or any other body exception)
    used to kill the after() chain outright: the dashboard froze forever,
    even across starting a brand new session, because the polling loop
    itself was dead, not the session."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")

    def tearDown(self):
        self.root.destroy()

    def test_refresh_reschedules_even_when_session_report_raises(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)  # cancel the real __init__ tick

        class ExplodingHandle:
            def session_report(self):
                raise RuntimeError("backend report boom")

        app.handle = ExplodingHandle()
        before = app._after_id

        app.refresh()

        self.assertIsNotNone(app._after_id)
        self.assertNotEqual(app._after_id, before)
        self.assertIn("Preview error", app.status_var.get())


class AutoSelectsEmuFirmwareTests(unittest.TestCase):
    """create_profile() must pick up a built emu firmware automatically
    (no PROTOPANDAEMU_EMU_FIRMWARE env var) and set the hash allowlist env
    var the supervisor needs to accept it, so the common "run my own
    firmware as-is" workflow needs zero manual shell exports."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")
        self._saved_emu_firmware = launcher.EMU_FIRMWARE
        self._saved_env = os.environ.pop("PROTOPANDAEMU_EXTRA_FIRMWARE_SHA256", None)
        os.environ.pop("PROTOPANDAEMU_EMU_FIRMWARE", None)

    def tearDown(self):
        self.root.destroy()
        launcher.EMU_FIRMWARE = self._saved_emu_firmware
        if self._saved_env is None:
            os.environ.pop("PROTOPANDAEMU_EXTRA_FIRMWARE_SHA256", None)
        else:
            os.environ["PROTOPANDAEMU_EXTRA_FIRMWARE_SHA256"] = self._saved_env

    def test_create_profile_uses_emu_firmware_when_present_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_firmware = Path(directory) / "merged_flash.bin"
            fake_firmware.write_bytes(b"\x00" * 64)
            launcher.EMU_FIRMWARE = fake_firmware

            hostdir = Path(directory) / "host"
            hostdir.mkdir()
            (hostdir / "animation.json").write_text(json.dumps({"frames": []}), encoding="utf-8")

            app = launcher.LauncherApp(self.root)
            self.root.after_cancel(app._after_id)
            app.set_hostdir(hostdir)

            profile_path = app.create_profile()

            self.assertIsNotNone(profile_path)
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            expected_hash = launcher.supervisor._stable_sha256(fake_firmware, "firmware_image")
            self.assertEqual(profile["firmware_image"], str(fake_firmware))
            self.assertEqual(profile["firmware_sha256"], expected_hash)
            self.assertEqual(os.environ.get("PROTOPANDAEMU_EXTRA_FIRMWARE_SHA256"), expected_hash)


class ExplicitFirmwarePickOverridesAutoDetectionTests(unittest.TestCase):
    """A firmware explicitly chosen via the GUI's "Choose firmware..."
    button must win over the auto-detected EMU_FIRMWARE build, not be
    silently ignored in favor of it."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")
        self._saved_emu_firmware = launcher.EMU_FIRMWARE
        self._saved_env = os.environ.pop("PROTOPANDAEMU_EXTRA_FIRMWARE_SHA256", None)
        os.environ.pop("PROTOPANDAEMU_EMU_FIRMWARE", None)

    def tearDown(self):
        self.root.destroy()
        launcher.EMU_FIRMWARE = self._saved_emu_firmware
        if self._saved_env is None:
            os.environ.pop("PROTOPANDAEMU_EXTRA_FIRMWARE_SHA256", None)
        else:
            os.environ["PROTOPANDAEMU_EXTRA_FIRMWARE_SHA256"] = self._saved_env

    def test_explicit_firmware_path_wins_over_auto_detected_default(self):
        with tempfile.TemporaryDirectory() as directory:
            auto_firmware = Path(directory) / "auto.bin"
            auto_firmware.write_bytes(b"\x01" * 64)
            launcher.EMU_FIRMWARE = auto_firmware

            chosen_firmware = Path(directory) / "chosen.bin"
            chosen_firmware.write_bytes(b"\x02" * 64)

            hostdir = Path(directory) / "host"
            hostdir.mkdir()
            (hostdir / "animation.json").write_text(json.dumps({"frames": []}), encoding="utf-8")

            app = launcher.LauncherApp(self.root)
            self.root.after_cancel(app._after_id)
            app.set_hostdir(hostdir)
            app.firmware_path = chosen_firmware  # simulates choose_firmware()'s effect

            profile_path = app.create_profile()

            self.assertIsNotNone(profile_path)
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            expected_hash = launcher.supervisor._stable_sha256(chosen_firmware, "firmware_image")
            self.assertEqual(profile["firmware_image"], str(chosen_firmware))
            self.assertNotEqual(profile["firmware_image"], str(auto_firmware))
            self.assertEqual(profile["firmware_sha256"], expected_hash)
            self.assertEqual(app.firmware_var.get(), str(chosen_firmware))

class FakeSerialHandle:
    """Minimal stand-in for supervisor.SessionHandle: a running session
    that records every line written to the console."""

    def __init__(self, accept: bool = True):
        self.accept = accept
        self.sent: list[str] = []

    def wait(self, timeout: float | None = None) -> bool:
        return False  # still running

    def send_serial_input(self, text: str) -> bool:
        if self.accept:
            self.sent.append(text)
        return self.accept


class BoopConfigLoadTests(unittest.TestCase):
    """_load_boop_config() must read misc.json's boop.gpio only when
    trigger_mode is "gpio" -- the lidar mode has no digital pin to
    override, and a toggle button wired to a nonexistent pin would be
    silently useless."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")

    def tearDown(self):
        self.root.destroy()

    def _hostdir_with_misc(self, directory, misc):
        hostdir = Path(directory) / "host"
        hostdir.mkdir()
        (hostdir / "animation.json").write_text(json.dumps({"frames": []}), encoding="utf-8")
        (hostdir / "misc.json").write_text(json.dumps(misc), encoding="utf-8")
        return hostdir

    def test_gpio_trigger_mode_picks_up_the_configured_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            hostdir = self._hostdir_with_misc(
                directory, {"boop": {"trigger_mode": "gpio", "gpio": 2, "gpio_state": 1}})
            app = launcher.LauncherApp(self.root)
            self.root.after_cancel(app._after_id)
            app.set_hostdir(hostdir)
            self.assertEqual(app.boop_gpio, 2)

    def test_lidar_trigger_mode_leaves_no_gpio_to_override(self):
        with tempfile.TemporaryDirectory() as directory:
            # A stray numeric "gpio" alongside lidar settings (leftover from
            # switching modes) must not be picked up: only trigger_mode
            # "gpio" has a real pin for digitalReadEmu() to override.
            hostdir = self._hostdir_with_misc(
                directory, {"boop": {"trigger_mode": "lidar", "gpio": 5}})
            app = launcher.LauncherApp(self.root)
            self.root.after_cancel(app._after_id)
            app.set_hostdir(hostdir)
            self.assertIsNone(app.boop_gpio)


class ToggleBoopSendsGpioOverrideTests(unittest.TestCase):
    """_toggle_boop() must inject "GPIO:<pin>:<0|1>" on the session console
    (the channel boop.lua's digitalReadEmu() reads) and flip boop_near only
    when the write actually succeeds."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")

    def tearDown(self):
        self.root.destroy()

    def test_toggle_sends_pin_and_flips_state(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app.boop_gpio = 2
        app.handle = FakeSerialHandle()

        app._toggle_boop()
        self.assertEqual(app.handle.sent, ["GPIO:2:1\n"])
        self.assertTrue(app.boop_near)

        app._toggle_boop()
        self.assertEqual(app.handle.sent, ["GPIO:2:1\n", "GPIO:2:0\n"])
        self.assertFalse(app.boop_near)

    def test_failed_write_does_not_flip_state(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app.boop_gpio = 2
        app.handle = FakeSerialHandle(accept=False)

        app._toggle_boop()
        self.assertFalse(app.boop_near)

    def test_no_configured_pin_refuses_to_toggle(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app.boop_gpio = None
        app.handle = FakeSerialHandle()

        app._toggle_boop()
        self.assertEqual(app.handle.sent, [])
        self.assertFalse(app.boop_near)


class DrawHub75SplitsLeftRightHalvesTests(unittest.TestCase):
    """The real 2-panel horizontal chain packs both eyes into one combined
    FRAME_DUMP (cols 0..half-1 = left panel, half..width-1 = right panel,
    per hardware.json's horizontal_panel_count=2). The preview renders each
    half on its own physically-positioned canvas instead of one combined
    strip -- confirmed by feeding a synthetic frame with distinct solid
    colors on each half and checking each canvas's image reflects only its
    own half, not the other's color."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")

    def tearDown(self):
        self.root.destroy()

    def test_left_and_right_canvases_show_distinct_half_content(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)

        width, height = 8, 4  # synthetic frame, half=4
        data = bytearray(width * height * 3)
        for y in range(height):
            for x in range(width):
                base = (y * width + x) * 3
                if x < width // 2:
                    data[base:base + 3] = bytes((255, 0, 0))  # left half: red
                else:
                    data[base:base + 3] = bytes((0, 0, 255))  # right half: blue

        dump = {"seq": 1, "width": width, "height": height, "data": bytes(data)}
        app._draw_hub75(dump)

        self.assertIsNotNone(app._hub75_left_photo)
        self.assertIsNotNone(app._hub75_right_photo)
        self.assertEqual(app._hub75_left_photo.get(0, 0), (255, 0, 0))
        self.assertEqual(app._hub75_right_photo.get(0, 0), (0, 0, 255))
        # Both halves are the same source width (4 native columns each) and
        # render through the same draw path, so they must scale identically.
        self.assertEqual(app._hub75_left_photo.width(), app._hub75_right_photo.width())
        # The rightmost pixel of the LEFT photo must still be red (never
        # bleeds into the right half's blue), proving the split boundary is
        # exact, not off-by-one.
        last_left_x = app._hub75_left_photo.width() - 1
        self.assertEqual(app._hub75_left_photo.get(last_left_x, 0), (255, 0, 0))


if __name__ == "__main__":
    unittest.main()
