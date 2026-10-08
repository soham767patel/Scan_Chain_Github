import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ece598_board import Board
from ece598_scan import DEFAULT_CHAIN_LENGTH, Scan, ScanBus, ScanError, make_addr
from scan_map import ScanMap

MAP_PATH = Path(__file__).resolve().parent.parent / "maps" / "block_scan.txt"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--words", type=int, default=32)
    ap.add_argument("--group", type=int, default=0, choices=(0, 1, 2))
    ap.add_argument("--serial", default=None)
    args = ap.parse_args()

    n = max(1, min(args.words, 2048))

    with Board.open(serial=args.serial) as board:
        bus = ScanBus(
            Scan(board, length=DEFAULT_CHAIN_LENGTH, verbose=False),
            ScanMap(MAP_PATH)
        )

        board.reset_pulse(hold_s=0.001)

        fails = 0
        #first write all the values
        for i in range(n):
        
            data = 0xA5000000 | i
            addr = make_addr(args.group, offset=i)
            bus.write(addr, data) 
            print(f"word {i:4d}: wrote 0x{data:08X}")
            
        for i in range(n):
            expected = 0xA5000000 | i

            try:
                got = bus.sram_read(i, group=args.group)
            except ScanError as exc:
                print(f"ERROR reading word {i}: {exc}")
                sys.exit(1)

            if got == expected:
                print(f"word {i:4d}: expected 0x{expected:08X}, "
                      f"read 0x{got:08X}  OK")
            else:
                print(f"word {i:4d}: expected 0x{expected:08X}, "
                      f"read 0x{got:08X}  FAIL")
                fails += 1

        print(f"\n{n - fails}/{n} words correct")

        if fails == 0:
            print("PASS")
        else:
            print(f"FAIL ({fails} mismatches)")


if __name__ == "__main__":
    main()