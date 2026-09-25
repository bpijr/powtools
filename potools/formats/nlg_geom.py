"""
nlg_geom.py — Punch-Out!! Wii geometry extractor (mesh -> OBJ).

Reads a model's meshes from a loaded nlg_pack.Archive: positions/normals/UVs and the triangle-strip
index buffer, converts strips to triangles, and writes a Wavefront OBJ (openable in Blender).
This validates our understanding of the vertex format before we build the encoder.

Vertex attributes (SoA, per mesh; see notes): 0=pos(0x0A,12) 1=normal(0xFE,12) 2=uv(0xCC,4)
6=boneIdx(0xD4,4) 7=weights(0xB0,16), plus aux 3/4/5. Index buffer = one triangle strip (u16).
"""
import struct
import os


def _u32(b, o): return struct.unpack_from(">I", b, o)[0]
def _u16(b, o): return struct.unpack_from(">H", b, o)[0]
def _s16(b, o): return struct.unpack_from(">h", b, o)[0]
def _f32(b, o): return struct.unpack_from(">f", b, o)[0]


# --- coordinate convention ---
# Punch-Out is Z-up (native). Blender's OBJ import defaults to Y-up, so we export Y-up and convert
# back on import. Keep Blender import & export on DEFAULT settings and it round-trips.
def po_to_obj(p):   # native (x, y_depth, z_up) -> OBJ (x, y_up, z)
    return (p[0], p[2], -p[1])


def obj_to_po(p):   # inverse
    return (p[0], -p[2], p[1])


class Mesh:
    pass


def _abs(archive, ri):
    bi = archive._chunk_block(ri)
    return archive.blocks[bi], archive.chunks[ri][4], archive.chunks[ri][3]


def read_model(archive, hashnames=None):
    """Read the FIRST model (main body) of the archive. Returns list of Mesh with positions,
    normals, uvs, indices (triangle strip), and bone ids/weights."""
    hn = hashnames or {}
    # first IndexData(0xB007), VertexData(0xB006), MeshData(0xB004), attr ptr(0xB005)
    idx_ri = archive.find_chunks(type_id=0xB007)[0]
    vtx_ri = archive.find_chunks(type_id=0xB006)[0]
    mesh_ri = archive.find_chunks(type_id=0xB004)[0]
    attr_ri = archive.find_chunks(type_id=0xB005)[0]
    idxbuf = archive.get_chunk_bytes(idx_ri)
    vtxbuf = archive.get_chunk_bytes(vtx_ri)
    meshdata = archive.get_chunk_bytes(mesh_ri)
    attrdata = archive.get_chunk_bytes(attr_ri)

    nmesh = len(meshdata) // 52
    meshes = []
    pi = 0
    for m in range(nmesh):
        o = m * 52
        istart = _u32(meshdata, o)
        idxflags = _u32(meshdata, o + 4)
        icount = idxflags & 0xFFFFFF
        ifmt = idxflags >> 24
        vcount = _u16(meshdata, o + 8)
        nA = meshdata[o + 11]
        mh = _u32(meshdata, o + 20)

        attrs = []
        for k in range(nA):
            ao = (pi + k) * 8
            attrs.append((_u32(attrdata, ao), attrdata[ao + 4], attrdata[ao + 5]))  # off,type,stride
        pi += nA

        mesh = Mesh()
        mesh.name = hn.get(mh, f"{mh:08X}").replace("gj_mats/", "")
        mesh.pos, mesh.nrm, mesh.uv = [], [], []
        mesh.uv_damage, mesh.uv2 = [], []
        mesh.bidx, mesh.bwt = [], []
        for a_off, a_type, a_str in attrs:
            if a_type == 0x0A and a_str == 12:
                for v in range(vcount):
                    p = a_off + 12 * v
                    mesh.pos.append((_f32(vtxbuf, p), _f32(vtxbuf, p + 4), _f32(vtxbuf, p + 8)))
            elif a_type == 0xFE and a_str == 12:
                for v in range(vcount):
                    p = a_off + 12 * v
                    mesh.nrm.append((_f32(vtxbuf, p), _f32(vtxbuf, p + 4), _f32(vtxbuf, p + 8)))
            elif a_type == 0xCC and a_str == 4:
                for v in range(vcount):
                    p = a_off + 4 * v
                    # UVs are SIGNED s16/1024, not unsigned. Read unsigned, 42 meshes across
                    # bearhugger/glassjoe/littlemac/donkeykong come out with UVs up to 64.0
                    # (i.e. raw values near 0xFFFF); read signed they all land in a clean ~0..1
                    # range. bh_nipple went 0.26..63.78 unsigned -> -0.25..1.19 signed.
                    mesh.uv.append((_s16(vtxbuf, p) / 1024.0, _s16(vtxbuf, p + 2) / 1024.0))
            elif a_type == 0x05 and a_str == 4:
                # Texture coordinate set 1. Hurt slot-1 artwork uses this independent unwrap;
                # sampling it with UV0 stretches isolated bruise texels across the whole body.
                for v in range(vcount):
                    p = a_off + 4 * v
                    mesh.uv_damage.append((_s16(vtxbuf, p) / 1024.0,
                                           _s16(vtxbuf, p + 2) / 1024.0))
            elif a_type == 0x3D and a_str == 4:
                for v in range(vcount):
                    p = a_off + 4 * v
                    mesh.uv2.append((_s16(vtxbuf, p) / 1024.0,
                                     _s16(vtxbuf, p + 2) / 1024.0))
            elif a_type == 0xD4 and a_str == 4:
                for v in range(vcount):
                    p = a_off + 4 * v
                    mesh.bidx.append(tuple(vtxbuf[p:p + 4]))
            elif a_type == 0xB0 and a_str == 16:
                for v in range(vcount):
                    p = a_off + 16 * v
                    mesh.bwt.append((_f32(vtxbuf, p), _f32(vtxbuf, p + 4), _f32(vtxbuf, p + 8), _f32(vtxbuf, p + 12)))

        # index buffer: one triangle strip of u16
        strip = [_u16(idxbuf, istart + 2 * i) for i in range(icount)]
        mesh.strip = strip
        mesh.tris = strip_to_tris(strip)
        mesh.vcount = vcount
        meshes.append(mesh)
    return meshes


