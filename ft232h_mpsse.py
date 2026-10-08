"""Minimal, reusable FT232H MPSSE driver (ECE598 test kit).

Generic transport layer: it knows about the FT232H and its MPSSE engine, and
nothing about the ECE598 chip. Board-specific pin naming lives in
``ece598_board.py``; the scan protocol lives in ``ece598_scan.py``.

    with FT232H_MPSSE(clock_hz=1_000_000) as ft:   # finds the FT232H itself
        ft.set_pins_high(values=0b0010_0000, directions=0b1101_1111)
        print(hex(ft.read_pins_high()))
        ft.clock_cycles(1000)          # exactly 1000 TCK pulses on ADBUS0

Device selection: the FT232H is found by chip type, never by list position.
A ZCU104 plugged into the same PC adds four FT4232H channels to the FTDI
device list, and channel A (the board's JTAG) is MPSSE-capable -- opening it
by index would drive the FPGA's JTAG lines instead of your FT232H pins.

Pin naming (FTDI datasheet / Adafruit FT232H breakout):
    ADBUS0..7  = "D0..D7" on the breakout  -> the "low byte"  (0x80/0x81)
    ACBUS0..7  = "C0..C7" on the breakout  -> the "high byte" (0x82/0x83)

Install:
    pip install ftd2xx      (Windows: FTDI D2XX driver ships with the default
                             FTDI CDM driver; do NOT replace it with
                             libusb/WinUSB via Zadig)
"""

from __future__ import annotations

import os
import time

import ftd2xx


class FT232HError(Exception):
    pass


# ---------------------------------------------------------------------------
# MPSSE opcode cheat sheet (the subset this kit uses; full list: FTDI AN_108)
# ---------------------------------------------------------------------------
# 0x80 val dir   set ADBUS output values + directions (1 = output)
# 0x82 val dir   set ACBUS output values + directions
# 0x81           read ADBUS pin states (1 result byte)
# 0x83           read ACBUS pin states (1 result byte)
# 0x86 lo hi     set clock divisor: f = 12MHz / ((1 + (hi<<8 | lo)) * 2)
# 0x8B           enable divide-by-5 of the 60MHz master clock (-> 12MHz base)
# 0x8D           disable 3-phase clocking
# 0x97           disable adaptive clocking
# 0x9E 0x00 0x00 disable drive-zero (open-drain) mode on all pins
# 0x85           disable internal loopback
# 0x87           send-immediate (flush read data back to host now)
# 0x8E n         clock (n+1) bits on TCK, no data transfer
# 0x8F lo hi     clock 8*(n+1) bits on TCK, no data transfer
# 0x22 0x00      clock 1 bit IN on the TCK edge -- a *pacer*.  WARNING: this
#                toggles TCK = ADBUS0.  On the ECE598 board ADBUS0 is the chip
#                clock (clk_i), so pacing would clock the chip.  Do not use it
#                with a chip attached -- see paced_gpio_sequence().
# 0xAA / 0xAB    invalid opcodes -- engine echoes 0xFA + the bad opcode;
#                used to verify the MPSSE engine is alive and in sync

# Largest command block handed to a single dev.write().  The FT232H RX FIFO is
# 1 KB, so command streams that *produce* result bytes must be chunked by the
# caller (see ece598_scan.py); this cap only guards the write path.
MAX_WRITE_CHUNK = 4096

# ftd2xx device-type code of the FT232H (FT_DEVICE_232H).
FT232H_TYPE = 8


def list_ftdi_devices() -> list[dict]:
    """Every FTDI device/channel the D2XX driver sees, as plain dicts."""
    out = []
    for i in range(ftd2xx.createDeviceInfoList()):
        info = ftd2xx.getDeviceInfoDetail(i, update=False)
        out.append({
            "index": i,
            "type": info["type"],
            "serial": info["serial"].decode(errors="replace"),
            "description": info["description"].decode(errors="replace"),
            "busy": bool(info["flags"] & 1),     # opened by another process
        })
    return out


