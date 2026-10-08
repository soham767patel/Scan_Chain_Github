"""Test 7 - configure the clock in reset, then release rst_n. FIRST LIGHT.

Why this order (from the netlist review):
  * rst_n resets clk_gen (ring oscillator + dividers + slow_clk_o) and the
    group cores -- and nothing else.
  * The scan chain and static_config_clk are reset-less latches, so scanning
    works while rst_n is low, and the configuration survives later resets.
  * Therefore the RO's frequency setting is RANDOM at power-up. Releasing
    rst_n before configuring it starts the oscillator at an unknown
    osc_sel/div_sel. Always scan the clock config in first.

Clock configuration (course RTL clk_gen.v / rosc.v / div_mux.v):
    static_config_clk[1:0] = osc_sel   0 = fastest RO ... 3 = slowest RO
    static_config_clk[4:2] = div_sel   000 /1, 001 /2, 010 /4, 011 /8, 1xx /16
The default (osc_sel=3, div_sel=100) is the slowest clock the chip can make.

What you should see:
  * Before release: slow_clk_o = 0 (it is the one thing actively cleared).
    The internal clocks park HIGH in reset -- if you scope clk_o and see a
    steady 1, that is correct, not a fault.
  * After release: slow_clk_o toggles. That is first light.
    slow_clk_o = chip clock / 1024 (bypass_mux.v), so multiply the frequency
    you measure on the Saleae by 1024 to get the chip clock.

Prerequisites: test_04 (safe state) -> supplies up -> test_05 -> test_06 pass.

Run:  python tests/test_07_first_light.py [--osc N] [--div N] [--external]
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ece598_board import Board                                    # noqa: E402
from ece598_scan import DEFAULT_CHAIN_LENGTH, Scan, ScanBus       # noqa: E402
from scan_map import ScanMap, ScanMapError                        # noqa: E402

MAP_PATH = Path(__file__).resolve().parent.parent / "maps" / "block_scan.txt"

SLOWEST_OSC_SEL = 3        # longest delay tap in delay_mux.v
SLOWEST_DIV_SEL = 0b100    # div_sel[2] = 1 selects /16 in div_mux.v


def watch_slow_clk(board: Board, seconds: float = 1.0) -> set[int]:
    """Poll slow_clk_o and report which levels were seen.

    Polling over USB is far too slow to MEASURE a frequency -- use the Saleae
    for that. It is fast enough to prove the pin is toggling.
    """
    seen = set()
    deadline = time.time() + seconds
    while time.time() < deadline:
        seen.add(board.get("slow_clk_o"))
        if len(seen) == 2:
            break
    return seen


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--osc", type=int, default=SLOWEST_OSC_SEL,
                    help="osc_sel 0..3 (3 = slowest)")
    ap.add_argument("--div", type=int, default=SLOWEST_DIV_SEL,
                    help="div_sel 0..7 (0 = /1, 1 = /2, 2 = /4, 3 = /8, 4+ = /16)")
    ap.add_argument("--external", action="store_true",
                    help="after first light, switch to the external clk_i and "
                         "issue a burst of exactly --cycles pulses")
    ap.add_argument("--cycles", type=int, default=1000)
    args = ap.parse_args()
    if not (0 <= args.osc <= 3 and 0 <= args.div <= 7):
        sys.exit("osc must be 0..3 and div must be 0..7")
    config = (args.div << 2) | args.osc

    smap = ScanMap(MAP_PATH)
    try:
        smap.require_verified()
    except ScanMapError as exc:
        print(f"REFUSING TO RUN:\n{exc}")
        sys.exit(2)

    with Board.open(clock_hz=1_000_000, verbose=True) as board:
        board.set(rst_n=0, bypass_i=0, clk_i=0)
        scan = Scan(board, length=DEFAULT_CHAIN_LENGTH)
        bus = ScanBus(scan, smap)

        print(f"\nslow_clk_o in reset: {board.get('slow_clk_o')} "
              f"(expected 0 -- it is the only thing actively cleared)")

        print(f"\nScanning in static_config_clk = 0x{config:02X} "
              f"(osc_sel={args.osc}, div_sel={args.div:03b}), still in reset")
        board.pulse_trigger()
        bus.write_clk_config(config)
        readback = bus.read_clk_config()
        print(f"Read back: 0x{readback:02X}")
        if readback & 0x1F != config:
            print("FAIL - the configuration did not stick. Stop here and "
                  "re-run test_05 / test_06.")
            sys.exit(1)

        print("\nReleasing rst_n with the clock stopped ...")
        board.reset_release()
        seen = watch_slow_clk(board, seconds=1.0)

        if seen == {0, 1}:
            print("PASS - slow_clk_o is toggling. FIRST LIGHT.")
            print("  Log: slow_clk_o frequency x 1024 = chip clock, and the")
            print("  per-rail current, for this osc_sel/div_sel setting.")
        else:
            print(f"FAIL - slow_clk_o stuck at {seen}.")
            print("  The configuration read back correctly, so suspect the")
            print("  ring oscillator itself: check VDD, then try --external.")

        if args.external:
            print(f"\nSwitching to the external clock and issuing exactly "
                  f"{args.cycles} pulses.")
            print("  (the clock mux is not glitch-free, so this happens in "
                  "reset with clk_i stopped)")
            board.select_clock_source(external=True)
            board.reset_release()
            board.pulse_trigger()
            board.clock_cycles(args.cycles)
            print("  Done. NOTE: with bypass_i=1 the core clock tree toggles")
            print("  even in reset -- return bypass_i to 0 before any")
            print("  leakage-current measurement.")
            board.select_clock_source(external=False)

        board.reset_assert()
        print("\nrst_n re-asserted. Power-down order: rst_n low (done) ->")
        print("all FT232H outputs low (on exit) -> VDD1-4 to 0 -> VDDPST to 0.")


if __name__ == "__main__":
    main()
