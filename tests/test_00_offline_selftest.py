"""Test 0 - offline self-test. NO HARDWARE, NO CHIP, NO FT232H.

Injects a fake ftd2xx module that simulates the MPSSE command interpreter and
the ECE598 scan logic, then runs the real Board / Scan / ScanBus / ScanMap
code against it. Catches pin-map, bit-order, protocol and command-encoding
bugs on a laptop before anyone touches silicon.

The chip model follows the chip RTL (group3 design, verilog_scan_clk_top/share)
and the course group interface (w26/tutorial/A_Team_Scan_Chain):
  block_scan      87-bit master/slave latch chain, load_chain / load_chip
  group_mux       static_addr[19:18] selects group A..C or the clock config;
                  the config latch is open only while scan_id = 1
  group A         scan_id toggle -> SRAM or control-register access
                  (rwctr / mem_reg_mux / spram / cs_reg, with the clock
                  abstracted away: an access completes on the toggle)

The fake USB bus also carries a ZCU104's four FT4232H channels, so the test
proves the kit opens the FT232H and never the FPGA board's JTAG channel.

Students: run this first. If it fails, the kit is broken, not your wiring.

Run:  python tests/test_00_offline_selftest.py
"""

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CHAIN_LEN = 87

# --- ACBUS pin positions (must agree with ece598_board) ----------------------
BIT_LOAD_CHAIN, BIT_LOAD_CHIP, BIT_PHI, BIT_PHI_BAR = 0, 1, 2, 3
BIT_DATA_IN, BIT_DATA_OUT, BIT_RST_N, BIT_SCAN_ID = 4, 5, 6, 7


def field(bits, lsb, width):
    return sum((bits[lsb + k] & 1) << k for k in range(width))


def put(bits, lsb, width, value):
    for k in range(width):
        bits[lsb + k] = (value >> k) & 1


class FakeChip:
    """block_scan + group_mux + one group (A) with SRAM and registers."""

    def __init__(self):
        self.master = [0] * CHAIN_LEN
        self.slave = [0] * CHAIN_LEN
        self.static = {"wen": 0, "ren": 0, "addr": 0, "wdata": 0}
        self.config_clk = 0                 # group_mux static_config_clk latch
        self.sram = {}
        self.cr, self.sr = 0, 0x1234        # cs_reg (sr written by the "core")
        self.rdata_a, self.ready_a = 0, 0   # rwctr outputs
        self.scan_id_a = 0
        self.rst_n = 0

    # group_mux.sv -----------------------------------------------------------
    def group(self):
        return (self.static["addr"] >> 18) & 3

    def static_rdata(self):
        g = self.group()
        if g == 0:
            return self.rdata_a
        if g == 3 and self.static["ren"]:
            return self.config_clk
        return 0

    def static_ready(self):
        g = self.group()
        return self.ready_a if g == 0 else (1 if g == 3 else 0)

    def scan_load(self):
        bits = [0] * CHAIN_LEN
        put(bits, 0, 1, self.static["wen"])
        put(bits, 1, 1, self.static["ren"])
        put(bits, 2, 20, self.static["addr"])
        put(bits, 22, 32, self.static["wdata"])
        put(bits, 54, 32, self.static_rdata())
        put(bits, 86, 1, self.static_ready())
        return bits

    # pin behaviour ----------------------------------------------------------
    def pins(self, phi, phi_bar, data_in, load_chain, load_chip, rst_n,
             scan_id):
        if phi and phi_bar:
            raise AssertionError("phi and phi_bar high at the same time -- "
                                 "the two-phase clocks must never overlap")
        if phi:                              # master transparent
            self.master = (self.scan_load() if load_chain
                           else self.slave[1:] + [data_in])
        if phi_bar:                          # slave transparent
            self.slave = list(self.master)
        if load_chip:                        # static_* latches transparent
            self.static = {"wen": self.slave[0], "ren": self.slave[1],
                           "addr": field(self.slave, 2, 20),
                           "wdata": field(self.slave, 22, 32)}
        if self.group() == 3 and self.static["wen"] and scan_id:
            self.config_clk = self.static["wdata"]      # chip group_mux.v
        self.rst_n = rst_n
        if not rst_n:                        # rwctr / cs_reg in reset
            self.rdata_a, self.ready_a = 0, 0
        if self.group() == 0:                # scan_id_A follows while selected
            if scan_id != self.scan_id_a and rst_n:
                self.group_a_access()
            self.scan_id_a = scan_id

    def group_a_access(self):
        s = self.static
        if not (s["wen"] or s["ren"]):
            return
        is_reg = (s["addr"] >> 11) & 1
        word = s["addr"] & 0x7FF
        self.ready_a = 0
        if s["wen"]:
            if is_reg:
                self.cr = (s["wdata"] >> 15) & 0x1FFFF
            else:
                self.sram[word] = s["wdata"]
        elif s["ren"]:
            self.rdata_a = ((self.cr << 15) | self.sr) if is_reg \
                else self.sram.get(word, 0)
            self.ready_a = 1

    @property
    def data_out(self):
        return self.slave[0]


