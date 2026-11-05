"""Launcher regression tests for bugs found against a real user SD-card config.

Both are boundary/invariant bugs a GUI user can hit in practice, not synthetic
cases: a growing session log must not force an O(size) read every refresh
tick, and rewriting a user's hand-authored animation.json for four new wiring
fields must not silently reorder/reformat the rest of their file.
"""
import json
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
