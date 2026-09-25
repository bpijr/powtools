"""PO Tools: one import entry, one export entry, and context-aware panels (Blender 3.6 and 5.x).

File > Import > Punch-Out!! Asset (.dict) imports any archive; the mode chooses model sets,
the combined fighter path, cinematic cameras, the effect graph or one source-node clip.
File > Export > Punch-Out!! Asset (.dict) exports the active asset through its own audited
path to a new external file. A plain-language change review is shown before export and the audit after.

View3D > Sidebar > PO Tools holds eight panels. Each polls the active asset's metadata
(po_metadata profile) and shows only what applies: a static prop has no rig, damage or
retarget controls; a fighter has no cinematic timeline. The per-format panels of the other
modules (material inputs, cinematic samples, effect inspection) become sub-panels here, and
the older per-format File menu entries are folded into the single entries above; their
operators stay available for scripts and F3.
"""
import json
import os
import sys
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper
from mathutils import Vector

import po_errors
import po_metadata as meta
import po_scene

CATEGORY = "PO Tools"
SHORT = {"model_sets": "model sets", "fighter": "fighter", "cinematic": "cinematic", "effects": "effects",
         "animation": "animation", "cutscene": "cutscene"}
MODES = (("AUTO", "Automatic", "Whole cutscene for NIS files with shots, the fighter path for boxers and the referee, model sets for other models; otherwise the effect graph or cameras"),
         ("CUTSCENE", "Whole cutscene", "Every shot on one timeline: actors, props, helpers, the camera and the arena; edits export back to the NIS"),
         ("MODEL_SETS", "Model sets (one object per mesh)", "Every model set placed by its node transforms, skeletons, textures and shader inputs"),
         ("FIGHTER", "Fighter, combined", "The fighter path: one combined mesh, rig, animations, damage state and bone reshaping"),
         ("CINEMATIC", "Cinematic cameras", "Camera clips as editable sampled paths with locked preview cameras"),
         ("EFFECTS", "Effect graph", "Effect groups, emitters and bindings as inspection markers"),
         ("ANIMATION", "Source-node animation clip", "One clip on its exact source node hierarchy"))
MODE_KIND = {"MODEL_SETS": "model_sets", "FIGHTER": "fighter", "CINEMATIC": "cinematic", "EFFECTS": "effects",
             "ANIMATION": "animation", "CUTSCENE": "cutscene"}
ARENAS = (("AUTO", "Fighter's circuit", "The arena of the cutscene's fighter (the NIS does not name one)"),
          ("minorcircuit", "Minor circuit", ""), ("majorcircuit", "Major circuit", ""),
          ("worldcircuit", "World circuit", ""), ("traininggym", "Training gym", ""), ("NONE", "No arena", ""))
LEGACY_MENUS = (("io_import_punchout", "menu_func", "import"), ("io_export_punchout", "menu_func", "export"),
                ("io_punchout_asset", "_menu_import", "import"), ("io_punchout_asset", "_menu_export", "export"),
                ("io_punchout_animation", "_import_menu", "import"), ("io_punchout_animation", "_export_menu", "export"),
                ("io_punchout_cinematic", "menu_import", "import"), ("io_punchout_cinematic", "menu_export", "export"),
                ("io_punchout_effects", "_import_menu", "import"), ("io_punchout_effects", "_export_menu", "export"))
ADOPT = (("PO_PT_material_inputs", "PO_PT_materials"), ("PO_PT_cinematic", "PO_PT_cameras"),
         ("PO_PT_effect_inspect", "PO_PT_effects"))
FIGHTER_OPTIONS = ("bake_colors", "shade_floor", "neutralize_lighting", "write_skeleton", "export_materials",
                   "allow_new_textures")


def selected_bones(context, arm):
    """Selected bone names in any mode; Blender 5.x moved pose selection off Bone.select."""
    if context.mode == "EDIT_ARMATURE":
        return [eb.name for eb in arm.data.edit_bones if eb.select]
    if arm.pose is None: return []
    return [pb.name for pb in arm.pose.bones
            if (pb.select if hasattr(pb, "select") else getattr(pb.bone, "select", False))]


def _layered_actions():
    return bpy.app.version >= (4, 4, 0)


def _state(context):
    doc, asset = po_scene.active_asset(context)
    return doc, asset, meta.profile(asset)


def _wrap(layout, lines, width=60):
    for line in lines:
        text = str(line)
        while len(text) > width:
            cut = text.rfind(" ", 0, width)
            cut = cut if cut > 20 else width
            layout.label(text=text[:cut]); text = "   " + text[cut:].lstrip()
        layout.label(text=text)


# ---------------------------------------------------------------------------------------
# Import

def is_fighter(path):
    """Boxer-style archive: body and shadow-volume models, a bind skeleton, facial morphs and an
    animation rig. Props, ropes and belts carry one model and use the model-set importer."""
    import po_archive
    try:
        a = po_archive.load_archive(path)
    except (ValueError, OSError):
        return False
    types = [a.chunks[i][2] for i in a.find_chunks()]
    return types.count(0xB004) >= 2 and all(t in types for t in (0xB00A, 0xB00C, 0x8001))


def detect_mode(path):
    content = meta.content_from_archive(path)
    if (content.get("sections") or 1) > 1 and content.get("cameras"):
        import nlg_asset, nlg_cutscene
        try:
            if nlg_cutscene.shots(nlg_asset.AssetDocument(path)): return "CUTSCENE", content
        except ValueError:
            pass
    if content["model_sets"]: return ("FIGHTER" if is_fighter(path) else "MODEL_SETS"), content
    if content.get("effect_groups"): return "EFFECTS", content
    if content.get("cameras"): return "CINEMATIC", content
    return "MODEL_SETS", content