class FullMesh:
    """A mesh captured with ALL attributes as raw bytes (so aux attrs 3/4/5 survive a round-trip)."""
    __slots__ = ("record", "vcount", "idxfmt", "attrs", "strip")
    # record: 52-byte mesh record (patched on encode)
    # attrs:  list of (type, stride, flags_u16, raw_bytes)
    # strip:  list of index ints (one triangle strip)


def read_model_full(archive):
    """Capture the first model's meshes losslessly for re-encoding. Returns (meshes, chunk_ids)."""
    idx_ri = archive.find_chunks(type_id=0xB007)[0]
    vtx_ri = archive.find_chunks(type_id=0xB006)[0]
    mesh_ri = archive.find_chunks(type_id=0xB004)[0]
    attr_ri = archive.find_chunks(type_id=0xB005)[0]
    idxbuf = archive.get_chunk_bytes(idx_ri)
    vtxbuf = archive.get_chunk_bytes(vtx_ri)
    meshdata = archive.get_chunk_bytes(mesh_ri)
    attrdata = archive.get_chunk_bytes(attr_ri)
    nmesh = len(meshdata) // 52
    meshes = []
    pi = 0
    for m in range(nmesh):
        o = m * 52
        istart = _u32(meshdata, o)
        idxflags = _u32(meshdata, o + 4)
        icount = idxflags & 0xFFFFFF
        ifmt = idxflags >> 24
        vcount = _u16(meshdata, o + 8)
        nA = meshdata[o + 11]
        fm = FullMesh()
        fm.record = bytearray(meshdata[o:o + 52])
        fm.vcount = vcount
        fm.idxfmt = ifmt
        fm.attrs = []
        for k in range(nA):
            ao = (pi + k) * 8
            a_off = _u32(attrdata, ao); a_type = attrdata[ao + 4]; a_str = attrdata[ao + 5]
            a_flags = _u16(attrdata, ao + 6)
            raw = bytes(vtxbuf[a_off:a_off + a_str * vcount])
            fm.attrs.append((a_type, a_str, a_flags, raw))
        pi += nA
        if ifmt == 0:
            fm.strip = [_u16(idxbuf, istart + 2 * i) for i in range(icount)]
        else:
            fm.strip = [idxbuf[istart + i] for i in range(icount)]
        meshes.append(fm)
    return meshes, (idx_ri, vtx_ri, mesh_ri, attr_ri)


def encode_model(archive, meshes, chunk_ids, align=0x20):
    """Rebuild IndexData/VertexData/MeshData/attr-pointer chunks from `meshes` and inject them.
    Supports arbitrary per-mesh vertex/index counts (resize-safe via the repacker)."""
    idx_ri, vtx_ri, mesh_ri, attr_ri = chunk_ids
    idxbuf = bytearray(); vtxbuf = bytearray(); attrbuf = bytearray(); meshbuf = bytearray()

    def pad(buf):
        while len(buf) % align:
            buf.append(0)

    for fm in meshes:
        pad(idxbuf)
        istart = len(idxbuf)
        if fm.idxfmt == 0:
            for v in fm.strip:
                idxbuf += struct.pack(">H", v)
        else:
            idxbuf += bytes(fm.strip)

        attr_ptrs = []
        for (a_type, a_str, a_flags, raw) in fm.attrs:
            pad(vtxbuf)
            ao = len(vtxbuf)
            vtxbuf += raw
            attr_ptrs.append((ao, a_type, a_str, a_flags))

        rec = bytearray(fm.record)
        struct.pack_into(">I", rec, 0, istart)
        struct.pack_into(">I", rec, 4, (fm.idxfmt << 24) | len(fm.strip))
        struct.pack_into(">H", rec, 8, fm.vcount)
        rec[11] = len(fm.attrs)
        meshbuf += rec
        for (ao, a_type, a_str, a_flags) in attr_ptrs:
            attrbuf += struct.pack(">I", ao) + bytes((a_type, a_str)) + struct.pack(">H", a_flags)

    # inject (index/vertex first, then mesh table + pointers)
    archive.replace_chunk(idx_ri, bytes(idxbuf))
    archive.replace_chunk(vtx_ri, bytes(vtxbuf))
    archive.replace_chunk(mesh_ri, bytes(meshbuf))
    archive.replace_chunk(attr_ri, bytes(attrbuf))


