"""Name <-> bit-position map for a scan chain (Python 3 port of pyscan).

Test scripts should never contain magic bit indices.  The bit map lives in a
text file next to the code, so when the map changes you edit data, not scripts.

File format (one signal per line, comments after #):

    #!length: 87
    #!order: lsb_first
    #!verified: yes
    # [bus_notation]:signal_name
    [0]:static_wen
    [1]:static_ren
    [21:2]:static_addr
    ...

Rules enforced at load time: no duplicate indices, no gaps, and the total must
equal the declared length.  That check alone catches most map errors before
they reach silicon.

Bit order (#!order):
    lsb_first  chain bit 0 is shifted in first and read out first, so a
               Python list `bits` is indexed by chain bit number:
               bits[i] = chain bit i.  This is the ECE598 block_scan.
    msb_first  the legacy pyscan convention: the highest bit is shifted in
               first, so list_index = (length - 1) - bit_index.
"""

from __future__ import annotations

from pathlib import Path


class ScanMapError(Exception):
    pass


class ScanMap:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.fields: dict[str, tuple[int, int]] = {}   # name -> (start, width)
        self.order: list[str] = []                     # field names, file order
        self.length: int | None = None
        self.bit_order = "msb_first"
        self.verified = False
        self._load()

    # -- loading -------------------------------------------------------------
    def _load(self) -> None:
        covered: list[int] = []
        for lineno, raw in enumerate(self.path.read_text().splitlines(), 1):
            line = raw.strip()
            if line.startswith("#!"):
                key, _, value = line[2:].partition(":")
                key, value = key.strip().lower(), value.strip().lower()
                if key == "length":
                    self.length = int(value)
                elif key == "verified":
                    self.verified = value in ("yes", "true", "1")
                elif key == "order":
                    if value not in ("lsb_first", "msb_first"):
                        raise ScanMapError(f"{self.path}:{lineno}: order must "
                                           f"be lsb_first or msb_first")
                    self.bit_order = value
                continue
            line = line.split("#")[0].strip()
            if not line:
                continue
            if len(line.split()) != 1:
                raise ScanMapError(f"{self.path}:{lineno}: no spaces allowed "
                                   f"inside an entry: {raw!r}")
            name = line.split(":")[-1]
            bus = ":".join(line.split(":")[:-1]).strip("[]")
            parts = bus.split(":")
            if len(parts) == 2:
                msb, lsb = int(parts[0]), int(parts[1])
            elif len(parts) == 1:
                msb = lsb = int(parts[0])
            else:
                raise ScanMapError(f"{self.path}:{lineno}: bad bus notation "
                                   f"{bus!r}")
            if msb < lsb:
                raise ScanMapError(f"{self.path}:{lineno}: [{msb}:{lsb}] has "
                                   f"msb < lsb")
            width = msb - lsb + 1
            if name != "none":
                if name in self.fields:
                    raise ScanMapError(f"{self.path}:{lineno}: duplicate "
                                       f"signal {name!r}")
                self.fields[name] = (lsb, width)
                self.order.append(name)
            covered.extend(range(lsb, lsb + width))

        if len(covered) != len(set(covered)):
            dupes = sorted({i for i in covered if covered.count(i) > 1})
            raise ScanMapError(f"{self.path}: overlapping bit indices {dupes}")
        if self.length is None:
            self.length = len(covered)
        if sorted(covered) != list(range(self.length)):
            missing = sorted(set(range(self.length)) - set(covered))
            extra = sorted(set(covered) - set(range(self.length)))
            raise ScanMapError(
                f"{self.path}: map does not cover exactly 0..{self.length - 1}"
                f" (missing {missing[:16]}, out of range {extra[:16]})")

    # -- field access --------------------------------------------------------
    def _positions(self, name: str) -> list[int]:
        """List indices of the field's bits, LSB of the field first."""
        try:
            start, width = self.fields[name]
        except KeyError:
            raise ScanMapError(f"unknown signal {name!r}; known: "
                               f"{self.order}") from None
        chain_bits = range(start, start + width)
        if self.bit_order == "lsb_first":
            return list(chain_bits)
        return [self.length - 1 - b for b in chain_bits]

    def get(self, bits: list[int], name: str) -> int:
        value = 0
        for k, pos in enumerate(self._positions(name)):
            value |= (1 if bits[pos] else 0) << k
        return value

    def set(self, bits: list[int], name: str, value: int) -> list[int]:
        positions = self._positions(name)
        if value < 0 or value >= (1 << len(positions)):
            raise ScanMapError(f"{name} is {len(positions)} bit(s); {value} "
                               f"does not fit")
        out = list(bits)
        for k, pos in enumerate(positions):
            out[pos] = (value >> k) & 1
        return out

    def build(self, defaults: int = 0, **values: int) -> list[int]:
        """Build a full chain vector from named field values."""
        bits = [defaults & 1] * self.length
        for name, value in values.items():
            bits = self.set(bits, name, value)
        return bits

    def describe(self, bits: list[int]) -> str:
        lines = []
        for name in self.order:
            start, width = self.fields[name]
            value = self.get(bits, name)
            lines.append(f"    {name:<16} [{start + width - 1}:{start}] = "
                         f"0x{value:0{(width + 3) // 4}X}")
        return "\n".join(lines)

    def require_verified(self) -> None:
        if not self.verified:
            raise ScanMapError(
                f"{self.path} is marked '#!verified: no'.\n"
                f"  Confirm the field positions against the RTL and with "
                f"test_06_scan_integrity.py, then set '#!verified: yes'.\n"
                f"  Refusing to drive the chip with an unverified bit map.")