def import_any(path, mode="AUTO", **options):
    """Run one import mode and record it in the scene document. Returns (kind, message)."""
    path = str(Path(path).resolve())
    with po_errors.context(archive=path):
        if mode == "AUTO":
            mode, _ = detect_mode(path)
        kind = MODE_KIND[mode]
        if kind == "model_sets":
            import io_punchout_asset
            doc, root, created = io_punchout_asset.import_asset(path, options.get("load_textures", True))
            message = "Imported %d meshes into %s." % (len(created), root.name) + ("" if doc.editable else " Read-only: %s" % doc.limitation)
        elif kind == "fighter":
            import io_import_punchout
            io_import_punchout.do_import(path, options.get("import_anims", True), options.get("anim_filter", ""),
                                         options.get("load_textures", True), options.get("import_outline", True))
            message = "Imported the fighter as one combined mesh with its rig."
        elif kind == "cinematic":
            import io_punchout_cinematic
            _, _, paths, cameras = io_punchout_cinematic.import_cinematic(path)
            message = "Imported %d preview cameras and %d editable sampled paths." % (len(cameras), len(paths))
        elif kind == "cutscene":
            import io_punchout_cutscene
            cs, _, notes = io_punchout_cutscene.import_cutscene(path, options.get("arena", "AUTO"), options.get("load_textures", True),
                                                               options.get("import_outline", True))
            counts = {k: sum(1 for a in cs.actors if a.kind == k) for k in ("character", "prop", "helper")}
            message = ("Imported %d shots over %d frames: %d actors, %d props, %d helpers and the camera."
                       % (len(cs.shots), cs.frames, counts["character"], counts["prop"], counts["helper"])
                       + ("" if not notes else " " + " ".join(notes)))
        elif kind == "effects":
            import io_punchout_effects
            dep = options.get("dependency") or ""
            _, _, emitters = io_punchout_effects.import_effects(path, [bpy.path.abspath(dep)] if dep else [])
            message = "Imported %d emitter markers (ownership and local offsets only)." % len(emitters)
        else:
            import io_punchout_animation
            io_punchout_animation.import_clip(path, options.get("clip_index", 0), options.get("section_index", 0))
            message = "Imported one source-node clip."
    po_scene.record_import(bpy.context.scene, kind, path, {k: v for k, v in options.items() if isinstance(v, (int, float, str, bool))})
    return kind, message


class PO_OT_import_any(bpy.types.Operator, ImportHelper):
    """Import any Punch-Out!! archive: models, a fighter, cameras, effects or an animation clip"""
    bl_idname = "import_scene.punchout_any"
    bl_label = "Import Punch-Out!! Asset"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".dict"
    filter_glob: StringProperty(default="*.dict", options={"HIDDEN"})
    mode: EnumProperty(name="Mode", items=MODES, default="AUTO")
    load_textures: BoolProperty(name="Texture previews", default=True)
    import_anims: BoolProperty(name="Animations", default=True)
    anim_filter: StringProperty(name="Only animations containing", default="",
                                description="Comma-separated substrings; blank imports every clip")
    import_outline: BoolProperty(name="Toon outline", default=True)
    clip_index: IntProperty(name="Clip", default=0, min=0)
    section_index: IntProperty(name="Section", default=0, min=0)
    dependency: StringProperty(name="Shared effects archive", subtype="FILE_PATH", default="")
    arena: EnumProperty(name="Arena", items=ARENAS, default="AUTO")

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "mode")
        if self.mode in ("AUTO", "MODEL_SETS", "FIGHTER", "CUTSCENE"):
            col.prop(self, "load_textures")
        if self.mode in ("AUTO", "CUTSCENE"):
            col.prop(self, "arena"); col.prop(self, "import_outline")
        if self.mode == "FIGHTER":
            col.prop(self, "import_anims"); sub = col.column(); sub.enabled = self.import_anims
            sub.prop(self, "anim_filter"); col.prop(self, "import_outline")
        if self.mode == "ANIMATION":
            col.prop(self, "section_index"); col.prop(self, "clip_index")
        if self.mode == "EFFECTS":
            col.prop(self, "dependency")
        _wrap(col, [next(m[2] for m in MODES if m[0] == self.mode)], 40)

    def execute(self, context):
        options = {k: getattr(self, k) for k in ("load_textures", "import_anims", "anim_filter", "import_outline",
                                                  "clip_index", "section_index", "dependency", "arena")}
        try:
            kind, message = import_any(self.filepath, self.mode, **options)
        except Exception as ex:     # every failure is reported, located and kept for the export panel
            if isinstance(ex, ValueError): po_errors.located(ex, archive=self.filepath)
            po_scene.record(context.scene, "last_refusal", po_errors.as_record(ex))
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        self.report({"INFO"}, message)
        return {"FINISHED"}


# ---------------------------------------------------------------------------------------
# Review and export

def _asset_rigs(context, asset):
    return [o for o in po_scene.asset_objects(context.scene, asset) if "po_animation" in o]


def _fighter_armature(context, asset):
    return next((o for o in po_scene.asset_objects(context.scene, asset) if o.type == "ARMATURE"), None)


def fighter_summary(context, asset):
    import po_shader
    arm = _fighter_armature(context, asset)
    if arm is None: raise po_errors.Refusal("The fighter armature is missing from the scene.", archive=asset["archive"])
    meshes = [o for o in context.scene.objects if o.type == "MESH" and any(
        m.type == "ARMATURE" and m.object == arm for m in o.modifiers)]
    slots = max((len(o["po_mesh_tris"]) for o in meshes if "po_mesh_tris" in o), default=0)
    changed = sorted({m.name for o in meshes for m in o.data.materials if m is not None and m.get("po_record")
                      and not po_shader.material_is_pristine(m)})
    reshaped = []
    for b in arm.data.bones:
        head = b.get("po_rest_head")
        if head is not None and (b.matrix_local.translation - Vector(head)).length > 1e-5:
            reshaped.append(b.name)
    return {"meshes": [o.name for o in meshes], "vertices": sum(len(o.data.vertices) for o in meshes),
            "source_slots": slots, "changed_materials": changed, "reshaped_bones": reshaped}


