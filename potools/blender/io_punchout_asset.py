"""
io_punchout_asset.py — generic Punch-Out!! asset import/export for Blender.

File > Import > Punch-Out!! Asset (.dict) reads any archive through nlg_asset (the strict loader
in po_archive) and builds:

    PO <archive>                  collection, carries the asset summary
    ├── Section N                 only for multi-section cinematic containers
    │   └── Model N               one per model set
    │       ├── Meshes            one object per source mesh, placed by its node transform
    │       └── Skeleton          armature from B00A bind matrices (skinned sets only)

Every mesh object carries `po_asset` (versioned JSON, nlg_asset.SCHEMA): source archive and
hashes, section, model set, node, mesh, material span and textures, transform, attribute
hashes, what is editable and what is only preserved, and cross-archive candidates.

File > Export > Punch-Out!! Asset (.dict) turns the scene back into a PatchSet: moved vertices,
UV changes, topology with original-vertex provenance, texture pixels and mip chains, and
node transform changes of static sets. The Material inputs panel also exports proven
scalar/texture fields through guarded patches. An untouched scene exports a byte-identical
archive. New vertices must inherit an imported vertex's provenance. Vertex-group weights of
skinned meshes export into the weight/palette-index arrays (4 influences, normalized, B00B
palette compacted or extended with B00A bones). With the export options enabled, deleting or
duplicating an imported object of a static model set removes or adds a mesh inside its node.
Node insertion/removal, whole-mesh edits in skinned sets and multi-section rebuilds stay locked.

The combined fighter importer (io_import_punchout) stays available as a compatibility mode.
"""
import json
import math
import os
import struct

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper
from mathutils import Matrix, Vector

import nlg_asset
import nlg_model
import po_errors

MATRIX_TOLERANCE = 1e-5


def _names_for(dict_path):
    import nlg_hash
    d = os.path.dirname(os.path.abspath(dict_path))
    while True:
        candidate = os.path.join(d, "hashid.bin")
        if os.path.isfile(candidate):
            try:
                return nlg_hash.load_hashid_bin(candidate)
            except Exception:
                return {}
        parent = os.path.dirname(d)
        if parent == d:
            return {}
        d = parent


def _short(name):
    return name.rstrip("/").split("/")[-1].strip() or name


def _blender_matrix(stored):
    return Matrix(nlg_model.world_matrix(stored))


def _stored_matrix(m):
    return tuple(m[r][c] for c in range(4) for r in range(4))


def _flat(m):
    return [m[r][c] for r in range(4) for c in range(4)]


def _collection(name, parent):
    col = bpy.data.collections.new(name)
    parent.children.link(col)
    return col


def _image(doc, section, entry, cache):
    key = (section, entry.index)
    if key in cache:
        return cache[key]
    import nlg_texture
    img = None
    try:
        rgba = nlg_texture.decode_texture(doc.sections[section].archive, entry)
        w, h = entry.width, entry.height
        img = bpy.data.images.new(_short(entry.name)[:60], w, h, alpha=True)
        px = [0.0] * (w * h * 4)
        for y in range(h):          # archive rows are top-down, Blender's bottom-up
            src = (h - 1 - y) * w * 4; dst = y * w * 4
            for i in range(w * 4):
                px[dst + i] = rgba[src + i] / 255.0
        img.pixels.foreach_set(px)
        img.update()
        img.pack()
        img["po_texture"] = json.dumps({"section": section, "index": entry.index, "hash": entry.hash,
                                        "source": str(doc.path), "source_sha256": doc.source_hashes})
    except (NotImplementedError, ValueError) as ex:
        print("PunchOut asset: texture %s not previewed: %s" % (entry.name, ex))
    cache[key] = img
    return img


def _material(doc, section, model_set, mesh, meta, cache, images, load_textures=True):
    import io_punchout_material
    return io_punchout_material.build_material(doc, section, model_set, mesh, meta, cache,
                                              lambda entry: _image(doc, section, entry, images), load_textures)