class FakeDevice:
    """Just enough MPSSE to run this kit."""

    FIXED = {0x8B: 1, 0x8D: 1, 0x97: 1, 0x85: 1, 0x8A: 1,
             0x86: 3, 0x9E: 3, 0x8E: 2, 0x8F: 3}

    def __init__(self):
        self.rx = bytearray()
        self.c_val = self.c_dir = self.d_val = self.d_dir = 0
        self.chip = FakeChip()
        self.tck_pulses = 0

    # -- ftd2xx surface -----------------------------------------------------
    def resetDevice(self): self.rx.clear()
    def setUSBParameters(self, *a): pass
    def setChars(self, *a): pass
    def setTimeouts(self, *a): pass
    def setLatencyTimer(self, *a): pass
    def setBitMode(self, *a): pass
    def getQueueStatus(self): return len(self.rx)
    def close(self): pass

    def read(self, length):
        out = bytes(self.rx[:length])
        del self.rx[:length]
        return out

    def write(self, data):
        data = bytes(data)
        i = 0
        while i < len(data):
            op = data[i]
            if op in (0x80, 0x82):
                val, direction = data[i + 1], data[i + 2]
                i += 3
                if op == 0x80:
                    self.d_val, self.d_dir = val, direction
                else:
                    self._acbus(val, direction)
            elif op == 0x81:
                self.rx.append(self.d_val & self.d_dir)
                i += 1
            elif op == 0x83:
                self.rx.append(self._acbus_read())
                i += 1
            elif op == 0x87:
                i += 1
            elif op in (0xAA, 0xAB):
                self.rx += bytes([0xFA, op])
                i += 1
            elif op == 0x22:
                self.rx.append(0x00)
                i += 2
            elif op in self.FIXED:
                if op == 0x8E:
                    self.tck_pulses += data[i + 1] + 1
                elif op == 0x8F:
                    self.tck_pulses += 8 * ((data[i + 2] << 8 | data[i + 1]) + 1)
                i += self.FIXED[op]
            else:
                raise AssertionError(f"simulator saw unknown opcode 0x{op:02X}")
        return len(data)

    def _acbus(self, val, direction):
        if direction & (1 << BIT_DATA_OUT):
            raise AssertionError("FT232H is driving C5 = scan_data_out; that "
                                 "is contention with the chip")
        self.c_val, self.c_dir = val, direction
        b = lambda n: (val >> n) & 1
        self.chip.pins(phi=b(BIT_PHI), phi_bar=b(BIT_PHI_BAR),
                       data_in=b(BIT_DATA_IN), load_chain=b(BIT_LOAD_CHAIN),
                       load_chip=b(BIT_LOAD_CHIP), rst_n=b(BIT_RST_N),
                       scan_id=b(BIT_SCAN_ID))

    def _acbus_read(self):
        return (self.c_val & self.c_dir) | (self.chip.data_out << BIT_DATA_OUT)


# --- fake USB bus: a ZCU104 (4 x FT4232H channels) plus our FT232H ----------
DEVICE = FakeDevice()
FT232H_INDEX = 4
BUS = [
    {"type": 3, "serial": b"", "description": b"", "flags": 1},  # JTAG, in use
    {"type": 7, "serial": b"99734B", "description": b"JTAG+3Serial B", "flags": 0},
    {"type": 7, "serial": b"99734C", "description": b"JTAG+3Serial C", "flags": 0},
    {"type": 7, "serial": b"99734D", "description": b"JTAG+3Serial D", "flags": 0},
    {"type": 8, "serial": b"FTSIM001", "description": b"FT232H", "flags": 0},
]


def fake_open(index=0):
    if index != FT232H_INDEX:
        raise AssertionError(f"kit tried to open FTDI index {index}, which is "
                             f"not the FT232H -- it would drive the ZCU104")
    return DEVICE


fake = types.ModuleType("ftd2xx")
fake.open = fake_open
fake.createDeviceInfoList = lambda: len(BUS)
fake.getDeviceInfoDetail = lambda i, update=False: dict(BUS[i])
fake.listDevices = lambda *a: [d["serial"] for d in BUS]
fake.defines = types.SimpleNamespace(OPEN_BY_SERIAL_NUMBER=1,
                                     OPEN_BY_DESCRIPTION=2)
sys.modules["ftd2xx"] = fake

from ece598_board import Board, C_DIR, D_DIR                    # noqa: E402
from ece598_scan import (GROUP_A, Scan, ScanBus, ScanError,     # noqa: E402
                         bits_to_int, int_to_bits, make_addr)
from ft232h_mpsse import FT232HError, find_ft232h               # noqa: E402
from scan_map import ScanMap                                    # noqa: E402

MAP = Path(__file__).resolve().parent.parent / "maps" / "block_scan.txt"


def check(label, condition):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    return bool(condition)


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    return False


