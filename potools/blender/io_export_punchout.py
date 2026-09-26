bl_info = {
    "name": "Punch-Out!! Wii Mod Exporter",
    "author": "Bryan Intindola",
    "version": (1, 0),
    "blender": (3, 0, 0),
    "location": "File > Export > Punch-Out!! Mod (.dict)",
    "description": "One-click export of a rigged Blender character into any Punch-Out!! Wii "
                   "character slot: geometry, weights, and color-baked ramp textures, straight "
                   "to a bootable .dict/.data. No JSON, no command line.",
    "category": "Import-Export",
}

import bpy
import os
import sys
import math
import struct
import importlib
from bpy.props import StringProperty, BoolProperty, FloatProperty, EnumProperty, IntProperty
from bpy_extras.io_utils import ExportHelper


# ===========================================================================
# ===========================================================================
# self-contained CMPR (GameCube DXT1) decode + encode  (Blender has no Pillow)
# ===========================================================================
# The CMPR codec lives in nlg_texture and NOWHERE ELSE.
#
# This file used to carry its own private copy, because nlg_texture imported Pillow at module
# scope and Blender ships without it. That is no longer true -- PIL is imported lazily inside
# the *_png helpers only -- so the copy bought nothing and cost correctness: it silently drifted
# and kept the ORIGINAL uniform-block bug (equal endpoints => 3-colour PUNCH-THROUGH mode on GX)
# long after nlg_texture was fixed. That is the banding/stripes on flat surfaces, and it was
# invisible here because our own decoder reads index 0 correctly either way.
#
# Keep these as thin aliases so every call site below is unchanged, but there is now exactly one
# implementation to fix.
def _decode_cmpr(data, w, h):
    import nlg_texture
    return nlg_texture.decode_cmpr(data, w, h)


def _encode_cmpr(rgba, w, h):
    import nlg_texture
    return nlg_texture.encode_cmpr(rgba, w, h)


def _srgb(c):
    c = max(0.0, min(1.0, c))
    return 1.055 * (c ** (1/2.4)) - 0.055 if c > 0.0031308 else 12.92 * c


# ===========================================================================
# tool discovery (pure-python nlg modules live in potools/formats)
# ===========================================================================
def _load_tools():
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "formats")
    if here not in sys.path:
        sys.path.append(here)
    import nlg_pack, nlg_geom, nlg_hash, nlg_anim2
    for m in (nlg_pack, nlg_geom, nlg_hash, nlg_anim2):
        importlib.reload(m)
    return nlg_pack, nlg_geom, nlg_hash, nlg_anim2


