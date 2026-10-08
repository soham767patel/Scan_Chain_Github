"""Test 2 — blink one GPIO pin (default: C0 / ACBUS0).

Wiring: LED + resistor from the chosen pin to GND, or just probe the pin
with a multimeter / scope / logic analyzer.

Run:  python tests/test_02_gpio_blink.py [pin_bit] [bus]
      pin_bit = 0..7 (default 0), bus = high|low (default high)

Expected: the pin toggles at 1 Hz for 5 seconds (5 full cycles).
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ft232h_mpsse import FT232H_MPSSE                             # noqa: E402


def main() -> None:
    pin_bit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    bus = sys.argv[2] if len(sys.argv) > 2 else "high"
    mask = 1 << pin_bit
    name = ("C" if bus == "high" else "D") + str(pin_bit)

    with FT232H_MPSSE() as ft:
        setter = ft.set_pins_high if bus == "high" else ft.set_pins_low
        print(f"Blinking {name} at 1 Hz for 5 s ...")
        for i in range(10):
            setter(mask if i % 2 == 0 else 0, mask)
            time.sleep(0.5)
        setter(0, 0)  # release the pin (back to input)
    print("PASS (verify the blink on the pin)")


if __name__ == "__main__":
    main()