def collect(context, asset):
    """(headline, sections, patch set or None) for the asset's pending export."""
    source = asset["archive"]; kind = asset["kind"]
    with po_errors.context(archive=source):
        if not os.path.isfile(source):
            raise po_errors.Refusal("The source archive is not at its imported path.", field="source path")
        if kind == "fighter":
            headline, sections = meta.fighter_review(fighter_summary(context, asset), source)
            return headline, sections, None
        if kind == "model_sets":
            import io_punchout_asset
            ps, _ = io_punchout_asset.collect_patch_set(source)
        elif kind == "cinematic":
            import io_punchout_cinematic
            ps = io_punchout_cinematic.collect_patch_set(source)
        elif kind == "cutscene":
            import io_punchout_cutscene
            ps = io_punchout_cutscene.collect_patch_set(source)
        elif kind == "effects":
            import io_punchout_effects
            ps = io_punchout_effects.collect_effect_patches(source)
        else:
            import io_punchout_animation, nlg_asset
            rigs = _asset_rigs(context, asset)
            if not rigs: raise po_errors.Refusal("No imported source-node rig for this archive.", field="rig")
            patches = []; doc = None
            for rig in rigs:
                with po_errors.context(mesh=rig.name):
                    doc, part = io_punchout_animation.collect_patch_set(rig)
                patches += part.patches
            ps = nlg_asset.PatchSet(doc.source_hashes, patches)
    headline, sections = meta.review(ps.review(), kind, source)
    return headline, sections, ps


def _fighter_export(context, asset, output, options):
    try:
        result = bpy.ops.export_scene.punchout_mod(filepath=str(output), source_dict=asset["archive"], **options)
    except RuntimeError as ex:          # the legacy operator reports its refusal as an error
        raise po_errors.Refusal(str(ex).replace("Error:", "").strip(), field="fighter export") from ex
    if "FINISHED" not in result:
        raise po_errors.Refusal("The fighter exporter refused; its message is in the Info log.", field="fighter export")
    report = {"audit": meta.chunk_audit(asset["archive"], output)}
    import po_archive
    try: report["validation"] = po_archive.validate(output, asset["archive"])
    except (ValueError, OSError, IndexError, KeyError) as ex: report["validation"] = {"passed": False, "warnings": [str(ex)]}
    return report


def export(context, asset, output, options=None):
    """Write the asset's changes to `output`; returns the report shown in the panel."""
    source = asset["archive"]; kind = asset["kind"]; output = Path(output)
    with po_errors.context(archive=source):
        if kind == "fighter":
            report = _fighter_export(context, asset, output, options or {})
        elif kind == "model_sets":
            import io_punchout_asset
            report, _ = io_punchout_asset.export_asset(source, str(output))
        elif kind == "cinematic":
            import io_punchout_cinematic
            report = io_punchout_cinematic.export_cinematic(source, output)
        elif kind == "cutscene":
            import io_punchout_cutscene
            report = io_punchout_cutscene.export_cutscene(source, output)
        elif kind == "effects":
            import io_punchout_effects
            report = io_punchout_effects.export_effects(source, output)
        else:
            import nlg_asset
            _, _, ps = collect(context, asset)
            report = nlg_asset.write_rebuild(source, ps, output)
    report = dict(report); report["output"] = str(output)
    return report


def _store_review(context, asset):
    try:
        headline, sections, _ = collect(context, asset)
    except Exception as ex:
        if isinstance(ex, ValueError): po_errors.located(ex, archive=asset["archive"])
        po_scene.record(context.scene, "last_refusal", po_errors.as_record(ex), asset)
        return None, ex
    value = {"headline": headline, "sections": [[h, rows] for h, rows in sections]}
    po_scene.record(context.scene, "last_review", value, asset)
    return value, None


def _target_asset(context, ident=""):
    doc, asset = po_scene.active_asset(context)
    return meta.find_asset(doc, ident) if ident else asset


class PO_OT_bake_crowd(bpy.types.Operator):
    """Bake the arena crowd's animated impostor sprites (one atlas every Nth frame, plus each shot start)"""
    bl_idname = "po.bake_crowd"
    bl_label = "Bake crowd animation"
    bl_options = {"REGISTER", "UNDO"}
    step: IntProperty(name="Every Nth frame", default=3, min=1, max=30,
                      description="Each baked frame takes about 10 s; the frames between are held")

    @classmethod
    def poll(cls, context):
        return any("po_crowd_bake" in o for o in context.scene.objects)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        import io_punchout_cutscene, po_crowd
        scene = context.scene
        obj = next(o for o in scene.objects if "po_crowd_bake" in o)
        frames = set(range(scene.frame_start, scene.frame_end + 1, self.step))
        try:                        # never hold an atlas across a camera cut
            shots = json.loads(scene.camera[io_punchout_cutscene.CAMERA])["shots"]
            frames |= {shot[2] + 1 for shot in shots}
        except (TypeError, KeyError, ValueError):
            pass
        frames = sorted(frames)
        wm = context.window_manager; wm.progress_begin(0, len(frames))
        try:
            po_crowd.bake(scene, obj, frames=frames)
        finally:
            wm.progress_end()
        self.report({"INFO"}, "Baked %d crowd atlases (every %d frames)." % (len(frames), self.step))
        return {"FINISHED"}


class PO_OT_review(bpy.types.Operator):
    """Compare the scene with the source archive and list, in plain language, what an export would change"""
    bl_idname = "po.review_changes"
    bl_label = "Review changes"
    asset: StringProperty(default="", options={"HIDDEN"})

    def execute(self, context):
        asset = _target_asset(context, self.asset)
        if asset is None: self.report({"ERROR"}, "Import a Punch-Out!! asset first."); return {"CANCELLED"}
        value, ex = _store_review(context, asset)
        if ex is not None: self.report({"ERROR"}, str(ex)); return {"CANCELLED"}
        self.report({"INFO"}, value["headline"]); return {"FINISHED"}


