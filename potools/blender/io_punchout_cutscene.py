"""Blender: a whole NIS cutscene on one 30 fps timeline, and its edits back to the NIS.

Import (nlg_cutscene): every character actor comes through the fighter importer (mesh,
bind-pose rig, materials, face shapes), local props through the model-set importer, and
each shot's clip is keyed onto one timeline at the shot's own frames. Clip face-morph
tracks drive the character's shape keys. One camera follows every shot's NIS camera and a
marker names each shot. The arena is the fighter's circuit by default (the game loads it;
the NIS does not name it). Helpers without any rig (Dummy*, FX_*) are shown as empties.

Export: actor rigs, prop placements and camera keys are sampled at the shot frames they own,
turned back into node-local values and compared with the source clips; only real changes
are written (static tracks may become per-frame). Frame counts, shots, helpers and face
morph tracks stay locked. Runtime placement, IK and foot planting are not simulated.
"""
import json
import math
import os
import uuid
from pathlib import Path

import bpy
from mathutils import Matrix, Quaternion, Vector

import nlg_cinematic
import nlg_cutscene as cut
import po_action
import po_errors

ROOT = "po_cutscene"
ACTOR = "po_cutscene_actor"
CAMERA = "po_cutscene_camera"
SCHEMA = 1
SENSOR = 24.0     # mm; vertical fit at the NIS authored aspect (PAL projection consumer)


def _json(value):
    try: return json.loads(value)
    except (TypeError, ValueError): return None


def _mat(t, q, s):
    return Matrix.LocRotScale(Vector(t), Quaternion((q[3], q[0], q[1], q[2])), Vector(s))


def _collection(name, parent):
    col = bpy.data.collections.new(name); parent.children.link(col); return col


def _move(obj, col):
    for c in list(obj.users_collection): c.objects.unlink(obj)
    col.objects.link(obj)


def _frame_map(appearances):
    """{blender frame: (shot, clip, clip frame)}; a later shot owns frames it shares."""
    out = {}
    for shot, clip in sorted(appearances, key=lambda sc: (sc[0].first, sc[0].section, sc[0].index)):
        for i in range(clip.frames): out[shot.first + i + 1] = (shot, clip, i)
    return out


def _parents(actor, clip):
    return cut._parents(actor, clip)


class _Poser:
    """World node matrices per clip frame (decoded values cached per clip)."""
    def __init__(self, actor):
        self.actor = actor; self.values = {}

    def world(self, clip, frame):
        if clip not in self.values: self.values[clip] = cut.clip_values(clip)
        parents, translations = _parents(self.actor, clip)
        local = cut.local_pose(clip, self.values[clip], translations, frame)
        world = [None] * clip.node_count
        for n in cut.order(parents):
            m = _mat(*local[n]); world[n] = m if parents[n] < 0 else world[parents[n]] @ m
        return world


def _write(action, slot, path, frames, rows, group=None):
    for k in range(len(rows[0])):
        fc = po_action.new_fcurve(action, path, index=k, group=group, slot=slot)
        fc.keyframe_points.add(len(frames))
        fc.keyframe_points.foreach_set("co", [v for f, r in zip(frames, rows) for v in (f, r[k])])
        fc.update()


def _continuous(quats):
    out = []
    for q in quats:
        if out and sum(a * b for a, b in zip(out[-1], q)) < 0: q = tuple(-x for x in q)
        out.append(tuple(q))
    return out


def _key_armature(arm, bones, fmap, poser, name):
    """bones: {bone name: rig node}. Pose world of each bone = the node's world matrix."""
    rest = {b: arm.data.bones[b].matrix_local.copy() for b in bones}
    parent = {b: (arm.data.bones[b].parent.name if arm.data.bones[b].parent else None) for b in bones}
    frames = sorted(fmap)
    rows = {b: ([], [], []) for b in bones}
    for f in frames:
        shot, clip, i = fmap[f]
        world = poser.world(clip, i)
        for b, n in bones.items():
            p = parent[b]
            if p is None or p not in bones:
                basis = rest[b].inverted() @ world[n]
            else:
                basis = rest[b].inverted() @ rest[p] @ world[bones[p]].inverted() @ world[n]
            t, q, s = basis.decompose()
            rows[b][0].append(tuple(t)); rows[b][1].append((q.w, q.x, q.y, q.z)); rows[b][2].append(tuple(s))
    action, slot = po_action.new_action(arm, name)
    for b in bones:
        pb = arm.pose.bones[b]; pb.rotation_mode = "QUATERNION"
        base = pb.path_from_id()
        _write(action, slot, base + ".location", frames, rows[b][0], b)
        _write(action, slot, base + ".rotation_quaternion", frames, _continuous(rows[b][1]), b)
        if any(abs(x - 1.0) > 1e-6 for r in rows[b][2] for x in r):
            _write(action, slot, base + ".scale", frames, rows[b][2], b)
    return action


def _key_object(obj, node, fmap, poser, name):
    frames = sorted(fmap); loc, rot, scl = [], [], []
    for f in frames:
        shot, clip, i = fmap[f]
        t, q, s = poser.world(clip, i)[node].decompose()
        loc.append(tuple(t)); rot.append((q.w, q.x, q.y, q.z)); scl.append(tuple(s))
    obj.rotation_mode = "QUATERNION"
    action, slot = po_action.new_action(obj, name)
    _write(action, slot, "location", frames, loc); _write(action, slot, "rotation_quaternion", frames, _continuous(rot))
    _write(action, slot, "scale", frames, scl)


