"""Blender 3.6/5.x raw animation-node actions and bounded source-key export.

Actions display the stored node-local transforms in source coordinates. Game placement,
fighter facing correction, IK, constraints, skin retargeting and scalar side tracks are
not simulated. Exact node directories/signatures are used; no name/order guesses.
"""
import json
import struct
from pathlib import Path

import bpy
from bpy.props import IntProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper
from mathutils import Matrix, Vector

import nlg_animation as codec
import nlg_asset
import nlg_model
import po_errors
import po_action


def _matrix(stored):
    return Matrix(nlg_model.world_matrix(stored))


def _flat(matrix):
    return [float(x) for row in matrix for x in row]


def _fingerprint(obj):
    return {"bones": [[b.name, b.parent.name if b.parent else None, _flat(b.matrix_local)] for b in obj.data.bones],
            "matrix": _flat(obj.matrix_world)}


def bind_armature(doc, model_set, name, collection):
    """Bind matrices stay unchanged; exact local node hashes establish bone parenting."""
    arm = bpy.data.armatures.new(name); obj = bpy.data.objects.new(name, arm)
    collection.objects.link(obj)
    prev = bpy.context.view_layer.objects.active
    bpy.context.view_layer.objects.active = obj
    with bpy.context.temp_override(object=obj, active_object=obj):     # also when built off-window
        bpy.ops.object.mode_set(mode="EDIT")
    bone_names = {}
    for i, (h, stored) in enumerate(model_set.bones):
        eb = arm.edit_bones.new(doc.name(h, "bone %d" % i).rstrip("/").split("/")[-1][:60])
        m = _matrix(stored)
        eb.head = m.translation; eb.tail = m.translation + m.to_3x3().col[1].normalized() * 0.05
        eb.matrix = m; bone_names[h] = eb.name
    note = "No unique local node hierarchy; bind matrices preserved with flat parenting"
    signature = None
    section = next(s for s in doc.sections if model_set in s.model_sets)
    try:
        rig, parents = section.animations.rig_for_bones([h for h, _ in model_set.bones])
        for h, parent in parents.items():
            if parent is not None:
                arm.edit_bones[bone_names[h]].parent = arm.edit_bones[bone_names[parent]]
        note = "Exact node hashes and 0x8009 parents; nearest represented ancestor"
        signature = rig.signature
    except codec.AnimationError:
        pass
    with bpy.context.temp_override(object=obj, active_object=obj):
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.context.view_layer.objects.active = prev
    obj["po_skeleton"] = json.dumps({"model_set": model_set.index, "section": section.index,
                                     "bones": [h for h, _ in model_set.bones],
                                     "hierarchy": note, "rig_signature": signature})
    return obj, bone_names


def _new_action(obj, name):
    action, slot = po_action.new_action(obj, name)
    return action, po_action.fcurves(action, slot)


def _curves(obj, action):
    if not po_action.LAYERED:
        return po_action.fcurves(action)
    layers, strips, bags, slots = po_action.layout(action)
    if layers != 1 or strips != 1:
        raise codec.AnimationError("Action must retain its one source-key layer and strip")
    if bags != 1 or slots != 1:
        raise codec.AnimationError("Action must retain its one source object slot")
    if po_action.active_slot(obj) != action.slots[0]:
        raise codec.AnimationError("Action slot changed")
    return po_action.fcurves(action, action.slots[0])


def _ui_values(track, values, rig):
    if track.kind == "rotation":
        return [(q[3], q[0], q[1], q[2]) for q in values]
    if track.kind == "translation":
        offset = rig.translations[track.node]
        return [tuple(v[i] - offset[i] for i in range(3)) for v in values]
    return values