def find_ft232h(serial: str | None = None) -> tuple[int, str]:
    """Return (index, serial) of the FT232H to use.

    With serial=None the FT232H_SERIAL environment variable is used if set;
    otherwise there must be exactly one FT232H connected. With two or more,
    set FT232H_SERIAL (or pass serial=) to pick one. Other FTDI chips (for
    example the ZCU104's FT4232H) are never selected.
    """
    if serial is None:
        serial = os.environ.get("FT232H_SERIAL") or None
    devices = list_ftdi_devices()
    found = [d for d in devices if d["type"] == FT232H_TYPE
             and (serial is None or d["serial"] == serial)]
    if len(found) == 1:
        return found[0]["index"], found[0]["serial"]
    listing = "\n".join(
        f"    index {d['index']}: type={d['type']} serial={d['serial']!r} "
        f"desc={d['description']!r}{' (busy)' if d['busy'] else ''}"
        for d in devices) or "    (no FTDI devices at all)"
    if not found:
        want = f"FT232H with serial {serial!r}" if serial else "an FT232H"
        raise FT232HError(f"could not find {want}. FTDI devices seen:\n"
                          f"{listing}\n  Check the FT232H USB cable.")
    raise FT232HError("more than one FT232H is connected; set FT232H_SERIAL "
                      "(or pass serial=) to choose one:\n" + "\n".join(
                          f"    serial={d['serial']!r}" for d in found))


