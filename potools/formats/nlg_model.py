"""
nlg_model.py — every model set in an archive section, read losslessly and patched in place.

A section holds any number of MODEL SETS. Each is a run of chunks in this fixed order
(verified on all 356 sets in the retail dump, ordinary archives and NIS sections alike):

    B016 materials, B007 indices, B006 vertices, B005 attribute pointers, B004 meshes,
    B002 transforms, B003 nodes, then optionally B00B x meshCount (bone palettes),
    B00A bones, B00C (skin/morph header), then optionally D001, D002.

B003 NODE (12 B): u32 nameHash, u32 meshCount, u32 0. Nodes partition the mesh table in
    order (the counts always sum to the mesh count). Names are the scene objects of the
    source file, e.g. "_feminor/punchingbag/capsule01".
B002 TRANSFORM (64 B): 4x4 f32, row-vector convention (translation in row 3). A mesh selects
    one with its +24 index; vertices are node-local, world = [x y z 1] . M. Several nodes may
    share a transform and every transform is used by at least one mesh.
B004 MESH (52 B): +0 index start, +4 index flags (count low 24 bits, format high 8; always u16
    strips in retail), +8 vertex count, +10 u8 (always 1), +11 attribute count, +12 byte
    offset of its first B005 pointer, +16 shader hash, +20 name hash, +24 transform index,
    +28/+32 format words (preserved), +36 material byte offset into B016, +40..+51 zero.
B005 POINTER (8 B): u32 offset into B006, u8 storage type, u8 stride, u16 flags. The flags
    carry the semantic: high byte 1 position, 2 normal, 3 colour, 4 texture coordinates,
    5 weights, 7 bone indices; low byte is the set number. The storage byte names the
    encoding; ones without an established scale are preserved and flagged unverified.
Material records are addressed by byte offset; each record spans to the next referenced
offset and its size is fixed by the mesh shader hash (see MATERIAL_SIZES).

RETAIL PACKING (re-encoding every unchanged set reproduces its chunks byte-for-byte, all 356):
    B007  u16 strips of every mesh back to back in mesh order; no alignment, no tail padding.
    B006  attribute arrays in pointer order, each at a 32-byte boundary, chunk padded to 32.
          Gap bytes are exporter garbage: within a set every gap is a prefix of one filler
          pattern (the longest gap), so the pattern is learned per set and reused.
    B005  8-byte pointers back to back; B004 52-byte records back to back.
    B016  one material record per mesh, in mesh order (no record is shared by two meshes in
          any retail set), so +36 is the running offset; B003 counts are recomputed.
    B00B  u32 bone hashes, all present in B00A; every entry is used by some weight; at most 9
          per mesh (PALETTE_LIMIT). Weights f32 x 4 (0xB0, flags 0x500) and palette slot
          indices u8 x 4 (0xD4; flags 0x701 on hippodiffuseskin, 0x700 elsewhere): <= 4
          influences, nonzero weights first, empty slots index 0 / weight 0, sum 1 within 1e-6.
          Slot order is neither by weight nor by index in retail; edits write heaviest first.
          One B00B chunk record per mesh, counted by a 0xB008 directory record, so whole-mesh
          insertion/deletion in skinned sets changes the chunk table (not done here).
    B00C  morph targets address LOCAL vertex indices (remapped on rebuild).

0x6101 chunks (ring environments) are a culling tree: consecutive chunks in pre-order, each
    6 f32 AABB (min xyz, max xyz), u32 item count, u32 0, u32 has-first-child, u32
    has-second-child (both set on inner nodes: a full binary tree), then item hashes naming
    B003 nodes. Every box equals the float32 rounding of the world-space bounds of its items'
    geometry and its children's boxes (exact on all 273 retail nodes), so it is recomputed
    whenever geometry or transforms change.

No bpy here. Blender and the CLI tools both read through this module.
"""
import math
import struct

MAIN = (0xB016, 0xB007, 0xB006, 0xB005, 0xB004, 0xB002, 0xB003)
TRAILING = (0xB00B, 0xB00A, 0xB00C, 0xD001, 0xD002)
SEMANTICS = {1: "position", 2: "normal", 3: "color", 4: "texcoord", 5: "weights", 7: "indices"}

# (storage type, stride) -> (struct code, components, scale). Scales are proven: f32 values,
# s8 normals of length 64, s16 UV0 over 1024 (fighter skins), u8 colour and bone indices.
STORAGE = {
    (0x0A, 12): ("f", 3, 1.0), (0x67, 12): ("f", 3, 1.0),
    (0xFE, 12): ("f", 3, 1.0), (0xFE, 3): ("b", 3, 1 / 64),
    (0xE9, 4): ("B", 4, 1), (0xB6, 4): ("B", 4, 1),
    (0xCC, 4): ("h", 2, 1 / 1024), (0x26, 8): ("f", 2, 1.0),
    (0xD4, 4): ("B", 4, 1), (0xB0, 16): ("f", 4, 1.0),
    # These repeat UV0's raw integers exactly on a large share of vertices in the same meshes
    # (0x3D 83-88%, 0x52 85%, 0xF9 98%, 0x05 26%, 0x4C 29%, 0x2E 14%), which only happens at
    # the same s16/1024 scale. Their extreme values belong to unused sets holding junk.
    (0x05, 4): ("h", 2, 1 / 1024), (0x3D, 4): ("h", 2, 1 / 1024), (0x4C, 4): ("h", 2, 1 / 1024),
    (0x52, 4): ("h", 2, 1 / 1024), (0xF9, 4): ("h", 2, 1 / 1024), (0x2E, 4): ("h", 2, 1 / 1024),
    # Each of these type/stride pairs occurs with one shader, whose vtable slot 3 in main.dol calls
    # GXSetVtxAttrFmt with explicit fractional bits (SHADER_VAT): diffusedetail TEX0/TEX1 frac 8,
    # constantcolour TEX0 frac 12, stadiumflatreflection TEX0-TEX3 frac 10. The same calls give
    # tests/dol_vertex_format_scan.py checks these three shader consumers in the supplied DOL.
    (0x16, 4): ("h", 2, 1 / 256), (0x17, 4): ("h", 2, 1 / 256),
    (0x26, 4): ("h", 2, 1 / 4096), (0xFC, 4): ("h", 2, 1 / 1024),
}
# shader hash -> {GX attribute: (component type, frac)} as set by the shader's vertex-format
# function in main.dol (GX_VA_POS 9, NRM 10, CLR0 11, TEX0.. 13..; type 1 s8, 3 s16, 4 f32,
# 5 rgba8). Texture set N is GX_VA_TEX0 + N.
SHADER_VAT = {
    0xBACEA013: {9: (4, 0), 10: (4, 0), 13: (3, 10)},                                 # crowdskin
    0x55951F36: {9: (4, 0), 10: (1, 6), 13: (3, 10), 14: (3, 10), 15: (3, 10)},        # hippobasicenvironment
    0xC89C219A: {9: (4, 0), 10: (4, 0), 13: (3, 10), 14: (3, 10), 15: (3, 10)},        # hippodiffuseskin
    0x2DFB08EA: {9: (4, 0), 10: (4, 0), 13: (3, 10)},                                  # ropeskin
    0xF2D57AC6: {9: (4, 0), 11: (5, 0), 13: (3, 10), 14: (3, 10), 15: (3, 10)},        # stadiumdetailmaskwithuvsliding
    0x32BC21E8: {9: (4, 0), 10: (1, 6), 11: (5, 0), 13: (3, 10), 14: (3, 10), 15: (3, 10), 16: (3, 10)},  # stadiumflatreflection
    0xEE9D919D: {9: (4, 0), 13: (3, 12)},                                              # constantcolour
    0x21DB4385: {9: (4, 0), 13: (4, 0)},                                               # diffuse
    0x46ABE398: {9: (4, 0), 13: (3, 8), 14: (3, 8)},                                   # diffusedetail
}
PALETTE_LIMIT = 9        # bones per mesh palette (retail maximum; GX has ten matrix slots)
INFLUENCE_LIMIT = 4      # u8 x 4 bone indices / f32 x 4 weights per vertex
INDEX_LIMIT = 0xFFFF     # u16 vertex count and u16 strip indices

