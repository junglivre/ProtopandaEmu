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
import time
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

    def __init__(self, accept: bool = True, marker_set: bool = True):
        self.accept = accept
        self.sent: list[str] = []
        self.marker_set = marker_set

    def wait(self, timeout: float | None = None) -> bool:
        return False  # still running

    def send_serial_input(self, text: str) -> bool:
        if self.accept:
            self.sent.append(text)
        return self.accept

    def wait_for_marker(self, marker: str, timeout: float) -> bool:
        return self.marker_set

    def session_report(self) -> dict:
        return {"resources": {}, "stderr_log_path": None, "uart_log_path": None, "exit_reason": None}


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


class DeferredBoopDefaultOverrideTests(unittest.TestCase):
    """Sending the boop-far override right when the session starts races
    the guest: the console parser only reads Serial once loop() is
    running, and bytes arriving during the ROM bootloader/setup() window
    can be lost (this previously left boop reading as permanently
    triggered from boot, since the real unconnected pin happened to read
    the same level as the configured gpio_state). refresh() must defer
    the send until the "vfs_init" boot marker confirms the guest loop is
    actually consuming console input, and must send it exactly once."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")

    def tearDown(self):
        self.root.destroy()

    def test_waits_for_vfs_init_marker_before_sending(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app.boop_gpio = 2
        app.handle = FakeSerialHandle(marker_set=False)
        app._boop_default_sent = False

        app.refresh()
        self.root.after_cancel(app._after_id)
        self.assertEqual(app.handle.sent, [])
        self.assertFalse(app._boop_default_sent)

    def test_sends_once_marker_is_set_and_does_not_resend(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app.boop_gpio = 2
        app.handle = FakeSerialHandle(marker_set=True)
        app._boop_default_sent = False

        app.refresh()
        self.root.after_cancel(app._after_id)
        self.assertEqual(app.handle.sent, ["GPIO:2:0\n"])
        self.assertTrue(app._boop_default_sent)

        app.refresh()
        self.root.after_cancel(app._after_id)
        self.assertEqual(app.handle.sent, ["GPIO:2:0\n"])



class ButtonHoldRepeatTests(unittest.TestCase):
    """D-pad/Back/Aux must keep injecting while the mouse stays down, not
    just once per click: the firmware's own press() (input.lua) refreshes
    its button_timeout to now+250ms on each decoded command and
    auto-releases once that elapses, so a single injection only ever holds
    the button for 250ms regardless of how long the real mouse button
    stays down. _start_button_hold() must re-inject well inside that
    window for as long as it keeps getting called, and _stop_button_hold()
    must actually stop further injections, not just leave a stale handle."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")

    def tearDown(self):
        self.root.destroy()

    def test_start_sends_immediately_and_repeat_calls_send_again(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app.handle = FakeSerialHandle()

        app._start_button_hold("B9")
        self.assertEqual(app.handle.sent, ["IR:FF00:B9\n"])
        self.assertIsNotNone(app._hold_repeat_id)

        # Simulates the scheduled repeat tick firing while still held.
        app._start_button_hold("B9")
        self.assertEqual(app.handle.sent, ["IR:FF00:B9\n", "IR:FF00:B9\n"])
        self.assertIsNotNone(app._hold_repeat_id)

        app._stop_button_hold()
        self.assertIsNone(app._hold_repeat_id)

    def test_real_hold_keeps_injecting_until_released(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app.handle = FakeSerialHandle()

        app._start_button_hold("BF")
        # Pump the real Tk event loop past several 150ms repeat intervals;
        # a held button must keep sending, not just the initial injection.
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.02)
        self.assertGreater(len(app.handle.sent), 1)

        app._stop_button_hold()
        sent_at_release = len(app.handle.sent)
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.02)
        self.assertEqual(len(app.handle.sent), sent_at_release)


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


