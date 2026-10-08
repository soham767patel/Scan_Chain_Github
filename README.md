# ECE598 FT232H Test Kit

Python control of the ECE598 tape-out chip through an **Adafruit FT232H**
breakout: reset sequencing, and scan-chain write/read.

```
ece598_ft232h_kit/
├── ft232h_mpsse.py      generic FT232H MPSSE driver (knows nothing about the chip)
├── ece598_board.py      pin map, shadow registers, safe state, reset control
├── ece598_scan.py       two-phase scan chain: shift / update / capture
├── scan_map.py          bit-map file parser (names instead of magic indices)
├── maps/block_scan.txt  the 87-bit chain layout (verified against block_scan.sv)
├── tests/               THE FILE NUMBER IS THE ORDER. Just go 00, 01, 02, ...
│   │                    -- bench phase: no chip and no PCB needed --
│   ├── test_00_offline_selftest.py  no hardware at all -- run this first
│   ├── test_01_enumerate.py         driver + MPSSE sync, no wiring
│   ├── test_02_gpio_blink.py        one pin, scope/LED check
│   ├── test_03_scan_loopback.py     C4->C5 jumper; proves the code, not the chip
│   │                    -- chip phase: PCB with the chip on it --
│   ├── test_04_safe_state.py        run BEFORE powering the chip
│   ├── test_05_chain_length.py      measure the chain (expect 87)
│   ├── test_06_scan_integrity.py    write/read patterns over the shift path
│   ├── test_07_first_light.py       config in reset, release rst_n
│   ├── test_08_clk_config.py        clock-config register write/read (in reset)
│   └── test_09_sram_rw.py           SRAM + control register through scan
├── tools/check_c_pins.py  new-board check: every ACBUS pin, one at a time
└── README.md            you are here
```

## 1. Setup

```bash
pip install ftd2xx
python tests/test_00_offline_selftest.py     # no hardware needed
python tests/test_01_enumerate.py            # FT232H plugged in
```

Windows: the standard FTDI CDM driver (Device Manager -> Universal Serial Bus
controllers -> "USB Serial Converter") already includes D2XX. **Do not run
Zadig.** libusb/WinUSB is for `pyftdi`/Blinka and will make `ftd2xx` stop
seeing the device.

