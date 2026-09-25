bl_info = {
    "name": "Punch-Out!! Wii Importer",
    "author": "Bryan Intindola",
    "version": (1, 0),
    "blender": (3, 0, 0),
    "location": "File > Import > Punch-Out!! Asset (.dict), mode Fighter, combined",
    "description": "Import a Punch-Out!! Wii fighter asset: combined mesh, skeleton, and animations.",
    "category": "Import-Export",
}

import bpy, sys, os, struct, math, json
from bpy.props import StringProperty, BoolProperty, EnumProperty, IntProperty
from bpy_extras.io_utils import ImportHelper
from mathutils import Vector, Quaternion, Matrix

# the shader itself lives in po_shader.py so the exporter can share the exact same definition
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import po_shader
import importlib as _il
_il.reload(po_shader)
from po_shader import (KEY_LIGHT_DIR, PO_RAMP_POS, PO_ALBEDO_GAIN, PO_ALBEDO_LIFT, PO_GLOW,
                       PO_AMBIENT, SHOW_DAMAGE, DAMAGE_NAME_HINTS,
                       PO_PRESETS, RIM_DEFAULT, FRESNEL_DEFAULT,
                       po_build_shader, po_new_material, build_light_rig,
                       po_set_ramp_pos, po_set_albedo_gain, po_set_albedo_lift, po_show_damage)

# The PO Tools panel drives ReshapeRig / WeightMesh. These MUST be imported at module scope --
# the operator classes below reference them as globals, so a function-local import inside
# do_import() leaves them undefined and every button raises NameError.
try:
    import anim_retarget
    _il.reload(anim_retarget)
    from anim_retarget import ReshapeRig, WeightMesh
    _RETARGET_ERR = None
except Exception as _ex:            # older installs may not ship anim_retarget.py
    ReshapeRig = WeightMesh = None
    _RETARGET_ERR = str(_ex)


# Retail's DefaultOutlineColour tweak is (0.13, 0.13, 0.13), an encoded display value.
OUTLINE_COLOUR = 0.13


def _outline_linear(raw):
    """Emission value that displays as OUTLINE_COLOUR: the encoded value itself under the Raw
    view transform, its exact sRGB-decoded value under the Standard view."""
    c = OUTLINE_COLOUR
    return c if raw else (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)


def _set_outline_colour(mat, raw):
    node = mat.node_tree.nodes.get("PO_OutlineColour") if mat.node_tree else None
    if node is not None:
        v = _outline_linear(raw)
        node.inputs["Color"].default_value = (v, v, v, 1)


def _outline_material():
    """One shared dark-grey inverted-hull material for fighters and cutscene props.

    OUTLINE_COLOUR is an encoded display value. The fighter importer shows the scene through
    Blender's Standard view, so the emission holds its sRGB-decoded value; the exact-GX cutscene
    preview renders Raw and switches it back with outline_view_raw()."""
    mat = next((m for m in bpy.data.materials
                if m.get("po_outline") and m.name.split(".")[0] == "PO_Outline"), None)
    if mat is not None:
        return mat
    mat = bpy.data.materials.new("PO_Outline"); mat.use_nodes = True
    mat["po_outline"] = True
    nt = mat.node_tree; nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    emission = nt.nodes.new("ShaderNodeEmission"); emission.name = "PO_OutlineColour"
    nt.links.new(emission.outputs["Emission"], out.inputs["Surface"])
    _set_outline_colour(mat, bpy.context.scene.view_settings.view_transform == "Raw")
    try: mat.use_backface_culling = True
    except Exception: pass
    return mat


def outline_view_raw(raw=True):
    """Keep outline colours right after the scene's view transform changes to Raw (or back)."""
    for mat in bpy.data.materials:
        if mat.get("po_outline"):
            _set_outline_colour(mat, raw)


# Retail draws its outline as a roughly 1.5-2.5 px edge line at 1080p; measured against the
# retail DK pre-fight video, a 0.0075 shell was about 4.4 px in close-ups, so 0.004 matches.
OUTLINE_THICKNESS = -0.004


def add_outline(obj, thickness=OUTLINE_THICKNESS):
    """Add the same editable inverted hull used by the fighter importer.

    Keeping this public lets NIS props and accessories receive the game's silhouette pass
    without duplicating its material/modifier policy.
    """
    if obj.type != "MESH" or any(m.type == "SOLIDIFY" and m.name.startswith("Outline")
                                  for m in obj.modifiers):
        return
    outline = _outline_material()
    if outline.name not in obj.data.materials:
        obj.data.materials.append(outline)
    try:
        weld = obj.modifiers.new("OutlineWeld", "WELD")
        weld.merge_threshold = 0.0002
    except Exception:
        pass
    solidify = obj.modifiers.new("Outline", "SOLIDIFY")
    solidify.thickness = thickness
    solidify.use_flip_normals = True
    solidify.use_quality_normals = True
    solidify.material_offset = 1000
    solidify.use_rim = False