class FT232H_MPSSE:
    """FT232H in MPSSE mode with GPIO + clock-burst primitives."""

    def __init__(self, ft_id: int | None = None, clock_hz: float = 1_000_000,
                 verbose: bool = False, serial: str | None = None):
        """Open the FT232H and bring MPSSE up.

        Leave ft_id=None (the default) so the FT232H is found by chip type.
        Pass serial= when more than one FT232H is connected. An explicit ft_id
        opens that list index unchecked -- only for debugging.
        """
        self.verbose = verbose
        if ft_id is None:
            ft_id, found_serial = find_ft232h(serial)
            self._log(f"found FT232H serial {found_serial!r} at index {ft_id}")
        self._log(f"opening ftd2xx device index {ft_id}")
        self.dev = ftd2xx.open(ft_id)
        self._configure_usb()
        self._enter_mpsse()
        self._sync_mpsse()
        self._configure_engine(clock_hz)

    # -- context manager ----------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        try:
            self.dev.close()
        except Exception:
            pass

    # -- bring-up steps (order matters!) ------------------------------------
    def _configure_usb(self) -> None:
        """Step 1: reset the chip and configure the USB transport."""
        self._log("resetting device, configuring USB transport")
        self.dev.resetDevice()
        pending = self.dev.getQueueStatus()          # drop stale RX bytes
        if pending:
            self.dev.read(pending)
        self.dev.setUSBParameters(65536, 65535)      # request transfer sizes
        self.dev.setChars(False, 0, False, 0)        # no event/error chars
        self.dev.setTimeouts(5000, 5000)             # read/write timeout (ms)
        self.dev.setLatencyTimer(1)                  # 1 ms: snappy small reads

    def _enter_mpsse(self) -> None:
        """Step 2: switch the port into MPSSE mode."""
        self.dev.setBitMode(0x0, 0x00)               # reset mode
        self.dev.setBitMode(0x0, 0x02)               # 0x02 = MPSSE

    def _sync_mpsse(self) -> None:
        """Step 3: prove the engine is alive using deliberate bad opcodes."""
        self.flush()
        for bad in (b"\xAA", b"\xAB"):
            self.send(bad)
            echo = self.read(2)
            if echo != b"\xFA" + bad:
                raise FT232HError(f"MPSSE sync failed on {bad!r}: got {echo!r}")
        self._log("MPSSE engine synchronized")

    def _configure_engine(self, clock_hz: float) -> None:
        """Step 4: clocking + misc engine settings, then pin defaults."""
        self.send(b"\x8B"            # divide-by-5 -> 12 MHz base clock
                  b"\x8D"            # no 3-phase clocking
                  b"\x97"            # no adaptive clocking
                  b"\x9E\x00\x00"    # no drive-zero (push-pull outputs)
                  b"\x85")           # loopback off
        self.set_clock_hz(clock_hz)
        # Default: every pin an input (Hi-Z) until the caller claims it.
        # NOTE for ECE598: the chip's rst_n pad has an on-die PULL-UP, so a
        # Hi-Z FT232H leaves the chip OUT of reset.  The board's 4.7k pull-down
        # is what holds it in reset; the first thing your script must do is
        # claim rst_n as an output and drive it 0 (Board.safe_state()).
        self.set_pins_low(0x00, 0x00)
        self.set_pins_high(0x00, 0x00)

    # -- clocking ------------------------------------------------------------
    def set_clock_hz(self, clock_hz: float) -> float:
        """Set TCK frequency; returns the actual achieved frequency."""
        divisor = max(0, round(12e6 / (2 * clock_hz)) - 1)
        divisor = min(divisor, 0xFFFF)
        self.send(bytes([0x86, divisor & 0xFF, divisor >> 8]))
        actual = 12e6 / ((1 + divisor) * 2)
        self._log(f"clock divisor {divisor} -> TCK {actual:.0f} Hz")
        self.clock_hz = actual
        return actual

    def clock_cycles(self, n: int) -> None:
        """Emit exactly `n` TCK pulses on ADBUS0, with no data transfer.

        This is how the ECE598 kit drives the chip's clk_i in bypass mode: a
        known frequency (set_clock_hz) and an exact, repeatable cycle count,
        which is what lets you do

            reset -> scan in stimulus -> clock N cycles -> capture -> scan out

        and compare against an RTL simulation.  ADBUS0 must already be
        configured as an output (Board.safe_state() does that).
        """
        if n <= 0:
            return
        cmd = b""
        whole, rem = divmod(int(n), 8)
        while whole > 0:
            blk = min(whole, 0x10000)                # 0x8F length field is n-1
            cmd += bytes([0x8F, (blk - 1) & 0xFF, (blk - 1) >> 8])
            whole -= blk
        if rem:
            cmd += bytes([0x8E, rem - 1])            # 0x8E length field is n-1
        self.send(cmd)

    # -- GPIO ----------------------------------------------------------------
    def set_pins_low(self, values: int, directions: int) -> None:
        """Drive ADBUS (D0..D7). direction bit 1 = output."""
        self.send(bytes([0x80, values & 0xFF, directions & 0xFF]))

    def set_pins_high(self, values: int, directions: int) -> None:
        """Drive ACBUS (C0..C7). direction bit 1 = output."""
        self.send(bytes([0x82, values & 0xFF, directions & 0xFF]))

    def read_pins_low(self) -> int:
        """Sample ADBUS pin states (inputs and outputs alike)."""
        self.send(b"\x81\x87")
        return self.read(1)[0]

    def read_pins_high(self) -> int:
        """Sample ACBUS pin states."""
        self.send(b"\x83\x87")
        return self.read(1)[0]

    # -- paced multi-write (legacy bit-bang trick) ---------------------------
    def paced_gpio_sequence(self, states: list[int], directions: int,
                            bus: str = "high", pace_bits: int = 1) -> None:
        """Apply a sequence of GPIO states, one per TCK bit period.

        Between consecutive states we insert `pace_bits` dummy clocked-read
        commands (0x22 0x00), each of which burns exactly one bit period at the
        configured clock.  That gives a constant-rate bit-banged waveform.

        *** DO NOT USE THIS ON THE ECE598 BOARD WITH A CHIP ATTACHED. ***
        0x22 toggles TCK = ADBUS0 = the chip clk_i.  Pacing would clock the
        chip once per pin update.  It is unnecessary here anyway: the ECE598
        scan chain is level-sensitive latches with no minimum shift rate, so
        unpaced GPIO writes are safe (ece598_scan.py relies on that).

        Kept for reference and for boards whose serial port needs even timing.
        """
        opcode = 0x82 if bus == "high" else 0x80
        # Chunk so we never queue more result bytes than the 1 KB RX FIFO holds.
        per_chunk = max(1, 512 // max(1, pace_bits))
        for start in range(0, len(states), per_chunk):
            block = states[start:start + per_chunk]
            chunk = b""
            for state in block:
                chunk += b"\x22\x00" * pace_bits
                chunk += bytes([opcode, state & 0xFF, directions & 0xFF])
            self.send(chunk + b"\x87")
            self.flush_after(len(block) * pace_bits)

    def flush_after(self, expected_bytes: int) -> None:
        """Drain the read bytes produced by dummy clocked reads."""
        deadline = time.time() + 5.0
        while self.dev.getQueueStatus() < expected_bytes:
            if time.time() > deadline:
                break
            time.sleep(0.005)
        self.flush()

    # -- raw transport -------------------------------------------------------
    def send(self, commands: bytes) -> int:
        total = 0
        for start in range(0, len(commands), MAX_WRITE_CHUNK):
            block = bytes(commands[start:start + MAX_WRITE_CHUNK])
            written = self.dev.write(block)
            if written != len(block):
                raise FT232HError(f"short write: {written}/{len(block)}")
            total += written
        return total

    def read(self, length: int, timeout_s: float = 2.0) -> bytes:
        deadline = time.time() + timeout_s
        while self.dev.getQueueStatus() < length:
            if time.time() > deadline:
                raise FT232HError(
                    f"read timeout waiting for {length} byte(s); got "
                    f"{self.dev.getQueueStatus()}. Did you forget 0x87, or "
                    f"leave earlier result bytes undrained?")
            time.sleep(0.002)
        return self.dev.read(length)

    def flush(self) -> None:
        pending = self.dev.getQueueStatus()
        if pending:
            self.dev.read(pending)

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[ft232h] {msg}")