def main() -> None:
    ok = True
    os.environ.pop("FT232H_SERIAL", None)     # the fake bus decides, not the shell
    print("Device selection (ZCU104 + FT232H on the same PC)")
    ok &= check("finds the FT232H behind four FT4232H channels",
                find_ft232h() == (FT232H_INDEX, "FTSIM001"))
    ok &= check("wrong serial is refused",
                raises(lambda: find_ft232h("NOPE"), FT232HError))
    BUS.append({"type": 8, "serial": b"FTSIM002", "description": b"FT232H",
                "flags": 0})
    ok &= check("two FT232H boards without serial= is refused",
                raises(find_ft232h, FT232HError))
    ok &= check("two FT232H boards with serial= picks the right one",
                find_ft232h("FTSIM002")[1] == "FTSIM002")
    os.environ["FT232H_SERIAL"] = "FTSIM002"
    ok &= check("two FT232H boards with FT232H_SERIAL picks the right one",
                find_ft232h()[1] == "FTSIM002")
    del os.environ["FT232H_SERIAL"]
    BUS.pop()

    print("\nPin map")
    ok &= check("C_DIR = 0xDF (C5 is an input)", C_DIR == 0xDF)
    ok &= check("D_DIR = 0x49 (clk_i, bypass_i, trigger are outputs)",
                D_DIR == 0x49)

    board = Board.open()
    scan = Scan(board, length=CHAIN_LEN, verbose=False)
    chip = DEVICE.chip

    print("\nSafe state")
    ok &= check("rst_n driven low", (board.c_value >> 6) & 1 == 0)
    ok &= check("bypass_i driven low", (board.d_value >> 3) & 1 == 0)

    print("\nScan chain")
    measured = scan.detect_length(max_length=256)
    ok &= check(f"detect_length() == {CHAIN_LEN} (got {measured})",
                measured == CHAIN_LEN)
    ok &= check("integrity_test() over 4 patterns", scan.integrity_test())

    print("\nBit map and bit order")
    smap = ScanMap(MAP)
    ok &= check("map is 87 bits, lsb_first, verified",
                smap.length == CHAIN_LEN and smap.bit_order == "lsb_first"
                and smap.verified)
    frame = smap.build(static_wen=1, static_addr=0xC0005, static_wdata=0x1234ABCD)
    scan.shift_bits(frame)
    scan.update()
    ok &= check("UPDATE lands wen/addr/wdata in the right static latches",
                chip.static == {"wen": 1, "ren": 0, "addr": 0xC0005,
                                "wdata": 0x1234ABCD})
    ok &= check("int_to_bits / bits_to_int are LSB-first",
                int_to_bits(0b1011, 4) == [1, 1, 0, 1]
                and bits_to_int(int_to_bits(0xA5, 8)) == 0xA5)

    bus = ScanBus(scan, smap, settle_s=0)

    print("\nClock generator config (works with rst_n still low)")
    bus.write_clk_config(0b10111)
    ok &= check("static_config_clk latched 0b10111", chip.config_clk == 0b10111)
    ok &= check("read_clk_config() returns it", bus.read_clk_config() == 0b10111)

    print("\nGroup A in reset: accesses must not happen")
    ok &= check("SRAM read with rst_n=0 raises (ready=0)",
                raises(lambda: bus.sram_read(3), ScanError))

    board.reset_release()
    print("\nGroup A SRAM through scan (rst_n released)")
    words = {0: 0x87654321, 1: 0xDEADBEEF, 5: 0x00000001, 2047: 0xFFFFFFFF}
    for w, d in words.items():
        bus.sram_write(w, d)
    ok &= check("writes reached the SRAM model",
                all(chip.sram.get(w) == d for w, d in words.items()))
    ok &= check("sram_read() returns every word",
                all(bus.sram_read(w) == d for w, d in words.items()))

    print("\nGroup A control/status registers")
    bus.write(make_addr(GROUP_A, register=True), 0x0400 << 15)
    rdata, ready = bus.read(make_addr(GROUP_A, register=True))
    ok &= check("control register written through wdata[31:15]",
                chip.cr == 0x0400)
    ok &= check("register read returns {cr, sr} with ready=1",
                ready == 1 and rdata == ((0x0400 << 15) | 0x1234))

    print("\nMid-test reset")
    board.reset_pulse(hold_s=0)
    ok &= check("clock config survives rst_n pulse (reset-less latch)",
                bus.read_clk_config() == 0b10111)

    print("\nExact clock bursts")
    before = DEVICE.tck_pulses
    board.clock_cycles(1000)
    ok &= check("clock_cycles(1000) emits exactly 1000 TCK pulses",
                DEVICE.tck_pulses - before == 1000)
    before = DEVICE.tck_pulses
    board.clock_cycles(13)
    ok &= check("clock_cycles(13) emits exactly 13 TCK pulses",
                DEVICE.tck_pulses - before == 13)

    board.safe_state()
    board.close()
    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
