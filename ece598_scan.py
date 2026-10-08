"""ECE598 two-phase scan chain over FT232H GPIO.

The chip's scan port is a LATCH-BASED, TWO-PHASE NON-OVERLAPPING chain -- not
JTAG, not a single-clock flop chain.  The fabricated netlist is 228 LHQD1
latches with muxes and zero flip-flops, so:

  * There is no minimum shift rate.  USB jitter is harmless; we do not need
    the MPSSE pacing trick (which would clock the chip -- see
    FT232H_MPSSE.paced_gpio_sequence).
  * The chain has no reset.  It works with rst_n held low, which is exactly
    how you configure the clock before first light.

Three operations, and only three (course RTL: share/block_scan.sv):

    SHIFT    phi then phi_bar, never overlapping          one bit along the chain
    UPDATE   pulse scan_load_chip                         chain -> static_* latches
    CAPTURE  scan_load_chain=1, pulse phi then phi_bar    chip state -> chain

CAPTURE needs the phi/phi_bar pulse because load_chain only selects what the
master latches take (scan_next = load_chain ? scan_load : shifted chain).

Bit order: LSB first.  Chip bit 0 (static_wen) is shifted in first and comes
out of scan_data_out first.  A Python list `bits` therefore uses the chip's
own numbering: bits[i] is chip bit i, for writing and for reading.

On top of the chain, ScanBus replays the chip's own testbench tasks
(group3 design, verilog_scan_clk_top/test/test_scan.sv): write_stuff,
read_stuff and write_clk, including the scan_id handshake.

Chip vs. course tutorial: the chip's share/group_mux.v latches the clock
configuration only while scan_id = 1 (sel_clk & static_wen & scan_id), so a
clock-generator write pulses scan_id 0 -> 1 -> 0 exactly like write_clk. The
tutorial copy of group_mux.sv omits that term and also declares Group_ID one
bit wide, which makes the clock target unreachable -- trust the chip file.
"""

from __future__ import annotations

import time

from ece598_board import (Board, C_DATA_OUT, C_DIR,
                          LOAD_CHAIN_ACTIVE_HIGH, LOAD_CHIP_ACTIVE_HIGH)

# Logical scan chain length per block, from share/block_scan.sv.
# Verify per block with test_05_chain_length.py before trusting a bit map.
DEFAULT_CHAIN_LENGTH = 87

# Bits per USB round trip.  Each read bit produces one result byte and the
# FT232H RX FIFO is 1 KB, so keep this well under 1024.
BITS_PER_CHUNK = 32


class ScanError(Exception):
    pass


