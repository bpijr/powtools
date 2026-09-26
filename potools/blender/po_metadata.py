"""Versioned scene metadata for the Blender plug-in (pure Python; bpy lives in po_scene.py).

Before this schema the tools left scattered custom properties behind: scene keys naming the
last source (`po_export_source_dict`, `po_cinematic_source`, `po_effect_source`), legacy Workshop
session keys (`po_workshop_*`), per-collection summaries and per-ID provenance (`po_asset`,
`po_material`, `po_skeleton`, `po_node`, ...). One scene document now records what was
imported and what the panels may offer:

    scene["po_metadata"] = {"format": "po-scene", "version": 1,
        "assets": [{id, kind, archive, name, sha256, collections, objects, content, import}],
        "active", "workshop", "preferences", "last_review", "last_export", "last_refusal",
        "legacy_keys"}

Per-ID provenance stays on the ID that owns it (it must travel with duplicated or linked
data, and the exporters check it byte-for-byte); `KEYS` registers every one of those keys
with its holder and asset kind, and the typed readers below are the only way the UI reads
them. `migrate(snapshot, stored)` reads a scene saved with only the old keys (version 0)
or a stored document and always returns a version-1 document; newer versions are refused.

A snapshot is a plain dict made by po_scene.snapshot(scene) (or by hand in tests):
    {"scene": {key: value}, "collections": [{"name", "props", "children"}],
     "roots": [top-level collection names], "objects": [{"name", "type", "props", "roots",
     "parent", "armature", "bones"}], "materials": [{"name", "props"}], "images": [...]}
"""
import json
import os
import re
from dataclasses import dataclass

FORMAT = "po-scene"
VERSION = 1
PROPERTY = "po_metadata"
KINDS = ("model_sets", "fighter", "cinematic", "effects", "animation", "cutscene")
KIND_LABELS = {"model_sets": "Model sets (one object per mesh)", "fighter": "Fighter, combined",
               "cinematic": "Cinematic cameras", "effects": "Effect graph", "animation": "Source-node animation",
               "cutscene": "Whole cutscene (actors, props, camera)"}


class MetadataError(ValueError):
    pass


@dataclass(frozen=True)
class Key:
    name: str        # property name; a trailing * matches a numbered/named family
    holder: str      # scene, collection, object, bone, material, image, node, node_group, action
    kind: str        # asset kind it belongs to, or "scene" for document-level state
    meaning: str
    scene_level: bool = False   # migrated into the document (not per-ID provenance)