def _armature(doc, model_set, name, collection):
    from io_punchout_animation import bind_armature
    return bind_armature(doc, model_set, name, collection)


def _build_mesh(doc, section, model_set, mesh, meta, name):
    pos = mesh.attribute("position").values()
    md = bpy.data.meshes.new(name)
    md.from_pydata([tuple(p) for p in pos], [], mesh.triangles())
    md.update()
    provenance = md.attributes.new("po_vertex_source", "INT", "POINT")
    provenance.data.foreach_set("value", list(range(1, len(pos) + 1)))
    uv_attr = mesh.attribute("texcoord", 0)
    if uv_attr is not None and uv_attr.storage:
        uvs = uv_attr.values()
        layer = md.uv_layers.new(name="UV").data
        for loop in md.loops:
            u, v = uvs[loop.vertex_index]
            layer[loop.index].uv = (u, 1.0 - v)
    for a in mesh.attributes:        # further sets preview only; never exported unless verified
        if a.semantic == "texcoord" and a.set > 0 and a.storage:
            vals = a.values(); layer = md.uv_layers.new(name="UV%d%s" % (a.set, "" if a.verified else "_preview"))
            if layer is None:
                continue
            for loop in md.loops:
                u, v = vals[loop.vertex_index]
                layer.data[loop.index].uv = (u, 1.0 - v)
    if md.uv_layers.get("UV") is not None:
        md.uv_layers.active = md.uv_layers["UV"]
    for attr in mesh.attributes:
        if attr.semantic == "color" and attr.storage:
            colors = attr.values()
            layer = md.color_attributes.new(name="PO_Color%d" % attr.set,
                                            type="FLOAT_COLOR", domain="CORNER")
            scale = 1 / 255 if attr.storage[0] == "B" else 1.0
            for loop in md.loops:
                rgba = tuple(v * scale for v in colors[loop.vertex_index])
                layer.data[loop.index].color = rgba if len(rgba) == 4 else (*rgba, 1.0)
    nrm = mesh.attribute("normal")
    if nrm is not None and nrm.storage:
        normals = []
        for n in nrm.values():
            ln = math.sqrt(sum(x * x for x in n)) or 1.0
            normals.append(tuple(x / ln for x in n))
        try:
            md.polygons.foreach_set("use_smooth", [True] * len(md.polygons))
            if hasattr(md, "use_auto_smooth"):
                md.use_auto_smooth = True
            md.normals_split_custom_set_from_vertices(normals)
        except Exception as ex:
            print("PunchOut asset: authored normals skipped on %s: %s" % (name, ex))
    return md


def import_asset(dict_path, load_textures=True):
    names = _names_for(dict_path)
    doc = nlg_asset.AssetDocument(dict_path, names)
    stem = os.path.splitext(os.path.basename(dict_path))[0]
    root = _collection("PO %s" % stem, bpy.context.scene.collection)
    root["po_asset_document"] = json.dumps(doc.summary())
    bpy.context.scene["po_export_source_dict"] = str(dict_path)
    materials, images = {}, {}
    created = []
    for section in doc.sections:
        parent = root if len(doc.sections) == 1 else _collection("Section %d" % section.index, root)
        for ms in section.model_sets:
            model_col = _collection("Model %d" % ms.index, parent)
            meshes_col = _collection("Meshes", model_col)
            arm_obj = bone_names = None
            if ms.skinned and ms.bones:
                arm_obj, bone_names = _armature(doc, ms, "Skeleton %d" % ms.index, _collection("Skeleton", model_col))
            for mesh in ms.meshes:
                meta = doc.mesh_metadata(section.index, ms, mesh)
                node = meta["node"]["name"]
                name = ("%s:%s" % (_short(node), _short(meta["mesh"]["name"])))[:60]
                md = _build_mesh(doc, section.index, ms, mesh, meta, name)
                md.materials.append(_material(doc, section.index, ms, mesh, meta, materials, images, load_textures))
                obj = bpy.data.objects.new(name, md)
                meshes_col.objects.link(obj)
                obj.matrix_world = _blender_matrix(ms.transforms[mesh.transform])
                if arm_obj is not None and mesh.palette:
                    _skin(obj, mesh, bone_names, arm_obj)
                    meta["bone_groups"] = {str(h): bone_names[h] for h in mesh.palette if h in bone_names}
                    meta["skin_bones"] = {name: h for h, name in bone_names.items()}   # B00A bones weights may use
                created.append((obj, meta))
    # Record the matrix Blender actually holds: loc/rot/scale cannot carry shear, so export
    # compares against this, not the stored matrix, to tell a real move from decomposition.
    bpy.context.view_layer.update()
    for obj, meta in created:
        meta["blender"] = {"object_matrix": _flat(obj.matrix_world), "object": obj.name}
        obj["po_asset"] = json.dumps(meta)
        obj["po_source_dict"] = str(dict_path)
    if load_textures:
        for section in doc.sections:
            for entry in section.textures: _image(doc,section.index,entry,images)
        _game_view()
    return doc, root, [obj for obj, _ in created]


