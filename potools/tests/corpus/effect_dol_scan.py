"""Opt-in static PAL executable evidence check. Never runs the game or writes files.

python potools/tests/corpus/effect_dol_scan.py <main.dol>
Checks consumer operand relationships and executes the original polynomial sampler
instructions against synthetic segments. Printed SHA-256 applies only to this PAL image.
"""
import hashlib
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1])); import paths  # noqa: E402
from nlg_dol import Dol
from nlg_effect import Parameter

PAL_SHA256 = "6ae3388ff644758f030bb28de28f898885dc7c2da76bff082faae9eb60e66137"


def sampler(dol, parameter, t):
    """Bounded PPC subset for fn_8016B32C only, using synthetic memory.

    Inputs use dyadic fractions and small integer coefficients so fused-vs-double
    rounding does not obscure the independently executed arithmetic comparison.
    """
    base, curves = 0x10000000, 0x10001000
    p = bytearray(parameter.raw); struct.pack_into(">I", p, 16, curves)
    memory = {base: bytes(p), curves: parameter.curve_raw}
    def read(addr):
        for start, raw in memory.items():
            if start <= addr <= start + len(raw) - 4: return raw[addr - start:addr - start + 4]
        raise AssertionError(f"Sampler read outside synthetic memory: {addr:08X}")
    r = [0] * 32; f = [0.0] * 32; r[3] = base; f[1] = t
    pc, ctr, compare = 0x8016B32C, 0, 0
    for _ in range(300):
        w = dol.word(pc); op = w >> 26; d = w >> 21 & 31; a = w >> 16 & 31; b = w >> 11 & 31
        imm = (w & 65535) - (65536 if w & 32768 else 0); nxt = pc + 4
        if w == 0x4E800020: return f[1]
        if op == 32: r[d] = struct.unpack(">I", read(r[a] + imm))[0]
        elif op == 48: f[d] = struct.unpack(">f", read(r[a] + imm))[0]
        elif op == 14: r[d] = ((r[a] if a else 0) + imm) & 0xFFFFFFFF
        elif op == 7: r[d] = r[a] * imm & 0xFFFFFFFF
        elif op == 10: compare = (r[a] > (w & 65535)) - (r[a] < (w & 65535))
        elif op == 31:
            xo = w >> 1 & 1023
            if xo == 467: ctr = r[d]
            elif xo == 535: f[d] = struct.unpack(">f", read(r[a] + r[b]))[0]
            elif xo == 266: r[d] = (r[a] + r[b]) & 0xFFFFFFFF
            else: raise AssertionError(f"Unsupported sampler integer instruction at {pc:08X}")
        elif op == 63 and w >> 1 & 1023 == 32:
            compare = (f[a] > f[b]) - (f[a] < f[b])
        elif op == 59:
            xo = w >> 1 & 31; c = w >> 6 & 31
            if xo == 25: value = f[a] * f[c]
            elif xo == 29: value = f[a] * f[c] + f[b]
            elif xo == 21: value = f[a] + f[b]
            else: raise AssertionError(f"Unsupported sampler float instruction at {pc:08X}")
            f[d] = struct.unpack(">f", struct.pack(">f", value))[0]
        elif op == 16:
            bo, bi = d, a
            if bo == 16: ctr -= 1; take = ctr != 0
            elif bo == 4 and bi == 1: take = compare <= 0
            elif bo == 12 and bi == 1: take = compare > 0
            else: raise AssertionError(f"Unsupported sampler branch at {pc:08X}")
            disp = (w & 0xFFFC) - (0x10000 if w & 0x8000 else 0)
            if take: nxt = pc + disp
        else: raise AssertionError(f"Unsupported sampler opcode at {pc:08X}")
        pc = nxt
        assert 0x8016B32C <= pc < 0x8016B39C
    raise AssertionError("Sampler instruction limit")


def scan(path):
    d = Dol(str(path))
    assert hashlib.sha256(d.d).hexdigest() == PAL_SHA256, "DOL differs from the documented PAL image"
    # Relevant lwz/stw operands at the proven loader and consumer locations.
    evidence = [(0x8016E458, 32, 0, 31, 8), (0x8016E4A0, 32, 0, 31, 16),
                (0x8016B78C, 36, 3, 30, 88), (0x8016B79C, 32, 3, 29, 56),
                (0x8016E2DC, 32, 0, 6, 4), (0x8016E2E8, 36, 0, 6, 4),
                (0x8016E7D8, 32, 3, 6, 56), (0x8016E7F8, 32, 4, 3, 84)]
    for addr, op, rt, ra, imm in evidence:
        w = d.word(addr)
        assert (w >> 26, w >> 21 & 31, w >> 16 & 31, w & 65535) == (op, rt, ra, imm)
    checked = 0
    for count in (1, 2, 4):
        for seed in range(12):
            raw = struct.pack(">IffII", 1, 0, 0, count, 0)
            curve = b"".join(struct.pack(">5fI", i / count, seed + i, 2 - i, -seed, 4 + i, 0xDEADBEEF)
                             for i in range(count))
            p = Parameter(0, 0, raw, 1, curve)
            for step in range(17):
                t = step / 16
                assert sampler(d, p, t) == p.sample(t), (count, seed, t)
                checked += 1
    print("DOL sha256", PAL_SHA256)
    print("Static consumer operand checks", len(evidence))
    print("Original sampler synthetic evaluations", checked)
    print("Sampler function sha256", hashlib.sha256(d.read(0x8016B32C, 0x70)).hexdigest())
    print("EFFECT_DOL_SCAN_PASS (static evidence; runtime unverified)")


if __name__ == "__main__": scan(sys.argv[1])
