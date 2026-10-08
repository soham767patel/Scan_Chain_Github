"""Test 9 - SRAM write / read test through the scan chain.

This is the course testbench's write_stuff / read_stuff (test_scan.sv), run
on real hardware: every word goes in through the 87-bit chain, is written by
the group's rwctr on a scan_id toggle, and comes back through CAPTURE.

The group interface is synchronous, so the target must be clocked and out
of reset. The script releases rst_n itself. On the chip, run test_07 first
so the clock is configured; on the ZCU104 scan design the FPGA supplies the
clock.

Patterns:
  address-in-data   word i holds 0xA5000000 | i   (catches address aliasing)
  walking ones      one bit set per word           (catches stuck data bits)
  control register  write {cr}, read back {cr, sr}

Run:  python tests/test_09_sram_rw.py [--words N] [--group 0..2] [--serial FT...]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ece598_board import Board                                    # noqa: E402
from ece598_scan import (DEFAULT_CHAIN_LENGTH, Scan, ScanBus,     # noqa: E402
                         ScanError, make_addr)
from scan_map import ScanMap                                      # noqa: E402

MAP_PATH = Path(__file__).resolve().parent.parent / "maps" / "block_scan.txt"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--words", type=int, default=32,
                    help="how many SRAM words to test (max 2048)")
    ap.add_argument("--group", type=int, default=0, choices=(0, 1, 2),
                    help="0 = group A, 1 = B, 2 = C")
    ap.add_argument("--serial", default=None)
    args = ap.parse_args()
    n = max(1, min(args.words, 2048))

    patterns = {
        "address-in-data": [0xA5000000 | i for i in range(n)],
        "walking ones": [1 << (i % 32) for i in range(n)],
    }

    fails = 0
    with Board.open(serial=args.serial) as board:
        bus = ScanBus(Scan(board, length=DEFAULT_CHAIN_LENGTH, verbose=False),
                      ScanMap(MAP_PATH))
        board.reset_pulse(hold_s=0.001)     # clean group state, then run
        print(f"Group {'ABC'[args.group]}, {n} SRAM words\n")

        for name, data in patterns.items():
            for i, d in enumerate(data):
                bus.sram_write(i, d, group=args.group)
            bad = []
            for i, d in enumerate(data):
                try:
                    got = bus.sram_read(i, group=args.group)
                except ScanError as exc:
                    print(f"  FAIL {name}: {exc}")
                    sys.exit(1)
                if got != d:
                    bad.append((i, d, got))
            fails += len(bad)
            print(f"  {name:<16} {n - len(bad)}/{n} words ok")
            for i, d, got in bad[:8]:
                print(f"      word {i:4d}: wrote 0x{d:08X}  read 0x{got:08X}")

        cr = 0x0400
        bus.write(make_addr(args.group, register=True), cr << 15)
        rdata, ready = bus.read(make_addr(args.group, register=True))
        reg_ok = ready == 1 and (rdata >> 15) == cr
        fails += 0 if reg_ok else 1
        print(f"  {'control register':<16} wrote cr=0x{cr:05X}  read "
              f"cr=0x{rdata >> 15:05X} sr=0x{rdata & 0x7FFF:04X} "
              f"ready={ready}  {'ok' if reg_ok else 'MISMATCH'}")

    print("\nPASS" if fails == 0 else f"\nFAIL ({fails} mismatches)")
    sys.exit(0 if fails == 0 else 1)


if __name__ == "__main__":
    main()