KEYS = (
    Key(PROPERTY, "scene", "scene", "This versioned scene document"),
    Key("po_export_source_dict", "scene", "model_sets", "Last imported model-set/fighter source", True),
    Key("po_cinematic_source", "scene", "cinematic", "Last imported cinematic source", True),
    Key("po_cinematic_output", "scene", "cinematic", "Last cinematic export path", True),
    Key("po_effect_source", "scene", "effects", "Last imported effect source (scene and markers)", True),
    Key("po_workshop_source", "scene", "scene", "Workshop session source archive", True),
    Key("po_workshop_output", "scene", "scene", "Workshop session output (returned.dict)", True),
    Key("po_workshop_mode", "scene", "scene", "Workshop session mode (asset or fighter)", True),
    Key("po_material_state", "scene", "fighter", "Fighter damage-state preview (NORMAL/HURT); also on materials", True),
    Key("po_show_damage", "scene", "fighter", "Fighter damage artwork toggle (mirrors po_material_state)", True),
    Key("po_asset_document", "collection", "model_sets", "Asset summary (nlg_asset.AssetDocument.summary)"),
    Key("po_cinematic_root", "collection", "cinematic", "Cinematic import inventory and session"),
    Key("po_effect_document", "collection", "effects", "Effect graph source fingerprint"),
    Key("po_cutscene", "collection", "cutscene", "Cutscene import: shots, actors, arena and session"),
    Key("po_cutscene_actor", "object", "cutscene", "Cutscene actor/prop/helper object and its clip identity"),
    Key("po_cutscene_camera", "object", "cutscene", "Cutscene camera and the shots it follows"),
    Key("po_asset", "object", "model_sets", "Per-mesh provenance (nlg_asset.mesh_metadata)"),
    Key("po_source_dict", "object", "fighter", "Source archive of a fighter mesh/armature or model-set mesh"),
    Key("po_mesh_tris", "object", "fighter", "Triangles per archive mesh slot in the combined fighter mesh"),
    Key("po_skeleton", "object", "model_sets", "Bind-pose armature provenance"),
    Key("po_animation", "object", "animation", "Source-node clip provenance and channel map"),
    Key("po_cinematic_path", "object", "cinematic", "Editable sampled camera path"),
    Key("po_cinematic_view", "object", "cinematic", "Locked preview camera"),
    Key("po_cinematic_preview_pose", "object", "cinematic", "Last refreshed preview pose"),
    Key("po_effect", "object", "effects", "Effect marker specification"),
    Key("po_effect_texture", "object", "effects", "Staged emitter texture hash"),
    Key("po_effect_references", "object", "effects", "Resolved emitter references (inspection)"),
    Key("po_node", "bone", "fighter", "Archive node index of a fighter bone"),
    Key("po_rest_head", "bone", "fighter", "Imported rest head (reshape diff)"),
    Key("po_rest_quat", "bone", "fighter", "Imported rest rotation (reshape diff)"),
    Key("po_material", "material", "model_sets", "Generic: shader record provenance JSON; fighter: True"),
    Key("po_material_edits", "material", "model_sets", "Staged texture-input edits"),
    Key("po_field_*", "material", "model_sets", "Bounded shader input controls"),
    Key("po_preview_note", "material", "model_sets", "Preview limitation note"),
    Key("po_blend_preview", "material", "model_sets", "Approximate effect blend policy"),
    Key("po_texture_sequence_error", "material", "model_sets", "Unsupported IFL record diagnostic"),
    Key("po_texture_sequence", "node", "model_sets", "Packed images and IFL hold durations"),
    Key("po_texture_atlas", "node", "model_sets", "Packed persistent IFL atlas image"),
    Key("po_effect_archive", "object", "cutscene", "Fighter effect dependency"),
    Key("po_effect_candidates", "object", "cutscene", "Effect groups matching helper name"),
    Key("po_effect_link_note", "object", "cutscene", "Helper effect binding evidence and limits"),
    Key("po_effect_binding", "object", "cutscene", "Emitter marker source and attachment"),
    Key("po_runtime_preview", "object", "cutscene", "Non-exported runtime arena instance or billboard"),
    Key("po_runtime_asset", "object", "cutscene", "Decoded runtime asset provenance and reference-fitted root placement"),
    Key("po_runtime_node", "object", "cutscene", "Decoded animation node followed by this runtime mesh"),
    Key("po_runtime_counts", "collection", "cutscene", "Decoded 0x6000 animated-arena instance counts by rig"),
    Key("po_crowd", "object", "cutscene", "Impostor crowd mesh: member count, layout settings and sprite atlas shape"),
    Key("po_crowd_bake", "object", "cutscene", "What the crowd impostor bake needs: files root, arena, RNG state, views used"),
    Key("po_crowd_baked", "object", "cutscene", "True once the impostor atlases have been baked and linked"),
    Key("po_crowd_source", "object", "cutscene", "Cutscene archive the impostor crowd was laid out for"),
    Key("po_tev_light_vector", "object", "fighter", "World-space TEV key-light vector preview control"),
    Key("po_note", "object", "scene", "Human-readable non-exported preview note"),
    Key("po_input_slot", "node", "model_sets", "Shader input slot of a preview image node"),
    Key("po_texture", "image", "model_sets", "Texture table entry provenance"),
    Key("po_vertex_source", "mesh attribute", "model_sets", "1-based source vertex per point (topology provenance)"),
    Key("po_damage_mesh", "material", "fighter", "Hurt-state material"),
    Key("po_optional_damage", "material", "fighter", "Optional damage overlay hidden in Normal state"),
    Key("po_outline", "material", "fighter", "Inverted-hull outline material (not exported)"),
    Key("po_hurt_keys", "shape keys", "fighter", "Shape keys the Normal/Hurt toggle sets (static hurt targets)"),
    Key("po_tint", "material", "fighter", "Skin tint"),
    Key("po_spec_power", "material", "fighter", "Specular power"),
    Key("po_alpha", "material", "fighter", "Alpha"),
    Key("po_preset", "material", "fighter", "Shader preset"),
    Key("po_tex_slot*", "material", "fighter", "Texture name per material slot"),
    Key("po_slot_hash*", "material", "fighter", "Texture hash per material slot"),
    Key("po_fp_*", "material", "fighter", "Fingerprints of ramps/images/params at import (and *_shown: last ramp edit auto-previewed)"),
    Key("po_formula", "node", "fighter", "GX formula a shader-group frame implements (documentation only)"),
    Key("po_version", "node_group", "fighter", "Version of the shared PO Skin TexGen/TEV node groups; rebuilt when it changes"),
    Key("po_mesh_material", "material", "fighter", "Source mesh material name"),
    Key("po_matoff", "material", "fighter", "Material record offset"),
    Key("po_slot", "material", "fighter", "Archive mesh slot"),
    Key("po_record", "material", "fighter", "Original 204-byte material record (hex)"),
    Key("po_animation_note", "action", "animation", "Limitation note on imported actions"),
    Key("po_clip_index", "window manager", "scene", "UI only: clip index for Load clip (not saved)"),
)


