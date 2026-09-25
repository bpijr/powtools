"""
validate_mod.py -- structurally validate an exported character archive BEFORE you boot it.

Booting to find out is a 2-minute loop that tells you one bit of information: froze, or didn't.
This tells you WHICH invariant broke, in about a second, offline. Every check here corresponds
to something the game dereferences on load; a failure is a crash/hang candidate, not a cosmetic
issue.

    python validate_mod.py <modded.dict> [source.dict]

With a source it also diffs the two and reports what changed, which is usually the fastest way
to see that you rewrote something you never meant to touch.
Exit code 0 = clean, 1 = FAIL found.
"""
import sys, os, math, struct

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "formats"))
from nlg_pack import Archive
import nlg_hash, nlg_texture, nlg_geom

MAT_SIZE = 204
MESH_REC = 52
BONE_REC = 68


class Report:
    def __init__(self):
        self.fails, self.warns, self.notes = [], [], []

    def fail(self, msg): self.fails.append(msg); print("  FAIL  " + msg)
    def warn(self, msg): self.warns.append(msg); print("  warn  " + msg)
    def ok(self, msg):   self.notes.append(msg); print("  ok    " + msg)


def _hn(dict_path):
    for rel in ("../hashid.bin", "../../art/hashid.bin", "../art/hashid.bin"):
        p = os.path.abspath(os.path.join(os.path.dirname(dict_path), rel))
        if os.path.exists(p):
            return nlg_hash.load_hashid_bin(p)
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "art", "hashid.bin")
    return nlg_hash.load_hashid_bin(p) if os.path.exists(p) else {}