def _game_view():
    """Same view as the fighter importer: the Wii writes straight sRGB with no tonemap, and
    Blender's Filmic (3.x) / AgX (4.x) default desaturates and lifts the blacks. Lit shaders
    (skin family, hwlit, ropes) get the fighter arena light rig; lightmapped ones ignore it."""
    scene = bpy.context.scene
    try:
        scene.view_settings.view_transform = "Standard"; scene.view_settings.look = "None"
        scene.display_settings.display_device = "sRGB"
        import io_import_punchout
        io_import_punchout.outline_view_raw(False)
    except (TypeError, AttributeError) as ex:
        print("PunchOut asset: could not set the Standard view transform:", ex)
    if not any(o.name.startswith("PO_TEV_KeyVector") for o in bpy.data.objects):
        import po_shader
        po_shader.build_light_rig()


def _skin(obj, mesh, bone_names, arm_obj):
    wts, idx = nlg_model.skin_attributes(mesh)      # fighter skins keep indices in set 1
    if idx is None or wts is None:
        return
    groups = {}
    for v, (ii, ww) in enumerate(zip(idx.values(), wts.values())):
        for k in range(4):
            if ww[k] <= 0.0001 or ii[k] >= len(mesh.palette):
                continue
            bone = bone_names.get(mesh.palette[ii[k]])
            if bone is None:
                continue
            groups.setdefault(bone, []).append((v, ww[k]))
    for bone, pairs in groups.items():
        vg = obj.vertex_groups.new(name=bone)
        for v, w in pairs:
            vg.add([v], w, "REPLACE")
    world = obj.matrix_world.copy()
    obj.parent = arm_obj
    obj.matrix_world = world
    mod = obj.modifiers.new("Armature", "ARMATURE"); mod.object = arm_obj


# ---------------------------------------------------------------------------------------
# Export: scene -> PatchSet

class ExportRefused(po_errors.Refusal):
    """Refusal naming archive, section, model set, mesh object and field (po_errors)."""


def _scene_objects(source):
    out = []
    for obj in bpy.data.objects:
        raw = obj.get("po_asset")
        if obj.type != "MESH" or not raw:
            continue
        meta = json.loads(raw)
        if meta.get("schema") != nlg_asset.SCHEMA:
            raise ExportRefused("imported by an incompatible tool version (schema %s)" % meta.get("schema"),
                                mesh=obj.name, field="po_asset schema")
        if os.path.normcase(os.path.abspath(meta["source"]["path"])) == os.path.normcase(os.path.abspath(source)):
            out.append((obj, meta))
    return out


def _mesh_positions(md):
    co = [0.0] * (len(md.vertices) * 3); md.vertices.foreach_get("co", co)
    return [tuple(co[i:i + 3]) for i in range(0, len(co), 3)]