def key_for(name):
    for k in KEYS:
        if k.name == name or (k.name.endswith("*") and name.startswith(k.name[:-1])):
            return k
    return None


def _norm(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path)))) if path else ""


def asset_id(kind, archive):
    return "%s:%s" % (kind, _norm(archive))


def _json(value):
    if isinstance(value, str):
        try: return json.loads(value)
        except ValueError: return None
    return value if isinstance(value, dict) else None


# ---------------------------------------------------------------------------------------
# Typed per-ID readers

def object_role(obj):
    """(kind, archive or None, role) for one snapshot object, from its own keys only."""
    p = obj.get("props", {})
    for key in ("po_cutscene_actor", "po_cutscene_camera"):     # before fighter/model-set keys actors also carry
        meta = _json(p.get(key))
        if meta is not None:
            return "cutscene", meta.get("source"), meta.get("role", "camera")
    meta = _json(p.get("po_asset"))
    if meta is not None:
        return "model_sets", meta.get("source", {}).get("path"), "mesh"
    meta = _json(p.get("po_animation"))
    if meta is not None:
        return "animation", meta.get("source"), "node rig"
    for key, role in (("po_cinematic_path", "camera path"), ("po_cinematic_view", "preview camera")):
        meta = _json(p.get(key))
        if meta is not None:
            return "cinematic", meta.get("source"), role
    if "po_effect" in p:
        spec = _json(p["po_effect"]) or {}
        return "effects", p.get("po_effect_source"), "effect %s" % spec.get("role", "marker")
    if "po_skeleton" in p:
        return "model_sets", None, "skeleton"
    if p.get("po_source_dict") and (obj.get("type") == "ARMATURE" or "po_mesh_tris" in p):
        return "fighter", p["po_source_dict"], "armature" if obj.get("type") == "ARMATURE" else "combined mesh"
    return None, None, None


def material_role(mat):
    """'model_sets' (generic shader inputs), 'fighter' (legacy 204-byte), 'outline' or None."""
    p = mat.get("props", {})
    if p.get("po_outline"): return "outline"
    value = p.get("po_material")
    if isinstance(value, str) and _json(value) is not None: return "model_sets"
    if value or p.get("po_record") or "po_slot" in p: return "fighter"
    return None


def generic_material(mat):
    """Parsed generic material provenance, or None."""
    return _json(mat.get("props", {}).get("po_material")) if material_role(mat) == "model_sets" else None


# ---------------------------------------------------------------------------------------
# Document

def new_document():
    return {"format": FORMAT, "version": VERSION, "assets": [], "active": None, "workshop": None,
            "preferences": {"material_state": "NORMAL"}, "last_review": None, "last_export": None,
            "last_refusal": None, "legacy_keys": []}


