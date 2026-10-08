"""Test 4 - drive the board to its safe state. RUN THIS BEFORE POWERING THE CHIP.

After MPSSE bring-up every FT232H pin is an INPUT (Hi-Z). The chip's rst_n pad
has an on-die PULL-UP, so a Hi-Z FT232H leaves the chip OUT of reset; the
board's 4.7k pull-down is what makes "held in reset" the default. This script
claims every output and drives it LOW, which is also the only correct state
while VDDPST is at 0 V (the pad's output PMOS body diode goes PAD -> VDDPST,
and this library is not fail-safe).

Run:  python tests/test_04_safe_state.py
Order: run this with the supplies OFF, then bring up VDDPST, then VDD.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ece598_board import Board                                    # noqa: E402


def main() -> None:
    board = Board.open(clock_hz=1_000_000, verbose=True)
    print("\nSafe state applied:")
    print("  rst_n     = 0   (asserted -- clk_gen and the group cores held)")
    print("  bypass_i  = 0   (internal RO selected, so the power-up current")
    print("                   check really is leakage only)")
    print("  clk_i     = 0   phi = 0   phi_bar = 0")
    print("  load_chip / load_chain inactive")
    print("\nPin sample:", board.report_inputs())
    print("\nNow, in this order:")
    print("  1. Enable 3.3 V VDDPST1-4 (all four together). Expect leakage.")
    print("  2. Enable 1.2 V VDD1-4. Blocks are in reset and clockless ->")
    print("     leakage only. Anything large: stop and debug.")
    print("  3. Run tests/test_05_chain_length.py, then _06, then _07.")
    print("\nLeaving the FT232H open would release the pins, so this script")
    print("keeps them driven until you press Enter.")
    try:
        input(">> press Enter to release the pins and exit ")
    finally:
        board.safe_state()
        board.close()
    print("PASS")


if __name__ == "__main__":
    main()
