"""Test 3 - board-only scan loopback. NO CHIP NEEDED, NO CHIP POWER NEEDED.

Wiring: jumper the scan_data_in header (C4) to the scan_data_out header (C5).
With the chip socket empty, or the chip unpowered and the jumper isolating it.

This proves your pin map, the shadow register and the read path before any of
it is blamed on the chip. It is the single most useful debug step: if this
fails, the chip is innocent.

Run:  python tests/test_03_scan_loopback.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ece598_board import Board                                    # noqa: E402
from ece598_scan import Scan                                      # noqa: E402


def main() -> None:
    ok = True
    with Board.open() as board:
        print("C4 (scan_data_in) -> C5 (scan_data_out) jumper expected.\n")
        for level in (1, 0, 1, 0):
            board.set(data_in=level)
            got = board.get("data_out")
            print(f"  drove data_in={level}, read data_out={got}")
            ok &= got == level

        if not ok:
            print("\nFAIL: data_out did not follow data_in.")
            print("  Check the jumper, and that C5 is NOT in the output")
            print("  direction mask (ece598_board.C_DIR should be 0xDF).")
            sys.exit(1)

        print("\nNow exercising the two-phase shift waveform on the analyzer.")
        print("Probe: phi (C2), phi_bar (C3), data_in (C4), data_out (C5).")
        scan = Scan(board, length=32, verbose=False)
        board.pulse_trigger()
        pattern = [(0xA5 >> (7 - i % 8)) & 1 for i in range(32)]
        echoed = scan.shift_and_sample(pattern)
        print(f"  shifted {''.join(map(str, pattern))}")
        print(f"  sampled {''.join(map(str, echoed))}")
        print("  (a direct jumper echoes each bit immediately, so these must")
        print("   be identical. With a real chain they differ -- data_out then")
        print("   comes from the far end of the chain, not from data_in.)")
        if echoed != pattern:
            print("\nFAIL: echoed stream does not match what was driven.")
            ok = False

    print("\nPASS" if ok else "\nFAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
