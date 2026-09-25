"""Blender effect graph and local-origin inspection; audited texture-reference export.

Markers show ownership and (where proven) local spawn offsets. They are not a
particle simulation, fighter attachment reconstruction, or a game render.
"""
import json
from pathlib import Path

import bpy
from bpy.props import StringProperty, EnumProperty
from bpy_extras.io_utils import ImportHelper, ExportHelper

import nlg_asset
import po_errors
from nlg_effect import Effects, ResourceIndex, names_for, EffectFormatError
import po_archive


def _refuse(message, **where):
    """EffectFormatError that names archive, section, marker object and field."""
    return po_errors.located(EffectFormatError(message), **where)

SCHEMA = 1
_choices = []


def _document(path):
    doc = nlg_asset.AssetDocument(str(Path(path).resolve()))
    if doc.sections: doc.names = names_for(doc.sections[0].archive)
    return doc


def view_specs(doc):
    """Deterministic graph manifest, independently recreated when collecting edits."""
    specs = []
    for section in doc.sections:
        fx = Effects(section.archive, doc.names)
        for group in fx.groups:
            gkey = f"s{section.index}/g{group.index}"
            title = doc.name(group.name_hash) if group.name_hash is not None else "Unsupported effect"
            specs.append({"key": gkey, "parent": None, "name": title, "role": "group",
                          "location": [group.index % 8 * 5, group.index // 8 * 5, section.index * 5],
                          "section": section.index, "chunk": group.chunk, "layout": group.layout,
                          "note": group.reason or "Ownership diagram. Each group is separated for inspection."})
            for e in group.emitters:
                ekey = gkey + f"/e{e.index}"
                specs.append({"key": ekey, "parent": gkey, "name": doc.name(e.name_hash), "role": "emitter",
                              "location": [0, e.index * 0.3, 0], "section": section.index, "chunk": e.chunk,
                              "texture_hash": e.texture_hash, "model_hash": e.model_hash,
                              "layout": e.layout, "source_record_sha256": nlg_asset.sha256(e.raw),
                              "note": fx.texture_swap_reason(e) or "Only the texture reference can be exported.",
                              "atlas_cells": e.atlas_cells,
                              "parameters": [{"chunk": p.chunk, "curve_chunk": p.curve_chunk, "segments": len(p.segments)}
                                             for p in e.parameters]})
            for s in group.binding_sets:
                for b in s.bindings:
                    e = group.emitters[b.emitter_index]
                    origin = b.origin_preview(e)
                    specs.append({"key": gkey + f"/b{s.index}.{b.index}", "parent": gkey + f"/e{e.index}",
                                  "name": f"Binding {s.index + 1}.{b.index + 1}", "role": "binding",
                                  "location": list(origin) if origin is not None else [0, 0, 0],
                                  "section": section.index, "chunk": b.chunk, "offset": b.offset,
                                  "attachment_hash": b.attachment_hash, "attachment_name": doc.name(b.attachment_hash),
                                  "note": "Local spawn offset in source axes; attachment/world transform unresolved." if origin is not None else
                                          "Spawn volume variant not reconstructed; marker is at the diagram origin."})
    return specs


def import_effects(source, dependencies=()):
    doc = _document(source)
    specs = view_specs(doc)
    if not specs: raise EffectFormatError("Archive contains no effect groups with verified table ownership.")
    root = bpy.data.collections.new("PO Effects " + Path(source).stem)
    bpy.context.scene.collection.children.link(root)
    root["po_effect_document"] = json.dumps({"schema": SCHEMA, "source": str(doc.path.resolve()), "sha256": doc.source_hashes})
    resources = ResourceIndex()
    effects_by_section = {s.index: Effects(s.archive, doc.names) for s in doc.sections}
    for section in doc.sections: resources.add(section.archive, section.index)
    for path in dependencies:
        dependency = _document(path)
        for section in dependency.sections: resources.add(section.archive, section.index)
    by_key = {}; emitters = []
    for spec in specs:
        obj = bpy.data.objects.new(spec["name"], None); root.objects.link(obj)
        obj.empty_display_type = "PLAIN_AXES" if spec["role"] == "group" else "SPHERE" if spec["role"] == "emitter" else "CUBE"
        obj.empty_display_size = 0.15 if spec["role"] != "binding" else 0.06
        obj.show_name = True
        if spec["parent"] is not None: obj.parent = by_key[spec["parent"]]
        obj.location = spec["location"]
        obj["po_effect"] = json.dumps(spec, sort_keys=True)
        obj["po_effect_source"] = str(doc.path.resolve())
        if spec["role"] == "emitter":
            obj["po_effect_texture"] = f"{spec['texture_hash']:08X}"
            fx = effects_by_section[spec["section"]]
            e = fx.emitter(spec["chunk"])
            obj["po_effect_references"] = json.dumps(resources.references(e))
            # A display colour only; the full 25-sample ramp remains in the inspector.
            obj.color = [v / 255 for v in e.colour_samples[0]]
            emitters.append(obj)
        by_key[spec["key"]] = obj
    bpy.context.scene["po_effect_source"] = str(doc.path.resolve())
    bpy.context.view_layer.update()
    return doc, root, emitters


def collect_effect_patches(source):
    doc = _document(source)
    if not doc.editable: raise EffectFormatError(doc.limitation or "Container is read-only.")
    roots = []
    for collection in bpy.context.scene.collection.children_recursive:
        if "po_effect_document" not in collection: continue
        try: meta = json.loads(collection["po_effect_document"])
        except (ValueError, TypeError): raise _refuse("Effect collection metadata is invalid.", archive=doc.path, field="po_effect_document")
        if meta.get("source") == str(doc.path.resolve()): roots.append((collection, meta))
    if len(roots) != 1: raise _refuse("Exactly one imported effect graph must match the source.", archive=doc.path, field="effect collections")
    root, meta = roots[0]
    if meta != {"schema": SCHEMA, "source": str(doc.path.resolve()), "sha256": doc.source_hashes}:
        raise _refuse("Effect graph source fingerprint or schema changed.", archive=doc.path, field="po_effect_document")
    specs = {s["key"]: s for s in view_specs(doc)}
    objects = {}; patches = []; fx = Effects(doc.sections[0].archive, doc.names)
    for obj in root.all_objects:
        where = dict(archive=doc.path, mesh=obj.name)
        if "po_effect" not in obj: raise _refuse("New objects cannot be exported as particle records.", field="po_effect", **where)
        try: spec = json.loads(obj["po_effect"])
        except (ValueError, TypeError): raise _refuse("Effect object metadata is invalid.", field="po_effect", **where)
        where["section"] = spec.get("section")
        key = spec.get("key")
        if key not in specs or spec != specs[key] or key in objects:
            raise _refuse("Effect object identity changed or was duplicated.", field="po_effect key", **where)
        objects[key] = obj
        if obj.data is not None or obj.constraints or obj.animation_data:
            raise _refuse("Particle geometry, constraints, and animation export are not supported.",
                          field="object data/constraints/animation", **where)
        if any(abs(a - b) > 1e-6 for a, b in zip(obj.location, spec["location"])) or \
                any(abs(v) > 1e-6 for v in obj.rotation_euler) or any(abs(v - 1) > 1e-6 for v in obj.scale) or \
                obj.rotation_mode != "XYZ" or any(abs(v) > 1e-6 for v in obj.delta_location) or \
                any(abs(v) > 1e-6 for v in obj.delta_rotation_euler) or any(abs(v - 1) > 1e-6 for v in obj.delta_scale):
            raise _refuse("Moved display markers cannot be exported; only texture references are writable.",
                          field="marker transform", **where)
        if any(abs(obj.matrix_parent_inverse[r][c] - (1 if r == c else 0)) > 1e-6 for r in range(4) for c in range(4)):
            raise _refuse("Changed display parenting cannot be exported.", field="parent inverse", **where)
        if spec["role"] == "emitter":
            value = obj.get("po_effect_texture")
            if not isinstance(value, str) or len(value) != 8 or any(c not in "0123456789abcdefABCDEF" for c in value):
                raise _refuse("Choose a valid compatible texture from the effect panel.", field="po_effect_texture", **where)
            new = int(value, 16)
            if new != spec["texture_hash"]:
                with po_errors.context(field="po_effect_texture (emitter chunk %d)" % spec["chunk"], **where):
                    patches.extend(fx.texture_patches(spec["chunk"], spec["texture_hash"], new))
    if set(objects) != set(specs): raise _refuse("Effect graph objects are missing; deletion is not an export operation.",
                                                 archive=doc.path, field="effect markers")
    for key, obj in objects.items():
        parent = specs[key]["parent"]
        if obj.parent != (objects[parent] if parent else None):
            raise _refuse("Effect ownership parenting changed.", archive=doc.path, mesh=obj.name, field="parent")
    return nlg_asset.PatchSet(doc.source_hashes, patches)


def export_effects(source, destination):
    source = Path(source).resolve(); destination = Path(destination)
    if destination.suffix.lower() != ".dict": raise EffectFormatError("Choose a .dict output path.")
    protected = source.parent
    for parent in source.parents:
        if (parent / "hashid.bin").exists(): protected = parent
        if (parent / "sys").is_dir() and (parent / "files").is_dir(): protected = parent; break
    destination = po_archive.external(destination, protected)
    for path in (destination, destination.with_suffix(".data"), destination.with_suffix(".patchset.json")):
        if path.exists(): raise _refuse("Choose a new output name; existing files are preserved.", archive=source, field="output path")
    ps = collect_effect_patches(source)
    original_reasons = [g.reason for g in Effects(po_archive.load_archive(source)).groups]
    report = nlg_asset.write_rebuild(source, ps, destination)
    rebuilt = Effects(po_archive.load_archive(destination))
    if [g.reason for g in rebuilt.groups] != original_reasons: raise EffectFormatError("Rebuilt effect graph failed validation.")
    return report


def _texture_items(self, context):
    global _choices
    _choices = []
    obj = context.active_object
    if not obj or "po_effect" not in obj: return _choices
    spec = json.loads(obj["po_effect"])
    doc = _document(obj["po_effect_source"])
    fx = Effects(doc.sections[spec["section"]].archive, doc.names)
    _choices = [(f"{t.hash:08X}", t.name, f"{t.width} x {t.height}") for t in fx.texture_candidates(spec["chunk"])]
    return _choices


class IMPORT_OT_punchout_effects(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.punchout_effects"
    bl_label = "Import Punch-Out!! Effect Graph"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".dict"
    filter_glob: StringProperty(default="*.dict", options={"HIDDEN"})
    dependency: StringProperty(name="Optional shared effects archive", subtype="FILE_PATH")

    def execute(self, context):
        try:
            _, root, emitters = import_effects(self.filepath, [self.dependency] if self.dependency else [])
            self.report({"INFO"}, f"Imported {len(emitters)} emitter markers. Ownership/local offsets only; no simulation.")
            return {"FINISHED"}
        except (ValueError, OSError) as ex:
            self.report({"ERROR"}, str(ex)); return {"CANCELLED"}


class EXPORT_OT_punchout_effects(bpy.types.Operator, ExportHelper):
    bl_idname = "export_scene.punchout_effects"
    bl_label = "Export Punch-Out!! Effect Textures"
    filename_ext = ".dict"
    filter_glob: StringProperty(default="*.dict", options={"HIDDEN"})
    source_dict: StringProperty(name="Source archive", subtype="FILE_PATH")

    def invoke(self, context, event):
        self.source_dict = self.source_dict or context.scene.get("po_effect_source", "")
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        try:
            report = export_effects(self.source_dict, self.filepath)
            self.report({"INFO"}, f"Exported {report['audit']['changed_bytes']} changed bytes; runtime unverified.")
            return {"FINISHED"}
        except (ValueError, OSError) as ex:
            self.report({"ERROR"}, str(ex)); return {"CANCELLED"}


class PO_EFFECT_OT_texture(bpy.types.Operator):
    bl_idname = "po.effect_texture"
    bl_label = "Choose compatible effect picture"
    bl_options = {"REGISTER", "UNDO"}
    texture: EnumProperty(name="Picture", items=_texture_items)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        try:
            obj = context.active_object; spec = json.loads(obj["po_effect"])
            doc = _document(obj["po_effect_source"])
            fx = Effects(doc.sections[spec["section"]].archive, doc.names)
            fx.texture_patches(spec["chunk"], spec["texture_hash"], int(self.texture, 16))
            obj["po_effect_texture"] = self.texture
            return {"FINISHED"}
        except (ValueError, OSError) as ex:
            self.report({"ERROR"}, str(ex)); return {"CANCELLED"}


class PO_EFFECT_PT_inspect(bpy.types.Panel):
    bl_label = "Effect inspection"
    bl_idname = "PO_PT_effect_inspect"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "PO Tools"

    @classmethod
    def poll(cls, context): return context.active_object and "po_effect" in context.active_object

    def draw(self, context):
        obj = context.active_object; spec = json.loads(obj["po_effect"])
        self.layout.label(text=spec["name"])
        self.layout.label(text=spec["role"].title() + ": " + spec.get("layout", "local offset"))
        self.layout.label(text=spec["note"])
        if spec["role"] == "emitter":
            row = self.layout.row()
            row.enabled = spec["note"] == "Only the texture reference can be exported."
            row.operator("po.effect_texture")
            self.layout.label(text="Texture: " + obj["po_effect_texture"])
        self.layout.label(text="Inspection markers; no game simulation.")


def _import_menu(self, context): self.layout.operator(IMPORT_OT_punchout_effects.bl_idname, text="Punch-Out!! Effect Graph (.dict)")
def _export_menu(self, context): self.layout.operator(EXPORT_OT_punchout_effects.bl_idname, text="Punch-Out!! Effect Textures (.dict)")
CLASSES = (IMPORT_OT_punchout_effects, EXPORT_OT_punchout_effects, PO_EFFECT_OT_texture, PO_EFFECT_PT_inspect)


def register():
    for cls in CLASSES: bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(_import_menu); bpy.types.TOPBAR_MT_file_export.append(_export_menu)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(_export_menu); bpy.types.TOPBAR_MT_file_import.remove(_import_menu)
    for cls in reversed(CLASSES): bpy.utils.unregister_class(cls)