class Scan:
    """Shift / update / capture on one block's scan chain."""

    def __init__(self, board: Board, length: int = DEFAULT_CHAIN_LENGTH,
                 scan_id: int = 0, verbose: bool = True):
        self.board = board
        self.length = length
        self.verbose = verbose
        self.scan_id = 1 if scan_id else 0
        self.board.set(scan_id=self.scan_id, phi=0, phi_bar=0, data_in=0)

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"  [scan] {msg}")

    # -- command compilation -------------------------------------------------
    def _shift_states(self, bit: int) -> list[int]:
        r"""Five ACBUS bytes that shift one bit, phases never overlapping.

            din  --<      bit      >--
            phi  ___/---\____________
            phib __________/---\_____
        """
        b = self.board
        return [
            b.c_with(data_in=bit, phi=0, phi_bar=0),   # setup data
            b.c_with(data_in=bit, phi=1, phi_bar=0),   # master transparent
            b.c_with(data_in=bit, phi=0, phi_bar=0),   # master holds
            b.c_with(data_in=bit, phi=0, phi_bar=1),   # slave transparent
            b.c_with(data_in=bit, phi=0, phi_bar=0),   # slave holds -> idle
        ]

    def sample(self) -> int:
        """Read scan_data_out without shifting."""
        return self.board.get("data_out")

    @staticmethod
    def _cmd(states: list[int]) -> bytes:
        out = b""
        for value in states:
            out += bytes([0x82, value & 0xFF, C_DIR])
        return out

    # -- primitives ----------------------------------------------------------
    def update(self) -> None:
        """UPDATE: chain contents -> static_* latches (pulse load_chip)."""
        on, off = (1, 0) if LOAD_CHIP_ACTIVE_HIGH else (0, 1)
        self.board.set(load_chip=off)
        self.board.set(load_chip=on)
        self.board.set(load_chip=off)

    def capture(self) -> None:
        """CAPTURE: live chip state -> scan chain (destroys chain contents).

        Same sequence as the load_chain task in test_scan.sv: raise
        load_chain, clock one phi / phi_bar pair, drop load_chain.
        """
        on, off = (1, 0) if LOAD_CHAIN_ACTIVE_HIGH else (0, 1)
        b = self.board
        states = [
            b.c_with(load_chain=on,  phi=0, phi_bar=0),
            b.c_with(load_chain=on,  phi=1, phi_bar=0),
            b.c_with(load_chain=on,  phi=0, phi_bar=0),
            b.c_with(load_chain=on,  phi=0, phi_bar=1),
            b.c_with(load_chain=on,  phi=0, phi_bar=0),
            b.c_with(load_chain=off, phi=0, phi_bar=0),
        ]
        b.ft.send(self._cmd(states))
        b.adopt_c(states[-1])

    def toggle_scan_id(self) -> None:
        """Flip scan_id. The selected group acts on either edge."""
        self.set_scan_id(self.scan_id ^ 1)

    def set_scan_id(self, level: int) -> None:
        self.scan_id = 1 if level else 0
        self.board.set(scan_id=self.scan_id)

    def shift_bits(self, bits: list[int]) -> None:
        """Shift `bits` in, bits[0] first. Does not UPDATE."""
        for start in range(0, len(bits), BITS_PER_CHUNK):
            chunk = bits[start:start + BITS_PER_CHUNK]
            cmd = b""
            for bit in chunk:
                cmd += self._cmd(self._shift_states(1 if bit else 0))
            self.board.ft.send(cmd)
        if bits:
            # Keep the shadow register in step with the last state we emitted.
            self.board.adopt_c(self._shift_states(1 if bits[-1] else 0)[-1])
            self.board.set(data_in=0)

    def shift_and_sample(self, bits_out: list[int]) -> list[int]:
        """Shift `bits_out` in while sampling scan_data_out.

        Sample BEFORE each shift: the bit currently presented at data_out is
        the one you want; shifting first would lose it.
        Returns one sampled bit per element of `bits_out`.
        """
        sampled: list[int] = []
        for start in range(0, len(bits_out), BITS_PER_CHUNK):
            chunk = bits_out[start:start + BITS_PER_CHUNK]
            cmd = b""
            for bit in chunk:
                states = self._shift_states(1 if bit else 0)
                cmd += bytes([0x82, states[0] & 0xFF, C_DIR])   # idle, data set
                cmd += b"\x83"                                  # <-- sample
                cmd += self._cmd(states[1:])                    # then shift
            cmd += b"\x87"                                      # send-immediate
            self.board.ft.send(cmd)
            raw = self.board.ft.read(len(chunk))
            if len(raw) != len(chunk):
                raise ScanError(f"expected {len(chunk)} result bytes, "
                                f"got {len(raw)}")
            sampled += [(byte >> C_DATA_OUT) & 1 for byte in raw]
        if bits_out:
            self.board.adopt_c(self._shift_states(1 if bits_out[-1] else 0)[-1])
            self.board.set(data_in=0)
        return sampled

    # -- chain-level operations ---------------------------------------------
    def write_chain(self, bits: list[int], update: bool = True) -> None:
        """WRITE = shift the whole chain in, then UPDATE."""
        if len(bits) != self.length:
            raise ScanError(f"got {len(bits)} bits, chain length is "
                            f"{self.length}")
        self._log(f"writing {self.length} bits: {bits_to_str(bits)}")
        self.shift_bits(bits)
        if update:
            self.update()
            self._log("load_chip pulsed (chain -> chip)")

    def read_chain(self, capture: bool = True,
                   shift_in: list[int] | None = None) -> list[int]:
        """READ = CAPTURE, then shift the whole chain out.

        `shift_in` is what gets shifted in behind the data (zeros by default).
        That overwrites the chain, but not the static_* latches: those only
        change on UPDATE.
        """
        if capture:
            self.capture()
            self._log("captured (chip -> chain)")
        bits = self.shift_and_sample(shift_in or [0] * self.length)
        self._log(f"read    {self.length} bits: {bits_to_str(bits)}")
        return bits

    # -- bring-up helpers ----------------------------------------------------
    def detect_length(self, max_length: int = 512) -> int:
        """Measure the chain length by walking a single 1 through it.

        Flush the chain with zeros, push one 1, then count shifts until it
        appears at scan_data_out.  Run this before trusting any bit map --
        it validates the wiring, the polarity of phi/phi_bar and your pin map
        all at once.
        """
        self._log(f"flushing chain with {max_length} zeros")
        self.shift_bits([0] * max_length)
        if self.sample() != 0:
            raise ScanError(
                "scan_data_out is still 1 after flushing the chain with "
                "zeros -- the shift path is broken, or C5 is being driven.")
        self._log("walking a single 1 through the chain ...")
        self.shift_bits([1])                       # one bit pushed
        for pushed in range(1, max_length + 1):
            if self.sample() == 1:
                self._log(f"chain length = {pushed}")
                return pushed
            self.shift_bits([0])                   # push one more
        raise ScanError(
            f"no 1 arrived at scan_data_out within {max_length} shifts.\n"
            f"  Check: pin map, phi/phi_bar polarity, that the block is "
            f"powered, and that C5 is not being driven by the FT232H.")

    def integrity_test(self, walking: bool = False) -> bool:
        """Write patterns and read them straight back (no capture in between).

        This exercises the shift path only, so it is valid with the chip held
        in reset.  Catches stuck-at bits, shorted neighbours and off-by-one
        errors in your bit map.  `walking=True` adds a walking-1 and walking-0
        sweep over every bit position (2*N round trips -- slow but thorough).
        """
        n = self.length
        cases = [
            ("all zeros", [0] * n),
            ("all ones", [1] * n),
            ("0xA5 repeat", [(0xA5 >> (i % 8)) & 1 for i in range(n)]),
            ("alternating", [i % 2 for i in range(n)]),
        ]
        if walking:
            for i in range(n):
                cases.append((f"walking 1 @ {i}",
                              [1 if j == i else 0 for j in range(n)]))
            for i in range(n):
                cases.append((f"walking 0 @ {i}",
                              [0 if j == i else 1 for j in range(n)]))
        ok = True
        for name, bits in cases:
            if self._round_trip(name, bits):
                self._log(f"PASS {name}")
            else:
                ok = False
        return ok

    def _round_trip(self, name: str, bits: list[int]) -> bool:
        self.shift_bits(bits)                       # no UPDATE: chain only
        got = self.shift_and_sample([0] * self.length)
        # After shifting, chip bit i holds bits[i]; bit 0 comes out first.
        if got == bits:
            return True
        diff = [i for i, (a, b) in enumerate(zip(bits, got)) if a != b]
        print(f"  FAIL {name}: {len(diff)} bit(s) differ at {diff[:16]}"
              f"{' ...' if len(diff) > 16 else ''}")
        print(f"       wrote {bits_to_str(bits)}")
        print(f"       read  {bits_to_str(got)}")
        return False