def import_clip(source, clip_index=0, section_index=0, rig_source=None, rig_section=0, rig_index=None):
    """Create a node armature and one action. Optional external rig requires exact signature."""
    doc = nlg_asset.AssetDocument(source)
    s = doc.sections[section_index].animations
    clip = s.clips[clip_index]
    rig_doc = nlg_asset.AssetDocument(rig_source) if rig_source else doc
    rs = rig_doc.sections[rig_section if rig_source else section_index].animations
    rig = rs.rigs[rig_index] if rig_index is not None else rs.rig_for(clip)
    clip.compatible(rig)
    col = bpy.data.collections.new("PO Animation " + clip.name)
    bpy.context.scene.collection.children.link(col)
    arm = bpy.data.armatures.new("Source node hierarchy")
    obj = bpy.data.objects.new("Nodes " + rig.name, arm); col.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    positions = {}; names = {}
    for node in rig.order:
        eb = arm.edit_bones.new("node_%04d_%08X" % (node, rig.hashes[node]))
        parent = rig.parents[node]
        positions[node] = Vector(rig.translations[node]) + (positions[parent] if parent >= 0 else Vector((0, 0, 0)))
        eb.head = positions[node]; eb.tail = positions[node] + Vector((0, 0.05, 0))
        if parent >= 0: eb.parent = arm.edit_bones[names[parent]]
        names[node] = eb.name
    bpy.ops.object.mode_set(mode="OBJECT")
    action, curves = _new_action(obj, clip.name)
    for node, name in names.items(): obj.pose.bones[name].rotation_mode = "QUATERNION"
    channels = []
    for key, track in sorted(clip.tracks.items()):
        if track.type == 0x7112: continue
        prop = {"rotation": "rotation_quaternion", "translation": "location", "scale": "scale"}[track.kind]
        path = obj.pose.bones[names[track.node]].path_from_id(prop)
        values = _ui_values(track, track.values(), rig)
        for component in range(len(values[0])):
            fc = curves.new(path, index=component)
            fc.keyframe_points.add(track.keys)
            for i, value in enumerate(values):
                fc.keyframe_points[i].co = (i + 1, value[component])
                fc.keyframe_points[i].interpolation = "LINEAR"
            fc.update()
            channels.append({"path": path, "component": component, "node": track.node, "type": track.type})
    bpy.context.scene.frame_start = 1; bpy.context.scene.frame_end = clip.frames
    bpy.context.scene.frame_set(1)
    bpy.context.view_layer.update()
    obj["po_animation"] = json.dumps({"schema": 1, "source": str(Path(source).resolve()),
                                      "sha256": doc.source_hashes, "section": section_index,
                                      "clip": clip.index, "directory": clip.directory,
                                      "rig_source": str(rig_doc.path.resolve()), "rig_sha256": rig_doc.source_hashes,
                                      "rig_section": rig_section if rig_source else section_index,
                                      "rig_index": rig.index, "rig_fingerprint": rig.fingerprint,
                                      "rig_structure": _fingerprint(obj), "channels": channels})
    action["po_animation_note"] = "Source node transforms. Engine placement, IK and scalar side tracks are not simulated."
    obj.show_in_front = True
    return doc, obj, action


def collect_patch_set(obj):
    """(document, PatchSet); refusals name the clip archive, section, rig object and channel."""
    try: meta = json.loads(obj["po_animation"]) if "po_animation" in obj else {}
    except (TypeError, ValueError): meta = {}
    with po_errors.context(archive=meta.get("source"), section=meta.get("section"), mesh=obj.name):
        return _collect_patch_set(obj)