class PO_OT_export_any(bpy.types.Operator, ExportHelper):
    """Export the active Punch-Out!! asset to a new external file"""
    bl_idname = "export_scene.punchout_any"
    bl_label = "Export Punch-Out!! Asset"
    filename_ext = ".dict"
    filter_glob: StringProperty(default="*.dict", options={"HIDDEN"})
    asset: StringProperty(default="", options={"HIDDEN"})
    bake_colors: BoolProperty(name="Bake material colors into ramps", default=True)
    shade_floor: FloatProperty(name="Shading floor", default=1.0, min=0.0, max=1.0)
    neutralize_lighting: BoolProperty(name="Neutralize lighting maps", default=True)
    write_skeleton: BoolProperty(name="Write bind joint positions (skeleton)", default=True)
    export_materials: BoolProperty(name="Export materials + textures", default=True)
    allow_new_textures: BoolProperty(name="Allow new textures", default=True)

    def _resolve(self, context):
        doc = po_scene.document(context.scene)
        asset = _target_asset(context, self.asset)
        return doc, asset

    def invoke(self, context, event):
        doc, asset = self._resolve(context)
        if asset is None:
            self.report({"ERROR"}, "Import a Punch-Out!! asset first."); return {"CANCELLED"}
        self.asset = asset["id"]
        _store_review(context, asset)
        self.filepath = ((asset["kind"] in ("cinematic", "cutscene") and context.scene.get("po_cinematic_output")) or
                         os.path.join(os.path.expanduser("~"), "%s_edit.dict" % asset["name"]))
        return ExportHelper.invoke(self, context, event)

    def draw(self, context):
        doc, asset = self._resolve(context)
        col = self.layout.column()
        if asset is None: col.label(text="No asset selected", icon="ERROR"); return
        col.label(text="%s · %s" % (asset["name"], meta.KIND_LABELS[asset["kind"]]), icon="EXPORT")
        col.label(text="Target: a new external file")
        _draw_review(col, doc, asset)
        if asset["kind"] == "fighter":
            box = col.box()
            for name in FIGHTER_OPTIONS: box.prop(self, name)

    def execute(self, context):
        doc, asset = self._resolve(context)
        if asset is None: self.report({"ERROR"}, "Import a Punch-Out!! asset first."); return {"CANCELLED"}
        profile = meta.profile(asset)
        if not profile["exportable"]:
            self.report({"ERROR"}, po_errors.format_message(profile["export_note"], archive=asset["archive"]))
            return {"CANCELLED"}
        output = bpy.path.abspath(self.filepath)
        options = {k: getattr(self, k) for k in FIGHTER_OPTIONS}
        try:
            report = export(context, asset, output, options)
        except Exception as ex:
            if isinstance(ex, ValueError): po_errors.located(ex, archive=asset["archive"])
            po_scene.record(context.scene, "last_refusal", po_errors.as_record(ex), asset)
            self.report({"ERROR"}, str(ex)); return {"CANCELLED"}
        lines = meta.audit_lines(report)
        po_scene.record(context.scene, "last_refusal", None)
        po_scene.record(context.scene, "last_export", {"output": report["output"], "target": "file",
                                                        "lines": lines, "passed": (report.get("validation") or {}).get("passed")}, asset)
        for line in lines: print("PO export:", line)
        self.report({"INFO"}, lines[0])
        return {"FINISHED"}


# ---------------------------------------------------------------------------------------
# Small helpers used by the panels

class PO_OT_scene_sync(bpy.types.Operator):
    """Re-read the scene's Punch-Out!! metadata and the source archives' contents"""
    bl_idname = "po.scene_sync"
    bl_label = "Refresh asset summary"

    def execute(self, context):
        doc = po_scene.sync(context.scene)
        self.report({"INFO"}, "%d asset(s) recorded (metadata version %d)" % (len(doc["assets"]), meta.VERSION))
        return {"FINISHED"}


class PO_OT_pick_asset(bpy.types.Operator):
    """Make this asset the one the panels describe"""
    bl_idname = "po.pick_asset"
    bl_label = "Show asset"
    asset: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        doc = po_scene.set_active(context.scene, self.asset)
        asset = meta.find_asset(doc, self.asset)
        objs = po_scene.asset_objects(context.scene, asset) if asset else []
        context.view_layer.objects.active = objs[0] if objs else None
        return {"FINISHED"}


class PO_OT_select_model_set(bpy.types.Operator):
    """Select the mesh objects of one model set"""
    bl_idname = "po.select_model_set"
    bl_label = "Select model set"
    bl_options = {"REGISTER", "UNDO"}
    section: IntProperty(default=0)
    model_set: IntProperty(default=0)

    def execute(self, context):
        _, asset, _ = _state(context)
        if asset is None: return {"CANCELLED"}
        hits = []
        for o in po_scene.asset_objects(context.scene, asset):
            record = meta._json(o.get("po_asset"))
            if record and record["model_set"] == self.model_set and record["source"]["section"] == self.section:
                hits.append(o)
        for o in context.view_layer.objects: o.select_set(o in hits)
        if hits: context.view_layer.objects.active = hits[0]
        self.report({"INFO"}, "%d mesh object(s) selected" % len(hits))
        return {"FINISHED"}


class PO_OT_load_view(bpy.types.Operator):
    """Load another view of the active asset's archive (cameras, effect markers or one clip)"""
    bl_idname = "po.load_view"
    bl_label = "Load view"
    bl_options = {"REGISTER", "UNDO"}
    mode: EnumProperty(items=[m[:3] for m in MODES if m[0] in ("CINEMATIC", "EFFECTS", "ANIMATION")])
    clip_index: IntProperty(name="Clip", default=0, min=0)
    section_index: IntProperty(name="Section", default=0, min=0)

    def execute(self, context):
        _, asset, _ = _state(context)
        if asset is None: return {"CANCELLED"}
        try:
            _, message = import_any(asset["archive"], self.mode, clip_index=self.clip_index, section_index=self.section_index)
        except (ValueError, OSError, IndexError) as ex:
            po_scene.record(context.scene, "last_refusal", po_errors.as_record(ex), asset)
            self.report({"ERROR"}, str(ex)); return {"CANCELLED"}
        self.report({"INFO"}, message); return {"FINISHED"}


