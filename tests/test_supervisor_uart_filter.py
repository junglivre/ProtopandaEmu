"""Regression tests for supervisor._drain_uart's LED_DUMP terminal filter.

The raw UART echo to the host terminal is intentional (it's the headless
CLI's only live output), so it must not be silenced wholesale: only the
per-frame [EMU] LED_DUMP hex buffers are held back from the echo, while
the log file keeps every byte and every other line still streams through.
"""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from protopandaemu import supervisor  # noqa: E402


class FakeStream:
    """Mimics the BufferedReader API _drain_uart consumes (read1)."""

    def __init__(self, *chunks: bytes):
        self._chunks = list(chunks)

    def read1(self, _size):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def _make_handle(uart_log: Path) -> supervisor.SessionHandle:
    # _drain_uart only touches _uart_log plus what _observe reads; building
    # those directly avoids needing a real QEMU process for a unit test.
    handle = supervisor.SessionHandle.__new__(supervisor.SessionHandle)
    handle._uart_log = uart_log
    handle._lock = threading.RLock()
    handle._condition = threading.Condition(handle._lock)
    handle._uart_pending = ""
    handle._stderr_pending = ""
    handle._report = {"markers": {name: False for name in supervisor.MARKER_NAMES}}
    return handle


class DrainUartLedDumpFilterTests(unittest.TestCase):
    def _drain(self, *chunks: bytes) -> tuple[bytes, bytes]:
        with tempfile.TemporaryDirectory() as directory:
            uart_log = Path(directory) / "qemu.uart.log"
            handle = _make_handle(uart_log)
            echoed = []
            original = supervisor.SessionHandle._echo_to_stdout
            supervisor.SessionHandle._echo_to_stdout = staticmethod(echoed.append)
            try:
                handle._drain_uart(FakeStream(*chunks))
            finally:
                supervisor.SessionHandle._echo_to_stdout = original
            return uart_log.read_bytes(), b"".join(echoed)

    def test_led_dump_lines_are_held_back_but_still_logged(self):
        log, echoed = self._drain(
            b"boot noise\n",
            b"[EMU] LED_DUMP count=2 hex=ff0000ff00",
            b"00\nmore boot\n",
        )
        self.assertNotIn(b"LED_DUMP", echoed)
        self.assertIn(b"boot noise\n", echoed)
        self.assertIn(b"more boot\n", echoed)
        # The log file keeps every byte, dump included.
        self.assertIn(b"[EMU] LED_DUMP count=2 hex=ff0000ff0000\n", log)

    def test_normal_lines_split_across_read_blocks_are_reassembled(self):
        log, echoed = self._drain(b"[100][I] half a lin", b"e that continued\n")
        self.assertIn(b"[100][I] half a line that continued\n", echoed)
        self.assertEqual(log, b"[100][I] half a line that continued\n")

    def test_trailing_partial_line_still_echoed_at_eof(self):
        _log, echoed = self._drain(b"final partial line without newline")
        self.assertEqual(echoed, b"final partial line without newline")

    def test_trailing_partial_led_dump_is_not_echoed(self):
        # An EOF can land mid-dump; echoing the orphaned fragment would
        # leak a bare wall of hex with no prefix, same reasoning as the
        # GUI panel's own mid-line-seek guard.
        _log, echoed = self._drain(b"boot noise\n[EMU] LED_DUMP count=2 hex=ff00")
        self.assertEqual(echoed, b"boot noise\n")

    def test_quiet_env_still_silences_everything(self):
        original = os.environ.get("PROTOPANDAEMU_QUIET_UART")
        os.environ["PROTOPANDAEMU_QUIET_UART"] = "1"
        try:
            log, echoed = self._drain(b"boot noise\n[EMU] LED_DUMP count=1 hex=ff\n")
        finally:
            if original is None:
                os.environ.pop("PROTOPANDAEMU_QUIET_UART", None)
            else:
                os.environ["PROTOPANDAEMU_QUIET_UART"] = original
        self.assertEqual(echoed, b"")
        self.assertIn(b"LED_DUMP", log)


if __name__ == "__main__":
    unittest.main()