def _collect_patch_set(obj):
    if "po_animation" not in obj:
        raise codec.AnimationError("Select an imported source-node animation armature")
    meta = json.loads(obj["po_animation"])
    doc = nlg_asset.AssetDocument(meta["source"])
    if not doc.editable: raise codec.AnimationError(doc.limitation)
    if meta["sha256"] != doc.source_hashes: raise codec.AnimationError("Animation source bytes changed")
    rig_doc = nlg_asset.AssetDocument(meta["rig_source"])
    if meta["rig_sha256"] != rig_doc.source_hashes: raise codec.AnimationError("Rig source bytes changed")
    rig = rig_doc.sections[meta["rig_section"]].animations.rigs[meta["rig_index"]]
    clip = doc.sections[meta["section"]].animations.clips[meta["clip"]]
    clip.compatible(rig)
    if clip.directory != meta["directory"] or rig.fingerprint != meta["rig_fingerprint"]:
        raise codec.AnimationError("Source rig/clip identity changed")
    if _fingerprint(obj) != meta["rig_structure"]:
        raise codec.AnimationError("Node names, parenting, rest matrices or object placement changed")
    ad = obj.animation_data
    if ad is None or ad.action is None or ad.drivers or ad.nla_tracks:
        raise codec.AnimationError("Export requires the imported action without drivers or NLA tracks")
    if obj.constraints or obj.modifiers or any(b.constraints or b.rotation_mode != "QUATERNION" for b in obj.pose.bones):
        raise codec.AnimationError("Pose constraints, modifiers and alternate rotation modes are unsupported")
    curves = list(_curves(obj, ad.action))
    expected = {(c["path"], c["component"]): c for c in meta["channels"]}
    if len(curves) != len(expected) or {(c.data_path, c.array_index) for c in curves} != set(expected):
        raise codec.AnimationError("Action channels were added, removed or retargeted")
    values = {key: [list(v) for v in _ui_values(t, t.values(), rig)] for key, t in clip.tracks.items() if t.type != 0x7112}
    changed = set()
    for fc in curves:
        channel = expected[(fc.data_path, fc.array_index)]
        key = (channel["node"], channel["type"]); track = clip.tracks[key]
        if fc.modifiers or fc.mute or len(fc.keyframe_points) != track.keys:
            raise po_errors.located(codec.AnimationError("Source key count, channel muting or curve modifiers changed"),
                                    field="%s[%d]" % (fc.data_path, fc.array_index))
        for i, point in enumerate(fc.keyframe_points):
            if point.co[0] != i + 1 or point.interpolation != "LINEAR":
                raise po_errors.located(codec.AnimationError("Key times/interpolation changed; only existing sampled values can be exported"),
                                        field="%s[%d] key %d" % (fc.data_path, fc.array_index, i))
            before = values[key][i][fc.array_index]; after = float(point.co[1])
            if struct.pack(">f", before) != struct.pack(">f", after):
                values[key][i][fc.array_index] = after; changed.add(key)
    edits = {}
    for key in changed:
        track = clip.tracks[key]; ui = values[key]
        if track.kind == "rotation":
            edits[key] = [(v[1], v[2], v[3], v[0]) for v in ui]
        elif track.kind == "translation":
            offset = rig.translations[track.node]
            edits[key] = [tuple(v[i] + offset[i] for i in range(3)) for v in ui]
        else: edits[key] = ui
    return doc, nlg_asset.PatchSet(doc.source_hashes, clip.patches(edits, rig))


def export_clip(obj, out_dict):
    import po_archive
    doc, patches = collect_patch_set(obj)
    target = Path(out_dict).resolve()
    meta = json.loads(obj["po_animation"])
    for source in (doc.path, Path(meta["rig_source"])):
        source = source.resolve()
        root = next((p for p in source.parents if (p / "hashid.bin").is_file()), source.parent)
        po_archive.external(target, root)
    if any(p.exists() for p in (target, target.with_suffix(".data"), target.with_suffix(".patchset.json"))):
        raise codec.AnimationError("Choose a new output name; existing exports are preserved")
    return nlg_asset.write_rebuild(doc.path, patches, target)


class PO_OT_import_clip(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.punchout_clip"
    bl_label = "Punch-Out!! Source-node Animation"
    filename_ext = ".dict"
    filter_glob: StringProperty(default="*.dict", options={"HIDDEN"})
    clip_index: IntProperty(name="Clip index", default=0, min=0)
    section_index: IntProperty(name="Section index", default=0, min=0)

    def execute(self, context):
        try:
            import_clip(self.filepath, self.clip_index, self.section_index)
        except (ValueError, IndexError) as ex:
            self.report({"ERROR"}, str(ex)); return {"CANCELLED"}
        return {"FINISHED"}


class PO_OT_export_clip(bpy.types.Operator, ExportHelper):
    bl_idname = "export_scene.punchout_clip"
    bl_label = "Punch-Out!! Source-node Animation"
    filename_ext = ".dict"
    filter_glob: StringProperty(default="*.dict", options={"HIDDEN"})

    def execute(self, context):
        try:
            if context.active_object is None: raise codec.AnimationError("Select a source-node armature")
            report = export_clip(context.active_object, self.filepath)
            self.report({"INFO"}, "%d changed bytes; runtime unverified" % report["audit"]["changed_bytes"])
        except (ValueError, IndexError) as ex:
            self.report({"ERROR"}, str(ex)); return {"CANCELLED"}
        return {"FINISHED"}


def _import_menu(self, context): self.layout.operator(PO_OT_import_clip.bl_idname, text="Punch-Out!! Source-node Animation (.dict)")
def _export_menu(self, context): self.layout.operator(PO_OT_export_clip.bl_idname, text="Punch-Out!! Source-node Animation (.dict)")


def register():
    for cls in (PO_OT_import_clip, PO_OT_export_clip): bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(_import_menu)
    bpy.types.TOPBAR_MT_file_export.append(_export_menu)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(_import_menu)
    bpy.types.TOPBAR_MT_file_export.remove(_export_menu)
    for cls in (PO_OT_export_clip, PO_OT_import_clip): bpy.utils.unregister_class(cls)