# ---------------------------------------------------------------------------
# The course's scan transactions (test_scan.sv), on top of the chain
# ---------------------------------------------------------------------------
# static_addr[19:18] selects the target (group_mux.sv)
GROUP_A, GROUP_B, GROUP_C, GROUP_CLK = 0, 1, 2, 3


def make_addr(group: int, offset: int = 0, register: bool = False) -> int:
    """Build a 20-bit static_addr.

    [19:18] group (0..2 = groups A..C, 3 = clock generator)
    [11]    0 = SRAM, 1 = control/status registers (mem_reg_mux.sv)
    [10:0]  SRAM word address
    """
    if not 0 <= group <= 3:
        raise ScanError(f"group must be 0..3, got {group}")
    if not 0 <= offset < (1 << 11):
        raise ScanError(f"offset must fit in 11 bits, got {offset}")
    return (group << 18) | ((1 if register else 0) << 11) | offset


class ScanBus:
    """Read and write the chip through the scan chain, like test_scan.sv.

    Needs a verified ScanMap for the 87-bit frame (maps/block_scan.txt).
    Group transactions need the target running: rst_n released and a clock,
    because the group interface (rwctr / syn_pulse_gen) is synchronous.
    Clock-generator writes (group 3) are pure latches and work in reset.
    """

    def __init__(self, scan: Scan, smap, settle_s: float = 0.01):
        smap.require_verified()
        if smap.length != scan.length:
            raise ScanError(f"map is {smap.length} bits, chain is "
                            f"{scan.length}")
        self.scan = scan
        self.smap = smap
        self.settle_s = settle_s

    def frame(self, wen=0, ren=0, addr=0, wdata=0) -> list[int]:
        return self.smap.build(static_wen=wen, static_ren=ren,
                               static_addr=addr, static_wdata=wdata)

    def decode(self, bits: list[int]) -> dict:
        return {name: self.smap.get(bits, name) for name in self.smap.order}

    def write(self, addr: int, data: int) -> None:
        """write_stuff / write_clk from the chip testbench.

        Groups A..C: shift wen=1 + addr + data, UPDATE, toggle scan_id.
        Clock generator: scan_id = 0, shift, UPDATE, then pulse scan_id
        0 -> 1 -> 0 -- the chip's config latch is open only while scan_id = 1.
        """
        clock_target = addr >> 18 == GROUP_CLK
        if clock_target:
            self.scan.set_scan_id(0)
        self.scan.shift_bits(self.frame(wen=1, addr=addr, wdata=data))
        self.scan.update()
        time.sleep(self.settle_s)
        if clock_target:
            self.scan.set_scan_id(1)
            time.sleep(self.settle_s)
            self.scan.set_scan_id(0)
        else:
            self.scan.toggle_scan_id()
        time.sleep(self.settle_s)

    def read(self, addr: int) -> tuple[int, int]:
        """read_stuff: shift ren=1 + addr, UPDATE, toggle scan_id, CAPTURE,
        shift out. Returns (static_rdata, static_ready)."""
        cmd = self.frame(ren=1, addr=addr)
        self.scan.shift_bits(cmd)
        self.scan.update()
        time.sleep(self.settle_s)
        if addr >> 18 != GROUP_CLK:
            self.scan.toggle_scan_id()
            time.sleep(self.settle_s)
        fields = self.decode(self.scan.read_chain(shift_in=cmd))
        return fields["static_rdata"], fields["static_ready"]

    # -- convenience -------------------------------------------------------
    def write_clk_config(self, value: int) -> None:
        """write_clk: static_config_clk <= value (osc_sel [1:0], div_sel [4:2])."""
        self.write(make_addr(GROUP_CLK), value)

    def read_clk_config(self) -> int:
        rdata, ready = self.read(make_addr(GROUP_CLK))
        if not ready:
            raise ScanError("clock-generator read returned ready=0")
        return rdata

    def sram_write(self, word: int, data: int, group: int = GROUP_A) -> None:
        self.write(make_addr(group, word), data)

    def sram_read(self, word: int, group: int = GROUP_A) -> int:
        rdata, ready = self.read(make_addr(group, word))
        if not ready:
            raise ScanError(f"SRAM read of word {word} returned ready=0 "
                            f"(is the target clocked and out of reset?)")
        return rdata


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def bits_to_str(bits: list[int]) -> str:
    s = "".join(str(b) for b in bits)
    return s if len(s) <= 96 else s[:48] + "..." + s[-48:]


def int_to_bits(value: int, width: int) -> list[int]:
    """LSB-first bit list: bits[i] = bit i of value, the chain's order."""
    return [(value >> i) & 1 for i in range(width)]


def bits_to_int(bits: list[int]) -> int:
    """Inverse of int_to_bits (bits[0] is the LSB)."""
    out = 0
    for i, b in enumerate(bits):
        out |= (1 if b else 0) << i
    return out