def load(value):
    """Parse a stored document (text or dict). Returns None when absent; refuses newer ones."""
    if value is None or value == "": return None
    doc = _json(value)
    if doc is None or doc.get("format") != FORMAT:
        raise MetadataError("Scene metadata is not a po-scene document.")
    version = doc.get("version")
    if not isinstance(version, int) or version < 1:
        raise MetadataError("Scene metadata has an invalid version: %r" % (version,))
    if version > VERSION:
        raise MetadataError("Scene metadata version %d was written by a newer tool (this tool reads %d)."
                            % (version, VERSION))
    out = new_document(); out.update({k: v for k, v in doc.items() if k in out})
    if not isinstance(out["assets"], list) or any(not isinstance(a, dict) or a.get("kind") not in KINDS
                                                   for a in out["assets"]):
        raise MetadataError("Scene metadata asset list is malformed.")
    return out


def dump(doc):
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def _summary_content(summary):
    """Content counts from the model-set import summary (nlg_asset.AssetDocument.summary)."""
    sections = summary.get("sections", [])
    return {"source": "import summary", "sections": len(sections), "editable": summary.get("editable"),
            "limitation": summary.get("limitation"),
            "model_sets": [{"section": s["index"], "index": m["index"], "meshes": m["meshes"],
                            "skinned": m["skinned"], "bones": m["bones"], "nodes": m["nodes"]}
                           for s in sections for m in s.get("model_sets", [])],
            "textures": sum(s.get("textures", 0) for s in sections),
            "rigs": sum(s.get("rigs", 0) for s in sections),
            "clips": sum(s.get("animation_clips", 0) for s in sections),
            "cameras": None, "effect_groups": None, "definitions": None}


def derive_assets(snapshot):
    """Assets implied by the per-ID keys in the scene (the version-0 layout)."""
    assets = {}; order = []

    def asset(kind, archive):
        key = asset_id(kind, archive)
        if key not in assets:
            order.append(key)
            assets[key] = {"id": key, "kind": kind, "archive": str(archive),
                           "name": os.path.splitext(os.path.basename(str(archive)))[0],
                           "sha256": None, "collections": [], "objects": 0, "roles": {},
                           "content": None, "import": None}
        return assets[key]

    root_asset = {}
    for col in snapshot.get("collections", []):
        p = col.get("props", {})
        for key, kind, path in (("po_asset_document", "model_sets", "path"),
                                ("po_cinematic_root", "cinematic", "source"),
                                ("po_effect_document", "effects", "source"),
                                ("po_cutscene", "cutscene", "source")):
            meta = _json(p.get(key))
            if meta is None or not meta.get(path): continue
            a = asset(kind, meta[path])
            a["collections"].append(col["name"]); root_asset[col["name"]] = a
            if meta.get("sha256"): a["sha256"] = meta["sha256"]
            if kind == "model_sets" and a["content"] is None: a["content"] = _summary_content(meta)
    for obj in snapshot.get("objects", []):
        kind, archive, role = object_role(obj)
        owner = None
        if kind and archive:
            owner = asset(kind, archive)
        else:
            owner = next((root_asset[r] for r in obj.get("roots", []) if r in root_asset), None)
            if owner is None and kind is None: continue
            if owner is None:            # skeleton without a recorded root: leave unassigned
                continue
        owner["objects"] += 1
        owner["roles"][role or "other"] = owner["roles"].get(role or "other", 0) + 1
        if kind == "animation" and not owner["collections"]:
            owner["collections"] = list(obj.get("roots", []))
        if owner["sha256"] is None:
            p = obj.get("props", {}); meta = _json(p.get("po_asset"))
            if meta: owner["sha256"] = meta.get("source", {}).get("sha256")
            meta = _json(p.get("po_animation")) or _json(p.get("po_cinematic_path"))
            if meta: owner["sha256"] = meta.get("sha256")
    return [assets[k] for k in order]


def _legacy_workshop(scene):
    if not scene.get("po_workshop_source"): return None
    output = scene.get("po_workshop_output") or ""
    return {"source": str(scene["po_workshop_source"]), "output": str(output),
            "mode": str(scene.get("po_workshop_mode") or "fighter"),
            "session": os.path.dirname(str(output)) if output else ""}


