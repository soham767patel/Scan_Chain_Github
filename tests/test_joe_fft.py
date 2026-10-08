import argparse
from contextlib import contextmanager
import math
import sys
from pathlib import Path
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ece598_board import Board
from ece598_scan import DEFAULT_CHAIN_LENGTH, Scan, ScanBus, ScanError
from scan_map import ScanMap
from ft232h_mpsse import FT232HError

REGISTERS = {"point": 0x600, "start": 0x500, "reset": 0x480,
             "done": 0x440, "stage": 0x420}
CLOCK_CONFIG = 0x08  # osc_sel=0, div_sel=2 (/4); not a measured MHz value
MAP_PATH = Path(__file__).resolve().parent.parent / "maps" / "block_scan.txt"


def uint32(text):
    try:
        value = int(text, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use decimal or 0x-prefixed hex") from exc
    if not 0 <= value <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("value must fit in unsigned 32 bits")
    return value


def register(text):
    if text.lower() in REGISTERS:
        return REGISTERS[text.lower()]
    value = uint32(text)
    if value not in REGISTERS.values():
        raise argparse.ArgumentTypeError("use point/start/reset/done/stage or its exact address")
    return value


def positive_seconds(text):
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return value


def add_connection_args(parser):
    parser.add_argument("--serial", default=None)


@contextmanager
def connected(serial=None):
    """Open in reset, configure clock, release global reset; reset again on exit."""
    with Board.open(serial=serial) as board:
        board.set(rst_n=0)
        bus = ScanBus(Scan(board, length=DEFAULT_CHAIN_LENGTH, verbose=False),
                      ScanMap(MAP_PATH))
        bus.write_clk_config(CLOCK_CONFIG)
        if bus.read_clk_config() != CLOCK_CONFIG:
            raise ScanError("clock configuration readback mismatch")
        time.sleep(0.001)
        board.reset_release()
        time.sleep(0.001)
        yield bus


def read_word(bus, addr):
    value, ready = bus.read(addr)  # Group 9 is group 0: no upper group bits
    if ready != 1:
        raise ScanError(f"read at 0x{addr:03X}: ready={ready}, expected 1")
    return value


def check_word(bus, addr, expected):
    got = read_word(bus, addr)
    if got != expected:
        raise ScanError(f"0x{addr:03X}: expected 0x{expected:08X}, got 0x{got:08X}")
    return got


def load_words(path, count):
    """Sequential 32-bit hex words, whitespace separated; // and # comments."""
    words = []
    for line in Path(path).read_text().splitlines():
        for token in line.split("//", 1)[0].split("#", 1)[0].split():
            value = int(token, 16)
            if not 0 <= value <= 0xFFFFFFFF:
                raise ValueError(f"{path}: word does not fit in 32 bits: {token}")
            words.append(value)
    if len(words) != count:
        raise ValueError(f"{path}: expected {count} words, found {len(words)}")
    return words


def run_fft(bus, inputs, expected, stages=9, timeout=30.0):
    """Follow supplied stimulus; stage count is independent of point count."""
    points = len(inputs)
    bus.write(REGISTERS["reset"], 0)
    check_word(bus, REGISTERS["reset"], 0)
    bus.write(REGISTERS["start"], 0)
    bus.write(REGISTERS["point"], points.bit_length() - 1 - 3)
    check_word(bus, REGISTERS["point"], points.bit_length() - 1 - 3)
    bus.write(REGISTERS["stage"], stages - 1)
    check_word(bus, REGISTERS["stage"], stages - 1)
    bus.write(REGISTERS["reset"], 1)
    check_word(bus, REGISTERS["reset"], 1)
    if read_word(bus, REGISTERS["done"]) & 1:
        raise ScanError("done bit is already set before start")

    for addr, value in enumerate(inputs):
        bus.write(addr, value)
    for addr, value in enumerate(inputs):
        check_word(bus, addr, value)
    bus.write(REGISTERS["start"], 1)

    deadline = time.monotonic() + timeout
    while True:
        if time.monotonic() >= deadline:
            raise ScanError(f"FFT done timeout after {timeout:g} seconds")
        if read_word(bus, REGISTERS["done"]) & 1:
            break
        time.sleep(0.01)

    failures = 0
    for addr, want in enumerate(expected):
        got = read_word(bus, addr)
        match = got == want
        failures += not match
        print(f"SRAM[0x{addr:03X}] = 0x{got:08X}, expected 0x{want:08X} "
              f"{'OK' if match else 'MISMATCH'}")
    if failures:
        raise ScanError(f"{failures}/{len(expected)} output words mismatched")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points", type=int, choices=[8, 16, 32, 64, 128, 256, 512, 1024], default=8)
    parser.add_argument("--stages", type=int, choices=range(1, 11), default=9,
                        help="stage count; default 9 from supplied stimulus, written as 8")
    parser.add_argument("--input", type=Path, help="exactly --points packed 32-bit hex input words")
    parser.add_argument("--expected", type=Path, help="exactly --points golden hex output words in SRAM order")
    parser.add_argument("--timeout", type=positive_seconds, default=30.0)
    add_connection_args(parser)
    args = parser.parse_args()
    if (args.input is None) != (args.expected is None):
        parser.error("provide both --input and --expected, or neither for the zero-input smoke test")
    try:
        inputs = load_words(args.input, args.points) if args.input else [0] * args.points
        expected = load_words(args.expected, args.points) if args.expected else [0] * args.points
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Group 9: {args.points} points, {args.stages} stages, clock config 0x08 (osc 0 /4)")
    if args.input is None:
        print("Simple FFT: zero-input smoke test; use golden vectors to validate nonzero FFT arithmetic.")
    try:
        with connected(args.serial) as bus:
            run_fft(bus, inputs, expected, args.stages, args.timeout)
    except (ScanError, FT232HError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("PASS: done asserted and all output words matched; global reset asserted on exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
