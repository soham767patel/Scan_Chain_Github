"""Read one Group 9 register after global reset; optionally check an expected value."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_joe_fft import add_connection_args, connected, register, uint32, read_word
from ece598_scan import ScanError
from ft232h_mpsse import FT232HError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("register", type=register, help="name or exact hex address")
    parser.add_argument("--expect", type=uint32, help="expected decimal or hex value")
    add_connection_args(parser)
    args = parser.parse_args()
    try:
        with connected(args.serial) as bus:
            got = read_word(bus, args.register)
            print(f"Register 0x{args.register:03X} = 0x{got:08X}")
            if args.expect is not None:
                if got != args.expect:
                    raise ScanError(f"expected 0x{args.expect:08X}, got 0x{got:08X}")
                print("PASS")
    except (ScanError, FT232HError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