def legacy_keys(snapshot):
    found = set()
    for holder in ("collections", "objects", "materials", "images"):
        for item in snapshot.get(holder, []):
            for name in item.get("props", {}):
                if key_for(name) and name != PROPERTY: found.add(key_for(name).name)
            for bone in item.get("bones", []):
                for name in bone.get("props", {}):
                    if key_for(name): found.add(key_for(name).name)
    for name in snapshot.get("scene", {}):
        if key_for(name) and name != PROPERTY: found.add(key_for(name).name)
    return sorted(found)


def migrate(snapshot, stored=None):
    """Version-1 document from a stored document (any supported version) plus the per-ID keys
    actually present. Assets whose objects were all deleted disappear; stored details
    (content, import options, sha) survive for assets that remain."""
    scene = snapshot.get("scene", {})
    previous = load(stored if stored is not None else scene.get(PROPERTY))
    doc = new_document()
    if previous is not None:
        doc.update({k: previous[k] for k in ("active", "workshop", "preferences", "last_review",
                                               "last_export", "last_refusal", "legacy_keys")})
    else:
        doc["legacy_keys"] = legacy_keys(snapshot)
    old = {a["id"]: a for a in (previous or {}).get("assets", [])}
    for a in derive_assets(snapshot):
        prior = old.get(a["id"])
        if prior:
            for k in ("sha256", "import"):
                if a.get(k) is None and prior.get(k) is not None: a[k] = prior[k]
            if prior.get("content") and (a["content"] is None or prior["content"].get("source") == "archive"):
                a["content"] = prior["content"]
        doc["assets"].append(a)
    if doc["workshop"] is None:
        doc["workshop"] = _legacy_workshop(scene)
    if previous is None and scene.get("po_material_state") in ("NORMAL", "HURT"):
        doc["preferences"]["material_state"] = scene["po_material_state"]
    ids = [a["id"] for a in doc["assets"]]
    if doc["active"] not in ids:
        hint = scene.get("po_export_source_dict") or scene.get("po_cinematic_source") or scene.get("po_effect_source")
        match = [i for i in ids if hint and i.split(":", 1)[1] == _norm(hint)]
        doc["active"] = (match or ids or [None])[-1]
    return doc


def find_asset(doc, ident):
    return next((a for a in doc.get("assets", []) if a["id"] == ident), None)


def asset_for_object(doc, obj):
    """The document asset owning one snapshot object (by its own keys, then its root collection)."""
    kind, archive, _ = object_role(obj)
    if kind and archive:
        return find_asset(doc, asset_id(kind, archive))
    for a in doc.get("assets", []):
        if set(a.get("collections", [])) & set(obj.get("roots", [])):
            return a
    return None


def workshop_asset(doc):
    w = doc.get("workshop")
    if not w: return None
    hits = [a for a in doc.get("assets", []) if _norm(a["archive"]) == _norm(w["source"])]
    want = "fighter" if w.get("mode") == "fighter" else None
    return next((a for a in hits if a["kind"] == want), None) or (hits[0] if hits else None)


# ---------------------------------------------------------------------------------------
# What an asset holds and which controls apply

def content_from_archive(path, names=None):
    """Counts for the summary and panel polls, read through the shared loaders (no bpy)."""
    import nlg_asset
    import nlg_cinematic
    doc = nlg_asset.AssetDocument(path, names)
    content = _summary_content(doc.summary()); content["source"] = "archive"
    content["sha256"] = doc.source_hashes
    cameras = 0; groups = 0; scripts = combos = 0; notes = []
    for s in doc.sections:
        try: cameras += len(nlg_cinematic.camera_clips(s.archive))
        except ValueError as ex: notes.append("section %d cameras: %s" % (s.index, ex))
        try:
            from nlg_effect import Effects
            groups += len(Effects(s.archive).groups)
        except ValueError as ex: notes.append("section %d effects: %s" % (s.index, ex))
        if s.definitions is not None:
            scripts += len(s.definitions.scripts); combos += len(s.definitions.combos)
        elif s.definition_note: notes.append("section %d definitions: %s" % (s.index, s.definition_note))
    content.update(cameras=cameras, effect_groups=groups, definitions={"scripts": scripts, "combos": combos},
                   notes=notes)
    return content


