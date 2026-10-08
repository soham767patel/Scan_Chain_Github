"""Test 1 — smoke test: find the FT232H, enter MPSSE mode, verify sync.

No wiring needed. If this passes, drivers and the MPSSE engine are healthy.
The FT232H is picked by chip type, so a ZCU104 on the same PC is fine.

Run:  python tests/test_01_enumerate.py            # exactly one FT232H
      python tests/test_01_enumerate.py FTxxxxxx   # pick one by serial
Expected output ends with "PASS".
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ft232h_mpsse import (FT232H_MPSSE, FT232H_TYPE, FT232HError,  # noqa: E402
                          list_ftdi_devices)


def main() -> None:
    serial = sys.argv[1] if len(sys.argv) > 1 else None

    devices = list_ftdi_devices()
    print("FTDI devices seen by the D2XX driver:")
    for d in devices:
        kind = "FT232H" if d["type"] == FT232H_TYPE else f"type {d['type']}"
        print(f"  index {d['index']}: {kind:<8} serial={d['serial']!r} "
              f"desc={d['description']!r}{'  (in use)' if d['busy'] else ''}")
    if not devices:
        print("FAIL: no FTDI device found. Check the USB cable and that the "
              "FTDI D2XX driver (not libusb/Zadig) is installed.")
        sys.exit(1)

    # The sync check with bad opcodes happens inside the constructor; an
    # exception here means the engine did not echo 0xFA as required.
    try:
        with FT232H_MPSSE(clock_hz=100_000, verbose=True, serial=serial) as ft:
            print(f"MPSSE up, TCK = {ft.clock_hz:.0f} Hz")
            print("ADBUS reads:", hex(ft.read_pins_low()),
                  "| ACBUS reads:", hex(ft.read_pins_high()))
    except FT232HError as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