def _key_visibility(objs, fmap, total):
    """Hide an actor on frames no shot gives it (constant keys at the changes)."""
    shown = [f in fmap for f in range(1, total + 1)]
    if all(shown): return
    for obj in objs:
        for f in range(1, total + 1):
            if f == 1 or shown[f - 1] != shown[f - 2]:
                obj.hide_viewport = obj.hide_render = not shown[f - 1]
                obj.keyframe_insert("hide_viewport", frame=f); obj.keyframe_insert("hide_render", frame=f)
        obj.hide_viewport = obj.hide_render = False


def _morph_weights(clip):
    """frames x channels from the clip's 0x7009 counts and 0x700B u8 keys (as nlg_morph)."""
    import nlg_animation, struct
    a = clip.archive
    try:
        counts = struct.unpack(">%dI" % (len(a.get_chunk_bytes(nlg_animation._one(a, clip.chunks, 0x7009))) // 4),
                               a.get_chunk_bytes(nlg_animation._one(a, clip.chunks, 0x7009)))
        stream = a.get_chunk_bytes(nlg_animation._one(a, clip.chunks, 0x700B))
        hashes = a.get_chunk_bytes(nlg_animation._one(a, clip.chunks, 0x700A))
    except nlg_animation.AnimationError:
        return None, None
    rows = []
    for f in range(clip.frames):
        row = []; o = 0
        for cnt in counts:
            row.append(min(stream[o + min(f, cnt - 1)] / 255.0, 1.0) if cnt else 0.0); o += cnt
        rows.append(row)
    return rows, list(struct.unpack(">%dI" % (len(hashes) // 4), hashes))


def _key_morphs(mesh, source, fmap):
    keys = mesh.data.shape_keys
    if keys is None: return
    # Blender initializes newly-created shape-key values from the active import state.
    # A cutscene only writes curves for channels present in its 0x7009/0x700B stream, so
    # omitted channels must explicitly remain at the game's neutral value.  DK's pre-fight
    # clips carry channels 0..13; leaving 14..19 untouched enabled every damage shape.
    for key in keys.key_blocks[1:]:
        key.value = 0.0
    import nlg_hash, nlg_morph
    try: morph = nlg_morph.Morphs(source, nlg_hash.find_hashid_bin(source)[0])
    except Exception as ex:
        print("PunchOut cutscene: no face shapes for %s: %s" % (os.path.basename(source), ex)); return
    frames = sorted(fmap); curves = {}
    cache = {}
    for f in frames:
        shot, clip, i = fmap[f]
        if clip not in cache: cache[clip] = _morph_weights(clip)
        rows, hashes = cache[clip]
        if not rows: continue
        for ch_clip, w in enumerate(rows[i]):
            ch = morph.model_channel(hashes if hashes and len(hashes) == len(rows[i]) else None, ch_clip)
            if ch is None or not morph.channel_deltas(ch): continue
            bp = morph.bp[ch]
            names = morph.key_names(ch)
            if len(bp) == 1: vals = [w / bp[0]]
            elif w <= bp[0]: vals = [w / bp[0], 0.0]
            else: t = (w - bp[0]) / (bp[1] - bp[0]); vals = [1.0 - t, t]
            for kn, v in zip(names, vals):
                if keys.key_blocks.get(kn) is not None: curves.setdefault(kn, {})[f] = v
    if not curves: return
    action, slot = po_action.new_action(keys, mesh.name + " face")
    for kn, values in curves.items():
        fs = sorted(values)
        _write(action, slot, 'key_blocks["%s"].value' % kn, fs, [(values[f],) for f in fs])


def _meta(cs, actor, role, **extra):
    return json.dumps(dict({"schema": SCHEMA, "source": str(cs.path), "sha256": cs.doc.source_hashes,
                            "id": actor.ident, "name": actor.name, "kind": actor.kind, "role": role,
                            "resolved": actor.note, "archive": actor.source}, **extra))


def _character(cs, actor, col, fmap, options, shared):
    import io_import_punchout
    before = set(bpy.data.objects)
    try:
        arm, mesh = io_import_punchout.do_import(actor.source, do_anims=False, do_textures=options["load_textures"],
                                                 do_outline=options["outline"])
    except Exception as ex:           # rigs the fighter path cannot read (ring ropes): model sets instead
        for o in [o for o in bpy.data.objects if o not in before]: bpy.data.objects.remove(o)
        print("PunchOut cutscene: %s through model sets (%s)" % (os.path.basename(actor.source), ex))
        import nlg_asset
        doc = shared.setdefault(("doc", actor.source), None) or nlg_asset.AssetDocument(actor.source, cs.names)
        shared[("doc", actor.source)] = doc
        sets = [m for s in doc.sections for m in s.model_sets if m.skinned and any(h in actor.rig.hashes for h, _ in m.bones)]
        if not sets: raise
        return _model_sets(cs, actor, doc, sets, col, fmap, options, shared, "actor")
    made = [o for o in bpy.data.objects if o not in before and not o.name.startswith("PO_TEV_KeyVector")]
    for o in made: _move(o, col)
    arm.name = actor.name if actor.instance == 0 else "%s.%d" % (actor.name, actor.instance)
    mesh.name = arm.name + " mesh"
    bones = {b.name: int(b["po_node"]) for b in arm.data.bones if "po_node" in b and int(b["po_node"]) < actor.rig.node_count}
    _key_armature(arm, bones, fmap, _Poser(actor), arm.name)
    _key_morphs(mesh, actor.source, fmap)
    arm[ACTOR] = _meta(cs, actor, "actor rig"); mesh[ACTOR] = _meta(cs, actor, "actor mesh")
    return [arm, mesh]


def _prop(cs, actor, col, fmap, options, shared):
    return _model_sets(cs, actor, cs.doc, actor.model_sets, col, fmap, options, shared, "prop")


def _model_sets(cs, actor, doc, model_sets, col, fmap, options, shared, role):
    """Model sets on the actor's rig: skinned sets get a bind-pose rig keyed per node, static
    meshes follow their node. Mesh data is shared between instances of one prop."""
    import io_punchout_asset as asset
    from io_punchout_animation import bind_armature
    rig = actor.rig; poser = _Poser(actor); objs = []
    label = actor.name if actor.instance == 0 else "%s.%d" % (actor.name, actor.instance)
    for ms in model_sets:
        section = next(s.index for s in doc.sections if ms in s.model_sets)
        arm = bones = None
        if ms.skinned and ms.bones:
            arm, names = bind_armature(doc, ms, label + " rig", col)
            bones = {}
            for h, bname in names.items():
                if h in rig.hashes:
                    n = rig.hashes.index(h); arm.data.bones[bname]["po_node"] = n; bones[bname] = n
            if bones: _key_armature(arm, bones, fmap, poser, label)
            arm[ACTOR] = _meta(cs, actor, role + " rig", model_set=ms.index); objs.append(arm)
        followed = {mesh.index: (rig.hashes.index(ms.node_of(mesh.index).name_hash)
                                 if ms.node_of(mesh.index).name_hash in rig.hashes else None)
                    for mesh in ms.meshes}
        has_followers = any(n is not None for n in followed.values())
        for mesh in ms.meshes:
            key = (str(doc.path), section, ms.index, mesh.index)
            meta = doc.mesh_metadata(section, ms, mesh)
            if key not in shared:
                md = asset._build_mesh(doc, section, ms, mesh, meta, "%s:%s" % (label, mesh.index))
                md.materials.append(asset._material(doc, section, ms, mesh, meta, shared.setdefault(("materials", str(doc.path)), {}),
                                                    shared.setdefault(("images", str(doc.path)), {}), options["load_textures"]))
                shared[key] = md
            obj = bpy.data.objects.new("%s:%d" % (label, mesh.index), shared[key]); col.objects.link(obj)
            obj.matrix_world = asset._blender_matrix(ms.transforms[mesh.transform])
            n = followed[mesh.index]
            if arm is not None and mesh.palette:
                asset._skin(obj, mesh, names, arm)
            elif n is not None:
                _key_object(obj, n, fmap, poser, obj.name)
            elif arm is None and has_followers:
                # One local model set can hold alternate pieces for several two-node prop
                # rigs.  An unmatched piece is not part of this actor instance.  Rendering it
                # created one static banana bunch at the origin for every animated bunch.
                obj.hide_set(True); obj.hide_render = True
            obj[ACTOR] = _meta(cs, actor, role + " mesh", model_set=ms.index, mesh=mesh.index, node=n,
                               skinned=bool(arm is not None and mesh.palette))
            if role == "prop" and options.get("outline"):
                import io_import_punchout
                io_import_punchout.add_outline(obj)
            objs.append(obj)
    return objs


def _helper(cs, actor, col, fmap):
    obj = bpy.data.objects.new(actor.name if actor.instance == 0 else "%s.%d" % (actor.name, actor.instance), None)
    obj.empty_display_type = "ARROWS"; obj.empty_display_size = 0.1; col.objects.link(obj)
    clip = actor.appearances[0][1]
    _key_object(obj, clip.node_count - 1, fmap, _Poser(actor), obj.name)
    obj[ACTOR] = _meta(cs, actor, "helper", note="No rig anywhere has this node count/signature; hierarchy assumed, not exported.")
    _effect_helper(cs, actor, obj, col)
    return [obj]


def _effect_helper(cs, actor, helper, col):
    """Link uniquely named fighter effects to helper transforms for inspection.
    No particle lifetime, trigger timing or ambiguous name mapping is invented."""
    if not cs.root or not actor.name.lower().startswith("fx_"):
        return
    import nlg_asset
    from nlg_effect import Effects
    fighter = cs.path.parent.name.lower().removesuffix("2")
    source = cs.root / "effects" / (fighter + ".dict")
    if not source.is_file():
        return
    cache = cs.__dict__.setdefault("_effect_groups", {})
    if str(source) not in cache:
        doc = nlg_asset.AssetDocument(source, cs.names)
        cache[str(source)] = [(section.index, fx) for section in doc.sections
                              for fx in Effects(section.archive, cs.names).groups if not fx.reason]
    token = actor.name.lower()[3:]
    matches = [(section, fx) for section, fx in cache[str(source)]
               if (cs.names.get(fx.name_hash, "").lower() == token or
                   cs.names.get(fx.name_hash, "").lower().endswith("_" + token))]
    helper["po_effect_archive"] = str(source)
    helper["po_effect_candidates"] = json.dumps([
        {"section": section, "group": fx.index, "name": cs.names.get(fx.name_hash)} for section, fx in matches])
    if len(matches) != 1:
        helper["po_effect_link_note"] = "No unique effect name match; runtime binding unresolved."
        return
    section, fx = matches[0]
    helper["po_effect_link_note"] = "Unique suffix match; attached emitter markers only, runtime trigger unverified."
    for binding_set in fx.binding_sets:
        for binding in binding_set.bindings:
            emitter = fx.emitters[binding.emitter_index]
            origin = binding.origin_preview(emitter)
            marker = bpy.data.objects.new("%s emitter %d" % (actor.name, emitter.index), None)
            col.objects.link(marker); marker.parent = helper
            marker.empty_display_type = "SPHERE"; marker.empty_display_size = 0.04
            marker.location = origin or (0, 0, 0)
            marker["po_effect_binding"] = json.dumps({"archive": str(source), "section": section,
                "group": fx.index, "emitter": emitter.index, "texture": emitter.texture_hash,
                "attachment": binding.attachment_hash, "origin_known": origin is not None})


def _runtime_model_instance(doc, arena_objects, parent, model_set, rig_name, clip_name,
                            name, matrix, frames=371, rate=1.0, phase=0.0,
                            source_record=None):
    """Instance an authored arena model and its exact local rig/clip channels.

    Placement comes from the flagged ``0x6000`` runtime record in ``gameworld.dict`` and is
    kept on one labelled root Empty.  Everything below it is decoded source data and remains
    directly editable.
    """
    section = next(s for s in doc.sections if any(ms.index == model_set for ms in s.model_sets))
    source_set = next(ms for ms in section.model_sets if ms.index == model_set)
    rig = next(r for r in section.animations.rigs if r.name == rig_name)
    clip = next(c for c in section.animations.clips if c.name == clip_name and c.signature == rig.signature)
    clip.compatible(rig)
    templates = []
    for obj in arena_objects:
        if obj.type != "MESH" or "po_asset" not in obj: continue
        meta = _json(obj["po_asset"])
        if meta and meta.get("model_set") == model_set: templates.append((obj, meta))
    if not templates: return []

    import io_punchout_asset as asset
    root = bpy.data.objects.new(name, None); parent.objects.link(root)
    root.matrix_world = asset._blender_matrix(matrix)
    root["po_runtime_asset"] = json.dumps({"source": str(doc.path), "model_set": model_set,
        "rig": rig.index, "clip": clip.index, "clip_name": clip.name,
        "animation_rate": rate, "animation_phase": phase,
        "placement": "decoded 0x6000 world-definition matrix", "world_record": source_record})
    nodes = {}
    for node in rig.order:
        obj = bpy.data.objects.new("%s node %02d" % (name, node), None); parent.objects.link(obj)
        obj.empty_display_type = "PLAIN_AXES"; obj.empty_display_size = .08
        obj.parent = root if rig.parents[node] < 0 else nodes[rig.parents[node]]
        obj.location = rig.translations[node]; obj.rotation_mode = "QUATERNION"; nodes[node] = obj

        channels = {t.kind: t for (n, _), t in clip.tracks.items() if n == node and t.kind in
                    ("translation", "rotation", "scale")}
        if channels:
            action, slot = po_action.new_action(obj, "%s · %s" % (name, clip.name))
            scene_frames = list(range(1, frames + 1))
            for kind, track in channels.items():
                raw = track.values()
                def sample(frame):
                    if track.keys == 1: return raw[0]
                    t = ((frame - 1) * rate + phase * clip.frames) % len(raw)
                    i = int(math.floor(t)); amount = t - i
                    a, b = raw[i], raw[(i + 1) % len(raw)]
                    if kind == "rotation":
                        qa = Quaternion((a[3], a[0], a[1], a[2]))
                        qb = Quaternion((b[3], b[0], b[1], b[2]))
                        q = qa.slerp(qb, amount)
                        return (q.x, q.y, q.z, q.w)
                    return tuple(x + (y - x) * amount for x, y in zip(a, b))
                values = [sample(f) for f in scene_frames]
                if kind == "rotation":
                    values = [(q[3], q[0], q[1], q[2]) for q in values]
                    _write(action, slot, "rotation_quaternion", scene_frames, values)
                elif kind == "translation": _write(action, slot, "location", scene_frames, values)
                else: _write(action, slot, "scale", scene_frames, values)

    cumulative = {}
    for node in rig.order:
        local = Matrix.Translation(Vector(rig.translations[node]))
        cumulative[node] = (cumulative[rig.parents[node]] @ local if rig.parents[node] >= 0 else local)
    made = [root, *nodes.values()]
    binding_by_name = {doc.name(b["proxy_hash"]).rsplit("/", 1)[-1].lower(): b["node"]
                       for b in source_record["bindings"]}
    for template, meta in templates:
        mesh_index = meta["mesh"]["index"] if isinstance(meta["mesh"], dict) else meta["mesh"]
        source_name = doc.name(source_set.node_of(mesh_index).name_hash).rsplit("/", 1)[-1].lower()
        node = binding_by_name[source_name]
        obj = bpy.data.objects.new("%s · %s" % (name, template.name), template.data)
        parent.objects.link(obj); obj.parent = nodes[node]
        obj.matrix_local = cumulative[node].inverted() @ template.matrix_world
        obj["po_runtime_asset"] = root["po_runtime_asset"]
        obj["po_runtime_node"] = node
        made.append(obj)
    return made


def _worldcircuit_preview(arena_doc, arena_root, arena_objects, frames):
    """Instantiate every animated arena object from its decoded 0x6000 world record."""
    col = _collection("World Circuit authored runtime assets", arena_root)
    section = arena_doc.sections[0]
    decoded = cut.arena_runtime_instances(section.archive, section.animations.rigs)
    roles = {"longbanner": (1, "longbanner"), "longbanner2": (2, "longbanner2"),
             "longbanner3": (3, "longbanner3"), "laser2": (4, "laser2"),
             "redbanner": (5, "redbanner")}
    made = []; seen = {}
    for record in decoded:
        if record["rig_name"] not in roles: continue
        model_set, clip = roles[record["rig_name"]]
        n = seen.get(record["rig_name"], 0); seen[record["rig_name"]] = n + 1
        label = "%s world instance %02d" % (record["rig_name"], n)
        source_record = {k: record[k] for k in
                         ("chunk", "offset", "matrix_offset", "animation_rate",
                          "animation_phase", "bindings")}
        made += _runtime_model_instance(arena_doc, arena_objects, col, model_set,
            record["rig_name"], clip, label, record["matrix"], frames,
            rate=record["animation_rate"], phase=record["animation_phase"],
            source_record=source_record)
    col["po_runtime_counts"] = json.dumps(seen, sort_keys=True)
    return made


def _impostor_crowd(cs, arena_doc, arena_root, scene, camera, notes):
    """The arena crowd as the game builds it: camera-facing impostor billboards (po_crowd).

    Layout, roster, tint and pose logic come from po_crowd (decoded from main.dol).  Only the
    ring-corner regions are spawned for World Circuit (see po_crowd.RING_SIDE_RADIUS).  One
    sprite atlas is baked per camera shot and held across it; the "Bake crowd animation"
    operator re-bakes every Nth frame.  Atlases are cached outside the repository."""
    import po_crowd
    arena = cs.arena_name
    if arena not in po_crowd.CIRCUITS or not cs.root:
        return None
    helpers = cut.arena_crowd_helpers(arena_doc.sections[0].archive)
    if not helpers:
        return None
    files_root = cs.root.parent
    out_dir = po_crowd.cache_dir(cs.path, arena)
    col = _collection("PO crowd impostors", arena_root)
    obj, members = po_crowd.build(scene, helpers, str(files_root), arena, col, camera, image_dir=out_dir,
                                  names=cs.names, keep_helpers=po_crowd.ring_side_helpers(arena, helpers))
    obj["po_crowd_source"] = str(cs.path)
    frames = sorted({shot.first + 1 + shot.camera.frames // 2 for shot in cs.shots
                     if shot.camera is not None and not shot.camera.limitation}) or [scene.frame_start]
    try:
        po_crowd.bake(scene, obj, frames=frames, out_dir=out_dir)
        notes.append("Ring-side crowd: %d impostors from %d authored regions, baked once per camera shot "
                     "(%d atlases, cached at %s). Use 'Bake crowd animation' for motion."
                     % (len(members), len({m["helper"] for m in members}), len(frames), out_dir))
    except Exception as ex:                      # the layout is still useful without sprites
        notes.append("Crowd impostors laid out (%d) but not baked: %s" % (len(members), ex))
    return obj


def _arena_effect_sprites(cs, arena_doc, arena_root, scene, camera, notes):
    """Lamp glows and stars: the arena's authored effect placements drawn with the effect data."""
    import po_fxsprites
    effects_dict = cs.root / "effects" / "effects.dict" if cs.root else None
    if effects_dict is None or not effects_dict.is_file():
        return []
    placed = cut.arena_effect_placements(arena_doc.sections[0].archive, po_fxsprites.SUPPORTED)
    if not placed:
        return []
    col = _collection("Arena effect sprites", arena_root)
    made = []
    for name in po_fxsprites.SUPPORTED:
        rows = [p for p in placed if p["effect"] == name]
        if rows:
            made += po_fxsprites.build(scene, col, camera, effects_dict, cs.names, rows, name)
    if made:
        notes.append("Arena effect sprites: %d authored placements drawn with their effect emitters (%s); "
                     "emitter phase is a pseudo-random steady state." %
                     (sum(1 for p in placed), ", ".join(sorted({p["effect"] for p in placed}))))
    return made


def _camera(cs, col):
    data = bpy.data.cameras.new("Cutscene camera"); data.sensor_fit = "VERTICAL"; data.sensor_height = SENSOR
    data.clip_start = 0.05; data.clip_end = 500.0
    cam = bpy.data.objects.new("Cutscene camera", data); col.objects.link(cam); cam.rotation_mode = "QUATERNION"
    fmap = {}; shots = []
    for shot in cs.shots:
        if shot.camera is None or shot.camera.limitation: continue
        shots.append([shot.section, shot.camera.index, shot.first, shot.camera.name])
        for i in range(shot.camera.frames): fmap[shot.first + i + 1] = (shot, i)
    frames = sorted(fmap)
    if frames:
        loc, rot, lens = [], [], []
        for f in frames:
            shot, i = fmap[f]; tr = shot.camera.tracks
            loc.append(tuple(tr["position"][i])); x, y, z, w = tr["rotation_xyzw"][i]; rot.append((w, x, y, z))
            fov = max(1e-4, min(3.1, tr["angle_radians"][i][0])); lens.append((SENSOR / 2 / math.tan(fov / 2),))
        action, slot = po_action.new_action(cam, "Cutscene camera")
        _write(action, slot, "location", frames, loc); _write(action, slot, "rotation_quaternion", frames, _continuous(rot))
        daction, dslot = po_action.new_action(data, "Cutscene lens")
        _write(daction, dslot, "lens", frames, lens)
    cam[CAMERA] = json.dumps({"schema": SCHEMA, "source": str(cs.path), "sha256": cs.doc.source_hashes, "shots": shots,
                              "sensor": SENSOR, "fov": "vertical (unverified)"})
    return cam


def import_cutscene(path, arena="AUTO", load_textures=True, outline=True):
    arena = None if arena in ("AUTO", "", None) else arena
    cs = cut.Cutscene(path, arena=None if arena == "NONE" else arena)
    scene = bpy.context.scene
    # Reversible cleanup of the startup trio; leave other scene objects alone.
    for name, kind in (("Cube", "MESH"), ("Light", "LIGHT"), ("Camera", "CAMERA")):
        obj = scene.objects.get(name)
        if obj is not None and obj.type == kind and not any(k.startswith("po_") for k in obj.keys()):
            obj.hide_set(True)
            obj.hide_render = True
    scene.render.fps = cut.FPS; scene.render.fps_base = 1.0
    scene.frame_start = 1; scene.frame_end = cs.frames
    root = _collection("%s · cutscene" % cs.path.stem, scene.collection)
    cols = {k: _collection(label, root) for k, label in (("character", "Actors"), ("prop", "Props"),
                                                       ("helper", "Helpers"), ("camera", "Camera"))}
    options = {"load_textures": load_textures, "outline": outline}; shared = {}; notes = []
    for actor in cs.actors:
        fmap = _frame_map(actor.appearances)
        with po_errors.context(archive=str(cs.path), mesh=actor.name):
            try:
                if actor.kind == "character": objs = _character(cs, actor, _collection(actor.name, cols["character"]), fmap, options, shared)
                elif actor.kind == "prop" and actor.model_sets: objs = _prop(cs, actor, cols["prop"], fmap, options, shared)
                else: objs = _helper(cs, actor, cols["helper"], fmap)
            except Exception as ex:          # one broken actor must not lose the rest of the scene
                notes.append("%s: %s" % (actor.name, ex)); print("PunchOut cutscene: %s skipped: %s" % (actor.name, ex)); continue
        _key_visibility([o for o in objs if o.type != "ARMATURE"], fmap, cs.frames)
    # A pre-fight NIS is the undamaged roster state.  Keep this explicit even when a
    # fighter archive also contains hurt textures/meshes for later gameplay swaps.
    scene["po_show_damage"] = False
    for mat in bpy.data.materials:
        mix = mat.node_tree.nodes.get("PO_DamageMix") if mat.use_nodes else None
        if mix is not None: mix.inputs[0].default_value = 0.0
        vis = mat.node_tree.nodes.get("PO_DamageVis") if mat.use_nodes else None
        if vis is not None: vis.inputs[0].default_value = 0.0
    cam = _camera(cs, cols["camera"]); scene.camera = cam
    aspects = [cut.camera_aspect(cs.doc.sections[shot.section].archive, shot.camera)
               for shot in cs.shots if shot.camera is not None and not shot.camera.limitation]
    if aspects and max(aspects)-min(aspects) < 1e-5:
        scene.render.resolution_y = 1080
        scene.render.resolution_x = round(1080 * aspects[0])
        scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1.0
        info = json.loads(cam[CAMERA])
        info.update(fov="vertical at authored aspect (PAL projection consumer)", authored_aspect=aspects[0])
        cam[CAMERA] = json.dumps(info)
    elif aspects:
        notes.append("Camera shots have differing authored aspects; output-aspect conversion remains unresolved.")
    for shot in cs.shots:
        m = scene.timeline_markers.new("S%d.%d %s" % (shot.section, shot.index, shot.camera.name if shot.camera else "-"),
                                       frame=shot.first + 1)
        m.camera = cam
    arena_path = cs.root / "environments" / cs.arena_name / "gameworld.dict" if cs.root and cs.arena_name else None
    if arena_path and arena_path.is_file() and arena != "NONE":
        import io_punchout_asset
        arena_doc, arena_root, arena_objects = io_punchout_asset.import_asset(str(arena_path), load_textures)
        # The spatial tree places static scenery. Other model sets are reusable
        # animation assets; their bind transforms are not runtime placements.
        unplaced = []
        for obj in arena_objects:
            meta = json.loads(obj["po_asset"])
            section = meta["source"]["section"]
            ms = arena_doc.sections[section].model_sets[meta["model_set"]]
            # Not every non-spatial object is a template (e.g. a gym chair).
            # Restrict this to separately scoped assets with a matching animation rig.
            rigs = arena_doc.sections[section].animations.rigs
            animated_asset = ms.index > 0 and any(mesh.name_hash in rig.hashes
                                                  for mesh in ms.meshes for rig in rigs)
            if animated_asset:
                unplaced.append(obj)
        if unplaced and len(unplaced) < len(arena_objects):
            templates = _collection("Unplaced arena assets (hidden)", arena_root)
            for obj in unplaced:
                _move(obj, templates)
            templates.hide_viewport = True
            templates.hide_render = True
            notes.append("Hidden %d arena animation templates at their non-runtime authoring transforms." % len(unplaced))
        if cs.arena_name == "worldcircuit":
            runtime = _worldcircuit_preview(arena_doc, arena_root, arena_objects, cs.frames)
            if runtime:
                notes.append("Instanced authored World Circuit banner/laser meshes and source clips at decoded 0x6000 world matrices.")
            if load_textures:
                _impostor_crowd(cs, arena_doc, arena_root, scene, cam, notes)
        if load_textures:
            try:
                _arena_effect_sprites(cs, arena_doc, arena_root, scene, cam, notes)
            except Exception as ex:                  # sprites are decoration; never lose the scene
                notes.append("Arena effect sprites failed: %s" % ex)
        rope = cs.root / "characters" / cut.ROPES.get(cs.arena_name, "missing.dict")
        loaded = {Path(a.source).resolve() for a in cs.actors if a.source}
        if rope.is_file() and rope.resolve() not in loaded:
            io_punchout_asset.import_asset(str(rope), load_textures)
            notes.append("Loaded circuit ropes: %s (bind pose; no rope actor in this NIS)." % rope.name)
        notes.append("Arena %s is the fighter's circuit (the NIS does not name it)." % cs.arena_name)
    elif load_textures:
        import io_punchout_asset
        io_punchout_asset._game_view()
    if load_textures:
        # Replace the preview graphs with the exact GX/TEV programs recovered from main.dol
        # (po_gx): arena, fighters, props, ropes and crowd shaders, in display-encoded space.
        try:
            import po_gx
            # The script-set character light (80412D54) is only known for some NIS files;
            # a calibrated entry replaces the Glass Joe value where it is not.
            light = po_gx.CALIBRATED_NIS_LIGHTS.get(("%s/%s" % (cs.path.parent.name, cs.path.stem)).lower())
            gx = po_gx.rebuild_scene(scene, ctx={"light": light} if light else None)
            import io_import_punchout
            io_import_punchout.outline_view_raw()     # po_gx renders with the Raw view transform
            notes.append("Exact GX materials: " + ", ".join("%s %d" % kv for kv in sorted(gx.items())))
            if light:
                notes.append("Character light is calibrated against retail footage (the NIS script light is not decoded).")
        except Exception as ex:
            notes.append("Exact GX materials failed: %s" % ex)
    summary = cs.summary(); summary.update(schema=SCHEMA, arena=cs.arena_name, notes=notes, session=uuid.uuid4().hex)
    summary["preview_limits"] = [
        "IFL durations loop from scene frame 1; runtime trigger offsets are not decoded.",
        "Effect blend previews use texture names; GX blend states and runtime visibility remain unresolved.",
        "Fighter effect links are unique suffix matches and emitter markers, not particle simulation.",
        "Materials use the exact TEV programs, texgens, samplers and light vectors from main.dol (po_gx); the scene renders with the Raw view.",
        "Arena animation (lasers, banners) runs on the arena clock; its offset from the NIS start is a runtime value.",
        "World Circuit banner/laser mesh, UV, material, motion and 0x6000 root matrices are decoded; runtime state triggers remain unresolved.",
        "Camera uses vertical FOV at the authored aspect; changed output aspects and precise retail frame matching remain unverified."]
    root[ROOT] = json.dumps(summary)
    scene.frame_set(1)
    return cs, root, notes


# ---------------------------------------------------------------------------------------
# Export

def _curve_values(idblock, path, count, default):
    ad = idblock.animation_data
    curves = {}
    if ad is not None and ad.action is not None:
        for fc in po_action.all_fcurves(ad.action):
            if fc.data_path == path: curves[fc.array_index] = fc
    if ad is not None and (ad.drivers or ad.nla_tracks):
        raise po_errors.Refusal("drivers and NLA strips are not exported; key the action directly.", mesh=getattr(idblock, "name", ""),
                                field=path)
    return lambda f: tuple(curves[k].evaluate(f) if k in curves else default[k] for k in range(count))


def _object_world(obj):
    if obj.parent is not None or obj.constraints:
        raise po_errors.Refusal("parents and constraints on cutscene objects are not exported.", mesh=obj.name, field="parent")
    loc = _curve_values(obj, "location", 3, tuple(obj.location))
    rot = _curve_values(obj, "rotation_quaternion", 4, tuple(obj.rotation_quaternion))
    scl = _curve_values(obj, "scale", 3, tuple(obj.scale))
    if obj.rotation_mode != "QUATERNION":
        raise po_errors.Refusal("keep quaternion rotation on cutscene objects.", mesh=obj.name, field="rotation_mode")
    return lambda f: Matrix.LocRotScale(Vector(loc(f)), Quaternion(rot(f)), Vector(scl(f)))


def _armature_world(arm):
    """f -> {bone: pose world matrix}, from the keyed pose (Blender's parenting rule)."""
    if arm.constraints or any(pb.constraints for pb in arm.pose.bones):
        raise po_errors.Refusal("pose constraints are not exported; bake them into keys.", mesh=arm.name, field="constraints")
    placement = _object_world(arm) if arm.animation_data and arm.animation_data.action and any(
        fc.data_path in ("location", "rotation_quaternion", "scale") for fc in po_action.all_fcurves(arm.animation_data.action)) \
        else (lambda f, m=arm.matrix_world.copy(): m)
    bones = list(arm.data.bones); rest = {b.name: b.matrix_local.copy() for b in bones}
    chans = {}
    for b in bones:
        pb = arm.pose.bones[b.name]
        if pb.rotation_mode != "QUATERNION":
            raise po_errors.Refusal("keep quaternion rotation on actor bones.", mesh=arm.name, field="bone '%s' rotation_mode" % b.name)
        base = pb.path_from_id()
        chans[b.name] = (_curve_values(arm, base + ".location", 3, tuple(pb.location)),
                         _curve_values(arm, base + ".rotation_quaternion", 4, tuple(pb.rotation_quaternion)),
                         _curve_values(arm, base + ".scale", 3, tuple(pb.scale)))

    def at(f):
        out = {}
        for b in bones:                               # data.bones lists parents before children
            loc, rot, scl = chans[b.name]
            basis = Matrix.LocRotScale(Vector(loc(f)), Quaternion(rot(f)), Vector(scl(f)))
            if b.parent is None: out[b.name] = rest[b.name] @ basis
            else: out[b.name] = out[b.parent.name] @ rest[b.parent.name].inverted() @ rest[b.name] @ basis
        top = placement(f)
        return {k: top @ v for k, v in out.items()}
    return at


def _local(m):
    t, q, s = m.decompose()
    return tuple(t), (q.x, q.y, q.z, q.w), tuple(s)


def collect_patch_set(source):
    source = str(Path(source).resolve())
    roots = [c for c in bpy.data.collections if ROOT in c and (_json(c[ROOT]) or {}).get("source") == source]
    if len(roots) != 1: raise po_errors.Refusal("Keep exactly one cutscene import of this archive.", archive=source, field="cutscene collections")
    info = _json(roots[0][ROOT])
    cs = cut.Cutscene(source, arena=info.get("arena"))
    if info.get("sha256") != cs.doc.source_hashes: raise po_errors.Refusal("The cutscene archive changed since import.", archive=source, field="sha256")
    actors = {a.ident: a for a in cs.actors}
    owners = {}
    for obj in bpy.data.objects:
        meta = _json(obj.get(ACTOR))
        if meta and meta.get("source") == source: owners.setdefault(meta["id"], []).append((obj, meta))
    clip_edits = {}
    for ident, objs in sorted(owners.items()):
        actor = actors.get(ident)
        if actor is None: raise po_errors.Refusal("This actor is not in the source cutscene any more.", archive=source, mesh=objs[0][0].name, field=ACTOR)
        if actor.kind == "helper": continue
        with po_errors.context(archive=source, mesh=actor.name):
            _actor_edits(actor, objs, clip_edits)
    camera_edits = {}
    cams = [o for o in bpy.data.objects if (_json(o.get(CAMERA)) or {}).get("source") == source]
    if len(cams) > 1: raise po_errors.Refusal("Keep one cutscene camera.", archive=source, field=CAMERA)
    if cams: _camera_edits(cs, cams[0], camera_edits)
    return cut.patch_set(cs, clip_edits, camera_edits)


def _actor_edits(actor, objs, clip_edits):
    fmap = _frame_map(actor.appearances); poser = _Poser(actor)
    samplers = []                        # f -> {node: world}
    for obj, meta in objs:
        if meta["role"] in ("actor rig", "prop rig"):
            nodes = {b.name: int(b["po_node"]) for b in obj.data.bones if "po_node" in b}
            at = _armature_world(obj)
            samplers.append(lambda f, at=at, nodes=nodes: {n: m for b, m in at(f).items() for n in [nodes.get(b)] if n is not None})
        elif meta["role"].endswith(" mesh") and not meta.get("skinned") and meta.get("node") is not None:
            world = _object_world(obj)
            samplers.append(lambda f, world=world, n=meta["node"], name=obj.name: {n: world(f)})
    if not samplers: return
    by_clip = {}
    for f, (shot, clip, i) in fmap.items(): by_clip.setdefault((shot.section, clip.index, id(clip)), (clip, []))[1].append((f, i))
    for (si, ci, _), (clip, owned) in by_clip.items():
        parents, translations = _parents(actor, clip)
        values = cut.clip_values(clip)
        defaults = lambda n: {cut.ROTATION: (0.0, 0.0, 0.0, 1.0), cut.TRANSLATION: tuple(translations[n]), cut.SCALE: (1.0, 1.0, 1.0)}
        desired = {}
        for f, i in owned:
            world = poser.world(clip, i); sampled = set()
            for sample in samplers:
                for n, m in sample(f).items(): world[n] = m; sampled.add(n)
            for n in range(clip.node_count):
                p = parents[n]
                if n not in sampled and p not in sampled: continue      # untouched: the source value stands
                local = _local(world[n] if p < 0 else world[p].inverted() @ world[n])
                for kind, value in zip((cut.TRANSLATION, cut.ROTATION, cut.SCALE), local):
                    if (n, kind) not in desired:
                        old = values.get((n, kind))
                        desired[(n, kind)] = ([tuple(old[0 if len(old) == 1 else k]) for k in range(clip.frames)] if old
                                              else [defaults(n)[kind]] * clip.frames)
                    desired[(n, kind)][i] = value
        try:
            changes = cut.clip_changes(clip, desired, translations)
        except cut.CutsceneError as ex:
            raise po_errors.Refusal(str(ex), section=si, field="clip '%s'" % clip.name) from ex
        if changes: clip_edits.setdefault((si, ci), {}).update(changes)


def _camera_edits(cs, cam, camera_edits):
    meta = _json(cam[CAMERA])
    world = _object_world(cam)
    lens = _curve_values(cam.data, "lens", 1, (cam.data.lens,))
    if cam.data.sensor_fit != "VERTICAL" or abs(cam.data.sensor_height - meta.get("sensor", SENSOR)) > 1e-6:
        raise po_errors.Refusal("keep the imported vertical sensor fit and height; change the lens instead.", mesh=cam.name, field="sensor")
    owner = {}
    for shot in cs.shots:
        if shot.camera is None or shot.camera.limitation: continue
        for i in range(shot.camera.frames): owner[shot.first + i + 1] = (shot, i)
    edits = {}
    for f, (shot, i) in owner.items():
        key = (shot.section, shot.camera.index)
        if key not in edits: edits[key] = (shot.camera, {k: [tuple(v) for v in shot.camera.tracks[k]] for k in cut.CAMERA_TRACKS})
        t, q, _ = world(f).decompose()
        tracks = edits[key][1]
        tracks["position"][i] = tuple(t); tracks["rotation_xyzw"][i] = (q.x, q.y, q.z, q.w)
        tracks["angle_radians"][i] = (2 * math.atan(SENSOR / 2 / max(lens(f)[0], 1e-6)),)
    for key, (clip, tracks) in edits.items():
        changes = cut.camera_changes(clip, tracks)
        if changes: camera_edits[key] = changes


def export_cutscene(source, output):
    import po_cinematics
    return po_cinematics.write_patch_set(source, collect_patch_set(source), output)
