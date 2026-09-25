"""Read-only proof of the three formerly provisional UV shader formats.

python potools/tests/corpus/dol_vertex_format_scan.py <main.dol>

Checks shader constructor identity/material size/vtable, the vtable's setup call, then
the constant argument registers at each GX VAT setter call. No executable bytes are
printed or stored. Addresses describe the supplied PAL R7PP01 executable; another build
must fail closed until its consumers are identified independently.
"""
from pathlib import Path
import hashlib
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1])); import paths  # noqa: E402
from nlg_dol import Dol
from nlg_model import SHADER_VAT, MATERIAL_SIZES

SET_VAT = 0x801F10B0
SHADERS = {
    0x46ABE398: (0x80119F90, 0x8033B9EC, 0x8011A0BC),
    0xEE9D919D: (0x801191FC, 0x8033B8E8, 0x8011931C),
    0x32BC21E8: (0x801159DC, 0x8033B830, 0x80115B20),
}


def branch(addr, word):
    offset = word & 0x03FFFFFC
    if offset & 0x02000000: offset -= 0x04000000
    return offset & 0xFFFFFFFF if word & 2 else (addr + offset) & 0xFFFFFFFF


def constants(dol, start, size=0x200):
    regs = {}; stores = {}; calls = []
    for addr in range(start, start + size, 4):
        w = dol.word(addr)
        if w is None: raise ValueError("Unmapped executable code")
        if w == 0x4E800020: break
        op = w >> 26; rd, ra, imm = (w >> 21) & 31, (w >> 16) & 31, w & 0xFFFF
        signed = imm - 0x10000 if imm & 0x8000 else imm
        if op in (14, 15):
            if ra == 0 or ra in regs:
                regs[rd] = ((regs.get(ra, 0) if ra else 0) + (signed << 16 if op == 15 else signed)) & 0xFFFFFFFF
            else: regs.pop(rd, None)
        elif op == 36 and ra == 31 and rd in regs: stores[signed] = regs[rd]
        elif op == 18 and w & 1:
            calls.append((branch(addr, w), dict(regs)))
            for reg in range(3, 13): regs.pop(reg, None)
        elif op in (16, 18):
            break  # VAT arguments precede the first conditional branch; no path inference.
    return stores, calls


def scan(path):
    dol = Dol(str(path)); calls_checked = 0
    assert hashlib.sha256(dol.d).hexdigest() == "6ae3388ff644758f030bb28de28f898885dc7c2da76bff082faae9eb60e66137", "Executable build differs from the audited consumer"
    for shader, (constructor, vtable, setup) in SHADERS.items():
        stores, _ = constants(dol, constructor)
        assert stores.get(0) == vtable and stores.get(4) == shader and stores.get(8) == MATERIAL_SIZES[shader], "shader identity changed"
        _, calls = constants(dol, dol.word(vtable + 12))
        assert any(target == setup for target, _ in calls), "shader no longer invokes the expected VAT setup"
        _, calls = constants(dol, setup)
        formats = {}
        for target, regs in calls:
            if target != SET_VAT: continue
            assert all(r in regs for r in range(3, 8)), "nonconstant VAT arguments"
            assert regs[3] == 0, "unexpected VAT selector"
            formats[regs[4]] = (regs[6], regs[7]); calls_checked += 1
        assert formats == SHADER_VAT[shader], f"shader {shader:08X}: VAT differs"
    return len(SHADERS), calls_checked


if __name__ == "__main__":
    shaders, calls = scan(sys.argv[1])
    print("DOL_VERTEX_FORMAT_SCAN_PASS", shaders, "shaders", calls, "VAT calls")
