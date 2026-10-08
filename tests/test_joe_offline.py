"""Offline control-flow checks for Joe's FFT programs; no USB or RTL simulation."""
import argparse
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tests.test_joe_fft as fft
import test_joe_write_register as write_cli
import test_joe_read_register as read_cli
import tests.test_joe_fft as fft_cli


class Bus:
    def __init__(self, done=True, corrupt=False, ready=1):
        self.words = {}
        self.writes = []
        self.started = False
        self.done = done
        self.corrupt = corrupt
        self.ready = ready

    def write(self, addr, value):
        self.writes.append((addr, value))
        self.words[addr] = value
        if addr == 0x500 and value == 1:
            self.started = True

    def read(self, addr):
        if addr == 0x440:
            return int(self.started and self.done), self.ready
        value = self.words.get(addr, 0)
        if self.started and self.corrupt and addr == 0:
            value ^= 1
        return value, self.ready


class Tests(unittest.TestCase):
    def test_sequence_and_addresses(self):
        bus = Bus()
        with contextlib.redirect_stdout(io.StringIO()):
            fft.run_fft(bus, [0] * 8, [0] * 8)
        self.assertEqual(bus.writes[:5], [(0x480, 0), (0x500, 0),
                                        (0x600, 0), (0x420, 8), (0x480, 1)])
        self.assertEqual(bus.writes[5:13], [(i, 0) for i in range(8)])
        self.assertEqual(bus.writes[-1], (0x500, 1))
        self.assertFalse(any(addr & 0x800 for addr, _ in bus.writes))

    def test_output_mismatch(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(fft.ScanError, "output words mismatched"):
                fft.run_fft(Bus(corrupt=True), [0] * 8, [0] * 8)

    def test_read_not_ready(self):
        with self.assertRaisesRegex(fft.ScanError, "ready=0"):
            fft.run_fft(Bus(ready=0), [0] * 8, [0] * 8)

    def test_timeout(self):
        with patch.object(fft.time, "monotonic", side_effect=[0, 0, 2]), \
                patch.object(fft.time, "sleep"):
            with self.assertRaisesRegex(fft.ScanError, "timeout"):
                fft.run_fft(Bus(done=False), [0] * 8, [0] * 8, timeout=1)

    def test_stale_done(self):
        bus = Bus()
        bus.started = True
        with self.assertRaisesRegex(fft.ScanError, "already set"):
            fft.run_fft(bus, [0] * 8, [0] * 8)

    def test_values_and_files(self):
        self.assertEqual(fft.register("point"), 0x600)
        self.assertEqual(fft.register("0x420"), 0x420)
        for value in ("-1", "0x100000000"):
            with self.assertRaises(argparse.ArgumentTypeError):
                fft.uint32(value)
        with self.assertRaises(argparse.ArgumentTypeError):
            fft.register("0xE00")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "words.hex"
            path.write_text("00000000 // comment\nFFFFFFFF # comment\n")
            self.assertEqual(fft.load_words(path, 2), [0, 0xFFFFFFFF])
            with self.assertRaises(ValueError):
                fft.load_words(path, 8)

    def call_cli(self, module, args, bus):
        with patch.object(sys, "argv", ["test"] + args), \
                patch.object(module, "connected", return_value=contextlib.nullcontext(bus)), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return module.main()

    def test_cli_results(self):
        self.assertEqual(self.call_cli(write_cli, ["stage", "8", "--verify"], Bus()), 0)
        self.assertEqual(self.call_cli(read_cli, ["done", "--expect", "1"], Bus()), 1)
        self.assertEqual(self.call_cli(fft_cli, [], Bus()), 0)
        self.assertEqual(self.call_cli(fft_cli, [], Bus(corrupt=True)), 1)

    def test_clock_before_release_and_cleanup(self):
        from unittest.mock import MagicMock
        board = MagicMock()
        bus = MagicMock()
        bus.read_clk_config.return_value = 8
        events = []
        bus.write_clk_config.side_effect = lambda v: events.append(("clock", v))
        board.reset_release.side_effect = lambda: events.append(("release",))
        with patch.object(fft.Board, "open") as opening, \
                patch.object(fft, "Scan"), patch.object(fft, "ScanBus", return_value=bus), \
                patch.object(fft.time, "sleep"):
            opening.return_value.__enter__.return_value = board
            with fft.connected():
                pass
            self.assertEqual(events, [("clock", 8), ("release",)])
            opening.return_value.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
