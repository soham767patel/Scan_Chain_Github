"""ECE598 test board: FT232H pin map, shadow registers, safe state, reset.

Why a shadow register?
    One MPSSE write (0x82) sets all eight ACBUS pins at once.  If you rebuild
    that byte from scratch every time you want to move one signal, you glitch
    the other seven.  `Board` keeps the current value/direction of each bank in
    software and rewrites the whole byte with only the requested bit changed.

Why all the scan signals on ONE bank?
    Because a single 0x82 is atomic.  You cannot change an ACBUS pin and an
    ADBUS pin in the same command, so any two signals that must move together
    (phi, phi_bar, data_in, load_*) have to share a bank.

Chip facts this module encodes (verified against the fabricated netlist):
  * rst_n has an on-die PULL-UP -> an undriven chip is OUT of reset.  The
    board's 4.7k pull-down is what makes "held in reset" the default, and it
    is mandatory, not optional.
  * rst_n resets only clk_gen (ring oscillator + dividers + slow_clk_o) and
    the group cores.  The scan chain and static_config_clk are reset-less
    latches, so scan works with the chip held in reset -- that is the intended
    bring-up path, not a trick.
  * In reset the internal clocks park HIGH (clk_osc/clk_div/clk_o = 1); only
    slow_clk_o is actively cleared to 0.  "Static", not "low".
  * bypass_i does NOT gate on reset.  With bypass_i=1 and clk_i running, the
    core clock tree toggles through reset and burns dynamic power.  Keep
    bypass_i=0 for the power-up leakage check.
  * The clock source mux (CKMUX2D2) is not glitch-free: change bypass_i only
    while rst_n is asserted and the external clock is stopped.
"""

from __future__ import annotations

import time

from ft232h_mpsse import FT232H_MPSSE

# ---------------------------------------------------------------------------
# Pin map
# ---------------------------------------------------------------------------
# ACBUS (breakout C0..C7) -- every scan-critical signal, so one 0x82 moves
# them atomically.
C_LOAD_CHAIN = 0    # out  scan_load_chain   CAPTURE: chip state -> chain
C_LOAD_CHIP  = 1    # out  scan_load_chip    UPDATE:  chain -> chip registers
C_PHI        = 2    # out  scan_phi          master latch
C_PHI_BAR    = 3    # out  scan_phi_bar      slave latch (never overlap PHI)
C_DATA_IN    = 4    # out  scan_data_in
C_DATA_OUT   = 5    # IN   scan_data_out
C_RST_N      = 6    # out  rst_n             (board 4.7k pull-down)
C_SCAN_ID    = 7    # out  scan_id

# ADBUS (breakout D0..D7)
D_CLK_I      = 0    # out  clk_i  == MPSSE TCK, driven by 0x8E/0x8F bursts
#              1    #      TDI/DO -- MPSSE data pin, leave UNCONNECTED
#              2    #      TDO/DI -- MPSSE data pin, leave UNCONNECTED
D_BYPASS     = 3    # out  bypass_i
D_SLOW_CLK   = 4    # IN   slow_clk_o
D_HALT       = 5    # IN   halt (block 1 only)
D_TRIGGER    = 6    # out  spare -> Saleae trigger

# Which pins we drive.  C_DATA_OUT is deliberately absent: it is a chip output,
# and driving it would be contention.  D1/D2 are absent because the MPSSE data
# commands own them; they stay unconnected on the PCB.
C_OUTPUTS = {C_LOAD_CHAIN, C_LOAD_CHIP, C_PHI, C_PHI_BAR,
             C_DATA_IN, C_RST_N, C_SCAN_ID}
D_OUTPUTS = {D_CLK_I, D_BYPASS, D_TRIGGER}

# Direction masks, bit 1 = output.  These are not a one-time "configure" step:
# the mask is the THIRD BYTE of every 0x80/0x82 command, so directions are
# re-asserted on every single pin write.  Computed from the sets above so the
# mask is never hand-typed; test_00 pins the expected values.
C_DIR = sum(1 << b for b in C_OUTPUTS)      # 0xDF = 0b1101_1111 (bit 5 = input)
D_DIR = sum(1 << b for b in D_OUTPUTS)      # 0x49 = 0b0100_1001

# Signal name -> (bank, bit, is_output)
PINS = {
    "load_chain": ("C", C_LOAD_CHAIN, True),
    "load_chip":  ("C", C_LOAD_CHIP,  True),
    "phi":        ("C", C_PHI,        True),
    "phi_bar":    ("C", C_PHI_BAR,    True),
    "data_in":    ("C", C_DATA_IN,    True),
    "data_out":   ("C", C_DATA_OUT,   False),
    "rst_n":      ("C", C_RST_N,      True),
    "scan_id":    ("C", C_SCAN_ID,    True),
    "clk_i":      ("D", D_CLK_I,      True),
    "bypass_i":   ("D", D_BYPASS,     True),
    "slow_clk_o": ("D", D_SLOW_CLK,   False),
    "halt":       ("D", D_HALT,       False),
    "trigger":    ("D", D_TRIGGER,    True),
}

# --- polarity -------------------------------------------------------------
# Both load strobes are ACTIVE HIGH. Verified in the course RTL
# (w26/tutorial/A_Team_Scan_Chain/verilog/share/block_scan.sv, identical in
# all eleven group copies):
#   scan_next = scan_load_chain ? scan_load : {scan_data_in, scan_slave[86:1]}
#   always @* if (scan_load_chip) static_* = scan_slave[...]
# (The previous chip generation had load_chain active LOW; this one does not.)
LOAD_CHIP_ACTIVE_HIGH = True
LOAD_CHAIN_ACTIVE_HIGH = True


