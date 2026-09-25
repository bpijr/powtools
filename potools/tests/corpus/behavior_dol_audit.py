"""Recheck the narrow static PPC evidence used by the definition field editors.

No disassembler dependency, machine code dump or executable mutation. This verifies
instruction operands and call targets in the recorded PAL (R7PP01) executable; it is not a
runtime test or a general control-flow proof.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1])); import paths  # noqa: E402
from nlg_dol import Dol
from nlg_hash import string_to_hash

DOL_SHA256 = "6ae3388ff644758f030bb28de28f898885dc7c2da76bff082faae9eb60e66137"


def audit(path):
    d = Dol(str(path)); digest = hashlib.sha256(d.d).hexdigest()
    if digest != DOL_SHA256: raise ValueError("Static evidence is pinned to a different main.dol.")
    def operands(addr):
        w = d.word(addr)
        return w >> 26, (w >> 21) & 31, (w >> 16) & 31, (w & 65535) - (65536 if w & 32768 else 0)
    def expect(addr, op, reg, base, displacement):
        assert operands(addr) == (op, reg, base, displacement), f"PPC evidence changed at {addr:08X}"
    def call(addr, target):
        w = d.word(addr); delta = w & 0x3FFFFFC
        if delta & 0x2000000: delta -= 0x4000000
        assert w >> 26 == 18 and w & 3 == 1 and addr + delta == target
    checks = [(0x80135B78, 32, 0, 27, 4), (0x80135BB4, 33, 0, 3, 4),
              (0x80135BB8, 11, 0, 0, 2), (0x80135BF8, 48, 0, 31, 4),
              (0x80135C00, 52, 0, 29, 0), (0x80135C88, 11, 0, 0, 1),
              (0x80135CC8, 32, 29, 30, 4), (0x80135EF4, 33, 0, 3, 4),
              (0x80135EF8, 11, 0, 0, 3), (0x80135F38, 32, 0, 31, 4),
              (0x80166054, 14, 4, 5, 32), (0x8016606C, 32, 0, 5, 16),
              (0x80166088, 32, 0, 5, 20), (0x80166260, 14, 18, 18, 12)]
    for check in checks: expect(*check)
    fields = {}
    for name, high, low, dest, branch, offset in [
        ("BlendAmount", 0x8000A488, 0x8000A490, 0x8000A494, 0x8000A4A0, 0x78),
        ("TimeScale", 0x8000A4F8, 0x8000A500, 0x8000A504, 0x8000A510, 0x90)]:
        h = string_to_hash(name, True)
        assert operands(high)[:3] == (15, 4, 0) and operands(low)[:3] == (14, 4, 4)
        assert ((operands(high)[3] << 16) + operands(low)[3]) & 0xFFFFFFFF == h
        expect(dest, 14, 5, 31, offset); call(branch, 0x80135B54)
        fields[name] = {"case_sensitive_hash": f"{h:08X}", "destination_offset": offset,
                        "reader": "80135B54", "call": f"{branch:08X}"}
    assert operands(0x8000A3C0)[:3] == (15, 3, 0)
    assert operands(0x8000A400)[:3] == (14, 4, 3)
    assert ((operands(0x8000A3C0)[3] << 16) + operands(0x8000A400)[3]) & 0xFFFFFFFF == string_to_hash("AnimProperties", True)
    call(0x8000A40C, 0x80135E94)
    return {"format": "po-behavior-static-evidence", "version": 1, "main_dol_sha256": digest,
            "instruction_operand_checks": len(checks), "fields": fields,
            "function_span_sha256": {f"{start:08X}": hashlib.sha256(d.read(start, end - start)).hexdigest()
               for start, end in [(0x8000A384, 0x8000A5A8), (0x80135B54, 0x80135C24),
                                  (0x80135E94, 0x80135F6C), (0x80166044, 0x80166290)]},
            "runtime_verified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("dol"); parser.add_argument("--report", type=Path)
    args = parser.parse_args(); report = json.dumps(audit(args.dol), indent=2, sort_keys=True) + "\n"
    if args.report: args.report.write_text(report, encoding="utf-8")
    print(report); print("BEHAVIOR_DOL_AUDIT_PASS")