class PO_OT_copy_path(bpy.types.Operator):
    """Copy this path to the clipboard"""
    bl_idname = "po.copy_path"
    bl_label = "Copy path"
    path: StringProperty()

    def execute(self, context):
        context.window_manager.clipboard = self.path
        self.report({"INFO"}, "Copied " + self.path); return {"FINISHED"}


# ---------------------------------------------------------------------------------------
# Panels

class _Panel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    needs = None             # profile flag that must be true

    @classmethod
    def poll(cls, context):
        try:
            _, asset, profile = _state(context)
        except Exception:
            return False
        return asset is not None and (cls.needs is None or bool(profile.get(cls.needs)))

    def draw(self, context):
        try:
            doc, asset, profile = _state(context)
            self.body(context, self.layout, doc, asset, profile)
        except Exception as ex:          # a broken record must not blank the sidebar
            self.layout.label(text="Could not read this asset: %s" % ex, icon="ERROR")


def _summary_rows(asset):
    c = asset.get("content") or {}
    sets = c.get("model_sets") or []
    rows = ["Sections: %s" % c.get("sections", "?"),
            "Model sets: %d (%d meshes, %d skinned)" % (len(sets), sum(s.get("meshes", 0) for s in sets),
                                                         sum(1 for s in sets if s.get("skinned") and s.get("bones")))]
    for key, label in (("textures", "Textures"), ("rigs", "Animation rigs"), ("clips", "Animation clips"),
                       ("cameras", "Camera clips"), ("effect_groups", "Effect groups")):
        if c.get(key) is not None: rows.append("%s: %d" % (label, c[key]))
    defs = c.get("definitions")
    if defs is not None: rows.append("Behavior definitions: %d scripts, %d combo graphs" % (defs.get("scripts", 0), defs.get("combos", 0)))
    return rows


class PO_PT_tools(_Panel, bpy.types.Panel):
    """Asset summary: what was imported, where it came from, and what the tools can claim"""
    bl_idname = "PO_PT_tools"
    bl_label = "Asset summary"
    bl_order = 0

    @classmethod
    def poll(cls, context):
        return True

    def body(self, context, col, doc, asset, profile):
        if doc.get("error"): col.label(text=doc["error"], icon="ERROR")
        col.operator(PO_OT_import_any.bl_idname, text="Import Punch-Out!! Asset…", icon="IMPORT")
        if asset is None:
            col.label(text="No Punch-Out!! asset in this scene yet.", icon="INFO"); return
        if po_scene.needs_sync(context.scene): po_scene.schedule_sync()
        if len(doc["assets"]) > 1:
            box = col.box(); box.label(text="Assets in this scene")
            for a in doc["assets"]:
                op = box.operator(PO_OT_pick_asset.bl_idname, text="%s · %s" % (a["name"], SHORT[a["kind"]]),
                                  depress=a["id"] == asset["id"])
                op.asset = a["id"]
        found = os.path.isfile(asset["archive"])
        box = col.box()
        box.label(text="%s · %s" % (asset["name"], meta.KIND_LABELS[asset["kind"]]), icon="FILE")
        if not found: box.label(text="Source missing at its import path", icon="ERROR")
        if asset.get("content_error"): _wrap(box, [asset["content_error"]])
        if any("po_crowd_bake" in o for o in context.scene.objects):
            col.operator(PO_OT_bake_crowd.bl_idname, icon="SEQUENCE")


class PO_PT_details(_Panel, bpy.types.Panel):
    """Archive path, contents and what the tools can claim for this asset"""
    bl_idname = "PO_PT_details"
    bl_label = "Details"
    bl_parent_id = "PO_PT_tools"
    bl_options = {"DEFAULT_CLOSED"}

    def body(self, context, col, doc, asset, profile):
        _wrap(col, ["Archive: " + asset["archive"]])
        if asset.get("content"):
            _wrap(col, _summary_rows(asset))
            if (asset["content"].get("cameras") or 0) and not profile["cameras"] and asset["kind"] != "cinematic":
                _wrap(col, ["Camera clips: load them with Import mode 'Cinematic cameras'."])
        else:
            col.label(text="Archive contents not read yet", icon="TIME")
        caps = col.box(); caps.label(text="Capability")
        for family, status, _ in meta.capabilities(asset):
            _wrap(caps, ["%s: %s" % (family, status)])
        caps.label(text="Runtime: offline checks only; test output in game.", icon="INFO")
        col.operator(PO_OT_scene_sync.bl_idname, icon="FILE_REFRESH")


class PO_PT_models(_Panel, bpy.types.Panel):
    bl_idname = "PO_PT_models"
    bl_label = "Models and meshes"
    bl_order = 2
    needs = "meshes"

    def body(self, context, col, doc, asset, profile):
        obj = context.active_object
        if asset["kind"] == "fighter":
            mesh = next((o for o in po_scene.asset_objects(context.scene, asset) if "po_mesh_tris" in o), None)
            if mesh is not None:
                col.label(text="%s: %d archive mesh slots, %d vertices" % (mesh.name, len(mesh["po_mesh_tris"]), len(mesh.data.vertices)))
            col.label(text="Combined mesh; materials map faces to archive slots.")
            row = col.row(align=True)
            op = row.operator("export_scene.punchout_list_slots", text="List source slots"); op.source_dict = asset["archive"]
            row.operator("po.recover_slots", text="Recover slots")
            return
        c = asset.get("content") or {}
        sets = c.get("model_sets") or []
        for s in sets[:12]:
            row = col.row(align=True)
            label = "Section %d · Model %d: %d meshes, %s" % (s["section"], s["index"], s["meshes"],
                                                            "skinned (%d bones)" % s["bones"] if s["skinned"] and s["bones"] else "static")
            op = row.operator(PO_OT_select_model_set.bl_idname, text=label, icon="RESTRICT_SELECT_OFF")
            op.section = s["section"]; op.model_set = s["index"]
        if len(sets) > 12: col.label(text="… and %d more model sets" % (len(sets) - 12))
        if c.get("editable") is False: _wrap(col, ["Read-only: %s" % c.get("limitation")])
        record = meta._json(obj.get("po_asset")) if obj is not None else None
        if record:
            box = col.box(); box.label(text=obj.name, icon="MESH_DATA")
            _wrap(box, ["Model set %d · node '%s' · mesh %d '%s'" % (record["model_set"], record["node"]["name"],
                                                                      record["mesh"]["index"], record["mesh"]["name"]),
                        "%d source vertices; transform %d" % (record["mesh"]["vertices"], record["transform"]["index"]),
                        "Editable: " + (", ".join(record.get("editable", [])) or "nothing"),
                        "Preserved: " + (", ".join(record.get("preserved", [])) or "nothing")])
            _wrap(box, ["New vertices must copy an imported one (duplicate/extrude); whole mesh or node insertion is refused."])


