"""Synthetic archive builders shared by the unit tests."""
import struct




def archive_bytes(payloads):
    """Single-section archive with every (type, bytes) payload in block zero, 32-byte aligned."""
    data = bytearray(); table = bytearray()
    for kind, raw in payloads:
        table += struct.pack(">BBHII", 0, 0, kind, len(raw), len(data))
        data += raw + bytes(-len(raw) % 32)
    header = struct.pack(">IHBBIIII", 0xA9F32458, 1, 0, 0, 1, 0, 0, len(table))
    header += b"".join(struct.pack(">II", len(data) if i == 0 else 0, 4) for i in range(8))
    return header, bytes(data) + bytes(table)


def container_bytes(parts):
    """Multi-section NIS-style container from single-section (dict, data) pairs, 2 KB aligned."""
    dd = bytearray(parts[0][0][:8] + struct.pack(">I", len(parts))); da = bytearray()
    for single, body in parts:
        da += bytes(-len(da) % 2048)
        dd += struct.pack(">I", len(da)) + single[16:88]
        da += body
    return bytes(dd), bytes(da)


def texture_header(width, height, fmt, levels, offset, key):
    header = bytearray(96)
    struct.pack_into(">IHH", header, 0, key, width, height)
    header[10] = header[11] = levels; header[13] = fmt
    struct.pack_into(">I", header, 20, offset)
    return bytes(header)



def skin_material():
    """Synthetic skin material with shader ownership and the observed disk reference state."""
    material = bytearray(204); mesh = bytearray(52)
    for i in range(8): struct.pack_into(">II", material, i * 8, i + 1, 0xFFFF0000)
    struct.pack_into(">fff", material, 156, 1, 1, 1)
    struct.pack_into(">f", material, 168, 1)
    struct.pack_into(">I", mesh, 16, 0xC89C219A)
    return material, mesh