def profile(asset):
    """Which panels and controls apply. Unknown counts (None) never enable a control."""
    kind = asset["kind"] if asset else None
    c = (asset or {}).get("content") or {}
    sets = c.get("model_sets") or []
    n = lambda key: c.get(key) or 0
    defs = c.get("definitions") or {}
    fighter = kind == "fighter"
    cutscene = kind == "cutscene"
    meshes = fighter or cutscene or (kind == "model_sets" and any(s.get("meshes") for s in sets))
    skinned = fighter or any(s.get("skinned") and s.get("bones") for s in sets)
    rigged = fighter or cutscene or kind == "animation" or (kind == "model_sets" and (skinned or n("rigs") or n("clips")))
    timeline = kind == "cinematic"
    cameras = timeline or cutscene or (kind == "model_sets" and n("sections") > 1 and n("cameras") > 0)
    effect_graph = kind == "effects"
    effects = effect_graph or (kind == "model_sets" and n("effect_groups") > 0)
    behavior = fighter or bool(defs.get("scripts") or defs.get("combos"))
    if kind == "model_sets" and c.get("editable") is False:
        export, reason = False, c.get("limitation") or "Read-only container."
    elif kind == "model_sets" and c.get("model_sets") == [] and not n("textures"):
        export, reason = False, ("This archive has no models or textures to edit in Blender"
                                 + ("; its behavior definitions are edited through nlg_behavior." if behavior else "."))
    elif kind is None:
        export, reason = False, "Import a Punch-Out!! asset first."
    else:
        export, reason = True, None
    return {"kind": kind, "fighter": fighter, "cutscene": cutscene, "meshes": meshes, "skinned": skinned, "rig": rigged,
            "clips": n("clips") > 0, "timeline": timeline, "cameras": cameras, "effect_graph": effect_graph,
            "effects": effects, "behavior": behavior, "materials": fighter or meshes,
            "textures": fighter or n("textures") > 0, "exportable": export, "export_note": reason,
            "fighter_controls": fighter}


def capability_families(asset):
    kind = asset["kind"]; c = asset.get("content") or {}; p = profile(asset)
    out = ["Cinematic containers (NIS, multi-section)" if (c.get("sections") or 1) > 1 else
           "Archive containers (single section)"]
    if kind == "fighter":
        out += ["Fighter geometry (main model)", "Fighter materials (hippodiffuseskin, 204-byte)",
                "Skeleton and skinning", "Animation clips", "Textures (CMPR)", "Crowd, HUD, lighting and damage-state"]
    if kind == "model_sets" and p["meshes"]:
        out += ["Model sets (all models, nodes, transforms)", "Model topology and culling bounds",
                "Shader material inputs (12 observed layouts)"]
        if p["textures"]: out.append("Blender texture painting through patch sets")
        if p["skinned"]: out.append("Skeleton and skinning")
    if kind in ("model_sets", "animation") and (c.get("rigs") or kind == "animation"):
        out.append("Generic animation node rigs")
    if kind == "animation" or (kind == "model_sets" and c.get("clips")):
        out.append("Source-node rotation, translation and scale keys")
    if kind == "cutscene":
        out += ["Generic animation node rigs", "Source-node rotation, translation and scale keys"]
    if kind != "fighter" and (kind in ("cinematic", "cutscene") or c.get("cameras")):
        out.append("Cameras")
    if p["effects"]: out.append("Particles and effects (0x40xx)")
    if (c.get("definitions") or {}).get("scripts") or (c.get("definitions") or {}).get("combos"):
        out.append("Fighter behavior definitions")
    return list(dict.fromkeys(out))


def capabilities(asset):
    """[(family, status, note)] from po_capability; unknown family names are skipped."""
    import po_capability as capability
    rows = []
    for family in capability_families(asset):
        try: c = capability.by_family(family)
        except KeyError: continue
        rows.append((c.family, c.status(), c.note))
    return rows


