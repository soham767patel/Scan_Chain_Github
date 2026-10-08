"""Write one Group 9 register. Opening/closing the board asserts global reset."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_joe_fft import (REGISTERS, add_connection_args, connected, register,
                     uint32, check_word)
from ece598_scan import ScanError
from ft232h_mpsse import FT232HError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("register", type=register, help="name or exact hex address")
    parser.add_argument("value", type=uint32, help="decimal or 0x-prefixed hex")
    parser.add_argument("--verify", action="store_true", help="check immediate readback")
    add_connection_args(parser)
    args = parser.parse_args()
    if args.register == REGISTERS["done"]:
        parser.error("done is a read-only status register")
    try:
        with connected(args.serial) as bus:
            bus.write(args.register, args.value)
            print(f"Wrote 0x{args.value:08X} to register 0x{args.register:03X}")
            if args.verify:
                check_word(bus, args.register, args.value)
                print("Readback PASS")
    except (ScanError, FT232HError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