def transform_positions_inplace(archive, fn, models="first"):
    """Rewrite mesh vertex POSITIONS (attr 0x0A) in place using fn(x,y,z)->(x,y,z) in NATIVE PO
    coords. Same vertex count => same byte size => no resize needed. Commits to the archive.
    Returns number of vertices moved. Also renormalizes nothing (normals left as-is)."""
    vtx_ri = archive.find_chunks(type_id=0xB006)[0]
    mesh_ri = archive.find_chunks(type_id=0xB004)[0]
    attr_ri = archive.find_chunks(type_id=0xB005)[0]
    vtxbuf = bytearray(archive.get_chunk_bytes(vtx_ri))
    meshdata = archive.get_chunk_bytes(mesh_ri)
    attrdata = archive.get_chunk_bytes(attr_ri)

    nmesh = len(meshdata) // 52
    pi = 0
    moved = 0
    for m in range(nmesh):
        o = m * 52
        vcount = _u16(meshdata, o + 8)
        nA = meshdata[o + 11]
        for k in range(nA):
            ao = (pi + k) * 8
            a_off = _u32(attrdata, ao); a_type = attrdata[ao + 4]; a_str = attrdata[ao + 5]
            if a_type == 0x0A and a_str == 12:
                for v in range(vcount):
                    p = a_off + 12 * v
                    x = _f32(vtxbuf, p); y = _f32(vtxbuf, p + 4); z = _f32(vtxbuf, p + 8)
                    nx, ny, nz = fn(x, y, z)
                    struct.pack_into(">fff", vtxbuf, p, nx, ny, nz)
                    moved += 1
        pi += nA
    archive.replace_chunk(vtx_ri, bytes(vtxbuf))
    return moved


def strip_to_tris(strip):
    """Convert one triangle strip to a triangle list (skips degenerate tris)."""
    tris = []
    for i in range(len(strip) - 2):
        a, b, c = strip[i], strip[i + 1], strip[i + 2]
        if a == b or b == c or a == c:
            continue
        if i % 2 == 0:
            tris.append((a, b, c))
        else:
            tris.append((a, c, b))
    return tris


def export_obj(meshes, path, only=None):
    """Write meshes to an OBJ. `only` = optional list of mesh-name substrings to include."""
    with open(path, "w") as f:
        base = 1
        for mi, m in enumerate(meshes):
            if only and not any(s in m.name for s in only):
                continue
            f.write(f"o {mi:02d}_{m.name.replace('/','_')}\n")
            for p in m.pos:
                x, y, z = po_to_obj(p)
                f.write(f"v {x:.6f} {y:.6f} {z:.6f}\n")
            for (u, v) in m.uv:
                f.write(f"vt {u:.6f} {1.0-v:.6f}\n")
            for n in m.nrm:
                nx, ny, nz = po_to_obj(n)
                f.write(f"vn {nx:.6f} {ny:.6f} {nz:.6f}\n")
            has_uv = len(m.uv) == len(m.pos)
            has_n = len(m.nrm) == len(m.pos)
            for (a, b, c) in m.tris:
                def vert(i):
                    ii = base + i
                    if has_uv and has_n:
                        return f"{ii}/{ii}/{ii}"
                    if has_n:
                        return f"{ii}//{ii}"
                    return f"{ii}"
                f.write(f"f {vert(a)} {vert(b)} {vert(c)}\n")
            base += len(m.pos)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from nlg_pack import Archive
    from nlg_hash import load_hashid_bin
    here = os.path.dirname(os.path.abspath(__file__))
    hn = load_hashid_bin(os.path.join(here, "..", "..", "art", "hashid.bin"))
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "..", "art", "characters", "glassjoe.dict")
    out = sys.argv[2] if len(sys.argv) > 2 else "glassjoe.obj"
    ms = read_model(Archive(target), hn)
    export_obj(ms, out)
    nv = sum(len(m.pos) for m in ms)
    nt = sum(len(m.tris) for m in ms)
    print(f"exported {out}: {len(ms)} meshes, {nv} verts, {nt} tris")