class PO_PT_materials(_Panel, bpy.types.Panel):
    bl_idname = "PO_PT_materials"
    bl_label = "Materials and textures"
    bl_order = 3
    needs = "materials"

    def body(self, context, col, doc, asset, profile):
        c = asset.get("content") or {}
        if asset["kind"] != "fighter":
            col.label(text="%d textures · select a mesh for its shader inputs" % (c.get("textures") or 0))
        if profile["textures"]:
            _wrap(col, ["Paint textures in Texture Paint; same-size edits export with every mip level."])


class PO_PT_fighter_damage(_Panel, bpy.types.Panel):
    """Normal/Hurt: hurt artwork and hurt shape keys together (combined fighter imports only)"""
    bl_idname = "PO_PT_fighter_damage"
    bl_label = "Normal / Hurt"
    bl_order = 1
    needs = "fighter_controls"

    def body(self, context, col, doc, asset, profile):
        col.row().prop(context.scene, "po_material_state", expand=True)
        keys = next((o.data.shape_keys for o in po_scene.asset_objects(context.scene, asset)
                     if o.type == "MESH" and o.data.shape_keys is not None), None)
        hurt = json.loads(keys["po_hurt_keys"]) if keys is not None and keys.get("po_hurt_keys") else []
        if hurt:
            _wrap(col, ["Hurt shape keys: " + ", ".join(n.split("_", 1)[1] for n in hurt)])
        elif keys is not None:
            col.label(text="No hurt shape keys on this fighter")


class PO_PT_fighter_materials(_Panel, bpy.types.Panel):
    """Step through normal and hurt materials and edit the active one"""
    bl_idname = "PO_PT_fighter_materials"
    bl_label = "Material editor"
    bl_parent_id = "PO_PT_materials"
    bl_options = {"DEFAULT_CLOSED"}
    needs = "fighter_controls"

    def body(self, context, col, doc, asset, profile):
        import io_import_punchout as fighter
        obj = fighter._po_character_mesh(context)
        if obj is None or not obj.material_slots: return
        for state in ("NORMAL", "HURT"):
            nav = col.row(align=True)
            for direction, icon in ((-1, "TRIA_LEFT"), (1, "TRIA_RIGHT")):
                op = nav.operator("po.cycle_material", text="%s %s" % ("Prev" if direction < 0 else "Next", state.title()), icon=icon)
                op.state = state; op.direction = direction
        mat = obj.active_material
        if mat is not None and mat.get("po_record"):
            box = col.box()
            box.label(text="%s: %s" % ("Hurt" if mat.get("po_damage_mesh") else "Normal", mat.name))
            if mat.get("po_mesh_material"): box.label(text=mat["po_mesh_material"])
            nt = mat.node_tree
            for node, label in (("PO_Detail", "Detail / albedo (slot 0)"), ("PO_Damage", "Hurt overlay (slot 1)")):
                n = nt.nodes.get(node) if nt else None
                if n is not None: box.label(text=label); box.template_ID(n, "image", open="image.open")
            ramp = nt.nodes.get("PO_Ramp") if nt else None
            if ramp is not None: box.label(text="Diffuse / toon ramp"); box.template_color_ramp(ramp, "color_ramp", expand=True)
        row = col.row(align=True)
        row.operator("po.make_custom_material", text="Custom material"); row.operator("po.claim_slot", text="Claim slot")


class PO_PT_rig(_Panel, bpy.types.Panel):
    bl_idname = "PO_PT_rig"
    bl_label = "Rig and animation"
    bl_order = 4
    needs = "rig"

    def body(self, context, col, doc, asset, profile):
        c = asset.get("content") or {}
        objs = po_scene.asset_objects(context.scene, asset)
        if asset["kind"] == "cutscene":
            rows = {}
            for o in objs:
                record = meta._json(o.get("po_cutscene_actor"))
                if record: rows.setdefault(record["id"], record)
            for kind, label in (("character", "Actors"), ("prop", "Props"), ("helper", "Helpers (display only)")):
                found = [r for r in rows.values() if r["kind"] == kind]
                if found: _wrap(col, ["%s: %s" % (label, ", ".join(sorted({r["name"] for r in found}))[:120])])
            _wrap(col, ["Pose actor rigs and move props with keys on shot frames; export writes the clips' existing tracks.",
                        "Face shapes, helpers and shot timing are display-only."])
            return
        if asset["kind"] == "animation":
            for rig in [o for o in objs if "po_animation" in o][:4]:
                record = meta._json(rig["po_animation"]) or {}
                ad = rig.animation_data; action = ad.action if ad else None
                box = col.box(); box.label(text=rig.name, icon="ARMATURE_DATA")
                _wrap(box, ["Clip %s (section %s): %d channels" % (record.get("clip"), record.get("section"), len(record.get("channels", []))),
                            "Action: %s" % (action.name if action else "none"),
                            "Edit existing key values; key counts and timing are locked."])
            return
        if asset["kind"] == "fighter":
            arm = _fighter_armature(context, asset)
            ad = arm.animation_data if arm else None
            strips = sum(len(t.strips) for t in ad.nla_tracks) if ad else 0
            col.label(text="%s: %d bones, %d animation strips" % (arm.name if arm else "?", len(arm.data.bones) if arm else 0, strips))
            col.operator("po.recover_nodes", text="Recover bone node IDs")
            return
        for sk in [o for o in objs if "po_skeleton" in o][:6]:
            record = meta._json(sk["po_skeleton"]) or {}
            _wrap(col, ["%s: %d bones · %s" % (sk.name, len(record.get("bones", [])), record.get("hierarchy", ""))])
        if c.get("clips"):
            col.label(text="%d clips on %d rigs in the archive" % (c["clips"], c.get("rigs") or 0))
            row = col.row(align=True)
            op = row.operator(PO_OT_load_view.bl_idname, text="Load clip", icon="ACTION"); op.mode = "ANIMATION"
            op.clip_index = context.window_manager.po_clip_index
            row.prop(context.window_manager, "po_clip_index", text="Clip")
        _wrap(col, ["Skin weights and rig structure are locked; bind matrices are preserved."])