def _oriented(faces):
    return sorted(min(t, t[1:] + t[:1], t[2:] + t[:2]) for t in (tuple(f) for f in faces))


def _provenance(obj, mesh, where):
    attr = obj.data.attributes.get("po_vertex_source")
    if attr is None or attr.domain != "POINT" or attr.data_type != "INT":
        raise ExportRefused("original-vertex provenance is missing; reimport the mesh", field="po_vertex_source")
    source = [d.value - 1 for d in attr.data]
    if any(not 0 <= i < mesh.vertex_count for i in source):
        raise ExportRefused("new vertices must inherit a source vertex (duplicate or extrude imported vertices)",
                            field="po_vertex_source")
    return source


def _skin_changes(obj, meta, mesh, source, where):
    """{Blender vertex: {bone hash: weight}} for vertices whose vertex-group weights differ from
    their source vertex, plus how many were limited to the 4 strongest influences. Groups must be
    bones of the model set's skeleton; weights at or below 0.0001 are ignored as on import."""
    wts, idx = nlg_model.skin_attributes(mesh)
    if not mesh.palette or wts is None: return {}, 0
    if "skin_bones" not in meta and not obj.vertex_groups: return {}, 0   # imported before fighter skins had groups
    bones = {name: int(h) for name, h in meta.get("skin_bones", {}).items()}
    if not bones:     # older imports carried only the palette groups
        bones = {name: int(h) for h, name in meta.get("bone_groups", {}).items()}
    names = {g.index: g.name for g in obj.vertex_groups}
    source_skin = nlg_model.influences(mesh); changed = {}; limited = 0
    for v, s in zip(obj.data.vertices, source):
        want = {h: w for h, w in source_skin[s].items() if w > 0.0001}
        got = {}
        for g in v.groups:
            if g.weight > 0.0001:
                name = names[g.group]
                if name not in bones:
                    raise ExportRefused("%s: vertex group '%s' is not a bone of this model's skeleton; remove it before export" % (where, name))
                got[bones[name]] = got.get(bones[name], 0.0) + g.weight
        if set(want) == set(got) and all(abs(want[h] - got[h]) <= 1e-6 for h in want): continue
        if not got: raise ExportRefused("%s: vertex %d has no bone weight; every skinned vertex needs one" % (where, v.index))
        if len(got) > nlg_model.INFLUENCE_LIMIT:
            got = dict(sorted(got.items(), key=lambda kv: (-kv[1], kv[0]))[:nlg_model.INFLUENCE_LIMIT]); limited += 1
        changed[v.index] = got
    return changed, limited