**Which FTDI device?** The kit opens the FT232H by chip type, so other FTDI
parts on the same PC (the ZCU104's USB-JTAG is an FT4232H) are ignored. With
two FT232H boards plugged in, the scripts stop and list the serials; pick
one for every script with the `FT232H_SERIAL` environment variable
(PowerShell: `$env:FT232H_SERIAL = 'FT...'`) or `Board.open(serial=...)`.

## 2. Pin map

Every scan-critical signal is on the **ACBUS** bank, because one `0x82` write
updates all eight of those pins atomically — you cannot move an ACBUS pin and
an ADBUS pin in the same command.

| FT232H | Dir | Chip signal | Note |
|---|---|---|---|
| C0 | out | `scan_load_chain` | CAPTURE strobe |
| C1 | out | `scan_load_chip`  | UPDATE strobe |
| C2 | out | `scan_phi`        | master latch |
| C3 | out | `scan_phi_bar`    | slave latch, never overlapping |
| C4 | out | `scan_data_in`    | |
| C5 | **in** | `scan_data_out` | never drive this |
| C6 | out | `rst_n`           | board 4.7 kΩ pull-down mandatory |
| C7 | out | `scan_id`         | |
| D0 | out | `clk_i`           | = MPSSE TCK; use `clock_cycles()` |
| D1, D2 | — | *unconnected*  | MPSSE data pins, keep off the chip |
| D3 | out | `bypass_i`        | clock source select |
| D4 | in  | `slow_clk_o`      | |
| D5 | in  | `halt`            | block 1 only |
| D6 | out | spare             | Saleae trigger |

## 3. Chip facts the code depends on

Verified against the fabricated netlist:

- `rst_n` resets **only** `clk_gen` (ring oscillator, dividers, `slow_clk_o`)
  and the group cores. The 87-bit scan chain and `static_config_clk` are
  **reset-less latches** → scan works with the chip held in reset, and the
  configuration survives later resets.
- The RO frequency setting is **random at power-up**. Always scan the clock
  config in *before* releasing `rst_n`.
- `rst_n` has an **on-die pull-up** → an undriven chip is *out* of reset. The
  board pull-down is what makes "held in reset" the default.
- In reset the internal clocks park **HIGH** (`clk_osc`/`clk_div`/`clk_o` = 1);
  only `slow_clk_o` is cleared to 0. Probing a steady 1 is correct, not a fault.
- `bypass_i` does **not** gate on reset. With `bypass_i=1` and `clk_i` running,
  the core clock tree toggles through reset and burns dynamic power → keep
  `bypass_i=0` for the power-up leakage check.
- The clock mux is not glitch-free → change `bypass_i` only in reset with the
  external clock stopped (`Board.select_clock_source()` enforces this).

## 4. Scan protocol (verified against the RTL)

Frame: 87 bits, shifted LSB-first. `[0]` wen, `[1]` ren, `[21:2]` addr,
`[53:22]` wdata, `[85:54]` rdata, `[86]` ready (`maps/block_scan.txt`).

| Step | Pins | Used by |
|---|---|---|
| SHIFT | `phi` then `phi_bar` pulse per bit, never overlapping | both |
| UPDATE | pulse `load_chip` → frame into the `static_*` latches | both |
| ACCESS | toggle `scan_id` → the group's 2-flop synchronizer fires one access | both |
| CAPTURE | `load_chain` high + one `phi`/`phi_bar` pulse → `rdata`/`ready` into the chain | read |
| SHIFT OUT | next frame shifted in while the result comes out | read |

`static_addr[19:18]` picks the target: `00`/`01`/`10` = group A/B/C, `11` =
clock config. Inside a group, `addr[11]` = 0 SRAM, 1 registers; `addr[10:0]`
= word. The clock-config latch (`group_mux.v`) is open only while
`sel_clk & wen & scan_id`, so a clock write pulses `scan_id` 0→1→0 instead of
toggling it. `ScanBus` in `ece598_scan.py` does all of this:
`write`, `read`, `sram_write`, `sram_read`, `write_clk_config`, `read_clk_config`.

Clock config: `[1:0]` osc_sel (3 = slowest RO), `[4:2]` div_sel (`000` /1,
`001` /2, `010` /4, `011` /8, `1xx` /16). `slow_clk_o` = chip clock / 1024.

**No chip yet?** FPGA tutorial Modules 5–6 put the same scan RTL on the ZCU104
(PMOD J55), and tests 05, 06, 08 and 09 run against it unchanged. Skip
`test_07` there: it watches the chip's `slow_clk_o` (D4), which the FPGA
design does not have, so it always reports FAIL.

## 5. Bring-up order

**Bench phase — no chip, no PCB.** Do all of this before the silicon arrives.

1. `test_00` — offline. If this fails, the kit is broken, not your wiring.
2. `test_01` — FT232H enumerates, MPSSE syncs (`0xFA 0xAA`).
3. `test_02` — blink a pin, first Saleae capture.
4. `test_03` — jumper C4→C5, loopback. Passing this means your code, pin map
   and read path are right, so any later failure is the board or the chip.

**Chip phase — PCB with the chip on it.**

5. ESD strap. Cold checks: rail-to-GND resistance, jumper positions. Supplies
   pre-set to 3.3 V / 1.2 V, current limits ~50 mA (I/O) and ~20 mA per core
   rail, **outputs off**.
6. `test_04` — USB in, MPSSE up, **all FT232H outputs driven LOW** including
   `rst_n`. Do this *before* chip power: this pad library is not fail-safe
   (output PMOS body diode goes PAD → VDDPST), so a pin driven high into an
   unpowered rail pushes current into it.
7. Enable **3.3 V VDDPST1–4** together → expect leakage.
8. Enable **1.2 V VDD1–4** → in reset, clockless, `bypass_i=0` → leakage only.
   Anything large: stop.
9. `test_05` — chain length must read 87.
10. `test_06` — pattern integrity over the shift path.
11. `test_07` — scan in the clock config (Group_ID `0b11`, `scan_id=1`,
    `wen=1`), release `rst_n`, watch `slow_clk_o`. First light.
12. `test_08` — clock-config write/read-back over several values; ends at the
    slowest setting.
13. `test_09` — SRAM and control register through scan. Needs the chip
    clocked and out of reset (the script releases `rst_n`).

**Power-down (mirror image):** assert `rst_n` → all FT232H outputs LOW →
`VDD1–4` to 0 → `VDDPST` to 0 → USB whenever. The 10 kΩ board pull-downs beat
the FT232H's ~75 kΩ tri-state pull-ups, so a connected-but-idle FTDI holds nets
near 0.4 V, which is harmless.

## 6. Saleae channel plan

`Board.pulse_trigger()` blips D6 at the start of each operation — trigger on it
and captures become trivial to find. Ground the analyzer to the FT232H: a
missing GND is the most common cause of a garbage capture.

**With 8 channels**, one capture covers everything:

`scan_phi | scan_phi_bar | scan_data_in | scan_data_out | scan_load_chip |
scan_load_chain | rst_n | slow_clk_o`

**With 4 channels**, take two captures instead. Nothing is lost — the two
phases of the work need different signals anyway.

| | CH0 | CH1 | CH2 | CH3 | trigger |
|---|---|---|---|---|---|
| A. shift path (`test_03/05/06`) | `phi` C2 | `phi_bar` C3 | `data_in` C4 | `data_out` C5 | `phi` rising |
| B. reset & first light (`test_07`) | `rst_n` C6 | `load_chip` C1 | `slow_clk_o` D4 | trigger D6 | D6 rising |

Notes for capture A:
- During `test_03` the C4→C5 jumper shorts `data_in` and `data_out` into one
  node, so CH3 is redundant — the software already verifies the jumper. Use
  CH3 for the D6 trigger instead, or drop to three probes.
- With a real chip attached, CH3 (`data_out`) is the interesting one: it is
  what `test_05`/`test_06` actually measure.
- Sample rate matters. A 32-bit shift is compiled into ONE USB transfer and
  the MPSSE runs it back-to-back at engine speed, so pulses are in the
  hundreds of nanoseconds. Use 25 MS/s or more — the 100 kS/s that is plenty
  for `test_02`'s 1 Hz blink will show nothing here.

## 7. Open items

Resolved against the RTL (`block_scan.sv`, chip `group_mux.v`, `clk_gen.v`):

- [x] `maps/block_scan.txt` field positions — verified, `#!verified: yes`.
- [x] `osc_sel` / `div_sel` encodings — see section 4.
- [x] `scan_load_chip` / `scan_load_chain` polarity — both active-high.
- [x] `scan_id` — an access strobe: each toggle starts one group access; for
      the clock config it is part of the latch enable (pulse it).

Still open:

- [ ] **Block 4 `group_2_scan_in`** — second scan group; how it shares
      `scan_data_out`.
- [ ] **I/O voltage.** Some internal PCB documents label VDDPST as **2.5 V**;
      the verified power sequence and this kit assume **3.3 V**, which is what
      the Adafruit FT232H drives. Confirm the rail before powering a board.

## 8. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `listDevices` returns nothing | cable/power, or Zadig replaced the FTDI driver — reinstall FTDI CDM |
| Sync check fails (no `0xFA`) | not in MPSSE mode, or another program holds the device open (Saleae, another Python shell) |
| Reads time out | forgot `0x87` send-immediate, or earlier result bytes left undrained |
| `detect_length` finds nothing | pin map, `phi`/`phi_bar` polarity, block unpowered, or C5 being driven |
| Chain reads all 0 or all 1 | `scan_data_out` not connected, or the wrong block selected |
| Config written but nothing happens | `load_chip` polarity, or you wrote while `wen`/`group_id` were wrong |
| Worked, then stopped | stale bytes in the RX queue — `ft.flush()`; power-cycle the FT232H as a last resort |

## 9. References

- FTDI **AN_108** — MPSSE command processor (the opcode bible)
- FTDI **AN_135** — MPSSE basics and bring-up
- The previous generation's `pyscan` scan program (Python 2) — ask the GSI;
  `ece598_scan.py` is its Python 3 successor for this chip
- TSMC I/O databook — cite for **voltage ranges (Table 2.1) only**. It says
  nothing about supply sequencing; VDDPST-before-core follows from the pad's
  PAD→VDDPST body diode and the library not being fail-safe.
# Scan_Chain_Github
