"""Test 5 - measure the scan chain length by walking a single 1 through it.

Do this BEFORE trusting any bit map. It validates, in one shot: the pin map,
the polarity of phi / phi_bar, that the block is powered, and that the shift
path from scan_data_in to scan_data_out is intact.

The chain is reset-less latches, so run it with rst_n asserted (the safe
state). Nothing in the chip needs to be running.

Expected: 87 for every block (block_scan.v). A different number means the map
is wrong, or you are talking to the wrong block.

Run:  python tests/test_05_chain_length.py [max_length]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ece598_scan import DEFAULT_CHAIN_LENGTH, Scan, ScanError     # noqa: E402
from ece598_board import Board                                    # noqa: E402

EXPECTED = DEFAULT_CHAIN_LENGTH


def main() -> None:
    max_length = int(sys.argv[1]) if len(sys.argv) > 1 else 512
    with Board.open() as board:
        board.reset_assert()          # chain works in reset; keep it there
        scan = Scan(board, length=EXPECTED)
        board.pulse_trigger()
        try:
            n = scan.detect_length(max_length=max_length)
        except ScanError as exc:
            print(f"\nFAIL: {exc}")
            sys.exit(1)

    print(f"\nMeasured chain length: {n}")
    if n == EXPECTED:
        print(f"PASS (matches block_scan.v = {EXPECTED})")
    else:
        print(f"MISMATCH: expected {EXPECTED} from block_scan.v.")
        print("  Either the map is stale, or scan_id / the block select is")
        print("  pointing somewhere you did not intend. Do not proceed to")
        print("  test_07 until this matches.")
        sys.exit(1)


if __name__ == "__main__":
    main()