def _geometry_edit(obj, mesh, meta, where, bake=None):
    """One Blender mesh -> (fixed attribute edits or provenance-preserving MeshData, skin
    overrides per output vertex, vertices limited to 4 influences). `bake` moves a duplicate's
    object transform into its vertices, because copies share their node's transform."""
    md = obj.data; source = _provenance(obj, mesh, where)
    skin, limited = _skin_changes(obj, meta, mesh, source, where)
    if any(m.type != "ARMATURE" and (m.show_viewport or m.show_render) for m in obj.modifiers):
        raise ExportRefused("apply mesh modifiers before exporting", field="modifiers")
    md.calc_loop_triangles()
    tris = [tuple(t.vertices) for t in md.loop_triangles]
    topology = source != list(range(mesh.vertex_count)) or _oriented(tris) != _oriented(mesh.triangles())
    local = _mesh_positions(md); output_vertices = list(range(len(local)))
    positions = local if bake is None else [tuple(bake @ Vector(p)) for p in local]
    uv_attrs = [a for a in mesh.attributes if a.semantic == "texcoord" and a.verified
                and md.uv_layers.get("UV" if a.set == 0 else "UV%d" % a.set) is not None]
    layers = [md.uv_layers["UV" if a.set == 0 else "UV%d" % a.set] for a in uv_attrs]
    per_vertex = [[None] * len(source) for _ in uv_attrs]; variants = {}; loop_vertices = {}; seen_vertices = set()
    for loop in md.loops:
        v = loop.vertex_index
        values = [(layer.data[loop.index].uv[0], 1.0 - layer.data[loop.index].uv[1]) for layer in layers]
        encoded = tuple(a.encode_element(value) for a, value in zip(uv_attrs, values))
        key = (v, encoded)
        if key not in variants:
            if v not in seen_vertices:
                target = v
            else:
                target = len(output_vertices); output_vertices.append(v); topology = True
                for arr in per_vertex: arr.append(None)
            variants[key] = target
            seen_vertices.add(v)
            for arr, value in zip(per_vertex, values): arr[target] = value
        loop_vertices[loop.index] = variants[key]
    output_source = [source[v] for v in output_vertices]
    edits = {}
    for a, values in zip(uv_attrs, per_vertex):
        old = a.values()
        edits[("texcoord", a.set)] = [value if value is not None else old[output_source[i]] for i,value in enumerate(values)]
    new_pos = [positions[v] for v in output_vertices]
    pos_attr = mesh.attribute("position")
    changed = lambda pts: [i for i, (s, p) in enumerate(zip(output_source, pts)) if pos_attr.encode_element(p) != pos_attr.raw[s*pos_attr.stride:(s+1)*pos_attr.stride]]
    moved = changed(new_pos)
    edited = moved if bake is None else changed([local[v] for v in output_vertices])   # edit-mode changes only
    if moved or topology: edits[("position",0)] = new_pos
    normal = mesh.attribute("normal")
    if normal is not None and normal.verified and (moved or topology):
        summed = [Vector((0,0,0)) for _ in positions]
        for a,b,c in tris:
            n = (Vector(positions[b])-Vector(positions[a])).cross(Vector(positions[c])-Vector(positions[a]))
            for i in (a,b,c): summed[i] += n
        affected = set(range(len(positions))) if topology else {v for tri in tris if any(i in edited for i in tri) for v in tri}
        turn = None if bake is None else bake.to_3x3().inverted().transposed()
        old = normal.values(); values = []
        for i,v in enumerate(output_vertices):
            n = summed[v]; o = old[output_source[i]]
            if v in affected and n.length_squared: values.append(tuple(n.normalized()))
            elif turn is None: values.append(o)
            else:
                r = turn @ Vector(o)
                values.append(tuple(r.normalized() * Vector(o).length) if r.length_squared else o)
        edits[("normal",normal.set)] = values
    overrides = {i: skin[v] for i, v in enumerate(output_vertices) if v in skin}
    if topology:
        faces = [tuple(loop_vertices[i] for i in t.loops) for t in md.loop_triangles]
        return None, nlg_model.remap_mesh(mesh,output_source,faces,edits), overrides, limited
    return edits, None, overrides, limited


def _texture_edits(doc):
    patches = []; seen = {}
    for img in bpy.data.images:
        if not img.get("po_texture"): continue
        meta = json.loads(img["po_texture"])
        if os.path.normcase(os.path.abspath(meta.get("source", ""))) != os.path.normcase(os.path.abspath(doc.path)): continue
        where = dict(section=meta.get("section"), mesh=img.name, field="texture %s" % meta.get("index"))
        if meta.get("source_sha256") != doc.source_hashes: raise ExportRefused("Texture was imported from a different source version", **where)
        section = doc.sections[meta["section"]]; entry = section.textures[meta["index"]]
        if entry.hash != meta["hash"] or tuple(img.size) != (entry.width,entry.height):
            raise ExportRefused("Texture identity or dimensions changed; same-size painting is supported", **where)
        px = [0.0] * (entry.width*entry.height*4); img.pixels.foreach_get(px)
        if not all(math.isfinite(v) for v in px): raise ExportRefused("Texture contains nonfinite pixels", **where)
        rgba = bytearray(len(px)); row = entry.width*4
        for y in range(entry.height):
            rgba[y*row:(y+1)*row] = bytes(max(0,min(255,round(v*255))) for v in px[(entry.height-1-y)*row:(entry.height-y)*row])
        key = (section.index,entry.index)
        if key in seen and seen[key] != rgba: raise ExportRefused("Duplicate copies of a texture were edited differently", **where)
        if key not in seen: patches += nlg_asset.texture_patches(section,entry,rgba)
        seen[key] = rgba
    return patches


