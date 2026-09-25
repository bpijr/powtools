"""Shader-keyed material views and fixed-size, evidence-bounded edits (no UI).

Record spans come from B004 references, never division by a guessed record size.
Texture prefixes are established by corpus texture-key matches and FFFF0000 state
words; their rendering roles are unnamed except for the documented skin shader.
The only named scalar fields are MATERIALS.md's skin tint, alpha and specular power.
All other bytes are opaque. See MATERIAL_LAYOUTS.md and tests/corpus_material_scan.py.
"""
from dataclasses import dataclass
import hashlib
import math
import struct

import nlg_model


class MaterialError(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    name: str
    offset: int
    count: int
    maximum: float
    label: str


@dataclass(frozen=True)
class Layout:
    shader: int
    name: str
    size: int
    slots: tuple
    fields: tuple = ()


SKIN = 0xC89C219A
SKIN_FIELDS = (Field("specular_power", 0x84, 1, 256.0, "Highlight sharpness"),
               Field("tint", 0x9C, 3, 1.0, "Surface tint"), Field("alpha", 0xA8, 1, 1.0, "Opacity"))
SKIN_SLOTS = ("Detail", "Damage", "Specular mask", "Diffuse ramp", "Rim ramp", "HDR", "Fresnel", "Specular ramp")


def _layout(shader, name, count):
    return Layout(shader, name, nlg_model.MATERIAL_SIZES[shader],
                  SKIN_SLOTS if shader == SKIN else tuple("Texture input %d" % (i + 1) for i in range(count)),
                  SKIN_FIELDS if shader == SKIN else ())


LAYOUTS = {v.shader: v for v in (
    _layout(SKIN, "hippodiffuseskin", 8),
    _layout(0x55951F36, "hippobasicenvironment", 5),
    _layout(0xEE5973A5, "hippodiffusenonskin", 8),
    _layout(0x485F111C, "hippohwlitenvironment", 5),
    _layout(0xF2D57AC6, "stadiumdetailmaskwithuvsliding", 3),
    _layout(0xBACEA013, "crowdskin", 3),
    _layout(0xA8F6FE22, "crowdskindk", 3),
    _layout(0x32BC21E8, "stadiumflatreflection", 4),
    _layout(0x2DFB08EA, "ropeskin", 2),
    _layout(0x46ABE398, "diffusedetail", 2),
    _layout(0xEE9D919D, "constantcolour", 1),
    _layout(0x21DB4385, "diffuse", 1),
)}


def _u32(raw, offset):
    return struct.unpack_from(">I", raw, offset)[0]


def layout_error(shader, raw):
    layout = LAYOUTS.get(shader)
    if layout is None: return "Unknown shader; material bytes are opaque."
    if len(raw) != layout.size: return "Material span does not match the shader layout."
    if any(raw[i * 8 + 4:i * 8 + 8] != b"\xff\xff\0\0" for i in range(len(layout.slots))):
        return "Texture reference state differs from the observed disk layout."
    return None


@dataclass(frozen=True)
class Record:
    chunk: int
    model_set: int
    index: int
    offset: int
    shader: int
    raw: bytes
    meshes: tuple
    names: tuple
    limitation: object = None

    @property
    def layout(self): return LAYOUTS.get(self.shader)

    @property
    def error(self): return self.limitation or layout_error(self.shader, self.raw)

    @property
    def editable_fields(self):
        if self.error: return ()
        return tuple(f.name for f in self.layout.fields) + tuple("texture_%d" % i for i in range(len(self.layout.slots)))

    def values(self):
        if self.error: return {}
        result = {}
        for f in self.layout.fields:
            vals = struct.unpack_from(">%df" % f.count, self.raw, f.offset)
            # JSON cannot carry NaN/Infinity, and unbounded values cannot be edited.
            result[f.name] = (vals[0] if f.count == 1 else list(vals)) if all(math.isfinite(v) for v in vals) else None
        return result

    def references(self, textures=(), names=None):
        if self.error: return []
        names = names or {}; local = {}
        for e in textures: local.setdefault(e.hash, []).append(e)
        rows = []
        for i, label in enumerate(self.layout.slots):
            h = _u32(self.raw, i * 8); entries = local.get(h, [])
            rows.append({"slot": i, "word": i * 8, "label": label, "hash": h,
                         "name": names.get(h, entries[0].name if entries else "%08X" % h),
                         "resolution": "local" if entries else "external or unresolved",
                         "indices": [e.index for e in entries],
                         "cached_index": 65535, "control_raw": 0, "tail_raw": 0})
        return rows

    def describe(self, textures=(), names=None):
        names = names or {}; known = self.layout
        refs = self.references(textures, names)
        defined = [(r["word"], r["word"] + 8) for r in refs]
        if not self.error: defined += [(f.offset, f.offset + f.count * 4) for f in known.fields]
        opaque = []; start = None
        for i in range(len(self.raw) + 1):
            unknown = i < len(self.raw) and not any(lo <= i < hi for lo, hi in defined)
            if unknown and start is None: start = i
            elif not unknown and start is not None:
                opaque.append({"offset": start, "bytes": i - start,
                               "sha256": hashlib.sha256(self.raw[start:i]).hexdigest()}); start = None
        return {"chunk": self.chunk, "model_set": self.model_set, "index": self.index, "offset": self.offset,
                "bytes": len(self.raw), "shader": self.shader, "shader_name": known.name if known else "%08X" % self.shader,
                "meshes": list(self.meshes), "label": ", ".join(names.get(h, "mesh %d / %08X" % (m, h))
                                                                    for m, h in zip(self.meshes, self.names)),
                "layout_known": not self.error, "limitation": self.error, "values": self.values(),
                "editable_fields": list(self.editable_fields), "texture_inputs": refs, "opaque_ranges": opaque,
                "record_sha256": hashlib.sha256(self.raw).hexdigest(), "runtime_verified": False}


def records(archive):
    """Pair each B016 with its following mesh table; no fabricated owners for orphan data.

    A material-only inspector need not decode geometry. Pairing follows model chunk order;
    another B016 or a non-model chunk terminates the candidate run. Invalid tables remain
    inspectable, but any ambiguous/partial coverage locks the complete material chunk.
    """
    result = []; model = -1; pending = None
    for ri in archive.find_chunks():
        kind = archive.chunks[ri][2]
        if kind == 0xB016:
            if pending is not None: result.append(_orphan(archive, pending, model))
            model += 1; pending = ri
        elif pending is not None and kind == 0xB004:
            result.extend(_records(archive, pending, ri, model)); pending = None
        elif pending is not None and kind not in (0xB007, 0xB006, 0xB005):
            result.append(_orphan(archive, pending, model)); pending = None
    if pending is not None: result.append(_orphan(archive, pending, model))
    return result


def _orphan(a, ri, model):
    return Record(ri, model, 0, 0, 0, bytes(a.get_chunk_bytes(ri)), (), (), "No owning mesh table; material layout is unproved.")


def _records(a, ri, mesh_ri, model):
    raw = bytes(a.get_chunk_bytes(ri)); meshraw = a.get_chunk_bytes(mesh_ri)
    if not meshraw or len(meshraw) % 52:
        return [Record(ri, model, 0, 0, 0, raw, (), (), "Empty or partial mesh table.")]
    owners = {}
    for o in range(0, len(meshraw), 52):
        owners.setdefault(_u32(meshraw, o + 36), []).append((o // 52, _u32(meshraw, o + 16), _u32(meshraw, o + 20)))
    offsets = sorted(owners); out = []; problems = []
    if offsets[0] != 0 or offsets[-1] >= len(raw): problems.append("Material offsets do not cover the chunk.")
    for index, offset in enumerate(offsets):
        end = offsets[index + 1] if index + 1 < len(offsets) else len(raw)
        users = owners[offset]; shaders = {x[1] for x in users}; shader = users[0][1]
        data = raw[offset:end] if 0 <= offset < end <= len(raw) else b""
        if len(shaders) != 1: problems.append("A material record has conflicting shader owners.")
        reason = layout_error(shader, data)
        if reason: problems.append(reason)
        out.append((index, offset, shader, data, tuple(x[0] for x in users), tuple(x[2] for x in users)))
    reason = " ".join(dict.fromkeys(problems)) or None
    return [Record(ri, model, i, off, shader, data, meshes, names, reason) for i, off, shader, data, meshes, names in out]


def find_record(archive, chunk, offset):
    if type(chunk) is not int or type(offset) is not int or offset < 0:
        raise MaterialError("Choose a valid material chunk and byte offset.")
    for rec in records(archive):
        if rec.chunk == chunk and rec.offset == offset:
            if rec.error: raise MaterialError(rec.error)
            return rec
    raise MaterialError("Material is not referenced by an owning mesh.")


def record_for_mesh(model_set, mesh):
    for rec in _records(model_set.archive, model_set.chunks[0xB016], model_set.chunks[0xB004], model_set.index):
        if mesh.index in rec.meshes: return rec
    raise MaterialError("Mesh has no material record.")


def field_patches(record, field, value, textures=()):
    """Patch one proven field. Texture keys must select one existing local texture entry.

    Texture state bytes, shader hash, offsets, other records, and unknown bytes never change.
    A shared record intentionally affects every mesh listed in its provenance/review.
    """
    if record.error: raise MaterialError(record.error)
    if field not in record.editable_fields: raise MaterialError("That material field is opaque or inspect-only.")
    if field.startswith("texture_"):
        slot = int(field[8:]); offset = 8 * slot
        if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
            raise MaterialError("Choose a texture already present in this archive section.")
        old = record.raw[offset:offset + 4]; new = struct.pack(">I", value)
        # An untouched unresolved reference is always preserved verbatim.
        if old == new: return []
        matches = [e for e in textures if e.hash == value]
        if len(matches) != 1: raise MaterialError("Replacement texture must resolve uniquely inside this archive section.")
        if not matches[0].width or not matches[0].height:
            raise MaterialError("Replacement texture has invalid dimensions.")
        label = record.layout.slots[slot]
    else:
        spec = next(f for f in record.layout.fields if f.name == field)
        values = [value] if spec.count == 1 else value
        if not isinstance(values, (list, tuple)) or len(values) != spec.count or any(
                type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= spec.maximum for v in values):
            raise MaterialError("%s requires %d finite value(s) from 0 to %g." % (spec.label, spec.count, spec.maximum))
        offset = spec.offset; old = record.raw[offset:offset + spec.count * 4]
        new = struct.pack(">%df" % spec.count, *values); label = spec.label
    label = "model set %d: material @%d %s (%d mesh users)" % (record.model_set, record.offset, label, len(record.meshes))
    return nlg_model.diff_patches(record.chunk, record.offset + offset, old, new, label, merge_gap=0)


def patches(archive, chunk, offset, field, value):
    import nlg_texture
    rec = find_record(archive, chunk, offset)
    try: textures = nlg_texture.list_all_textures(archive)
    except ValueError: textures = []
    if isinstance(field, str) and field in rec.editable_fields and field.startswith("texture_"):
        old = _u32(rec.raw, int(field[8:]) * 8)
        if value != old:
            for entry in textures:
                if entry.hash == value and (nlg_texture.FORMATS.get(entry.fmt) not in nlg_texture.DECODERS or
                        not 0 < entry.width <= 2048 or not 0 < entry.height <= 2048 or entry.levels > 12 or
                        entry.data_offset + entry.chain_size > archive.chunks[entry.data_chunk][3]):
                    raise MaterialError("Replacement texture has unsupported dimensions, format, or a truncated mip chain.")
    return field_patches(rec, field, value, textures)


def inspect_archive(archive, names=None):
    import nlg_texture
    try: textures = nlg_texture.list_all_textures(archive, names)
    except ValueError: textures = []
    return [rec.describe(textures, names) for rec in records(archive)]