# ---------------------------------------------------------------------------------------
# Plain-language change review

_RULES = (
    ("Geometry", re.compile(r"model set (\d+), mesh (\d+): (\d+) (vertex position|normal|UV)s? changed")),
    ("Geometry", re.compile(r"model set (\d+), mesh (\d+): rebuild topology \((\d+) vertices, (\d+) faces\)")),
    ("Geometry", re.compile(r"culling bounds")),
    ("Placement", re.compile(r"model set (\d+): transform (\d+) changed")),
    ("Textures", re.compile(r"texture (\d+): pixels")),
    ("Materials", re.compile(r"material @(\d+) (.+) \((\d+) mesh users?\)")),
    ("Animation keys", re.compile(r"^(?:section \d+: )?(.+): node (\d+) (\w+) key (\d+) changed")),
    ("Camera samples", re.compile(r"camera (.+): (position|target|rotation|angle) sample (\d+)")),
    ("Effects", re.compile(r"effect emitter (\d+): texture ([0-9A-F]+) -> ([0-9A-F]+)")),
)
ORDER = ("Geometry", "Placement", "Textures", "Materials", "Animation keys", "Camera samples", "Effects", "Other")
LOCKED = {
    "model_sets": "Whole mesh/node insertion or removal, skin-weight edits and multi-section geometry stay locked.",
    "fighter": "The combined fighter exporter rewrites geometry and changed materials; untouched materials are copied from their imported records.",
    "cinematic": "Lens, rotation, timing and clip length stay locked; only existing position/target samples change.",
    "effects": "Only compatible local emitter texture swaps are written; markers are display-only.",
    "animation": "Only existing sampled key values change; key counts, timing and rig structure stay locked.",
    "cutscene": "Shot timing, frame counts, helpers, face-morph tracks and the actors' own archives stay locked; "
                "a node channel without a source track cannot gain one.",
}


def review(lines, kind=None, archive=None):
    """(headline, [(heading, [plain lines])]) from PatchSet.review() lines."""
    groups = {h: [] for h in ORDER}; keys = {}; cameras = {}
    for line in lines:
        if line.startswith("No changes"): continue
        for heading, rule in _RULES:
            m = rule.search(line)
            if not m: continue
            if heading == "Animation keys":
                clip, node, kind_ = m.group(1), m.group(2), m.group(3)
                entry = keys.setdefault(clip, {"keys": 0, "nodes": set(), "kinds": set()})
                entry["keys"] += 1; entry["nodes"].add(node); entry["kinds"].add(kind_)
            elif heading == "Camera samples":
                cameras.setdefault((m.group(1), m.group(2)), set()).add(int(m.group(3)))
            else:
                groups[heading].append(line)
            break
        else:
            groups["Other"].append(line)
    for clip, e in sorted(keys.items()):
        groups["Animation keys"].append("%s: %d key%s changed on %d node%s (%s)" % (
            clip, e["keys"], "s" if e["keys"] != 1 else "", len(e["nodes"]), "s" if len(e["nodes"]) != 1 else "",
            ", ".join(sorted(e["kinds"]))))
    for (camera, track), samples in sorted(cameras.items()):
        groups["Camera samples"].append("camera %s: %d %s sample%s moved (first %d, last %d)" % (
            camera, len(samples), track, "s" if len(samples) != 1 else "", min(samples), max(samples)))
    sections = [(h, groups[h]) for h in ORDER if groups[h]]
    where = os.path.basename(str(archive)) if archive else "the source"
    if not sections:
        headline = "No changes: the export will be byte-identical to %s." % where
    else:
        count = sum(len(v) for _, v in sections)
        headline = "%d change%s in %s. Everything else is copied unchanged from %s." % (
            count, "s" if count != 1 else "", ", ".join(h.lower() for h, _ in sections), where)
    if kind in LOCKED:
        sections.append(("Stays locked", [LOCKED[kind]]))
    return headline, sections