def collect_patch_set(source, allow_delete=False, allow_duplicate=False):
    """Compare the scene with its source and return (PatchSet, review lines). Whole-mesh
    deletion (a missing object) and duplication (extra copies of an imported object) change the
    mesh table only when allowed; otherwise they are refused as before."""
    doc = nlg_asset.AssetDocument(source)
    if not doc.editable:
        raise ExportRefused("Source is read-only: %s" % doc.limitation, archive=source, field="container")
    objects = _scene_objects(source)
    has_images = any(img.get("po_texture") and json.loads(img["po_texture"]).get("source") == str(doc.path) for img in bpy.data.images)
    if not objects and not has_images:
        raise ExportRefused("No imported Punch-Out asset objects for %s in this scene." % os.path.basename(source))
    patches, notes, transforms, topology, layouts = [], [], {}, {}, {}
    slots = {}
    for obj, meta in objects:
        if meta["source"]["sha256"] != doc.source_hashes:
            raise ExportRefused("%s was imported from a different version of the source archive." % obj.name)
        slots.setdefault((meta["model_set"], meta["mesh"]["index"]), []).append((obj, meta))
    unknown = set(slots) - {(ms.index, m.index) for ms in doc.sections[0].model_sets for m in ms.meshes}
    if unknown: raise ExportRefused("Scene objects name mesh slots the source does not have: %s" % sorted(unknown)[:4])
    originals = []
    for ms in doc.sections[0].model_sets:
        copies, per_set, bones = [], {}, {h for h, _ in ms.bones}
        for mesh in ms.meshes:
            group = slots.get((ms.index, mesh.index), [])
            if len(group) > 1:
                where = "%s (model set %d, mesh %d)" % (group[0][0].name, ms.index, mesh.index)
                if not allow_duplicate:
                    raise ExportRefused("%s: duplicate mesh object; mesh-slot insertion is off (enable 'Duplicated objects become new mesh slots')" % where)
                named = [g for g in group if g[0].name == g[1]["blender"]["object"]]
                if len(named) != 1:
                    raise ExportRefused("%s: cannot tell the imported object from its copies; keep the imported object's name" % where)
                group = named + sorted((g for g in group if g is not named[0]), key=lambda g: g[0].name)
            if not group and not allow_delete:
                raise ExportRefused("Imported mesh objects are missing; deleting whole mesh slots is off (enable 'Deleted objects remove their mesh slots')")
            for k, (obj, meta) in enumerate(group):      # output slots: source order, copies follow
                per_set[sum(copies) + k] = (mesh, obj, meta, k)
            copies.append(len(group))
            if group: originals.append(group[0])
        if copies != [1] * len(ms.meshes):
            try: nlg_model.check_layout(ms, nlg_model.copies_layout(ms, copies))
            except nlg_model.ModelFormatError as ex: raise ExportRefused("Model set %d: %s" % (ms.index, ex))
            layouts[ms.index] = copies
        rebuilt, fixed = {}, {}
        for out, (mesh, obj, meta, k) in sorted(per_set.items()):
            where = "%s (model set %d, mesh %d)" % (obj.name, ms.index, mesh.index)
            imported = meta["blender"]["object_matrix"]; now = _flat(obj.matrix_world)
            moved = max(abs(a - b) for a, b in zip(imported, now)) > MATRIX_TOLERANCE
            bake = Matrix([imported[r * 4:r * 4 + 4] for r in range(4)]).inverted() @ obj.matrix_world if k and moved else None
            with po_errors.context(archive=source, section=meta["source"]["section"], model_set=ms.index, mesh=obj.name):
                edits, resized, overrides, limited = _geometry_edit(obj, mesh, meta, where, bake)
            if limited: notes.append("%s: %d vertices kept their 4 strongest bone influences (renormalized)" % (where, limited))
            if overrides or (resized is None and edits and (ms.index in layouts)):
                d = resized or nlg_model.mesh_data(mesh)
                if resized is None:
                    for key, value in (edits or {}).items():
                        a = mesh.attribute(*key); d.arrays[mesh.attributes.index(a)] = a.patched(value)
                if overrides:
                    try: d = nlg_model.reskin(mesh, d, overrides, bones)
                    except nlg_model.ModelFormatError as ex: raise ExportRefused("%s: %s" % (where, ex))
                wts, idx = nlg_model.skin_attributes(mesh)
                if resized is None and ms.index not in layouts and d.palette == mesh.palette:
                    edits = dict(edits or {})    # same-size skin edit: audited fixed patches
                    if overrides:
                        ww = d.arrays[mesh.attributes.index(wts)]; ii = d.arrays[mesh.attributes.index(idx)]
                        edits[("weights", wts.set)] = [struct.unpack_from(">4f", ww, 16 * v) for v in range(d.vertex_count)]
                        edits[("indices", idx.set)] = [tuple(ii[4 * v:4 * v + 4]) for v in range(d.vertex_count)]
                    fixed[mesh.index] = edits
                else: rebuilt[out] = d
            elif resized is not None: rebuilt[out] = resized
            elif edits: fixed[out] = edits
            if moved and not k:
                if ms.skinned:
                    raise ExportRefused("%s: skinned models move with their skeleton; moving the object is not exported." % where)
                stored = _stored_matrix(obj.matrix_world)
                key = (ms.index, mesh.transform)
                if key in transforms and max(abs(a - b) for a, b in zip(transforms[key][0], stored)) > MATRIX_TOLERANCE:
                    raise ExportRefused("%s and %s share transform %d but were moved differently."
                                        % (transforms[key][1], obj.name, mesh.transform))
                transforms[key] = (stored, obj.name)
        if rebuilt or ms.index in layouts:
            for out, values in fixed.items():
                mesh = per_set[out][0]; d = nlg_model.mesh_data(mesh)
                for key, value in values.items():
                    a = mesh.attribute(*key); d.arrays[mesh.attributes.index(a)] = a.patched(value)
                if d.digest() != nlg_model.mesh_data(mesh).digest(): rebuilt[out] = d
            if rebuilt: topology[ms.index] = rebuilt
        elif fixed: patches += nlg_model.geometry_patches(ms, fixed)
    for (set_index, t), (stored, _) in sorted(transforms.items()):
        ms = doc.sections[0].model_sets[set_index]
        patches += nlg_asset.transform_patches(ms, t, stored)
    with po_errors.context(archive=source):
        patches += _texture_edits(doc)
    import io_punchout_material
    with po_errors.context(archive=source, section=0):
        material = io_punchout_material.collect_material_patches(doc, originals)
    if any(p.chunk == doc.sections[0].model_sets[si].chunks[0xB016] for p in material for si in layouts):
        raise ExportRefused("Material input edits and whole-mesh deletion/duplication rebuild the same material table; export them separately")
    patches += material
    ps = nlg_asset.PatchSet(doc.source_hashes, patches, notes, topology=topology, layout=layouts)
    return ps, ps.review() or ["No changes: the export will be byte-identical to the source."]


