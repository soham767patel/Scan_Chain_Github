# Modified from test_08_clk_config.py
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ece598_board import Board
from ece598_scan import DEFAULT_CHAIN_LENGTH, Scan, ScanBus
from scan_map import ScanMap

MAP_PATH = Path(__file__).resolve().parent.parent / "maps" / "block_scan.txt"
GROUP9_CLK_CONFIG = 0x00000008  # osc_sel=0, div_sel=2 (divide by 4)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default=None)
    args = ap.parse_args()

    with Board.open(serial=args.serial) as board:
        board.set(rst_n=0)
        bus = ScanBus(
            Scan(board, length=DEFAULT_CHAIN_LENGTH, verbose=False),
            ScanMap(MAP_PATH),
        )

        bus.write_clk_config(GROUP9_CLK_CONFIG)
        got = bus.read_clk_config()
        ok = got == GROUP9_CLK_CONFIG  # check read back

        print(
            f"Group 9 clock: wrote 0x{GROUP9_CLK_CONFIG:08X}, "
            f"read 0x{got:08X} — {'PASS' if ok else 'FAIL'}"
        )

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