class PO_PT_fighter_rig(_Panel, bpy.types.Panel):
    """Bone reshaping, weight tools and the reshape cheat sheet (combined fighter imports only)"""
    bl_idname = "PO_PT_fighter_rig"
    bl_label = "Fighter bone reshaping"
    bl_parent_id = "PO_PT_rig"
    bl_options = {"DEFAULT_CLOSED"}
    needs = "fighter_controls"

    def body(self, context, col, doc, asset, profile):
        import io_import_punchout as fighter
        arm = context.active_object
        if arm is None or arm.type != "ARMATURE":
            col.label(text="Select the fighter armature to reshape bones", icon="INFO"); return
        if fighter._RETARGET_ERR is not None:
            col.label(text="anim_retarget.py unavailable: %s" % fighter._RETARGET_ERR, icon="ERROR")
        col.operator("po.retool_print_report", text="Print bone report", icon="CONSOLE")
        if context.mode != "EDIT_ARMATURE":
            col.label(text="Tab into Edit mode to reshape", icon="INFO")
        sel = selected_bones(context, arm)
        if sel:
            col.label(text="Selected: " + ", ".join(sel[:4]) + (" …" if len(sel) > 4 else ""), icon="BONE_DATA")
            row = col.row(align=True)
            row.operator("po.retool_scale_y", text="Length ×1.20").scale_val = 1.2
            row.operator("po.retool_scale_y", text="Length ×0.80").scale_val = 0.8
            row = col.row(align=True)
            row.operator("po.retool_scale_x", text="Width ×1.20").scale_val = 1.2
            row.operator("po.retool_scale_x", text="Width ×0.80").scale_val = 0.8
            row = col.row(align=True)
            for label, angle in (("Rotate Z +15°", 15.0), ("Rotate Z -15°", -15.0)):
                op = row.operator("po.retool_rotate", text=label); op.rot_angle = angle; op.rot_axis = "z"
            row = col.row(align=True)
            row.operator("po.retool_move", text="Shift Z +0.01").move_z = 0.01
            row.operator("po.retool_move", text="Shift Z -0.01").move_z = -0.01
            row = col.row(align=True)
            row.operator("po.retool_uniform_scale", text="Uniform ×1.10").scale_factor = 1.1
            row.operator("po.retool_uniform_scale", text="Uniform ×0.90").scale_factor = 0.9
            col.operator("po.retool_restore", text="Restore all originals", icon="LOOP_BACK")
        else:
            col.label(text="Select bones to reshape them", icon="INFO")
        col.separator()
        col.operator("po.retool_auto_weight", text="Auto weight (cluster)", icon="GROUP_VERTEX")
        col.operator("po.retool_transfer_weights", text="Transfer weights from selected mesh", icon="IMPORT")
        box = col.box()
        _wrap(box, ["Change lengths, not hierarchy; keep bone names.", "Animations follow the reshaped rest pose."])


class PO_PT_cameras(_Panel, bpy.types.Panel):
    bl_idname = "PO_PT_cameras"
    bl_label = "Cameras and cinematic timeline"
    bl_order = 5
    needs = "cameras"

    def body(self, context, col, doc, asset, profile):
        c = asset.get("content") or {}
        scene = context.scene
        if asset["kind"] == "cutscene":
            info = next((meta._json(x["po_cutscene"]) for x in bpy.data.collections if "po_cutscene" in x
                         and (meta._json(x["po_cutscene"]) or {}).get("source") == asset["archive"]), None) or {}
            col.label(text="%d shots, frames 1-%d at 30 fps" % (len(info.get("shots", [])), info.get("frames", 0)), icon="SEQUENCE")
            current = next((s for s in reversed(info.get("shots", [])) if s["first"] + 1 <= scene.frame_current), None)
            if current: col.label(text="Shot: section %d · %s (frames %d-%d)" % (current["section"], current["camera"],
                                                                                  current["first"] + 1, current["last"] + 1))
            col.prop(scene, "frame_current", text="Frame")
            if scene.camera is not None: col.label(text="View: " + scene.camera.name, icon="VIEW_CAMERA")
            _wrap(col, ["Key the camera's location, rotation or lens on any shot frame; frame counts stay fixed.",
                        "Arena: %s (the fighter's circuit; the NIS does not name one)." % (info.get("arena") or "none")])
            return
        if not profile["timeline"]:
            col.label(text="%d camera clips in %s sections" % (c.get("cameras") or 0, c.get("sections")))
            col.operator(PO_OT_load_view.bl_idname, text="Load camera paths", icon="CAMERA_DATA").mode = "CINEMATIC"
            return
        scene = context.scene
        paths = [o for o in po_scene.asset_objects(scene, asset) if "po_cinematic_path" in o]
        col.label(text="%d editable sample paths; frames %d-%d" % (len(paths), scene.frame_start, scene.frame_end))
        col.prop(scene, "frame_current", text="Sample frame")
        if scene.camera is not None: col.label(text="View: " + scene.camera.name, icon="VIEW_CAMERA")