# Shader hash -> material record bytes; each shader occurs with exactly one span.
MATERIAL_SIZES = {
    0xC89C219A: 204,  # hippodiffuseskin (fighters; fields documented in MATERIALS.md)
    0x55951F36: 140,  # hippobasicenvironment
    0xEE5973A5: 184,  # hippodiffusenonskin
    0x485F111C: 364,  # hippohwlitenvironment
    0xF2D57AC6: 52,   # stadiumdetailmaskwithuvsliding
    0xBACEA013: 40,   # crowdskin
    0xA8F6FE22: 40,   # crowdskindk
    0x32BC21E8: 48,   # stadiumflatreflection
    0x2DFB08EA: 32,   # ropeskin
    0x46ABE398: 20,   # diffusedetail
    0xEE9D919D: 24,   # constantcolour
    0x21DB4385: 8,    # diffuse
}


class ModelFormatError(ValueError):
    pass


def _u32(b, o): return struct.unpack_from(">I", b, o)[0]


class Attribute:
    __slots__ = ("type", "stride", "flags", "offset", "count", "raw", "shader")

    def __init__(self, type_, stride, flags, offset, count, raw, shader=None):
        self.type, self.stride, self.flags, self.offset, self.count, self.raw = type_, stride, flags, offset, count, raw
        self.shader = shader

    @property
    def semantic(self): return SEMANTICS.get(self.flags >> 8, "unknown")

    @property
    def set(self): return self.flags & 0xFF

    @property
    def storage(self):
        special = {(0x16, 4): 0x46ABE398, (0x17, 4): 0x46ABE398,
                   (0x26, 4): 0xEE9D919D, (0xFC, 4): 0x32BC21E8}
        if (self.type, self.stride) in special and self.shader != special[self.type, self.stride]:
            return None
        if (self.type, self.stride) in special:
            fmt = SHADER_VAT[self.shader].get(13+self.set) if self.semantic == "texcoord" else None
            storage = STORAGE[self.type,self.stride]
            if fmt != (3,round(-math.log2(storage[2]))): return None
        return STORAGE.get((self.type, self.stride))

    @property
    def verified(self): return self.storage is not None

    def values(self):
        s = self.storage
        if s is None: return None
        code, n, scale = s
        raw = struct.unpack(">%d%s" % (n * self.count, code), self.raw)
        if scale == 1: return [raw[i:i + n] for i in range(0, len(raw), n)]
        return [tuple(x * scale for x in raw[i:i + n]) for i in range(0, len(raw), n)]

    def encode_element(self, value):
        if self.storage is None:
            raise ModelFormatError("Attribute storage has no established scale for this shader")
        code, n, scale = self.storage
        if len(value) != n or not all(math.isfinite(x) for x in value):
            raise ModelFormatError(f"{self.semantic}: expected {n} finite components")
        if code in "bhB":
            lo, hi = {"b": (-128, 127), "h": (-32768, 32767), "B": (0, 255)}[code]
            ints = [round(x / scale) if scale != 1 else round(x) for x in value]
            if any(not lo <= x <= hi for x in ints):
                raise ModelFormatError(f"{self.semantic} value {value} is outside the storage range")
            return struct.pack(">%d%s" % (n, code), *ints)
        return struct.pack(">%d%s" % (n, code), *value)

    def patched(self, values):
        """New raw bytes where only elements whose value changed are re-encoded, so an
        unchanged element keeps its exact original bytes whatever the storage quantization."""
        if not self.verified:
            raise ModelFormatError(f"{self.semantic} storage 0x{self.type:02X}/{self.stride} has no established scale; it is preserved but cannot be edited")
        if len(values) != self.count:
            raise ModelFormatError("Vertex count changed; in-place patching needs the same topology")
        old = self.values(); out = bytearray(self.raw)
        for i, (a, b) in enumerate(zip(old, values)):
            if tuple(a) != tuple(b):
                # A value that quantizes back to the stored bytes is unchanged (e.g. a float
                # round-tripped through another tool); only a real change is written.
                encoded = self.encode_element(b)
                if encoded != self.raw[i * self.stride:(i + 1) * self.stride]:
                    out[i * self.stride:(i + 1) * self.stride] = encoded
        return bytes(out)


class Mesh:
    __slots__ = ("index", "record", "strip", "attributes", "palette")

    def __init__(self, index, record, strip, attributes, palette):
        self.index, self.record, self.strip, self.attributes, self.palette = index, record, strip, attributes, palette

    vertex_count = property(lambda s: struct.unpack_from(">H", s.record, 8)[0])
    shader = property(lambda s: _u32(s.record, 16))
    name_hash = property(lambda s: _u32(s.record, 20))
    transform = property(lambda s: _u32(s.record, 24))
    material_offset = property(lambda s: _u32(s.record, 36))

    def attribute(self, semantic, set_=0):
        for a in self.attributes:
            if a.semantic == semantic and a.set == set_:
                return a
        return None

    def triangles(self):
        return strip_to_tris(self.strip)


