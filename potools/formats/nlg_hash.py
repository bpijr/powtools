"""
nlg_hash.py — Next Level Games (Punch-Out!! Wii) hash utilities.

The whole engine references resources/bones/meshes/materials by a 32-bit hash of
their name. This module lets us (a) reverse known hashes to names and (b) COMPUTE
the hash for a brand-new name, which is required to add new resources.

Hash algorithm:
    h = -1
    for each byte c of name:  h = (h*33 + c) & 0xFFFFFFFF
Credit: RoadrunnerWMC. Same DJB-variant used by LM3/NLG (Switch-Toolbox NLG_Common).

The registry is not governed by one universal case policy. In the retail registry, all
14,987 names match either case-sensitive or ASCII-case-folded hashing; 14,984 match the former,
11,128 the latter, and three match only the latter. Callers minting a new name must choose the
policy appropriate to that resource family and validate it in context.

hashid.bin layout (big-endian):
    uint32 count
    count x { uint32 hash, uint32 stringOffset }
    string table (offsets are relative to end of the record array = count*8+4)
    each name is zero-terminated ASCII.
"""
import struct


def string_to_hash(name: str, case_sensitive: bool = False) -> int:
    """Compute the 32-bit NLG name hash.

    ``case_sensitive=False`` folds ASCII A-Z for resource families that use that policy. It is a
    compatibility default for existing callers, not a universal rule for every registry entry.
    """
    data = name.encode("latin1")
    h = -1
    for c in data:
        if not case_sensitive and 65 <= c <= 90:  # 'A'..'Z'
            c |= 0x20
        h = (h * 33 + c) & 0xFFFFFFFF
    return h & 0xFFFFFFFF


def load_hashid_bin(path: str) -> dict[int, str]:
    """Load art/hashid.bin -> {hash: name}."""
    data = open(path, "rb").read()
    (count,) = struct.unpack_from(">I", data, 0)
    strtbl = count * 8 + 4
    out = {}
    for i in range(count):
        h, off = struct.unpack_from(">II", data, 4 + i * 8)
        end = data.index(b"\x00", strtbl + off)
        out[h] = data[strtbl + off : end].decode("latin1")
    return out


def build_reverse(names: dict[int, str]) -> dict[str, int]:
    return {v: k for k, v in names.items()}


def find_hashid_bin(archive_path):
    """(path or None, [searched folders]): the nearest hashid.bin in the archive's folder or
    any parent. No folder name is assumed; the dump layout only needs hashid.bin above it."""
    import os
    d = os.path.dirname(os.path.abspath(archive_path)); searched = []
    while True:
        searched.append(d)
        candidate = os.path.join(d, "hashid.bin")
        if os.path.isfile(candidate):
            return candidate, searched
        parent = os.path.dirname(d)
        if parent == d:
            return None, searched
        d = parent


def missing_hashid_message(archive_path, searched):
    return ("hashid.bin was not found beside %s or in any of its %d parent folders (searched %s up to %s). "
            "Bone and mesh names cannot be resolved without it; keep hashid.bin in any folder above the archive."
            % (archive_path, max(len(searched) - 1, 0), searched[0] if searched else "-",
               searched[-1] if searched else "-"))


if __name__ == "__main__":
    import sys
    # self-test against a real hashid.bin if given
    if len(sys.argv) > 1:
        names = load_hashid_bin(sys.argv[1])
        insensitive = sum(1 for h, n in names.items() if string_to_hash(n) == h)
        sensitive = sum(1 for h, n in names.items() if string_to_hash(n, True) == h)
        either = sum(1 for h, n in names.items()
                     if string_to_hash(n) == h or string_to_hash(n, True) == h)
        print(f"loaded {len(names)} names; case-sensitive={sensitive}, "
              f"case-insensitive={insensitive}, either={either}")
    else:
        for n in ["Idle", "littlemac", "bip01 l foretwist", "LeftJabRollCycle"]:
            print(f"{string_to_hash(n):08X}  {n}")