class PO_PT_effects(_Panel, bpy.types.Panel):
    bl_idname = "PO_PT_effects"
    bl_label = "Effects"
    bl_order = 6
    needs = "effects"

    def body(self, context, col, doc, asset, profile):
        c = asset.get("content") or {}
        if not profile["effect_graph"]:
            col.label(text="%d effect groups in this archive" % (c.get("effect_groups") or 0))
            col.operator(PO_OT_load_view.bl_idname, text="Load effect graph markers", icon="PARTICLES").mode = "EFFECTS"
            return
        markers = [o for o in po_scene.asset_objects(context.scene, asset) if "po_effect" in o]
        emitters = sum(1 for o in markers if '"role": "emitter"' in o["po_effect"])
        col.label(text="%d markers, %d emitters" % (len(markers), emitters))
        _wrap(col, ["Select an emitter marker to inspect it; only compatible texture swaps export."])


class PO_PT_behavior(_Panel, bpy.types.Panel):
    bl_idname = "PO_PT_behavior"
    bl_label = "Behavior"
    bl_order = 7
    bl_options = {"DEFAULT_CLOSED"}
    needs = "behavior"

    def body(self, context, col, doc, asset, profile):
        defs = (asset.get("content") or {}).get("definitions") or {}
        if defs.get("scripts") or defs.get("combos"):
            col.label(text="%d scripts, %d combo graphs (read-only here)" % (defs.get("scripts", 0), defs.get("combos", 0)))
        _wrap(col, ["Experimental. Fighter behavior lives in characterdefinitions archives; "
                    "TimeScale / BlendAmount edits go through nlg_behavior."])
        for path in related_definitions(asset)[:8]:
            row = col.row(); row.label(text=os.path.basename(path)); row.operator(PO_OT_copy_path.bl_idname, text="", icon="COPYDOWN").path = path


def related_definitions(asset):
    """Definition archives that sit beside a fighter in the dump (found from hashid.bin's folder)."""
    import nlg_hash
    hashid, _ = nlg_hash.find_hashid_bin(asset["archive"])
    if hashid is None: return []
    folder = Path(hashid).parent / "characterdefinitions" / asset["name"]
    return sorted(str(p) for p in folder.glob("*.dict")) if folder.is_dir() else []


def _draw_review(col, doc, asset):
    review = doc.get("last_review")
    if review and review.get("asset") == asset["id"]:
        box = col.box(); _wrap(box, [review["headline"]])
        for heading, rows in review["sections"]:
            box.label(text=heading + ":")
            _wrap(box, ["  " + r for r in rows[:6]] + (["  … and %d more" % (len(rows) - 6)] if len(rows) > 6 else []))


class PO_PT_export(_Panel, bpy.types.Panel):
    bl_idname = "PO_PT_export"
    bl_label = "Validation and export"
    bl_order = 8

    def body(self, context, col, doc, asset, profile):
        col.label(text="Target: new external .dict", icon="FILE_NEW")
        if not profile["exportable"]:
            _wrap(col, ["Export locked: %s" % profile["export_note"]]); return
        row = col.row(align=True)
        row.operator(PO_OT_review.bl_idname, icon="VIEWZOOM").asset = asset["id"]
        op = row.operator(PO_OT_export_any.bl_idname, text="Export…", icon="EXPORT"); op.asset = asset["id"]
        _draw_review(col, doc, asset)
        last = doc.get("last_export")
        if last and last.get("asset") == asset["id"]:
            box = col.box(); box.label(text="Last export: " + os.path.basename(last["output"]),
                                       icon="CHECKMARK" if last.get("passed") is not False else "ERROR")
            _wrap(box, last.get("lines", []))
        refusal = doc.get("last_refusal")
        if refusal and refusal.get("asset") in (None, asset["id"]):
            box = col.box(); box.label(text="Refused", icon="ERROR")
            _wrap(box, po_errors.lines(refusal))


# ---------------------------------------------------------------------------------------
# Registration

CLASSES = (PO_OT_import_any, PO_OT_export_any, PO_OT_bake_crowd, PO_OT_review, PO_OT_scene_sync,
           PO_OT_pick_asset, PO_OT_select_model_set, PO_OT_load_view, PO_OT_copy_path,
           PO_PT_tools, PO_PT_details, PO_PT_fighter_damage, PO_PT_models, PO_PT_materials, PO_PT_fighter_materials,
           PO_PT_rig, PO_PT_fighter_rig,
           PO_PT_cameras, PO_PT_effects, PO_PT_behavior, PO_PT_export)
PANELS = [c for c in CLASSES if issubclass(c, bpy.types.Panel)]
_removed = []
_adopted = []


def _menu_import(self, context):
    self.layout.operator(PO_OT_import_any.bl_idname, text="Punch-Out!! Asset (.dict)")


def _menu_export(self, context):
    self.layout.operator(PO_OT_export_any.bl_idname, text="Punch-Out!! Asset (.dict)")


def _menus(which):
    return bpy.types.TOPBAR_MT_file_import if which == "import" else bpy.types.TOPBAR_MT_file_export


def register():
    po_scene.register()
    bpy.types.WindowManager.po_clip_index = IntProperty(name="Clip", default=0, min=0)
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    for idname, parent in ADOPT:           # the per-format panels become sub-panels
        cls = getattr(bpy.types, idname, None)
        if cls is None or getattr(cls, "bl_parent_id", "") == parent: continue
        bpy.utils.unregister_class(cls); cls.bl_parent_id = parent; bpy.utils.register_class(cls)
        _adopted.append(cls)
    for module, name, which in LEGACY_MENUS:   # one File > Import and one File > Export entry
        fn = getattr(sys.modules.get(module), name, None)
        if fn is not None:
            _menus(which).remove(fn); _removed.append((fn, which))
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)
    bpy.types.TOPBAR_MT_file_export.append(_menu_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(_menu_export)
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    for fn, which in reversed(_removed):
        _menus(which).append(fn)
    _removed.clear()
    for cls in reversed(_adopted):
        try:
            bpy.utils.unregister_class(cls); del cls.bl_parent_id; bpy.utils.register_class(cls)
        except (RuntimeError, AttributeError) as ex:
            print("PO Tools: could not release %s: %s" % (cls.__name__, ex))
    _adopted.clear()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.WindowManager.po_clip_index
    po_scene.unregister()