def check_materials(a, r):
    """Every mesh's materialOffset must land exactly on a record boundary, inside the chunk.

    NOTE: characters can hold MORE THAN ONE model. The referee is 34 meshes + a second
    7-mesh model in a separate 0xB004/0xB016 pair. The importer and exporter only ever touch
    model 0, so model 1 keeps its original geometry -- but it shares the skeleton, so a bind
    matrix rewrite reaches it anyway. Check every model, not just the first.
    """
    meshcs = a.find_chunks(type_id=0xB004)
    matcs = a.find_chunks(type_id=0xB016)
    if len(meshcs) > 1:
        r.warn("archive holds %d models (%s meshes). Only model 0 is imported/exported; the "
               "others keep their original geometry while sharing the skeleton."
               % (len(meshcs),
                  [len(a.get_chunk_bytes(c)) // MESH_REC for c in meshcs]))
    total_n = 0
    for mi_model, (mc, mtc) in enumerate(zip(meshcs, matcs)):
        total_n += _check_one_model(a, r, mc, mtc, mi_model)
    return len(a.get_chunk_bytes(meshcs[0])) // MESH_REC


def _check_one_model(a, r, meshc, matc, model_i):
    tag = "model %d: " % model_i
    md = a.get_chunk_bytes(meshc)
    mt = a.get_chunk_bytes(matc)
    n = len(md) // MESH_REC
    if len(mt) % MAT_SIZE:
        r.fail(tag + "0xB016 is %d bytes, not a whole number of %d-byte records" % (len(mt), MAT_SIZE))
    nrec = len(mt) // MAT_SIZE
    bad_align, oob = [], []
    for i in range(n):
        o = struct.unpack_from(">I", md, i * MESH_REC + 36)[0]
        if o % MAT_SIZE:
            bad_align.append(i)
        if o + MAT_SIZE > len(mt):
            oob.append(i)
    if bad_align:
        r.fail(tag + "meshes %s have a materialOffset that is not on a record boundary -> the game "
               "reads a record straddling two others (hang)" % bad_align)
    if oob:
        r.fail(tag + "meshes %s point past the end of 0xB016 (dangling materialOffset)" % oob)
    if not bad_align and not oob:
        r.ok(tag + "%d meshes -> %d material records, all offsets aligned and in range" % (n, nrec))
    used = {struct.unpack_from(">I", md, i * MESH_REC + 36)[0] // MAT_SIZE for i in range(n)}
    if len(used) < nrec:
        r.warn(tag + "%d of %d material records are orphaned (nothing points at them) -- usually means "
               "meshes got mapped to the wrong records" % (nrec - len(used), nrec))
    return n


def check_textures(a, r):
    """Textures store a MIP CHAIN, and the record's mip count at +0x0B says how many levels
    the game will read contiguously. Measuring only the base level is what let a truncated
    add-texture sail through this check and hang the game."""
    ents = nlg_texture.list_textures(a)
    tdc = a.find_chunks(type_id=0xB603)
    td = a.get_chunk_bytes(tdc[0]) if tdc else b""
    hdr = a.get_chunk_bytes(a.find_chunks(type_id=0xB601)[0])

    def chain(w, h, levels):
        return sum(max(1, (max(1, w >> k) + 7) // 8) * max(1, (max(1, h >> k) + 7) // 8) * 32
                   for k in range(max(1, levels)))

    over, wrongcount = [], []
    for i, e in enumerate(ents):
        levels = hdr[i * 96 + 11]
        need = chain(e.width, e.height, levels)
        if e.data_offset + need > len(td):
            over.append((e.name or "%08x" % e.hash, "%dx%d" % (e.width, e.height),
                         "mips=%d" % levels, "needs %d, only %d left"
                         % (need, len(td) - e.data_offset)))
        expect = nlg_texture.mip_levels(e.width, e.height)
        if levels != expect:
            # Shipped archives are authoritative. Bear Hugger, for example, deliberately
            # stores two levels for a 256x8 strip even though the common size heuristic stops
            # at 8px. The on-disk count is what the game reads; only an overrun is unsafe.
            wrongcount.append((e.name or "%08x" % e.hash, "%dx%d" % (e.width, e.height),
                               "record=%d common=%d" % (levels, expect)))
    if over:
        r.fail("texture mip chain runs past the end of 0xB603 (this hangs the game on load): %s"
               % over[:4])
    if wrongcount:
        r.warn("%d texture mip count(s) differ from the common size heuristic but their full "
               "declared chains fit (valid shipped layout): %s" % (len(wrongcount), wrongcount[:4]))
    if not over and not wrongcount:
        r.ok("%d textures, every declared mip chain fits inside the %d-byte pixel chunk"
             % (len(ents), len(td)))
    return {e.hash for e in ents}


def check_geometry(a, hn, r):
    try:
        ms = nlg_geom.read_model(a, hn)
    except Exception as ex:
        r.fail("geometry will not even parse: %s" % ex)
        return []
    empties, bad = [], 0
    for i, m in enumerate(ms):
        nv = len(m.pos)
        if nv == 0:
            empties.append(i)
            continue
        hi = max((max(t) for t in m.tris), default=-1)
        if hi >= nv:
            r.fail("mesh %d: triangle index %d >= %d vertices (GPU reads out of bounds)"
                   % (i, hi, nv)); bad += 1
        if len(m.nrm) != nv:
            r.fail("mesh %d: %d positions but %d normals" % (i, nv, len(m.nrm))); bad += 1
        if m.uv and len(m.uv) != nv:
            r.fail("mesh %d: %d positions but %d UVs" % (i, nv, len(m.uv))); bad += 1
    if not bad:
        r.ok("%d meshes, all triangle indices inside their vertex arrays" % len(ms))
    if empties:
        r.warn("meshes %s have 0 vertices (zeroed slots -- fine if intentional)" % empties)
    return ms


def check_bones(a, hn, r):
    """The one that bites after a skeleton edit: a non-invertible bind matrix.

    Skinning is v' = W_anim * W_bind^-1 * v. A singular or NaN W_bind makes that inverse
    explode, which is a hang or a screenful of NaN geometry, not a subtle error.
    """
    bc = a.find_chunks(type_id=0xB00A)
    if not bc:
        r.warn("no BoneData (0xB00A) chunk"); return
    bd = a.get_chunk_bytes(bc[0])
    n = len(bd) // BONE_REC
    nonfinite, singular, scaled = [], [], []
    for i in range(n):
        o = i * BONE_REC
        h = struct.unpack_from(">I", bd, o)[0]
        m = struct.unpack_from(">16f", bd, o + 4)
        nm = hn.get(h, "#%08x" % h)
        if any(not math.isfinite(v) for v in m):
            nonfinite.append(nm); continue
        # rotation is stored column-major; rows of the 3x3 as the game reads them
        R = [[m[0], m[4], m[8]], [m[1], m[5], m[9]], [m[2], m[6], m[10]]]
        det = (R[0][0] * (R[1][1] * R[2][2] - R[1][2] * R[2][1])
               - R[0][1] * (R[1][0] * R[2][2] - R[1][2] * R[2][0])
               + R[0][2] * (R[1][0] * R[2][1] - R[1][1] * R[2][0]))
        if abs(det) < 1e-6:
            singular.append((nm, det))
        elif abs(abs(det) - 1.0) > 0.05:
            scaled.append((nm, round(det, 4)))
    if nonfinite:
        r.fail("bind matrices contain NaN/Inf: %s" % nonfinite[:6])
    if singular:
        r.fail("bind matrices are singular (cannot be inverted -> skinning blows up): %s"
               % singular[:6])
    if scaled:
        r.warn("%d bind matrices carry non-unit scale (det != 1): %s -- legal, but it means "
               "the skin renders bigger/smaller around those bones" % (len(scaled), scaled[:5]))
    if not (nonfinite or singular):
        r.ok("%d bind matrices, all finite and invertible" % n)
    return n


def check_palettes(a, r, nmesh):
    bh = a.find_chunks(type_id=0xB00B)
    if len(bh) < nmesh:
        r.fail("only %d bone palettes for %d meshes" % (len(bh), nmesh))
        return
    empty = [i for i in range(nmesh) if len(a.get_chunk_bytes(bh[i])) == 0]
    if empty:
        r.warn("meshes %s have an empty bone palette" % empty)
    big = [(i, len(a.get_chunk_bytes(bh[i])) // 4) for i in range(nmesh)
           if len(a.get_chunk_bytes(bh[i])) // 4 > 255]
    if big:
        r.fail("palettes over 255 bones (index is 1 byte): %s" % big)
    else:
        r.ok("%d bone palettes, all within the 255-bone index limit" % nmesh)


def check_morphs(a, dict_path, ms, r):
    try:
        import nlg_morph
        hb = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "art", "hashid.bin")
        m = nlg_morph.Morphs(dict_path, hb)
    except Exception as ex:
        r.warn("morph check skipped (%s)" % ex); return
    worst = {}
    for ch in range(getattr(m, "B", 0)):
        for mi, s, recs in (m.channel_deltas(ch) or []):
            if recs:
                worst[mi] = max(worst.get(mi, -1), max(x[0] for x in recs))
    bad = [(mi, hi, len(ms[mi].pos)) for mi, hi in worst.items()
           if mi < len(ms) and hi >= len(ms[mi].pos)]
    if bad:
        r.fail("morph targets point past the end of their mesh (mesh, idx, verts): %s" % bad)
    elif worst:
        r.ok("%d morph-animated meshes, all target indices in range" % len(worst))


def _model_bbox(a, mi):
    mcs = a.find_chunks(type_id=0xB004)
    vcs = a.find_chunks(type_id=0xB006)
    acs = a.find_chunks(type_id=0xB005)
    if mi >= len(mcs) or mi >= len(vcs) or mi >= len(acs):
        return None
    md = a.get_chunk_bytes(mcs[mi]); vtx = a.get_chunk_bytes(vcs[mi])
    attrd = a.get_chunk_bytes(acs[mi])
    lo = [1e30] * 3; hi = [-1e30] * 3; pi = 0; seen = False
    for m in range(len(md) // 52):
        vc = struct.unpack_from(">H", md, m * 52 + 8)[0]
        nA = md[m * 52 + 11]
        for k in range(nA):
            ao = (pi + k) * 8
            if attrd[ao + 4] == 0x0A and attrd[ao + 5] == 12 and vc:
                off = struct.unpack_from(">I", attrd, ao)[0]
                for v in range(vc):
                    p = struct.unpack_from(">3f", vtx, off + v * 12)
                    for c in range(3):
                        lo[c] = min(lo[c], p[c]); hi[c] = max(hi[c], p[c])
                    seen = True
        pi += nA
    return (lo, hi) if seen else None


def check_extra_models(a, r):
    """The shadow volume has to track the body it belongs to.

    Only model 0 is imported and rebuilt. The other models -- the referee's is a 7-mesh
    `shadow_volume/shadow` hull -- are skinned to the same skeleton but keep their original
    vertices, so reshaping the character leaves a full-size black silhouette poking out of it.
    """
    b0 = _model_bbox(a, 0)
    if b0 is None or len(a.find_chunks(type_id=0xB004)) < 2:
        return
    for mi in range(1, len(a.find_chunks(type_id=0xB004))):
        bn = _model_bbox(a, mi)
        if bn is None:
            continue
        # how far the extra model sticks out, relative to the visible model's own height
        span = max(1e-6, b0[1][2] - b0[0][2])
        over = max(bn[1][c] - b0[1][c] for c in range(3))
        under = max(b0[0][c] - bn[0][c] for c in range(3))
        worst = max(over, under)
        if worst > 0.15 * span:
            r.fail("model %d (shadow volume) sticks out %.3f past model 0, %.0f%% of its height "
                   "-- it did not follow the skeleton and will render as black wedges through "
                   "the character. Keep 'Write bind matrices' ticked and re-export."
                   % (mi, worst, 100.0 * worst / span))
        else:
            r.ok("model %d tracks model 0 (max overhang %.3f, %.0f%% of height)"
                 % (mi, worst, 100.0 * worst / span))


def check_aux_attrs(mod, src, r):
    """Catch a rebuilt mesh that dropped the vertex attributes Blender cannot author.

    Every boxer mesh declares eight: position, normal, UV0, 0x05 (unidentified), 0x3D (a second
    UV set), 0xE9 (vertex colour RGBA8), bone indices, weights. The exporter can only build five
    of them from a Blender mesh and used to fill the rest with zeros -- silently, on every mesh
    of every character. Zeroed vertex colour is black at alpha 0; a zeroed UV set pins that
    texture stage to texel (0,0).
    """
    try:
        A, _ = nlg_geom.read_model_full(src)
        B, _ = nlg_geom.read_model_full(mod)
    except Exception as ex:
        r.warn("could not compare vertex attributes: %s" % ex)
        return
    KNOWN = {0x0A, 0xFE, 0xCC, 0xD4, 0xB0}
    dead = []
    for i, (s, m) in enumerate(zip(A, B)):
        if not m.vcount:
            continue
        so = {t: raw for (t, _st, _f, raw) in s.attrs}
        for (t, _st, _f, raw) in m.attrs:
            if t in KNOWN or t not in so:
                continue
            if any(so[t]) and not any(raw):
                dead.append((i, "0x%02X" % t))
    if dead:
        r.fail("vertex attributes zeroed that the source filled: %s -- vertex colour goes black "
               "and transparent, a second UV set collapses onto texel (0,0)" % dead[:12])
    else:
        r.ok("aux vertex attributes (0x05, UV set 2, vertex colour) survived the rebuild")


def diff_against_source(mod, src, r):
    print("\n-- diff vs source --")
    for t, name in ((0xB00A, "BoneData"), (0xB00B, "BonePalettes"), (0xB004, "MeshRecords"),
                    (0xB016, "MaterialRecords"), (0xB601, "TextureTable"), (0xB603, "TextureData")):
        ca, cb = src.find_chunks(type_id=t), mod.find_chunks(type_id=t)
        da = b"".join(src.get_chunk_bytes(c) for c in ca)
        db = b"".join(mod.get_chunk_bytes(c) for c in cb)
        state = "identical" if da == db else "CHANGED"
        print("  0x%04X %-16s %8d -> %-8d %s" % (t, name, len(da), len(db), state))
    mdA = src.get_chunk_bytes(src.find_chunks(type_id=0xB004)[0])
    mdB = mod.get_chunk_bytes(mod.find_chunks(type_id=0xB004)[0])
    if len(mdA) != len(mdB):
        r.fail("mesh count changed (%d -> %d); slots are fixed by the archive"
               % (len(mdA) // MESH_REC, len(mdB) // MESH_REC))
        return
    ch = [i for i in range(len(mdA) // MESH_REC)
          if struct.unpack_from(">H", mdA, i * MESH_REC + 8)[0]
          != struct.unpack_from(">H", mdB, i * MESH_REC + 8)[0]]
    if ch:
        print("  vertex counts changed on slots: %s" % ch)


def main(argv):
    if not argv:
        print(__doc__); return 2
    mod_path = argv[0]
    src_path = argv[1] if len(argv) > 1 else None
    hn = _hn(mod_path)
    a = Archive(mod_path)
    r = Report()
    print("== %s ==" % os.path.basename(mod_path))
    nmesh = check_materials(a, r)
    check_textures(a, r)
    ms = check_geometry(a, hn, r)
    check_bones(a, hn, r)
    check_palettes(a, r, nmesh)
    check_extra_models(a, r)
    check_morphs(a, mod_path, ms, r)
    if src_path and os.path.exists(src_path):
        srca = Archive(src_path)
        check_aux_attrs(a, srca, r)
        diff_against_source(a, srca, r)
    print("\n%d FAIL, %d warn" % (len(r.fails), len(r.warns)))
    if r.fails:
        print("Do not boot this -- fix the FAILs first.")
        return 1
    print("Checked structural invariants passed. Unchecked layout and runtime behavior remain unverified.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