def export_asset(source, out_dict, allow_delete=False, allow_duplicate=False):
    ps, review = collect_patch_set(source, allow_delete, allow_duplicate)
    report = nlg_asset.write_rebuild(source, ps, out_dict)
    return report, review


# ---------------------------------------------------------------------------------------
# Operators

class IMPORT_OT_punchout_asset(bpy.types.Operator, ImportHelper):
    """Import any Punch-Out!! archive: every model, node transform, skeleton and texture preview"""
    bl_idname = "import_scene.punchout_asset"
    bl_label = "Import Punch-Out!! Asset"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".dict"
    filter_glob: StringProperty(default="*.dict", options={"HIDDEN"})
    mode: EnumProperty(
        name="Mode",
        items=(("GENERIC", "Asset (one object per mesh)", "Every model set, placed by its node transforms"),
               ("FIGHTER", "Fighter, combined (compatibility)", "The original fighter importer: one combined mesh, rig and animations")),
        default="GENERIC")
    load_textures: BoolProperty(name="Texture previews", default=True)

    def execute(self, context):
        try:
            if self.mode == "FIGHTER":
                import io_import_punchout
                io_import_punchout.do_import(self.filepath)
                return {"FINISHED"}
            doc, root, created = import_asset(self.filepath, self.load_textures)
        except (ValueError, OSError, nlg_model.ModelFormatError) as ex:
            self.report({"ERROR"}, "%s: %s" % (os.path.basename(self.filepath), ex))
            return {"CANCELLED"}
        note = "" if doc.editable else " Read-only: %s" % doc.limitation
        self.report({"INFO"}, "Imported %d meshes into %s.%s" % (len(created), root.name, note))
        return {"FINISHED"}