def fighter_review(summary, archive=None):
    """Plain review for the combined fighter path, which has no patch set.

    summary: {"meshes": [names], "vertices": int, "source_slots": int, "changed_materials": [names],
              "reshaped_bones": [names], "actions": int}"""
    sections = []
    geo = ["%d mesh object%s, %d vertices, rewritten into %d archive mesh slot%s" % (
        len(summary.get("meshes", [])), "s" if len(summary.get("meshes", [])) != 1 else "",
        summary.get("vertices", 0), summary.get("source_slots", 0), "s" if summary.get("source_slots", 0) != 1 else "")]
    sections.append(("Geometry", geo))
    if summary.get("changed_materials"):
        sections.append(("Materials", ["%d edited: %s" % (len(summary["changed_materials"]),
                                                          ", ".join(summary["changed_materials"][:6]))]))
    if summary.get("reshaped_bones"):
        sections.append(("Skeleton", ["%d reshaped bone%s: %s" % (
            len(summary["reshaped_bones"]), "s" if len(summary["reshaped_bones"]) != 1 else "",
            ", ".join(summary["reshaped_bones"][:6]))]))
    sections.append(("Stays locked", [LOCKED["fighter"]]))
    mats, bones = len(summary.get("changed_materials", [])), len(summary.get("reshaped_bones", []))
    headline = ("Fighter export of %s: geometry is always rewritten; %d edited material%s, %d reshaped bone%s."
                % (os.path.basename(str(archive)) if archive else "the source",
                   mats, "s" if mats != 1 else "", bones, "s" if bones != 1 else ""))
    return headline, sections


def audit_lines(report):
    """Plain lines for an export report (write_rebuild report, or the fighter chunk audit)."""
    audit = (report or {}).get("audit") or {}
    out = []
    if audit.get("audit_mode") == "chunk comparison":
        out.append("%d byte%s differ in %d chunk%s; the combined fighter path rewrites geometry and edited materials."
                   % (audit["changed_bytes"], "s" if audit["changed_bytes"] != 1 else "", len(audit["changed_chunks"]),
                      "s" if len(audit["changed_chunks"]) != 1 else ""))
    elif "changed_bytes" in audit:
        out.append("%d byte%s changed in %d patch%s; everything else matches the source." % (
            audit["changed_bytes"], "s" if audit["changed_bytes"] != 1 else "", audit.get("patches", 0),
            "es" if audit.get("patches", 0) != 1 else ""))
    for c in audit.get("changed_chunks", [])[:8]:
        out.append("chunk %s (%s): %d -> %d bytes%s" % (c["chunk"], c["type"], c["old_bytes"], c["new_bytes"],
                                                        ", resized" if c.get("resized") else ""))
    if audit.get("derived_culling_patches"):
        out.append("%d culling bound update(s) follow the geometry." % len(audit["derived_culling_patches"]))
    if report and report.get("deterministic"):
        out.append("A second rebuild produced identical bytes.")
    validation = (report or {}).get("validation")
    if validation:
        out.append("Validation: %s" % ("passed" if validation.get("passed") else "FAILED"))
        out += [w for w in validation.get("warnings", [])[:3]]
    out.append("Runtime unverified: no game has loaded this output yet.")
    return out


def chunk_audit(source, output):
    """Chunk-level comparison for exports without a patch set (the combined fighter path)."""
    import po_archive
    a, b = po_archive.load_archive(source), po_archive.load_archive(output)
    changes = []; changed = 0
    for ri in b.find_chunks():
        new = b.get_chunk_bytes(ri)
        if ri >= len(a.chunks) or a.chunks[ri][2] != b.chunks[ri][2]:
            changes.append({"chunk": ri, "type": "0x%04X" % b.chunks[ri][2], "old_bytes": 0, "new_bytes": len(new),
                            "resized": True}); changed += len(new); continue
        old = a.get_chunk_bytes(ri)
        if old == new: continue
        diff = sum(x != y for x, y in zip(old, new)) + abs(len(old) - len(new))
        changes.append({"chunk": ri, "type": "0x%04X" % b.chunks[ri][2], "old_bytes": len(old), "new_bytes": len(new),
                        "resized": len(old) != len(new)}); changed += diff
    return {"changed_chunks": changes, "changed_bytes": changed, "patches": 0, "audit_mode": "chunk comparison"}