class Board:
    """Named-pin access to the ECE598 test board through one FT232H."""

    def __init__(self, ft: FT232H_MPSSE):
        self.ft = ft
        self._c_val = 0x00
        self._d_val = 0x00

    # -- construction -------------------------------------------------------
    @classmethod
    def open(cls, serial: str | None = None, clock_hz: float = 1_000_000,
             verbose: bool = False, ft_id: int | None = None) -> "Board":
        """Open the FT232H (found by chip type) and drive the safe state.

        Pass serial="FT..." if more than one FT232H is connected.
        """
        board = cls(FT232H_MPSSE(ft_id=ft_id, clock_hz=clock_hz,
                                 verbose=verbose, serial=serial))
        board.safe_state()
        return board

    def close(self) -> None:
        self.ft.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.safe_state()
        finally:
            self.close()

    # -- safe state ---------------------------------------------------------
    def safe_state(self) -> None:
        """Drive every output LOW and claim it, before touching the chip.

        Run this while the chip supplies are still OFF.  This pad library is
        not fail-safe: the output PMOS body diode connects PAD -> VDDPST, so a
        pin driven high into an unpowered VDDPST pushes current into the dead
        rail.  All-outputs-low is the only correct pre-power state.

        Leaves: rst_n = 0 (asserted), bypass_i = 0 (internal RO selected, so
        the power-up current check really is leakage-only), clk_i = 0, both
        scan phases low, both load strobes inactive.
        """
        self._c_val = 0x00
        self._d_val = 0x00
        if not LOAD_CHAIN_ACTIVE_HIGH:
            self._c_val |= 1 << C_LOAD_CHAIN
        if not LOAD_CHIP_ACTIVE_HIGH:
            self._c_val |= 1 << C_LOAD_CHIP
        self.ft.set_pins_high(self._c_val, C_DIR)
        self.ft.set_pins_low(self._d_val, D_DIR)

    # -- pin access ---------------------------------------------------------
    def set(self, **signals: int) -> None:
        """Set one or more output pins. Pins on the same bank move atomically.

            board.set(phi=1)
            board.set(data_in=1, phi=0, phi_bar=0)   # one 0x82, no glitches
        """
        touched_c = touched_d = False
        for name, value in signals.items():
            try:
                bank, bit, is_out = PINS[name]
            except KeyError:
                raise KeyError(f"unknown signal {name!r}; "
                               f"known: {sorted(PINS)}") from None
            if not is_out:
                raise ValueError(f"{name!r} is a chip output; do not drive it")
            if bank == "C":
                self._c_val = (self._c_val | (1 << bit)) if value else \
                              (self._c_val & ~(1 << bit))
                touched_c = True
            else:
                self._d_val = (self._d_val | (1 << bit)) if value else \
                              (self._d_val & ~(1 << bit))
                touched_d = True
        if touched_c:
            self.ft.set_pins_high(self._c_val, C_DIR)
        if touched_d:
            self.ft.set_pins_low(self._d_val, D_DIR)

    def get(self, name: str) -> int:
        """Sample one pin (inputs and outputs alike)."""
        bank, bit, _ = PINS[name]
        raw = self.ft.read_pins_high() if bank == "C" else self.ft.read_pins_low()
        return (raw >> bit) & 1

    @property
    def c_value(self) -> int:
        """Current shadow value of the ACBUS byte."""
        return self._c_val

    @property
    def d_value(self) -> int:
        return self._d_val

    def c_with(self, **signals: int) -> int:
        """ACBUS byte with `signals` applied, without sending anything.

        Used by ece598_scan.py to compile many pin states into one USB write.
        """
        val = self._c_val
        for name, value in signals.items():
            bank, bit, is_out = PINS[name]
            if bank != "C":
                raise ValueError(f"{name!r} is not on the ACBUS bank")
            if not is_out:
                raise ValueError(f"{name!r} is a chip output; do not drive it")
            val = (val | (1 << bit)) if value else (val & ~(1 << bit))
        return val

    def adopt_c(self, value: int) -> None:
        """Tell the shadow register what the ACBUS byte ended up as."""
        self._c_val = value & 0xFF

    # -- reset --------------------------------------------------------------
    def reset_assert(self) -> None:
        self.set(rst_n=0)

    def reset_release(self) -> None:
        """Release rst_n with the external clock stopped (see module docstring)."""
        self.set(rst_n=1)

    def reset_pulse(self, hold_s: float = 1e-5) -> None:
        """Mid-test reset. Clears group state + clock dividers.

        The scan chain and static_config_clk are reset-less latches, so the
        configuration you scanned in survives this.
        """
        self.set(rst_n=0)
        time.sleep(hold_s)
        self.set(rst_n=1)

    def select_clock_source(self, external: bool) -> None:
        """Switch between the internal RO and the external clk_i.

        The clock mux is not glitch-free, so this is only safe in reset with
        the external clock stopped. Caller must restart the clock afterwards.
        """
        self.set(rst_n=0)
        self.set(clk_i=0)
        self.set(bypass_i=1 if external else 0)

    # -- misc ---------------------------------------------------------------
    def clock_cycles(self, n: int) -> None:
        """Exactly n clk_i pulses (bypass_i must be 1 for them to reach core)."""
        self.ft.clock_cycles(n)

    def pulse_trigger(self) -> None:
        """Blip the spare pin so a Saleae capture is easy to find."""
        self.set(trigger=1)
        self.set(trigger=0)

    def report_inputs(self) -> str:
        c = self.ft.read_pins_high()
        d = self.ft.read_pins_low()
        return (f"ACBUS=0b{c:08b} ADBUS=0b{d:08b} | "
                f"data_out={(c >> C_DATA_OUT) & 1} "
                f"slow_clk_o={(d >> D_SLOW_CLK) & 1} "
                f"halt={(d >> D_HALT) & 1}")
