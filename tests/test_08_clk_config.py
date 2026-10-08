"""Test 8 - write and read the clock-generator config register through scan.

Group 3 (static_addr[19:18] = 11) is a latch inside group_mux.v, so this
works with rst_n held low and no clock at all. It is the quickest end-to-end
check of the whole scan protocol: shift -> UPDATE -> CAPTURE -> shift out.

On the ZCU104 scan design, static_config_clk[3:0] also drives the four user
LEDs (DS38 = bit 0, DS37 = bit 1, DS39 = bit 2, DS40 = bit 3), so you can
watch each value land.

On the chip this changes osc_sel / div_sel while the chip is in reset, which
is harmless. The script finishes by writing the slowest setting (0x13).

Run:  python tests/test_08_clk_config.py [--hold SECONDS] [--serial FT...]
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ece598_board import Board                                    # noqa: E402
from ece598_scan import DEFAULT_CHAIN_LENGTH, Scan, ScanBus       # noqa: E402
from scan_map import ScanMap                                      # noqa: E402

MAP_PATH = Path(__file__).resolve().parent.parent / "maps" / "block_scan.txt"

VALUES = [0x00000001, 0x00000002, 0x00000004, 0x00000008,   # walking 1 on LEDs
          0x0000000F, 0x00000000, 0xA5A5A5A5, 0x5A5A5A5A,
          0xFFFFFFFF, 0x00000013]                           # ends at slowest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=float, default=0.5,
                    help="seconds to pause after each value (to watch LEDs)")
    ap.add_argument("--serial", default=None)
    args = ap.parse_args()

    ok = True
    with Board.open(serial=args.serial) as board:
        board.set(rst_n=0)                  # works in reset: pure latches
        bus = ScanBus(Scan(board, length=DEFAULT_CHAIN_LENGTH, verbose=False),
                      ScanMap(MAP_PATH))
        for value in VALUES:
            bus.write_clk_config(value)
            got = bus.read_clk_config()
            match = got == value
            ok &= match
            print(f"  wrote 0x{value:08X}  read 0x{got:08X}  "
                  f"LEDs={value & 0xF:04b}  {'ok' if match else 'MISMATCH'}")
            time.sleep(args.hold)

    print("\nPASS" if ok else "\nFAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