# --- PIL-free CMPR texture decode (nlg_texture imports Pillow, which Blender lacks) ---
def _rgb565(c):
    r = (c >> 11) & 0x1F; g = (c >> 5) & 0x3F; b = c & 0x1F
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def _decode_cmpr(data, w, h):
    """GameCube CMPR (DXT1) -> RGBA bytearray (w*h*4), top-down."""
    out = bytearray(w * h * 4); off = 0
    bw = (w + 7) // 8; bh = (h + 7) // 8
    for by in range(bh):
        for bx in range(bw):
            for sub in range(4):
                sx = (sub & 1) * 4; sy = (sub >> 1) * 4
                if off + 8 > len(data):
                    return out
                c0 = struct.unpack_from(">H", data, off)[0]; c1 = struct.unpack_from(">H", data, off + 2)[0]
                idx = data[off + 4:off + 8]; off += 8
                r0, g0, b0 = _rgb565(c0); r1, g1, b1 = _rgb565(c1)
                pal = [(r0, g0, b0, 255), (r1, g1, b1, 255)]
                if c0 > c1:
                    pal.append(((2*r0+r1)//3, (2*g0+g1)//3, (2*b0+b1)//3, 255))
                    pal.append(((r0+2*r1)//3, (g0+2*g1)//3, (b0+2*b1)//3, 255))
                else:
                    pal.append(((r0+r1)//2, (g0+g1)//2, (b0+b1)//2, 255))
                    pal.append((0, 0, 0, 0))
                for py in range(4):
                    row = idx[py]
                    for px in range(4):
                        p = (row >> (6 - px*2)) & 3
                        X = bx*8 + sx + px; Y = by*8 + sy + py
                        if X < w and Y < h:
                            o = (Y*w + X) * 4; out[o:o+4] = bytes(pal[p])
    return out


def _texture_table(archive):
    """hash -> (w, h, fmt, dataOffset, cmpr_size).  All PO character textures are CMPR (fmt 6)."""
    th = archive.find_chunks(type_id=0xB601)
    if not th:
        return {}
    hdr = archive.get_chunk_bytes(th[0]); out = {}
    for i in range(len(hdr) // 96):
        o = i * 96
        hh = struct.unpack_from(">I", hdr, o)[0]
        w = struct.unpack_from(">H", hdr, o + 4)[0]; h = struct.unpack_from(">H", hdr, o + 6)[0]
        fmt = hdr[o + 13]; doff = struct.unpack_from(">I", hdr, o + 20)[0]
        out[hh] = (w, h, fmt, doff, ((w + 7)//8) * ((h + 7)//8) * 32)
    return out


def _find_tools(dict_path):
    """The decode modules live in potools/formats; the archive's folder is irrelevant."""
    formats = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "formats")
    return formats if os.path.exists(os.path.join(formats, "nlg_anim2.py")) else None


def _find_hashid(dict_path):
    """Nearest hashid.bin in the archive's folder or any parent; no folder name is assumed."""
    import nlg_hash
    return nlg_hash.find_hashid_bin(dict_path)[0]


def do_import(dict_path, do_anims=True, anim_filter="", do_textures=True, do_outline=True):
    tools = _find_tools(dict_path)
    if not tools:
        raise RuntimeError("The Punch-Out!! format modules (nlg_anim2.py and friends) are missing from potools/formats (looked from %s)."
                           % os.path.abspath(__file__))
    if tools not in sys.path:
        sys.path.append(tools)
    import importlib
    import nlg_pack, nlg_geom, nlg_hash, nlg_anim2
    for m in (nlg_pack, nlg_geom, nlg_hash, nlg_anim2):
        importlib.reload(m)

    hashid, searched = nlg_hash.find_hashid_bin(dict_path)
    if hashid is None:
        raise RuntimeError(nlg_hash.missing_hashid_message(dict_path, searched))
    rig = nlg_anim2.Rig(dict_path, hashid)
    hn = nlg_hash.load_hashid_bin(hashid)
    meshes = nlg_geom.read_model(rig.a, hn)
    qrot = nlg_anim2.qrot
    nn = rig.nn

    # unique bone names (used for both bones and vertex groups)
    used, bname = set(), []
    for n in range(nn):
        nm = rig.names[n] or f"node{n}"
        base, k = nm, 1
        while nm in used:
            nm = f"{base}.{k:03d}"; k += 1
        used.add(nm); bname.append(nm)

    # ---- node bind WORLD pos/quat ----
    # For nodes with a real bone, use the exact BoneData SKIN bind (matches how the mesh is skinned in
    # the GIFs); this avoids the FK drift that stretches shoulders/hands. Helper nodes (no bone) fall
    # back to FK of the bind-local offsets.
    order = sorted(range(nn), key=rig._depth)
    fkp = [None]*nn
    for n in order:
        p = rig.par[n]
        if p < 0:
            fkp[n] = rig.loff[n]
        else:
            off = qrot(rig.wq_bind[p], rig.loff[n])
            fkp[n] = (fkp[p][0]+off[0], fkp[p][1]+off[1], fkp[p][2]+off[2])
    wp = [None]*nn; wq = [None]*nn
    for n in range(nn):
        b = rig.node2bone[n]
        if b is not None:
            wp[n] = rig.bone_wp[b]        # exact skin-bind position (BoneData)
            wq[n] = rig.bone_wq[b]        # exact skin-bind orientation (BoneData, already transposed)
        else:
            wp[n] = fkp[n]
            wq[n] = rig.wq_bind[n]

    # ---- armature ----
    arm_data = bpy.data.armatures.new("PO_Armature")
    arm_obj = bpy.data.objects.new(os.path.basename(dict_path).replace(".dict", ""), arm_data)
    bpy.context.collection.objects.link(arm_obj)
    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")
    ebnames = []
    for n in range(nn):
        eb = arm_data.edit_bones.new(bname[n])
        q = wq[n]  # (x,y,z,w)
        M = Matrix.Translation(Vector(wp[n])) @ Quaternion((q[3], q[0], q[1], q[2])).to_matrix().to_4x4()
        eb.head = Vector(wp[n]); eb.tail = Vector(wp[n]) + Vector((0, 0.05, 0))
        eb.matrix = M
        eb.length = 0.05
        ebnames.append(eb.name)
    for n in range(nn):
        p = rig.par[n]
        if p >= 0:
            arm_data.edit_bones[ebnames[n]].parent = arm_data.edit_bones[ebnames[p]]
    bpy.ops.object.mode_set(mode="OBJECT")
    # Record which NODE each bone is. Names get deduped (.001) and the node table is indexed,
    # not name-keyed, so the exporter must not have to reverse-engineer this from names.
    for n in range(nn):
        b = arm_data.bones[ebnames[n]]
        b["po_node"] = n
        # Snapshot the rest pose AS IMPORTED. The exporter needs it to apply what you CHANGED
        # rather than recomputing offsets from scratch: the archive's node offsets and its
        # BoneData bind positions genuinely disagree by up to ~1cm on some nodes, so
        # recomputing would quietly deform a rig you never touched.
        q = b.matrix_local.to_quaternion()
        t = b.matrix_local.translation
        b["po_rest_head"] = [t[0], t[1], t[2]]
        b["po_rest_quat"] = [q[0], q[1], q[2], q[3]]      # w, x, y, z

    # ---- mesh (combined) with weights + UVs ----
    hash2node = {h: n for n, h in enumerate(rig.hashes)}
    bh = rig.a.find_chunks(type_id=0xB00B)
    palettes = []
    for ri in bh[:len(meshes)]:
        d = rig.a.get_chunk_bytes(ri)
        palettes.append([struct.unpack_from(">I", d, o)[0] for o in range(0, len(d), 4)])
    verts, faces, guv, guv_damage, guv2 = [], [], [], [], []
    vg_pairs = {}   # node -> [(vidx, weight)]
    base = 0
    for mi, m in enumerate(meshes):
        pal = palettes[mi] if mi < len(palettes) else []
        for p in m.pos:
            verts.append((p[0], p[1], p[2]))
        for vi in range(len(m.pos)):
            u, v = (m.uv[vi] if vi < len(m.uv) else (0.0, 0.0))
            guv.append((u, 1.0 - v))
            duv = getattr(m, "uv_damage", [])
            du, dv = (duv[vi] if vi < len(duv) else (u, v))
            guv_damage.append((du, 1.0 - dv))
            # GX TEX2 = attribute 0x3D: the specular-mask coordinates (skin technique stage 4).
            uv2 = getattr(m, "uv2", [])
            su, sv = (uv2[vi] if vi < len(uv2) else (u, v))
            guv2.append((su, 1.0 - sv))
        for (a, b, c) in m.tris:
            faces.append((base+a, base+b, base+c))
        for vi in range(len(m.pos)):
            bidx = m.bidx[vi] if vi < len(m.bidx) else (0, 0, 0, 0)
            wts = m.bwt[vi] if vi < len(m.bwt) else (1, 0, 0, 0)
            for k in range(4):
                w = wts[k]
                if w <= 0.0001:
                    continue
                pj = bidx[k]
                if pj >= len(pal):
                    continue
                node = hash2node.get(pal[pj])
                if node is None:
                    continue
                vg_pairs.setdefault(node, []).append((base+vi, w))
        base += len(m.pos)

    # Small patches such as the nipples share vertex positions with the skin but are not
    # overlays: each plugs a hole in the skin and shares its border edges (120 of 125 such
    # patches across the roster). They must stay exactly where the archive puts them; any
    # offset tears them off their border and leaves a visible gap.

    md = bpy.data.meshes.new("PO_Mesh")
    md.from_pydata(verts, [], faces)
    md.update()
    uv0 = md.uv_layers.new(name="UV")
    uvl = uv0.data
    damage_uvl = md.uv_layers.new(name="UV_Damage").data
    # GX texture-coordinate sets by index for the exact TEV preview (po_gx): UV = TEX0 (0xCC),
    # UV1 = TEX1 (0x05, same data as UV_Damage) and UV2 = TEX2 (0x3D).
    uv1l = md.uv_layers.new(name="UV1").data
    uv2l = md.uv_layers.new(name="UV2").data
    for loop in md.loops:
        uvl[loop.index].uv = guv[loop.vertex_index]
        damage_uvl[loop.index].uv = guv_damage[loop.vertex_index]
        uv1l[loop.index].uv = guv_damage[loop.vertex_index]
        uv2l[loop.index].uv = guv2[loop.vertex_index]
    try:
        md.uv_layers.active_index = 0
        uv0.active_render = True
    except Exception:
        pass

    # ---- authored vertex normals (attr 0xFE) -> Blender custom split normals ----
    # WHY THIS MATTERS: PO splits vertices at every UV/normal seam (Bear Hugger: 6385 verts,
    # only 3709 unique positions -- 42% are coincident duplicates, 1335 of them UV seams and
    # 394 genuine hard edges). Without the authored normals Blender re-derives them from face
    # geometry, so each side of a seam gets its own averaged normal and you see shading seams
    # and a torn inverted-hull outline. Merge-by-distance "fixes" it only by destroying the
    # seams (and the 394 hard edges with them). Feeding the game's own normals in is the
    # correct fix and needs no geometry edit at all.
    gnrm = []
    for m in meshes:
        for vi in range(len(m.pos)):
            n = m.nrm[vi] if vi < len(m.nrm) else (0.0, 0.0, 1.0)
            ln = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2]) or 1.0
            gnrm.append((n[0] / ln, n[1] / ln, n[2] / ln))
    try:
        md.polygons.foreach_set("use_smooth", [True] * len(md.polygons))
        if hasattr(md, "use_auto_smooth"):        # Blender < 4.1 gate for custom normals
            md.use_auto_smooth = True
        md.normals_split_custom_set_from_vertices(gnrm)
        md.update()
    except Exception as ex:
        print("PunchOut: custom split normals skipped:", ex)

    obj = bpy.data.objects.new("PO_Character", md)
    bpy.context.collection.objects.link(obj)
    for node, pairs in vg_pairs.items():
        vg = obj.vertex_groups.new(name=bname[node])
        for vidx, w in pairs:
            vg.add([vidx], w, "REPLACE")
    obj.parent = arm_obj
    mod = obj.modifiers.new("Armature", "ARMATURE")
    mod.object = arm_obj
    # the geometry exporter maps materials onto archive MESH SLOTS, so record how the faces
    # were laid out; each imported material is stamped with its exact source slot below.
    obj["po_mesh_tris"] = [len(m.tris) for m in meshes]
    obj["po_source_dict"] = dict_path

    # ---- facial morphs (0xB00C) -> shape keys ----
    # One key per stored shape. 2-breakpoint channels (in-betweens, e.g. blink) become
    # morphNN_50 / morphNN_100; single-shape channels become morphNN. Local vertex indices are
    # offset into the combined mesh by each source mesh's base.
    morph = None
    try:
        import nlg_morph
        importlib.reload(nlg_morph)
        morph = nlg_morph.Morphs(dict_path, hashid)
    except Exception as ex:
        print("PunchOut: no morph data:", ex)
    if morph is not None and any(morph.channel_deltas(c) for c in range(morph.B)):
        bases = []
        acc = 0
        for m in meshes:
            bases.append(acc); acc += len(m.pos)
        obj.shape_key_add(name="Basis", from_mix=False)
        sk_names = {}   # (ch, shapeIdx) -> key name
        for ch in range(morph.B):
            per = morph.channel_deltas(ch)
            if not per:
                continue
            for si, nm2 in enumerate(morph.key_names(ch)):
                key = obj.shape_key_add(name=nm2, from_mix=False)
                key.slider_min, key.slider_max = 0.0, 1.0
                for mi, s, recs in per:
                    if s != si or mi >= len(bases):
                        continue
                    b0 = bases[mi]
                    for idx, dx, dy, dz in recs:
                        v = key.data[b0 + idx]
                        v.co = (v.co[0] + dx, v.co[1] + dy, v.co[2] + dz)
                sk_names[(ch, si)] = key.name
        print(f"PunchOut: {len(sk_names)} shape keys from {morph.B} morph channels")
        # Hurt shapes are static states the clips never drive; the Normal/Hurt toggle owns them.
        hurt = [name for ch in morph.hurt_channels() for name in morph.key_names(ch)
                if obj.data.shape_keys.key_blocks.get(name) is not None]
        obj.data.shape_keys["po_hurt_keys"] = json.dumps(hurt)
        _apply_hurt_keys(obj, bpy.context.scene.po_material_state == "HURT")
        print(f"PunchOut: hurt shape keys: {', '.join(hurt) or 'none'}")

    # ---- materials ----
    # Built by po_shader.build_materials_from_archive so the importer and the exporter share
    # ONE definition. It also stamps each material with its original 204B record, its slot
    # hashes and a fingerprint of every ramp/image -- that is what lets the exporter write
    # untouched materials back byte-identically instead of re-deriving them.
    if do_textures:
        def _mkimg(h, nm, raw, w, ht):
            img = bpy.data.images.new(nm or ("%08x" % h), w, ht, alpha=True)
            try:
                import numpy as _np
                arr = _np.frombuffer(bytes(raw), dtype=_np.uint8).astype(_np.float32) / 255.0
                arr = _np.ascontiguousarray(arr.reshape(ht, w, 4)[::-1]).ravel()  # Blender is bottom-up
                img.pixels.foreach_set(arr)
            except Exception:
                px = [0.0] * (w * ht * 4)
                for y in range(ht):
                    for x in range(w):
                        sO = ((ht - 1 - y) * w + x) * 4; d = (y * w + x) * 4
                        for k in range(4):
                            px[d + k] = raw[sO + k] / 255.0
                img.pixels[:] = px
            img.update(); img.pack()
            return img

        mats, mesh_to_mat = po_shader.build_materials_from_archive(rig.a, hn, _mkimg)
        added = []
        for m in mats:
            obj.data.materials.append(m); added.append(m)
        face_idx = []
        for mi, mm in enumerate(meshes):
            idx = added.index(mesh_to_mat[mi]) if mi in mesh_to_mat else 0
            for _ in mm.tris:
                face_idx.append(idx)
        for i, poly in enumerate(obj.data.polygons):
            if i < len(face_idx):
                poly.material_index = face_idx[i]
        print("PunchOut: %d materials over %d meshes" % (len(mats), len(meshes)))

        # Damage-named records are required replacement pieces of the surface (cheek, lip,
        # torso), not optional overlay geometry. Hiding them punches literal holes in Normal.
        # Keep every record visible and let PO_DamageMix bypass/enable the slot-1 artwork.
        for mat in mats:
            mat["po_material_state"] = ("HURT" if mat.get("po_damage_mesh") else "NORMAL")
        # Some archives put a hurt record in slot 0. Start the material editor on the first
        # normal material so the default Normal preview and the active edit target agree.
        for i, mat in enumerate(obj.data.materials):
            if mat is not None and not mat.get("po_damage_mesh") and not mat.get("po_outline"):
                obj.active_material_index = i
                break


        # signature Punch-Out black outline (inverted hull). Optional; tweak thickness if inverted.
        if do_outline:
            try:
                # Weld FIRST. Solidify pushes the shell along per-vertex normals; at a split
                # vertex the copies have different normals and tear the hull at UV seams.
                add_outline(obj)
            except Exception as ex:
                print("PunchOut: outline step skipped:", ex)

    # ---- animations -> actions (NLA strips) ----
    # Actions go through po_action: legacy Action.fcurves before 4.4, slots/channelbags after.
    if do_anims:
        import po_action
        bpy.context.view_layer.objects.active = arm_obj
        for pb in arm_obj.pose.bones:
            pb.rotation_mode = "QUATERNION"
        arm_obj.animation_data_create()
        track = arm_obj.animation_data.nla_tracks.new()
        track.name = "PunchOut Anims"
        names = sorted(rig.anims.keys())
        if anim_filter.strip():
            keys = [s.strip() for s in anim_filter.split(",") if s.strip()]
            names = [a for a in names if any(k in a for k in keys)]

        # bind WORLD matrix per node (rest)
        def wmat(pos, q):
            return Matrix.Translation(Vector(pos)) @ Quaternion((q[3], q[0], q[1], q[2])).to_matrix().to_4x4()
        Bw = [wmat(wp[n], wq[n]) for n in range(nn)]
        Bwi = [m.inverted() for m in Bw]

        pbones = [arm_obj.pose.bones.get(bname[n]) for n in range(nn)]
        strip_start = 0
        strip_starts = {}
        for anim in names:
            strip_starts[anim] = int(strip_start)
            fr, posf, qff = rig.pose(anim)
            act, slot = po_action.new_action(arm_obj, anim)
            for f in range(fr):
                Aw = [wmat(posf[f][n], qff[f][n]) for n in range(nn)]
                for n in range(nn):
                    pb = pbones[n]
                    if pb is None:
                        continue
                    p = rig.par[n]
                    # local basis (order-independent): mb = Bw[n]^-1 . Bw[p] . Aw[p]^-1 . Aw[n]
                    if p < 0:
                        mb = Bwi[n] @ Aw[n]
                    else:
                        mb = Bwi[n] @ Bw[p] @ Aw[p].inverted() @ Aw[n]
                    pb.matrix_basis = mb
                    pb.keyframe_insert("location", frame=f+1)
                    pb.keyframe_insert("rotation_quaternion", frame=f+1)
            po_action.assign_action(arm_obj, None)
            strip = po_action.add_strip(track, anim, strip_start, act, slot)
            strip.name = anim
            strip_start += fr + 5   # bounded by total frame count; never near INT32_MAX

        # ---- facial morph weights -> shape-key actions on a matching NLA track ----
        # Anim weight channel i drives 0xB00C channel i (validated: DK blink ch0-3 = eyelids).
        # In-between pairs: w<=b0 drives shape_50 (w/b0), w>b0 fades _50 down / _100 up.
        if morph is not None and md.shape_keys is not None:
            sk = md.shape_keys
            sk.animation_data_create()
            sk_track = sk.animation_data.nla_tracks.new()
            sk_track.name = "PunchOut Morphs"
            # Hurt channels get no curves: a keyed 0 would pin them and override the toggle.
            static = set(morph.hurt_channels()) - morph.animated_channels()
            for anim in names:
                try:
                    W = morph.anim_weights(anim)
                except Exception:
                    continue
                if not W or not any(any(r) for r in W):
                    continue
                act, slot = po_action.new_action(sk, anim + "_morphs", assign=False)
                for ch in range(min(len(W[0]), morph.B)):
                    per = morph.channel_deltas(ch)
                    if not per or ch in static:
                        continue
                    bp = morph.bp[ch]
                    keys = morph.key_names(ch)
                    curves = []
                    for kn in keys:
                        if sk.key_blocks.get(kn) is None:
                            curves.append(None); continue
                        fc = po_action.new_fcurve(act, f'key_blocks["{kn}"].value', slot=slot)
                        curves.append(fc)
                    for f, row in enumerate(W):
                        w = row[ch]
                        if len(bp) == 1:
                            vals = [w / bp[0]]
                        else:
                            if w <= bp[0]:
                                vals = [w / bp[0], 0.0]
                            else:
                                t = (w - bp[0]) / (bp[1] - bp[0])
                                vals = [1.0 - t, t]
                        for fc, v in zip(curves, vals):
                            if fc is not None:
                                fc.keyframe_points.insert(f + 1, v, options={'FAST'})
                if po_action.has_curves(act):
                    strip = po_action.add_strip(sk_track, anim + "_morphs", strip_starts.get(anim, 0), act, slot)
                    strip.name = anim + "_morphs"
                else:
                    bpy.data.actions.remove(act)

    # remember where this rig came from; the exporter and the PO Tools panel both read it
    arm_obj["po_source_dict"] = dict_path
    # Saved in the .blend and used by the export entry points.
    bpy.context.scene["po_export_source_dict"] = dict_path


    # ---- colour management: the Wii writes straight sRGB with no tonemap. Blender 4.x
    #      defaults to AgX (and 3.x to Filmic), which desaturates and lifts the blacks --
    #      that alone makes an otherwise correct import look muddy. Standard = 1:1. ----
    if do_textures:
        try:
            vs = bpy.context.scene.view_settings
            vs.view_transform = "Standard"
            vs.look = "None"
            bpy.context.scene.display_settings.display_device = "sRGB"
            outline_view_raw(False)
        except Exception as ex:
            print("PunchOut: could not set view transform:", ex)

        # ---- arena light rig. The ramps carry no contrast, so without lamps he reads flat. ----
        if not any(o.name.startswith("PO_TEV_KeyVector") for o in bpy.data.objects):
            try:
                build_light_rig()
            except Exception as ex:
                print("PunchOut: light rig skipped:", ex)

        # Respect the current panel state for materials created after registration.
        if hasattr(bpy.context.scene, "po_show_damage"):
            _po_update_show_damage(None, bpy.context)

    return arm_obj, obj


class ImportPunchOut(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.punchout"
    bl_label = "Import Punch-Out!! Fighter (combined)"
    filename_ext = ".dict"
    filter_glob: StringProperty(default="*.dict", options={"HIDDEN"})
    import_anims: BoolProperty(name="Import Animations", default=True)
    anim_filter: StringProperty(name="Only anims containing",
                                description="Comma-separated substrings; blank = all 38",
                                default="")
    import_textures: BoolProperty(name="Import Textures / Materials", default=True)
    import_outline: BoolProperty(name="Add Toon Outline", default=True,
                                 description="Inverted-hull black outline (the Punch-Out look)")

    def execute(self, context):
        try:
            do_import(self.filepath, self.import_anims, self.anim_filter,
                      self.import_textures, self.import_outline)
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        self.report({"INFO"}, "Imported Punch-Out!! asset (fighter, combined)")
        return {"FINISHED"}


# =============================================================================
# Bone reshaping, weight painting and damage-state operators (panels: io_punchout_ui)
# =============================================================================

def _selected_bone_names(context, arm):
    """Names of the selected bones, whatever mode we are in.

    edit_bones only exists (and is only populated) in Edit mode; everywhere else the
    selection lives on arm.data.bones, which is valid in Pose AND Object mode."""
    if context.mode == "EDIT_ARMATURE":
        return [eb.name for eb in arm.data.edit_bones if eb.select]
    return [b.name for b in arm.data.bones if b.select]


class _POBoneOpMixin:
    """Shared preamble for every bone-reshaping operator.

    ReshapeRig works through armature.data.edit_bones, which is EMPTY outside Edit mode --
    so without this guard the operators silently did nothing and just printed
    "Bone '...' not found" to the console."""

    def _prepare(self, context):
        """Returns (ReshapeRig, [bone names]) or (None, None) after reporting why not."""
        arm = context.active_object
        if not arm or arm.type != "ARMATURE":
            self.report({"ERROR"}, "Select an armature")
            return None, None
        if ReshapeRig is None:
            self.report({"ERROR"}, "anim_retarget.py not available: %s" % _RETARGET_ERR)
            return None, None
        if context.mode != "EDIT_ARMATURE":
            self.report({"ERROR"}, "Enter Edit mode on the armature first (Tab)")
            return None, None
        sel = _selected_bone_names(context, arm)
        if not sel:
            self.report({"ERROR"}, "No bones selected")
            return None, None
        return ReshapeRig(arm), sel


class PO_RETOOL_OT_apply_scale_x(_POBoneOpMixin, bpy.types.Operator):
    bl_idname = "po.retool_scale_x"
    bl_label = "Scale X"
    scale_val: bpy.props.FloatProperty(name="Factor", default=1.2, min=0.1, max=5.0)

    def execute(self, context):
        rr, sel = self._prepare(context)
        if rr is None:
            return {"CANCELLED"}
        for bn in sel:
            rr.scale_bone(bn, self.scale_val, axis='x')
        self.report({"INFO"}, f"Scaled {sel} X by {self.scale_val:.2f}")
        return {"FINISHED"}


class PO_RETOOL_OT_apply_scale_y(_POBoneOpMixin, bpy.types.Operator):
    bl_idname = "po.retool_scale_y"
    bl_label = "Scale Y (Length)"
    scale_val: bpy.props.FloatProperty(name="Factor", default=1.2, min=0.1, max=5.0)

    def execute(self, context):
        rr, sel = self._prepare(context)
        if rr is None:
            return {"CANCELLED"}
        for bn in sel:
            rr.scale_bone(bn, self.scale_val, axis='y')
        self.report({"INFO"}, f"Scaled {sel} Y by {self.scale_val:.2f}")
        return {"FINISHED"}


class PO_RETOOL_OT_apply_move(_POBoneOpMixin, bpy.types.Operator):
    bl_idname = "po.retool_move"
    bl_label = "Move Bone"
    move_x: bpy.props.FloatProperty(name="X", default=0.0)
    move_y: bpy.props.FloatProperty(name="Y", default=0.0)
    move_z: bpy.props.FloatProperty(name="Z", default=0.0)

    def execute(self, context):
        rr, sel = self._prepare(context)
        if rr is None:
            return {"CANCELLED"}
        for bn in sel:
            rr.move_bone(bn, (self.move_x, self.move_y, self.move_z))
        self.report({"INFO"}, f"Moved {sel}")
        return {"FINISHED"}


class PO_RETOOL_OT_apply_rotate(_POBoneOpMixin, bpy.types.Operator):
    bl_idname = "po.retool_rotate"
    bl_label = "Rotate Bone"
    rot_angle: bpy.props.FloatProperty(name="Angle (deg)", default=15.0, min=-180.0, max=180.0)
    rot_axis: bpy.props.EnumProperty(items=[('x', 'X', 'Rotate around X axis'), ('y', 'Y', 'Rotate around Y axis'), ('z', 'Z', 'Rotate around Z axis')])

    def execute(self, context):
        rr, sel = self._prepare(context)
        if rr is None:
            return {"CANCELLED"}
        for bn in sel:
            rr.rotate_bone(bn, self.rot_angle, axis=self.rot_axis)
        self.report({"INFO"}, f"Rotated {sel} {self.rot_axis.upper()} by {self.rot_angle:.1f}°")
        return {"FINISHED"}


class PO_RETOOL_OT_set_length(_POBoneOpMixin, bpy.types.Operator):
    bl_idname = "po.retool_set_length"
    bl_label = "Set Bone Length"
    bone_length: bpy.props.FloatProperty(name="Length", default=0.40, min=0.01, max=10.0)

    def execute(self, context):
        rr, sel = self._prepare(context)
        if rr is None:
            return {"CANCELLED"}
        for bn in sel:
            rr.set_bone_length(bn, self.bone_length)
        self.report({"INFO"}, f"Set {sel} length to {self.bone_length:.2f}")
        return {"FINISHED"}


class PO_RETOOL_OT_stretch_to(_POBoneOpMixin, bpy.types.Operator):
    bl_idname = "po.retool_stretch_to"
    bl_label = "Stretch Bone To"
    target_bone: bpy.props.StringProperty()

    def execute(self, context):
        rr, sel = self._prepare(context)
        if rr is None:
            return {"CANCELLED"}
        for bn in sel:
            rr.stretch_to_bone(bn, self.target_bone)
        self.report({"INFO"}, f"Stretched {sel} to {self.target_bone}")
        return {"FINISHED"}


class PO_RETOOL_OT_uniform_scale(_POBoneOpMixin, bpy.types.Operator):
    bl_idname = "po.retool_uniform_scale"
    bl_label = "Uniform Scale"
    scale_factor: bpy.props.FloatProperty(name="Factor", default=1.0, min=0.1, max=5.0)

    def execute(self, context):
        rr, sel = self._prepare(context)
        if rr is None:
            return {"CANCELLED"}
        for bn in sel:
            rr.uniform_scale(bn, self.scale_factor)
        self.report({"INFO"}, f"Uniform scaled {sel} by {self.scale_factor:.2f}")
        return {"FINISHED"}


class PO_RETOOL_OT_print_report(bpy.types.Operator):
    bl_idname = "po.retool_print_report"
    bl_label = "Print Bone Report"

    def execute(self, context):
        arm = bpy.context.active_object
        if arm and arm.type == "ARMATURE":
            rr = ReshapeRig(arm)
            rr.print_bone_report()
        return {"FINISHED"}


class PO_RETOOL_OT_restore_originals(bpy.types.Operator):
    bl_idname = "po.retool_restore"
    bl_label = "Restore Originals"

    def execute(self, context):
        arm = bpy.context.active_object
        if arm and arm.type == "ARMATURE":
            rr = ReshapeRig(arm)
            rr.restore_originals()
            self.report({"INFO"}, "Restored all bones to original positions")
        return {"FINISHED"}


class PO_RETOOL_OT_auto_weight(bpy.types.Operator):
    bl_idname = "po.retool_auto_weight"
    bl_label = "Auto Weight Mesh"
    method: bpy.props.EnumProperty(
        items=[('CLUSTER_DEFORM', 'Cluster Deform', ''),
               ('RANDOM', 'Random', ''),
               ('EMPTY', 'Empty', '')],
        default='CLUSTER_DEFORM'
    )

    def execute(self, context):
        active = context.active_object
        if not active or active.type != "MESH":
            self.report({"ERROR"}, "Select a mesh object first")
            return {"CANCELLED"}
        if WeightMesh is None:
            self.report({"ERROR"}, "anim_retarget.py not available: %s" % _RETARGET_ERR)
            return {"CANCELLED"}
        armature = None
        for obj in context.selected_objects:
            if obj.type == "ARMATURE":
                armature = obj
                break
        if not armature:
            self.report({"ERROR"}, "Select an armature too")
            return {"CANCELLED"}
        wm = WeightMesh(active, armature)
        wm.auto_weight(self.method)
        self.report({"INFO"}, f"Applied {self.method} weighting")
        return {"FINISHED"}


class PO_RETOOL_OT_transfer_weights(bpy.types.Operator):
    bl_idname = "po.retool_transfer_weights"
    bl_label = "Transfer Weights from Source"

    def execute(self, context):
        # This assumes the source mesh is selected alongside the target
        selected = [o for o in context.selected_objects if o.type == "MESH"]
        if len(selected) < 2:
            self.report({"ERROR"}, "Select source and target meshes")
            return {"CANCELLED"}
        if WeightMesh is None:
            self.report({"ERROR"}, "anim_retarget.py not available: %s" % _RETARGET_ERR)
            return {"CANCELLED"}
        source = selected[0]
        target = selected[1]
        armature = None
        for obj in context.selected_objects:
            if obj.type == "ARMATURE":
                armature = obj
                break
        if not armature:
            self.report({"ERROR"}, "Select an armature too")
            return {"CANCELLED"}
        wm = WeightMesh(target, armature)
        wm.transfer_from_source(source)
        self.report({"INFO"}, "Attempted weight transfer")
        return {"FINISHED"}


def _disable_legacy_hurt_mask(obj):
    """Neutralize the old modifier that removed required cheek/lip/torso geometry."""
    if obj.type != "MESH":
        return False
    mask = obj.modifiers.get("PO Hurt Visibility")
    if mask is None or mask.type != "MASK":
        return False
    mask.show_viewport = False
    mask.show_render = False
    return True


def _ensure_damage_uv_layer(obj):
    """Restore texture coordinate set 1 in an already-open import from its source archive."""
    if obj.type != "MESH" or obj.data.uv_layers.get("UV_Damage") is not None:
        return True
    source = obj.get("po_source_dict")
    if not source:
        return False
    source = bpy.path.abspath(source)
    if not os.path.isfile(source):
        return False
    try:
        import nlg_pack, nlg_geom
        _il.reload(nlg_geom)
        meshes = nlg_geom.read_model(nlg_pack.Archive(source))
        coords = []
        for mesh in meshes:
            uv0 = mesh.uv
            damage_uv = getattr(mesh, "uv_damage", [])
            for i in range(len(mesh.pos)):
                u, v = (damage_uv[i] if i < len(damage_uv)
                        else (uv0[i] if i < len(uv0) else (0.0, 0.0)))
                coords.append((u, 1.0 - v))
        if len(coords) != len(obj.data.vertices):
            print("PunchOut: reopen '%s' to restore damage UVs (topology was edited)" % obj.name)
            return False
        layer = obj.data.uv_layers.new(name="UV_Damage").data
        for loop in obj.data.loops:
            layer[loop.index].uv = coords[loop.vertex_index]
        print("PunchOut: restored damage UVs on '%s' from source archive" % obj.name)
        return True
    except Exception as ex:
        print("PunchOut: could not restore damage UVs on '%s': %s" % (obj.name, ex))
        return False


def _ensure_damage_mix(mat, show):
    """Migrate a direct slot-1 multiply to a white-vs-damage Normal/Hurt switch."""
    if (not mat.get("po_damage_mesh") or not mat.use_nodes or
            mat.node_tree is None):
        return None
    nt = mat.node_tree
    damage = nt.nodes.get("PO_Damage")
    if damage is None:
        # Saved files from before slot 1 was decoded cannot be repaired without reopening the
        # archive, but they must still stay opaque instead of producing holes.
        vis = nt.nodes.get("PO_DamageVis")
        if vis is not None:
            vis.inputs["Fac"].default_value = 1.0
        return None
    damage_uv = nt.nodes.get("PO_DamageUV")
    if damage_uv is None:
        damage_uv = nt.nodes.new("ShaderNodeUVMap")
        damage_uv.name = "PO_DamageUV"; damage_uv.label = "Damage UV (texcoord 1)"
        damage_uv.location = (damage.location.x - 200, damage.location.y)
    damage_uv.uv_map = "UV_Damage"
    vector_input = damage.inputs.get("Vector")
    if vector_input is not None and (not vector_input.is_linked or
            vector_input.links[0].from_node != damage_uv):
        for link in list(vector_input.links):
            nt.links.remove(link)
        nt.links.new(damage_uv.outputs["UV"], vector_input)
    mix = nt.nodes.get("PO_DamageMix")
    if mix is None:
        source = damage.outputs.get("Color")
        outgoing = [link for link in nt.links if link.from_socket == source]
        mix = nt.nodes.new("ShaderNodeMixRGB")
        mix.name = "PO_DamageMix"; mix.label = "Normal / Hurt"
        mix.blend_type = "MIX"
        mix.location = (damage.location.x + 300, damage.location.y - 70)
        for link in outgoing:
            target = link.to_socket
            nt.links.remove(link)
            nt.links.new(mix.outputs[0], target)
        nt.links.new(source, mix.inputs[2])
    mix.inputs[1].default_value = (1.0, 1.0, 1.0, 1.0)
    mix.inputs[0].default_value = 1.0 if show else 0.0
    vis = nt.nodes.get("PO_DamageVis")
    if vis is not None:
        vis.inputs["Fac"].default_value = 1.0
    return mix


def _apply_hurt_keys(obj, hurt):
    """Set the object's hurt shape keys (tagged at import) to 1 in Hurt and 0 in Normal."""
    keys = obj.data.shape_keys if obj.type == "MESH" else None
    if keys is None or not keys.get("po_hurt_keys"):
        return
    for name in json.loads(keys["po_hurt_keys"]):
        block = keys.key_blocks.get(name)
        if block is not None:
            block.value = 1.0 if hurt else 0.0


def _po_update_show_damage(self, context):
    """Switch slot-1 artwork and hurt shape keys while keeping every surface piece visible."""
    show = bool(context.scene.po_show_damage)
    for obj in bpy.data.objects:
        _disable_legacy_hurt_mask(obj)
        _ensure_damage_uv_layer(obj)
        _apply_hurt_keys(obj, show)
    for mat in bpy.data.materials:
        _ensure_damage_mix(mat, show)
    context.view_layer.update()


def _po_update_material_state(self, context):
    show = context.scene.po_material_state == "HURT"
    if context.scene.po_show_damage != show:
        context.scene.po_show_damage = show
    else:
        _po_update_show_damage(self, context)


def _po_character_mesh(context):
    active = context.active_object
    if active is not None and active.type == "MESH":
        return active
    arm = active if active is not None and active.type == "ARMATURE" else None
    if arm is not None:
        for obj in bpy.data.objects:
            if obj.type == "MESH" and any(
                    mod.type == "ARMATURE" and mod.object == arm for mod in obj.modifiers):
                return obj
    for obj in context.selected_objects:
        if obj.type == "MESH":
            return obj
    return next((obj for obj in bpy.data.objects
                 if obj.type == "MESH" and obj.get("po_source_dict")), None)


class PO_OT_cycle_material(bpy.types.Operator):
    """Activate the next normal or hurt material on the imported character"""
    bl_idname = "po.cycle_material"
    bl_label = "Choose Punch-Out Material"
    bl_options = {"REGISTER", "UNDO"}

    state: EnumProperty(items=(("NORMAL", "Normal", "Normal-state materials"),
                               ("HURT", "Hurt", "Hurt-state materials")),
                        default="NORMAL")
    direction: IntProperty(default=1, min=-1, max=1, options={"HIDDEN"})

    def execute(self, context):
        obj = _po_character_mesh(context)
        if obj is None:
            self.report({"ERROR"}, "Select an imported Punch-Out mesh or armature")
            return {"CANCELLED"}
        want_hurt = self.state == "HURT"
        choices = [i for i, slot in enumerate(obj.material_slots)
                   if slot.material is not None and
                   bool(slot.material.get("po_damage_mesh")) == want_hurt and
                   not slot.material.get("po_outline")]
        if not choices:
            self.report({"WARNING"}, "This character has no %s materials" % self.state.lower())
            return {"CANCELLED"}
        current = obj.active_material_index
        if current in choices:
            pos = (choices.index(current) + self.direction) % len(choices)
        else:
            pos = 0 if self.direction >= 0 else len(choices) - 1
        obj.active_material_index = choices[pos]
        context.view_layer.objects.active = obj
        obj.select_set(True)
        context.scene.po_material_state = "HURT" if want_hurt else "NORMAL"
        mat = obj.material_slots[choices[pos]].material
        self.report({"INFO"}, "%s material: %s" % (self.state.title(), mat.name))
        return {"FINISHED"}


# The PO Tools panels (bone reshaping, damage state, weights) live in io_punchout_ui.py,
# which shows them only for combined fighter imports.


def menu_func(self, context):
    # Compatibility path; File > Import > Punch-Out!! Asset is the general importer.
    self.layout.operator(ImportPunchOut.bl_idname, text="Punch-Out!! Fighter, combined (.dict)")


def register():
    bpy.utils.register_class(ImportPunchOut)
    bpy.types.TOPBAR_MT_file_import.append(menu_func)
    bpy.types.Scene.po_show_damage = BoolProperty(
        name="Show Damage State",
        description="Enable the slot-1 bruise, welt, and black-eye texture artwork",
        default=SHOW_DAMAGE,
        update=_po_update_show_damage,
    )
    bpy.types.Scene.po_material_state = EnumProperty(
        name="Material State",
        description="Preview clean textures or the hurt texture artwork",
        items=(("NORMAL", "Normal", "Bypass black eyes, welts, bruises, and other hurt artwork",
                "CHECKMARK", 0),
               ("HURT", "Hurt", "Enable the hurt-state texture artwork", "MOD_MASK", 1)),
        default="HURT" if SHOW_DAMAGE else "NORMAL",
        update=_po_update_material_state,
    )
    bpy.utils.register_class(PO_RETOOL_OT_apply_scale_x)
    bpy.utils.register_class(PO_RETOOL_OT_apply_scale_y)
    bpy.utils.register_class(PO_RETOOL_OT_apply_move)
    bpy.utils.register_class(PO_RETOOL_OT_apply_rotate)
    bpy.utils.register_class(PO_RETOOL_OT_set_length)
    bpy.utils.register_class(PO_RETOOL_OT_stretch_to)
    bpy.utils.register_class(PO_RETOOL_OT_uniform_scale)
    bpy.utils.register_class(PO_RETOOL_OT_print_report)
    bpy.utils.register_class(PO_RETOOL_OT_restore_originals)
    bpy.utils.register_class(PO_RETOOL_OT_auto_weight)
    bpy.utils.register_class(PO_RETOOL_OT_transfer_weights)
    bpy.utils.register_class(PO_OT_cycle_material)
    # Repair already-open imports and upgrade decoded slot-1 material graphs. At startup the
    # add-on sees a restricted context without a scene, so wait until one exists.
    bpy.app.timers.register(_repair_open_imports, first_interval=0.1)


def _repair_open_imports():
    if getattr(bpy.context, "scene", None) is None:
        return 0.5
    _po_update_show_damage(None, bpy.context)
    return None


def unregister():
    if bpy.app.timers.is_registered(_repair_open_imports):
        bpy.app.timers.unregister(_repair_open_imports)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func)
    del bpy.types.Scene.po_material_state
    del bpy.types.Scene.po_show_damage
    bpy.utils.unregister_class(PO_OT_cycle_material)
    bpy.utils.unregister_class(PO_RETOOL_OT_transfer_weights)
    bpy.utils.unregister_class(PO_RETOOL_OT_auto_weight)
    bpy.utils.unregister_class(PO_RETOOL_OT_restore_originals)
    bpy.utils.unregister_class(PO_RETOOL_OT_print_report)
    bpy.utils.unregister_class(PO_RETOOL_OT_uniform_scale)
    bpy.utils.unregister_class(PO_RETOOL_OT_stretch_to)
    bpy.utils.unregister_class(PO_RETOOL_OT_set_length)
    bpy.utils.unregister_class(PO_RETOOL_OT_apply_rotate)
    bpy.utils.unregister_class(PO_RETOOL_OT_apply_move)
    bpy.utils.unregister_class(PO_RETOOL_OT_apply_scale_y)
    bpy.utils.unregister_class(PO_RETOOL_OT_apply_scale_x)
    bpy.utils.unregister_class(ImportPunchOut)


if __name__ == "__main__":
    register()