class EXPORT_OT_punchout_asset(bpy.types.Operator, ExportHelper):
    """Write the changed bytes back into a copy of the source archive"""
    bl_idname = "export_scene.punchout_asset"
    bl_label = "Export Punch-Out!! Asset"
    filename_ext = ".dict"
    filter_glob: StringProperty(default="*.dict", options={"HIDDEN"})
    source_dict: StringProperty(name="Source archive", subtype="FILE_PATH")
    allow_delete: BoolProperty(name="Deleted objects remove their mesh slots", default=False,
                               description="Static models only: a deleted imported object removes its mesh from its node")
    allow_duplicate: BoolProperty(name="Duplicated objects become new mesh slots", default=False,
                                  description="Static models only: copies of an imported object become new meshes in the same node")

    def invoke(self, context, event):
        self.source_dict = self.source_dict or context.scene.get("po_export_source_dict", "")
        return ExportHelper.invoke(self, context, event)

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "source_dict")
        col.prop(self, "allow_delete"); col.prop(self, "allow_duplicate")
        try:
            _, review = collect_patch_set(bpy.path.abspath(self.source_dict), self.allow_delete, self.allow_duplicate)
            col.label(text="Changes to export:")
            for line in review[:12]:
                col.label(text=line)
            if len(review) > 12:
                col.label(text="... and %d more" % (len(review) - 12))
        except (ValueError, OSError) as ex:
            col.label(text=str(ex), icon="ERROR")

    def execute(self, context):
        try:
            report, review = export_asset(bpy.path.abspath(self.source_dict), bpy.path.abspath(self.filepath),
                                          self.allow_delete, self.allow_duplicate)
        except (ValueError, OSError) as ex:
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        for line in review:
            print("PunchOut asset export:", line)
        self.report({"INFO"}, "Exported %d audited byte(s); preserved chunks were verified against the source."
                    % report["audit"]["changed_bytes"])
        return {"FINISHED"}


def _menu_import(self, context):
    self.layout.operator(IMPORT_OT_punchout_asset.bl_idname, text="Punch-Out!! Asset (.dict)")


def _menu_export(self, context):
    self.layout.operator(EXPORT_OT_punchout_asset.bl_idname, text="Punch-Out!! Asset (.dict)")


CLASSES = (IMPORT_OT_punchout_asset, EXPORT_OT_punchout_asset)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)
    bpy.types.TOPBAR_MT_file_export.append(_menu_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(_menu_export)
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