def build_bone_resolver(nlg_pack, nlg_hash, nlg_anim2, source_dict, hashid):
    """Return (resolve, fallback_hash, bone_set).

    resolve(vertex_group_name) -> a hash of a REAL skinning bone (BoneData 0xB00A), or None.
    - exact skinning bones pass through
    - helper / animation-only nodes (twists, footsteps, neck, root...) collapse to their
      nearest skinning ANCESTOR via the node parent table
    - anything not in the character's node graph at all (foreign groups from another rig)
      returns None so the caller can drop + renormalize it
    """
    a = nlg_pack.Archive(source_dict)
    bd = a.get_chunk_bytes(a.find_chunks(type_id=0xB00A)[0])
    bone_set = set(struct.unpack_from(">I", bd, i * 68)[0] for i in range(len(bd) // 68))
    rig = nlg_anim2.Rig(source_dict, hashid)
    h2node = {h: i for i, h in enumerate(rig.hashes)}

    # skin-bone world rest positions (same native space as the imported mesh) for nearest-bone
    # fallback of vertices that end up with no valid weight
    bone_pos = {}
    for i in range(len(bd) // 68):
        h = struct.unpack_from(">I", bd, i * 68)[0]
        try:
            bone_pos[h] = tuple(rig.bone_wp[i])
        except Exception:
            pass

    cache = {}

    def resolve(name):
        if name in cache:
            return cache[name]
        h = nlg_hash.string_to_hash(name)
        out = None
        if h in bone_set:
            out = h
        else:
            n = h2node.get(h)
            guard = 0
            while n is not None and n >= 0 and guard < 100:
                hh = rig.hashes[n]
                if hh in bone_set:
                    out = hh
                    break
                n = rig.par[n]
                guard += 1
        cache[name] = out
        return out

    pelvis = nlg_hash.string_to_hash("bip01 pelvis")
    fallback = pelvis if pelvis in bone_set else next(iter(bone_set))

    def nearest_bone(co):
        best, bd2 = fallback, None
        for h, p in bone_pos.items():
            d = (co[0]-p[0])**2 + (co[1]-p[1])**2 + (co[2]-p[2])**2
            if bd2 is None or d < bd2:
                bd2 = d; best = h
        return best

    return resolve, fallback, bone_set, nearest_bone


def _find_hashid(source_dict):
    """Nearest hashid.bin in the archive's folder or any parent; no folder name is assumed."""
    import nlg_hash
    return nlg_hash.find_hashid_bin(source_dict)[0]


# ===========================================================================
# scene reading
# ===========================================================================
def find_armature():
    arms = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    if not arms:
        raise RuntimeError("No armature in the scene. The mesh must be parented to the "
                           "imported Punch-Out armature.")
    if len(arms) > 1:
        print(f"[PO export] {len(arms)} armatures found, using '{arms[0].name}'")
    return arms[0]


def find_mesh_objects(armature):
    out = []
    for o in bpy.data.objects:
        if o.type != "MESH":
            continue
        if any(m.type == "ARMATURE" and m.object == armature for m in o.modifiers):
            out.append(o)
    if not out:
        raise RuntimeError("No mesh with an Armature modifier pointing at the armature.")
    return out


def _node_chunks(a):
    """The node table lives in the 0x8001..0x8011 range: 0x8003 hashes, 0x8009 parents,
    0x8010 local offsets. Same lookup nlg_anim2.Rig uses."""
    seq = [(a.chunks[i][2], i) for i in range(a.num_file_entries, len(a.chunks))]
    g = {}
    for t, i in seq:
        if 0x8001 <= t <= 0x8011:
            g.setdefault(t, []).append(i)
    return {t: v[0] for t, v in g.items()}


def _bone_by_node(armature, nn):
    """node index -> Blender bone, via the po_node stamped at import (name fallback)."""
    out = [None] * nn
    for b in armature.data.bones:
        n = b.get("po_node")
        if n is not None and 0 <= int(n) < nn:
            out[int(n)] = b
    return out


def write_node_offsets(a, armature, tol=1e-6):
    """Rebuild the node table's LOCAL offsets (0x8010) from the armature's rest pose.

    THIS is what sets the character's proportions at runtime, not BoneData. The game builds
    the animated hierarchy as

        wp[n] = wp[parent] + rotate(wq_bind[parent], loff[n])

    so if you shrink the rig in Blender and only rewrite the bind matrices, every bone snaps
    back to its original spacing and the skin is stretched to the original proportions --
    so a shrunken character comes back at its original size.

    loff is expressed in the PARENT's bind frame, so the inverse is
        loff[n] = parent_rest_rotation^-1 * (pos[n] - pos[parent])
    with the root's offset being its bare position.
    """
    g = _node_chunks(a)
    if 0x8010 not in g or 0x8009 not in g or 0x8003 not in g:
        return [], "node table chunks missing"
    off_ri = g[0x8010]
    off = bytearray(a.get_chunk_bytes(off_ri))
    par = list(struct.unpack(">%di" % (len(a.get_chunk_bytes(g[0x8003])) // 4),
                             a.get_chunk_bytes(g[0x8009])))
    nn = len(par)
    bones = _bone_by_node(armature, nn)
    if all(b is None for b in bones):
        return [], ("no bone carries 'po_node' -- re-import with the current addon, or the "
                    "node table cannot be rebuilt")

    from mathutils import Vector as _V, Quaternion as _Q

    def _local(pos, ppos, pquat):
        """position expressed in the parent's frame"""
        if ppos is None:
            return _V(pos)
        return pquat.inverted() @ (_V(pos) - _V(ppos))

    written, missing, no_rest = [], [], []
    for n in range(nn):
        b = bones[n]
        if b is None:
            missing.append(n)
            continue
        rest_h = b.get("po_rest_head")
        if rest_h is None:
            no_rest.append(n)
            continue
        p = par[n]
        pb = bones[p] if p >= 0 else None
        if p >= 0 and (pb is None or pb.get("po_rest_head") is None):
            missing.append(n)
            continue

        now = _local(b.matrix_local.translation,
                     pb.matrix_local.translation if pb else None,
                     pb.matrix_local.to_quaternion() if pb else None)
        imp = _local(list(rest_h),
                     list(pb["po_rest_head"]) if pb else None,
                     _Q(list(pb["po_rest_quat"])) if pb else None)

        cur = struct.unpack_from(">3f", off, n * 12)
        # apply only what CHANGED, on top of the archive's own offset
        new = tuple(cur[k] + (now[k] - imp[k]) for k in range(3))
        if max(abs(new[k] - cur[k]) for k in range(3)) <= tol:
            continue
        struct.pack_into(">3f", off, n * 12, *new)
        written.append(b.name)
    if no_rest:
        missing.extend(no_rest)
    if written:
        a.replace_chunk(off_ri, bytes(off))
    return written, ("%d node(s) had no Blender bone" % len(missing)) if missing else ""


def write_bind_matrices(a, armature, hn, tol=1e-5):
    """Push each bone's REST world *position* from Blender into BoneData (0xB00A).

    Skinning in-game is  v' = W_anim * W_bind^-1 * v.  The exporter writes your vertices at
    their new rest positions but used to leave W_bind at the ORIGINAL character's values, so
    every vertex got dragged toward where its bone used to be -- limbs firing off as thin
    spikes. It looks perfect in Blender because Blender skins with YOUR armature.

    Bone rotations are deliberately NOT copied from Blender. The game has no writable rest-
    rotation table for animated nodes: every animation supplies rotations in the source rig's
    coordinate frames. Copying an edit bone's roll or direction into W_bind therefore makes
    W_anim * W_bind^-1 contain a permanent twist. That is why an arm could look correct in
    Blender yet corkscrew in-game. Keep the archive's 3x3 basis and move only the joint origin;
    the unchanged game animation then rotates the custom mesh around its new joint correctly.

    Layout (see nlg_skeleton.py): 68 bytes per bone = u32 nameHash + 4x4 f32 big-endian.
    The rotation is column-major (nlg_anim2 transposes it on read), and the translation occupies
    floats 12..14.

    Only bones that actually moved are rewritten -- an untouched rig stays byte-identical,
    which keeps round-trips clean and makes diffs meaningful.
    """
    ri = a.find_chunks(type_id=0xB00A)[0]
    bd = bytearray(a.get_chunk_bytes(ri))
    bones = armature.data.bones
    written, missing = [], []
    for i in range(len(bd) // 68):
        o = i * 68
        h = struct.unpack_from(">I", bd, o)[0]
        nm = hn.get(h)
        b = bones.get(nm) if nm else None
        if b is None:
            missing.append(nm or "#%08x" % h)
            continue
        M = b.matrix_local                      # target joint position, armature space
        cur = list(struct.unpack_from(">16f", bd, o + 4))
        new = list(cur)                          # retain the source animation basis
        new[12], new[13], new[14] = M[0][3], M[1][3], M[2][3]
        new[15] = 1.0
        if max(abs(x - y) for x, y in zip(cur, new)) <= tol:
            continue                            # unchanged: leave the bytes alone
        struct.pack_into(">16f", bd, o + 4, *new)
        written.append(nm)
    if written:
        a.replace_chunk(ri, bytes(bd))
    return written, missing


def bind_matrices(a):
    """bone hash -> 4x4 row-major rest world matrix, read out of BoneData (0xB00A).

    Inverse of what write_bind_matrices() stores: the rotation is COLUMN-major on disk, so
    M[r][c] = stored[c*4+r], which also puts the translation at stored[12..14].
    """
    cs = a.find_chunks(type_id=0xB00A)
    if not cs:
        return {}
    bd = a.get_chunk_bytes(cs[0])
    out = {}
    for i in range(len(bd) // 68):
        o = i * 68
        h = struct.unpack_from(">I", bd, o)[0]
        f = struct.unpack_from(">16f", bd, o + 4)
        out[h] = [[f[c * 4 + r] for c in range(4)] for r in range(4)]
    return out


def _mat3_inv(M):
    a, b, c = M[0][0], M[0][1], M[0][2]
    d, e, f = M[1][0], M[1][1], M[1][2]
    g, h, i = M[2][0], M[2][1], M[2][2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        return None
    v = 1.0 / det
    return [[(e * i - f * h) * v, (c * h - b * i) * v, (b * f - c * e) * v],
            [(f * g - d * i) * v, (a * i - c * g) * v, (c * d - a * f) * v],
            [(d * h - e * g) * v, (b * g - a * h) * v, (a * e - b * d) * v]]


def _bind_delta(Mnew, Mold):
    """Mnew * Mold^-1 as a 3x4 (rotation+scale 3x3, translation 3), or None if degenerate."""
    R = _mat3_inv(Mold)
    if R is None:
        return None
    t = [-sum(R[r][k] * Mold[k][3] for k in range(3)) for r in range(3)]   # -Mold^-1 * t_old
    A = [[sum(Mnew[r][k] * R[k][c] for k in range(3)) for c in range(3)] for r in range(3)]
    b = [sum(Mnew[r][k] * t[k] for k in range(3)) + Mnew[r][3] for r in range(3)]
    return A, b


def rebind_extra_models(a, nlg_geom, old_bind, new_bind, tol=1e-9):
    """Re-pose every model the exporter does NOT rebuild into the new rest pose.

    The referee's second model is his SHADOW VOLUME: 7 meshes, its own bone palettes, skinned to
    the same skeleton. do_export only rebuilds model 0, so the hull keeps the ORIGINAL vertices
    while write_bind_matrices moves W_bind out from under it. Skinning is
    v' = W_anim * W_bind^-1 * v, so once the body is reshaped the hull no longer matches it --
    shrink the character and a full-size black silhouette stays behind, poking out through the
    cap and the shoulders. It never shows in Blender because model 1 is never imported.

    Each vertex goes through its own bones' change of bind pose,
        v_new = sum_i w_i * (M_new_i * M_old_i^-1) * v_old
    which is the relation the rebuilt model already satisfies by construction.
    """
    mcs = a.find_chunks(type_id=0xB004)
    vcs = a.find_chunks(type_id=0xB006)
    acs = a.find_chunks(type_id=0xB005)
    pcs = a.find_chunks(type_id=0xB00B)
    if len(mcs) < 2 or len(vcs) < len(mcs) or len(acs) < len(mcs):
        return 0, 0

    deltas = {}
    for h, Mo in old_bind.items():
        Mn = new_bind.get(h)
        if Mn is None:
            continue
        if max(abs(Mn[r][c] - Mo[r][c]) for r in range(3) for c in range(4)) <= tol:
            continue                                    # bone did not move
        d = _bind_delta(Mn, Mo)
        if d is not None:
            deltas[h] = d
    if not deltas:
        return 0, 0

    pal_base = len(a.get_chunk_bytes(mcs[0])) // 52     # model 0 owns the first N palettes
    moved_v, moved_m = 0, 0
    for mi in range(1, len(mcs)):
        meshdata = a.get_chunk_bytes(mcs[mi])
        attrd = a.get_chunk_bytes(acs[mi])
        vtx = bytearray(a.get_chunk_bytes(vcs[mi]))
        pi = 0
        for m in range(len(meshdata) // 52):
            o = m * 52
            vc = struct.unpack_from(">H", meshdata, o + 8)[0]
            nA = meshdata[o + 11]
            attrs = {}
            for k in range(nA):
                ao = (pi + k) * 8
                attrs[attrd[ao + 4]] = (struct.unpack_from(">I", attrd, ao)[0], attrd[ao + 5])
            pi += nA
            pidx = pal_base + m
            if not vc or pidx >= len(pcs) or 0x0A not in attrs or 0xD4 not in attrs \
                    or 0xB0 not in attrs:
                continue
            pal = a.get_chunk_bytes(pcs[pidx])
            bones = [struct.unpack_from(">I", pal, k * 4)[0] for k in range(len(pal) // 4)]
            poff = attrs[0x0A][0]; noff = attrs.get(0xFE, (None, 0))[0]
            ioff = attrs[0xD4][0]; woff = attrs[0xB0][0]
            touched = False
            for v in range(vc):
                idx = vtx[ioff + v * 4: ioff + v * 4 + 4]
                wts = struct.unpack_from(">4f", vtx, woff + v * 16)
                A = [[0.0] * 3 for _ in range(3)]; b = [0.0, 0.0, 0.0]
                tot = 0.0
                for k in range(4):
                    w = wts[k]
                    if w <= 0.0 or idx[k] >= len(bones):
                        continue
                    d = deltas.get(bones[idx[k]])
                    tot += w
                    if d is None:                       # bone unchanged: identity
                        for r in range(3):
                            A[r][r] += w
                        continue
                    dA, db = d
                    for r in range(3):
                        for c in range(3):
                            A[r][c] += w * dA[r][c]
                        b[r] += w * db[r]
                if tot <= 1e-6:
                    continue
                s = 1.0 / tot
                A = [[x * s for x in row] for row in A]; b = [x * s for x in b]
                p = struct.unpack_from(">3f", vtx, poff + v * 12)
                struct.pack_into(">3f", vtx, poff + v * 12,
                                 *[sum(A[r][c] * p[c] for c in range(3)) + b[r] for r in range(3)])
                if noff is not None:
                    n = struct.unpack_from(">3f", vtx, noff + v * 12)
                    q = [sum(A[r][c] * n[c] for c in range(3)) for r in range(3)]
                    L = math.sqrt(sum(x * x for x in q))
                    if L > 1e-9:
                        struct.pack_into(">3f", vtx, noff + v * 12, *[x / L for x in q])
                touched = True
                moved_v += 1
            if touched:
                moved_m += 1
        a.replace_chunk(vcs[mi], bytes(vtx))
    return moved_m, moved_v


def anim_root_node(a):
    """The node the animations' world-space translation track drives.

    Node 0 is 'root' and sits at the origin; the character's actual hips hang off it. So the
    animation root is the first node whose parent IS node 0 -- 'bip01' on every rig checked,
    carrying the hip height as its offset (referee: 1.0080).
    """
    g = _node_chunks(a)
    if 0x8009 not in g or 0x8003 not in g:
        return None
    nn = len(a.get_chunk_bytes(g[0x8003])) // 4
    par = struct.unpack(">%di" % nn, a.get_chunk_bytes(g[0x8009]))
    for n in range(nn):
        if par[n] == 0 and n != 0:
            return n
    return 1 if nn > 1 else None


def node_offset(a, n):
    g = _node_chunks(a)
    if 0x8010 not in g or n is None:
        return None
    return struct.unpack_from(">3f", a.get_chunk_bytes(g[0x8010]), n * 12)


def _anim_runs(a):
    """[(name, [chunk ids of its 0x7102 translation tracks, in file order])] for every animation."""
    idxs = sorted(range(a.num_file_entries, len(a.chunks)),
                  key=lambda i: (a._chunk_block(i), a.chunks[i][4]))
    runs, cur = [], None
    for i in idxs:
        t = a.chunks[i][2]
        if t == 0x7001:
            cur = {"name": None, "tr": []}
            runs.append(cur)
        if cur is None:
            continue
        if t == 0x7102:
            cur["tr"].append(i)
        elif t == 0x7002 and cur["name"] is None:
            cur["name"] = a.get_chunk_bytes(i).split(b"\0")[0].decode("ascii", "replace")
    return [(r["name"] or "?", r["tr"]) for r in runs]


def retarget_root_motion(a, delta, tol=1e-6):
    """Shift every animation's world root translation by `delta`.

    THIS IS WHAT KEEPS A RESHAPED CHARACTER ON THE CANVAS.

    The first 0x7102 track of each animation is the root's translation in WORLD space -- it is
    the only multi-frame translation track (the other 30 are single-frame local bone offsets),
    and its frame-0 Z matches the hip node's height in the archive almost exactly (referee:
    track 0.9824 vs node 1.0080, the difference being the animation's own slight crouch).

    So the height the game puts the hips at is baked into the ANIMATION, not derived from the
    skeleton. Shorten the legs and rewrite BoneData and the node table, and the hips are still
    commanded to the ORIGINAL height every frame while the legs no longer reach that far down --
    the character hangs in the air by exactly the amount the hips were lowered. Adding the same
    delta to the track restores the original hip-to-ground relationship, so the feet plant again.

    Returns (tracks_rewritten, animation_count).
    """
    if delta is None or max(abs(v) for v in delta) <= tol:
        return 0, 0
    runs = _anim_runs(a)
    n = 0
    for _name, tracks in runs:
        if not tracks:
            continue
        ri = tracks[0]                      # world root track; the rest are local offsets
        d = bytearray(a.get_chunk_bytes(ri))
        if len(d) % 12:
            continue                        # not XYZ f32 frames; leave it alone
        for f in range(len(d) // 12):
            x, y, z = struct.unpack_from(">3f", d, f * 12)
            struct.pack_into(">3f", d, f * 12,
                             x + delta[0], y + delta[1], z + delta[2])
        a.replace_chunk(ri, bytes(d))
        n += 1
    return n, len(runs)


def is_decoration_material(mat):
    """Viewport-only materials the importer adds, which must never reach the exporter.

    PO_Outline is the signature Punch-Out black outline: an inverted hull that exists only as
    a trailing material slot plus a Solidify modifier. It has no faces in the real mesh and
    corresponds to nothing in the archive, so demanding a mesh slot for it was simply a bug.
    Matches PO_Outline.001 / .002 too -- Blender renames on every re-import.
    """
    if mat is None:
        return True
    if mat.get("po_outline"):
        return True
    return (getattr(mat, "name", "") or "").split(".")[0] == "PO_Outline"


def slot_for_material(mat):
    """Resolve a material to a target mesh slot. Accepts a Material or a bare name.

    Prefer the explicit `po_slot` property. `slotN` remains as a compact manual fallback for
    scripts and older scenes."""
    mat_name = mat if isinstance(mat, str) else (getattr(mat, "name", "") or "")
    if not isinstance(mat, str) and mat is not None:
        s = mat.get("po_slot")
        if s is not None:
            return int(s)
    ln = mat_name.strip().lower()
    if ln.startswith("slot"):
        try:
            return int(ln[4:])
        except ValueError:
            pass
    raise RuntimeError(
        f"Material '{mat_name}' has no mesh slot, and one could not be recovered "
        f"automatically -- it has no 'po_matoff', so it was authored from scratch rather than "
        f"imported from the character.\n"
        f"  Give it one:  select it and run 'PO: Claim Slot for Active Material' (F3), or set "
        f"bpy.data.materials['{mat_name}']['po_slot'] = <index>.\n"
        f"  'List Source Slots' prints the target's slots with names and vert counts."
    )


def _po_ramp_color(mat):
    """Sample an imported PO material's own diffuse ramp, or None if it has no ramp node."""
    nt = getattr(mat, "node_tree", None)
    nd = nt.nodes.get("PO_Ramp") if nt else None
    if nd is None or not hasattr(nd, "color_ramp"):
        return None
    pos = nd.inputs["Fac"].default_value if "Fac" in nd.inputs else po_shader.PO_RAMP_POS
    rp = nt.nodes.get("PO_RampPos")
    if rp is not None:
        pos = rp.outputs[0].default_value
    try:
        c = nd.color_ramp.evaluate(max(0.0, min(1.0, pos)))
    except Exception:
        return None
    return (c[0], c[1], c[2])


def material_color(mat):
    """Linear-space (r,g,b) to bake this slot's ramp to.

    Careful with the Principled Base Color: po_build_shader LINKS the ramp chain into that
    socket and never writes its default_value, so on an imported PO material the default is
    Blender's stock 0.8 grey. Reading it blindly used to flatten every untouched slot of the
    character to grey the moment you exported with baking on. If the socket is linked, sample
    the material's own PO_Ramp instead, which makes baking an untouched material a no-op.
    """
    if mat and mat.use_nodes:
        for n in mat.node_tree.nodes:
            if n.type == "BSDF_PRINCIPLED":
                inp = n.inputs["Base Color"]
                if not inp.is_linked:
                    c = inp.default_value           # you set a flat color: use it
                    return (c[0], c[1], c[2])
                rc = _po_ramp_color(mat)            # driven by the PO graph: read the ramp
                if rc is not None:
                    return rc
                c = inp.default_value
                return (c[0], c[1], c[2])
        rc = _po_ramp_color(mat)                    # TEV graph (no Principled): read the ramp
        if rc is not None:
            return rc
    if mat:
        c = mat.diffuse_color
        return (c[0], c[1], c[2])
    return (0.8, 0.8, 0.8)


def base_color_is_image(mat):
    """True when Base Color is driven by an image that the colour bake cannot carry.

    The bake writes ONE flat colour into the slot's ramp texture. A material painted with an
    image looks right in the viewport and then exports as whatever single colour
    material_color() lands on -- which is how a red cap shipped as a flat white one. Painted
    images have to go through po_export_materials (PO_Detail), not the bake.
    """
    nt = getattr(mat, "node_tree", None)
    if not (mat and mat.use_nodes and nt):
        return False
    if nt.nodes.get("PO_Ramp") is not None:
        return False                       # a real PO material; the bake reads its ramp
    for n in nt.nodes:
        if n.type == "BSDF_PRINCIPLED" and n.inputs["Base Color"].is_linked:
            return True
    return False


def read_scene_buckets(armature, mesh_objs, resolve, fallback_hash, nearest_bone):
    """Return {slot: {...}}, {slot: rgb}, stats. Weights are stored as (bone_hash, weight),
    already resolved to real skinning bones (helper nodes collapsed, foreign groups dropped).
    Captured at REST pose."""
    prev = armature.data.pose_position
    armature.data.pose_position = "REST"
    bpy.context.view_layer.update()

    buckets = {}
    slot_color = {}
    stats = {"fallback_verts": 0, "dropped_groups": set(), "image_materials": set(),
             "material_slots": set(), "untouched_slots": set()}

    def bucket(slot):
        if slot not in buckets:
            buckets[slot] = {"verts": [], "normals": [], "uvs": [], "weights": [], "tris": [],
                             "source": [], "morph": [], "morph_keys": set()}
        return buckets[slot]

    def vweights(v, vgroups, co):
        acc = {}
        for g in v.groups:
            if g.weight <= 0.0001:
                continue
            name = vgroups[g.group].name
            h = resolve(name)
            if h is None:
                stats["dropped_groups"].add(name)
                continue
            acc[h] = acc.get(h, 0.0) + g.weight
        items = sorted(acc.items(), key=lambda x: -x[1])[:4]
        s = sum(w for _, w in items)
        if s <= 0.0:
            items = [(nearest_bone(co), 1.0)]
            s = 1.0
            stats["fallback_verts"] += 1
        items = [(h, w / s) for h, w in items]
        while len(items) < 4:
            items.append((0, 0.0))
        return items

    try:
        for obj in mesh_objs:
            mesh = obj.data
            mesh.calc_loop_triangles()
            # Corner normals carry the imported custom (authored) normals in every version;
            # MeshVertex.normal ignores them in 3.x, which rewrote every normal on export.
            if hasattr(mesh, "calc_normals_split"):
                mesh.calc_normals_split()
            vnormal = {}
            for loop in mesh.loops:
                n = vnormal.get(loop.vertex_index)
                vnormal[loop.vertex_index] = loop.normal.copy() if n is None else n + loop.normal
            # UV_Damage may be selected in the UV editor while painting bruises. Export UV0
            # by name so that UI selection cannot silently replace the character's skin UVs.
            uv_layer = mesh.uv_layers.get("UV")
            if uv_layer is None:
                uv_layer = mesh.uv_layers.active
            uv = uv_layer.data if uv_layer is not None else None
            src_attr = mesh.attributes.get(po_shader.VERTEX_SOURCE_ATTR)
            vsource = None
            if src_attr is not None and src_attr.domain == "POINT" and src_attr.data_type == "INT":
                vsource = [0] * len(mesh.vertices)
                src_attr.data.foreach_get("value", vsource)
            # Per-vertex morph deltas authored as shape keys: {key name: (dx, dy, dz)}.
            vmorph = shape_key_deltas(obj)
            morph_keys = ({kb.name for kb in mesh.shape_keys.key_blocks
                           if morph_key_channel(kb.name) is not None} if vmorph is not None else set())
            vgroups = obj.vertex_groups
            if not mesh.materials:
                raise RuntimeError(f"Object '{obj.name}' has no materials.")
            mat_slots = []
            for m in mesh.materials:
                if is_decoration_material(m):
                    mat_slots.append(None)          # skipped, not exported
                    continue
                s = slot_for_material(m)
                mat_slots.append(s)
                slot_color.setdefault(s, material_color(m))
                if base_color_is_image(m):
                    stats["image_materials"].add(m.name)
                # Slots the MATERIAL exporter is going to rebuild. The colour bake must not
                # flatten their ramps first -- it runs earlier, and po_export_materials will not
                # repaint a texture whose material it considers pristine, so the flat colour is
                # what ships. That is how a textured cap became a solid white one.
                if base_color_is_image(m) or not po_shader.material_is_pristine(m):
                    stats["material_slots"].add(s)
                elif m.get("po_record"):
                    # An imported material nobody touched. Its textures are already right; the
                    # bake would tint every one of them (detail, spec, ramp) to one flat colour.
                    stats["untouched_slots"].add(s)
            local = {}
            for tri in mesh.loop_triangles:
                slot = mat_slots[tri.material_index]
                if slot is None:
                    continue
                b = bucket(slot)
                b["morph_keys"] |= morph_keys
                idx = []
                for li, vi in zip(tri.loops, tri.vertices):
                    # The game stores one UV per vertex, so a UV seam must split the vertex.
                    # Keyed on the vertex alone, a seam you cut yourself kept whichever UV came
                    # first and the faces along it smeared across the texture.
                    u = tuple(uv[li].uv) if uv else (0.0, 0.0)
                    key = (obj.name, vi, slot, round(u[0], 5), round(u[1], 5))
                    if key not in local:
                        v = mesh.vertices[vi]
                        co = obj.matrix_world @ v.co
                        no = (obj.matrix_world.to_3x3() @ vnormal.get(vi, v.normal)).normalized()
                        b["verts"].append([co.x, co.y, co.z])
                        b["normals"].append([no.x, no.y, no.z])
                        b["uvs"].append([u[0], 1.0 - u[1]])
                        b["weights"].append(vweights(v, vgroups, (co.x, co.y, co.z)))
                        # the archive vertex this one came from, if it still sits in that slot
                        b["source"].append(vertex_source(vsource[vi], slot) if vsource is not None else -1)
                        b["morph"].append(vmorph.get(vi) if vmorph is not None else None)
                        local[key] = len(b["verts"]) - 1
                    idx.append(local[key])
                b["tris"].append(idx)
    finally:
        armature.data.pose_position = prev
        bpy.context.view_layer.update()

    return buckets, slot_color, stats


# ===========================================================================
# geometry encode
# ===========================================================================
def _f32(x):
    return struct.pack(">f", x)


def tris_to_strip(tris):
    strip = []
    for (a, b, c) in tris:
        if strip:
            strip.append(strip[-1]); strip.append(a)
        if len(strip) % 2 == 0:
            strip += [a, b, c]
        else:
            strip += [a, c, b]
    return strip


def _pack_uv(uvs):
    # SIGNED s16/1024 -- clamping at 0 here silently destroyed every negative UV
    # (bh_nipple, hankerchief, boot soles, gloves ... 42 meshes across 4 characters).
    return b"".join(struct.pack(">hh",
                    max(-32768, min(32767, round(u * 1024))),
                    max(-32768, min(32767, round(v * 1024)))) for (u, v) in uvs)


def _modal_bytes(raw, stride):
    """The most common per-vertex value in an attribute, or None if it is empty."""
    if not raw or stride <= 0:
        return None
    counts = {}
    for i in range(0, len(raw) - stride + 1, stride):
        b = bytes(raw[i:i + stride])
        counts[b] = counts.get(b, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0] if counts else None


def aux_default(a_type, stride, orig_raw):
    """What an aux vertex attribute reads when there is no source vertex to copy from.

    Zeros are never a safe default. 0xE9 is vertex colour RGBA8, so 00000000 is black AND
    fully transparent -- that is what a rebuilt mesh used to be filled with.
    """
    m = _modal_bytes(orig_raw, stride)
    if m is not None:
        return m
    if a_type == 0xE9 and stride == 4:
        return b"\xff\xff\xff\xff"
    return b"\x00" * stride


def nearest_source_map(old_pos, new_pos, old_uv=None, new_uv=None, old_nrm=None, new_nrm=None):
    """new vertex index -> old vertex index, matched by position, tie-broken by UV.

    Exact position first, so a mesh nobody edited (Blender only re-splits its vertices) copies
    its aux attributes back byte-for-byte. Everything else falls back to the nearest vertex in
    bbox-NORMALISED space, so a mesh that was moved or rescaled wholesale -- which is every
    reshaped character -- still lands on its real counterpart rather than on whatever happens to
    be nearest in absolute coordinates.

    Position alone is not a key. A UV seam is several vertices at the SAME point that differ only
    in their texture coordinates, and 0x3D is itself a UV set, so picking the wrong one of a
    coincident pair moves a seam vertex's second UV -- 179 of donkeykong's shirt vertices, before
    UV0 was used to break the tie. A hard edge is the same thing one level down: same position,
    same UV, different normal, and 0x05 varies across those. So the key is
    position, then UV, then normal.
    """
    n = len(new_pos)
    if not old_pos or not n:
        return [None] * n
    inv = 1.0 / 1e-4

    def cell(p):
        return (int(round(p[0] * inv)), int(round(p[1] * inv)), int(round(p[2] * inv)))

    grid = {}
    for j, p in enumerate(old_pos):
        grid.setdefault(cell(p), []).append(j)
    have_uv = (old_uv is not None and new_uv is not None
               and len(old_uv) >= len(old_pos) and len(new_uv) >= n)
    have_nrm = (old_nrm is not None and new_nrm is not None
                and len(old_nrm) >= len(old_pos) and len(new_nrm) >= n)

    claimed = set()

    def rank(j, i):
        d_uv = ((old_uv[j][0] - new_uv[i][0]) ** 2
                + (old_uv[j][1] - new_uv[i][1]) ** 2) if have_uv else 0.0
        d_n = sum((old_nrm[j][k] - new_nrm[i][k]) ** 2 for k in range(3)) if have_nrm else 0.0
        # Last key only separates vertices that are identical in position, UV and normal -- the
        # archive does store those, with different tangents. Handing each new vertex a source
        # nobody took yet recovers them by exclusion; when the exporter genuinely splits one
        # vertex into several, the distance keys have already decided and this changes nothing.
        return (d_uv, d_n, 1 if j in claimed else 0)

    lo = [min(p[k] for p in old_pos) for k in range(3)]
    hi = [max(p[k] for p in old_pos) for k in range(3)]
    nlo = [min(p[k] for p in new_pos) for k in range(3)]
    nhi = [max(p[k] for p in new_pos) for k in range(3)]
    scale = [(hi[k] - lo[k]) / (nhi[k] - nlo[k]) if (nhi[k] - nlo[k]) > 1e-9 else 0.0
             for k in range(3)]
    out = []
    for i, p in enumerate(new_pos):
        cands = grid.get(cell(p))
        if cands:
            j = cands[0] if len(cands) == 1 else min(cands, key=lambda c: rank(c, i))
            claimed.add(j)
            out.append(j)
            continue
        q = [lo[k] + (p[k] - nlo[k]) * scale[k] for k in range(3)]
        best, bd = 0, None
        for j2, o in enumerate(old_pos):
            d = (o[0] - q[0]) ** 2 + (o[1] - q[1]) ** 2 + (o[2] - q[2]) ** 2
            if bd is None or d < bd:
                bd, best = d, j2
        out.append(best)
    return out


def build_attrs(orig_attrs, verts, normals, uvs, bidx_bytes, weight_bytes, vcount, srcmap=None):
    """Rebuild every vertex attribute the mesh record declares -- including the ones we cannot
    author.

    Every boxer mesh ships EIGHT attributes: 0x0A position, 0xFE normal, 0xCC UV0, 0x05 UV1
    (used by damage textures), 0x3D UV2, 0xE9 vertex colour RGBA8, 0xD4 bone indices,
    0xB0 weights. Blender gives us five of those. The other three used to be written as zeros,
    which is how every exported character lost its vertex colours (-> black, alpha 0) and had its
    auxiliary UV sets collapsed onto texel (0,0). They are now resampled from the source mesh through
    `srcmap`.

    0x3D is NOT a copy of 0xCC in general -- it matches on all 34 referee meshes but differs on
    32 of donkeykong's 35 -- so it is only regenerated from the new UVs when the source mesh had
    the two byte-identical, and resampled otherwise.
    """
    orig = {t: (s, r) for (t, s, _f, r) in orig_attrs}
    uv2_mirrors_uv0 = (0x3D in orig and 0xCC in orig
                       and bool(orig[0xCC][1]) and orig[0x3D][1] == orig[0xCC][1])
    out = []
    for (a_type, a_str, a_flags, orig_raw) in orig_attrs:
        if a_type == 0x0A and a_str == 12:
            raw = b"".join(_f32(x) + _f32(y) + _f32(z) for (x, y, z) in verts)
        elif a_type == 0xFE and a_str == 12:
            raw = b"".join(_f32(x) + _f32(y) + _f32(z) for (x, y, z) in normals)
        elif a_type == 0xCC and a_str == 4:
            raw = _pack_uv(uvs)
        elif a_type == 0x3D and a_str == 4 and uv2_mirrors_uv0:
            raw = _pack_uv(uvs)
        elif a_type == 0xD4 and a_str == 4:
            raw = bidx_bytes
        elif a_type == 0xB0 and a_str == 16:
            raw = weight_bytes
        else:
            dflt = aux_default(a_type, a_str, orig_raw)
            n_orig = len(orig_raw) // a_str if a_str else 0
            parts = []
            for i in range(vcount):
                j = srcmap[i] if (srcmap is not None and i < len(srcmap)) else None
                parts.append(bytes(orig_raw[j * a_str:(j + 1) * a_str])
                             if (j is not None and j < n_orig) else dflt)
            raw = b"".join(parts)
        out.append((a_type, a_str, a_flags, raw))
    return out


def build_target_mesh(nlg_geom, fm_orig, bucket):
    """bucket['weights'] entries are lists of (bone_hash, weight) already resolved to real
    skinning bones. Build the per-mesh bone-hash palette + per-vertex indices from them."""
    verts = [tuple(v) for v in bucket["verts"]]
    normals = [tuple(n) for n in bucket["normals"]]
    uvs = [tuple(u) for u in bucket["uvs"]]
    tris = [tuple(t) for t in bucket["tris"]]
    vcount = len(verts)

    palette, seen, per_vert = [], {}, []
    for wlist in bucket["weights"]:
        entry = []
        for (h, w) in wlist:
            if not h or w <= 0.0001:
                entry.append((0, 0.0)); continue
            if h not in seen:
                seen[h] = len(palette); palette.append(h)
            entry.append((seen[h], w))
        per_vert.append(entry)
    if len(palette) > 255:
        raise RuntimeError(f"slot uses {len(palette)} bones; palette index is 1 byte (max 255). "
                           "Reduce distinct bone influences on this material.")

    bidx = bytearray(); wbuf = bytearray()
    for entry in per_vert:
        idxs = [e[0] for e in entry] + [0, 0, 0, 0]
        wts = [e[1] for e in entry] + [0.0, 0.0, 0.0, 0.0]
        bidx += bytes(idxs[:4])
        wbuf += b"".join(_f32(w) for w in wts[:4])

    fm = nlg_geom.FullMesh()
    fm.record = bytearray(fm_orig.record)
    fm.vcount = vcount
    fm.idxfmt = fm_orig.idxfmt
    # Aux attributes (0x05, UV set 2, vertex colour) can only come from the mesh that was
    # already in this slot, so pair every new vertex with the source vertex it stands in for.
    srcmap = nearest_source_map(mesh_positions(fm_orig), verts,
                                mesh_uvs(fm_orig), uvs, mesh_normals(fm_orig), normals)
    # a vertex that remembers its archive vertex takes that one's attributes, not a lookalike's
    for j, s in enumerate(bucket.get("source") or ()):
        if 0 <= s < fm_orig.vcount:
            srcmap[j] = s
    fm.attrs = build_attrs(fm_orig.attrs, verts, normals, uvs, bytes(bidx), bytes(wbuf), vcount,
                           srcmap)
    fm.strip = tris_to_strip(tris)
    palette_bytes = b"".join(struct.pack(">I", h) for h in palette)
    return fm, palette_bytes, palette


def mesh_positions(fm):
    """[(x, y, z)] read straight out of a FullMesh's position attribute (0x0A)."""
    for (t, stride, _f, raw) in fm.attrs:
        if t == 0x0A and stride == 12:
            return [struct.unpack_from(">3f", raw, i * 12) for i in range(len(raw) // 12)]
    return []


def mesh_normals(fm):
    """[(x, y, z)] read straight out of a FullMesh's normal attribute (0xFE)."""
    for (t, stride, _f, raw) in fm.attrs:
        if t == 0xFE and stride == 12:
            return [struct.unpack_from(">3f", raw, i * 12) for i in range(len(raw) // 12)]
    return []


def mesh_uvs(fm):
    """[(u, v)] read straight out of a FullMesh's UV0 attribute (0xCC), signed s16/1024."""
    for (t, stride, _f, raw) in fm.attrs:
        if t == 0xCC and stride == 4:
            return [(struct.unpack_from(">h", raw, i * 4)[0] / 1024.0,
                     struct.unpack_from(">h", raw, i * 4 + 2)[0] / 1024.0)
                    for i in range(len(raw) // 4)]
    return []


def build_vertex_remap(old_pos, new_pos, tol=1e-4):
    """old local vertex index -> [new local vertex indices at the same position].

    One-to-MANY on purpose. The exporter re-splits vertices per material slot, so a single
    archive vertex can come back as several; a morph delta has to move all of them or the mesh
    tears open along the seam the moment the shape fires.

    Positions are compared in world space, which is also archive space (the importer places
    everything at raw archive coordinates and never transforms the armature object).
    """
    if not old_pos or not new_pos:
        return {}
    inv = 1.0 / tol
    grid = {}
    for j, p in enumerate(new_pos):
        grid.setdefault((int(round(p[0]*inv)), int(round(p[1]*inv)), int(round(p[2]*inv))),
                        []).append(j)
    out = {}
    t2 = tol * tol
    for i, p in enumerate(old_pos):
        cx, cy, cz = int(round(p[0]*inv)), int(round(p[1]*inv)), int(round(p[2]*inv))
        hits = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for j in grid.get((cx+dx, cy+dy, cz+dz), ()):
                        q = new_pos[j]
                        if ((q[0]-p[0])**2 + (q[1]-p[1])**2 + (q[2]-p[2])**2) <= t2:
                            hits.append(j)
        if hits:
            out[i] = hits
    return out


def vertex_source(value, slot):
    """Local archive index recorded in a po_vertex_source value, or -1.

    -1 when the value is 0 (a vertex Blender created: joined, extruded, added) or names a
    different slot (a face reassigned to another material)."""
    src = value - 1
    if src < 0 or src // po_shader.VERTEX_SOURCE_SLOT != slot:
        return -1
    return src % po_shader.VERTEX_SOURCE_SLOT


def build_source_remap(old_pos, new_pos, source=None):
    """old local vertex index -> [new local vertex indices], by recorded identity first.

    `source[j]` is the archive vertex new vertex j was imported from (-1 if unknown). A position
    match cannot tell coincident vertices apart -- a hair flap lying on the scalp, an eyelid over
    its socket -- and gave the flap's delta to the scalp too, so the head tore open when the
    morph fired. It also lost every delta on a vertex that was moved. Position matching is kept
    only for vertices with no recorded source (older imports, geometry you added), and only
    against new vertices that have no source of their own.
    """
    if not source or all(s < 0 for s in source):
        return build_vertex_remap(old_pos, new_pos)
    out = {}
    for j, s in enumerate(source):
        if 0 <= s < len(old_pos):
            out.setdefault(s, []).append(j)
    loose = [j for j, s in enumerate(source) if not 0 <= s < len(old_pos)]
    orphans = [i for i in range(len(old_pos)) if i not in out]
    if loose and orphans:
        sub = build_vertex_remap([old_pos[i] for i in orphans], [new_pos[j] for j in loose])
        for oi, hits in sub.items():
            out[orphans[oi]] = [loose[h] for h in hits]
    return out


def _parse_b00c(d):
    """(A, B, breakpoints, S, lists) -- lists[mesh][channel] = [ [(idx,dx,dy,dz)...] per shape ].

    Grammar (nlg_morph.py): u32 A total shapes, u32 B channels, A f32 weight breakpoints,
    u32 recSize(16), u32 S mesh-lists, then S x B x {u32 nShapes, nShapes x {u32 nRecs,
    nRecs x {f32 dx, f32 dy, f32 dz, u32 LOCAL vertexIndex}}}.
    """
    A, B = struct.unpack_from(">2I", d, 0)
    o = 8
    bps = list(struct.unpack_from(">%df" % A, d, o)); o += 4 * A
    recSize, S = struct.unpack_from(">2I", d, o); o += 8
    if recSize != 16:
        raise ValueError("0xB00C record size is %d, expected 16" % recSize)
    lists = []
    for _li in range(S):
        chans = []
        for _ch in range(B):
            n = struct.unpack_from(">I", d, o)[0]; o += 4
            shapes = []
            for _si in range(n):
                nr = struct.unpack_from(">I", d, o)[0]; o += 4
                recs = []
                for _k in range(nr):
                    dx, dy, dz = struct.unpack_from(">3f", d, o)
                    idx = struct.unpack_from(">I", d, o + 12)[0]
                    recs.append((idx, dx, dy, dz)); o += 16
                shapes.append(recs)
            chans.append(shapes)
        lists.append(chans)
    return A, B, bps, S, lists


def _build_b00c(A, B, bps, S, lists):
    out = bytearray(struct.pack(">2I", A, B))
    out += struct.pack(">%df" % A, *bps)
    out += struct.pack(">2I", 16, S)
    for li in range(S):
        for ch in range(B):
            shapes = lists[li][ch]
            out += struct.pack(">I", len(shapes))
            for recs in shapes:
                out += struct.pack(">I", len(recs))
                for (idx, dx, dy, dz) in recs:
                    out += struct.pack(">3fI", dx, dy, dz, idx)
    return bytes(out)


def remap_morphs(a, remaps, vcounts):
    """Rewrite the facial morph chunk (0xB00C) to follow the geometry we just replaced.

    Morph deltas address vertices by LOCAL index inside a mesh, and the exporter rebuilds every
    mesh -- re-splitting, reordering and (for unmapped slots) zeroing them. Left alone, the
    records keep pointing at the OLD numbering: on the referee, mesh 6 went from 50 vertices to
    19 while a record still asked for index 20, so a blink writes past the end of the vertex
    buffer. Validated with validate_mod.py, which is what caught it.

    `remaps[mi]` is {old index: [new indices]} for a rebuilt slot, or None/absent when the slot
    has no usable correspondence. A delta whose vertex cannot be located is DROPPED -- shipping
    a wrong index is worse than losing one blend-shape vertex, and dropping keeps the chunk's
    shape/channel structure intact so the engine still finds what it expects.

    Returns (kept, dropped, duplicated) record counts.
    """
    cs = a.find_chunks(type_id=0xB00C)
    if not cs:
        return 0, 0, 0
    try:
        A, B, bps, S, lists = _parse_b00c(a.get_chunk_bytes(cs[0]))
    except Exception as ex:
        print("PunchOut: 0xB00C did not parse (%s); morphs left untouched" % ex)
        return 0, 0, 0
    kept = dropped = dup = 0
    for mi in range(S):
        if mi not in remaps:
            # Not a mesh we rebuilt (0xB00C can list more mesh slots than the model we touched),
            # so its indices are still valid. An ABSENT entry means leave alone; an EMPTY one
            # means we rebuilt it and found no correspondence, which does mean drop.
            continue
        rm = remaps[mi]
        limit = vcounts.get(mi, 0)
        for ch in range(B):
            shapes = lists[mi][ch]
            for si, recs in enumerate(shapes):
                if not recs:
                    continue
                out = []
                for (idx, dx, dy, dz) in recs:
                    tgt = rm.get(idx)
                    if not tgt:
                        dropped += 1
                        continue
                    hit = [j for j in tgt if j < limit]   # never emit an index the mesh lacks
                    if not hit:
                        dropped += 1
                        continue
                    out.extend((j, dx, dy, dz) for j in hit)
                    kept += 1
                    dup += len(hit) - 1
                shapes[si] = out
    a.replace_chunk(cs[0], _build_b00c(A, B, bps, S, lists))
    return kept, dropped, dup


MORPH_EPS = 1e-5    # smaller shape-key offsets are float noise, not an authored delta
MORPH_TOL = 1e-4    # authored and archive deltas this close are the same record


def morph_key_channel(name):
    """Channel number of an imported morph shape key ('06_blink_r_top' -> 6), else None."""
    return int(name[:2]) if len(name) > 3 and name[:2].isdigit() and name[2] == "_" else None


def shape_key_deltas(obj):
    """{vertex index: {key name: (dx, dy, dz)}} for every morph shape key, in archive space.

    None when the object has no shape keys at all, so meshes built without them keep the
    archive's records instead of being read as 'every morph deleted'."""
    keys = obj.data.shape_keys
    if keys is None:
        return None
    rot = obj.matrix_world.to_3x3()
    out = {}
    for kb in keys.key_blocks:
        if kb == keys.reference_key or morph_key_channel(kb.name) is None:
            continue
        base = kb.relative_key.data
        for i, p in enumerate(kb.data):
            d = p.co - base[i].co
            if d.length > MORPH_EPS:
                d = rot @ d
                out.setdefault(i, {})[kb.name] = (d.x, d.y, d.z)
    return out


def author_morphs(a, buckets):
    """Write shape-key edits into 0xB00C, on top of the records remap_morphs carried over.

    Each imported shape key is one (channel, shape) of the morph chunk: '06_blink_r_top', or
    '11_x_50' for the 0.5 stage of a two-stage channel. Wherever a key's deltas on a rebuilt
    mesh match the carried-over records they are left byte-for-byte; where they differ -- a
    vertex moved further, or one the source never morphed -- that mesh's shape is rewritten from
    the key. Channel count, breakpoints and every clip stay as they are, so the engine drives an
    extended shape exactly as it drove the original.

    Returns [(mesh, channel, shape, records before, records after)] for each rewritten shape.
    """
    cs = a.find_chunks(type_id=0xB00C)
    if not cs:
        return []
    A, B, bps, S, lists = _parse_b00c(a.get_chunk_bytes(cs[0]))
    counts = [len(s) for s in lists[0]] if S else []
    by_ch, i = [], 0
    for n in counts:
        by_ch.append(bps[i:i + n]); i += n

    def shape_of(name):
        ch = morph_key_channel(name)
        if ch is None or ch >= B or not by_ch[ch]:
            return None
        if len(by_ch[ch]) == 1:
            return ch, 0
        tail = name.rsplit("_", 1)[-1]
        for si, b in enumerate(by_ch[ch]):
            if tail == str(int(round(b * 100))):
                return ch, si
        return None

    changed = []
    for mi, b in buckets.items():
        per = b.get("morph") or []
        if mi >= S or not any(m is not None for m in per):
            continue
        authored = {}
        for j, deltas in enumerate(per):
            for name, d in (deltas or {}).items():
                cs_ = shape_of(name)
                if cs_ is not None:
                    authored.setdefault(cs_, {})[j] = d
        # shapes whose key exists but moves nothing on this mesh are authored as empty
        for (ch, si) in {shape_of(nm) for nm in b.get("morph_keys", ())} - {None}:
            authored.setdefault((ch, si), {})
        for (ch, si), want in authored.items():
            have = {idx: (dx, dy, dz) for idx, dx, dy, dz in lists[mi][ch][si]
                    if abs(dx) + abs(dy) + abs(dz) > MORPH_EPS}
            if want.keys() == have.keys() and all(
                    max(abs(p - q) for p, q in zip(want[k], have[k])) <= MORPH_TOL for k in want):
                continue
            before = len(lists[mi][ch][si])
            lists[mi][ch][si] = [(j,) + tuple(want[j]) for j in sorted(want)]
            changed.append((mi, ch, si, before, len(lists[mi][ch][si])))
    if changed:
        a.replace_chunk(cs[0], _build_b00c(A, B, bps, S, lists))
    return changed


def zero_mesh(nlg_geom, fm_orig):
    fm = nlg_geom.FullMesh()
    fm.record = bytearray(fm_orig.record)
    fm.vcount = 0
    fm.idxfmt = fm_orig.idxfmt
    fm.attrs = [(t, s, f, b"") for (t, s, f, _r) in fm_orig.attrs]
    fm.strip = []
    return fm


# ===========================================================================
# source slot introspection (for the mapping + color bake)
# ===========================================================================
def source_slot_table(archive, hn):
    """slot -> (mesh_name, vcount, ramp_hash, ramp_name)."""
    meshcs = archive.find_chunks(type_id=0xB004)
    matcs = archive.find_chunks(type_id=0xB016)
    meshdata = archive.get_chunk_bytes(meshcs[0])
    matdata = archive.get_chunk_bytes(matcs[0]) if matcs else b""
    out = {}
    for mi in range(len(meshdata) // 52):
        o = mi * 52
        vcount = struct.unpack_from(">H", meshdata, o + 8)[0]
        mh = struct.unpack_from(">I", meshdata, o + 20)[0]
        matoff = struct.unpack_from(">I", meshdata, o + 36)[0]
        ramp = struct.unpack_from(">I", matdata, matoff + 24)[0] if matoff + 28 <= len(matdata) else 0
        out[mi] = (hn.get(mh, f"{mh:08x}"), vcount, ramp, hn.get(ramp, f"{ramp:08x}"))
    return out


def _level_size(w, h):
    """CMPR bytes for ONE mip level at these dimensions."""
    return max(1, (max(1, w) + 7) // 8) * max(1, (max(1, h) + 7) // 8) * 32


def mip_dims(w, h, levels):
    """[(w, h, byte_size)] for every level the record declares, base first."""
    return [(max(1, w >> k), max(1, h >> k), _level_size(max(1, w >> k), max(1, h >> k)))
            for k in range(max(1, levels))]


def texture_table(archive):
    """hash -> (w, h, dataOffset, TOTAL chain bytes, mip levels, format byte).

    The 4th element is the size of the WHOLE mip chain, not the base level. Textures are stored
    as base + every mip contiguously (verified on the referee: ref_teeth_spec is 64x64 with 4
    levels at offset 0, and the next texture starts at 2720 = the chain size, not 2048 = base).

    This used to report the base size, so the colour bake decoded level 0, re-encoded level 0,
    and wrote back only those bytes -- leaving mips 1..N holding the SOURCE character's colours.
    On console that shows up as the original colour bleeding back as the camera pulls away,
    since the GPU picks a smaller level. 20 of the referee's 31 textures are mipped.
    """
    th = archive.find_chunks(type_id=0xB601)
    if not th:
        return {}
    import nlg_texture
    hdr = archive.get_chunk_bytes(th[0]); out = {}
    for i in range(len(hdr) // 96):
        # Sizes follow each record's own format: fighter ramps (128x4, 256x8) are RGB565, not CMPR.
        e = nlg_texture._entry(hdr, i, None)
        out[e.hash] = (e.width, e.height, e.data_offset,
                       nlg_texture.format_chain_size(e.fmt, e.width, e.height, e.levels), e.levels, e.fmt)
    return out


def recolor_chain(td, doff, w, h, levels, fn, fmt=6):
    """Apply `fn(rgba_bytes) -> rgba_bytes` to EVERY mip level of a texture, in place.

    Each level is decoded, transformed and re-encoded at its own dimensions in the record's own
    format, so the chain keeps exactly the byte length the record declares -- every format is
    fixed-size per dimensions, so this can never shift the offset of the texture that follows.
    Returns False (and changes nothing) for formats without an encoder.
    """
    import nlg_texture
    name = nlg_texture.FORMATS.get(fmt)
    decode, encode = nlg_texture.DECODERS.get(name), nlg_texture.ENCODERS.get(name)
    if decode is None or encode is None:
        return False
    o = doff
    for k in range(max(1, levels)):
        lw, lh = max(1, w >> k), max(1, h >> k)
        size = nlg_texture.texture_size(fmt, lw, lh)
        enc = encode(fn(decode(bytes(td[o:o + size]), lw, lh)), lw, lh)
        if len(enc) != size:                     # belt and braces: must not move the chain
            raise RuntimeError("%s re-encode changed size at %dx%d (%d != %d)"
                               % (name, lw, lh, len(enc), size))
        td[o:o + size] = enc
        o += size
    return True


# color-carrying texture slots in a material record (byte offsets from matoff). The base albedo
# lives at +0 ("*_shadow" for skin), shadow tone at +16, highlight/rim ramp at +24. The lighting
# maps at +32..+56 (rimlight/hdr/fresnel/specramp) are NOT recolored.
MATERIAL_COLOR_SLOTS = (0, 16, 24)


def material_color_textures(archive, slot):
    """Return the set of texture hashes this mesh slot references in its color slots."""
    meshdata = archive.get_chunk_bytes(archive.find_chunks(type_id=0xB004)[0])
    matdata = archive.get_chunk_bytes(archive.find_chunks(type_id=0xB016)[0])
    matoff = struct.unpack_from(">I", meshdata, slot * 52 + 36)[0]
    out = []
    for k in MATERIAL_COLOR_SLOTS:
        if matoff + k + 4 <= len(matdata):
            out.append(struct.unpack_from(">I", matdata, matoff + k)[0])
    return out


# lighting/reflection slots: rimlight ramp (+32), HDR env map (+40), fresnel (+48), spec ramp (+56).
# When these carry a colored (warm) tint they cast that color over the recolored albedo — the source
# of "purple skin / red metallic gloves". Desaturating them to neutral grey keeps the light/dark
# shaping while removing the color cast.
MATERIAL_LIGHTING_SLOTS = (32, 40, 48, 56)


def material_lighting_textures(archive, slot):
    meshdata = archive.get_chunk_bytes(archive.find_chunks(type_id=0xB004)[0])
    matdata = archive.get_chunk_bytes(archive.find_chunks(type_id=0xB016)[0])
    matoff = struct.unpack_from(">I", meshdata, slot * 52 + 36)[0]
    out = []
    for k in MATERIAL_LIGHTING_SLOTS:
        if matoff + k + 4 <= len(matdata):
            out.append(struct.unpack_from(">I", matdata, matoff + k)[0])
    return out


def duplicate_texture(archive, nlg_hash, src_hash):
    """Append an independent copy of texture `src_hash` (new TextureHeader + copied TextureData)
    and return the new texture's hash. Lets two meshes that shared one texture be colored
    separately. Existing offsets are untouched (we append), so this is resize-safe.

    The copy takes the WHOLE mip chain. The header is cloned verbatim, mip count included, and
    the game reads that many levels contiguously from the new offset -- so copying only the base
    level leaves it reading whatever bytes happen to follow as its smaller mips. On a 64x64/4-level
    texture that is 672 bytes of someone else's pixels, and at the end of the chunk it runs off
    the end entirely, which hangs on load rather than glitching.
    """
    th_ri = archive.find_chunks(type_id=0xB601)[0]
    td_ri = archive.find_chunks(type_id=0xB603)[0]
    hdr = bytearray(archive.get_chunk_bytes(th_ri))
    data = bytearray(archive.get_chunk_bytes(td_ri))

    # locate the source 96-byte header
    src = None
    existing = set()
    for i in range(len(hdr) // 96):
        h = struct.unpack_from(">I", hdr, i * 96)[0]
        existing.add(h)
        if h == src_hash:
            src = hdr[i * 96:i * 96 + 96]
    if src is None:
        return None
    import nlg_texture
    e = nlg_texture._entry(bytes(src), 0, None)
    levels, srcoff = e.levels, e.data_offset
    size = nlg_texture.format_chain_size(e.fmt, e.width, e.height, levels)
    pixels = bytes(data[srcoff:srcoff + size])
    if len(pixels) != size:
        raise RuntimeError("texture %08x claims %d mip level(s) = %d bytes at offset %d, but "
                           "only %d bytes remain in the pixel chunk -- the SOURCE archive is "
                           "already inconsistent." % (src_hash, levels, size, srcoff, len(pixels)))

    # align new data offset to 32
    while len(data) % 32:
        data.append(0)
    newoff = len(data)
    data += pixels

    # unique new hash
    n = 0
    while True:
        cand = nlg_hash.string_to_hash(f"_modcopy_{src_hash:08x}_{n}")
        if cand not in existing:
            break
        n += 1

    newhdr = bytearray(src)
    struct.pack_into(">I", newhdr, 0, cand)
    struct.pack_into(">I", newhdr, 20, newoff)
    hdr += newhdr

    archive.replace_chunk(th_ri, bytes(hdr))
    archive.replace_chunk(td_ri, bytes(data))
    return cand


def repoint_material_textures(archive, slot, old_hash, new_hash):
    """Point a mesh slot's material references from old_hash to new_hash (any slot it appears in)."""
    meshdata = archive.get_chunk_bytes(archive.find_chunks(type_id=0xB004)[0])
    matcs = archive.find_chunks(type_id=0xB016)[0]
    matbuf = bytearray(archive.get_chunk_bytes(matcs))
    mo = struct.unpack_from(">I", meshdata, slot * 52 + 36)[0]
    changed = 0
    for k in range(0, 64, 8):
        if mo + k + 4 <= len(matbuf) and struct.unpack_from(">I", matbuf, mo + k)[0] == old_hash:
            struct.pack_into(">I", matbuf, mo + k, new_hash)
            changed += 1
    archive.replace_chunk(matcs, bytes(matbuf))
    return changed


# ===========================================================================
# main export
# ===========================================================================
def do_export(source_dict, out_dict, bake_colors=True, shade_floor=1.0, neutralize_lighting=True,
              write_skeleton=True, materials_follow=False):
    # The UI export starts a fresh output; material chaining below remains internal.
    from pathlib import Path
    from po_archive import external, ArchiveError
    source = Path(source_dict).resolve()
    source_root = Path(_find_hashid(str(source)) or source).parent   # the dump root, by hashid.bin
    output = external(out_dict, source_root)
    if output.suffix != ".dict":
        raise ArchiveError("Choose a .dict output filename in a new external folder.")
    if output.exists() or output.with_suffix(".data").exists():
        raise ArchiveError("Output already exists. Choose a new filename to preserve the previous export.")
    nlg_pack, nlg_geom, nlg_hash, nlg_anim2 = _load_tools()
    hashid = _find_hashid(source_dict)
    hn = nlg_hash.load_hashid_bin(hashid) if hashid else {}

    resolve, fallback_hash, bone_set, nearest_bone = build_bone_resolver(
        nlg_pack, nlg_hash, nlg_anim2, source_dict, hashid)

    armature = find_armature()
    mesh_objs = find_mesh_objects(armature)

    # Recover any missing slot / node metadata ourselves. Every material imported from a PO
    # archive carries po_matoff, and materialOffset is 1:1 with the mesh slot, so this is
    # derivable -- there is no reason to stop the export and make the user run an operator by
    # hand. Only fills in what is absent; anything already set is left alone.
    for _o in mesh_objs:
        try:
            po_recover_slots(_o, source_dict, verbose=False)
        except Exception as _ex:
            print("PunchOut: slot auto-recovery skipped for '%s': %s" % (_o.name, _ex))
    try:
        if not any(b.get("po_node") is not None for b in armature.data.bones):
            po_recover_nodes(armature, source_dict, verbose=False)
    except Exception as _ex:
        print("PunchOut: node auto-recovery skipped: %s" % _ex)

    buckets, slot_color, stats = read_scene_buckets(
        armature, mesh_objs, resolve, fallback_hash, nearest_bone)

    a = nlg_pack.Archive(source_dict)
    meshes, chunk_ids = nlg_geom.read_model_full(a)
    bh = a.find_chunks(type_id=0xB00B)

    nmesh = len(meshes)
    for slot in buckets:
        if slot >= nmesh:
            raise RuntimeError(f"slot {slot} is out of range (source has {nmesh} mesh slots, 0..{nmesh-1}).")

    new_meshes, report = [], []
    morph_remaps, vcounts = {}, {}
    for i, fm_orig in enumerate(meshes):
        if i in buckets:
            fm, pal, palette = build_target_mesh(nlg_geom, fm_orig, buckets[i])
            new_meshes.append(fm)
            if i < len(bh):
                a.replace_chunk(bh[i], pal)
            report.append(f"slot {i}: {fm.vcount} verts, {len(palette)} bones")
            # Match the rebuilt vertices back to the ones the morph deltas were authored
            # against, so blend shapes keep addressing the right vertex. Untouched geometry
            # matches exactly; a reshaped mesh matches wherever it did not move.
            morph_remaps[i] = build_source_remap(mesh_positions(fm_orig), mesh_positions(fm),
                                                 buckets[i].get("source"))
        else:
            fm = zero_mesh(nlg_geom, fm_orig)
            new_meshes.append(fm)
            morph_remaps[i] = {}        # zeroed slot: every delta on it must go
        vcounts[i] = fm.vcount
    nlg_geom.encode_model(a, new_meshes, chunk_ids)

    # Morph records index vertices LOCALLY, so they have to follow the rebuild or they point
    # into the old numbering -- past the end of a shrunken mesh, in the worst case.
    mkept, mdropped, mdup = remap_morphs(a, morph_remaps, vcounts)
    if mkept or mdropped:
        report.append("morph deltas: %d remapped (%d split across duplicated vertices), "
                      "%d dropped with no matching vertex" % (mkept, mdup, mdropped))
        if mdropped and not mkept:
            report.append("NOTE: no morph delta could be matched -- facial animation will not "
                          "play. That is expected if you replaced the head geometry outright.")
    for mi, ch, si, before, after in author_morphs(a, buckets):
        report.append("morph shape keys: slot %d channel %d shape %d rewritten, %d -> %d records"
                      % (mi, ch, si, before, after))

    # Bind JOINT POSITIONS must follow the geometry. Vertices are written at their new rest
    # positions; if W_bind still uses the old joint origins the game skins them to the wrong
    # place. The source 3x3 axes stay intact because animation tracks use that coordinate frame.
    if write_skeleton:
        # Order matters: node offsets set the PROPORTIONS the animation drives; BoneData sets
        # where skinning joints sit. Both must agree.
        root_node = anim_root_node(a)
        root_before = node_offset(a, root_node)
        nwrote, nnote = write_node_offsets(a, armature)
        if nwrote:
            report.append("node offsets (proportions) updated for %d bone(s): %s%s"
                          % (len(nwrote), nwrote[:6], " ..." if len(nwrote) > 6 else ""))
        if nnote:
            # Writing binds without node offsets is the WORST of the three states: the skin
            # gets re-attached to a hierarchy that still has the original proportions, so the
            # character is stretched back to full size. Refuse rather than ship that.
            probe, _ = write_bind_matrices(nlg_pack.Archive(source_dict), armature, hn)
            if probe:
                raise RuntimeError(
                    "Cannot rebuild the node table (%s), but %d bone(s) have moved.\n"
                    "Writing bind joint positions without node offsets stretches the character back "
                    "to its original proportions -- that is the bug you just hit.\n"
                    "Fix: select the armature and run 'PO: Recover Bone Node IDs' (F3), point "
                    "it at the ORIGINAL %s, then export again. No re-import needed.\n"
                    "Or untick 'Write bind joint positions (skeleton)' to export geometry only."
                    % (nnote, len(probe), os.path.basename(source_dict)))
            report.append("node table untouched (%s); no bones moved, so nothing to write" % nnote)
        old_bind = bind_matrices(a)     # BEFORE the rewrite, for the models we do not rebuild
        wrote, miss = write_bind_matrices(a, armature, hn)
        if wrote:
            report.append("bind joint positions updated for %d bone(s): %s%s"
                          % (len(wrote), wrote[:6], " ..." if len(wrote) > 6 else ""))
        if miss:
            report.append("WARNING: %d BoneData bone(s) have no matching Blender bone "
                          "(left at their original bind): %s%s"
                          % (len(miss), miss[:6], " ..." if len(miss) > 6 else ""))

        # Models the exporter does NOT rebuild are skinned to the same bones and still hold the
        # ORIGINAL character's vertices. The referee's model 1 is his shadow volume; left alone
        # it stays full size around a shrunken body and renders as black wedges through the cap
        # and shoulders. Push it through the same change of bind pose.
        if wrote:
            rm, rv = rebind_extra_models(a, nlg_geom, old_bind, bind_matrices(a))
            if rm:
                report.append("re-posed %d mesh(es) / %d vertices in the models this export does "
                              "not rebuild (shadow volume) to follow the new skeleton"
                              % (rm, rv))

        # The hip height the game plays back lives in the ANIMATION, not the skeleton, so
        # lowering the hips in Blender leaves every animation still commanding the ORIGINAL
        # height and the character hangs above the canvas. Move the root motion with the rig.
        root_after = node_offset(a, root_node)
        if root_before is not None and root_after is not None:
            d = tuple(root_after[k] - root_before[k] for k in range(3))
            ntr, nan = retarget_root_motion(a, d)
            if ntr:
                report.append("root motion retargeted by (%.4f, %.4f, %.4f) across %d/%d "
                              "animation(s) -- keeps the feet on the canvas" % (d + (ntr, nan)))

    if stats["dropped_groups"]:
        dg = sorted(stats["dropped_groups"])
        report.append(f"DROPPED {len(dg)} non-skinning vertex group(s): "
                      f"{dg[:12]}{' ...' if len(dg) > 12 else ''}")
    if stats["fallback_verts"]:
        report.append(f"WARNING: {stats['fallback_verts']} vertex(es) had NO valid bone weight "
                      f"after cleanup -> snapped to pelvis. Clean these weights in Blender.")

    # The colour bake writes ONE flat colour into a slot's ramp. The material export writes real
    # textures. Both are on by default and the bake runs FIRST, so on any slot the material
    # exporter then treats as pristine the flat colour is what ships -- a textured cap left as
    # solid white. Hand those slots to the material exporter and bake only the rest.
    bake_skip = stats["material_slots"] if materials_follow else set()
    if bake_colors and bake_skip:
        report.append("colour bake skipped %d slot(s) whose material export writes real "
                      "textures: %s" % (len(bake_skip), sorted(bake_skip)[:12]))
    # Untouched imported materials are never baked: there is no new colour to put in, and a
    # flat tint over their detail/spec/ramp textures wiped skin, eyes and teeth to one grey.
    bake_skip = bake_skip | (stats["untouched_slots"] - stats["material_slots"])
    if bake_colors and stats["image_materials"] and not materials_follow:
        im = sorted(stats["image_materials"])
        report.append(f"WARNING: {len(im)} material(s) are painted with an image but the colour "
                      f"bake can only write ONE flat colour per slot: {im[:8]}"
                      f"{' ...' if len(im) > 8 else ''}. They will export as a solid colour. "
                      f"Tick 'Export materials + textures' so the image itself is written.")

    bake_slots = [s for s in buckets if s not in bake_skip]
    if bake_colors and bake_slots:
        textbl = texture_table(a)
        tdc = a.find_chunks(type_id=0xB603)
        if tdc:
            # ---- un-share color textures used by >1 active slot ----
            # Each active slot needs independent color inputs. Keep generic white/black helpers
            # shared, because recoloring those would affect every material that references them.
            NEUTRAL_KEEP = {"white", "black"}   # by short name: leave these shared, they're generic
            share = {}
            for slot in bake_slots:
                for th in set(material_color_textures(a, slot)):
                    if th in textbl:
                        share.setdefault(th, set()).add(slot)
            for th, users in list(share.items()):
                if len(users) <= 1:
                    continue
                short = hn.get(th, "").split("/")[-1]
                if short in NEUTRAL_KEEP:
                    continue
                # keep original for users[0]; duplicate for the rest
                for extra in sorted(users)[1:]:
                    newh = duplicate_texture(a, nlg_hash, th)
                    if newh is not None:
                        repoint_material_textures(a, extra, th, newh)
                        report.append(f"un-shared '{short}': slot {extra} gets its own copy")
            textbl = texture_table(a)   # refresh after additions

            td = bytearray(a.get_chunk_bytes(tdc[0]))
            # Now recolor: each active slot's color textures are uniquely owned (after un-sharing).
            tex_users = {}   # tex_hash -> set of active slots
            for slot in bake_slots:
                for th in material_color_textures(a, slot):
                    if th in textbl:
                        tex_users.setdefault(th, set()).add(slot)

            baked = 0
            for th, users in tex_users.items():
                if len(users) != 1:
                    report.append(f"skip still-shared texture {hn.get(th, hex(th))} (slots {sorted(users)})")
                    continue
                slot = next(iter(users))
                w, h, doff, _size, levels, fmt = textbl[th]
                cl = slot_color.get(slot, (0.8, 0.8, 0.8))
                cb = tuple(int(round(_srgb(c) * 255)) for c in cl)

                def _tint(rgba, cb=cb):
                    out = bytearray(len(rgba))
                    for i in range(0, len(rgba), 4):
                        lum = (0.299*rgba[i] + 0.587*rgba[i+1] + 0.114*rgba[i+2]) / 255.0
                        f = shade_floor + (1.0 - shade_floor) * lum
                        out[i]   = min(255, int(cb[0] * f))
                        out[i+1] = min(255, int(cb[1] * f))
                        out[i+2] = min(255, int(cb[2] * f))
                        out[i+3] = rgba[i+3]
                    return bytes(out)

                # every mip level, not just the base -- see texture_table()
                if not recolor_chain(td, doff, w, h, levels, _tint, fmt):
                    report.append(f"skip '{hn.get(th, hex(th))}': no encoder for its texture format")
                    continue
                report.append(f"slot {slot}: baked {cb} into '{hn.get(th, hex(th))}' "
                              f"({w}x{h}, {levels} mip level(s))")
                baked += 1

            # desaturate the lighting/reflection maps used by active slots so they stop tinting
            # the recolored albedo warm (the purple-skin / red-metallic-glove cast)
            if neutralize_lighting:
                light = set()
                for slot in buckets:
                    for th in material_lighting_textures(a, slot):
                        if th in textbl:
                            light.add(th)
                # DESATURATE to grey, keep the gradient STRUCTURE. These are toon-shading
                # ramps; a solid black ramp collapses shading into quantization bands (stripes).
                # Grey keeps the shading, removes the warm color cast.
                def _grey(rgba):
                    out = bytearray(len(rgba))
                    for i in range(0, len(rgba), 4):
                        y = int(0.299*rgba[i] + 0.587*rgba[i+1] + 0.114*rgba[i+2])
                        out[i] = out[i+1] = out[i+2] = y
                        out[i+3] = rgba[i+3]
                    return bytes(out)

                for th in light:
                    w, h, doff, _size, levels, fmt = textbl[th]
                    recolor_chain(td, doff, w, h, levels, _grey, fmt)
                report.append(f"desaturated {len(light)} lighting map(s): "
                              f"{sorted(hn.get(t, hex(t)).split('/')[-1] for t in light)}")

            a.replace_chunk(tdc[0], bytes(td))
            report.append(f"baked {baked} texture(s) total")

            # Repoint external lighting inputs (for example global/specramp) to global/black.
            # Unlike an arbitrary local fallback this is present for every character and cleanly
            # disables lighting that the archive cannot desaturate in place.
            if neutralize_lighting:
                neutral = nlg_hash.string_to_hash("global/black")
                matcs = a.find_chunks(type_id=0xB016)[0]
                matbuf = bytearray(a.get_chunk_bytes(matcs))
                meshdata2 = a.get_chunk_bytes(a.find_chunks(type_id=0xB004)[0])
                repointed = 0
                for slot in buckets:
                    mo = struct.unpack_from(">I", meshdata2, slot * 52 + 36)[0]
                    for k in MATERIAL_LIGHTING_SLOTS:
                        if mo + k + 4 > len(matbuf):
                            continue
                        h = struct.unpack_from(">I", matbuf, mo + k)[0]
                        if h not in textbl:   # external map we cannot rewrite in this archive
                            struct.pack_into(">I", matbuf, mo + k, neutral)
                            repointed += 1
                a.replace_chunk(matcs, bytes(matbuf))
                report.append("repointed %d external lighting ref(s) -> global/black"
                              " (disables their color cast)" % repointed)

    if not out_dict.endswith(".dict"):
        out_dict += ".dict"
    os.makedirs(os.path.dirname(out_dict) or ".", exist_ok=True)
    a.write(out_dict, out_dict[:-5] + ".data")
    report.append(f"Wrote {out_dict} / .data")
    for line in report:
        print("[PO export]", line)
    return report


# ===========================================================================
# operators + menu
# ===========================================================================
class ExportPunchOut(bpy.types.Operator, ExportHelper):
    bl_idname = "export_scene.punchout_mod"
    bl_label = "Export Punch-Out!! Mod"
    filename_ext = ".dict"
    filter_glob: StringProperty(default="*.dict", options={"HIDDEN"})

    source_dict: StringProperty(
        name="Source character",
        description="Original .dict whose skeleton/animations/slots you're reusing "
                    "(filled in from the imported fighter)",
        subtype="FILE_PATH", default="")
    bake_colors: BoolProperty(
        name="Bake material colors into ramps", default=True,
        description="FLAT COLOUR ONLY -- it recolors a slot's ramp, it cannot carry a painted "
                    "image. Slots whose material has real textures are left to 'Export materials "
                    "+ textures' below. Recolor each slot's ramp texture from its Base Color, "
                    "keeping the original cel-shading gradient. No PNGs needed.")
    shade_floor: FloatProperty(
        name="Shading floor", default=1.0, min=0.0, max=1.0,
        description="1.0 = flat solid color (recommended; the game lights the model itself). "
                    "Lower bakes the source ramp's gradient in, which can show as stripes on gradient "
                    "base textures.")
    neutralize_lighting: BoolProperty(
        name="Neutralize lighting maps", default=True,
        description="Desaturate the rim/HDR/fresnel/spec maps so they cast neutral light. "
                    "Removes the warm color cast (purple skin) and the red metallic sheen on gloves.")
    write_skeleton: BoolProperty(
        name="Write bind joint positions (skeleton)", default=True,
        description="Push REST joint positions into BoneData. Required if you reshaped the rig "
                    "-- without it the game skins your mesh to the original joints and limbs "
                    "blow out into spikes. Game animation axes stay intact to prevent twists.")
    allow_new_textures: BoolProperty(
        name="Allow new textures", default=True,
        description="Off = the archive gains NO texture entries. A texture that would have to be added (shared with another material, or a size CMPR cannot rewrite in place) is left at the source version instead. Use this to rule textures out when the game hangs")
    export_materials: BoolProperty(
        name="Export materials + textures", default=True,
        description="Also write the material records and any texture you edited, chained onto "
                    "the file this export just produced. Turn off for geometry only.")

    @staticmethod
    def _source_from_scene(context):
        """Find the imported/last-used base without repeated path entry."""
        candidates = [context.scene.get("po_export_source_dict")]
        active = context.active_object
        if active is not None:
            candidates.append(active.get("po_source_dict"))
            if active.type == "MESH":
                candidates.append(active.parent.get("po_source_dict") if active.parent else None)
                candidates.extend(mod.object.get("po_source_dict") for mod in active.modifiers
                                  if mod.type == "ARMATURE" and mod.object is not None)
        for obj in bpy.data.objects:
            if obj.type in {"ARMATURE", "MESH"}:
                candidates.append(obj.get("po_source_dict"))
        for path in candidates:
            if path:
                path = bpy.path.abspath(path)
                if os.path.isfile(path) and path.lower().endswith(".dict"):
                    return path
        return ""

    def invoke(self, context, event):
        # Import stamps po_source_dict on both rig and mesh.  Pull it into the export dialog
        # automatically; a manually chosen base is remembered in the .blend for next time.
        suggested = self._source_from_scene(context)
        if suggested:
            self.source_dict = suggested
        return ExportHelper.invoke(self, context, event)

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "source_dict")
        if self.source_dict and os.path.isfile(bpy.path.abspath(self.source_dict)):
            col.label(text="Base ready: %s" % os.path.basename(self.source_dict), icon="CHECKMARK")
        else:
            col.label(text="Import a character to fill this automatically", icon="INFO")
        col.prop(self, "bake_colors")
        sub = col.column(); sub.enabled = self.bake_colors
        sub.prop(self, "shade_floor")
        sub.prop(self, "neutralize_lighting")
        col.separator()
        col.prop(self, "write_skeleton")
        col.separator()
        col.prop(self, "export_materials")
        sub2 = col.column(); sub2.enabled = self.export_materials
        sub2.prop(self, "allow_new_textures")

    def execute(self, context):
        if not self.source_dict:
            self.source_dict = self._source_from_scene(context)
        if not self.source_dict or not os.path.exists(bpy.path.abspath(self.source_dict)):
            self.report({"ERROR"}, "Import a character first, or choose a base character .dict.")
            return {"CANCELLED"}
        self.source_dict = bpy.path.abspath(self.source_dict)
        context.scene["po_export_source_dict"] = self.source_dict
        try:
            report = do_export(bpy.path.abspath(self.source_dict), self.filepath,
                               self.bake_colors, self.shade_floor, self.neutralize_lighting,
                               self.write_skeleton, materials_follow=self.export_materials)
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}

        if self.export_materials:
            try:
                mesh_objs = find_mesh_objects(find_armature())
                # An author can split a fighter into separate shirt/cap/body objects. Export
                # only materials assigned to faces, so stale unused slots cannot overwrite a
                # newly authored material that deliberately claims the same game slot.
                seen, materials = set(), []
                for mesh_obj in mesh_objs:
                    used = {p.material_index for p in mesh_obj.data.polygons}
                    for i, mat in enumerate(mesh_obj.data.materials):
                        if i in used and mat is not None and id(mat) not in seen:
                            seen.add(id(mat)); materials.append(mat)
                if not materials:
                    raise RuntimeError("no face-used material found on meshes attached to the armature")
                # Chain onto the file we just WROTE, never the source .dict. Running this
                # against the source would rebuild the archive from the original and throw
                # away the geometry written a few lines above.
                m = po_export_materials(materials, self.filepath,
                                        allow_new_textures=self.allow_new_textures)
                line = ("materials %d written; textures +%d new, %d forked, %d overwritten, %d left alone"
                        % (m["materials"], len(m["textures_added"]),
                           len(m["textures_forked"]), len(m["textures_overwritten"]),
                           len(m["textures_skipped"])))
                report.append(line)
                for f in m["textures_forked"]:
                    print("PunchOut: forked", f)
            except Exception as e:
                # geometry is already on disk and valid -- say so rather than implying total failure
                self.report({"WARNING"},
                            "Geometry written OK, but materials failed: %s" % e)
                return {"FINISHED"}

        self.report({"INFO"}, report[-1])
        return {"FINISHED"}


class ListPunchOutSlots(bpy.types.Operator):
    bl_idname = "export_scene.punchout_list_slots"
    bl_label = "List Punch-Out!! Source Slots"
    bl_description = "Print the source character's mesh slots (index, name, verts, ramp) to the console"

    source_dict: StringProperty(name="Source character", subtype="FILE_PATH", default="")

    def execute(self, context):
        p = bpy.path.abspath(self.source_dict)
        if not p or not os.path.exists(p):
            self.report({"ERROR"}, "Set a valid source .dict first.")
            return {"CANCELLED"}
        nlg_pack, nlg_geom, nlg_hash, _nlg_anim2 = _load_tools()
        hashid = _find_hashid(p)
        hn = nlg_hash.load_hashid_bin(hashid) if hashid else {}
        a = nlg_pack.Archive(p)
        slots = source_slot_table(a, hn)
        print(f"\n=== {os.path.basename(p)}: {len(slots)} mesh slots ===")
        for mi, (name, vc, _rh, ramp) in slots.items():
            print(f"  slot {mi:2d}  {name:28s} verts={vc:5d}  ramp={ramp}")
        print("=== name a Blender material 'slotN' to target one ===\n")
        self.report({"INFO"}, f"Printed {len(slots)} slots to the system console.")
        return {"FINISHED"}


def menu_func(self, context):
    self.layout.operator(ExportPunchOut.bl_idname, text="Punch-Out!! Mod (.dict)")


def register():
    bpy.utils.register_class(ExportPunchOut)
    bpy.utils.register_class(ListPunchOutSlots)
    bpy.utils.register_class(PO_OT_claim_slot)
    bpy.utils.register_class(PO_OT_make_custom_material)
    bpy.utils.register_class(PO_OT_recover_slots)
    bpy.utils.register_class(PO_OT_recover_nodes)
    bpy.types.TOPBAR_MT_file_export.append(menu_func)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_func)
    bpy.utils.unregister_class(PO_OT_recover_nodes)
    bpy.utils.unregister_class(PO_OT_recover_slots)
    bpy.utils.unregister_class(PO_OT_make_custom_material)
    bpy.utils.unregister_class(PO_OT_claim_slot)
    bpy.utils.unregister_class(ListPunchOutSlots)
    bpy.utils.unregister_class(ExportPunchOut)


# ===========================================================================
# MATERIAL EXPORT  (ramps, detail maps, tints -> 0xB016 + textures)
# ---------------------------------------------------------------------------
# Separate from the geometry/slot path above. The governing rule is DO NOT TOUCH WHAT WAS NOT
# EDITED: every material carries the original 204B record and a fingerprint of each ramp/image
# from import, so anything unchanged is written back verbatim. Without that, a round-trip could
# never be byte-identical -- a repainted ramp cannot survive a 24-stop ColorRamp resample plus a
# re-encode through our own CMPR compressor, and the 204B record has ~15 u32s we still can't name.
# ===========================================================================
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import po_shader
importlib.reload(po_shader)


def po_recover_nodes(armature, dict_path, verbose=True):
    """Stamp po_node / po_rest_head / po_rest_quat onto an armature imported before those existed.

    No re-import needed: all of it is derivable from the source archive. The node table gives
    the node order and names, and the bind pose is recomputed exactly the way the importer
    built it -- BoneData world bind for nodes that own a skinning bone, FK of the local offsets
    for helper nodes. Bone names are matched using the same dedupe rule the importer used.

    The rest pose recorded is the ORIGINAL one from the archive, which is precisely the
    reference the exporter needs to diff your edits against.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.append(here)
    import nlg_anim2
    importlib.reload(nlg_anim2)
    import nlg_hash
    hashid, searched = nlg_hash.find_hashid_bin(dict_path)
    if hashid is None:
        raise RuntimeError(nlg_hash.missing_hashid_message(dict_path, searched))
    rig = nlg_anim2.Rig(dict_path, hashid)
    nn = rig.nn

    used, bname = set(), []                       # same unique-naming rule as the importer
    for n in range(nn):
        nm = rig.names[n] or ("node%d" % n)
        base, k = nm, 1
        while nm in used:
            nm = "%s.%03d" % (base, k); k += 1
        used.add(nm); bname.append(nm)

    order = sorted(range(nn), key=rig._depth)     # FK fallback for helper nodes
    fkp = [None] * nn
    for n in order:
        p = rig.par[n]
        if p < 0:
            fkp[n] = rig.loff[n]
        else:
            off = nlg_anim2.qrot(rig.wq_bind[p], rig.loff[n])
            fkp[n] = tuple(fkp[p][k] + off[k] for k in range(3))

    stamped, missing = [], []
    for n in range(nn):
        b = armature.data.bones.get(bname[n])
        if b is None:
            missing.append(bname[n])
            continue
        bo = rig.node2bone[n]
        pos = rig.bone_wp[bo] if bo is not None else fkp[n]
        q4 = rig.bone_wq[bo] if bo is not None else rig.wq_bind[n]     # (x, y, z, w)
        b["po_node"] = n
        b["po_rest_head"] = [pos[0], pos[1], pos[2]]
        b["po_rest_quat"] = [q4[3], q4[0], q4[1], q4[2]]               # w, x, y, z
        stamped.append(bname[n])

    if verbose:
        print("PunchOut: stamped %d/%d bones with node ids + original rest pose." % (len(stamped), nn))
        if missing:
            print("  no Blender bone for %d node(s) (renamed or deleted?): %s%s"
                  % (len(missing), missing[:8], " ..." if len(missing) > 8 else ""))
    return {"stamped": stamped, "missing": missing}


class PO_OT_recover_nodes(bpy.types.Operator):
    """Recover bone node ids + original rest pose from the source .dict, without re-importing"""
    bl_idname = "po.recover_nodes"
    bl_label = "PO: Recover Bone Node IDs"
    bl_options = {"REGISTER", "UNDO"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.dict", options={"HIDDEN"})

    def execute(self, context):
        arm = context.active_object
        if arm is None or arm.type != "ARMATURE":
            arm = find_armature() if context.active_object else None
        if arm is None or arm.type != "ARMATURE":
            self.report({"ERROR"}, "Select the armature")
            return {"CANCELLED"}
        path = self.filepath or arm.get("po_source_dict") or ""
        if not path or not os.path.exists(path):
            self.report({"ERROR"}, "Pick the ORIGINAL source .dict (not your modified copy)")
            return {"CANCELLED"}
        r = po_recover_nodes(arm, path)
        msg = "stamped %d bones" % len(r["stamped"])
        if r["missing"]:
            msg += "; %d node(s) unmatched" % len(r["missing"])
        self.report({"WARNING"} if r["missing"] else {"INFO"}, msg)
        return {"FINISHED"}

    def invoke(self, context, event):
        arm = context.active_object
        src = arm.get("po_source_dict") if arm else None
        if src and os.path.exists(src):
            self.filepath = src
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


def po_recover_slots(obj, dict_path, verbose=True):
    """Re-derive 'po_slot' for every material on `obj` from the record it was imported from.

    This does not depend on face order, so it works after joined meshes, deleted faces, or added
    geometry. Each imported material carries the 'po_matoff' stamped by the importer, and a
    materialOffset is 1:1 with the mesh slot in the shipped characters tested.

    Materials you authored from scratch have no po_matoff and are listed as unresolved; give
    those a slot with po_claim_slot().
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.append(here)
    import nlg_pack
    importlib.reload(nlg_pack)

    import nlg_hash as _nh
    a = nlg_pack.Archive(dict_path)
    meshdata = a.get_chunk_bytes(a.find_chunks(type_id=0xB004)[0])
    matcs = a.find_chunks(type_id=0xB016)
    matdata = a.get_chunk_bytes(matcs[0]) if matcs else b""
    hashid = _find_hashid(dict_path)
    hn = _nh.load_hashid_bin(hashid) if hashid else {}
    off2slot = {}
    for i in range(len(meshdata) // 52):
        off2slot.setdefault(struct.unpack_from(">I", meshdata, i * 52 + 36)[0], i)

    # slot -> the fingerprint key dirty_slots() guards it with
    _RAMP_FP = {3: "po_fp_ramp", 4: "po_fp_rim", 6: "po_fp_fres"}

    def _restore_ramp_fps(mat, mo):
        """Re-stamp the ramp fingerprints an untouched material had at import.

        Without them every ramp slot reads as dirty and gets re-derived from a 24-stop resample,
        which can come back near-flat -- and a flat lighting ramp bands the shading into stripes.
        With them, only a ramp you actually edited differs and gets written."""
        n = 0
        for slot, key in _RAMP_FP.items():
            if key in mat.keys():
                continue
            if mo + slot * 8 + 4 > len(matdata):
                continue
            h = struct.unpack_from(">I", matdata, mo + slot * 8)[0]
            try:
                mat[key] = po_shader.expected_ramp_fp(a, h)
                n += 1
            except Exception:
                pass
        return n

    def _restore_tex_names(mat, mo):
        """Put back the 8 texture-slot hashes/names the old po_claim_slot used to delete.

        Without them the exporter has no name to overwrite, so even a texture private to this
        material gets written as a NEW archive entry instead of being rewritten in place."""
        n = 0
        for i in range(8):
            if mo + i * 8 + 4 > len(matdata):
                break
            h = struct.unpack_from(">I", matdata, mo + i * 8)[0]
            if ("po_slot_hash%d" % i) not in mat.keys():
                po_shader.set_slot_hash(mat, i, h); n += 1
            if ("po_tex_slot%d" % i) not in mat.keys():
                mat["po_tex_slot%d" % i] = hn.get(h, "#%08x" % h)
        return n

    tagged, unresolved, kept, restored = [], [], [], []
    for mat in obj.data.materials:
        if mat is None:
            continue
        mo = mat.get("po_matoff")
        if mo is not None and mo in off2slot:
            _n = _restore_tex_names(mat, mo) + _restore_ramp_fps(mat, mo)
            if _n:
                restored.append(mat.name)
        if "po_slot" in mat.keys():
            kept.append((mat["po_slot"], mat.name))
            continue
        if mo is None or mo not in off2slot:
            unresolved.append(mat.name)
            continue
        mat["po_slot"] = off2slot[mo]
        tagged.append((off2slot[mo], mat.name))

    if verbose:
        print("PunchOut: recovered %d slot(s) from po_matoff, %d already set."
              % (len(tagged), len(kept)))
        if restored:
            print("  restored texture-slot names on %d material(s): %s" % (len(restored), restored[:6]))
        for s, nm in sorted(tagged):
            print("    slot %-3d %s" % (s, nm))
        if unresolved:
            print("  NO SLOT (author-made, use po_claim_slot): %s" % ", ".join(unresolved))
    return {"tagged": tagged, "kept": kept, "unresolved": unresolved}


class PO_OT_recover_slots(bpy.types.Operator):
    """Re-derive every material's mesh slot from the imported record. Safe on an edited scene"""
    bl_idname = "po.recover_slots"
    bl_label = "PO: Recover Material Slots"
    bl_options = {"REGISTER", "UNDO"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH",
                                       description="The source character .dict")
    filter_glob: bpy.props.StringProperty(default="*.dict", options={"HIDDEN"})

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.type != "MESH":
            self.report({"ERROR"}, "Select the character mesh")
            return {"CANCELLED"}
        path = self.filepath or obj.get("po_source_dict") or ""
        if not path or not os.path.exists(path):
            self.report({"ERROR"}, "Could not find the source .dict -- pick it manually")
            return {"CANCELLED"}
        r = po_recover_slots(obj, path)
        msg = "tagged %d, kept %d" % (len(r["tagged"]), len(r["kept"]))
        if r["unresolved"]:
            msg += "; NO SLOT: %s" % ", ".join(r["unresolved"])
        self.report({"WARNING"} if r["unresolved"] else {"INFO"}, msg)
        return {"FINISHED"}

    def invoke(self, context, event):
        obj = context.active_object
        # the importer stamps this, so the common case needs no file picker at all
        src = obj.get("po_source_dict") if obj else None
        if src and os.path.exists(src):
            self.filepath = src
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


def po_claim_slot(mat, slot):
    """Hand a mesh slot to `mat` and cut every tie to the material it was copied from.

    Use this when you want a material to be ITS OWN THING. All it has to do is stamp
    mat['po_slot'] so the geometry exporter accepts it by any name.

    It deliberately does NOT strip the inherited po_tex_slot* / po_slot_hash* names any more.
    That was wrong twice over: _put() already forks a texture whenever it is shared with
    another material, so ownership is handled archive-side where the sharing is actually
    known -- and dropping the names meant even a PRIVATE texture (like a slot's own ramp) had
    nothing to overwrite, so every claim added a brand new texture to the archive for no
    reason. Keeping the names means private textures are rewritten in place and only genuinely
    shared ones fork.

    Fingerprints are left alone too, so dirty_slots() still decides what gets re-baked and an
    untouched slot is not needlessly re-encoded. The 204-byte po_record stays as the template,
    which is what carries the ~15 u32s nobody has identified yet.
    """
    mat["po_slot"] = int(slot)
    print("PunchOut: '%s' now owns slot %d (textures: private ones rewritten in place, "
          "shared ones forked automatically)." % (mat.name, slot))
    return mat


def _linked_source(socket, limit=8):
    """Return the non-reroute node feeding `socket`, or None."""
    if socket is None or not socket.is_linked:
        return None
    node = socket.links[0].from_node
    while getattr(node, "type", "") == "REROUTE" and limit:
        if not node.inputs or not node.inputs[0].is_linked:
            return None
        node = node.inputs[0].links[0].from_node
        limit -= 1
    return node


def diffuse_image_from_material(mat):
    """Find the image that drives a normal Blender material's Principled Base Color.

    A lone image node is accepted as a practical fallback, but several unconnected images are
    ambiguous and must be wired explicitly by the artist.
    """
    nt = getattr(mat, "node_tree", None)
    if nt is None:
        return None
    for node in nt.nodes:
        if getattr(node, "type", "") != "BSDF_PRINCIPLED":
            continue
        src = _linked_source(node.inputs.get("Base Color"))
        if getattr(src, "type", "") == "TEX_IMAGE" and getattr(src, "image", None) is not None:
            return src.image
    images = [n.image for n in nt.nodes if getattr(n, "type", "") == "TEX_IMAGE"
              and getattr(n, "image", None) is not None]
    return images[0] if len(images) == 1 else None


def make_custom_po_material(source_mat, name, preset, slot, white_ramp=True):
    """Build an independent game material from a conventional Blender material.

    The diffuse image becomes game texture slot 0, rather than remaining on Principled Base
    Color where the exporter cannot serialize it. The game still applies its normal arena
    lighting, specular response, and rim reflection through `hippodiffuseskin`.
    """
    image = diffuse_image_from_material(source_mat)
    if image is None:
        raise RuntimeError(
            "The active material has no unambiguous diffuse image. Connect one Image Texture "
            "node to Principled Base Color, then run 'PO: Make Custom Game Material' again.")
    try:
        image.colorspace_settings.name = "Non-Color"
    except Exception:
        pass
    mat = po_shader.po_new_material(name, preset=preset, detail=image)
    if white_ramp:
        ramp = mat.node_tree.nodes.get("PO_Ramp")
        if ramp is not None:
            for element in ramp.color_ramp.elements:
                element.color = (1.0, 1.0, 1.0, 1.0)
    po_claim_slot(mat, slot)
    # po_new_material stamps its initial state. The deliberately-white ramp is an authored
    # edit, so po_export_materials will serialize both ramp and detail map.
    return mat


class PO_OT_make_custom_material(bpy.types.Operator):
    """Replace the active Blender material with a standalone game material and diffuse texture."""
    bl_idname = "po.make_custom_material"
    bl_label = "PO: Make Custom Game Material"
    bl_options = {"REGISTER", "UNDO"}

    slot: IntProperty(name="Target slot", default=0, min=0,
                      description="Mesh slot this material owns in the source character")
    material_name: StringProperty(name="Material name", default="PO_Custom")
    preset: EnumProperty(
        name="Surface response",
        items=[(key, key.title(), "Game-like %s surface response" % key)
               for key in ("skin", "cloth", "hair", "glove", "metal", "boot", "flat")],
        default="cloth")
    white_ramp: BoolProperty(
        name="Use diffuse image as painted",
        default=True,
        description="Write a white colour ramp so the diffuse image is not darkened by the source slot")

    def execute(self, context):
        obj = context.active_object
        old = obj.active_material if obj and obj.type == "MESH" else None
        if old is None:
            self.report({"ERROR"}, "Select a mesh with the diffuse material active")
            return {"CANCELLED"}
        try:
            mat = make_custom_po_material(old, self.material_name.strip() or "PO_Custom",
                                          self.preset, self.slot, self.white_ramp)
        except Exception as ex:
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        # Faces retain their assignments, so one action converts a whole cap/shirt/skin material.
        replaced = 0
        for i, candidate in enumerate(obj.data.materials):
            if candidate == old:
                obj.data.materials[i] = mat
                replaced += 1
        self.report({"INFO"}, "Created '%s' for slot %d from '%s' (%d slot(s) replaced)"
                    % (mat.name, self.slot, old.name, replaced))
        return {"FINISHED"}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)


class PO_OT_claim_slot(bpy.types.Operator):
    """Give the active material a mesh slot of its own, with its own textures"""
    bl_idname = "po.claim_slot"
    bl_label = "PO: Claim Slot for Active Material"
    bl_options = {"REGISTER", "UNDO"}

    slot: bpy.props.IntProperty(name="Slot", default=0, min=0,
                                description="Mesh slot index in the source character "
                                            "(use List Source Slots to find it)")

    def execute(self, context):
        obj = context.active_object
        mat = obj.active_material if obj else None
        if mat is None:
            self.report({"ERROR"}, "No active material")
            return {"CANCELLED"}
        po_claim_slot(mat, self.slot)
        self.report({"INFO"}, "'%s' owns slot %d; textures will be written fresh"
                    % (mat.name, self.slot))
        return {"FINISHED"}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)


PO_SLOT_ROLE = {0: "detail", 1: "damage", 2: "specmask", 3: "ramp", 4: "rimramp",
                5: "hdr", 6: "fresnel", 7: "specramp"}
PO_NODE_NAMES = {"PO_Detail", "PO_Damage", "PO_SpecMask", "PO_Hdr", "PO_Ramp", "PO_RimRamp", "PO_Fresnel", "PO_SpecRamp",
                 "PO_BSDF", "PO_Tint", "PO_RampPos", "PO_AlbedoGain", "PO_AlbedoLift"}


def resolve_po_nodes(mat):
    """Map PO texture slots -> shader nodes WITHOUT depending on node names.

    Names were the only route before, and that is fragile in a way that fails SILENTLY:
    Blender shows the image datablock name in a texture node's header, so you cannot see a
    node's name at a glance, and dropping in your own image node (or renaming one) made the
    exporter find nothing and skip your texture. It looked perfect in Blender and never
    reached the game.

    The graph's SHAPE is what actually defines each slot's role, so use that:
      - albedo is  ramp x detail  -> the one multiply fed by a ColorRamp and an image node
        identifies slot 3 (ramp) and slot 0 (detail) together;
      - the rim ramp (slot 4) is the ColorRamp driven by the geometry/dot-product chain;
      - a leftover ColorRamp is fresnel (slot 6), a leftover image node is the HDR map (slot 5).

    Names are still tried first, so existing materials take the fast path unchanged.
    Returns {slot: node}.
    """
    nt = getattr(mat, "node_tree", None)
    if nt is None:
        return {}
    out = {}
    for nm, slot in (("PO_Detail", 0), ("PO_Damage", 1), ("PO_SpecMask", 2),
                     ("PO_Ramp", 3), ("PO_RimRamp", 4), ("PO_Hdr", 5),
                     ("PO_Fresnel", 6), ("PO_SpecRamp", 7)):
        nd = nt.nodes.get(nm)
        if nd is not None:
            out[slot] = nd

    def src(node, idx):
        try:
            inp = node.inputs[idx]
        except Exception:
            return None
        if not inp.is_linked:
            return None
        n = inp.links[0].from_node
        seen = 0
        while getattr(n, "type", "") == "REROUTE" and seen < 8:
            if not n.inputs[0].is_linked:
                return None
            n = n.inputs[0].links[0].from_node; seen += 1
        return n

    nodes = list(nt.nodes)
    MIXES = ("VECT_MATH", "MIX_RGB", "MIX")

    # ramp x detail -> slots 3 and 0
    for n in nodes:
        if getattr(n, "type", "") not in MIXES:
            continue
        a, b = src(n, 0), src(n, 1)
        ta, tb = getattr(a, "type", None), getattr(b, "type", None)
        if {ta, tb} == {"VALTORGB", "TEX_IMAGE"}:
            out.setdefault(3, a if ta == "VALTORGB" else b)
            out.setdefault(0, a if ta == "TEX_IMAGE" else b)
            break

    # The exact-source LUT previews are viewport helpers, not texture slots. Left in the pool,
    # the last-image fallback below adopted PO_RampTexture as the HDR map (slot 5) on every
    # material without one, and the 512x1 LUT then crashed the CMPR encoder.
    preview_only = {"PO_RampTexture", "PO_SpecRampTexture"}
    ramps = [n for n in nodes if getattr(n, "type", "") == "VALTORGB"]
    imgs = [n for n in nodes if getattr(n, "type", "") == "TEX_IMAGE"
            and getattr(n, "image", None) is not None and n.name not in preview_only]

    def driven_by_geometry(rampnode, depth=6):
        """rim ramp's Fac comes off the view/normal dot-product chain"""
        seen, stack = set(), [src(rampnode, 0)]
        while stack and depth:
            n = stack.pop(); depth -= 1
            if n is None or id(n) in seen:
                continue
            seen.add(id(n))
            if getattr(n, "type", "") in ("NEW_GEOMETRY", "GEOMETRY", "FRESNEL", "LAYER_WEIGHT"):
                return True
            for i in range(len(getattr(n, "inputs", []))):
                stack.append(src(n, i))
        return False

    if 3 not in out and len(ramps) == 1:
        out[3] = ramps[0]
    left = [r for r in ramps if r not in out.values()]
    if 4 not in out:
        geo = [r for r in left if driven_by_geometry(r)]
        if len(geo) == 1:
            out[4] = geo[0]
        elif len(left) == 1:
            out[4] = left[0]
    left = [r for r in ramps if r not in out.values()]
    if 6 not in out and len(left) == 1:
        out[6] = left[0]

    if 0 not in out and len(imgs) == 1:
        out[0] = imgs[0]
    left_i = [i for i in imgs if i not in out.values()]
    if 5 not in out and len(left_i) == 1:
        out[5] = left_i[0]
    return out


def _mean_luma(px, n):
    if not n:
        return 1.0
    return sum(0.299 * px[i * 4] + 0.587 * px[i * 4 + 1] + 0.114 * px[i * 4 + 2]
               for i in range(n)) / n / 255.0


def inherited_dark_ramps(arch, mesh_to_record, records, hn, written_tex, floor=0.35):
    """Slots that got a NEW detail image sitting on a ramp inherited from the source material.

    Albedo is ramp x detail. A dark ramp with a bright detail map is perfectly normal in shipped
    art -- that is how hair works -- so the pixels alone are not a signal. What IS a signal is a
    detail texture this export added while the ramp stayed whatever the slot's original material
    had. That is how a red cap mapped onto `ref_sole` renders near-black: the ramp is a shoe
    sole's, mean luminance 0.24, and nothing in the pipeline says so.
    """
    import nlg_texture          # loaded lazily, same as everywhere else in this module
    try:
        tdc = arch.find_chunks(type_id=0xB603)
        if not tdc:
            return []
        td = arch.get_chunk_bytes(tdc[0])
        ts = {t.hash: t for t in nlg_texture.list_textures(arch, hn)}
    except Exception:
        return []

    def luma(h):
        e = ts.get(h)
        if e is None:
            return None
        try:
            px = nlg_texture.decode_cmpr(td[e.data_offset:e.data_offset + e.size],
                                         e.width, e.height)
        except Exception:
            return None
        return _mean_luma(px, e.width * e.height)

    out = []
    for mi, ri in sorted(mesh_to_record.items()):
        if ri >= len(records):
            continue
        rec = records[ri]
        dh = struct.unpack_from(">I", rec, 0)[0]
        rh = struct.unpack_from(">I", rec, 3 * 8)[0]
        # Only when WE wrote the detail and did NOT write the ramp. A dark ramp under shipped art
        # is intentional; a dark ramp under art this export just added is an accident.
        if dh not in written_tex or rh in written_tex:
            continue
        dl, rl = luma(dh), luma(rh)
        if dl is None or rl is None or rl >= floor:
            continue
        out.append("slot %d: detail '%s' (luma %.2f) x ramp '%s' (luma %.2f) -> about %.2f"
                   % (mi, hn.get(dh, "%08X" % dh), dl,
                      hn.get(rh, "%08X" % rh), rl, dl * rl))
    return out


def po_export_materials(obj, dict_path, out_path=None, prefix=None, ramp_size=(128, 8),
                        force=False, allow_new_textures=True):
    """Write the object's PO materials back into a character archive.

    Untouched materials are written back byte-for-byte from the record captured at import;
    only what you actually edited is re-derived. Pass force=True to re-derive everything
    (useful to see exactly how lossy the bake is). Returns a summary dict.
    """
    # The normal UI passes a list collected from every mesh attached to the armature. Keep the
    # original single-object API for scripts and the headless round-trip suite.
    material_list = (list(obj.data.materials) if hasattr(obj, "data") and hasattr(obj.data, "materials")
                     else list(obj))
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.append(here)
    import nlg_material, nlg_texture, nlg_hash, nlg_geom
    for mod in (nlg_material, nlg_texture, nlg_hash, nlg_geom):
        importlib.reload(mod)
    importlib.reload(po_shader)

    # dict_path is usually the fresh external export; names come from the imported source's dump.
    hashid = _find_hashid(dict_path) or _find_hashid(bpy.path.abspath(
        bpy.context.scene.get("po_export_source_dict", "") or dict_path)) or ""
    M = nlg_material.Materials(dict_path, hashid)
    arch = M.a
    rev = {v: k for k, v in M.hn.items()}
    by_hash = {e.hash: e for e in nlg_texture.list_textures(arch)}
    prefix = prefix or os.path.splitext(os.path.basename(dict_path))[0]
    rw, rh = ramp_size
    overwritten, added, rebuilt, pristine, forked, skipped = [], [], [], [], [], []
    written_tex = set()     # texture hashes this export actually wrote pixels into
    adopted, ignored, why_dirty = [], [], []
    PO_NODE_NAMES = {"PO_Detail", "PO_Damage", "PO_SpecMask", "PO_Hdr", "PO_Ramp", "PO_RimRamp", "PO_Fresnel", "PO_SpecRamp",
                     "PO_BSDF", "PO_Tint", "PO_RampPos", "PO_AlbedoGain", "PO_AlbedoLift"}

    # ---- which textures does more than one material record point at? ----
    # The shipped characters lean hard on shared neutrals: on the referee, ALL 34 slots
    # reference the same 32x32 'white'. Overwriting one of those in place repaints every
    # material that uses it. Nobody has ever wanted that, so _put() forks instead.
    def _shared_texture_hashes():
        try:
            meshdata = arch.get_chunk_bytes(M.mesh_ri)
            mcs = arch.find_chunks(type_id=0xB016)
            matdata = arch.get_chunk_bytes(mcs[0]) if mcs else b""
        except Exception:
            return set()
        owners = {}
        offs = {struct.unpack_from(">I", meshdata, i * 52 + 36)[0]
                for i in range(len(meshdata) // 52)}
        for mo in offs:
            for s in range(8):
                if mo + s * 8 + 4 <= len(matdata):
                    h = struct.unpack_from(">I", matdata, mo + s * 8)[0]
                    owners.setdefault(h, set()).add(mo)
        return {h for h, who in owners.items() if len(who) > 1}

    shared_tex = _shared_texture_hashes()

    def _uniq(full):
        if full not in rev:
            return full
        i = 2
        while "%s_%d" % (full, i) in rev:
            i += 1
        return "%s_%d" % (full, i)

    def _put(name, make, want_w, want_h, resizable, fork_name=None):
        """`make(w, h)` renders the pixels at whatever size is required.

        Overwrites the archive texture in place when that is safe. It is NOT safe when the
        texture is shared with another material, or when CMPR cannot hold the new dimensions
        (it is fixed-size per w/h). In either case we write a NEW texture and point this
        material at it -- your image goes in at your size and belongs to you alone."""
        full = name if "/" in name else "%s/%s" % (prefix, name)
        hsh = rev.get(full)
        ent = by_hash.get(hsh) if hsh is not None else None
        if ent is not None:
            why = None
            if ent.hash in shared_tex:
                why = "shared by several materials"
            elif (ent.width, ent.height) != (want_w, want_h) and not resizable:
                why = ("archive holds %dx%d, yours is %dx%d"
                       % (ent.width, ent.height, want_w, want_h))
            if why is None:
                if (ent.width, ent.height) != (want_w, want_h):
                    want_w, want_h = ent.width, ent.height      # resizable: re-render to fit
                ri, data = nlg_texture.replace_texture_rgba(arch, ent, make(want_w, want_h))
                arch.replace_chunk(ri, data)
                overwritten.append("%s (%dx%d)" % (full, want_w, want_h))
                written_tex.add(ent.hash)
                return ent.hash
            if not allow_new_textures:
                # Diagnostic mode: guarantee the archive gains no texture entries at all.
                # Leave this slot exactly as the source had it and say so.
                skipped.append("%s (%s)" % (full.split("/")[-1], why))
                return ent.hash
            # fork rather than repaint someone else's texture (or refuse outright)
            base = fork_name or (name + "_own")
            new_full = _uniq(base if "/" in base else "%s/%s" % (prefix, base))
            forked.append("%s -> %s (%s)" % (full.split("/")[-1],
                                             new_full.split("/")[-1], why))
            full = new_full
        if not allow_new_textures and ent is None and hsh is None:
            # a texture that does not exist yet cannot be written without adding an entry
            skipped.append("%s (would be a new entry)" % full.split("/")[-1])
            return 0
        if want_w % 8 or want_h % 8:
            want_w = ((want_w + 7) // 8) * 8; want_h = ((want_h + 7) // 8) * 8
        h2 = nlg_texture.add_texture_rgba(arch, full, make(want_w, want_h), want_w, want_h)
        by_hash.update({e.hash: e for e in nlg_texture.list_textures(arch)})
        rev[full] = h2
        added.append("%s (%dx%d)" % (full, want_w, want_h))
        written_tex.add(h2)
        return h2

    records, order, names_by_rec = [], {}, {}
    for si, mat in enumerate(material_list):
        if is_decoration_material(mat):
            continue        # PO_Outline et al: viewport only, no record belongs in the archive
        if not force and po_shader.material_is_pristine(mat):
            records.append(bytes.fromhex(mat["po_record"]))     # verbatim, unknown bytes intact
            pristine.append(mat.name)
        else:
            # Slots are resolved from the GRAPH, not from node names -- see resolve_po_nodes().
            # This MUST come before dirty_slots(), which needs the resolved nodes.
            resolved = resolve_po_nodes(mat)
            for _sl, _nd in resolved.items():
                _want = {0: 'PO_Detail', 1: 'PO_Damage', 2: 'PO_SpecMask', 3: 'PO_Ramp',
                         4: 'PO_RimRamp', 5: 'PO_Hdr', 6: 'PO_Fresnel', 7: 'PO_SpecRamp'}[_sl]
                if _nd.name != _want:
                    adopted.append("%s: '%s' -> slot %d (%s)"
                                   % (mat.name, _nd.name, _sl, PO_SLOT_ROLE[_sl]))
                    _nd.name = _want          # make it stick for next time
                    if getattr(_nd, 'type', '') == 'TEX_IMAGE':
                        try:
                            _nd.interpolation = 'Closest'
                            _nd.image.colorspace_settings.name = 'Non-Color'
                        except Exception:
                            pass
            _unmapped = [n.image.name for n in (mat.node_tree.nodes if mat.node_tree else [])
                         if getattr(n, 'type', '') == 'TEX_IMAGE'
                         and getattr(n, 'image', None) is not None
                         and n.name not in ("PO_RampTexture", "PO_SpecRampTexture")
                         and n not in resolved.values()]
            if _unmapped:
                ignored.append('%s: image node(s) %s are wired to no PO slot -- NOT exported'
                               % (mat.name, _unmapped[:3]))
            _why = []
            dirty = (po_shader.dirty_slots(mat, resolved, _why) if mat.get("po_record")
                     else set(range(8)))
            if _why:
                why_dirty.append((mat.name, list(_why)))
            names = {i: mat.get("po_tex_slot%d" % i) for i in range(8)}
            slots = [po_shader.get_slot_hash(mat, i) for i in range(8)]
            base = mat.name.replace(" ", "_")

            def _bake(slot, suffix, is_img):
                # only touch what changed -- see po_shader.dirty_slots
                if slot not in dirty:
                    return
                nd = resolved.get(slot)
                if nd is None or (is_img and getattr(nd, 'image', None) is None):
                    return
                nm = names.get(slot) or (base + suffix)
                if str(nm).startswith("#"):
                    nm = base + suffix
                if is_img:
                    rgba, w, h = po_shader.image_to_rgba(nd.image)
                    slots[slot] = _put(nm, lambda _w, _h: rgba, w, h, resizable=False,
                                       fork_name=base + suffix)
                else:
                    slots[slot] = _put(nm, lambda w, h: po_shader.ramp_to_rgba(nd, w, h), rw, rh,
                                       resizable=True, fork_name=base + suffix)

            _bake(3, "_ramp", False)
            _bake(0, "_detail", True)
            _bake(1, "_damage", True)
            _bake(2, "_specmask", True)
            _bake(4, "_rim", False)
            _bake(5, "_hdr", True)
            _bake(6, "_fresnel", False)
            _bake(7, "_specramp", False)
            t = mat.node_tree.nodes.get("PO_Tint") if mat.node_tree else None
            tint = (tuple(t.inputs[i].default_value for i in range(3)) if t is not None
                    else tuple(mat.get("po_tint", (1.0, 1.0, 1.0))))
            tmpl = bytes.fromhex(mat["po_record"]) if mat.get("po_record") else None
            records.append(M.build_record(slots,
                                          spec_power=float(mat.get("po_spec_power", 32.0)),
                                          tint=tint,
                                          alpha=float(mat.get("po_alpha", 1.0)),
                                          template=tmpl))
            rebuilt.append(mat.name)
        order[si] = len(records) - 1
        nm = mat.get("po_mesh_material") or mat.name
        names_by_rec[order[si]] = nm if "/" in nm else "%s_mats/%s" % (prefix, nm)

    # ---- mesh -> record ----
    # Drive this from po_slot, NOT from face order. The positional route below walks the
    # ORIGINAL archive face layout (po_mesh_tris) against the CURRENT polygons, so the moment
    # you add or delete a face every mesh after that point picks up the wrong record -- and
    # the .get(..., 0) default made the failure silent, quietly pointing meshes at record 0.
    # A mesh whose materialOffset lands mid-record is what hangs the game on load.
    mesh_to_record = {}
    for si, mat in enumerate(material_list):
        if is_decoration_material(mat) or si not in order:
            continue
        slot = mat.get("po_slot")
        if slot is not None:
            slot = int(slot)
            prior = mesh_to_record.get(slot)
            if prior is not None and prior != order[si]:
                raise RuntimeError(
                    "Two face-used materials claim mesh slot %d ('%s' and another material). "
                    "Give each visible material its own target slot; one game mesh slot can "
                    "only point at one material record." % (slot, mat.name))
            mesh_to_record[slot] = order[si]

    if not mesh_to_record:
        if not (hasattr(obj, "data") and hasattr(obj.data, "polygons")):
            raise RuntimeError("No face-used material has a 'po_slot'. Run 'PO: Make Custom Game "
                               "Material' or 'PO: Claim Slot for Active Material' first.")
        # legacy scene with no po_slot anywhere: fall back to the old positional walk, but
        # only when the mesh still matches the archive exactly.
        tris = list(obj.get("po_mesh_tris") or [])
        if not tris:
            tris = [len(m.tris) for m in nlg_geom.read_model(arch, M.hn)]
        polys = obj.data.polygons
        if sum(tris) != len(polys):
            raise RuntimeError(
                "No material carries 'po_slot', and '%s' has %d faces where the archive layout "
                "expects %d -- so meshes cannot be matched to material records. Run "
                "'PO: Recover Material Slots' and export again."
                % (obj.name, len(polys), sum(tris)))
        f = 0
        for mi, n in enumerate(tris):
            if f < len(polys):
                mesh_to_record[mi] = order.get(polys[f].material_index, 0)
            f += n

    # ---- carry over any mesh slot we did not map ----
    # write_all() replaces the ENTIRE 0xB016 chunk but leaves unmapped meshes holding their
    # original materialOffset. Once the record array is a different size that offset is a
    # dangling pointer -- it can land mid-record or past the end, which is a load-time hang,
    # not a visible glitch. So copy those records across verbatim and repoint them.
    orig_mat = arch.get_chunk_bytes(M.mat_ri)
    orig_mesh = arch.get_chunk_bytes(M.mesh_ri)
    rec_index = {}
    for i, r in enumerate(records):
        rec_index.setdefault(bytes(r), i)
    carried = []
    for mi in range(len(orig_mesh) // 52):
        if mi in mesh_to_record:
            continue
        mo = struct.unpack_from(">I", orig_mesh, mi * 52 + 36)[0]
        blob = bytes(orig_mat[mo:mo + 204])
        if len(blob) != 204:
            raise RuntimeError("mesh slot %d points at a truncated material record (@0x%x); "
                               "the source archive looks damaged." % (mi, mo))
        if blob not in rec_index:
            records.append(blob)
            rec_index[blob] = len(records) - 1
        mesh_to_record[mi] = rec_index[blob]
        carried.append(mi)
    if carried:
        print("PunchOut: carried %d untouched material record(s) across: slots %s"
              % (len(carried), carried))

    # every mesh must now resolve, or the archive ships with a dangling materialOffset
    nmesh_total = len(orig_mesh) // 52
    missing = [mi for mi in range(nmesh_total) if mi not in mesh_to_record]
    if missing:
        raise RuntimeError("mesh slots %s have no material record; refusing to write an "
                           "archive with dangling materialOffsets." % missing)

    # material NAME goes at +20. +16 is the SHADER name and must stay 'hippodiffuseskin'.
    mdrec = bytearray(arch.get_chunk_bytes(M.mesh_ri))
    for mi, ri in mesh_to_record.items():
        if ri in names_by_rec:
            struct.pack_into(">I", mdrec, mi * 52 + 20, nlg_hash.string_to_hash(names_by_rec[ri]))
    arch.replace_chunk(M.mesh_ri, bytes(mdrec))

    M.mat = bytearray(arch.get_chunk_bytes(M.mat_ri))
    M.write_all(records, mesh_to_record)
    arch.write(out_path or dict_path)
    if why_dirty:
        print("PunchOut: what each material considered changed:")
        for _nm, _rs in why_dirty:
            print("    %s" % _nm)
            for _r in _rs:
                print("       %s" % _r)
    if adopted:
        print("PunchOut: adopted %d hand-added image node(s) as PO_Detail:" % len(adopted))
        for ad in adopted:
            print("   ", ad)
    if ignored:
        print("PunchOut: IGNORED image node(s) -- these did NOT reach the archive:")
        for ig in ignored:
            print("   ", ig)
    if skipped:
        print("PunchOut: NO-NEW-TEXTURES mode, left %d texture(s) at the source version:" % len(skipped))
        for sk in skipped:
            print("   ", sk)
    if forked:
        print("PunchOut: gave %d texture(s) their own copy instead of overwriting a shared one:"
              % len(forked))
        for f in forked:
            print("   ", f)
    dark = inherited_dark_ramps(arch, mesh_to_record, records, M.hn, written_tex)
    if dark:
        print("PunchOut: albedo is RAMP x DETAIL, and these slots got a new detail image on top "
              "of a ramp they inherited from the source material:")
        for line in dark:
            print("   ", line)
        print("    -> the image is in the archive but the ramp multiplies it down. Lighten that "
              "material's PO_Ramp (white = show the image as painted).")

    return {"materials": len(records), "meshes": len(mesh_to_record),
            "pristine": pristine, "rebuilt": rebuilt,
            "textures_overwritten": overwritten, "textures_added": added,
            "textures_forked": forked, "textures_skipped": skipped,
            "nodes_adopted": adopted, "nodes_ignored": ignored,
            "dark_ramps": dark}


if __name__ == "__main__":
    register()
