"""Strict archive loading, output-path guards and structural validation shared by the tools."""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
from pathlib import Path
import struct
import sys

from nlg_pack import Archive
import nlg_texture


class ArchiveError(ValueError):
    """An actionable, user-facing safety or format error."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inside(path, parent):
    return path == parent or parent in path.parents


def external(path, source):
    path, source = Path(path).expanduser().resolve(), Path(source).resolve()
    if inside(path, source) or inside(source, path):
        raise ArchiveError("Choose an output outside the clean dump, not its parent or a subfolder.")
    if any((p / ".git").exists() for p in (path, *path.parents)):
        raise ArchiveError("Choose an external folder outside Git; generated game files must stay private.")
    return path


def source_file(root, relative):
    root = Path(root).resolve()
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise ArchiveError("Resource must be a relative path inside the clean dump.")
    path = (root / rel).resolve()
    if not inside(path, root) or not path.is_file():
        raise ArchiveError("Resource is missing or links outside the clean dump.")
    return path


SECTION_BYTES = 76


def sections(dd):
    """Section records of a .dict: a 12-byte header (magic, version, flags, section count), then
    per section: data offset, entry count, chunk-table bytes, 8 x (block size, block parameter).

    Ordinary archives have one section at offset 0 (88 bytes). NIS cinematics hold 2-13 sections,
    each laid out like a whole archive and padded with zeros to 2 KB in the .data file.
    """
    if len(dd) < 12 or dd[:4] != bytes.fromhex("a9f32458"):
        raise ArchiveError("Not a Punch-Out archive dictionary.")
    if dd[6]:
        raise ArchiveError("Compressed archive dictionaries are not supported.")
    count = struct.unpack_from(">I", dd, 8)[0]
    if not count or len(dd) != 12 + SECTION_BYTES * count:
        raise ArchiveError("Archive dictionary has an unsupported layout.")
    result = []
    for s in range(count):
        o = 12 + SECTION_BYTES * s
        offset, entries, table = struct.unpack_from(">III", dd, o)
        sizes = [struct.unpack_from(">I", dd, o + 12 + 8 * i)[0] for i in range(8)]
        if table % 12 or entries > table // 12:
            raise ArchiveError("Archive table size or block lengths are inconsistent.")
        result.append({"offset": offset, "entries": entries, "length": sum(sizes) + table,
                       "dictionary": dd[:8] + struct.pack(">II", 1, 0) + dd[o + 4:o + SECTION_BYTES]})
    return result


def _check_chunk_bounds(a):
    for i in range(a.num_file_entries, len(a.chunks)):
        block = a._chunk_block(i)
        _, _, _, size, offset = a.chunks[i]
        if block >= 0 and offset + size > len(a.blocks[block]):
            raise ArchiveError(f"Chunk {i} extends outside its data block.")


def load_sections(path):
    """Read-only Archive per section, for listing contents. Never used for editing: the
    container must rebuild byte-for-byte from its sections and zero padding."""
    path = Path(path)
    dd, da = path.read_bytes(), path.with_suffix(".data").read_bytes()
    parts, rebuilt = sections(dd), bytearray()
    for part in parts:
        gap = part["offset"] - len(rebuilt)
        body = da[part["offset"]:part["offset"] + part["length"]]
        if gap < 0 or any(da[len(rebuilt):part["offset"]]) or len(body) != part["length"]:
            raise ArchiveError("Archive sections overlap, hide data or extend past the payload.")
        rebuilt += bytes(gap) + body
        part["archive"] = a = Archive(str(path), part["dictionary"], body)
        _check_chunk_bounds(a)
        if a.build_dict() != part["dictionary"] or a.build_data() != body:
            raise ArchiveError("An archive section does not round-trip exactly.")
    if bytes(rebuilt) != da:
        raise ArchiveError("Archive payload has data outside its sections.")
    return [part["archive"] for part in parts]


def load_archive(path):
    """Reject malformed layouts before the permissive legacy parser reads them."""
    path = Path(path)
    dd, da = path.read_bytes(), path.with_suffix(".data").read_bytes()
    parts = sections(dd)
    if len(parts) != 1 or parts[0]["offset"]:
        raise ArchiveError(f"Multi-section cinematic container ({len(parts)} sections): contents can be listed, editing is not supported yet.")
    if parts[0]["length"] != len(da):
        raise ArchiveError("Archive table size or block lengths are inconsistent.")
    a = Archive(str(path))
    _check_chunk_bounds(a)
    for block in range(7):
        ranges = sorted((a.chunks[i][4], a.chunks[i][4] + a.chunks[i][3])
                        for i in a.find_chunks(block=block) if a.chunks[i][3])
        if any(right[0] < left[1] for left, right in zip(ranges, ranges[1:])):
            raise ArchiveError("Overlapping data chunks are inspect-only in the legacy tools; safe editing is disabled.")
    if a.build_dict() != dd or a.build_data() != da:
        raise ArchiveError("This archive does not round-trip exactly; editing is disabled.")
    return a


def inspect_archive(a):
    import nlg_hash
    import nlg_material_layout
    names = {}
    # Optional names stay in memory. No registry is copied into projects or recipes.
    for parent in Path(a.dict_path).parents:
        candidate = parent / "hashid.bin"
        if candidate.is_file():
            try:
                names = nlg_hash.load_hashid_bin(str(candidate))
            except (OSError, ValueError, struct.error):
                names = {}  # Names are optional; a bad registry must not block numeric inspection.
            break
    layout_rows = nlg_material_layout.inspect_archive(a, names)
    materials = []; other_materials = []
    mesh_chunks = a.find_chunks(type_id=0xB004); decoded = material_chunks(a)
    for model, ri in enumerate(a.find_chunks(type_id=0xB016)):
        data = a.get_chunk_bytes(ri)
        meshes = a.get_chunk_bytes(mesh_chunks[model]) if model < len(mesh_chunks) else b""
        if ri not in decoded:
            # Listed but uneditable instead of blocking the whole archive.
            status = ("See Materials & shader inputs for decoded references and guarded edits." if any(
                row["chunk"] == ri and row["layout_known"] for row in layout_rows) else "Material layout not decoded; inspect-only")
            other_materials.append({"chunk": ri, "bytes": len(data), "status": status})
            continue
        for index in range(len(data) // 204):
            mesh_names = []
            if meshes:
                for off in range(0, len(meshes) - 51, 52):
                    if struct.unpack_from(">I", meshes, off + 36)[0] == index * 204:
                        h = struct.unpack_from(">I", meshes, off + 20)[0]
                        mesh_names.append(names.get(h, f"mesh {off // 52} / {h:08X}"))
            materials.append({"chunk": ri, "index": index,
                              "label": ", ".join(mesh_names) or f"Unassigned material {index}",
                              "alpha": struct.unpack_from(">f", data, index * 204 + 168)[0],
                              "specular_power": struct.unpack_from(">f", data, index * 204 + 132)[0],
                              "rgb": struct.unpack_from(">fff", data, index * 204 + 156)})
    textures = []; texture_note = None
    try: entries = texture_entries(a, names)
    except ArchiveError as ex: entries = []; texture_note = str(ex)
    for e in entries:
        if nlg_texture.FORMATS.get(e.fmt) in nlg_texture.DECODERS and (not e.width or not e.height or
                e.levels > 12 or e.data_offset + e.chain_size > a.chunks[e.data_chunk][3]):
            raise ArchiveError(f"Texture {e.index} has invalid dimensions or a truncated mip chain.")
        textures.append({k: getattr(e, k) for k in ("index", "hash", "name", "width", "height", "fmt", "levels", "table")})
    cameras = [{"chunk": i, "type": f"0x{a.chunks[i][2]:04X}", "bytes": a.chunks[i][3]}
               for i in a.find_chunks() if 0x5000 <= a.chunks[i][2] <= 0x5FFF]
    return {"materials": materials, "other_materials": other_materials, "material_layouts": layout_rows, "textures": textures,
            "texture_note": texture_note, "camera_chunks": cameras}


def material_chunks(a):
    """0xB016 chunks in the decoded 204-byte fighter layout. Meshes address their material by
    byte offset (+36); crowds (40), ropes (32), rings (48-364, mixed) and effects use other
    shader layouts, so a length that happens to be a multiple of 204 is not proof on its own."""
    import nlg_material_layout as layout
    records = layout.records(a)
    return sorted({r.chunk for r in records if r.shader == layout.SKIN and not r.error and
                   all(x.shader == layout.SKIN and not x.error for x in records if x.chunk == r.chunk)})


def texture_entries(a, names=None):
    """Every texture across all header/pixel tables; global indices match single-table archives."""
    try:
        return nlg_texture.list_all_textures(a, names)
    except ValueError as ex:
        raise ArchiveError(f"Texture tables are unsupported: {ex}.") from ex


def validate(path, source=None):
    a = load_archive(path)
    inspect_archive(a)
    report = {"passed": True, "scope": "container and selected recolor invariants",
              "warnings": ["Runtime unverified: passing offline checks does not prove the mod will work in game."],
              "details": "Exact container round-trip and chunk bounds passed."}
    # Reuse the existing deeper checks only when its required character profile exists.
    types = {a.chunks[i][2] for i in a.find_chunks()}
    if {0xB004, 0xB016, 0xB601, 0xB603, 0xB006, 0xB007, 0xB00A, 0xB00B}.issubset(types):
        tools = str(Path(__file__).resolve().parents[1] / "tools")
        if tools not in sys.path:
            sys.path.append(tools)
        import validate_mod
        stream = io.StringIO()
        try:
            with contextlib.redirect_stdout(stream):
                code = validate_mod.main([str(path)] + ([str(source)] if source else []))
            report["passed"] = code == 0
        except Exception as ex:
            report["passed"] = False
            stream.write(f"\nCharacter validator could not finish: {ex}")
        report["details"] = stream.getvalue()
        report["scope"] = "container and legacy character checks"
    else:
        report["warnings"].append("Not a complete supported character profile; geometry/rig checks were not run.")
    return report


