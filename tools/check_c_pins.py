"""Check every ACBUS pin (C0..C7) on a freshly soldered FT232H board.

Two independent checks:

1. SHORT / STUCK scan -- automatic, no logic analyzer needed.
   One pin at a time is driven LOW while the other seven are left as inputs.
   An undriven FT232H pin floats HIGH on its internal pull-up, so any other
   pin that reads LOW is electrically tied to the driven one: a solder bridge,
   or a short to ground. Nothing is ever driven against anything else, so
   there is no contention even if a bridge exists.

2. VISUAL patterns -- watch all eight channels on the analyzer.
   Walking one, walking zero, and a binary count. A pin that never moves is
   an open joint; two channels that always move together are bridged.

Wiring: FT232H C0..C7 -> Saleae CH0..CH7, plus GND. Nothing else attached.
Unplug the ZCU104 first, so the ZCU104's FTDI chip is not in the way.

Run:  python tools/check_c_pins.py
      python tools/check_c_pins.py --dwell 0.3      # slower, easier to watch
      python tools/check_c_pins.py --short-only     # skip the visual patterns
      python tools/check_c_pins.py --loop           # repeat until Ctrl+C
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ft232h_mpsse import FT232H_MPSSE, FT232HError, find_ft232h  # noqa: E402

ALL_OUTPUTS = 0xFF
ALL_INPUTS = 0x00


# ---------------------------------------------------------------------------
# check 1: shorts and stuck pins
# ---------------------------------------------------------------------------
def bits(value: int) -> str:
    return " ".join(f"C{b}={(value >> b) & 1}" for b in range(7, -1, -1))


def short_scan(ft: FT232H_MPSSE) -> bool:
    print("\n=== 1. short / stuck scan (no analyzer needed) ===")
    print("    driving one pin LOW at a time, the rest floating HIGH\n")
    ok = True
    for i in range(8):
        ft.set_pins_high(0x00, 1 << i)          # only Ci is an output, held low
        time.sleep(0.01)
        raw = ft.read_pins_high()
        expected = 0xFF & ~(1 << i)
        note = []
        if (raw >> i) & 1:
            note.append(f"C{i} did NOT go low -- open joint on C{i}, or it is "
                        f"shorted to 3.3 V")
            ok = False
        tied = [b for b in range(8) if b != i and not (raw >> b) & 1]
        if tied:
            note.append("also low: " + ", ".join(f"C{b}" for b in tied)
                        + "  -> bridged to C%d, or shorted to GND" % i)
            ok = False
        flag = "ok " if not note else "BAD"
        print(f"  drive C{i}=0 -> read 0b{raw:08b} (expect 0b{expected:08b})"
              f"  {flag}")
        for n in note:
            print(f"        {n}")
    ft.set_pins_high(0x00, ALL_INPUTS)          # release every pin
    print("\n  RESULT: " + ("no shorts or stuck pins detected"
                            if ok else "PROBLEMS ABOVE"))
    print("  Note: a pin shorted to GND looks low in every row.")
    return ok


# ---------------------------------------------------------------------------
# check 2: patterns to watch on the analyzer
# ---------------------------------------------------------------------------
def visual(ft: FT232H_MPSSE, dwell: float, rounds: int) -> None:
    def show(value: int, hold: float) -> None:
        ft.set_pins_high(value, ALL_OUTPUTS)
        time.sleep(hold)

    print("\n=== 2. visual patterns (watch CH0..CH7) ===")

    print("  sync marker: all high, then all low")
    show(0xFF, 0.5)
    show(0x00, 0.5)

    print(f"  walking 1  (C0 -> C7, {rounds} rounds)")
    for _ in range(rounds):
        for b in range(8):
            show(1 << b, dwell)
    show(0x00, dwell)

    print(f"  walking 0  (C0 -> C7, {rounds} rounds)")
    for _ in range(rounds):
        for b in range(8):
            show(0xFF & ~(1 << b), dwell)
    show(0xFF, dwell)
    show(0x00, dwell)

    print("  binary count 0..255")
    for value in range(256):
        show(value, dwell / 5.0)

    ft.set_pins_high(0x00, ALL_INPUTS)          # release every pin
    print("  done, all pins released to inputs")


def estimate(dwell: float, rounds: int) -> float:
    return 1.0 + 2 * rounds * 8 * dwell + 3 * dwell + 256 * dwell / 5.0


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dwell", type=float, default=0.15,
                    help="seconds per step in the visual patterns")
    ap.add_argument("--rounds", type=int, default=2,
                    help="walking-1 / walking-0 repetitions")
    ap.add_argument("--serial", default=None,
                    help="pick a specific FT232H by serial number")
    ap.add_argument("--short-only", action="store_true",
                    help="run only the automatic short scan")
    ap.add_argument("--loop", action="store_true",
                    help="repeat the visual patterns until Ctrl+C")
    args = ap.parse_args()

    try:
        index, serial = find_ft232h(args.serial)
    except FT232HError as exc:
        sys.exit(str(exc))
    print(f"FT232H found: device index {index}, serial {serial!r}")
    print("Saleae mapping: CH0=C0  CH1=C1  ...  CH7=C7   (plus GND)")
    if not args.short_only:
        print(f"Visual sequence takes about {estimate(args.dwell, args.rounds):.0f} s"
              f" -- start the capture first, or just watch it live.")

    with FT232H_MPSSE(ft_id=index, clock_hz=1_000_000) as ft:
        ok = short_scan(ft)
        if not args.short_only:
            try:
                while True:
                    visual(ft, args.dwell, args.rounds)
                    if not args.loop:
                        break
            except KeyboardInterrupt:
                ft.set_pins_high(0x00, ALL_INPUTS)
                print("\n  stopped, pins released")

    print("\nPASS" if ok else "\nFAIL (see the short scan above)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