class Node:
    __slots__ = ("index", "name_hash", "first_mesh", "mesh_count", "tail")

    def __init__(self, index, name_hash, first_mesh, mesh_count, tail):
        self.index, self.name_hash, self.first_mesh, self.mesh_count, self.tail = index, name_hash, first_mesh, mesh_count, tail

    @property
    def meshes(self): return range(self.first_mesh, self.first_mesh + self.mesh_count)


class ModelSet:
    """One model set: chunk indices into its Archive plus decoded, provenance-carrying views."""

    def __init__(self, archive, index, chunks, trailing):
        self.archive, self.index, self.chunks, self.trailing = archive, index, chunks, trailing
        get = archive.get_chunk_bytes
        self.materials = get(chunks[0xB016])
        meshes = get(chunks[0xB004]); ptrs = get(chunks[0xB005])
        idx = get(chunks[0xB007]); vtx = get(chunks[0xB006])
        if len(meshes) % 52 or len(ptrs) % 8:
            raise ModelFormatError(f"model set {index}: mesh or pointer table has a partial record")
        xforms = get(chunks[0xB002])
        if len(xforms) % 64:
            raise ModelFormatError(f"model set {index}: transform table has a partial matrix")
        self.transforms = [struct.unpack_from(">16f", xforms, o) for o in range(0, len(xforms), 64)]
        palettes = [t for t in trailing if t[0] == 0xB00B]
        if palettes and len(palettes) != len(meshes) // 52:
            raise ModelFormatError(f"model set {index}: {len(palettes)} bone palettes for {len(meshes) // 52} meshes")
        self.meshes = []
        expected_ptr = 0
        for m in range(len(meshes) // 52):
            rec = meshes[52 * m:52 * m + 52]
            istart, iflags = struct.unpack_from(">II", rec, 0)
            count, fmt = iflags & 0xFFFFFF, iflags >> 24
            vc, n_attr, ptr = struct.unpack_from(">H", rec, 8)[0], rec[11], _u32(rec, 12)
            if ptr != expected_ptr or ptr + 8 * n_attr > len(ptrs):
                raise ModelFormatError(f"model set {index} mesh {m}: attribute pointers out of order")
            expected_ptr += 8 * n_attr
            width = 2 if fmt == 0 else 1
            if istart + count * width > len(idx):
                raise ModelFormatError(f"model set {index} mesh {m}: index strip extends past its chunk")
            strip = list(struct.unpack_from(">%dH" % count, idx, istart)) if fmt == 0 else list(idx[istart:istart + count])
            attrs = []
            for k in range(n_attr):
                off, typ, stride, flags = struct.unpack_from(">IBBH", ptrs, ptr + 8 * k)
                if off + stride * vc > len(vtx):
                    raise ModelFormatError(f"model set {index} mesh {m}: attribute {k} extends past vertex data")
                attrs.append(Attribute(typ, stride, flags, off, vc, bytes(vtx[off:off + stride * vc]), _u32(rec, 16)))
            if _u32(rec, 24) >= len(self.transforms):
                raise ModelFormatError(f"model set {index} mesh {m}: transform index out of range")
            palette = None
            if palettes:
                raw = get(palettes[m][1]); palette = [_u32(raw, o) for o in range(0, len(raw) - 3, 4)]
            self.meshes.append(Mesh(m, bytes(rec), strip, attrs, palette))
        nodes = get(chunks[0xB003]); self.nodes = []; first = 0
        for o in range(0, len(nodes) - 11, 12):
            h, n, tail = struct.unpack_from(">III", nodes, o)
            self.nodes.append(Node(o // 12, h, first, n, tail)); first += n
        if first != len(self.meshes):
            raise ModelFormatError(f"model set {index}: nodes cover {first} of {len(self.meshes)} meshes")
        bones = [t for t in trailing if t[0] == 0xB00A]
        self.bones = []
        for _, ri in bones:
            raw = get(ri)
            self.bones += [(_u32(raw, o), struct.unpack_from(">16f", raw, o + 4)) for o in range(0, len(raw) - 67, 68)]

    @property
    def skinned(self): return any(t[0] == 0xB00A for t in self.trailing)

    def node_of(self, mesh_index):
        for n in self.nodes:
            if mesh_index in n.meshes: return n
        raise IndexError(mesh_index)

    def material_span(self, mesh):
        """(offset, size) of the mesh's material record: to the next referenced offset."""
        offsets = sorted({m.material_offset for m in self.meshes}) + [len(self.materials)]
        i = offsets.index(mesh.material_offset)
        return mesh.material_offset, offsets[i + 1] - offsets[i]

    def material_record(self, mesh):
        o, n = self.material_span(mesh)
        return self.materials[o:o + n]

    def chunk_ids(self):
        return [self.chunks[t] for t in MAIN] + [ri for _, ri in self.trailing]


def model_sets(archive):
    """Every model set in chunk order. Raises ModelFormatError on an unexpected layout."""
    result = []; run = None
    for ri in archive.find_chunks():
        t = archive.chunks[ri][2]
        if t == 0xB016:
            run = {"main": {}, "trailing": [], "order": []}; result.append(run)
        if run is None:
            continue
        if t in MAIN and not run["trailing"] and t not in run["main"]:
            run["main"][t] = ri; run["order"].append(t)
        elif t in TRAILING and len(run["main"]) == len(MAIN):
            run["trailing"].append((t, ri))
        elif t in MAIN or t in TRAILING:
            raise ModelFormatError(f"chunk {ri} (0x{t:04X}) is out of model-set order")
        else:
            run = None  # a non-model chunk ends the run
    sets = []
    for i, run in enumerate(result):
        if tuple(run["order"]) != MAIN:
            raise ModelFormatError(f"model set {i} has chunks {['%04X' % t for t in run['order']]}")
        sets.append(ModelSet(archive, i, run["main"], run["trailing"]))
    return sets


def strip_to_tris(strip):
    """One triangle strip to a triangle list (skips degenerate triangles)."""
    tris = []
    for i in range(len(strip) - 2):
        a, b, c = strip[i], strip[i + 1], strip[i + 2]
        if a == b or b == c or a == c:
            continue
        tris.append((a, b, c) if i % 2 == 0 else (a, c, b))
    return tris


def world_matrix(m):
    """Stored row-vector matrix -> column-vector rows (what Blender's Matrix() expects)."""
    return [[m[c * 4 + r] for c in range(4)] for r in range(4)]


# ---------------------------------------------------------------------------------------
# Changed-byte patch sets. An edit never rebuilds a chunk; it overwrites fixed-size byte
# ranges and records the original bytes, so an untouched archive is byte-identical by
# construction and every change can be audited.

class Patch:
    """Fixed-size change inside one chunk; `section` selects the part of a multi-section
    cinematic container (0 for ordinary archives)."""
    __slots__ = ("chunk", "offset", "old", "new", "label", "section")

    def __init__(self, chunk, offset, old, new, label, section=0):
        self.chunk, self.offset, self.old, self.new, self.label = chunk, offset, bytes(old), bytes(new), label
        self.section = section

    def as_dict(self):
        return {"section": self.section, "chunk": self.chunk, "offset": self.offset, "bytes": len(self.new), "label": self.label}


def diff_patches(chunk, base_offset, old, new, label, merge_gap=16, section=0):
    """Minimal patches turning old into new (same length); nearby runs are merged."""
    if len(old) != len(new):
        raise ModelFormatError(f"{label}: size changed ({len(old)} -> {len(new)}); in-place patching needs equal sizes")
    patches = []; i = 0; n = len(old)
    while i < n:
        if old[i] == new[i]:
            i += 1; continue
        j = i + 1
        while j < n:
            if old[j] != new[j]:
                j += 1; continue
            k = j
            while k < n and k - j < merge_gap and old[k] == new[k]: k += 1
            if k < n and k - j < merge_gap: j = k
            else: break
        patches.append(Patch(chunk, base_offset + i, old[i:j], new[i:j], label, section)); i = j
    return patches


def geometry_patches(model_set, edits, section=0):
    """edits: {mesh_index: {(semantic, set): [values per vertex]}} -> list of Patch on B006.
    Values use the decoded convention of Attribute.values() (native Z-up coordinates)."""
    ri = model_set.chunks[0xB006]; patches = []
    for mi, per_attr in edits.items():
        mesh = model_set.meshes[mi]
        for (semantic, set_), values in per_attr.items():
            attr = mesh.attribute(semantic, set_)
            if attr is None:
                raise ModelFormatError(f"mesh {mi} has no {semantic} set {set_}")
            new = attr.patched(values)
            changed = sum(new[i:i + attr.stride] != attr.raw[i:i + attr.stride] for i in range(0, len(new), attr.stride))
            noun = {"position": "vertex position", "normal": "normal", "texcoord": "UV"}.get(semantic, semantic)
            label = f"model set {model_set.index}, mesh {mi}: {changed} {noun}{'s' if changed != 1 else ''} changed" + \
                    (f" (set {set_})" if set_ else "")
            patches += diff_patches(ri, attr.offset, attr.raw, new, label, section=section)
    return patches


def apply_patches(archive, patches):
    """Write patches into the archive in place after checking every original byte."""
    chunks = set(archive.find_chunks())
    for p in patches:
        if type(p.chunk) is not int or p.chunk not in chunks or type(p.offset) is not int or len(p.old) != len(p.new):
            raise ModelFormatError("Fixed patches need a valid chunk, integer offset and equal old/new byte lengths")
        block = archive._chunk_block(p.chunk)
        size, offset = archive.chunks[p.chunk][3:5]
        if block < 0 or p.offset < 0 or p.offset + len(p.new) > size:
            raise ModelFormatError(f"patch {p.label} leaves chunk {p.chunk}")
        start = offset + p.offset
        if bytes(archive.blocks[block][start:start + len(p.old)]) != p.old:
            raise ModelFormatError(f"patch {p.label}: source bytes differ from the recorded original")
        archive.blocks[block][start:start + len(p.new)] = p.new
    return archive


def changed_ranges(before, after):
    """Byte ranges [start, end) where two equal-length buffers differ (changed-range audit)."""
    if len(before) != len(after):
        return [(0, max(len(before), len(after)))]
    out = []; i = 0
    while i < len(before):
        if before[i] != after[i]:
            j = i
            while j < len(before) and before[j] != after[j]: j += 1
            out.append((i, j)); i = j
        else:
            i += 1
    return out


# ---------------------------------------------------------------------------------------
# Rebuilding model sets (topology edits). A mesh's content is its strip, one raw array per
# source attribute pointer (same storage, same order) and its bone palette; re-encoding with
# the retail packing rules reproduces an unchanged set exactly, so only edited meshes differ.

class MeshData:
    """Content for one mesh slot. `source` maps each output vertex to the source vertex whose
    morph records it inherits (None = identity, for an unchanged vertex list)."""
    __slots__ = ("strip", "vertex_count", "arrays", "palette", "source")

    def __init__(self, strip, vertex_count, arrays, palette=None, source=None):
        self.strip, self.vertex_count, self.arrays = list(strip), vertex_count, [bytes(a) for a in arrays]
        self.palette = None if palette is None else list(palette); self.source = source

    def digest(self):
        import hashlib
        h = hashlib.sha256(struct.pack(">I%dH" % len(self.strip), self.vertex_count, *self.strip))
        for a in self.arrays: h.update(struct.pack(">I", len(a))); h.update(a)
        if self.palette is not None: h.update(struct.pack(">%dI" % len(self.palette), *self.palette))
        return h.hexdigest()


def mesh_data(mesh):
    return MeshData(mesh.strip, mesh.vertex_count, [a.raw for a in mesh.attributes], mesh.palette)


def vertex_filler(model_set):
    """The set's B006 gap pattern: every retail gap is a prefix of the longest one."""
    vtx = model_set.archive.get_chunk_bytes(model_set.chunks[0xB006])
    spans = sorted((a.offset, a.offset + a.stride * a.count) for m in model_set.meshes for a in m.attributes)
    best = b""
    for k, (_, end) in enumerate(spans):
        nxt = spans[k + 1][0] if k + 1 < len(spans) else len(vtx)
        if nxt - end > len(best): best = vtx[end:nxt]
    return bytes(best)


def skin_attributes(mesh):
    """(weights, indices) of a skinned mesh whatever their set number (hippodiffuseskin keeps
    its u8 palette indices in set 1, other skins in set 0); (None, None) if either is missing."""
    w = next((a for a in mesh.attributes if a.semantic == "weights" and a.verified), None)
    i = next((a for a in mesh.attributes if a.semantic == "indices" and a.verified), None)
    return (w, i) if w is not None and i is not None and (w.type, w.stride, i.type, i.stride) == (0xB0, 16, 0xD4, 4) else (None, None)


def check_skin(where, weights, indices, palette, bones=None):
    """Retail skin rules (all 2,846 retail skinned meshes, 490k vertices, satisfy them):
    <= 4 influences, nonzero weights first, empty slots are index 0 with weight 0, weights
    positive and summing to 1 within 1e-5, one slot per bone, every palette entry used, the
    palette unique, at most PALETTE_LIMIT bones, all present in B00A when `bones` is given."""
    n = len(indices) // 4
    if len(set(palette)) != len(palette) or len(palette) > PALETTE_LIMIT:
        raise ModelFormatError(f"{where}: palette of {len(palette)} bones must be unique and at most {PALETTE_LIMIT} (retail limit)")
    if bones is not None and any(h not in bones for h in palette):
        raise ModelFormatError(f"{where}: palette names a bone that is not in the model set's B00A bone table")
    used = set()
    for v in range(n):
        ww = struct.unpack_from(">4f", weights, 16 * v); ii = indices[4 * v:4 * v + 4]
        k = sum(1 for x in ww if x != 0)
        if not all(math.isfinite(x) and x >= 0 for x in ww) or any(ww[j] == 0 for j in range(k)) \
                or any(ii[j] for j in range(k, 4)) or abs(sum(ww) - 1) > 1e-5 or k == 0:
            raise ModelFormatError(f"{where}: vertex {v} skin weights break the retail layout")
        if any(ii[j] >= len(palette) for j in range(k)) or len(set(ii[:k])) != k:
            raise ModelFormatError(f"{where}: vertex {v} names a bone slot past its {len(palette)}-entry palette or twice")
        used.update(ii[:k])
    if used != set(range(len(palette))):
        raise ModelFormatError(f"{where}: every palette entry must be used by some weight (retail rule)")


def check_mesh_data(mesh, d, bones=None):
    """Refuse content the retail layout or the GX limits cannot hold. `bones` (B00A hashes)
    authorizes palette changes: skin arrays may then differ from the source map."""
    where = f"mesh {mesh.index}"
    if d.source is None:
        if d.vertex_count != mesh.vertex_count:
            raise ModelFormatError(f"{where}: resized vertices require an explicit source-vertex map")
    elif len(d.source) != d.vertex_count or any(type(i) is not int or not 0 <= i < mesh.vertex_count for i in d.source):
        raise ModelFormatError(f"{where}: source-vertex map must name one original vertex per output vertex")
    if len(d.arrays) != len(mesh.attributes):
        raise ModelFormatError(f"{where}: {len(d.arrays)} attribute arrays for {len(mesh.attributes)} pointers")
    if not 0 < d.vertex_count <= INDEX_LIMIT:
        raise ModelFormatError(f"{where}: {d.vertex_count} vertices; the u16 index format holds 1 to {INDEX_LIMIT}")
    skin = [a for a in skin_attributes(mesh) if a is not None] if d.palette is not None else []
    skinned = False
    for a, raw in zip(mesh.attributes, d.arrays):
        if len(raw) != a.stride * d.vertex_count:
            raise ModelFormatError(f"{where}: {a.semantic}{a.set} array has {len(raw)} bytes for {d.vertex_count} vertices")
        if a.semantic not in ("position", "normal", "texcoord") or not a.verified:
            source = range(mesh.vertex_count) if d.source is None else d.source
            expected = b"".join(a.raw[i * a.stride:(i + 1) * a.stride] for i in source)
            if raw != expected:
                if a not in skin:
                    raise ModelFormatError(f"{where}: preserved {a.semantic}{a.set} data differs from its source map")
                skinned = True
        elif a.storage[0] == "f" and not all(math.isfinite(v[0]) for v in struct.iter_unpack(">f", raw)):
            raise ModelFormatError(f"{where}: nonfinite {a.semantic} data")
    if mesh.record[4] != 0:
        raise ModelFormatError(f"{where}: 8-bit strips are not written (no retail mesh uses them)")
    if not 3 <= len(d.strip) < 1 << 24 or any(type(i) is not int or not 0 <= i < d.vertex_count for i in d.strip):
        raise ModelFormatError(f"{where}: strip has {len(d.strip)} indices or points past the vertex list")
    if (mesh.palette is None) != (d.palette is None):
        raise ModelFormatError(f"{where}: a bone palette can only be kept, not added or removed")
    if d.palette is not None:
        if (skinned or d.palette != mesh.palette) and (bones is None or len(skin) != 2):
            raise ModelFormatError(f"{where}: skin weight/palette edits need the model set's B00A bones and skin arrays")
        if len(d.palette) > PALETTE_LIMIT:
            raise ModelFormatError(f"{where}: uses {len(d.palette)} bones; a mesh can use at most {PALETTE_LIMIT} (retail limit)")
        wts, idx = skin_attributes(mesh)
        if wts is not None:
            ww = d.arrays[mesh.attributes.index(wts)]; ii = d.arrays[mesh.attributes.index(idx)]
            if skinned or d.palette != mesh.palette:
                check_skin(where, ww, ii, d.palette, bones)
            elif any(struct.unpack_from(">f", ww, 4 * j)[0] > 0 and ii[j] >= len(d.palette) for j in range(len(ii))):
                raise ModelFormatError(f"{where}: a bone index points past its {len(d.palette)}-entry palette")


def influences(mesh, d=None):
    """Per vertex {bone hash: weight} through the palette (nonzero weights only)."""
    wts, idx = skin_attributes(mesh)
    if wts is None: return None
    d = d or mesh_data(mesh)
    ww = d.arrays[mesh.attributes.index(wts)]; ii = d.arrays[mesh.attributes.index(idx)]
    out = []
    for v in range(d.vertex_count):
        w = struct.unpack_from(">4f", ww, 16 * v)
        out.append({d.palette[ii[4 * v + j]]: w[j] for j in range(4) if w[j] != 0})
    return out


def reskin(mesh, d, overrides, bones):
    """MeshData `d` (for source mesh `mesh`) with the skin of some output vertices replaced.
    overrides: {output vertex: {bone hash: weight}}, at most INFLUENCE_LIMIT positive weights,
    normalized here. Edited vertices are written heaviest first, the largest weight absorbing
    float32 rounding so every sum is 1. Palette entries no vertex uses any more are dropped and
    new bones are appended in first-use order; unchanged vertices keep their weight bytes and
    only have their slot indices renumbered when the palette moves. Bones must be in B00A."""
    wts, idx = skin_attributes(mesh)
    if wts is None or d.palette is None:
        raise ModelFormatError(f"mesh {mesh.index} has no editable skin (weights, palette indices and B00B palette)")
    semantic = influences(mesh, d)
    for v, weights in overrides.items():
        if type(v) is not int or not 0 <= v < d.vertex_count:
            raise ModelFormatError(f"mesh {mesh.index}: skin edit names vertex {v}, which does not exist")
        weights = {h: float(w) for h, w in weights.items() if w > 0}
        if not weights or len(weights) > INFLUENCE_LIMIT or not all(math.isfinite(w) for w in weights.values()):
            raise ModelFormatError(f"mesh {mesh.index} vertex {v}: needs 1 to {INFLUENCE_LIMIT} finite positive bone weights")
        missing = [h for h in weights if h not in bones]
        if missing:
            raise ModelFormatError(f"mesh {mesh.index} vertex {v}: bone {missing[0]:08X} is not in the model set's B00A bone table")
        semantic[v] = weights
    used = {h for w in semantic for h in w}
    palette = [h for h in d.palette if h in used]
    for w in semantic:
        for h in sorted(w, key=lambda h: (-w[h], h)):
            if h not in palette: palette.append(h)
    if len(palette) > PALETTE_LIMIT:
        raise ModelFormatError(f"mesh {mesh.index}: the edited skin uses {len(palette)} bones; a mesh can use at most {PALETTE_LIMIT} (retail limit)")
    slot = {h: k for k, h in enumerate(palette)}
    old_w = d.arrays[mesh.attributes.index(wts)]; old_i = d.arrays[mesh.attributes.index(idx)]
    new_w, new_i = bytearray(old_w), bytearray(old_i)
    f32 = lambda x: struct.unpack(">f", struct.pack(">f", x))[0]
    for v in range(d.vertex_count):
        if v in overrides:
            w = semantic[v]; total = sum(w.values())
            order = sorted(w, key=lambda h: (-w[h], slot[h]))
            rest = [f32(w[h] / total) for h in order[1:]]
            values = [f32(1.0 - sum(rest))] + rest
            values += [0.0] * (4 - len(values)); slots = [slot[h] for h in order] + [0] * (4 - len(order))
            new_w[16 * v:16 * v + 16] = struct.pack(">4f", *values); new_i[4 * v:4 * v + 4] = bytes(slots)
        else:
            for j in range(4):
                if struct.unpack_from(">f", old_w, 16 * v + 4 * j)[0] != 0:
                    new_i[4 * v + j] = slot[d.palette[old_i[4 * v + j]]]
    arrays = list(d.arrays)
    arrays[mesh.attributes.index(wts)] = bytes(new_w); arrays[mesh.attributes.index(idx)] = bytes(new_i)
    out = MeshData(d.strip, d.vertex_count, arrays, palette, d.source)
    check_mesh_data(mesh, out, bones)
    return out


def copies_layout(model_set, copies):
    """Output copies per source mesh (0 deletes, 2 duplicates) -> source mesh per output mesh."""
    if len(copies) != len(model_set.meshes) or any(type(n) is not int or n < 0 for n in copies):
        raise ModelFormatError(f"Mesh table edit needs one copy count per source mesh ({len(model_set.meshes)})")
    return [m for m, n in enumerate(copies) for _ in range(n)]


def check_layout(model_set, layout):
    """A whole-mesh table edit: output mesh k is a copy of source mesh layout[k]. Source order is
    kept, copies follow their original inside the same B003 node and every node keeps a mesh.
    Skinned sets are refused: each mesh owns one B00B chunk record listed by count in a 0xB008
    directory, so adding or removing a mesh changes the archive's chunk table, which the
    single-section relocation writer does not do. Meshes whose name hash another chunk of the
    section references (animation/node tables) are refused too."""
    n = len(model_set.meshes)
    if not layout or any(type(s) is not int or not 0 <= s < n for s in layout) or list(layout) != sorted(layout):
        raise ModelFormatError("Mesh table edit must list existing source meshes in source order")
    if list(layout) == list(range(n)): return
    if any(t[0] in (0xB00B, 0xB00A, 0xB00C) for t in model_set.trailing):
        raise ModelFormatError("Whole-mesh deletion or duplication in skinned model sets needs a B00B chunk per mesh; "
                               "the archive chunk table cannot gain or lose records here")
    for node in model_set.nodes:
        if not any(s in node.meshes for s in layout):
            raise ModelFormatError(f"B003 node {node.name_hash:08X} would lose its last mesh; node deletion is not supported")
    touched = {model_set.meshes[s].name_hash for s in set(range(n)) ^ set(layout)} | \
              {model_set.meshes[s].name_hash for s in layout if layout.count(s) > 1}
    offsets = [m.material_offset for m in model_set.meshes]
    if len(set(offsets)) != n or offsets != sorted(offsets) or \
            sum(model_set.material_span(m)[1] for m in model_set.meshes) != len(model_set.materials):
        raise ModelFormatError("Material records are shared or out of mesh order; the mesh table cannot be repacked")
    a = model_set.archive; own = {ri for s in model_sets(a) for ri in s.chunk_ids()}
    for ri in a.find_chunks():
        if ri in own or a.chunks[ri][2] == 0x6101: continue
        raw = a.get_chunk_bytes(ri)
        hit = next((h for h in touched if struct.pack(">I", h) in raw), None)
        if hit is not None:
            raise ModelFormatError(f"Mesh name {hit:08X} is referenced by chunk {ri} (0x{a.chunks[ri][2]:04X}); its mesh slots stay locked")


def encode_model_set(model_set, changed=None, layout=None):
    """{chunk record index: payload} for B007/B006/B005/B004, the B00B palettes and B00C,
    with `changed` ({output mesh index: MeshData}) substituted. With `layout` (see
    check_layout) B016 and B003 are regenerated too: every retail material record belongs to
    exactly one mesh, in mesh order, so B016 is the records in mesh order and each mesh's +36
    is its running offset. Unchanged input -> original bytes."""
    changed = changed or {}
    tables = layout is not None
    layout = list(range(len(model_set.meshes))) if layout is None else list(layout)
    if tables: check_layout(model_set, layout)
    if any(type(mi) is not int or not 0 <= mi < len(layout) for mi in changed):
        raise ModelFormatError("Topology edit names a nonexistent mesh")
    bones = {h for h, _ in model_set.bones}
    filler = vertex_filler(model_set)
    idx, vtx, ptrs, recs, mats = bytearray(), bytearray(), bytearray(), bytearray(), bytearray()
    pad = lambda n: (filler[:n] + bytes(n))[:n]
    palettes = [ri for t, ri in model_set.trailing if t == 0xB00B]
    out = {}
    for k, s in enumerate(layout):
        m = model_set.meshes[s]
        d = changed.get(k) or mesh_data(m)
        if k in changed: check_mesh_data(m, d, bones)
        rec = bytearray(m.record)
        struct.pack_into(">IIH", rec, 0, len(idx), len(d.strip), d.vertex_count)
        struct.pack_into(">I", rec, 12, len(ptrs))
        if tables:
            struct.pack_into(">I", rec, 36, len(mats)); mats += model_set.material_record(m)
        idx += struct.pack(">%dH" % len(d.strip), *d.strip)
        for a, raw in zip(m.attributes, d.arrays):
            vtx += pad(-len(vtx) % 32)
            ptrs += struct.pack(">IBBH", len(vtx), a.type, a.stride, a.flags)
            vtx += raw
        recs += rec
        if palettes:
            out[palettes[m.index]] = struct.pack(">%dI" % len(d.palette), *d.palette)
    vtx += pad(-len(vtx) % 32)
    out.update({model_set.chunks[0xB007]: bytes(idx), model_set.chunks[0xB006]: bytes(vtx),
                model_set.chunks[0xB005]: bytes(ptrs), model_set.chunks[0xB004]: bytes(recs)})
    if tables:
        out[model_set.chunks[0xB016]] = bytes(mats)
        out[model_set.chunks[0xB003]] = b"".join(struct.pack(">III", n.name_hash, sum(s in n.meshes for s in layout), n.tail)
                                                 for n in model_set.nodes)
    for t, ri in model_set.trailing:
        if t == 0xB00C:
            morphs = parse_morphs(model_set.archive.get_chunk_bytes(ri))
            if len(morphs["lists"]) != len(model_set.meshes):
                raise ModelFormatError("B00C mesh count differs from the model set")
            for mi, channels in enumerate(morphs["lists"]):
                if any(v >= model_set.meshes[mi].vertex_count for shapes in channels for recs in shapes for _, v in recs):
                    raise ModelFormatError("B00C morph index is outside its source mesh")
            maps = {mi: d.source for mi, d in changed.items() if d.source is not None}
            out[ri] = encode_morphs(remap_morphs(morphs, maps) if maps else morphs)
    return out


def rebuild_chunks(model_set, changed, layout=None):
    """[(chunk, new payload)] for the chunks whose bytes an edit actually changes."""
    get = model_set.archive.get_chunk_bytes
    identity = None if layout is None else list(range(len(model_set.meshes)))
    if any(raw != get(ri) for ri, raw in encode_model_set(model_set, layout=identity).items()):
        raise ModelFormatError("Model packing is not byte-exact under the established retail layout; topology edit refused")
    return [(ri, raw) for ri, raw in sorted(encode_model_set(model_set, changed, layout).items()) if raw != get(ri)]


def remap_mesh(mesh, source, triangles, edits=None):
    """Add/delete/reorder vertices by explicit original-vertex provenance, preserving every
    coupled attribute. New vertices inherit a chosen original vertex's skin/morph data;
    position, normal and proven UV values can then be supplied per output vertex."""
    source = list(source)
    if not source or any(type(i) is not int or not 0 <= i < mesh.vertex_count for i in source):
        raise ModelFormatError("Each output vertex must name a valid original vertex")
    arrays = [b"".join(a.raw[i * a.stride:(i + 1) * a.stride] for i in source) for a in mesh.attributes]
    for (semantic, set_), values in (edits or {}).items():
        a = mesh.attribute(semantic, set_)
        if a is None or not a.verified or semantic not in ("position", "normal", "texcoord"):
            raise ModelFormatError(f"Cannot edit {semantic}{set_} during topology remapping")
        if len(values) != len(source): raise ModelFormatError("Attribute count differs from the output vertex count")
        arrays[mesh.attributes.index(a)] = b"".join(a.encode_element(v) for v in values)
    d = MeshData(tris_to_strip(triangles), len(source), arrays, mesh.palette, source)
    check_mesh_data(mesh, d)
    return d


def tris_to_strip(tris):
    """Triangle list -> one strip with the same triangles and winding. Greedy: the strip runs
    on across a shared edge while the alternating winding allows, otherwise it joins the next
    run with two repeated indices (degenerate triangles, skipped by the GPU and strip_to_tris)."""
    tris = [tuple(t) for t in tris]
    if any(len(t) != 3 or any(type(i) is not int or i < 0 for i in t) for t in tris):
        raise ModelFormatError("Faces must be triangles with nonnegative integer vertex indices")
    tris = [t for t in tris if len(set(t)) == 3]
    edges = {}
    for n, (a, b, c) in enumerate(tris):
        for u, v, w in ((a, b, c), (b, c, a), (c, a, b)):
            edges.setdefault((u, v), []).append((n, w))
    used = [False] * len(tris); strip = []
    for n, (a, b, c) in enumerate(tris):
        if used[n]: continue
        used[n] = True
        if strip: strip += [strip[-1], a]
        strip += [a, b, c] if len(strip) % 2 == 0 else [a, c, b]
        while True:
            u, v = strip[-2], strip[-1]
            key = (u, v) if (len(strip) - 2) % 2 == 0 else (v, u)
            nxt = next(((k, w) for k, w in edges.get(key, ()) if not used[k]), None)
            if nxt is None: break
            used[nxt[0]] = True; strip.append(nxt[1])
    return strip


# B00C: u32 shape count A, u32 channel count B, A f32 breakpoints, u32 16, u32 S (= meshes),
# then per mesh and channel: u32 shapes, per shape u32 records, per record 3 f32 delta +
# u32 LOCAL vertex index (nlg_morph documents the grammar). Records are kept as raw deltas.

def parse_morphs(raw):
    if len(raw) < 16: raise ModelFormatError("B00C morph header is truncated")
    count, channels = struct.unpack_from(">2I", raw, 0); o = 8 + 4 * count
    if o + 8 > len(raw): raise ModelFormatError("B00C morph breakpoints are truncated")
    size, meshes = struct.unpack_from(">2I", raw, o); o += 8
    if size != 16: raise ModelFormatError(f"B00C record size {size} is not 16")
    if meshes > INDEX_LIMIT or channels * meshes > (len(raw) - o) // 4:
        raise ModelFormatError("B00C mesh/channel counts exceed the available records")
    lists = []
    for _ in range(meshes):
        chans = []
        for _ in range(channels):
            if o + 4 > len(raw): raise ModelFormatError("B00C morph list is truncated")
            n = _u32(raw, o); o += 4; shapes = []
            for _ in range(n):
                if o + 4 > len(raw): raise ModelFormatError("B00C morph shape is truncated")
                r = _u32(raw, o); o += 4
                if o + 16 * r > len(raw): raise ModelFormatError("B00C morph records are truncated")
                shapes.append([(raw[p:p + 12], _u32(raw, p + 12)) for p in range(o, o + 16 * r, 16)]); o += 16 * r
            chans.append(shapes)
        lists.append(chans)
    if o != len(raw): raise ModelFormatError("B00C has trailing bytes")
    return {"head": bytes(raw[:8 + 4 * count]), "lists": lists}


def encode_morphs(m):
    out = bytearray(m["head"]) + struct.pack(">2I", 16, len(m["lists"]))
    for chans in m["lists"]:
        for shapes in chans:
            out += struct.pack(">I", len(shapes))
            for recs in shapes:
                out += struct.pack(">I", len(recs))
                for delta, v in recs: out += delta + struct.pack(">I", v)
    return bytes(out)


def remap_morphs(m, maps):
    """maps: {mesh index: [source vertex or None per output vertex]}. Each record follows every
    output vertex made from its source vertex; deleted vertices drop their records."""
    lists = list(m["lists"])
    for mi, source in maps.items():
        inverse = {}
        for j, s in enumerate(source):
            if s is not None: inverse.setdefault(s, []).append(j)
        lists[mi] = [[[(delta, j) for delta, v in recs for j in inverse.get(v, ())] for recs in shapes]
                     for shapes in m["lists"][mi]]
    return {"head": m["head"], "lists": lists}


# ---------------------------------------------------------------------------------------
# 0x6101 culling tree (see the module docstring for the layout and its proof).

class CullNode:
    __slots__ = ("chunk", "box", "items", "children")

    def __init__(self, chunk, box, items):
        self.chunk, self.box, self.items, self.children = chunk, box, items, []


def culling_tree(archive):
    """Culling nodes in chunk (pre-)order with child links; [] when the section has none."""
    raw_nodes = []
    for ri in archive.find_chunks(type_id=0x6101):
        raw = archive.get_chunk_bytes(ri)
        if len(raw) < 40: raise ModelFormatError(f"0x6101 chunk {ri} is shorter than its 40-byte header")
        count, reserved, first, second = struct.unpack_from(">4I", raw, 24)
        if len(raw) != 40 + 4 * count or reserved != 0 or first not in (0, 1) or first != second:
            raise ModelFormatError(f"0x6101 chunk {ri} has an unexpected layout")
        box = struct.unpack_from(">6f", raw, 0)
        if not all(math.isfinite(v) for v in box) or any(box[i] > box[i+3] for i in range(3)):
            raise ModelFormatError(f"0x6101 chunk {ri} has invalid bounds")
        raw_nodes.append((CullNode(ri, struct.unpack_from(">6f", raw, 0), struct.unpack_from(">%dI" % count, raw, 40)), first))
    if not raw_nodes: return []
    pos, stack = 1, [(0, 2 if raw_nodes[0][1] else 0)]   # iterative pre-order walk
    while stack:
        parent, missing = stack[-1]
        if not missing: stack.pop(); continue
        if pos >= len(raw_nodes): raise ModelFormatError("0x6101 tree is missing children")
        stack[-1] = (parent, missing - 1); raw_nodes[parent][0].children.append(pos)
        stack.append((pos, 2 if raw_nodes[pos][1] else 0)); pos += 1
    if pos != len(raw_nodes): raise ModelFormatError("0x6101 chunks hold more than one tree")
    return [n for n, _ in raw_nodes]


def node_bounds(sets):
    """B003 node hash -> (min xyz, max xyz) of its meshes in world space (float64)."""
    out = {}
    for s in sets:
        for node in s.nodes:
            lo, hi = [math.inf] * 3, [-math.inf] * 3
            for mi in node.meshes:
                mesh = s.meshes[mi]; M = s.transforms[mesh.transform]
                position = mesh.attribute("position")
                if position is None or not position.verified: raise ModelFormatError("Culling needs verified vertex positions")
                for x, y, z in position.values():
                    for j in range(3):
                        w = x * M[j] + y * M[4 + j] + z * M[8 + j] + M[12 + j]
                        if not math.isfinite(w): raise ModelFormatError("Culling geometry contains nonfinite world coordinates")
                        if w < lo[j]: lo[j] = w
                        if w > hi[j]: hi[j] = w
            if lo[0] <= hi[0]:
                old = out.get(node.name_hash)
                out[node.name_hash] = (lo, hi) if old is None else \
                    ([min(a, b) for a, b in zip(old[0], lo)], [max(a, b) for a, b in zip(old[1], hi)])
    return out


def culling_boxes(tree, bounds):
    """Recomputed box per node (6 float32 values); None for a node with nothing under it."""
    boxes = [None] * len(tree)
    for i in reversed(range(len(tree))):      # reversed pre-order visits children first
        node = tree[i]; lo, hi = [math.inf] * 3, [-math.inf] * 3; parts = []
        for h in node.items:
            if h not in bounds: raise ModelFormatError(f"0x6101 chunk {node.chunk} names {h:08X}, which has no geometry")
            parts.append(bounds[h])
        parts += [(boxes[c][:3], boxes[c][3:]) for c in node.children if boxes[c] is not None]
        for a, b in parts:
            lo = [min(p, q) for p, q in zip(lo, a)]; hi = [max(p, q) for p, q in zip(hi, b)]
        boxes[i] = struct.unpack(">6f", struct.pack(">6f", *lo, *hi)) if parts else None
    return boxes


def culling_patches(archive, sets=None, section=0):
    """Patches that set every 0x6101 box to the bounds of the current geometry ([] if equal)."""
    tree = culling_tree(archive)
    if not tree: return []
    boxes = culling_boxes(tree, node_bounds(model_sets(archive) if sets is None else sets))
    patches = []
    for node, box in zip(tree, boxes):
        if box is None: continue
        old = archive.get_chunk_bytes(node.chunk)[:24]; new = struct.pack(">6f", *box)
        if old != new:
            patches += diff_patches(node.chunk, 0, old, new, f"culling bounds of 0x6101 chunk {node.chunk} follow the edited geometry",
                                    section=section)
    return patches
