"""Test 6 - scan shift-path integrity. Write patterns, read them straight back.

No UPDATE is issued, so nothing reaches the chip's configuration registers:
this exercises the shift path only and is safe with rst_n asserted.

Catches stuck-at bits, shorted neighbours, and off-by-one errors in the bit
map. Pass this before you believe anything test_07 tells you.

Run:  python tests/test_06_scan_integrity.py            # 4 patterns, fast
      python tests/test_06_scan_integrity.py --walking  # + walking 1/0, slow
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ece598_board import Board                                    # noqa: E402
from ece598_scan import DEFAULT_CHAIN_LENGTH, Scan                # noqa: E402


def main() -> None:
    walking = "--walking" in sys.argv
    with Board.open() as board:
        board.reset_assert()
        scan = Scan(board, length=DEFAULT_CHAIN_LENGTH, verbose=True)
        board.pulse_trigger()
        ok = scan.integrity_test(walking=walking)

    if ok:
        print("\nPASS - the shift path is clean over "
              f"{DEFAULT_CHAIN_LENGTH} bits.")
    else:
        print("\nFAIL - see the differing bit positions above.")
        print("  A single differing position that is constant across patterns")
        print("  is a stuck-at bit. A uniform one-position shift is an")
        print("  off-by-one in the chain length. Adjacent pairs that always")
        print("  agree are a short.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