class ReadLatestHub75BrightnessTests(unittest.TestCase):
    """_read_latest_hub75_brightness() decodes Devices::SetMaxBrightness's
    dump (firmware-emu patch 0014) the same tail-scan way the other
    console-sourced decoders do, picking the newest value and clamping to
    the real uint8_t range."""

    def test_decodes_the_newest_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qemu.uart.log"
            path.write_text(
                "boot noise\n"
                "[EMU] HUB75_BRIGHTNESS value=1\n"
                "[EMU] HUB75_BRIGHTNESS value=64\n",
                encoding="utf-8",
            )
            self.assertEqual(launcher._read_latest_hub75_brightness(path), 64)

    def test_missing_file_returns_none(self):
        value = launcher._read_latest_hub75_brightness(Path("/nonexistent/qemu.uart.log"))
        self.assertIsNone(value)

    def test_no_lines_yet_returns_none(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qemu.uart.log"
            path.write_text("boot noise, no brightness dump yet\n", encoding="utf-8")
            self.assertIsNone(launcher._read_latest_hub75_brightness(path))


class DrawHub75ScalesToConfiguredBrightnessTests(unittest.TestCase):
    """The preview must scale the decoded (always full-brightness) color
    data down to the real configured brightness, since this backend's
    HUB75 decoder has no visibility into the panel's actual OE/blanking
    dimming signal -- confirmed by rendering the same full-white frame at
    two different self.hub75_brightness values and checking the pixel
    output actually differs proportionally, and that the default (unknown
    brightness) case renders undimmed."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")

    def tearDown(self):
        self.root.destroy()

    def _white_dump(self, width=4, height=2):
        data = bytes((255, 255, 255)) * (width * height)
        return {"seq": 1, "width": width, "height": height, "data": data}

    def test_default_brightness_renders_undimmed(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        self.assertEqual(app.hub75_brightness, 255)
        app._draw_hub75(self._white_dump())
        self.assertEqual(app._hub75_left_photo.get(0, 0), (255, 255, 255))

    def test_half_brightness_dims_the_preview_proportionally(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app.hub75_brightness = 128
        app._draw_hub75(self._white_dump())
        r, g, b = app._hub75_left_photo.get(0, 0)
        self.assertEqual((r, g, b), (128, 128, 128))

    def test_zero_brightness_renders_black(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app.hub75_brightness = 0
        app._draw_hub75(self._white_dump())
        self.assertEqual(app._hub75_left_photo.get(0, 0), (0, 0, 0))


class ReadLatestLedDumpTests(unittest.TestCase):
    """_read_latest_led_dump() decodes firmware-emu patch 0013's LED_DUMP
    console line the same way _read_latest_dumps() handles OLED/HUB75,
    including skipping a truncated tail match in favor of an earlier
    complete one."""

    def test_decodes_the_newest_complete_dump(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qemu.uart.log"
            path.write_text(
                "boot noise\n"
                "[EMU] LED_DUMP count=2 hex=ff0000" + "00ff00" + "\n"
                "[EMU] LED_DUMP count=1 hex=0000ff\n",
                encoding="utf-8",
            )
            dump = launcher._read_latest_led_dump(path)
            self.assertEqual(dump, {"count": 1, "data": bytes.fromhex("0000ff")})

    def test_skips_a_truncated_tail_match(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qemu.uart.log"
            path.write_text(
                "[EMU] LED_DUMP count=1 hex=ff0000\n"
                "[EMU] LED_DUMP count=2 hex=00ff",  # truncated, missing the rest
                encoding="utf-8",
            )
            dump = launcher._read_latest_led_dump(path)
            self.assertEqual(dump, {"count": 1, "data": bytes.fromhex("ff0000")})

    def test_missing_file_returns_none(self):
        dump = launcher._read_latest_led_dump(Path("/nonexistent/qemu.uart.log"))
        self.assertIsNone(dump)


class DrawLedStripRendersEachColorTests(unittest.TestCase):
    """_draw_led_strip() must render one pixel per LED, in order, matching
    the dump's own RGB bytes -- not an average, not just the first/last."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")

    def tearDown(self):
        self.root.destroy()

    def test_renders_distinct_colors_in_order(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)

        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
        data = b"".join(bytes(c) for c in colors)
        dump = {"count": len(colors), "data": data}
        app._draw_led_strip(dump)

        self.assertIsNotNone(app._led_photo)
        photo = app._led_photo
        scale = photo.width() // len(colors)
        self.assertGreaterEqual(scale, 1)
        for index, color in enumerate(colors):
            self.assertEqual(photo.get(index * scale, 0), color)

    def test_mismatched_byte_count_is_ignored_not_crashed(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app._draw_led_strip({"count": 3, "data": b"\x00\x01"})  # too short
        self.assertIsNone(app._led_photo)


class RmsToLevelMappingTests(unittest.TestCase):
    """_rms_to_level() maps host microphone RMS to a 0..1 dBFS-domain
    meter fraction, floor/ceil-clamped at MIC_DB_FLOOR/MIC_DB_CEIL --
    a linear-amplitude mapping would make the meter barely move for
    normal speech, since audio RMS is log-distributed in practice."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")

    def tearDown(self):
        self.root.destroy()

    def test_full_scale_rms_is_full_meter(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        self.assertAlmostEqual(app._rms_to_level(1.0), 1.0, places=6)

    def test_silence_is_empty_meter(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        self.assertEqual(app._rms_to_level(0.0), 0.0)

    def test_floor_db_is_empty_meter(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app.mic_gain_var.set(1.0)  # isolate the floor boundary from the gain boost
        floor_rms = 10 ** (app.MIC_DB_FLOOR / 20.0)
        self.assertAlmostEqual(app._rms_to_level(floor_rms), 0.0, places=6)

    def test_gain_boosts_a_quiet_signal(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        quiet_rms = 10 ** (app.MIC_DB_FLOOR / 20.0) * 2  # just above the floor
        app.mic_gain_var.set(1.0)
        unboosted = app._rms_to_level(quiet_rms)
        app.mic_gain_var.set(4.0)
        boosted = app._rms_to_level(quiet_rms)
        self.assertGreater(boosted, unboosted)


class MicSendLoopInjectsScaledEnergyTests(unittest.TestCase):
    """Each tick must inject the SAME per-band value, one "MIC:<band>:<n>"
    line per band, for every band in the card's configured
    speech_band_start..speech_band_end range (the firmware only ever
    sums whatever getBandValueFft() returns across that range, per
    lualib/fft.lua's getSpeechLevel), each computed as
    level * fft_max_energy * MIC_HEADROOM spread evenly over the band
    count -- not the raw host RMS and not a single full-scale value."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")

    def tearDown(self):
        self.root.destroy()

    def test_injects_energy_spread_over_the_configured_band_count(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app.fft_band_start, app.fft_band_end = 2, 8  # 7 bands, this user's real config
        app.fft_max_energy = 200000
        app.handle = FakeSerialHandle()
        app._mic_active = True
        app._mic_level = 1.0  # 0 dBFS -> full-scale level
        try:
            app._injection_loop()
        finally:
            if app._injection_job_id is not None:
                self.root.after_cancel(app._injection_job_id)
        expected = int(200000 * app.MIC_HEADROOM / 7)
        self.assertEqual(app.handle.sent, [f"MIC:{band}:{expected}\n" for band in range(2, 9)])

    def test_no_session_does_not_crash_and_sends_nothing(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app._mic_active = True
        app._mic_level = 1.0
        try:
            app._injection_loop()
        finally:
            if app._injection_job_id is not None:
                self.root.after_cancel(app._injection_job_id)
        # no exception, nothing to assert on -- there was no handle to send to


class MicThresholdFractionTests(unittest.TestCase):
    """The VU meter's threshold marker must sit at the SAME position the
    energy injection would reach once the host level is loud enough to
    cross speech_frist_frame_threshold -- using a different scale for the
    marker than for the injected value would make the line lie."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")

    def tearDown(self):
        self.root.destroy()

    def test_matches_this_users_real_config(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app.fft_max_energy = 200000
        app.fft_frist_frame_threshold = 40000
        expected = 40000 / (200000 * app.MIC_HEADROOM)
        self.assertAlmostEqual(app._mic_threshold_fraction(), expected, places=9)

    def test_clamped_to_one_when_threshold_exceeds_headroom(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app.fft_max_energy = 100
        app.fft_frist_frame_threshold = 999999
        self.assertEqual(app._mic_threshold_fraction(), 1.0)


class MicButtonExplainsWhyItCannotStartTests(unittest.TestCase):
    """The Mic button must stay clickable even when it can't actually
    start (no fft.enabled on this SD card, or sounddevice missing) --
    disabling it outright hid the reason from the user entirely, since a
    disabled ttk.Button never fires its command and so never shows the
    explanation. Clicking in either missing case must explain why and
    must not start capture."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")

    def tearDown(self):
        self.root.destroy()

    def _hostdir_with_fft(self, directory, enabled):
        path = Path(directory) / "misc.json"
        path.write_text(json.dumps({"fft": {
            "enabled": enabled, "speech_band_start": 2, "speech_band_end": 8,
            "speech_min_energy": 50000, "speech_max_energy": 200000,
            "speech_frist_frame_threshold": 40000,
        }}), encoding="utf-8")
        return directory

    def _patch_showerror(self):
        calls = []
        original = launcher.messagebox.showerror
        launcher.messagebox.showerror = lambda title, msg, **kw: calls.append((title, msg))
        self.addCleanup(setattr, launcher.messagebox, "showerror", original)
        return calls

    def test_button_stays_enabled_without_fft_on_card(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        with tempfile.TemporaryDirectory() as directory:
            app.set_hostdir(self._hostdir_with_fft(directory, enabled=False))
        self.assertEqual(str(app.mic_button["state"]), "normal")

    def test_device_list_has_default_without_sounddevice(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        self.assertEqual(list(app.mic_device_combo["values"]), ["Default input device"])
        self.assertEqual(app._selected_mic_device(), None)

    def test_threshold_text_shows_real_config_values(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        with tempfile.TemporaryDirectory() as directory:
            app.set_hostdir(self._hostdir_with_fft(directory, enabled=True))
        text = app.mic_threshold_var.get()
        self.assertIn("40000", text)
        self.assertIn("50000", text)
        self.assertIn("200000", text)

    def test_explains_missing_fft_and_does_not_start(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        calls = self._patch_showerror()
        with tempfile.TemporaryDirectory() as directory:
            app.set_hostdir(self._hostdir_with_fft(directory, enabled=False))
        app._start_mic_capture()
        self.assertEqual(len(calls), 1)
        self.assertIn("fft.enabled", calls[0][1])
        self.assertFalse(app._mic_active)

    def test_explains_missing_sounddevice_and_does_not_start(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        original = launcher._sounddevice
        launcher._sounddevice = None
        calls = self._patch_showerror()
        try:
            with tempfile.TemporaryDirectory() as directory:
                app.set_hostdir(self._hostdir_with_fft(directory, enabled=True))
            app._start_mic_capture()
        finally:
            launcher._sounddevice = original
        self.assertEqual(len(calls), 1)
        self.assertIn("sounddevice", calls[0][1])
        self.assertFalse(app._mic_active)


class EqualizerTests(unittest.TestCase):
    """The equalizer popup drives each FFT band independently -- a
    single uniform level made every band rise together, which doesn't
    let the user find whatever band shape the firmware actually needs
    to cross its activation threshold. Shares one injection loop with
    the Mic button (not a separate timer) so the two can run together:
    the Equalizer's "scale by live mic level" option needs both at
    once, and racing two independent timers against the same bands
    would otherwise flicker between whichever fired last."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")

    def tearDown(self):
        self.root.destroy()

    def _app_with_fft(self, directory, band_count=8):
        path = Path(directory) / "misc.json"
        path.write_text(json.dumps({"fft": {
            "enabled": True, "speech_band_start": 2, "speech_band_end": 4,
            "speech_min_energy": 50000, "speech_max_energy": 200000,
            "speech_frist_frame_threshold": 40000, "band_count": band_count,
        }}), encoding="utf-8")
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app.set_hostdir(directory)
        app.handle = FakeSerialHandle()
        return app

    def _open_and_disarm(self, app):
        """Opens the popup and cancels its live-send timer so a test can
        drive it manually instead of racing a background after() tick."""
        app._open_equalizer()
        if app._injection_job_id is not None:
            self.root.after_cancel(app._injection_job_id)
            app._injection_job_id = None

    def test_one_slider_per_band(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app_with_fft(directory, band_count=8)
        self._open_and_disarm(app)
        try:
            self.assertEqual(len(app.eq_band_vars), 8)
        finally:
            app._close_equalizer()

    def test_speech_range_only_boosts_configured_bands(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app_with_fft(directory, band_count=8)
        self._open_and_disarm(app)
        try:
            app._eq_set_speech_range()
            top = app._eq_slider_max()
            values = [var.get() for var in app.eq_band_vars]
            self.assertEqual(values, [0, 0, top, top, top, 0, 0, 0])
        finally:
            app._close_equalizer()

    def test_injection_loop_applies_boost_per_band(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app_with_fft(directory, band_count=8)
        self._open_and_disarm(app)
        try:
            app.eq_band_vars[3].set(1000)
            app.eq_boost_var.set(2.0)
            app.handle.sent.clear()
            app._injection_loop()
            if app._injection_job_id is not None:
                self.root.after_cancel(app._injection_job_id)
                app._injection_job_id = None
            self.assertIn("MIC:3:2000\n", app.handle.sent)
        finally:
            app._close_equalizer()

    def test_close_releases_every_band(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app_with_fft(directory, band_count=8)
        self._open_and_disarm(app)
        app.handle.sent.clear()
        app._close_equalizer()
        self.assertEqual(app.handle.sent, [f"MIC:{band}:-1\n" for band in range(8)])
        self.assertIsNone(app.eq_window)

    def test_scale_by_mic_uses_zero_when_mic_off(self):
        """Without Mic running, "scale by live mic level" must send 0 for
        every band -- not the slider's raw value (that would silently
        act as if the checkbox were off) and not crash on no capture
        state existing yet."""
        with tempfile.TemporaryDirectory() as directory:
            app = self._app_with_fft(directory, band_count=8)
        self._open_and_disarm(app)
        try:
            app.eq_band_vars[3].set(1000)
            app.eq_use_mic_var.set(True)
            app.handle.sent.clear()
            app._injection_loop()
            if app._injection_job_id is not None:
                self.root.after_cancel(app._injection_job_id)
                app._injection_job_id = None
            self.assertIn("MIC:3:0\n", app.handle.sent)
        finally:
            app._close_equalizer()

    def test_scale_by_mic_multiplies_slider_by_captured_level(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app_with_fft(directory, band_count=8)
        self._open_and_disarm(app)
        try:
            app.eq_band_vars[3].set(1000)
            app.eq_use_mic_var.set(True)
            app._mic_active = True
            app._mic_level = 1.0  # 0 dBFS -> full-scale level == 1.0
            app.handle.sent.clear()
            app._injection_loop()
            if app._injection_job_id is not None:
                self.root.after_cancel(app._injection_job_id)
                app._injection_job_id = None
            self.assertIn("MIC:3:1000\n", app.handle.sent)
        finally:
            app._mic_active = False
            app._close_equalizer()

    def test_mic_and_equalizer_can_run_together(self):
        """Opening the Equalizer must no longer stop a running Mic
        capture (the old mutex forced picking one or the other)."""
        with tempfile.TemporaryDirectory() as directory:
            app = self._app_with_fft(directory, band_count=8)
        app._mic_active = True
        self._open_and_disarm(app)
        try:
            self.assertTrue(app._mic_active)
        finally:
            app._mic_active = False
            app._close_equalizer()

    def test_slider_positions_survive_close_and_reopen(self):
        """Closing the popup must release the console override (the
        firmware shouldn't keep seeing stale test data after the user
        closes the tool) but must NOT reset the slider positions
        themselves -- losing a carefully-tuned test pattern on every
        close made the tool unusable for iterating on one."""
        with tempfile.TemporaryDirectory() as directory:
            app = self._app_with_fft(directory, band_count=8)
        self._open_and_disarm(app)
        app.eq_band_vars[3].set(12345)
        app.eq_band_vars[5].set(6789)
        app._close_equalizer()
        self.assertIsNone(app.eq_window)

        self._open_and_disarm(app)
        try:
            self.assertEqual(app.eq_band_vars[3].get(), 12345)
            self.assertEqual(app.eq_band_vars[5].get(), 6789)
        finally:
            app._close_equalizer()

    def test_band_count_change_resets_stale_positions(self):
        """A stored position list from a previous (differently-sized)
        SD card's band_count must not be reused at the wrong indices
        after switching cards -- it should reset cleanly instead."""
        with tempfile.TemporaryDirectory() as directory:
            app = self._app_with_fft(directory, band_count=8)
        self._open_and_disarm(app)
        app.eq_band_vars[3].set(12345)
        app._close_equalizer()

        app.fft_band_count = 16  # simulate switching to a different SD card
        self._open_and_disarm(app)
        try:
            self.assertEqual(len(app.eq_band_vars), 16)
            self.assertEqual([v.get() for v in app.eq_band_vars], [0] * 16)
        finally:
            app._close_equalizer()


class SetUartTailFiltersLedDumpTests(unittest.TestCase):
    """[EMU] LED_DUMP lines carry a full hex color buffer per frame and
    can dwarf everything else in the UART panel; they must not show up
    there, but the file on disk (what the LED strip canvas itself
    parses separately) must stay untouched."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")

    def tearDown(self):
        self.root.destroy()

    def test_led_dump_lines_excluded_from_the_visible_panel(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qemu.uart.log"
            path.write_text(
                "[1000][I] boot noise\n"
                "[EMU] LED_DUMP count=2 hex=ff0000ff0000\n"
                "[1001][I] more boot noise\n",
                encoding="utf-8",
            )
            app._set_uart_tail(path)
            shown = app.uart_text.get("1.0", "end")
            self.assertNotIn("LED_DUMP", shown)
            self.assertIn("boot noise", shown)
            # The file itself is untouched -- the strip canvas's own
            # reader (_read_latest_led_dump) still sees the real line.
            self.assertIn("LED_DUMP", path.read_text(encoding="utf-8"))

    def test_mid_line_seek_does_not_leak_a_hex_fragment(self):
        """A tail seek can land inside a LED_DUMP line's hex payload,
        stripping its "[EMU] LED_DUMP" prefix -- that orphaned fragment
        must not slip through the filter as a bare wall of hex."""
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        line1 = "[1000][I] boot noise\n"
        line2 = "[EMU] LED_DUMP count=2 hex=ff0000ff0000\n"
        line3 = "[1001][I] more boot noise\n"
        content = line1 + line2 + line3
        # Put the seek point right after "hex=ff0000" -- mid payload --
        # so the tail window's first "line" is the orphaned fragment
        # "ff0000\n" plus line3, with no "[EMU] LED_DUMP" prefix left.
        split_point = len(line1) + line2.index("hex=") + len("hex=ff0000")
        original_tail_bytes = launcher._DUMP_TAIL_BYTES
        launcher._DUMP_TAIL_BYTES = len(content) - split_point
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "qemu.uart.log"
                path.write_text(content, encoding="utf-8")
                app._set_uart_tail(path)
                shown = app.uart_text.get("1.0", "end")
                self.assertNotIn("ff0000", shown)
                self.assertIn("more boot noise", shown)
        finally:
            launcher._DUMP_TAIL_BYTES = original_tail_bytes


class ReadLatestLedBrightnessTests(unittest.TestCase):
    """_read_latest_led_brightness() decodes LedStrip::setBrightness's
    dump the same tail-scan way _read_latest_hub75_brightness() does,
    picking the newest value and clamping to the real uint8_t range."""

    def test_decodes_the_newest_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qemu.uart.log"
            path.write_text(
                "boot noise\n"
                "[EMU] LED_BRIGHTNESS value=1\n"
                "[EMU] LED_BRIGHTNESS value=64\n",
                encoding="utf-8",
            )
            self.assertEqual(launcher._read_latest_led_brightness(path), 64)

    def test_missing_file_returns_none(self):
        value = launcher._read_latest_led_brightness(Path("/nonexistent/qemu.uart.log"))
        self.assertIsNone(value)

    def test_no_lines_yet_returns_none(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qemu.uart.log"
            path.write_text("boot noise, no brightness dump yet\n", encoding="utf-8")
            self.assertIsNone(launcher._read_latest_led_brightness(path))


class DrawLedStripScalesToConfiguredBrightnessTests(unittest.TestCase):
    """Same reasoning as DrawHub75ScalesToConfiguredBrightnessTests: the
    decoded LED buffer is always full-brightness, so the preview must
    scale it down to the real configured value separately."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")

    def tearDown(self):
        self.root.destroy()

    def _white_dump(self, count=3):
        return {"count": count, "data": bytes((255, 255, 255)) * count}

    def test_default_brightness_renders_undimmed(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        self.assertEqual(app.led_brightness, 255)
        app._draw_led_strip(self._white_dump())
        self.assertEqual(app._led_photo.get(0, 0), (255, 255, 255))

    def test_half_brightness_dims_the_preview_proportionally(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app.led_brightness = 128
        app._draw_led_strip(self._white_dump())
        self.assertEqual(app._led_photo.get(0, 0), (128, 128, 128))


class Hub75InverseGammaCorrectsContrastTests(unittest.TestCase):
    """ESP32-HUB75-MatrixPanel-I2S-DMA applies a CIE1931 gamma LUT before
    writing bit-planes (confirmed: NO_CIE1931 is never defined in
    Proto-panda), so HUB75_RE's decoded bytes are the post-gamma wire
    value, not the asset's original color: a real midtone input of 128
    only ever reaches the wire as 47. Without correction the preview
    renders a wire-accurate but much darker image than the asset; this
    must look close to the original instead."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")

    def tearDown(self):
        self.root.destroy()

    def test_endpoints_are_unchanged(self):
        self.assertEqual(launcher.HUB75_INVERSE_GAMMA[0], 0)
        self.assertEqual(launcher.HUB75_INVERSE_GAMMA[255], 255)

    def test_a_midtone_wire_value_is_boosted_back_toward_the_asset(self):
        # 47 is what the real library's gamma table reduces an original
        # 128 to (lumConvTab[128] >> 8); the preview must reconstruct
        # something close to 128, not display the dim wire value as-is.
        self.assertGreater(launcher.HUB75_INVERSE_GAMMA[47], 100)

    def test_rendered_pixel_is_corrected_not_left_dim(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        width, height = 2, 1
        data = bytes((47, 47, 47)) * width
        app._draw_hub75({"seq": 1, "width": width, "height": height, "data": data})
        r, g, b = app._hub75_left_photo.get(0, 0)
        self.assertEqual((r, g, b), (launcher.HUB75_INVERSE_GAMMA[47],) * 3)
        self.assertGreater(r, 47)


class ReadNewToneEventsTests(unittest.TestCase):
    """_read_new_tone_events() must only return events newer than the
    caller's last-seen seq: a tone is a discrete action, and the tail
    window holds far more history than one 50ms refresh tick, so
    replaying an already-applied event on the next poll would restart
    its auto-stop timer and the tone would never turn off."""

    def test_decodes_every_new_event_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qemu.uart.log"
            path.write_text(
                "boot noise\n"
                "[EMU] TONE seq=0 freq=440 duration_ms=500\n"
                "[EMU] TONE seq=1 freq=0 duration_ms=0\n",
                encoding="utf-8",
            )
            events = launcher._read_new_tone_events(path, after_seq=-1)
            self.assertEqual(events, [(0, 440, 500), (1, 0, 0)])

    def test_already_seen_events_are_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qemu.uart.log"
            path.write_text(
                "[EMU] TONE seq=0 freq=440 duration_ms=500\n"
                "[EMU] TONE seq=1 freq=640 duration_ms=50\n",
                encoding="utf-8",
            )
            events = launcher._read_new_tone_events(path, after_seq=0)
            self.assertEqual(events, [(1, 640, 50)])

    def test_no_new_events_returns_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qemu.uart.log"
            path.write_text("[EMU] TONE seq=0 freq=440 duration_ms=500\n", encoding="utf-8")
            self.assertEqual(launcher._read_new_tone_events(path, after_seq=0), [])

    def test_missing_file_returns_empty(self):
        events = launcher._read_new_tone_events(Path("/nonexistent/qemu.uart.log"), after_seq=-1)
        self.assertEqual(events, [])


class ApplyToneEventTests(unittest.TestCase):
    """_apply_tone_event() drives the speaker's current frequency and
    schedules its own auto-stop from the event's duration, independent
    of the firmware's own (possibly same-poll-window) stop line -- see
    _speaker_callback/_apply_tone_event for why a start always wins a
    guaranteed minimum audible duration."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")

    def tearDown(self):
        self.root.destroy()

    def test_start_event_sets_the_frequency(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app._apply_tone_event(440, 500)
        self.assertEqual(app._speaker_freq, 440)
        self.root.after_cancel(app._speaker_stop_job)

    def test_explicit_stop_event_silences_immediately(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app._apply_tone_event(440, 500)
        app._apply_tone_event(0, 0)
        self.assertEqual(app._speaker_freq, 0)
        self.assertIsNone(app._speaker_stop_job)

    def test_new_start_cancels_a_pending_auto_stop(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app._apply_tone_event(440, 50000)  # long enough it won't fire during the test
        first_job = app._speaker_stop_job
        app._apply_tone_event(640, 50000)
        self.assertNotEqual(app._speaker_stop_job, first_job)
        self.assertEqual(app._speaker_freq, 640)
        self.root.after_cancel(app._speaker_stop_job)

    def test_auto_stop_fires_after_the_scheduled_duration(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app._apply_tone_event(440, 20)
        deadline = time.monotonic() + 1.0
        while app._speaker_freq != 0 and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertEqual(app._speaker_freq, 0)
        self.assertIsNone(app._speaker_stop_job)


class SelectOutputDevicePrefersPulseTests(unittest.TestCase):
    """Same bug class as the Mic's existing "pulse" preference: PortAudio's
    own default output can resolve to a raw ALSA hw device that bypasses
    the desktop sound server's routing (Bluetooth headphones, a selected
    output device) entirely, so a plain default-device OutputStream can
    come out of the wrong speaker or not at all."""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")
        self.app = launcher.LauncherApp(self.root)
        self.root.after_cancel(self.app._after_id)

    def tearDown(self):
        self.root.destroy()

    def test_prefers_the_pulse_device_when_present(self):
        class FakeSoundDevice:
            @staticmethod
            def query_devices():
                return [
                    {"name": "HD-Audio Generic", "max_output_channels": 2},
                    {"name": "pulse", "max_output_channels": 32},
                    {"name": "Loopback", "max_output_channels": 2},
                ]
        original = launcher._sounddevice
        launcher._sounddevice = FakeSoundDevice
        try:
            self.assertEqual(self.app._select_output_device(), 1)
        finally:
            launcher._sounddevice = original

    def test_falls_back_to_none_without_a_pulse_device(self):
        class FakeSoundDevice:
            @staticmethod
            def query_devices():
                return [{"name": "HD-Audio Generic", "max_output_channels": 2}]
        original = launcher._sounddevice
        launcher._sounddevice = FakeSoundDevice
        try:
            self.assertIsNone(self.app._select_output_device())
        finally:
            launcher._sounddevice = original

    def test_none_without_sounddevice(self):
        original = launcher._sounddevice
        launcher._sounddevice = None
        try:
            self.assertIsNone(self.app._select_output_device())
        finally:
            launcher._sounddevice = original


class SpeakerVolumeControlTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"no usable display for Tk: {exc}")

    def tearDown(self):
        self.root.destroy()

    def test_changing_the_slider_updates_volume_and_label(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app._on_speaker_volume_change("0.5")
        self.assertAlmostEqual(app._speaker_volume, 0.5, places=6)
        self.assertEqual(app.speaker_volume_label_var.get(), "50%")

    def test_volume_is_clamped_to_the_valid_range(self):
        app = launcher.LauncherApp(self.root)
        self.root.after_cancel(app._after_id)
        app._on_speaker_volume_change("5.0")
        self.assertEqual(app._speaker_volume, 1.0)
        app._on_speaker_volume_change("-1.0")
        self.assertEqual(app._speaker_volume, 0.0)


if __name__ == "__main__":
    unittest.main()
